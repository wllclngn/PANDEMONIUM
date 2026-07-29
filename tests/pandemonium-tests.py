#!/usr/bin/env python3
"""
PANDEMONIUM prism-scale orchestrator.

Unified throughput + latency benchmark with Prometheus metrics output.

Usage:
    ./tests/pandemonium-tests.py prism-scale
    ./tests/pandemonium-tests.py prism-scale --iterations 3
    ./tests/pandemonium-tests.py prism-scale --schedulers scx_rusty,scx_bpfland
"""

import argparse
import bisect
import json
import os
import threading
import traceback
import re
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    SCRIPT_DIR, TARGET_DIR, LOG_DIR, ARCHIVE_DIR, BINARY, SOURCE_PATTERNS,
    get_version, get_git_info, DmesgMonitor,
    log, log_info, log_warn, log_error, run_cmd,
    has_root_owned_files, clean_root_files, check_sources_changed, build,
    SCX_OPS, is_scx_active, scx_scheduler_name,
    wait_for_activation, wait_for_deactivation, wait_for_no_scheduler,
    set_cpu_online, restrict_cpus, restore_all_cpus, CpuGuard,
    get_possible_cpus, get_online_cpus, compute_core_counts,
    mean_stdev, percentile, mean as sub_mean, variance as sub_variance,
    montauk_trace, montauk_available, MONTAUK,
    PrometheusBuilder, table_header, table_row,
    stall_susceptibility,
    install_exit_guard, register_exit_cleanup,
    cpu_release_deprecated,
 warm_sudo, refresh_sudo,
)
from ipc_workload import (
    measure_ipc_cell, IPC_DEFAULT_ROUNDS, IPC_RTT_PRIMS, IPC_COMM,
)


# CONFIGURATION

DEFAULT_EXTERNALS = []

# montauk comm for the latency probe (run_probe sets it via prctl) -- lets montauk
# target JUST the probe in the longrun/mixed phases, not the 16 stress workers.
PROBE_COMM = "pand-probe"


# BUILD HELPERS

def fix_ownership():
    uid = os.environ.get("SUDO_UID", str(os.getuid()))
    gid = os.environ.get("SUDO_GID", str(os.getgid()))
    log_info(f"Fixing ownership to {uid}:{gid}...")
    for d in [TARGET_DIR, LOG_DIR]:
        if d.exists():
            subprocess.run(
                ["chown", "-R", f"{uid}:{gid}", str(d)],
                capture_output=True,
            )


def nuke_stale_build():
    """Nuke the build dir if any source file is newer than the binary."""
    if not TARGET_DIR.exists():
        return
    if not BINARY.exists():
        log_info(f"Nuking build directory (no binary): {TARGET_DIR}")
        subprocess.run(["sudo", "rm", "-rf", str(TARGET_DIR)],
                       capture_output=True)
        return
    bin_mtime = BINARY.stat().st_mtime
    for pattern in SOURCE_PATTERNS:
        for src in SCRIPT_DIR.glob(pattern):
            if src.stat().st_mtime > bin_mtime:
                log_warn(f"Source changed: {src.relative_to(SCRIPT_DIR)}")
                log_info(f"Nuking stale build directory: {TARGET_DIR}")
                subprocess.run(["sudo", "rm", "-rf", str(TARGET_DIR)],
                               capture_output=True)
                return


# SCHEDULER PROCESS MANAGEMENT

def find_scheduler(name: str) -> str | None:
    return shutil.which(name)


# Active scheduler guards, force-ejected on interrupt or normal exit. sched_ext
# schedulers run in their OWN process group (start_scheduler: preexec_fn=os.setpgrp),
# so a Ctrl+C to prism's foreground group never reaches them, and __del__-based cleanup
# does NOT reliably run on KeyboardInterrupt or interpreter exit -- without this an
# interrupted run leaves the scheduler REGISTERED and the system stays on it. stop()
# escalates SIGINT -> SIGKILL, and a SIGKILL'd loader makes the kernel auto-unregister,
# so this ejects even a hung scheduler.
_ACTIVE_GUARDS: set = set()


def _eject_active_schedulers():
    for g in list(_ACTIVE_GUARDS):
        try:
            g.stop()
        except Exception:
            pass


# CANONICAL exit cleanup -- pandemonium_common.install_exit_guard is the suite's
# ONE Ctrl+C / exit handler. Register this bench's scheduler-guard teardown as an
# extra cleanup; the robust eject (systemctl + force-kill by comm) and the CPU
# re-online are the module's job. The old hand-rolled _cleanup_on_exit /
# _fatal_signal_eject ejected only the spawned guards via a process-group SIGINT --
# which missed the systemd scheduler and left the box stuck on a CPU subset. Gone.
register_exit_cleanup(_eject_active_schedulers)
install_exit_guard()


def stop_systemd_scheduler() -> None:
    """Stop the systemd `pandemonium` service -- the canonical clear of a stale sched_ext
    registration. Sudo-aware: prefixes sudo only when not already root. Use this instead of
    open-coding `systemctl stop pandemonium` (it was duplicated a dozen ways across the
    suite, half with the wrong sudo handling)."""
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    subprocess.run(prefix + ["systemctl", "stop", "pandemonium"], capture_output=True)


class SchedulerProcess:
    """RAII-style guard for a running sched_ext scheduler."""

    def __init__(self, proc: subprocess.Popen, name: str,
                 stdout_path: str | None = None,
                 stderr_path: str | None = None):
        self.proc = proc
        self.name = name
        self.pgid = os.getpgid(proc.pid)
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        _ACTIVE_GUARDS.add(self)

    def stop(self):
        _ACTIVE_GUARDS.discard(self)
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.pgid, signal.SIGINT)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def drain_stdout(self) -> str:
        """Read all stdout captured to file (call after stop)."""
        if self.stdout_path:
            try:
                return Path(self.stdout_path).read_text()
            except (FileNotFoundError, PermissionError):
                pass
        return ""

    def read_stderr(self, limit: int = 4000) -> str:
        if self.stderr_path:
            try:
                return Path(self.stderr_path).read_text()[:limit]
            except (FileNotFoundError, PermissionError):
                pass
        return ""

    def cleanup(self):
        for p in [self.stdout_path, self.stderr_path]:
            if p:
                try:
                    os.unlink(p)
                except (FileNotFoundError, PermissionError):
                    pass

    def __del__(self):
        self.stop()
        self.cleanup()


def start_scheduler(cmd: list[str], name: str) -> SchedulerProcess | None:
    """Spawn a scheduler subprocess in its own process group.
    Stdout and stderr go to files to avoid pipe buffer overflow.
    Returns None if the binary cannot be found."""
    bin_path = cmd[0] if cmd else ""
    if bin_path and not os.path.exists(bin_path) and not shutil.which(bin_path):
        log_error(f"Binary not found: {bin_path}")
        return None
    # Refresh sudo credentials before spawning (prism-scale runs are long)
    subprocess.run(["sudo", "-v"], capture_output=True)
    full_cmd = ["sudo"] + cmd
    log_info(f"Starting: {' '.join(full_cmd)}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = str(LOG_DIR / f"sched-{name}-{os.getpid()}.stdout")
    stdout_f = open(stdout_path, "w")
    stderr_path = str(LOG_DIR / f"sched-{name}-{os.getpid()}.stderr")
    stderr_f = open(stderr_path, "w")
    proc = subprocess.Popen(
        full_cmd,
        stdout=stdout_f,
        stderr=stderr_f,
        preexec_fn=os.setpgrp,
    )
    stdout_f.close()
    stderr_f.close()
    return SchedulerProcess(proc, name, stdout_path, stderr_path)


def start_and_wait(cmd: list[str], name: str,
                   settle_secs: float = 2.0) -> SchedulerProcess | None:
    """Start a scheduler, wait for sched_ext activation. Returns None on failure."""
    # Detect stale struct_ops registration before starting
    try:
        stale = SCX_OPS.read_text().strip()
        if stale:
            log_warn(f"Stale scheduler detected: '{stale}', waiting for cleanup...")
            if not wait_for_no_scheduler(timeout=15):
                log_error("stale scheduler did not unregister")
                return None
            log_info("Stale scheduler cleared")
    except (FileNotFoundError, PermissionError):
        pass
    guard = start_scheduler(cmd, name)
    if guard is None:
        return None
    if not wait_for_activation(10.0):
        log_warn(f"{name} did not activate within 10s -- skipping")
        exited = guard.proc.poll() is not None
        if exited:
            log_error(f"{name} process exited early (code {guard.proc.returncode})")
        else:
            log_warn(f"{name} process still running but sched_ext not active")
        stderr = guard.read_stderr()
        if stderr.strip():
            for line in stderr.strip().splitlines()[:30]:
                log_error(f"  {line}")
        guard.stop()
        wait_for_deactivation(5.0)
        return None
    log_info(f"{name} is active")
    time.sleep(settle_secs)
    return guard


def scheduler_active(expected: str = "pandemonium") -> bool:
    """sched_ext is registered AND it is `expected` -- the REAL kernel state, not a live
    userspace process. A watchdog ejection drops the BPF while the pandemonium process
    lingers, so proc.poll() reads alive; this reads the scx registration directly, so the
    harness can never report PANDEMONIUM while the kernel has actually fallen back to EEVDF."""
    try:
        return is_scx_active() and scx_scheduler_name() == expected
    except Exception:
        return False


def sched_ejected(guard) -> bool:
    """True when the scheduler is no longer the active scx scheduler: the process died OR the
    BPF was ejected (watchdog) and the kernel fell back to EEVDF while the process lingers.
    The proc-only `guard.proc.poll()` check missed the ejection -- that was the lie. Use this
    for every 'did the scheduler survive this phase' check."""
    if guard is None:
        return False
    return guard.proc.poll() is not None or not scheduler_active()


def measure_struct_ops_cleanup():
    """Time how long the kernel takes to fully unregister struct_ops."""
    try:
        name = SCX_OPS.read_text().strip()
        if not name:
            return
    except (FileNotFoundError, PermissionError):
        return
    t0 = time.monotonic()
    while True:
        try:
            name = SCX_OPS.read_text().strip()
            if not name:
                elapsed_ms = (time.monotonic() - t0) * 1000
                log_info(f"  struct_ops cleanup: {elapsed_ms:.0f}ms")
                return
        except (FileNotFoundError, PermissionError):
            elapsed_ms = (time.monotonic() - t0) * 1000
            log_info(f"  struct_ops cleanup: {elapsed_ms:.0f}ms (ops disappeared)")
            return
        if time.monotonic() - t0 > 30:
            log_error("struct_ops cleanup: STILL REGISTERED AFTER 30s")
            return
        time.sleep(0.01)


def stop_and_wait(guard: SchedulerProcess | None) -> str:
    """Stop a scheduler, wait for deactivation. Returns captured stdout."""
    if guard is None:
        return ""
    guard.stop()
    stdout = guard.drain_stdout()
    if not wait_for_deactivation(5.0):
        log_warn(f"sched_ext still active after stopping {guard.name}")
    measure_struct_ops_cleanup()
    time.sleep(1)
    return stdout


# GENERIC MONTAUK TRACE DRIVER -- the single body behind every `prism-* --trace`.
# Each bench supplies its comm `pattern`, a `label`, and a `body_fn(rec_dir)` that
# runs its workload; this owns the activate -> montauk --trace -> deactivate
# lifecycle so no bench re-implements it.
_XDOM_PATHS = ["sel_tight", "sel_sync", "sel_normal", "sel_dfl",
               "enq_t1", "enq_t2", "steal", "step5"]


def parse_migration_line(stdout_text: str) -> dict:
    """Parse the [MIGRATION] per-cause line -- ALL cross-CPU moves by XDOM_* path, not
    just the cross-domain subset the [KNOBS] line carries. Names which decision drives a
    within-L3 bounce (steal vs sel vs enq). {} when no [MIGRATION] line (EEVDF / external)."""
    for line in stdout_text.splitlines():
        if "[MIGRATION]" not in line:
            continue
        out = {}
        for m in re.finditer(r"(\w+)=(\d+)", line.split("[MIGRATION]")[1]):
            out[m.group(1)] = int(m.group(2))
        return out
    return {}


def _write_cross_domain_marker(guard, rec_dir):
    """Producer marker (mirrors prism's write_stability_markers): after a
    traced PANDEMONIUM run stops, parse its shutdown [KNOBS] line for the per-path
    cross-CCX attribution and write it into the recording dir. montauk_analyze
    --digest surfaces it as a CROSS-CCX PLACEMENT block, so a multi-CCX user can
    tell SEL_DFL (topology-blind fallback) from STEAL/STEP5 (dispatch-side) in one
    read instead of only seeing montauk's trace-derived scatter percentage. No-op
    for EEVDF / external schedulers (no [KNOBS] line)."""
    if guard is None or rec_dir is None:
        return
    try:
        output = guard.read_output()
    except Exception:
        return
    _write_cross_domain_marker_text(output, rec_dir)


def _write_cross_domain_marker_text(output, rec_dir):
    """Write the cross-domain + migration markers from already-captured scheduler
    stdout. The IPC path drains the guard via stop_and_wait, so it cannot re-read the
    guard and passes the drained text here instead."""
    if rec_dir is None or not output:
        return
    knobs = parse_knobs_line(output)
    mig = parse_migration_line(output)
    have_xdom = any(f"cross_domain_{p}" in knobs for p in _XDOM_PATHS)
    have_mig = any(p in mig for p in _XDOM_PATHS)
    if not have_xdom and not have_mig:
        return
    lines = []
    if "cross_domain_scatter_pct" in knobs:
        lines.append(f"montauk_cross_domain_scatter_pct {knobs['cross_domain_scatter_pct']}")
    for p in _XDOM_PATHS:
        k = f"cross_domain_{p}"
        if k in knobs:
            lines.append(f'montauk_cross_domain_path{{path="{p}"}} {knobs[k]}')
    # ALL-MOVES per-path attribution -- the within-L3 core-to-core bounce the
    # cross-domain subset above cannot see. montauk_migration_path names the dominant
    # cause of a migration storm (steal vs sel vs enq) instead of only its cross-CCX share.
    for p in _XDOM_PATHS:
        if p in mig:
            lines.append(f'montauk_migration_path{{path="{p}"}} {mig[p]}')
    try:
        (Path(rec_dir) / "cross_domain.prom").write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def trace_workload(sched_name, activate_cmd, pattern, label, stamp, body_fn, *,
                   baseline_s=0.0, events=False, pin_cpu=None,
                   trace_activation=False):
    """Record `montauk --trace pattern` around a scheduler workload.

    Default: activate `sched_name` via start_and_wait(activate_cmd), record while
    body_fn(rec_dir) runs, then stop_and_wait. Returns (rec_dir, body_result), or
    (None, None) if activation failed (nothing to trace).

    trace_activation=True: montauk records FIRST (pattern should match the
    SCHEDULER comm), then activation is attempted inside the window -- so a FAILED
    activation lands in the recording instead of vanishing. body_fn runs only if
    activation took. Returns (rec_dir, body_result) on success, (rec_dir, None) on
    activation failure (the failure IS the capture).

    The caller owns the workload; this owns montauk and the scheduler lifecycle.
    """
    safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
    rlabel = f"{label}-{safe}"

    if trace_activation:
        with montauk_trace(pattern, rlabel, stamp, baseline_s=baseline_s,
                           events=events, pin_cpu=pin_cpu) as rec:
            guard = start_and_wait(activate_cmd, sched_name) if activate_cmd else None
            if activate_cmd is not None and guard is None:
                log_error(f"[{sched_name}] failed to activate "
                          f"-- captured in {rec.dir}")
                return rec.dir, None
            try:
                return rec.dir, body_fn(rec.dir)
            finally:
                if guard is not None:
                    stop_and_wait(guard)
                    _write_cross_domain_marker(guard, rec.dir)

    guard = None
    if activate_cmd is not None:
        log_info(f"[{sched_name}] activating scheduler...")
        guard = start_and_wait(activate_cmd, sched_name)
        if guard is None:
            log_error(f"[{sched_name}] failed to activate, skipping")
            return None, None
    rec_dir = None
    try:
        with montauk_trace(pattern, rlabel, stamp, baseline_s=baseline_s,
                           events=events, pin_cpu=pin_cpu) as rec:
            rec_dir = rec.dir
            return rec.dir, body_fn(rec.dir)
    finally:
        if guard is not None:
            stop_and_wait(guard)
            _write_cross_domain_marker(guard, rec_dir)


# MEASUREMENT

# PROMETHEUS HISTOGRAM BUCKETS (us). 1-2-5 ladder per decade, 1us..1s,
# shared across every us-domain latency distribution so per-cell CDFs can be
# reconstructed from the .prom alone.
HIST_BUCKETS_US = [
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 20_000, 50_000,
    100_000, 200_000, 500_000, 1_000_000,
]


def histogram(values: list[float]) -> dict:
    """Bucket raw us samples into a cumulative Prometheus histogram.

    Returns {"buckets": [(le, cum_count), ...] ascending incl. "+Inf",
    "sum": int, "count": int}. Empty input returns {}.
    """
    if not values:
        return {}
    counts = [0] * (len(HIST_BUCKETS_US) + 1)
    for v in values:
        counts[bisect.bisect_left(HIST_BUCKETS_US, v)] += 1
    cumulative = 0
    buckets = []
    for i, le in enumerate(HIST_BUCKETS_US):
        cumulative += counts[i]
        buckets.append((le, cumulative))
    cumulative += counts[-1]
    buckets.append(("+Inf", cumulative))
    return {"buckets": buckets, "sum": int(sum(values)), "count": len(values)}


def timed_run(cmd: str, clean_cmd: str | None = None) -> float | None:
    """Run a shell command, return wall-clock seconds or None on failure."""
    # The harness self-elevates to root for sched_ext, but a build WORKLOAD (the default
    # `cargo build`) must run as the INVOKING user: root has no rustup default toolchain
    # (the user does), so a root build fails with "no default toolchain configured", and a
    # root build would also root-own TARGET_DIR and collide with the user's own builds.
    # Drop back to SUDO_USER when elevated; the active scheduler still schedules it.
    def _asuser(c: str) -> list[str]:
        u = os.environ.get("SUDO_USER")
        if os.geteuid() == 0 and u and u != "root":
            return ["sudo", "-u", u, "sh", "-c", c]
        return ["sh", "-c", c]
    if clean_cmd:
        subprocess.run(_asuser(clean_cmd),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_info(f"Running: {cmd}")
    start = time.monotonic()
    result = subprocess.run(_asuser(cmd),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:500]
        log_error(f"Command failed (exit {result.returncode}): {stderr}")
        return None
    log_info(f"Completed in {elapsed:.2f}s")
    return elapsed


def parse_probe_output(stdout_text: str) -> dict:
    """Parse probe stdout (one overshoot_us per line) into latency stats."""
    values = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if line and line.lstrip("-").isdigit():
            values.append(float(line))
    if not values:
        return {"samples": 0, "median_us": 0, "p99_us": 0, "worst_us": 0}
    return {
        "samples": len(values),
        "median_us": int(percentile(values, 50)),
        "p99_us": int(percentile(values, 99)),
        "worst_us": int(max(values)),
        "hist": histogram(values),
    }


def measure_latency(binary: Path, n_cpus: int, iterations: int = 1,
                    duration_secs: int = 15, warmup_secs: int = 3) -> dict:
    """Spawn pinned stress workers on all cores + unpinned probe.

    Stress workers saturate every CPU. Probe floats -- the scheduler
    decides where to place it, measuring real preemption latency under
    full load (no reserved core).

    Multiple iterations pool all samples for final percentile calculation.
    """
    if n_cpus < 1:
        log_warn("Need at least 1 CPU for latency measurement")
        return {"samples": 0, "median_us": 0, "p99_us": 0, "worst_us": 0}

    stress_cpus = list(range(0, n_cpus))

    log_info(f"Latency: {len(stress_cpus)} stress workers, probe unpinned, "
             f"{iterations} iteration(s)")

    workers = []
    for cpu in stress_cpus:
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup probe (discard output, let scheduler classify workload)
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(warmup_secs)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    # Measurement iterations (pool all samples)
    all_values: list[float] = []
    for i in range(iterations):
        log_info(f"Latency iteration {i + 1}/{iterations}: {duration_secs}s")
        probe = subprocess.Popen(
            [str(binary), "probe"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        time.sleep(duration_secs)
        probe.send_signal(signal.SIGINT)
        try:
            stdout, _ = probe.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            probe.kill()
            stdout, _ = probe.communicate()

        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line and line.lstrip("-").isdigit():
                all_values.append(float(line))

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    if not all_values:
        result = {"samples": 0, "median_us": 0, "p99_us": 0, "worst_us": 0}
    else:
        result = {
            "samples": len(all_values),
            "median_us": int(percentile(all_values, 50)),
            "p99_us": int(percentile(all_values, 99)),
            "worst_us": int(max(all_values)),
            "hist": histogram(all_values),
        }

    log_info(f"Latency: {result['samples']} samples, "
             f"median={result['median_us']}us, "
             f"p99={result['p99_us']}us, "
             f"worst={result['worst_us']}us")
    return result


# BURST MEASUREMENT

def fire_burst(count: int, work_secs: float = 0.5) -> list[subprocess.Popen]:
    """Spawn count short-lived CPU-bound processes as fast as possible.

    Simulates application launch: fork/exec storm of processes that each
    do CPU work for work_secs then exit. Python startup overhead (~50ms)
    is intentional -- it mirrors real app initialization."""
    script = (
        "import time,hashlib\n"
        f"end=time.monotonic()+{work_secs}\n"
        "while time.monotonic()<end:\n"
        " hashlib.sha256(b'x'*4096).hexdigest()\n"
    )
    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    return procs


def measure_burst(binary: Path, n_cpus: int, burst_size: int,
                  burst_work_secs: float = 0.5,
                  baseline_secs: int = 5,
                  burst_measure_secs: int = 10,
                  warmup_secs: int = 2) -> dict:
    """Measure scheduling latency before and during a process burst under load.

    1. Stress workers saturate all CPUs
    2. Baseline probe (steady-state latency reference)
    3. Fire burst + measure through burst and settling
    4. Compare baseline vs burst P99
    """
    if n_cpus < 1:
        return {"survived": True, "baseline": {}, "burst": {}}

    stress_cpus = list(range(n_cpus))
    log_info(f"Burst test: {len(stress_cpus)} stress workers, "
             f"{burst_size} burst processes ({burst_work_secs}s each)")

    # Start stress workers on all CPUs
    workers = []
    for cpu in stress_cpus:
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup (discard output)
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(warmup_secs)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    # Baseline measurement
    log_info(f"Baseline: {baseline_secs}s")
    baseline_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(baseline_secs)
    baseline_probe.send_signal(signal.SIGINT)
    try:
        baseline_out, _ = baseline_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        baseline_probe.kill()
        baseline_out, _ = baseline_probe.communicate()
    baseline = parse_probe_output(baseline_out.decode(errors="replace"))
    log_info(f"Baseline: {baseline['samples']} samples, "
             f"median={baseline['median_us']}us, "
             f"p99={baseline['p99_us']}us")

    # Burst measurement: start probe, fire burst, measure during + after
    burst_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    log_info(f"Firing burst: {burst_size} processes")
    burst_start = time.monotonic()
    burst_procs = fire_burst(burst_size, burst_work_secs)

    # Wait for all burst processes to exit
    for p in burst_procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    burst_duration = time.monotonic() - burst_start
    log_info(f"Burst complete: {burst_duration:.1f}s")

    # Continue measuring through settling period
    remaining = burst_measure_secs - burst_duration
    if remaining > 0:
        log_info(f"Settling: {remaining:.0f}s")
        time.sleep(remaining)

    burst_probe.send_signal(signal.SIGINT)
    try:
        burst_out, _ = burst_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        burst_probe.kill()
        burst_out, _ = burst_probe.communicate()
    burst_stats = parse_probe_output(burst_out.decode(errors="replace"))
    log_info(f"Burst: {burst_stats['samples']} samples, "
             f"median={burst_stats['median_us']}us, "
             f"p99={burst_stats['p99_us']}us, "
             f"worst={burst_stats['worst_us']}us")

    # POST-BURST RECOVERY: MEASURE HOW QUICKLY LATENCY RETURNS TO NORMAL
    # DSQ COLLAPSE ROUTES EVERYTHING TO INTERACTIVE DSQ DURING BURST.
    # WHEN BURST CLEARS, ROUTING SNAPS BACK. TASKS ENQUEUED DURING BURST
    # ARE STILL IN THE INTERACTIVE DSQ. interactive_run MAY BE IN AN
    # ARBITRARY STATE. THIS MEASURES THE SETTLING BEHAVIOR.
    log_info("Recovery: 5s")
    recovery_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    recovery_probe.send_signal(signal.SIGINT)
    try:
        recovery_out, _ = recovery_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        recovery_probe.kill()
        recovery_out, _ = recovery_probe.communicate()
    recovery_stats = parse_probe_output(recovery_out.decode(errors="replace"))
    log_info(f"Recovery: {recovery_stats['samples']} samples, "
             f"p99={recovery_stats['p99_us']}us")

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    return {
        "survived": True,
        "burst_size": burst_size,
        "burst_duration_s": round(burst_duration, 1),
        "baseline": baseline,
        "burst": burst_stats,
        "recovery": recovery_stats,
    }


# LONG-RUNNING PROCESS MEASUREMENT

def spawn_longrunners(count: int, duration_secs: float) -> list[subprocess.Popen]:
    """Spawn persistent CPU-bound processes that report work completed.

    Each process does SHA256 hashing in a tight loop for duration_secs,
    then prints the number of iterations completed before exiting.
    This lets us measure whether long-runners actually get CPU time
    or starve under the scheduler."""
    script = (
        "import time,hashlib,sys\n"
        f"end=time.monotonic()+{duration_secs}\n"
        "iters=0\n"
        "while time.monotonic()<end:\n"
        " hashlib.sha256(b'x'*4096).hexdigest()\n"
        " iters+=1\n"
        "print(iters,flush=True)\n"
    )
    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    return procs


def measure_longrun(binary: Path, n_cpus: int,
                    longrun_count: int = 4,
                    longrun_secs: float = 20.0,
                    warmup_secs: int = 2) -> dict:
    """Measure scheduling behavior with persistent long-running CPU-bound processes.

    Simulates real desktop contention: a few heavy background processes (builds,
    Steam updates, video encoding) competing against interactive workloads.

    Measures two things:
    1. Interactive latency while long-runners are active (probe P99/worst)
    2. Long-runner throughput (SHA256 iterations completed -- starvation = 0 or near-0)

    Phases:
    1. Stress workers saturate all CPUs (same as burst/latency tests)
    2. Warmup period (discard)
    3. Spawn long-runners + start latency probe simultaneously
    4. Let everything run for longrun_secs
    5. Collect probe latency and long-runner work counts
    """
    if n_cpus < 1:
        return {"survived": True, "latency": {}, "longrun_work": []}

    stress_cpus = list(range(n_cpus))
    log_info(f"Long-run test: {len(stress_cpus)} stress workers, "
             f"{longrun_count} long-runners for {longrun_secs}s")

    # Start stress workers on all CPUs
    workers = []
    for cpu in stress_cpus:
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(warmup_secs)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    # Start latency probe + long-runners simultaneously
    probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    longrunners = spawn_longrunners(longrun_count, longrun_secs)
    log_info(f"Running: {longrun_count} long-runners + probe for {longrun_secs}s")

    # Wait for long-runners to finish (they self-terminate after longrun_secs)
    work_counts = []
    for lr in longrunners:
        try:
            stdout, _ = lr.communicate(timeout=longrun_secs + 10)
            line = stdout.decode(errors="replace").strip()
            work_counts.append(int(line) if line.isdigit() else 0)
        except (subprocess.TimeoutExpired, ValueError):
            lr.kill()
            lr.wait()
            work_counts.append(0)

    # Stop probe
    probe.send_signal(signal.SIGINT)
    try:
        probe_out, _ = probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe_out, _ = probe.communicate()
    latency = parse_probe_output(probe_out.decode(errors="replace"))

    total_work = sum(work_counts)
    min_work = min(work_counts) if work_counts else 0
    max_work = max(work_counts) if work_counts else 0

    log_info(f"Long-run latency: {latency['samples']} samples, "
             f"median={latency['median_us']}us, "
             f"p99={latency['p99_us']}us, "
             f"worst={latency['worst_us']}us")
    log_info(f"Long-run work: total={total_work}, "
             f"min={min_work}, max={max_work}, "
             f"per-process={work_counts}")

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    return {
        "survived": True,
        "longrun_count": longrun_count,
        "longrun_secs": longrun_secs,
        "latency": latency,
        "work_total": total_work,
        "work_min": min_work,
        "work_max": max_work,
        "work_per_process": work_counts,
    }


# MIXED WORKLOAD MEASUREMENT (BURST + LONGRUN SIMULTANEOUS)

def measure_mixed(binary: Path, n_cpus: int,
                  longrun_count: int = 4,
                  longrun_secs: float = 30.0,
                  burst_size: int = 0,
                  burst_delay_secs: float = 5.0,
                  burst_work_secs: float = 0.5,
                  warmup_secs: int = 2) -> dict:
    """Measure scheduling under combined burst + long-running load.

    Simulates the Steam scenario: background updates (long-runners) are
    already active when the user launches an app (burst of child processes).

    Phases:
    1. Stress workers saturate all CPUs
    2. Warmup (discard)
    3. Spawn long-runners + start latency probe simultaneously
    4. Wait burst_delay_secs for long-runners to establish vtime
    5. Fire burst while long-runners are still running
    6. Wait for burst to clear, continue through long-runner completion
    7. Collect probe latency + long-runner work counts
    """
    if n_cpus < 1:
        return {"survived": True, "latency": {}, "work_total": 0}

    if burst_size < 1:
        burst_size = max(8, n_cpus * 4)

    stress_cpus = list(range(n_cpus))
    log_info(f"Mixed test: {len(stress_cpus)} stress workers, "
             f"{longrun_count} long-runners ({longrun_secs}s), "
             f"{burst_size} burst procs after {burst_delay_secs}s delay")

    # Start stress workers on all CPUs
    workers = []
    for cpu in stress_cpus:
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(warmup_secs)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    # Start long-runners + probe simultaneously
    probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    longrunners = spawn_longrunners(longrun_count, longrun_secs)
    log_info(f"Running: {longrun_count} long-runners + probe")

    # Wait for long-runners to establish vtime, then fire burst
    log_info(f"Delay: {burst_delay_secs}s (establishing long-runner vtime)")
    time.sleep(burst_delay_secs)

    log_info(f"Firing burst: {burst_size} processes")
    burst_start = time.monotonic()
    burst_procs = fire_burst(burst_size, burst_work_secs)

    # Wait for burst processes to exit
    for bp in burst_procs:
        try:
            bp.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bp.kill()
            bp.wait()
    burst_duration = time.monotonic() - burst_start
    log_info(f"Burst complete: {burst_duration:.1f}s")

    # Wait for long-runners to finish
    work_counts = []
    for lr in longrunners:
        try:
            stdout, _ = lr.communicate(timeout=longrun_secs + 10)
            line = stdout.decode(errors="replace").strip()
            work_counts.append(int(line) if line.isdigit() else 0)
        except (subprocess.TimeoutExpired, ValueError):
            lr.kill()
            lr.wait()
            work_counts.append(0)

    # Stop probe
    probe.send_signal(signal.SIGINT)
    try:
        probe_out, _ = probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe_out, _ = probe.communicate()
    latency = parse_probe_output(probe_out.decode(errors="replace"))

    total_work = sum(work_counts)
    min_work = min(work_counts) if work_counts else 0
    max_work = max(work_counts) if work_counts else 0

    log_info(f"Mixed latency: {latency['samples']} samples, "
             f"median={latency['median_us']}us, "
             f"p99={latency['p99_us']}us, "
             f"worst={latency['worst_us']}us")
    log_info(f"Mixed long-run work: total={total_work}, "
             f"min={min_work}, max={max_work}, "
             f"per-process={work_counts}")

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    return {
        "survived": True,
        "longrun_count": longrun_count,
        "longrun_secs": longrun_secs,
        "burst_size": burst_size,
        "burst_duration_s": round(burst_duration, 1),
        "latency": latency,
        "work_total": total_work,
        "work_min": min_work,
        "work_max": max_work,
        "work_per_process": work_counts,
    }


# PERIODIC DEADLINE MEASUREMENT

def measure_deadline(binary: Path, n_cpus: int,
                     target_fps: int = 60,
                     duration_secs: int = 15,
                     warmup_secs: int = 3,
                     threshold_us: int = 500) -> dict:
    """Measure frame scheduling jitter under full CPU load.

    Simulates a game/compositor frame loop: workers wake on a periodic
    timer (16.6ms for 60fps), do a small fixed work unit (~1ms SHA256),
    then sleep until the next frame. Jitter = actual wake time minus
    expected wake time.

    A deadline miss is any frame where jitter exceeds threshold_us.
    """
    if n_cpus < 1:
        return {"survived": True, "total_frames": 0, "missed_frames": 0}

    period_us = 1_000_000 // target_fps
    period_secs = period_us / 1_000_000.0
    worker_count = min(4, n_cpus)

    log_info(f"Deadline test: {worker_count} frame workers @ {target_fps}fps "
             f"({period_us}us period), {n_cpus} stress workers, "
             f"threshold={threshold_us}us")

    # Start stress workers on all CPUs
    workers = []
    for cpu in range(n_cpus):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Deadline worker script: periodic wake, record jitter, do small work
    script = (
        "import time,hashlib,sys\n"
        f"period={period_secs}\n"
        f"duration={duration_secs}\n"
        "end=time.monotonic()+duration\n"
        "next_wake=time.monotonic()+period\n"
        "while time.monotonic()<end:\n"
        " time.sleep(max(0,next_wake-time.monotonic()))\n"
        " actual=time.monotonic()\n"
        " jitter_us=int((actual-next_wake)*1e6)\n"
        " print(jitter_us,flush=True)\n"
        " for _ in range(50):\n"
        "  hashlib.sha256(b'x'*4096).hexdigest()\n"
        " next_wake+=period\n"
    )

    # Warmup phase (discard)
    log_info(f"Warmup: {warmup_secs}s")
    warmup_workers = []
    for _ in range(worker_count):
        p = subprocess.Popen(
            [sys.executable, "-c", script.replace(f"duration={duration_secs}",
                                                   f"duration={warmup_secs}")],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        warmup_workers.append(p)
    for p in warmup_workers:
        try:
            p.wait(timeout=warmup_secs + 10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    # Measurement phase
    log_info(f"Measuring: {duration_secs}s")
    deadline_workers = []
    for _ in range(worker_count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        deadline_workers.append(p)

    all_jitter: list[int] = []
    for p in deadline_workers:
        try:
            stdout, _ = p.communicate(timeout=duration_secs + 10)
            for line in stdout.decode(errors="replace").splitlines():
                line = line.strip()
                if line and line.lstrip("-").isdigit():
                    all_jitter.append(int(line))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    total = len(all_jitter)
    missed = sum(1 for j in all_jitter if j > threshold_us) if all_jitter else 0
    miss_ratio = missed / total if total > 0 else 0.0

    result = {
        "survived": True,
        "target_fps": target_fps,
        "period_us": period_us,
        "threshold_us": threshold_us,
        "workers": worker_count,
        "total_frames": total,
        "missed_frames": missed,
        "miss_ratio": round(miss_ratio, 4),
        "jitter_median_us": int(percentile(all_jitter, 50)) if all_jitter else 0,
        "jitter_p99_us": int(percentile(all_jitter, 99)) if all_jitter else 0,
        "jitter_worst_us": int(max(all_jitter)) if all_jitter else 0,
        "jitter_hist": histogram([float(j) for j in all_jitter]),
    }

    log_info(f"Deadline: {total} frames, {missed} missed "
             f"({miss_ratio:.1%}), "
             f"jitter p99={result['jitter_p99_us']}us, "
             f"worst={result['jitter_worst_us']}us")
    return result


# IPC ROUND-TRIP MEASUREMENT

def measure_ipc(binary: Path, n_cpus: int,
                rounds: int = IPC_DEFAULT_ROUNDS) -> dict:
    """Measure IPC round-trip latency with the shared IPC engine.

    One CLEAN handoff pair per primitive (pipe/socket/eventfd/sem) looping a
    fixed round count under CPU saturation. Returns per-primitive
    {p50,p99,p999,worst,n} in 'cell', with the pipe primitive promoted to the
    legacy headline keys. Stress uses the shared BINARY in pandemonium_common.
    """
    if n_cpus < 1:
        return {"survived": True, "pairs": 0}

    log_info(f"IPC test: clean per-primitive RTT ({', '.join(IPC_RTT_PRIMS)}), "
             f"{rounds} rounds each, {n_cpus} stress workers")

    cell = measure_ipc_cell(n_cpus, rounds)
    pipe = cell.get("rtt_pipe") or {}

    for prim in IPC_RTT_PRIMS:
        d = cell.get(f"rtt_{prim}")
        if d:
            log_info(f"  {prim:<8} p50={d['p50']}us p99={d['p99']}us "
                     f"p99.9={d['p999']}us worst={d['worst']}us (n={d['n']})")

    result = {
        "survived": True,
        "pairs": 1,
        "rounds_per_pair": rounds,
        # Pipe headline (legacy keys for the report table + summary matrix).
        "total_ops": pipe.get("n", 0),
        "rtt_median_us": pipe.get("p50", 0),
        "rtt_p99_us": pipe.get("p99", 0),
        "rtt_p999_us": pipe.get("p999", 0),
        "rtt_worst_us": pipe.get("worst", 0),
        # Full per-primitive distributions (pipe/socket/eventfd/sem + fanout).
        "cell": cell,
    }
    return result


def analyze_ipc_trace(events_path: str) -> dict:
    """Fold montauk's deep IPC analysis of an --ipc capture into the report:
    wake2run dispatch latency (distinct from app RTT), cache locality / cross-CCX,
    the holder under saturation, and dispatch self-similarity. montauk is the data
    source -- we surface its own verdict lines, not a hand-rolled re-derivation.
    Returns {} when the capture or analyzer is unavailable."""
    res: dict = {}
    try:
        rep = subprocess.run(
            [MONTAUK + "_analyze", events_path, "--report",
             "sched,locality,dispatch-stall,fractal"],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return res
    cur = None
    for raw in rep.stdout.splitlines():
        s = raw.strip()
        if s.startswith("REPORT "):
            cur = s[len("REPORT "):].strip()
        elif cur == "sched" and s.startswith("VERDICT:") and "wake2run" in s \
                and "wake2run" not in res:
            res["wake2run"] = s[len("VERDICT:"):].strip()
        elif cur == "locality" and s.startswith("VERDICT:") and "locality" not in res:
            res["locality"] = s[len("VERDICT:"):].strip()
        elif cur == "dispatch-stall" and s.startswith("HELD by:"):
            res["holder"] = s
        elif cur == "fractal" and s.startswith("dispatch-rate"):
            res["fractal"] = " ".join(s.split())
    return res


# APPLICATION LAUNCH MEASUREMENT

def measure_launch(binary: Path, n_cpus: int,
                   launch_count: int = 100,
                   warmup_secs: int = 3) -> dict:
    """Measure fork+exec latency under full CPU load.

    Sequentially launches short-lived processes (/usr/bin/true) and measures
    wall-clock time from subprocess.run() start to completion. Simulates
    opening apps while the system is under compile load.
    """
    if n_cpus < 1:
        return {"survived": True, "launches": 0}

    log_info(f"Launch test: {launch_count} launches, {n_cpus} stress workers")

    # Start stress workers on all CPUs
    workers = []
    for cpu in range(n_cpus):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    launch_cmd = ["/usr/bin/true"]
    if not os.path.exists("/usr/bin/true"):
        launch_cmd = [sys.executable, "-c", ""]

    # Warmup (a few launches, discard)
    log_info(f"Warmup: {warmup_secs}s")
    warmup_end = time.monotonic() + warmup_secs
    while time.monotonic() < warmup_end:
        subprocess.run(launch_cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    # Measurement
    log_info(f"Measuring: {launch_count} launches")
    latencies_us: list[int] = []
    for _ in range(launch_count):
        start = time.monotonic()
        subprocess.run(launch_cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        elapsed_us = int((time.monotonic() - start) * 1_000_000)
        latencies_us.append(elapsed_us)

    # Stop stress workers
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    m, std = mean_stdev([float(x) for x in latencies_us])
    result = {
        "survived": True,
        "launches": launch_count,
        "launch_mean_us": int(m),
        "launch_median_us": int(percentile(latencies_us, 50)) if latencies_us else 0,
        "launch_p99_us": int(percentile(latencies_us, 99)) if latencies_us else 0,
        "launch_worst_us": int(max(latencies_us)) if latencies_us else 0,
        "hist": histogram([float(x) for x in latencies_us]),
    }

    log_info(f"Launch: {launch_count} runs, "
             f"mean={result['launch_mean_us']}us, "
             f"p99={result['launch_p99_us']}us, "
             f"worst={result['launch_worst_us']}us")
    return result


# TELEMETRY PARSING

def parse_tick_lines(stdout_text: str) -> list[dict]:
    """Parse d/s: tick lines from scheduler stdout.

    Handles both BPF-only format (ends with [BPF]) and adaptive format
    (ends with [Light/Mixed/Heavy]).
    """
    ticks = []
    for line in stdout_text.splitlines():
        if not line.startswith("d/s:"):
            continue

        tick = {}

        # Common fields
        m = re.search(r"d/s:\s*(\d+)", line)
        if m:
            tick["dispatches"] = int(m.group(1))
        m = re.search(r"idle:\s*(\d+)%", line)
        if m:
            tick["idle_pct"] = int(m.group(1))
        m = re.search(r"shared:\s*(\d+)", line)
        if m:
            tick["shared"] = int(m.group(1))
        m = re.search(r"preempt:\s*(\d+)", line)
        if m:
            tick["preempt"] = int(m.group(1))
        m = re.search(r"keep:\s*(\d+)", line)
        if m:
            tick["keep"] = int(m.group(1))
        m = re.search(r"kick:\s*H=(\d+)\s*S=(\d+)", line)
        if m:
            tick["kick_hard"] = int(m.group(1))
            tick["kick_soft"] = int(m.group(2))
        m = re.search(r"enq:\s*W=(\d+)\s*R=(\d+)", line)
        if m:
            tick["enq_wake"] = int(m.group(1))
            tick["enq_requeue"] = int(m.group(2))
        m = re.search(r"wake:\s*(\d+)us", line)
        if m:
            tick["wake_avg_us"] = int(m.group(1))
        m = re.search(r"lat_idle:\s*(\d+)us", line)
        if m:
            tick["lat_idle_us"] = int(m.group(1))
        m = re.search(r"lat_kick:\s*(\d+)us", line)
        if m:
            tick["lat_kick_us"] = int(m.group(1))
        m = re.search(r"l2:\s*B=(\d+)%\s*I=(\d+)%\s*L=(\d+)%", line)
        if m:
            tick["l2_pct_batch"] = int(m.group(1))
            tick["l2_pct_interactive"] = int(m.group(2))
            tick["l2_pct_latcrit"] = int(m.group(3))

        # REGIME + FLAGS: [BPF], [BPF BURST], [BPF LONGRUN],
        # [BPF BURST LONGRUN], [MIXED], [MIXED BURST], [HEAVY LONGRUN], etc.
        regime_match = re.search(
            r'\[(BPF|Light|Mixed|Heavy|LIGHT|MIXED|HEAVY)((?:\s+LONGRUN)*)\]', line)
        if regime_match:
            tick["regime"] = regime_match.group(1)
            flags = regime_match.group(2).upper()
            tick["longrun_active"] = "LONGRUN" in flags

            if tick["regime"] == "BPF":
                m = re.search(r"procdb:\s*(\d+)\s", line)
                if m:
                    tick["procdb_hits"] = int(m.group(1))
            else:
                m = re.search(r"p99:\s*(\d+)us", line)
                if m:
                    tick["p99_us"] = int(m.group(1))
                m = re.search(r"p99:.*?\[B:(\d+)\s*I:(\d+)\s*L:(\d+)\]", line)
                if m:
                    tick["tier_p99_batch"] = int(m.group(1))
                    tick["tier_p99_interactive"] = int(m.group(2))
                    tick["tier_p99_latcrit"] = int(m.group(3))
                m = re.search(r"procdb:\s*(\d+)/(\d+)", line)
                if m:
                    tick["procdb_total"] = int(m.group(1))
                    tick["procdb_confident"] = int(m.group(2))
                m = re.search(r"sleep:\s*io=(\d+)%", line)
                if m:
                    tick["io_pct"] = int(m.group(1))
                m = re.search(r"slice:\s*(\d+)us", line)
                if m:
                    tick["slice_us"] = int(m.group(1))
                m = re.search(r"batch:\s*(\d+)us", line)
                if m:
                    tick["batch_us"] = int(m.group(1))
        if tick:
            ticks.append(tick)

    return ticks


def parse_knobs_line(stdout_text: str) -> dict:
    """Parse [KNOBS] summary line from scheduler stdout."""
    for line in stdout_text.splitlines():
        if "[KNOBS]" not in line:
            continue

        knobs = {}
        for m in re.finditer(r"(\w+)=(\S+)", line.split("[KNOBS]")[1]):
            k, v = m.group(1), m.group(2)
            if v == "true":
                knobs[k] = True
            elif v == "false":
                knobs[k] = False
            else:
                try:
                    knobs[k] = int(v)
                except ValueError:
                    knobs[k] = v

        # Expand ticks=L:5/M:12/H:3
        if "ticks" in knobs and isinstance(knobs["ticks"], str):
            ticks_str = knobs.pop("ticks")
            for part in ticks_str.split("/"):
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    label = {"L": "ticks_light", "M": "ticks_mixed",
                             "H": "ticks_heavy"}.get(prefix)
                    if label:
                        try:
                            knobs[label] = int(val)
                        except ValueError:
                            pass

        # Expand l2_hit=B:75%/I:60%/L:80%
        if "l2_hit" in knobs and isinstance(knobs["l2_hit"], str):
            l2_str = knobs.pop("l2_hit")
            for part in l2_str.split("/"):
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    label = {"B": "l2_hit_batch", "I": "l2_hit_interactive",
                             "L": "l2_hit_latcrit"}.get(prefix)
                    if label:
                        try:
                            knobs[label] = int(val.rstrip("%"))
                        except ValueError:
                            pass

        return knobs

    return {}


def aggregate_ticks(ticks: list[dict]) -> dict:
    """Aggregate tick data into summary statistics."""
    if not ticks:
        return {}

    agg = {}
    numeric_keys = set()
    for t in ticks:
        for k, v in t.items():
            if isinstance(v, (int, float)) and k != "regime":
                numeric_keys.add(k)

    for key in sorted(numeric_keys):
        values = [float(t[key]) for t in ticks if key in t]
        if not values:
            continue
        agg[key] = {
            "mean": round(sub_mean(values), 1),
            "p99": round(percentile(values, 99), 1),
            "last": values[-1],
        }

    # Regime distribution
    regimes = [t.get("regime", "") for t in ticks if t.get("regime")]
    if regimes:
        agg["regime_counts"] = {}
        for r in regimes:
            agg["regime_counts"][r] = agg["regime_counts"].get(r, 0) + 1

    return agg


# PROMETHEUS OUTPUT

def write_prometheus(data: dict, stamp: str) -> Path:
    """Write Prometheus exposition format (.prom) to ~/.cache/pandemonium/."""
    # Delegate to the shared builder (unified schema). The local gauge()/hist()
    # wrappers strip the historical `pandemonium_bench_` prefix from each call
    # site so the family becomes `pandemonium_scale_*` -- every call site below
    # is preserved unchanged.
    pb = PrometheusBuilder("scale")

    def _suffix(name: str) -> str:
        return name.replace("pandemonium_bench_", "", 1)

    def gauge(name: str, help_text: str, value, labels: dict | None = None):
        pb.gauge(_suffix(name), value, help=help_text, labels=labels)

    def hist(name: str, help_text: str, h: dict | None, labels: dict):
        if not h or not h.get("buckets"):
            return
        pb.hist(_suffix(name), h["buckets"], h["count"], h["sum"],
                help=help_text, labels=labels)

    # Metadata -- single _info gauge + timestamp via the builder.
    version = data.get("version", "unknown")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=data.get("git_commit", "unknown"),
            git_dirty=data.get("git_dirty", False))
    gauge("pandemonium_bench_iterations", "Number of throughput iterations",
          data.get("iterations", 0))
    gauge("pandemonium_bench_max_cpus", "Maximum CPUs available",
          data.get("max_cpus", 0))

    results = data.get("results", {})
    for cores_str, schedulers in sorted(results.items(), key=lambda x: int(x[0])):
        cores = cores_str

        for sched_name, sched_data in schedulers.items():
            labels = {"scheduler": sched_name, "cores": cores}

            # Latency metrics
            lat = sched_data.get("latency", {})
            if lat.get("samples", 0) > 0:
                gauge("pandemonium_bench_latency_samples",
                      "Number of latency samples collected",
                      lat["samples"], labels)
                gauge("pandemonium_bench_latency_median_us",
                      "Median wakeup latency",
                      lat["median_us"], labels)
                gauge("pandemonium_bench_latency_p99_us",
                      "P99 wakeup latency",
                      lat["p99_us"], labels)
                gauge("pandemonium_bench_latency_worst_us",
                      "Worst-case wakeup latency",
                      lat["worst_us"], labels)
                hist("pandemonium_bench_latency",
                     "Wakeup latency distribution (us)",
                     lat.get("hist"), labels)

            # Throughput metrics
            tp = sched_data.get("throughput", {})
            if "mean_s" in tp:
                gauge("pandemonium_bench_throughput_seconds",
                      "Wall-clock workload time (mean)",
                      tp["mean_s"], labels)
                if "stdev_s" in tp:
                    gauge("pandemonium_bench_throughput_stdev_seconds",
                          "Throughput standard deviation",
                          tp["stdev_s"], labels)
                if "vs_eevdf_pct" in tp:
                    gauge("pandemonium_bench_throughput_vs_eevdf_pct",
                          "Throughput delta vs EEVDF",
                          tp["vs_eevdf_pct"], labels)

            # Burst metrics
            br = sched_data.get("burst", {})
            if br:
                survived = 1 if br.get("survived", True) else 0
                gauge("pandemonium_bench_burst_survived",
                      "Whether scheduler survived burst (1=OK, 0=CRASHED)",
                      survived, labels)
                baseline = br.get("baseline", {})
                if baseline.get("samples", 0) > 0:
                    gauge("pandemonium_bench_burst_baseline_p99_us",
                          "Baseline P99 before burst",
                          baseline["p99_us"], labels)
                    hist("pandemonium_bench_burst_baseline",
                         "Baseline latency distribution before burst (us)",
                         baseline.get("hist"), labels)
                burst = br.get("burst", {})
                if burst.get("samples", 0) > 0:
                    gauge("pandemonium_bench_burst_p99_us",
                          "P99 during burst",
                          burst["p99_us"], labels)
                    gauge("pandemonium_bench_burst_worst_us",
                          "Worst-case latency during burst",
                          burst["worst_us"], labels)
                    gauge("pandemonium_bench_burst_samples",
                          "Latency samples collected during burst",
                          burst["samples"], labels)
                    hist("pandemonium_bench_burst",
                         "Latency distribution during burst (us)",
                         burst.get("hist"), labels)

            # Long-running metrics
            lr = sched_data.get("longrun", {})
            if lr:
                survived = 1 if lr.get("survived", True) else 0
                gauge("pandemonium_bench_longrun_survived",
                      "Whether scheduler survived long-run test (1=OK, 0=CRASHED)",
                      survived, labels)
                lr_lat = lr.get("latency", {})
                if lr_lat.get("samples", 0) > 0:
                    gauge("pandemonium_bench_longrun_latency_p99_us",
                          "P99 latency during long-running process test",
                          lr_lat["p99_us"], labels)
                    gauge("pandemonium_bench_longrun_latency_worst_us",
                          "Worst-case latency during long-running process test",
                          lr_lat["worst_us"], labels)
                    hist("pandemonium_bench_longrun_latency",
                         "Latency distribution during long-run test (us)",
                         lr_lat.get("hist"), labels)
                if lr.get("work_total", 0) > 0:
                    gauge("pandemonium_bench_longrun_work_total",
                          "Total SHA256 iterations across all long-runners",
                          lr["work_total"], labels)
                    gauge("pandemonium_bench_longrun_work_min",
                          "Minimum work by any single long-runner (starvation detector)",
                          lr["work_min"], labels)

            # Mixed workload metrics
            mx = sched_data.get("mixed", {})
            if mx:
                survived = 1 if mx.get("survived", True) else 0
                gauge("pandemonium_bench_mixed_survived",
                      "Whether scheduler survived mixed test (1=OK, 0=CRASHED)",
                      survived, labels)
                mx_lat = mx.get("latency", {})
                if mx_lat.get("samples", 0) > 0:
                    gauge("pandemonium_bench_mixed_latency_p99_us",
                          "P99 latency during mixed workload test",
                          mx_lat["p99_us"], labels)
                    gauge("pandemonium_bench_mixed_latency_worst_us",
                          "Worst-case latency during mixed workload test",
                          mx_lat["worst_us"], labels)
                    hist("pandemonium_bench_mixed_latency",
                         "Latency distribution during mixed test (us)",
                         mx_lat.get("hist"), labels)
                if mx.get("work_total", 0) > 0:
                    gauge("pandemonium_bench_mixed_work_total",
                          "Total SHA256 iterations in mixed test",
                          mx["work_total"], labels)
                    gauge("pandemonium_bench_mixed_work_min",
                          "Minimum work by any long-runner in mixed test",
                          mx["work_min"], labels)
                if mx.get("work_max", 0) > 0:
                    gauge("pandemonium_bench_mixed_work_max",
                          "Maximum work by any long-runner in mixed test",
                          mx["work_max"], labels)

            # Deadline metrics
            dl = sched_data.get("deadline", {})
            if dl and dl.get("total_frames", 0) > 0:
                survived = 1 if dl.get("survived", True) else 0
                gauge("pandemonium_bench_deadline_survived",
                      "Whether scheduler survived deadline test (1=OK, 0=CRASHED)",
                      survived, labels)
                gauge("pandemonium_bench_deadline_total_frames",
                      "Total frame cycles measured",
                      dl["total_frames"], labels)
                gauge("pandemonium_bench_deadline_missed_frames",
                      "Frames exceeding jitter threshold",
                      dl["missed_frames"], labels)
                gauge("pandemonium_bench_deadline_miss_ratio",
                      "Fraction of frames missed",
                      dl["miss_ratio"], labels)
                gauge("pandemonium_bench_deadline_jitter_p99_us",
                      "P99 frame scheduling jitter",
                      dl["jitter_p99_us"], labels)
                gauge("pandemonium_bench_deadline_jitter_worst_us",
                      "Worst-case frame scheduling jitter",
                      dl["jitter_worst_us"], labels)
                hist("pandemonium_bench_deadline_jitter",
                     "Frame scheduling jitter distribution (us)",
                     dl.get("jitter_hist"), labels)

            # IPC metrics: per-primitive clean RTT (shared IPC engine)
            ipc = sched_data.get("ipc", {})
            if ipc and ipc.get("total_ops", 0) > 0:
                survived = 1 if ipc.get("survived", True) else 0
                gauge("pandemonium_bench_ipc_survived",
                      "Whether scheduler survived IPC test (1=OK, 0=CRASHED)",
                      survived, labels)
                for key, d in (ipc.get("cell") or {}).items():
                    if not d:
                        continue
                    prim = key[4:] if key.startswith("rtt_") else key
                    pl = dict(labels, primitive=prim)
                    gauge("pandemonium_bench_ipc_rtt_p50_us",
                          "Median IPC round-trip latency (us)", d["p50"], pl)
                    gauge("pandemonium_bench_ipc_rtt_p99_us",
                          "P99 IPC round-trip latency (us)", d["p99"], pl)
                    gauge("pandemonium_bench_ipc_rtt_p999_us",
                          "P99.9 IPC round-trip latency (us)", d["p999"], pl)
                    gauge("pandemonium_bench_ipc_rtt_worst_us",
                          "Worst IPC round-trip latency (us)", d["worst"], pl)
                    gauge("pandemonium_bench_ipc_rtt_samples",
                          "IPC round-trip samples", d["n"], pl)

            # Launch metrics
            lnch = sched_data.get("launch", {})
            if lnch and lnch.get("launches", 0) > 0:
                survived = 1 if lnch.get("survived", True) else 0
                gauge("pandemonium_bench_launch_survived",
                      "Whether scheduler survived launch test (1=OK, 0=CRASHED)",
                      survived, labels)
                gauge("pandemonium_bench_launch_mean_us",
                      "Mean fork+exec latency under load",
                      lnch["launch_mean_us"], labels)
                gauge("pandemonium_bench_launch_p99_us",
                      "P99 fork+exec latency under load",
                      lnch["launch_p99_us"], labels)
                gauge("pandemonium_bench_launch_worst_us",
                      "Worst-case fork+exec latency under load",
                      lnch["launch_worst_us"], labels)
                hist("pandemonium_bench_launch",
                     "Fork+exec latency distribution under load (us)",
                     lnch.get("hist"), labels)

            # Post-burst recovery metrics
            br = sched_data.get("burst", {})
            if br:
                br_recovery = br.get("recovery", {})
                if br_recovery.get("samples", 0) > 0:
                    gauge("pandemonium_bench_burst_recovery_p99_us",
                          "P99 latency during post-burst recovery",
                          br_recovery["p99_us"], labels)
                    gauge("pandemonium_bench_burst_recovery_worst_us",
                          "Worst-case latency during post-burst recovery",
                          br_recovery["worst_us"], labels)
                    hist("pandemonium_bench_burst_recovery",
                         "Latency distribution during post-burst recovery (us)",
                         br_recovery.get("hist"), labels)

            # Longrun detection verification
            lr_ticks = sched_data.get("longrun_ticks", 0)
            if lr_ticks > 0:
                gauge("pandemonium_bench_longrun_ticks",
                      "Number of scheduler ticks with longrun_mode active",
                      lr_ticks, labels)

            # Long-run work distribution
            lr = sched_data.get("longrun", {})
            if lr and lr.get("work_max", 0) > 0:
                gauge("pandemonium_bench_longrun_work_max",
                      "Maximum work by any single long-runner",
                      lr["work_max"], labels)

            # Scheduler telemetry (PANDEMONIUM only)
            telem = sched_data.get("telemetry", {})
            knobs = telem.get("knobs", {})
            tick_agg = telem.get("tick_aggregate", {})

            if not knobs and not tick_agg:
                continue

            mode = "BPF" if "BPF" in sched_name else "ADAPTIVE"
            telem_labels = {"mode": mode, "cores": cores}

            if knobs:
                knob_map = {
                    "slice_ns": "slice_ns",
                    "batch_slice_ns": "batch_ns",
                    "preempt_thresh_ns": "preempt_ns",
                    "cpu_bound_thresh_ns": "demotion_ns",
                    "lag_scale": "lag",
                }
                for src_key, prom_suffix in knob_map.items():
                    if src_key in knobs:
                        gauge(f"pandemonium_bench_knob_{prom_suffix}",
                              f"Final tuning knob: {prom_suffix}",
                              knobs[src_key], telem_labels)

                if "reflex" in knobs:
                    gauge("pandemonium_bench_reflex_events",
                          "Reflex tighten events",
                          knobs["reflex"], telem_labels)
                if "tightened" in knobs:
                    val = 1 if knobs["tightened"] else 0
                    gauge("pandemonium_bench_tightened",
                          "Graduated relax tighten active",
                          val, telem_labels)

                for regime_key in ["ticks_light", "ticks_mixed", "ticks_heavy"]:
                    if regime_key in knobs:
                        regime_name = regime_key.replace("ticks_", "")
                        gauge("pandemonium_bench_regime_ticks",
                              "Ticks spent in each regime",
                              knobs[regime_key],
                              {**telem_labels, "regime": regime_name})

                for l2_key in ["l2_hit_batch", "l2_hit_interactive",
                               "l2_hit_latcrit"]:
                    if l2_key in knobs:
                        tier = l2_key.replace("l2_hit_", "")
                        gauge("pandemonium_bench_l2_hit_pct",
                              "L2 cache hit rate by tier",
                              knobs[l2_key],
                              {**telem_labels, "tier": tier})

                # CROSS-CCX SCATTER: placement-side fraction (MWU PATHWAY 6
                # input) + per-path attribution, per mode/cores. Lets the
                # scaling report compare scatter between BPF and ADAPTIVE and
                # confirm which placement path dominates the storm.
                if "cross_domain_scatter_pct" in knobs:
                    gauge("pandemonium_bench_cross_domain_scatter_pct",
                          "Placement-side cross-CCX scatter percent",
                          knobs["cross_domain_scatter_pct"], telem_labels)
                for xk in ["sel_tight", "sel_sync", "sel_normal", "sel_dfl",
                           "enq_t1", "enq_t2", "steal", "step5"]:
                    key = f"cross_domain_{xk}"
                    if key in knobs:
                        gauge("pandemonium_bench_cross_domain_path",
                              "Cross-CCX landings per placement path",
                              knobs[key],
                              {**telem_labels, "path": xk})

            if tick_agg:
                for field in ["idle_pct", "preempt",
                              "wake_avg_us", "p99_us"]:
                    if field in tick_agg:
                        stats = tick_agg[field]
                        gauge(f"pandemonium_bench_{field}_mean",
                              f"Mean {field} during measurement",
                              stats["mean"], telem_labels)

                regime_counts = tick_agg.get("regime_counts", {})
                for regime, count in regime_counts.items():
                    gauge("pandemonium_bench_regime_observed",
                          "Observed regime ticks during measurement",
                          count, {**telem_labels, "regime": regime})

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    version = data.get("version", "unknown")
    path = ARCHIVE_DIR / f"{version}-{stamp}.prom"
    path.write_text(pb.render())
    return path


# PROMETHEUS LIVE OUTPUT (BENCH-SYS)

_SYS_TICK_FIELDS = [
    ("dispatches", "pandemonium_sys_dispatches"),
    ("idle_pct", "pandemonium_sys_idle_pct"),
    ("shared", "pandemonium_sys_shared"),
    ("preempt", "pandemonium_sys_preempt"),
    ("keep", "pandemonium_sys_keep"),
    ("kick_hard", "pandemonium_sys_kick_hard"),
    ("kick_soft", "pandemonium_sys_kick_soft"),
    ("enq_wake", "pandemonium_sys_enq_wake"),
    ("enq_requeue", "pandemonium_sys_enq_requeue"),
    ("wake_avg_us", "pandemonium_sys_wake_us"),
    ("lat_idle_us", "pandemonium_sys_lat_idle_us"),
    ("lat_kick_us", "pandemonium_sys_lat_kick_us"),
    ("p99_us", "pandemonium_sys_p99_us"),
    ("slice_us", "pandemonium_sys_slice_us"),
    ("batch_us", "pandemonium_sys_batch_us"),
    ("io_pct", "pandemonium_sys_io_pct"),
    ("procdb_total", "pandemonium_sys_procdb_total"),
    ("procdb_confident", "pandemonium_sys_procdb_confident"),
    ("procdb_hits", "pandemonium_sys_procdb_total"),
]

_SYS_TICK_TIERED = [
    ("pandemonium_sys_l2_hit_pct",
     [("l2_pct_batch", "batch"), ("l2_pct_interactive", "interactive"),
      ("l2_pct_latcrit", "latcrit")]),
    ("pandemonium_sys_tier_p99_us",
     [("tier_p99_batch", "batch"), ("tier_p99_interactive", "interactive"),
      ("tier_p99_latcrit", "latcrit")]),
]


def prom_sys_create(path: Path, version: str, git: dict, max_cpus: int):
    """Create .prom with metadata header and all HELP/TYPE declarations."""
    dirty = "true" if git.get("dirty") else "false"
    commit = git.get("commit", "unknown")

    decls = [
        ("pandemonium_sys_dispatches", "Dispatches per second"),
        ("pandemonium_sys_idle_pct", "Idle hit percentage"),
        ("pandemonium_sys_shared", "Shared dispatches"),
        ("pandemonium_sys_preempt", "Preemptions"),
        ("pandemonium_sys_keep", "Keep running count"),
        ("pandemonium_sys_kick_hard", "Hard kick count"),
        ("pandemonium_sys_kick_soft", "Soft kick count"),
        ("pandemonium_sys_enq_wake", "Enqueue wakeup count"),
        ("pandemonium_sys_enq_requeue", "Enqueue requeue count"),
        ("pandemonium_sys_wake_us", "Mean wakeup latency us"),
        ("pandemonium_sys_lat_idle_us", "Idle path latency us"),
        ("pandemonium_sys_lat_kick_us", "Kick path latency us"),
        ("pandemonium_sys_p99_us", "P99 wakeup latency us"),
        ("pandemonium_sys_slice_us", "Current time slice us"),
        ("pandemonium_sys_batch_us", "Current batch slice us"),
        ("pandemonium_sys_io_pct", "IO sleep percentage"),
        ("pandemonium_sys_procdb_total", "ProcDb profiles"),
        ("pandemonium_sys_procdb_confident", "Confident ProcDb profiles"),
        ("pandemonium_sys_l2_hit_pct", "L2 cache hit rate by tier"),
        ("pandemonium_sys_tier_p99_us", "Per-tier P99 latency us"),
        ("pandemonium_sys_knob_slice_ns", "Final knob: time slice ns"),
        ("pandemonium_sys_knob_batch_ns", "Final knob: batch slice ns"),
        ("pandemonium_sys_knob_preempt_ns", "Final knob: preempt thresh ns"),
        ("pandemonium_sys_knob_demotion_ns", "Final knob: demotion thresh ns"),
        ("pandemonium_sys_knob_lag", "Final knob: lag scale"),
        ("pandemonium_sys_reflex_events", "Reflex tighten events"),
        ("pandemonium_sys_tightened", "Graduated relax tighten active"),
        ("pandemonium_sys_regime_ticks", "Ticks spent in each regime"),
        ("pandemonium_sys_cross_domain_scatter_pct", "Placement-side cross-CCX scatter percent"),
        ("pandemonium_sys_cross_domain_path", "Cross-CCX landings per placement path"),
        ("pandemonium_sys_latency_samples", "Latency probe samples"),
        ("pandemonium_sys_latency_median_us", "Median probe latency us"),
        ("pandemonium_sys_latency_p99_us", "P99 probe latency us"),
        ("pandemonium_sys_latency_worst_us", "Worst probe latency us"),
    ]

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("# HELP pandemonium_sys_info Build and run metadata\n")
        f.write("# TYPE pandemonium_sys_info gauge\n")
        f.write(f'pandemonium_sys_info{{version="{version}",'
                f'git_commit="{commit}",git_dirty="{dirty}"}} 1\n')
        f.write("# HELP pandemonium_sys_max_cpus Maximum CPUs available\n")
        f.write("# TYPE pandemonium_sys_max_cpus gauge\n")
        f.write(f"pandemonium_sys_max_cpus {max_cpus}\n")
        for name, help_text in decls:
            f.write(f"# HELP {name} {help_text}\n")
            f.write(f"# TYPE {name} gauge\n")


def prom_sys_append_ticks(path: Path, ticks: list[dict],
                          label_str: str, base_ts_ms: int):
    """Append tick metrics to .prom file. Returns lines written."""
    if not ticks:
        return 0
    with open(path, "a") as f:
        for i, tick in enumerate(ticks):
            ts = base_ts_ms - (len(ticks) - 1 - i) * 1000
            regime = tick.get("regime", "")
            if regime:
                f.write(f"# tick regime={regime}\n")
            for tick_key, prom_name in _SYS_TICK_FIELDS:
                if tick_key in tick:
                    f.write(f"{prom_name}{{{label_str}}} "
                            f"{tick[tick_key]} {ts}\n")
            for prom_name, tiers in _SYS_TICK_TIERED:
                for tier_key, tier_name in tiers:
                    if tier_key in tick:
                        f.write(f'{prom_name}{{{label_str},'
                                f'tier="{tier_name}"}} '
                                f'{tick[tier_key]} {ts}\n')
    return len(ticks)


def prom_sys_append_knobs(path: Path, knobs: dict, label_str: str):
    """Append [KNOBS] shutdown metrics to .prom file."""
    if not knobs:
        return
    knob_map = [
        ("slice_ns", "pandemonium_sys_knob_slice_ns"),
        ("batch_slice_ns", "pandemonium_sys_knob_batch_ns"),
        ("preempt_thresh_ns", "pandemonium_sys_knob_preempt_ns"),
        ("cpu_bound_thresh_ns", "pandemonium_sys_knob_demotion_ns"),
        ("lag_scale", "pandemonium_sys_knob_lag"),
    ]
    with open(path, "a") as f:
        f.write("# shutdown knobs\n")
        for src, prom in knob_map:
            if src in knobs:
                f.write(f"{prom}{{{label_str}}} {knobs[src]}\n")
        if "reflex" in knobs:
            f.write(f"pandemonium_sys_reflex_events{{{label_str}}} "
                    f"{knobs['reflex']}\n")
        if "tightened" in knobs:
            val = 1 if knobs["tightened"] else 0
            f.write(f"pandemonium_sys_tightened{{{label_str}}} {val}\n")
        for rk in ["ticks_light", "ticks_mixed", "ticks_heavy"]:
            if rk in knobs:
                rname = rk.replace("ticks_", "")
                f.write(f'pandemonium_sys_regime_ticks{{{label_str},'
                        f'regime="{rname}"}} {knobs[rk]}\n')
        for lk in ["l2_hit_batch", "l2_hit_interactive", "l2_hit_latcrit"]:
            if lk in knobs:
                tier = lk.replace("l2_hit_", "")
                f.write(f'pandemonium_sys_l2_hit_pct{{{label_str},'
                        f'tier="{tier}"}} {knobs[lk]}\n')
        # CROSS-CCX SCATTER: placement-side fraction (MWU PATHWAY 6 input) plus
        # the per-path attribution. Surfaced for both BPF and ADAPTIVE so the
        # scatter difference between modes is directly comparable per run.
        if "cross_domain_scatter_pct" in knobs:
            f.write(f"pandemonium_sys_cross_domain_scatter_pct{{{label_str}}} "
                    f"{knobs['cross_domain_scatter_pct']}\n")
        for xk in ["sel_tight", "sel_sync", "sel_normal", "sel_dfl",
                   "enq_t1", "enq_t2", "steal", "step5"]:
            key = f"cross_domain_{xk}"
            if key in knobs:
                f.write(f'pandemonium_sys_cross_domain_path{{{label_str},'
                        f'path="{xk}"}} {knobs[key]}\n')


def prom_sys_append_probe(path: Path, lat: dict, label_str: str):
    """Append latency probe results to .prom file."""
    if not lat or lat.get("samples", 0) == 0:
        return
    with open(path, "a") as f:
        f.write("# latency probe\n")
        f.write(f"pandemonium_sys_latency_samples{{{label_str}}} "
                f"{lat['samples']}\n")
        f.write(f"pandemonium_sys_latency_median_us{{{label_str}}} "
                f"{lat['median_us']}\n")
        f.write(f"pandemonium_sys_latency_p99_us{{{label_str}}} "
                f"{lat['p99_us']}\n")
        f.write(f"pandemonium_sys_latency_worst_us{{{label_str}}} "
                f"{lat['worst_us']}\n")


# REPORT

def gauge_rr(per_sched_times):
    """Gauge R&R on throughput: can the bench resolve scheduler deltas from
    run-to-run noise? per_sched_times maps scheduler -> per-iteration seconds.
    Schedulers are the 'parts', iterations the repeated trials. Returns %GRR
    (sqrt of the within-scheduler variance fraction; AIAG: <10 good, <30
    acceptable, >=30 the gauge cannot distinguish parts), ICC, and within-CV.
    Returns None if fewer than 2 schedulers have >=2 iterations."""
    series = [t for t in per_sched_times.values() if len(t) >= 2]
    if len(series) < 2:
        return None
    var_within = sum(sub_variance(t) for t in series) / len(series)
    means = [sub_mean(t) for t in series]
    var_between = sub_variance(means)
    total = var_within + var_between
    if total <= 0:
        return None
    grand = sub_mean(means)
    grr_pct = (var_within / total) ** 0.5 * 100.0
    if grr_pct < 10.0:
        verdict = "EXCELLENT"
    elif grr_pct < 30.0:
        verdict = "ACCEPTABLE"
    else:
        verdict = "UNTRUSTWORTHY -- noise swamps the scheduler delta"
    return {"grr_pct": grr_pct, "icc": var_between / total,
            "within_cv": (var_within ** 0.5 / grand * 100.0) if grand > 0 else 0.0,
            "verdict": verdict}


def format_report(data: dict) -> str:
    """Format benchmark results into a human-readable report."""
    lines = []
    if data.get("deadline_only"):
        mode = "DEADLINE ONLY"
    elif data.get("ipc_only"):
        mode = "IPC ONLY"
    elif data.get("launch_only"):
        mode = "LAUNCH ONLY"
    elif data.get("mixed_only"):
        mode = "MIXED ONLY"
    elif data.get("longrun_only"):
        mode = "LONG-RUN ONLY"
    elif data.get("burst_only"):
        mode = "BURST ONLY"
    else:
        mode = "BENCH-SCALE"
    lines.append(f"PANDEMONIUM {mode}")
    lines.append(f"VERSION:     {data.get('version', '?')}")
    if not data.get("burst_only"):
        lines.append(f"ITERATIONS:  {data.get('iterations', '?')}")
    lines.append(f"MAX CPUS:    {data.get('max_cpus', '?')}")
    lines.append("")

    results = data.get("results", {})
    sorted_cores = sorted(results.keys(), key=int)

    for cores_str in sorted_cores:
        schedulers = results[cores_str]
        lines.append(f"[{cores_str} CORES]")

        # Throughput table
        lines.append(f"{'SCHEDULER':<28} {'MEAN':>10} {'STDEV':>10} "
                     f"{'VS EEVDF':>12}")
        for sched_name, sched_data in schedulers.items():
            tp = sched_data.get("throughput", {})
            if "mean_s" not in tp:
                continue
            delta = tp.get("vs_eevdf_pct")
            delta_str = f"{delta:+.1f}%" if delta is not None else "(baseline)"
            lines.append(f"{sched_name:<28} {tp['mean_s']:>9.2f}s "
                        f"{tp.get('stdev_s', 0):>9.2f}s {delta_str:>12}")

        # Latency table
        has_latency = any(s.get("latency", {}).get("samples", 0) > 0
                         for s in schedulers.values())
        if has_latency:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'SAMPLES':>8} {'MEDIAN':>10} "
                        f"{'P99':>10} {'WORST':>10}")
            for sched_name, sched_data in schedulers.items():
                lat = sched_data.get("latency", {})
                if lat.get("samples", 0) == 0:
                    continue
                lines.append(
                    f"{sched_name:<28} {lat['samples']:>8} "
                    f"{lat['median_us']:>9}us {lat['p99_us']:>9}us "
                    f"{lat['worst_us']:>9}us")

        # Burst table
        has_burst = any(
            s.get("burst", {}).get("burst", {}).get("samples", 0) > 0
            for s in schedulers.values())
        if has_burst:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'STATUS':>8} "
                        f"{'BASE P99':>10} {'BURST P99':>10} "
                        f"{'WORST':>10} {'RECV P99':>10} {'SAMPLES':>8}")
            for sched_name, sched_data in schedulers.items():
                br = sched_data.get("burst", {})
                if not br:
                    continue
                status = "OK" if br.get("survived", True) else "CRASHED"
                base = br.get("baseline", {})
                burst = br.get("burst", {})
                recovery = br.get("recovery", {})
                bp99 = (f"{base['p99_us']}us"
                        if base.get("samples", 0) > 0 else "--")
                brp99 = (f"{burst['p99_us']}us"
                         if burst.get("samples", 0) > 0 else "--")
                worst = (f"{burst['worst_us']}us"
                         if burst.get("samples", 0) > 0 else "--")
                rp99 = (f"{recovery['p99_us']}us"
                        if recovery.get("samples", 0) > 0 else "--")
                samples = (str(burst.get("samples", 0))
                           if burst.get("samples", 0) > 0 else "--")
                lines.append(f"{sched_name:<28} {status:>8} "
                             f"{bp99:>10} {brp99:>10} "
                             f"{worst:>10} {rp99:>10} {samples:>8}")

        # Long-running table
        has_longrun = any(
            s.get("longrun", {}).get("latency", {}).get("samples", 0) > 0
            for s in schedulers.values())
        if has_longrun:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'STATUS':>8} "
                        f"{'LAT P99':>10} {'WORST':>10} "
                        f"{'WORK TOT':>10} {'WORK MIN':>10} {'WORK MAX':>10}")
            for sched_name, sched_data in schedulers.items():
                lr = sched_data.get("longrun", {})
                if not lr:
                    continue
                status = "OK" if lr.get("survived", True) else "CRASHED"
                lat = lr.get("latency", {})
                lp99 = (f"{lat['p99_us']}us"
                        if lat.get("samples", 0) > 0 else "--")
                lworst = (f"{lat['worst_us']}us"
                          if lat.get("samples", 0) > 0 else "--")
                work_tot = str(lr.get("work_total", 0))
                work_min = str(lr.get("work_min", 0))
                work_max = str(lr.get("work_max", 0))
                lines.append(f"{sched_name:<28} {status:>8} "
                             f"{lp99:>10} {lworst:>10} "
                             f"{work_tot:>10} {work_min:>10} {work_max:>10}")

        # Mixed workload table
        has_mixed = any(
            s.get("mixed", {}).get("latency", {}).get("samples", 0) > 0
            for s in schedulers.values())
        if has_mixed:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'STATUS':>8} "
                        f"{'LAT P99':>10} {'WORST':>10} "
                        f"{'WORK TOT':>10} {'WORK MIN':>10} {'WORK MAX':>10}")
            for sched_name, sched_data in schedulers.items():
                mx = sched_data.get("mixed", {})
                if not mx:
                    continue
                status = "OK" if mx.get("survived", True) else "CRASHED"
                lat = mx.get("latency", {})
                mp99 = (f"{lat['p99_us']}us"
                        if lat.get("samples", 0) > 0 else "--")
                mworst = (f"{lat['worst_us']}us"
                          if lat.get("samples", 0) > 0 else "--")
                work_tot = str(mx.get("work_total", 0))
                work_min = str(mx.get("work_min", 0))
                work_max = str(mx.get("work_max", 0))
                lines.append(f"{sched_name:<28} {status:>8} "
                             f"{mp99:>10} {mworst:>10} "
                             f"{work_tot:>10} {work_min:>10} {work_max:>10}")

        # Deadline table
        has_deadline = any(
            s.get("deadline", {}).get("total_frames", 0) > 0
            for s in schedulers.values())
        if has_deadline:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'STATUS':>8} "
                        f"{'MISSES':>8} {'TOTAL':>8} "
                        f"{'RATIO':>8} {'JIT P99':>10} {'WORST':>10}")
            for sched_name, sched_data in schedulers.items():
                dl = sched_data.get("deadline", {})
                if not dl or dl.get("total_frames", 0) == 0:
                    continue
                status = "OK" if dl.get("survived", True) else "CRASHED"
                missed = str(dl.get("missed_frames", 0))
                total = str(dl.get("total_frames", 0))
                ratio = f"{dl.get('miss_ratio', 0):.1%}"
                jp99 = f"{dl['jitter_p99_us']}us"
                jworst = f"{dl['jitter_worst_us']}us"
                lines.append(f"{sched_name:<28} {status:>8} "
                             f"{missed:>8} {total:>8} "
                             f"{ratio:>8} {jp99:>10} {jworst:>10}")

        # IPC table
        has_ipc = any(
            s.get("ipc", {}).get("total_ops", 0) > 0
            for s in schedulers.values())
        if has_ipc:
            lines.append("")
            _thru = bool(data.get("ipc_only"))
            _hdr = (f"{'SCHEDULER':<28} {'STATUS':>8} {'PRIM':>8} "
                    f"{'P50':>9} {'P99':>9} {'P99.9':>9} {'WORST':>9}")
            if _thru:
                _hdr += f" {'RT/s':>10}"
            lines.append(_hdr)
            for sched_name, sched_data in schedulers.items():
                ipc = sched_data.get("ipc", {})
                if not ipc or ipc.get("total_ops", 0) == 0:
                    continue
                status = "OK" if ipc.get("survived", True) else "CRASHED"
                cell = ipc.get("cell", {})
                first = True
                for prim in ("pipe", "socket", "eventfd", "sem", "fanout"):
                    key = prim if prim == "fanout" else f"rtt_{prim}"
                    d = cell.get(key)
                    if not d:
                        continue
                    nm = sched_name if first else ""
                    st = status if first else ""
                    first = False
                    row = (f"{nm:<28} {st:>8} {prim:>8} "
                           f"{str(d['p50'])+'us':>9} {str(d['p99'])+'us':>9} "
                           f"{str(d['p999'])+'us':>9} {str(d['worst'])+'us':>9}")
                    if _thru:
                        # implied round-trips/sec from the median RTT (1 RT ~= p50)
                        rts = int(1e6 / d['p50']) if d.get('p50') else 0
                        row += f" {rts:>10}"
                    lines.append(row)
                # montauk deep analysis of this scheduler's capture (--ipc only)
                if _thru:
                    mon = ipc.get("montauk") or {}
                    for mk in ("wake2run", "locality", "holder", "fractal"):
                        mv = mon.get(mk)
                        if mv:
                            lines.append(f"{'':<20}{mk}: {mv}")

        # Launch table
        has_launch = any(
            s.get("launch", {}).get("launches", 0) > 0
            for s in schedulers.values())
        if has_launch:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'STATUS':>8} "
                        f"{'COUNT':>8} {'MEAN':>10} "
                        f"{'P99':>10} {'WORST':>10}")
            for sched_name, sched_data in schedulers.items():
                lnch = sched_data.get("launch", {})
                if not lnch or lnch.get("launches", 0) == 0:
                    continue
                status = "OK" if lnch.get("survived", True) else "CRASHED"
                count = str(lnch.get("launches", 0))
                mean = f"{lnch['launch_mean_us']}us"
                p99 = f"{lnch['launch_p99_us']}us"
                worst = f"{lnch['launch_worst_us']}us"
                lines.append(f"{sched_name:<28} {status:>8} "
                             f"{count:>8} {mean:>10} "
                             f"{p99:>10} {worst:>10}")

        lines.append("")

    # Measurement repeatability (Gauge R&R) on throughput -- gates whether the
    # bench can resolve scheduler deltas from run-to-run noise. Needs >=2
    # iterations; silent on single-iteration runs.
    grr_rows = []
    for cores_str in sorted_cores:
        per_sched = {}
        for sched, sd in results[cores_str].items():
            t = sd.get("throughput", {}).get("times", [])
            if len(t) >= 2:
                per_sched[sched] = t
        g = gauge_rr(per_sched)
        if g:
            grr_rows.append(f"{cores_str + 'C':>6} {g['grr_pct']:>7.1f} "
                            f"{g['icc']:>6.2f} {g['within_cv']:>9.2f}  "
                            f"{g['verdict']}")
    if grr_rows:
        lines.append("MEASUREMENT REPEATABILITY (GAUGE R&R, THROUGHPUT)")
        lines.append(f"{'CORES':>6} {'%GRR':>7} {'ICC':>6} {'WITHIN_CV':>9}  VERDICT")
        lines.extend(grr_rows)
        lines.append("")

    # Summary matrix: throughput delta vs EEVDF
    all_schedulers = []
    for cores_str in sorted_cores:
        for name in results[cores_str]:
            if name not in all_schedulers:
                all_schedulers.append(name)

    if len(all_schedulers) > 1 and len(sorted_cores) > 1:
        lines.append("THROUGHPUT VS EEVDF (NEGATIVE = FASTER)")
        header = f"{'SCHEDULER':<28}"
        for c in sorted_cores:
            header += f" {c + 'C':>8}"
        lines.append(header)

        for sched in all_schedulers:
            if sched == all_schedulers[0]:
                continue
            row = f"{sched:<28}"
            for c in sorted_cores:
                tp = results.get(c, {}).get(sched, {}).get("throughput", {})
                delta = tp.get("vs_eevdf_pct")
                if delta is not None:
                    row += f" {delta:>+7.1f}%"
                else:
                    row += f" {'--':>8}"
            lines.append(row)

        lines.append("")

        lines.append("LATENCY P99 (us)")
        header = f"{'SCHEDULER':<28}"
        for c in sorted_cores:
            header += f" {c + 'C':>8}"
        lines.append(header)

        for sched in all_schedulers:
            row = f"{sched:<28}"
            for c in sorted_cores:
                lat = results.get(c, {}).get(sched, {}).get("latency", {})
                p99 = lat.get("p99_us")
                if p99 is not None and lat.get("samples", 0) > 0:
                    row += f" {p99:>8}"
                else:
                    row += f" {'--':>8}"
            lines.append(row)

        lines.append("")

        # Burst summary matrix
        has_any_burst = any(
            results.get(c, {}).get(s, {}).get("burst", {})
            .get("burst", {}).get("samples", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_burst:
            lines.append("BURST P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    br = results.get(c, {}).get(sched, {}).get("burst", {})
                    burst = br.get("burst", {})
                    p99 = burst.get("p99_us")
                    if p99 is not None and burst.get("samples", 0) > 0:
                        survived = br.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(p99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

        # Long-run summary matrix
        has_any_longrun = any(
            results.get(c, {}).get(s, {}).get("longrun", {})
            .get("latency", {}).get("samples", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_longrun:
            lines.append("LONG-RUN LATENCY P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    lr = results.get(c, {}).get(sched, {}).get("longrun", {})
                    lat = lr.get("latency", {})
                    p99 = lat.get("p99_us")
                    if p99 is not None and lat.get("samples", 0) > 0:
                        survived = lr.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(p99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

            lines.append("LONG-RUN WORK (MIN PER-PROCESS)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    lr = results.get(c, {}).get(sched, {}).get("longrun", {})
                    work_min = lr.get("work_min")
                    if work_min is not None and work_min > 0:
                        row += f" {work_min:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

            lines.append("LONG-RUN WORK (MAX PER-PROCESS)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    lr = results.get(c, {}).get(sched, {}).get("longrun", {})
                    work_max = lr.get("work_max")
                    if work_max is not None and work_max > 0:
                        row += f" {work_max:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

        # Mixed summary matrix
        has_any_mixed = any(
            results.get(c, {}).get(s, {}).get("mixed", {})
            .get("latency", {}).get("samples", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_mixed:
            lines.append("MIXED LATENCY P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    mx = results.get(c, {}).get(sched, {}).get("mixed", {})
                    lat = mx.get("latency", {})
                    p99 = lat.get("p99_us")
                    if p99 is not None and lat.get("samples", 0) > 0:
                        survived = mx.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(p99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

            lines.append("MIXED WORK MIN (PER-PROCESS)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    mx = results.get(c, {}).get(sched, {}).get("mixed", {})
                    work_min = mx.get("work_min")
                    if work_min is not None and work_min > 0:
                        row += f" {work_min:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

            lines.append("MIXED WORK MAX (PER-PROCESS)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    mx = results.get(c, {}).get(sched, {}).get("mixed", {})
                    work_max = mx.get("work_max")
                    if work_max is not None and work_max > 0:
                        row += f" {work_max:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

        # Deadline jitter summary matrix
        has_any_deadline = any(
            results.get(c, {}).get(s, {}).get("deadline", {})
            .get("total_frames", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_deadline:
            lines.append("DEADLINE JITTER P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    dl = results.get(c, {}).get(sched, {}).get("deadline", {})
                    jp99 = dl.get("jitter_p99_us")
                    if jp99 is not None and dl.get("total_frames", 0) > 0:
                        survived = dl.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(jp99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

            lines.append("DEADLINE MISS RATIO")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    dl = results.get(c, {}).get(sched, {}).get("deadline", {})
                    ratio = dl.get("miss_ratio")
                    if ratio is not None and dl.get("total_frames", 0) > 0:
                        row += f" {ratio:>7.1%}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

        # IPC round-trip summary matrix
        has_any_ipc = any(
            results.get(c, {}).get(s, {}).get("ipc", {})
            .get("total_ops", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_ipc:
            lines.append("IPC PIPE RTT P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    ipc = results.get(c, {}).get(sched, {}).get("ipc", {})
                    rp99 = ipc.get("rtt_p99_us")
                    if rp99 is not None and ipc.get("total_ops", 0) > 0:
                        survived = ipc.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(rp99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

        # App launch summary matrix
        has_any_launch = any(
            results.get(c, {}).get(s, {}).get("launch", {})
            .get("launches", 0) > 0
            for c in sorted_cores for s in all_schedulers)
        if has_any_launch:
            lines.append("APP LAUNCH P99 (us)")
            header = f"{'SCHEDULER':<28}"
            for c in sorted_cores:
                header += f" {c + 'C':>8}"
            lines.append(header)

            for sched in all_schedulers:
                row = f"{sched:<28}"
                for c in sorted_cores:
                    lnch = results.get(c, {}).get(sched, {}).get("launch", {})
                    lp99 = lnch.get("launch_p99_us")
                    if lp99 is not None and lnch.get("launches", 0) > 0:
                        survived = lnch.get("survived", True)
                        tag = "" if survived else "*"
                        row += f" {str(lp99) + tag:>8}"
                    else:
                        row += f" {'--':>8}"
                lines.append(row)

            lines.append("")

    return "\n".join(lines)


# BENCH-PCPU: PER-CPU DSQ VISIBILITY TESTS
# DIRECTLY ATTACKS THE THREE LAYERS THAT PREVENT PER-CPU DSQ STARVATION:
#   1. BOUNDED DEPTH GATE (BURST MODE THRESHOLD=1)
#   2. L2 WORK STEALING (DISPATCH CHECKS SIBLING PER-CPU DSQs)
#   3. PER-CPU SOJOURN RESCUE (TICK DETECTS STALE TASKS AT 10MS)

def fire_burst_timed(count: int,
                     work_secs: float = 0.5) -> list[subprocess.Popen]:
    """Spawn count short-lived CPU-bound processes that report elapsed time.

    Each process records wall-clock elapsed time. If a process doing 0.5s
    of work takes 35s wall-clock, it was starved for approximately 34.5s.
    Output: one float per line on stdout (elapsed seconds)."""
    script = (
        "import time,hashlib\n"
        "start=time.monotonic()\n"
        f"end=start+{work_secs}\n"
        "while time.monotonic()<end:\n"
        " hashlib.sha256(b'x'*4096).hexdigest()\n"
        "print(f'{time.monotonic()-start:.4f}')\n"
    )
    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    return procs


def collect_burst_times(procs: list[subprocess.Popen],
                        timeout: float = 60.0) -> list[float]:
    """Collect elapsed times from timed burst processes."""
    times = []
    for p in procs:
        try:
            stdout, _ = p.communicate(timeout=timeout)
            for line in stdout.decode(errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        times.append(float(line))
                    except ValueError:
                        pass
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    return times


def measure_burst_starvation(binary: Path, n_cpus: int,
                             burst_size: int = 300,
                             burst_work_secs: float = 0.5) -> dict:
    """Reproduce the CS2 starvation scenario that killed v5.4.1.

    Full CPU saturation + massive fork/exec storm. Measures:
    - Max scheduling delay of burst tasks (elapsed - work_secs)
    - Probe wake-to-run latency during burst
    - Recovery latency after burst clears

    PASS: max_delay < 2s AND probe p99 < 50000us during burst
    FAIL: any burst task starved > 2s (per-CPU DSQ visibility broken)
    """
    if n_cpus < 2:
        return {"survived": True, "pass": True, "skip": True}

    log_info(f"[burst-starvation] {n_cpus}C, {burst_size} burst tasks, "
             f"{burst_work_secs}s work each")

    # SATURATE ALL CPUs
    workers = []
    for cpu in range(n_cpus):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # WARMUP: LET SCHEDULER CLASSIFY WORKLOAD
    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    # BASELINE PROBE (5S)
    log_info("[burst-starvation] Baseline: 5s")
    baseline_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    baseline_probe.send_signal(signal.SIGINT)
    try:
        baseline_out, _ = baseline_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        baseline_probe.kill()
        baseline_out, _ = baseline_probe.communicate()
    baseline = parse_probe_output(baseline_out.decode(errors="replace"))
    log_info(f"[burst-starvation] Baseline: median={baseline['median_us']}us "
             f"p99={baseline['p99_us']}us")

    # START PROBE FOR BURST MEASUREMENT
    burst_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    # DETONATE: FIRE TIMED BURST
    log_info(f"[burst-starvation] Firing {burst_size} burst tasks")
    burst_start = time.monotonic()
    burst_procs = fire_burst_timed(burst_size, burst_work_secs)

    # COLLECT BURST TASK WALL-CLOCK TIMES
    burst_times = collect_burst_times(burst_procs, timeout=60)
    burst_duration = time.monotonic() - burst_start
    log_info(f"[burst-starvation] Burst complete: {burst_duration:.1f}s, "
             f"{len(burst_times)} tasks reported")

    # SETTLING
    remaining = max(0, 15 - burst_duration)
    if remaining > 0:
        time.sleep(remaining)

    # STOP BURST PROBE
    burst_probe.send_signal(signal.SIGINT)
    try:
        burst_out, _ = burst_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        burst_probe.kill()
        burst_out, _ = burst_probe.communicate()
    burst_lat = parse_probe_output(burst_out.decode(errors="replace"))

    # RECOVERY PROBE (5S)
    log_info("[burst-starvation] Recovery: 5s")
    recovery_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    recovery_probe.send_signal(signal.SIGINT)
    try:
        recovery_out, _ = recovery_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        recovery_probe.kill()
        recovery_out, _ = recovery_probe.communicate()
    recovery = parse_probe_output(recovery_out.decode(errors="replace"))

    # STOP STRESS WORKERS
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    # ANALYZE BURST TASK DELAYS
    max_delay = 0.0
    delays = []
    for t in burst_times:
        delay = max(0, t - burst_work_secs)
        delays.append(delay)
        if delay > max_delay:
            max_delay = delay

    p99_delay = percentile(delays, 99) if delays else 0
    median_delay = percentile(delays, 50) if delays else 0

    passed = max_delay < 2.0 and burst_lat["p99_us"] < 50000

    verdict = "PASS" if passed else "FAIL"
    log_info(f"[burst-starvation] Task delay: max={max_delay:.3f}s "
             f"p99={p99_delay:.3f}s median={median_delay:.3f}s")
    log_info(f"[burst-starvation] Probe during burst: "
             f"p99={burst_lat['p99_us']}us worst={burst_lat['worst_us']}us")
    log_info(f"[burst-starvation] Recovery: p99={recovery['p99_us']}us")
    log_info(f"[burst-starvation] {verdict}")

    return {
        "survived": True,
        "pass": passed,
        "n_cpus": n_cpus,
        "burst_size": burst_size,
        "burst_duration_s": round(burst_duration, 1),
        "tasks_reported": len(burst_times),
        "max_delay_s": round(max_delay, 3),
        "p99_delay_s": round(p99_delay, 3),
        "median_delay_s": round(median_delay, 3),
        "baseline": baseline,
        "burst_latency": burst_lat,
        "recovery": recovery,
    }


def measure_work_stealing(binary: Path, n_cpus: int) -> dict:
    """Test L2 work stealing under asymmetric load.

    Pin stress workers to half the CPUs (saturate them). Probe floats
    across all CPUs. Work stealing should pull tasks from saturated CPUs'
    per-CPU DSQs to idle siblings in the same L2 domain.

    Compares probe latency under asymmetric load (half saturated) vs
    symmetric load (all saturated). If work stealing works, the ratio
    stays bounded.

    PASS: asymmetric p99 < 5x symmetric p99
    """
    if n_cpus < 4:
        return {"survived": True, "pass": True, "skip": True}

    half = n_cpus // 2
    log_info(f"[work-stealing] {n_cpus}C, saturating {half} CPUs, "
             f"{n_cpus - half} idle for stealing")

    # PHASE 1: SYMMETRIC (ALL CPUs SATURATED) -- BASELINE
    log_info("[work-stealing] Phase 1: symmetric (all CPUs saturated)")
    sym_workers = []
    for cpu in range(n_cpus):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sym_workers.append(p)

    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    sym_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(10)
    sym_probe.send_signal(signal.SIGINT)
    try:
        sym_out, _ = sym_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        sym_probe.kill()
        sym_out, _ = sym_probe.communicate()
    symmetric = parse_probe_output(sym_out.decode(errors="replace"))

    for w in sym_workers:
        w.send_signal(signal.SIGINT)
    for w in sym_workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    log_info(f"[work-stealing] Symmetric: median={symmetric['median_us']}us "
             f"p99={symmetric['p99_us']}us worst={symmetric['worst_us']}us")

    time.sleep(2)

    # PHASE 2: ASYMMETRIC (HALF CPUs SATURATED)
    log_info(f"[work-stealing] Phase 2: asymmetric ({half} CPUs saturated)")
    asym_workers = []
    for cpu in range(half):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        asym_workers.append(p)

    warmup = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    warmup.send_signal(signal.SIGINT)
    try:
        warmup.wait(timeout=5)
    except subprocess.TimeoutExpired:
        warmup.kill()
        warmup.wait()

    asym_probe = subprocess.Popen(
        [str(binary), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(10)
    asym_probe.send_signal(signal.SIGINT)
    try:
        asym_out, _ = asym_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        asym_probe.kill()
        asym_out, _ = asym_probe.communicate()
    asymmetric = parse_probe_output(asym_out.decode(errors="replace"))

    for w in asym_workers:
        w.send_signal(signal.SIGINT)
    for w in asym_workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    log_info(f"[work-stealing] Asymmetric: median={asymmetric['median_us']}us "
             f"p99={asymmetric['p99_us']}us worst={asymmetric['worst_us']}us")

    # ANALYSIS: RATIO OF ASYMMETRIC TO SYMMETRIC
    sym_p99 = max(symmetric["p99_us"], 1)
    asym_p99 = asymmetric["p99_us"]
    ratio = asym_p99 / sym_p99

    # ASYMMETRIC SHOULD BE BETTER THAN SYMMETRIC (HALF THE CPUs ARE FREE)
    # IF WORK STEALING IS BROKEN, TASKS ON SATURATED CPUs ROT,
    # AND RATIO COULD EXCEED 5X.
    passed = ratio < 5.0

    verdict = "PASS" if passed else "FAIL"
    log_info(f"[work-stealing] Ratio: {ratio:.1f}x (asym/sym p99)")
    log_info(f"[work-stealing] {verdict}")

    return {
        "survived": True,
        "pass": passed,
        "n_cpus": n_cpus,
        "half_saturated": half,
        "symmetric": symmetric,
        "asymmetric": asymmetric,
        "ratio": round(ratio, 2),
    }


def measure_sojourn_ceiling(binary: Path, n_cpus: int,
                            wave_count: int = 5,
                            tasks_per_wave: int = 50,
                            work_secs: float = 0.01) -> dict:
    """Test per-CPU sojourn rescue under sustained full saturation.

    All CPUs saturated with stress workers. Repeated waves of short-lived
    tasks (10ms work each) are fired into the system. The depth gate and
    work stealing handle most tasks. The sojourn rescue in tick() catches
    any that slip through -- stale sojourn_stamp_pcpu triggers a kick within
    the sojourn threshold (5ms default).

    PASS: no task waits > 1s
    """
    if n_cpus < 2:
        return {"survived": True, "pass": True, "skip": True}

    log_info(f"[sojourn-ceiling] {n_cpus}C, {wave_count} waves of "
             f"{tasks_per_wave} tasks ({work_secs}s work each)")

    # SATURATE ALL CPUs
    workers = []
    for cpu in range(n_cpus):
        p = subprocess.Popen(
            [str(binary), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)
    time.sleep(3)

    # FIRE WAVES OF SHORT TASKS
    all_delays = []
    max_delay = 0.0

    for wave in range(wave_count):
        log_info(f"[sojourn-ceiling] Wave {wave + 1}/{wave_count}: "
                 f"{tasks_per_wave} tasks")
        procs = fire_burst_timed(tasks_per_wave, work_secs)
        times = collect_burst_times(procs, timeout=30)

        for t in times:
            delay = max(0, t - work_secs)
            all_delays.append(delay)
            if delay > max_delay:
                max_delay = delay

        log_info(f"[sojourn-ceiling] Wave {wave + 1}: "
                 f"{len(times)} tasks, max_delay={max_delay:.3f}s")
        time.sleep(2)

    # STOP STRESS WORKERS
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()

    p99_delay = percentile(all_delays, 99) if all_delays else 0
    median_delay = percentile(all_delays, 50) if all_delays else 0
    passed = max_delay < 1.0

    verdict = "PASS" if passed else "FAIL"
    log_info(f"[sojourn-ceiling] Total: {len(all_delays)} tasks, "
             f"max={max_delay:.3f}s p99={p99_delay:.3f}s "
             f"median={median_delay:.3f}s")
    log_info(f"[sojourn-ceiling] {verdict}")

    return {
        "survived": True,
        "pass": passed,
        "n_cpus": n_cpus,
        "waves": wave_count,
        "tasks_per_wave": tasks_per_wave,
        "total_tasks": len(all_delays),
        "max_delay_s": round(max_delay, 3),
        "p99_delay_s": round(p99_delay, 3),
        "median_delay_s": round(median_delay, 3),
    }


def measure_pcpu_starvation(binary: Path, n_cpus: int,
                            duration_secs: float = 20.0) -> dict:
    """Regression test for the 2026-04-09 / 2026-04-12 per-CPU DSQ
    starvation crashes (v5.6.0 waterfall bug).

    Pathology: kworker/0:2 and systemd-journal[430] were stranded on
    a per-CPU DSQ for 35-39 seconds while the owning CPU ran a
    long-slice CPU hog. The old 8-step waterfall short-circuited
    before reaching the per-CPU DSQ on every dispatch; the new
    urgency-score dispatch scores every DSQ on every pass, so a
    stalled per-CPU DSQ cannot be skipped.

    This test pins:
      - one CPU hog on CPU 0 (tight loop, never yields)
      - one high-wakeup emitter on CPU 0 (dd bs=1 to /dev/null,
        syscall-per-byte wakeup storm with no voluntary yield)
    to the same CPU via taskset, then measures the emitter's
    wall-clock completion time vs a reference upper bound.

    PASS: emitter completes, no watchdog-kill in dmesg,
          elapsed < 2x duration_secs (generous upper bound).
    FAIL: dmesg crash pattern, emitter timeout, non-zero exit,
          or sched_ext disabled during the test.
    """
    if n_cpus < 2:
        log_info(f"[pcpu-starve] skip: n_cpus={n_cpus} (need >= 2)")
        return {"survived": True, "pass": True, "skip": True}

    target_cpu = 0
    # TARGET BYTES SIZED TO COMPLETE IN ~duration_secs UNDER A
    # HEALTHY SCHEDULER (10K OPS/SEC IS CONSERVATIVE FOR dd bs=1).
    emitter_target_bytes = int(duration_secs * 10000)

    log_info(f"[pcpu-starve] pinning hog + emitter to CPU {target_cpu}, "
             f"target {emitter_target_bytes} emitter ops, "
             f"timeout={duration_secs * 2:.0f}s")

    dmesg = DmesgMonitor()

    # CPU HOG: TIGHT LOOP, NEVER YIELDS, PINNED TO target_cpu.
    # RUNS FOR duration_secs THEN EXITS.
    hog = subprocess.Popen(
        ["taskset", "-c", str(target_cpu), "sh", "-c",
         f"timeout {duration_secs} sh -c 'while :; do :; done'"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # EMITTER: HIGH-WAKEUP-RATE TASK PINNED TO SAME CPU.
    # dd bs=1 PRODUCES A SYSCALL PER BYTE, SO IT CYCLES
    # RUNNABLE -> RUNNING -> RUNNABLE THOUSANDS OF TIMES PER SECOND.
    # THIS IS THE PATTERN kworker/systemd-journal EXHIBIT:
    # FREQUENT SHORT WAKEUPS, PINNED TO ONE CPU.
    emit_start = time.monotonic()
    emitter = subprocess.Popen(
        ["taskset", "-c", str(target_cpu), "dd",
         "if=/dev/urandom", "of=/dev/null",
         "bs=1", f"count={emitter_target_bytes}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        emitter_rc = emitter.wait(timeout=duration_secs * 2.5)
    except subprocess.TimeoutExpired:
        emitter.kill()
        emitter.wait()
        emitter_rc = -1
    emit_elapsed = time.monotonic() - emit_start

    # CLEAN UP HOG (SHOULD HAVE EXITED ON TIMEOUT ALREADY).
    try:
        hog.wait(timeout=5)
    except subprocess.TimeoutExpired:
        hog.terminate()
        try:
            hog.wait(timeout=2)
        except subprocess.TimeoutExpired:
            hog.kill()
            hog.wait()

    dmesg.check()

    # PASS CRITERIA: NO DMESG CRASH, EMITTER COMPLETED, ELAPSED < 2x BOUND.
    passed = (not dmesg.crashed
              and emitter_rc == 0
              and emit_elapsed < duration_secs * 2.0)

    verdict = "PASS" if passed else "FAIL"
    log_info(f"[pcpu-starve] emitter rc={emitter_rc} "
             f"elapsed={emit_elapsed:.1f}s bound={duration_secs * 2:.1f}s "
             f"crash={dmesg.crashed}  {verdict}")

    return {
        "survived": not dmesg.crashed,
        "pass": passed,
        "emitter_elapsed_s": round(emit_elapsed, 2),
        "emitter_rc": emitter_rc,
        "crashed": dmesg.crashed,
        "crash_msg": dmesg.crash_msg,
    }


# Every installed production scx scheduler, for the traced field sweep (mirrors
# prism-fork-thread's --all-scx list). scx_chaos (fault injection) is excluded;
# scx_layered self-skips without a layer spec.
SCX_FIELD = [
    "scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
    "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
    "scx_beerland", "scx_layered",
]


def field_arms(n_cpus: int, schedulers: str = "", all_scx: bool = False,
               pandemonium_only: bool = False):
    """Build the (sched_name, activate_cmd) arms for a traced field run.

    EEVDF is always the neutral baseline arm. The default and --all-scx keep
    PANDEMONIUM; an explicit --schedulers list runs EXACTLY what it names (plus
    the EEVDF baseline) -- PANDEMONIUM is in only if the caller named it.
      no flag        -> EEVDF + PANDEMONIUM
      --all-scx      -> EEVDF + PANDEMONIUM + every installed external
      --schedulers L -> EEVDF + each named (PANDEMONIUM only if in L)
    pandemonium_only wins over all of the above: PANDEMONIUM alone, no EEVDF
    arm, no externals. This is the ONE implementation of the flag; every
    subparser that accepts it threads it here.
    Uninstalled externals are warned and skipped, never fatal.
    """
    # Accept either a comma string ("scx_rusty,scx_lavd") or an already-split
    # list -- callers thread args.schedulers through getattr/subprocess and a
    # list slips through; normalize so the .split below never sees a non-str.
    if isinstance(schedulers, (list, tuple)):
        schedulers = ",".join(str(s) for s in schedulers)
    pand = ("PANDEMONIUM", [str(BINARY), "--nr-cpus", str(n_cpus)])
    if pandemonium_only:
        return [pand]
    arms = [("EEVDF", None)]
    if all_scx:
        arms.append(pand)
        for s in SCX_FIELD:
            if find_scheduler(s):
                arms.append((s, [s]))
            else:
                log_warn(f"{s} not found, skipping")
    elif schedulers:
        for raw in schedulers.split(","):
            s = raw.strip()
            if not s:
                continue
            low = s.lower()
            if low in ("pandemonium", "scx_pandemonium"):
                arms.append(pand)
            elif low == "eevdf":
                continue  # already the baseline
            elif find_scheduler(s):
                arms.append((s, [s]))
            else:
                log_warn(f"{s} not found, skipping")
    else:
        arms.append(pand)
    return arms


def trace_pcpu_burst(stamp: str, n_cpus: int, duration: int,
                     schedulers: str = "", all_scx: bool = False,
                     pandemonium_only: bool = False) -> int:
    """One montauk-traced burst-starvation capture, hard-capped at `duration`s.
    Saturates every CPU, then detonates fork/exec bursts for the window while
    montauk records the per-event wake-to-run -- so the worst burst wakeup is in
    the .events for montauk_analyze --digest. Trace IS the artifact; no
    probe/baseline/recovery measurement here. Workers run the `pandemonium`
    binary (stress-worker / burst tasks), so montauk targets comm `pandemonium`.
    montauk pins a drain core (CPUs are saturated) so it never drops events."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    drain = max(0, n_cpus - 1)
    log_info(f"[pcpu-burst] tracing burst-starvation {n_cpus}C for {duration}s "
             f"(montauk on cpu{drain})")

    def body(rec_dir):
        end = time.monotonic() + duration
        workers = [subprocess.Popen(
                       [str(BINARY), "stress-worker", "--cpu", str(c)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                   for c in range(n_cpus)]
        try:
            time.sleep(min(2.0, duration * 0.1))  # let the scheduler classify
            burst_size = max(n_cpus * 20, 100)
            while time.monotonic() < end:
                procs = fire_burst_timed(burst_size, 0.5)
                collect_burst_times(
                    procs, timeout=max(1, int(end - time.monotonic())))
        finally:
            for w in workers:
                w.send_signal(signal.SIGINT)
            for w in workers:
                try:
                    w.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    w.kill()
                    w.wait()
        return None

    # Arms on the IDENTICAL sustained burst. EEVDF (activate_cmd=None) is the
    # kernel baseline -- the only way to know whether a deep queueing tail is a
    # PANDEMONIUM deficiency or just the physics of 20x oversubscription. The
    # field (default EEVDF+PANDEMONIUM; widened by --schedulers/--all-scx) runs
    # the same body. The stress-worker/burst tasks are the `pandemonium` binary
    # under every scheduler, so montauk targets comm `pandemonium` for all arms.
    arms = field_arms(n_cpus, schedulers, all_scx, pandemonium_only)
    traced = 0
    for sched_name, activate_cmd in arms:
        rec_dir, _ = trace_workload(sched_name, activate_cmd,
                                    "pandemonium", f"pcpu-burst-{n_cpus}c", stamp,
                                    body, events=True, pin_cpu=drain)
        if rec_dir is None:
            log_error(f"[pcpu-burst] {sched_name} failed to activate -- skipped")
            continue
        log_info(f"[pcpu-burst] {sched_name} montauk recording -> {rec_dir}")
        traced += 1
    return 0 if traced else 1


def _cpufreq_governor_paths():
    import glob
    return sorted(glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"))


def _set_governor(gov):
    """Best-effort: set every CPU's cpufreq governor to `gov`, returning a
    {path: old} map for restore. scaling_governor is root-write (we run under
    sudo). If `gov` is not in scaling_available_governors the platform lacks it
    -- warn and leave governors untouched; the cold cycle still runs, just not at
    the worst-case ramp. Returns {} when nothing was changed."""
    paths = _cpufreq_governor_paths()
    if not paths:
        log_warn("[cold-wake] no cpufreq governor sysfs -- governor unchanged")
        return {}
    try:
        avail = open(paths[0].replace("scaling_governor",
                                      "scaling_available_governors")).read().split()
    except OSError:
        avail = []
    if avail and gov not in avail:
        log_warn(f"[cold-wake] governor '{gov}' unavailable "
                 f"(have: {' '.join(avail)}) -- governor unchanged")
        return {}
    saved = {}
    for p in paths:
        try:
            saved[p] = open(p).read().strip()
        except OSError:
            pass
    script = "; ".join(f"echo {gov} > {p}" for p in paths)
    r = subprocess.run(["sudo", "sh", "-c", script],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        log_warn("[cold-wake] could not set governor -- continuing as-is")
        return {}
    log_info(f"[cold-wake] governor set to {gov} ({len(paths)} CPUs)")
    return saved


def _restore_governor(saved):
    if not saved:
        return
    script = "; ".join(f"echo {old} > {p}" for p, old in saved.items())
    subprocess.run(["sudo", "sh", "-c", script],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_info("[cold-wake] governor restored")


def _coldq(vals, f):
    """f-quantile of a list by nearest-rank (vals need not be sorted)."""
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * f))]


def _bytes_lbl(b):
    if b >= 1 << 20: return f"{b // (1 << 20)}MB"
    if b >= 1 << 10: return f"{b // (1 << 10)}KB"
    return f"{b}B"


def _parse_coldwork_sizes(text):
    """coldwork SIZE / MEM lines -> {"cpu": {iters: d}, "mem": {bytes: d}} where d
    is {cn:[cold ns], cr:[cold ratio], wn:[warm ns], wr:[warm ratio]}. SIZE is the
    register CPU burst (frequency ramp); MEM is the pointer-chase (cold caches)."""
    out = {"cpu": {}, "mem": {}}
    for ln in text.splitlines():
        p = ln.split()
        if len(p) == 8 and p[2] == "COLD" and p[5] == "WARM" and p[0] in ("SIZE", "MEM"):
            try:
                key, cn, cr, wn, wr = int(p[1]), int(p[3]), int(p[4]), int(p[6]), int(p[7])
            except ValueError:
                continue
            grp = out["cpu" if p[0] == "SIZE" else "mem"]
            d = grp.setdefault(key, {"cn": [], "cr": [], "wn": [], "wr": []})
            d["cn"].append(cn); d["cr"].append(cr); d["wn"].append(wn); d["wr"].append(wr)
    return out


def _parse_coldwork_starve(text):
    """coldwork STARVE lines -> [{phase, interval_us, samples, mean_ns, worst_ns,
    over1ms, over10ms}]. Each is a pinned-waker sub-phase; worst_ns is the longest
    a runnable pinned task waited to be dispatched -- the dispatch-stall signal."""
    rows = []
    for ln in text.splitlines():
        p = ln.split()
        if len(p) == 14 and p[0] == "STARVE":
            try:
                rows.append({
                    "phase": p[1], "interval_us": int(p[3]), "samples": int(p[5]),
                    "mean_ns": int(p[7]), "worst_ns": int(p[9]),
                    "over1ms": int(p[11]), "over10ms": int(p[13]),
                })
            except (ValueError, IndexError):
                continue
    return rows


def trace_coldwake_cycle(stamp, n_cpus, duration, dwell=2,
                         sizes="100000,500000,1000000,4000000,16000000,50000000",
                         mem_sizes="32768,262144,2097152,8388608,33554432,134217728",
                         schedulers="", all_scx=False, pandemonium_only=False) -> int:
    """montauk-traced cold-core ramp capture with VARIED burst sizes. A single
    fixed quantum averages a fast frequency ramp away -- if the core ramps in the
    first few ms, a 50ms burst runs mostly warm and the cold start is invisible.
    But a user feels SHORT bursts (a cursor move, a keypress, a menu open), so this
    sweeps `sizes` (iteration counts) from sub-millisecond to tens of ms and
    measures each off a genuinely cold core against a warmed reference. The
    cold/warm penalty at each size says AT WHAT WORK SIZE a cold core costs the
    user -- the small end is where a fast ramp shows. aperf/mperf gives the actual
    delivered frequency per burst (cause beside effect).

    Governor pinned to powersave for the window, restored after. montauk traces
    comm `coldwork`. EEVDF baseline + PANDEMONIUM by default -- the same sweep runs
    under every arm, so the per-size penalty is the scheduler comparison."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    # Build the ramp generator: a deterministic CPU quantum with a distinct comm.
    # Unique per-run path -- a fixed /tmp/coldwork left root-owned by a prior sudo
    # run is unwritable by a later non-root gcc ("cannot open output file").
    src = Path(__file__).resolve().parent
    cwbin = f"/tmp/coldwork-{stamp}"   # name kept so montauk's comm trace target is unchanged
    cc = subprocess.run(["gcc", "-O2", "-march=native", "-pthread", "-o", cwbin,
                         str(src / "loadgen.c"), str(src / "loadgen_common.c")],
                        capture_output=True, text=True)
    if cc.returncode != 0:
        log_error(f"[cold-wake] coldwork build failed: {cc.stderr.strip()}")
        return 1
    drain = max(0, n_cpus - 1)            # montauk's pinned drain core
    wake_core = 1 if n_cpus > 2 else 0    # the core we let go cold (not the drain)
    nsz = len([s for s in sizes.split(",") if s.strip()])
    nmem = len([s for s in mem_sizes.split(",") if s.strip()])
    log_info(f"[cold-wake] cold-core sweep on cpu{wake_core}: idle({dwell}s) -> "
             f"{nsz} CPU burst sizes + {nmem} memory working sets, {duration}s per "
             f"arm (montauk on cpu{drain})")
    # Susceptibility profile up front: whether THIS box can even produce the dark
    # strand. A LOW score is the answer to "my machines never reproduce it."
    susc_lines, susc_score = stall_susceptibility()
    for ln in susc_lines:
        log_info(f"[cold-wake] {ln}" if ln == susc_lines[0] else f"           {ln}")
    if susc_score < 4:
        log_warn("[cold-wake] LOW susceptibility -- if no stall appears, the box "
                 "config (HZ/tickless/cores), not the scheduler, is likely why")
    saved_gov = _set_governor("powersave")

    def body(rec_dir):
        # Run under sudo so coldwork can read /dev/cpu/N/msr for the aperf/mperf
        # ratio (sudo creds were cached by cmd_bench_coldwake's `sudo true`).
        r = subprocess.run(["sudo", cwbin, "cache", str(wake_core), str(int(dwell * 1000)),
                            sizes, mem_sizes, str(duration)],
                           capture_output=True, text=True)
        # Persist the raw SIZE rows into the recording so the per-size ramp is
        # self-describing and re-analyzable, not just on the terminal.
        try:
            (Path(rec_dir) / "coldwork-quanta.txt").write_text(r.stdout)
        except OSError:
            pass
        return _parse_coldwork_sizes(r.stdout)

    try:
        arms = field_arms(n_cpus, schedulers, all_scx, pandemonium_only)
        traced = 0
        worst = []   # (sched_name, max_penalty_pct, size_ms) for the cross-arm verdict
        starve_worst = []   # (sched_name, worst_dispatch_ns) -- the stall verdict
        for sched_name, activate_cmd in arms:
            rec_dir, by_size = trace_workload(sched_name, activate_cmd,
                                              "coldwork", f"cold-wake-{n_cpus}c", stamp,
                                              body, events=True, pin_cpu=drain)
            if rec_dir is None:
                log_error(f"[cold-wake] {sched_name} failed to activate -- skipped")
                continue
            data = by_size or {"cpu": {}, "mem": {}}
            # CPU sweep: the frequency ramp (small -- base->boost over a few ms).
            log_info(f"[cold-wake] {sched_name} CPU sweep -- frequency ramp "
                     f"(cold burst off idle vs warm):")
            for it in sorted(data.get("cpu", {})):
                d = data["cpu"][it]
                cold = _coldq(d["cn"], 0.5); warm = _coldq(d["wn"], 0.5)
                cr = _coldq([x for x in d["cr"] if x], 0.5)
                wr = _coldq([x for x in d["wr"] if x], 0.5)
                pen = (100.0 * (cold - warm) / warm) if warm else 0.0
                fq = (f"freq {cr / 1000:.2f}x->{wr / 1000:.2f}x" if (cr or wr) else "")
                log_info(f"    cpu ~{warm / 1e6:6.2f}ms: cold {cold / 1e6:6.2f}ms "
                         f"{pen:+4.0f}%  {fq}")
            # MEMORY sweep: the cold-cache penalty -- the felt one, scaling with the
            # working set (milliseconds for L3-sized work off a cold core).
            log_info(f"[cold-wake] {sched_name} MEMORY sweep -- cold caches "
                     f"(pointer-chase off idle vs warm):")
            mem_pen, mem_lbl = 0.0, ""
            for by in sorted(data.get("mem", {})):
                d = data["mem"][by]
                cold = _coldq(d["cn"], 0.5); warm = _coldq(d["wn"], 0.5)
                pen = (100.0 * (cold - warm) / warm) if warm else 0.0
                mult = (cold / warm) if warm else 1.0
                log_info(f"    mem {_bytes_lbl(by):>6}: cold {cold / 1e6:8.3f}ms vs "
                         f"warm {warm / 1e6:8.3f}ms  {mult:.1f}x ({pen:+.0f}%)")
                if pen > mem_pen:
                    mem_pen, mem_lbl = pen, _bytes_lbl(by)
            worst.append((sched_name, mem_pen, mem_lbl))

            # STARVE capture: the dispatch-stall cell on the same arm. A pinned
            # waker (cannot be stolen) measures how long a runnable task waits to
            # be dispatched -- IDLE (alone on an idle core) and HOG (behind a
            # non-yielding hog on the same core). This is the kworker-stall repro
            # the cold-cache sweep above structurally cannot induce. Its own
            # montauk capture so wake2run is analyzable apart from the cache wakes.
            def starve_body(rec_dir):
                r = subprocess.run(["sudo", cwbin, "starve", str(wake_core),
                                    str(duration)],
                                   capture_output=True, text=True)
                try:
                    (Path(rec_dir) / "coldwork-starve.txt").write_text(r.stdout)
                    (Path(rec_dir) / "machine-profile.txt").write_text(
                        "\n".join(susc_lines) + "\n")
                except OSError:
                    pass
                return _parse_coldwork_starve(r.stdout)

            srec, srows = trace_workload(sched_name, activate_cmd, "coldwork",
                                         f"cold-wake-starve-{n_cpus}c", stamp,
                                         starve_body, events=True, pin_cpu=drain)
            if srec is not None and srows:
                log_info(f"[cold-wake] {sched_name} STARVE -- pinned-waker "
                         f"dispatch latency (worst = longest a runnable pinned "
                         f"task waited):")
                arm_worst = 0
                for sr in srows:
                    arm_worst = max(arm_worst, sr["worst_ns"])
                    flag = "  <-- STALL" if sr["worst_ns"] >= 100000000 else ""
                    log_info(f"    {sr['phase']:>4} @{sr['interval_us'] // 1000:>2}ms wake: "
                             f"worst {sr['worst_ns'] / 1e6:8.2f}ms  mean "
                             f"{sr['mean_ns'] / 1e3:6.0f}us  "
                             f">1ms {sr['over1ms']:>4}  >10ms {sr['over10ms']:>3}{flag}")
                starve_worst.append((sched_name, arm_worst))
            traced += 1
        # Cross-arm verdict on the cold-cache penalty (the felt regime). The
        # scheduler barely moves cache coldness -- it is a platform cost -- but
        # report it so a real difference would show.
        for name, mp, lbl in worst:
            log_info(f"[cold-wake] {name}: worst cold-cache penalty {mp:+.0f}% "
                     f"at {lbl} working set")
        if len(worst) >= 2:
            best = min(worst, key=lambda t: t[1])
            log_info(f"[cold-wake] lowest cold-cache penalty: {best[0]} ({best[1]:+.0f}%)")
        # STARVE verdict: the dispatch-stall signal. A worst dispatch latency in
        # the hundreds of ms (let alone seconds) is the stall reproducing -- a
        # runnable pinned task the scheduler left un-dispatched.
        for name, w in starve_worst:
            tag = "  STALL REPRODUCED" if w >= 100000000 else ("  elevated" if w >= 10000000 else "")
            log_info(f"[cold-wake] {name}: worst pinned-waker dispatch latency "
                     f"{w / 1e6:.1f}ms{tag}")
        return 0 if traced else 1
    finally:
        _restore_governor(saved_gov)
        subprocess.run(["sudo", "rm", "-f", cwbin], capture_output=True)


def cmd_bench_coldwake(args) -> int:
    """Cold-wake latency vs frequency-at-wake. Trace-only: cycles a pinned core
    idle->bare-wake under powersave so montauk's COLD-WAKE block can tell a
    governor / architecture frequency-ramp apart from a scheduler dispatch delay.
    Mirrors prism-power's idle framing; the montauk trace is the artifact."""
    if os.geteuid() != 0:
        # SELF-ELEVATE: cold-wake needs root end-to-end -- montauk eBPF attach,
        # sched_ext activation, governor write, /dev/cpu/N/msr -- and the trace
        # dir under /tmp/pandemonium is root-owned from prior runs, so a non-root
        # mkdir there fails. Re-exec under sudo so `./pandemonium.py prism-coldwake`
        # works without a sudo prefix, matching prism's main().
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    warm_sudo()
    # The aperf/mperf frequency read needs /dev/cpu/N/msr (the msr module). Load
    # it best-effort; coldwork reports freq 0 and the bench notes n/a if absent.
    subprocess.run(["sudo", "modprobe", "msr"], capture_output=True)
    nuke_stale_build()
    if not build():
        return 1
    # Clear any registered scheduler before the arms run -- above all the systemd
    # pandemonium service (back after a reboot). Without this the EEVDF arm traces
    # under whatever is loaded and the PANDEMONIUM arm wedges on "stale scheduler
    # ... still registered". Same pre-flight stop prism-scale uses.
    if is_scx_active():
        log_warn(f"sched_ext active ({scx_scheduler_name()}) -- stopping "
                 f"pandemonium service")
        stop_systemd_scheduler()
        if not wait_for_deactivation(5.0):
            log_error("could not deactivate sched_ext -- clear it "
                      "(sudo systemctl stop pandemonium)")
            return 1
    n_cpus = get_online_cpus()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if getattr(args, "storm", False):
        return trace_storm_cycle(stamp, n_cpus, args.duration,
                                 busy_per_cpu=args.busy_per_cpu,
                                 sleep_us=args.rt_sleep_us, spin_us=args.rt_spin_us,
                                 schedulers=args.schedulers, all_scx=args.all_scx,
                                 pandemonium_only=getattr(args, "pandemonium_only", False))
    return trace_coldwake_cycle(stamp, n_cpus, args.duration, dwell=args.dwell,
                                sizes=args.sizes, mem_sizes=args.mem_sizes,
                                schedulers=args.schedulers, all_scx=args.all_scx,
                                pandemonium_only=getattr(args, "pandemonium_only", False))


def _storm_captured(json_text: str):
    """montauk v7.17.0+: the --json envelope carries an explicit
    montauk_analysis_storm_captured gauge (0 = storm probes unattached, the
    surface was never measured). Returns True/False, or None when the envelope
    is unreadable (older analyzer) so the caller falls back to the text verdict."""
    try:
        env = json.loads(json_text)
        for r in env.get("reports", []):
            for g in r.get("gauges", []):
                if g.get("name") == "montauk_analysis_storm_captured":
                    return bool(g.get("value"))
    except (ValueError, AttributeError):
        pass
    return None


def _parse_storm_report(text: str) -> dict:
    """Parse `montauk_analyze --report storm` output into a verdict dict. montauk --
    not a scheduler-tick scrape -- is the storm observer: the StormReport VERDICT line
    carries the fraction, the REAL-IPI-vs-IDLE-churn kind and the kick/reenqueue rates."""
    d = {"kind": None, "pct": 0.0, "storm": 0, "intervals": 0,
         "p50": 0, "peak": 0, "kick_s": 0, "preempt_s": 0, "reenq_s": 0}
    # Not-captured is ABSENCE, never a zero: the captured: 0 header is stamped
    # by trace_storm_cycle from the analyzer's montauk_analysis_storm_captured
    # gauge, and the text verdict is the analyzer's own not-captured wording.
    if text.startswith("captured: 0") or "no sched_ext kick activity captured" in text:
        d["captured"] = False
        d["error"] = ("storm surface not measured (storm probes off -- "
                      "MONTAUK_SCX_STORM); absence, not a zero")
        return d
    m = re.search(r"VERDICT:\s*(.+)", text)
    if not m:
        d["error"] = "no storm verdict in analyzer output (trace empty or analyzer error)"
        return d
    d["captured"] = True
    v = m.group(1)
    km = re.match(r"([^;]+)", v)
    d["kind"] = km.group(1).strip() if km else v.strip()
    si = re.search(r"storm (\d+)/(\d+) intervals \(([\d.]+)%\)", v)
    if si:
        d["storm"], d["intervals"], d["pct"] = int(si.group(1)), int(si.group(2)), float(si.group(3))
    for key, pat in (("p50", r"p50=(\d+)"), ("peak", r"peak=(\d+)"),
                     ("kick_s", r"kick/s=(\d+)"), ("preempt_s", r"preempt (\d+)"),
                     ("reenq_s", r"reenq/s=(\d+)")):
        mm = re.search(pat, v)
        if mm:
            d[key] = int(mm.group(1))
    return d


def trace_storm_cycle(stamp, n_cpus, duration, busy_per_cpu=4,
                      sleep_us=100, spin_us=10,
                      schedulers="", all_scx=False, pandemonium_only=False) -> int:
    """cpu_release kick-storm reproducer -- the test that actually recreates the
    reboot live-lock (cold-wake measures cold caches, a different cost). Under
    powersave, stormwork drives the boot condition: a busy sched_ext population
    plus one SCHED_FIFO thread per CPU whose every wake yanks its CPU from scx
    (cpu_release -> reenqueue_local). montauk traces the flood and its StormReport
    (`montauk_analyze --report storm`) names the storm fraction and -- via fentry
    counters on scx_bpf_kick_cpu/reenqueue_local -- REAL IPI storm vs IDLE re-enqueue
    churn. EEVDF cannot storm (no Tier-0 kick loop), so the scored arms are the
    PANDEMONIUM variants; default is BPF."""
    # cpu_release is DEPRECATED on kernel 7.1+ (sched_ext for-6.19 rework) and
    # reworked into a deferred async reenqueue. This bench FLOODS cpu_release by
    # design -- one SCHED_FIFO thread per CPU yanks its CPU from scx every wake --
    # which hard-locks the box on 7.1+. The storm% it scores is a pre-7.1 failure
    # mode. Skip rather than freeze.
    if cpu_release_deprecated():
        log_warn(f"[storm] skipped on kernel {os.uname().release}: the cpu_release "
                 "kick-storm reproducer floods an op deprecated in 7.1+ and hard-locks "
                 "the box. Pre-7.1 only.")
        return 0
    src = Path(__file__).resolve().parent
    swbin = f"/tmp/stormwork-{stamp}"   # name kept so montauk's comm trace target is unchanged
    cc = subprocess.run(["gcc", "-O2", "-march=native", "-pthread", "-o", swbin,
                         str(src / "loadgen.c"), str(src / "loadgen_common.c")],
                        capture_output=True, text=True)
    if cc.returncode != 0:
        log_error(f"[storm] stormwork build failed: {cc.stderr.strip()}")
        return 1

    if schedulers:
        arms = [a for a in field_arms(n_cpus, schedulers, all_scx,
                                      pandemonium_only) if a[1] is not None]
    else:
        arms = [("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]),
                ("PANDEMONIUM (ADAPTIVE)", [str(BINARY)])]
    if not arms:
        log_error("[storm] no scx arm to score (EEVDF cannot storm)")
        subprocess.run(["sudo", "rm", "-f", swbin], capture_output=True)
        return 1

    log_info(f"[storm] cpu_release flood: {n_cpus} RT FIFO threads + "
             f"{n_cpus * busy_per_cpu} busy workers, {duration}s/arm under powersave")
    saved_gov = _set_governor("powersave")

    results = []
    try:
        for sched_name, cmd in arms:
            vcmd = list(cmd)
            if "--verbose" not in vcmd:
                vcmd.insert(1, "--verbose")
            guard = start_and_wait(vcmd, sched_name)
            if guard is None:
                log_error(f"[storm] {sched_name} failed to activate -- skipped")
                continue
            tag = sched_name.replace(" ", "_").replace("(", "").replace(")", "")
            # montauk -- not a tick-log scrape -- observes the storm: capture a trace
            # while loadgen floods, then read it back through `--report storm` (the
            # StormReport: fentry counters on scx_bpf_kick_cpu / reenqueue_local).
            trace = f"/tmp/storm-{tag}-{stamp}.bin"
            log_info(f"[storm] {sched_name}: flooding {duration}s under montauk")
            with open(trace + ".err", "w") as ef:
                # MONTAUK_SCX_STORM DISABLED -- v7.1 HARD-LOCK.
                # Setting it opts in the fentry/fexit trampolines on sched_ext's
                # hot kfuncs (scx_bpf_kick_cpu / scx_bpf_reenqueue_local). On the
                # 7.1 kernel this box runs, arming them under a live scx load
                # HARD-LOCKS the machine -- a full manual power-cycle, reproduced
                # 2026-07-14. montauk's "attach while scx is quiescent" protocol
                # did NOT prevent it: the probes stay live across the sweep and
                # the box froze anyway. Until a freeze-safe kick observer exists
                # (a tracepoint on the reschedule IPI path, not a live-text
                # trampoline on the kfunc), this capture runs with the probes
                # OFF. The var is scrubbed from the child env, not merely unset,
                # so an ambient `export MONTAUK_SCX_STORM=1` in the caller's shell
                # (the exact route that froze the box) cannot re-arm it here. With
                # the probes off the storm report honestly reads "not captured"
                # (montauk v7.17.0), never a fabricated kick/s=0.
                storm_env = {k: v for k, v in os.environ.items()
                             if k != "MONTAUK_SCX_STORM"}
                mon = subprocess.Popen([MONTAUK, "--trace", "stormwork",
                                        "--trace-out", trace],
                                       stdout=subprocess.DEVNULL, stderr=ef,
                                       env=storm_env)
                time.sleep(2.0)
                if mon.poll() is None:
                    subprocess.run(["sudo", swbin, "storm", str(duration),
                                    str(busy_per_cpu), str(sleep_us), str(spin_us)],
                                   capture_output=True, text=True)
                    mon.send_signal(signal.SIGINT)
                    try:
                        mon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        mon.kill()
                        mon.wait()
                else:
                    log_warn(f"[storm] {sched_name}: montauk trace did not attach")
            guard.stop()
            wait_for_deactivation(5.0)
            rep = subprocess.run([MONTAUK + "_analyze", trace, "--report", "storm"],
                                 capture_output=True, text=True)
            # v7.17.0: query the captured bit as structured data and stamp it
            # into the artifact NOW -- the trace is deleted below, so this is
            # the only moment absence-vs-zero can be recorded. An unmeasured
            # storm surface must never fossilize as an empty .storm file.
            jrep = subprocess.run([MONTAUK + "_analyze", trace, "--report", "storm", "--json"],
                                  capture_output=True, text=True)
            cap = _storm_captured(jrep.stdout)
            body = rep.stdout
            if cap is False:
                body = ("captured: 0\n"
                        "STORM: not captured -- storm probes off (MONTAUK_SCX_STORM "
                        "gated); montauk_analysis_storm_captured=0\n") + body
            elif cap is True:
                body = "captured: 1\n" + body
            out = LOG_DIR / f"storm-{tag}-{stamp}.storm"
            try:
                out.write_text(body)
            except OSError:
                out = None
            try:
                os.unlink(trace)  # the full storm trace is huge; the verdict is the artifact
            except OSError:
                pass
            results.append((sched_name, _parse_storm_report(body), out))
            guard.cleanup()

        for sched_name, d, out in results:
            if "error" in d:
                log_warn(f"[storm] {sched_name}: {d['error']}")
                continue
            log_info(f"[storm] {sched_name}: {d['pct']:.1f}% storm "
                     f"({d['storm']}/{d['intervals']} intervals), reenq/s "
                     f"p50={d['p50']} peak={d['peak']}")
            log_info(f"[storm] {sched_name}: {d['kind']} "
                     f"(kick/s={d['kick_s']} preempt={d['preempt_s']} "
                     f"reenq/s={d['reenq_s']})")
            if out:
                log_info(f"STORMLOG: {out}")
        return 0 if results else 1
    finally:
        _restore_governor(saved_gov)
        subprocess.run(["sudo", "rm", "-f", swbin], capture_output=True)


def cmd_bench_pcpu(args) -> int:
    """Per-CPU DSQ visibility stress test.

    Four tests attack per-CPU DSQ reachability:
      burst-starvation: Reproduces the CS2 starvation scenario
      work-stealing:    Tests L2 work stealing under asymmetric load
      sojourn-ceiling:  Tests per-CPU sojourn rescue under full saturation
      pcpu-starvation:  Regression test for v5.6.0 watchdog crashes
                        (kworker/0:2, systemd-journal[430]). Pins a
                        never-yielding CPU hog and a high-wakeup emitter
                        to the same CPU and asserts the emitter runs.

    Requires sudo. Runs at current online CPU count by default.
    """
    warm_sudo()
    nuke_stale_build()
    if not build():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.core_counts:
        core_counts = [int(c.strip()) for c in args.core_counts.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = compute_core_counts(max_cpus)

    version = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # --trace: capped montauk recordings of the burst-starvation load at EVERY
    # core width (2,4,8,...,max -- the suite's scaling), each its own capture, so
    # the width-specific cadence (2C strand vs wide spread) is visible. restrict
    # the online CPUs per width like the matrix path, restore after.
    if getattr(args, "trace", False):
        if not log.child:
            log_info(f"PANDEMONIUMv{version} BENCH-PCPU --trace  core counts: {core_counts}")
        rc = 0
        try:
            for nr in core_counts:
                # restrict_cpus only offlines -- restore to all-online first so an
                # ascending sweep (2,4,8,..) actually widens instead of staying at
                # the narrowest width.
                restore_all_cpus(max_cpus)
                if not restrict_cpus(nr, max_cpus):
                    log_warn(f"[pcpu-burst] could not restrict to {nr}C -- skipped")
                    continue
                rc |= trace_pcpu_burst(stamp, nr, args.duration,
                                       getattr(args, "schedulers", "") or "",
                                       getattr(args, "all_scx", False),
                                       getattr(args, "pandemonium_only", False))
        finally:
            restore_all_cpus(max_cpus)
        return rc

    log_info(f"PANDEMONIUMv{version} BENCH-PCPU")
    log_info(f"Core counts: {core_counts}")
    log_info(f"Iterations: {args.iterations}")

    all_results = {}
    overall_pass = True

    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] Starting per-CPU DSQ tests")

            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] Failed to restrict CPUs")
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(2)

            core_results = {"burst": [], "steal": [], "sojourn": [], "pcpu_starve": []}

            for iteration in range(1, args.iterations + 1):
                log_info(f"[{nr_cpus}C] Iteration {iteration}/{args.iterations}")

                # START SCHEDULER
                guard = start_and_wait(
                    [str(BINARY), "--nr-cpus", str(nr_cpus)],
                    "PANDEMONIUM", settle_secs=3)
                if guard is None:
                    log_error(f"[{nr_cpus}C] Scheduler failed to start")
                    core_results["burst"].append({"survived": False, "pass": False})
                    core_results["steal"].append({"survived": False, "pass": False})
                    core_results["sojourn"].append({"survived": False, "pass": False})
                    core_results["pcpu_starve"].append({"survived": False, "pass": False})
                    overall_pass = False
                    continue

                dmesg = DmesgMonitor()

                try:
                    # TEST 1: BURST STARVATION
                    burst_size = max(nr_cpus * 20, 100)
                    r = measure_burst_starvation(BINARY, nr_cpus,
                                                 burst_size=burst_size)
                    core_results["burst"].append(r)
                    if not r.get("pass", False) and not r.get("skip", False):
                        overall_pass = False

                    dmesg.check()
                    if dmesg.crashed:
                        log_error(f"[{nr_cpus}C] Crash detected after burst test")
                        overall_pass = False
                        stop_and_wait(guard)
                        continue

                    time.sleep(3)

                    # TEST 2: WORK STEALING
                    r = measure_work_stealing(BINARY, nr_cpus)
                    core_results["steal"].append(r)
                    if not r.get("pass", False) and not r.get("skip", False):
                        overall_pass = False

                    dmesg.check()
                    if dmesg.crashed:
                        log_error(f"[{nr_cpus}C] Crash detected after steal test")
                        overall_pass = False
                        stop_and_wait(guard)
                        continue

                    time.sleep(3)

                    # TEST 3: SOJOURN CEILING
                    r = measure_sojourn_ceiling(BINARY, nr_cpus)
                    core_results["sojourn"].append(r)
                    if not r.get("pass", False) and not r.get("skip", False):
                        overall_pass = False

                    dmesg.check()
                    if dmesg.crashed:
                        log_error(f"[{nr_cpus}C] Crash detected after sojourn test")
                        overall_pass = False
                        stop_and_wait(guard)
                        continue

                    time.sleep(3)

                    # TEST 4: PER-CPU DSQ STARVATION REGRESSION (v5.6.0 BUG)
                    r = measure_pcpu_starvation(BINARY, nr_cpus)
                    core_results["pcpu_starve"].append(r)
                    if not r.get("pass", False) and not r.get("skip", False):
                        overall_pass = False

                finally:
                    dmesg.save()
                    stdout = stop_and_wait(guard)
                    time.sleep(2)

            all_results[nr_cpus] = core_results

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
        overall_pass = False
    finally:
        restore_all_cpus(max_cpus)

    # REPORT
    log_info("")
    log_info(f"PANDEMONIUMv{version} BENCH-PCPU RESULTS")
    log_info("")

    for nr_cpus in sorted(all_results.keys()):
        cr = all_results[nr_cpus]
        log_info(f"  {nr_cpus}C:")

        for test_name in ["burst", "steal", "sojourn", "pcpu_starve"]:
            results = cr[test_name]
            if not results:
                continue
            passes = sum(1 for r in results
                         if r.get("pass", False) or r.get("skip", False))
            total = len(results)
            label = "PASS" if passes == total else "FAIL"

            detail = ""
            if test_name == "burst" and results:
                r = results[-1]
                if not r.get("skip"):
                    detail = (f"max_delay={r.get('max_delay_s', '?')}s "
                              f"burst_p99={r.get('burst_latency', {}).get('p99_us', '?')}us")
            elif test_name == "steal" and results:
                r = results[-1]
                if not r.get("skip"):
                    detail = (f"ratio={r.get('ratio', '?')}x "
                              f"asym_p99={r.get('asymmetric', {}).get('p99_us', '?')}us")
            elif test_name == "sojourn" and results:
                r = results[-1]
                if not r.get("skip"):
                    detail = (f"max_delay={r.get('max_delay_s', '?')}s "
                              f"p99_delay={r.get('p99_delay_s', '?')}s")
            elif test_name == "pcpu_starve" and results:
                r = results[-1]
                if not r.get("skip"):
                    detail = (f"emitter_elapsed={r.get('emitter_elapsed_s', '?')}s "
                              f"rc={r.get('emitter_rc', '?')}")

            log_info(f"    {test_name:20s} {label} ({passes}/{total})  {detail}")

    log_info("")
    if overall_pass:
        log_info("OVERALL: PASS")
    else:
        log_info("OVERALL: FAIL")

    # PROMETHEUS OUTPUT (unified schema via the shared builder; metadata is now a
    # real _info/_timestamp gauge pair, not file comments)
    prom_path = LOG_DIR / f"prism-pcpu-{stamp}.prom"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pb = PrometheusBuilder("pcpu")
    git = get_git_info()
    pb.info(version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    for nr_cpus in sorted(all_results.keys()):
        cr = all_results[nr_cpus]
        for test_name in ["burst", "steal", "sojourn", "pcpu_starve"]:
            results = cr[test_name]
            if not results:
                continue
            r = results[-1]
            lbl = {"test": test_name, "cpus": str(nr_cpus)}
            pb.gauge("pass", 1 if r.get("pass", False) else 0,
                     help="per-test pass (1) / fail (0)", labels=lbl)
            if test_name == "burst" and not r.get("skip"):
                pb.gauge("burst_max_delay_s", r.get("max_delay_s", 0), labels=lbl)
                bl = r.get("burst_latency", {})
                pb.gauge("burst_p99_us", bl.get("p99_us", 0), labels=lbl)
            elif test_name == "steal" and not r.get("skip"):
                pb.gauge("steal_ratio", r.get("ratio", 0), labels=lbl)
            elif test_name == "sojourn" and not r.get("skip"):
                pb.gauge("sojourn_max_delay_s", r.get("max_delay_s", 0), labels=lbl)
            elif test_name == "pcpu_starve" and not r.get("skip"):
                pb.gauge("starve_elapsed_s", r.get("emitter_elapsed_s", 0), labels=lbl)
    prom_path.write_text(pb.render())
    log_info(f"METRICS: {prom_path}")

    return 0 if overall_pass else 1


# BENCH-SCALE COMMAND

def entries_for_cores(
    base_entries: list[tuple[str, list[str] | None]],
    n: int,
) -> list[tuple[str, list[str] | None]]:
    """Adjust scheduler commands for a specific core count.

    PANDEMONIUM variants get --nr-cpus N.
    External schedulers see the online CPUs via kernel.
    EEVDF is None (no scheduler process).
    """
    adjusted = []
    for name, cmd in base_entries:
        if cmd is None:
            adjusted.append((name, None))
        elif "PANDEMONIUM" in name:
            adjusted.append((name, cmd + ["--nr-cpus", str(n)]))
        else:
            adjusted.append((name, list(cmd)))
    return adjusted


def cmd_bench_scale(args) -> int:
    """Unified benchmark: throughput + latency at each core count."""

    warm_sudo()

    dmesg = DmesgMonitor()


    nuke_stale_build()

    if not build():
        return 1

    # Stop any active sched_ext scheduler
    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping pandemonium service")
        stop_systemd_scheduler()
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext -- is another scheduler running?")
            return 1

    # Restore all CPUs (previous run may have left some offline)
    possible = get_possible_cpus()
    restore_all_cpus(possible)
    time.sleep(0.5)

    # Pre-flight: verify PANDEMONIUM can load BPF and activate
    if not log.child:
        log_info("Pre-flight: verifying PANDEMONIUM can activate...")
    preflight = start_and_wait([str(BINARY)], "PANDEMONIUM")
    if preflight is None:
        log_error("Pre-flight FAILED -- PANDEMONIUM cannot activate")
        log_error("Fix the error above before running prism-scale")
        dmesg.save()
        return 1
    stop_and_wait(preflight)
    if not log.child:
        log_info("Pre-flight PASSED")
        log.blank()

    # Build entry list
    if getattr(args, "pandemonium_only", False):
        base_entries: list[tuple[str, list[str] | None]] = [
            ("PANDEMONIUM (BPF)", [str(BINARY), "--verbose", "--no-adaptive"]),
            ("PANDEMONIUM (ADAPTIVE)", [str(BINARY), "--verbose"]),
        ]
        log_info("PANDEMONIUM-ONLY MODE: skipping EEVDF and external schedulers")
    else:
        # A named --schedulers field is EEVDF vs EXACTLY those (PANDEMONIUM only if
        # named) -- consistent with field_arms / prism-cachyos. Default keeps
        # PANDEMONIUM. schedulers may arrive as a list or a comma string.
        _sl = args.schedulers or []
        if isinstance(_sl, str):
            _sl = [s.strip() for s in _sl.split(",") if s.strip()]
        _named = {str(s).strip().lower() for s in _sl}
        _field_only = bool(_sl) and not getattr(args, "all_scx", False)
        base_entries: list[tuple[str, list[str] | None]] = [("EEVDF", None)]
        if (not _field_only) or (_named & {"pandemonium", "scx_pandemonium"}):
            base_entries.append(("PANDEMONIUM (BPF)", [str(BINARY), "--verbose", "--no-adaptive"]))
            base_entries.append(("PANDEMONIUM (ADAPTIVE)", [str(BINARY), "--verbose"]))
        for name in _sl:
            if str(name).strip().lower() in ("pandemonium", "scx_pandemonium", "eevdf"):
                continue
            path = find_scheduler(name)
            if path:
                log_info(f"Found: {name} ({path})")
                base_entries.append((name, [name]))
            else:
                log_warn(f"SKIPPING {name} (not installed)")

    # Workload
    workload_cmd = args.cmd or f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
    clean_cmd = args.clean_cmd
    if not args.cmd:
        clean_cmd = f"cargo clean --target-dir {TARGET_DIR}"

    # Core counts (use possible, not online -- previous crash may have left CPUs offline)
    max_cpus = get_possible_cpus()
    if args.core_counts:
        core_counts = [int(c.strip()) for c in args.core_counts.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        if max_cpus not in core_counts:
            core_counts.append(max_cpus)
        core_counts.sort()
    else:
        core_counts = compute_core_counts(max_cpus)

    print()
    log_info(f"Schedulers: {', '.join(name for name, _ in base_entries)}")
    log_info(f"Core counts: {core_counts}")
    if args.deadline:
        log_info("Mode: DEADLINE ONLY (periodic frame jitter)")
    elif args.ipc:
        log_info("Mode: IPC ONLY (pipe round-trip latency)")
    elif args.launch:
        log_info("Mode: LAUNCH ONLY (fork+exec latency)")
    elif args.mixed:
        log_info("Mode: MIXED ONLY (burst + long-run combined)")
    elif args.longrun:
        log_info("Mode: LONG-RUN ONLY (skipping latency, throughput, burst)")
    elif args.burst:
        log_info("Mode: BURST ONLY (skipping latency + throughput)")
    else:
        log_info(f"Iterations: {args.iterations}")
        log_info(f"Workload: {workload_cmd}")
    print()

    # Data structure for all results
    version = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    data = {
        "version": version,
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "timestamp": stamp,
        "iterations": args.iterations,
        "burst_only": args.burst,
        "longrun_only": args.longrun,
        "mixed_only": args.mixed,
        "deadline_only": args.deadline,
        "ipc_only": args.ipc,
        "launch_only": args.launch,
        "max_cpus": max_cpus,
        "results": {},
    }

    eevdf_mean = {}  # {cores_str: mean_s} for vs_eevdf calculation

    with CpuGuard(max_cpus):
        restore_all_cpus(max_cpus)
        time.sleep(0.5)

        for n in core_counts:
            cores_str = str(n)
            data["results"][cores_str] = {}

            log_info(f"[{n} CORES]")

            if n < max_cpus:
                log_info(f"Restricting to {n} CPUs via hotplug...")
                if not restrict_cpus(n, max_cpus):
                    log_error(f"CPU hotplug failed for {n} cores -- skipping")
                    restore_all_cpus(max_cpus)
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)

            online = get_online_cpus()
            log_info(f"Online: {online} CPUs")
            print()

            entries = entries_for_cores(base_entries, n)

            for sched_name, sched_cmd in entries:
                log_info(f"Scheduler: {sched_name}")

                sched_result: dict = {
                    "throughput": {},
                    "latency": {},
                    "telemetry": {},
                }

                # Start scheduler (EEVDF = no-op)
                guard = None
                if sched_cmd is not None:
                    settle = 10.0 if "ADAPTIVE" in sched_name else 5.0
                    guard = start_and_wait(sched_cmd, sched_name,
                                          settle_secs=settle)
                    if guard is None:
                        print()
                        continue

                any_single = (args.burst or args.longrun or args.mixed
                              or args.deadline or args.ipc or args.launch)
                run_full = not any_single

                if run_full:
                    # Latency measurement
                    latency = measure_latency(BINARY, n,
                                              iterations=args.iterations)
                    sched_result["latency"] = latency

                    # Throughput measurement
                    times = []
                    for i in range(args.iterations):
                        log_info(f"Throughput iteration {i + 1}/{args.iterations}")
                        t = timed_run(workload_cmd, clean_cmd)
                        if t is None:
                            log_warn(f"Workload failed under {sched_name}")
                            break
                        times.append(t)

                    if times:
                        m, std = mean_stdev(times)
                        tp = {"times": [round(t, 2) for t in times],
                              "mean_s": round(m, 2),
                              "stdev_s": round(std, 2)}
                        if sched_name == "EEVDF":
                            eevdf_mean[cores_str] = m
                        elif (cores_str in eevdf_mean
                              and eevdf_mean[cores_str] > 0):
                            delta = ((m - eevdf_mean[cores_str])
                                     / eevdf_mean[cores_str]) * 100.0
                            tp["vs_eevdf_pct"] = round(delta, 1)
                        sched_result["throughput"] = tp

                if run_full or args.burst:
                    # Burst measurement (app launch under full load)
                    burst_size = n * 4
                    if burst_size < 8:
                        burst_size = 8
                    burst_result = measure_burst(BINARY, n, burst_size)
                    if sched_ejected(guard):
                        burst_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during burst "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["burst"] = burst_result

                if run_full or args.longrun:
                    # Long-running process test. Capture the probe's wake-to-run
                    # with montauk (pand-probe comm) so the interactive-stall tail
                    # lands in .events for montauk_analyze, not just p99/worst.
                    longrun_count = max(4, n // 2)
                    _safe = (sched_name.replace(" ", "-")
                             .replace("(", "").replace(")", ""))
                    if getattr(args, "trace", False) and montauk_available():
                        with montauk_trace(PROBE_COMM, f"longrun-{_safe}-{n}c",
                                           stamp, events=True) as _rec:
                            longrun_result = measure_longrun(
                                BINARY, n, longrun_count=longrun_count)
                        log_info(f"[{sched_name}] {n}C longrun montauk -> "
                                 f"{_rec.dir}")
                    else:
                        longrun_result = measure_longrun(
                            BINARY, n, longrun_count=longrun_count)
                    if sched_ejected(guard):
                        longrun_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during long-run "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["longrun"] = longrun_result

                if run_full or args.mixed:
                    # Mixed workload test (burst + longrun combined). Same montauk
                    # capture of the probe -- this phase shows the 195ms stall tail.
                    mixed_burst_size = n * 4
                    if mixed_burst_size < 8:
                        mixed_burst_size = 8
                    _safe = (sched_name.replace(" ", "-")
                             .replace("(", "").replace(")", ""))
                    if getattr(args, "trace", False) and montauk_available():
                        with montauk_trace(PROBE_COMM, f"mixed-{_safe}-{n}c",
                                           stamp, events=True) as _rec:
                            mixed_result = measure_mixed(
                                BINARY, n, longrun_count=max(4, n // 2),
                                burst_size=mixed_burst_size)
                        log_info(f"[{sched_name}] {n}C mixed montauk -> "
                                 f"{_rec.dir}")
                    else:
                        mixed_result = measure_mixed(
                            BINARY, n, longrun_count=max(4, n // 2),
                            burst_size=mixed_burst_size)
                    if sched_ejected(guard):
                        mixed_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during mixed test "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["mixed"] = mixed_result

                if run_full or args.deadline:
                    # Periodic deadline (frame scheduling jitter)
                    deadline_result = measure_deadline(BINARY, n)
                    if sched_ejected(guard):
                        deadline_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during deadline test "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["deadline"] = deadline_result

                _ipc_rec = None
                if run_full or args.ipc:
                    # IPC round-trip (pipe ping-pong). Under --trace, wrap the
                    # measurement in a montauk pand-ipc recording with the raw
                    # per-event log (events=True) -- the only instrument fine
                    # enough to resolve a single RTT landing on the CONFIG_HZ tick
                    # floor. The IPC engine is one clean handoff pair per primitive
                    # (cores unsaturated), so montauk needs no pinned drain core.
                    if getattr(args, "trace", False) and montauk_available():
                        safe = (sched_name.replace(" ", "-")
                                .replace("(", "").replace(")", ""))
                        with montauk_trace(IPC_COMM, f"ipc-{safe}-{n}c", stamp,
                                           events=True, sched_detail=True) as _rec:
                            ipc_result = measure_ipc(BINARY, n)
                        _ipc_rec = _rec
                        ipc_result["events"] = (str(_rec.events_path)
                                                if _rec.events_path else None)
                        log_info(f"[{sched_name}] {n}C IPC montauk trace -> "
                                 f"{_rec.dir}")
                    else:
                        ipc_result = measure_ipc(BINARY, n)
                    if sched_ejected(guard):
                        ipc_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during IPC test "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["ipc"] = ipc_result

                if run_full or args.launch:
                    # Application launch under load
                    launch_result = measure_launch(BINARY, n)
                    if sched_ejected(guard):
                        launch_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during launch test "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["launch"] = launch_result

                # Stop scheduler, capture telemetry
                stdout = stop_and_wait(guard)
                if stdout and "PANDEMONIUM" in sched_name:
                    ticks = parse_tick_lines(stdout)
                    knobs = parse_knobs_line(stdout)
                    # The IPC capture uses montauk_trace (not trace_workload), so the
                    # generic marker never ran -- write the cross-domain + migration
                    # marker into its recording dir from the drained shutdown stdout.
                    if _ipc_rec is not None:
                        _write_cross_domain_marker_text(stdout, _ipc_rec.dir)

                    # LONGRUN ACTIVATION VERIFICATION
                    longrun_ticks = [t for t in ticks if t.get("longrun_active")]
                    if longrun_ticks:
                        log_info(f"Longrun verification: detected in "
                                 f"{len(longrun_ticks)}/{len(ticks)} ticks")
                    sched_result["longrun_ticks"] = len(longrun_ticks)

                    sched_result["telemetry"] = {
                        "tick_count": len(ticks),
                        "tick_aggregate": aggregate_ticks(ticks),
                        "knobs": knobs,
                    }

                # COMPREHENSIVE IPC: fold montauk's deep analysis of the capture
                # (wake2run, cross-CCX locality, holder, fractal) into the result.
                # --ipc only; every other scale path leaves the capture untouched.
                if getattr(args, "ipc", False):
                    _ev = sched_result.get("ipc", {}).get("events")
                    if _ev and Path(_ev).exists():
                        sched_result["ipc"]["montauk"] = analyze_ipc_trace(_ev)

                data["results"][cores_str][sched_name] = sched_result
                print()

            # Restore CPUs for next round
            if n < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(0.5)

    # Report
    report = format_report(data)
    print()
    log.report(report)

    # Save log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LOG_DIR / f"prism-scale-{stamp}.log"
    report_path.write_text(report)
    log_info(f"REPORT: {report_path}")

    # Write Prometheus metrics
    prom_path = write_prometheus(data, stamp)
    log_info(f"METRICS: {prom_path}")

    # Dmesg
    dmesg.save(stamp)

    # Restart PANDEMONIUM service if it was running
    ret = subprocess.run(["systemctl", "is-enabled", "pandemonium"],
                         capture_output=True).returncode
    if ret == 0:
        log_info("Re-starting PANDEMONIUM service...")
        subprocess.run(["sudo", "systemctl", "start", "pandemonium"],
                       capture_output=True)
        if wait_for_activation(5.0):
            log_info("PANDEMONIUM service restored")
        else:
            log_warn("Failed to restart PANDEMONIUM service")

    fix_ownership()

    if not data["results"]:
        return 1
    return 0


# BENCH-SYS COMMAND

def cmd_bench_sys(args) -> int:
    """Live system telemetry capture. Run a scheduler, use your desktop,
    Ctrl+C when done. Writes Prometheus metrics from the session.

    --scheduler values:
        adaptive     PANDEMONIUM with adaptive control loop (default)
        no-adaptive  PANDEMONIUM BPF-only
        eevdf        No scheduler (kernel default)
        <name>       Any installed sched_ext scheduler (e.g. scx_bpfland)
    """

    scheduler = args.scheduler
    is_pandemonium = scheduler in ("adaptive", "no-adaptive")
    is_eevdf = scheduler == "eevdf"
    is_external = not is_pandemonium and not is_eevdf

    warm_sudo()

    dmesg = DmesgMonitor()

    # Only build PANDEMONIUM binary when we need it (pandemonium modes or probe)
    if is_pandemonium or args.with_probe:
        nuke_stale_build()
        if not build():
            return 1

    # Resolve external scheduler
    if is_external:
        ext_path = find_scheduler(scheduler)
        if not ext_path:
            log_error(f"Scheduler not found: {scheduler}")
            return 1
        log_info(f"Found: {scheduler} ({ext_path})")

    # Stop any active sched_ext scheduler
    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping")
        stop_systemd_scheduler()
        subprocess.run(["sudo", "killall", "-INT", "pandemonium"],
                       capture_output=True)
        if not wait_for_deactivation(5.0):
            subprocess.run(["sudo", "killall", "-KILL", "pandemonium"],
                           capture_output=True)
            if not wait_for_deactivation(3.0):
                log_error("Could not deactivate sched_ext")
                return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    # Build scheduler command
    guard = None
    if is_pandemonium:
        sched_cmd = [str(BINARY), "--verbose"]
        if scheduler == "no-adaptive":
            sched_cmd.append("--no-adaptive")
        for comp in (args.compositor or []):
            sched_cmd.extend(["--compositor", comp])
        sched_display = f"PANDEMONIUM ({'BPF' if scheduler == 'no-adaptive' else 'ADAPTIVE'})"
    elif is_external:
        sched_cmd = [scheduler]
        sched_display = scheduler
    else:
        sched_cmd = None
        sched_display = "EEVDF"

    # Start scheduler (EEVDF = no-op)
    if sched_cmd is not None:
        settle = 10.0 if scheduler == "adaptive" else 5.0
        guard = start_and_wait(sched_cmd, sched_display, settle_secs=settle)
        if guard is None:
            log_error(f"{sched_display} failed to activate")
            dmesg.save()
            return 1

    # Optionally start latency probe
    probe_proc = None
    if args.with_probe:
        if not BINARY.exists():
            log_error("Probe requires PANDEMONIUM binary "
                      "-- build failed or skipped")
            if guard:
                stop_and_wait(guard)
            return 1
        log_info("Starting latency probe (unpinned)")
        probe_proc = subprocess.Popen(
            [str(BINARY), "probe"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    # Create .prom file immediately with header
    version = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prom_path = ARCHIVE_DIR / f"{version}-{stamp}.prom"
    labels = {"scheduler": sched_display, "cores": str(max_cpus)}
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())

    prom_sys_create(prom_path, version, git, max_cpus)
    log_info(f"METRICS: {prom_path}")

    log_info(f"{sched_display} is active ({max_cpus} CPUs)")
    log_info("Use your system normally. Ctrl+C to stop and collect.")
    print()

    # Live loop: append ticks to .prom as they arrive
    ticks_written = 0
    try:
        while True:
            if sched_ejected(guard):
                log_warn(f"{sched_display} exited unexpectedly")
                break
            time.sleep(1)

            if is_pandemonium and guard is not None and guard.stdout_path:
                try:
                    stdout_text = Path(guard.stdout_path).read_text()
                except (FileNotFoundError, PermissionError):
                    continue
                ticks = parse_tick_lines(stdout_text)
                new_count = len(ticks) - ticks_written
                if new_count > 0:
                    now_ms = int(time.time() * 1000)
                    prom_sys_append_ticks(
                        prom_path, ticks[ticks_written:],
                        label_str, now_ms)
                    ticks_written = len(ticks)
    except KeyboardInterrupt:
        print()
        log_info("Stopping...")

    # Block SIGINT during final cleanup
    prev_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        # Collect probe data
        if probe_proc is not None:
            probe_proc.send_signal(signal.SIGINT)
            try:
                stdout_bytes, _ = probe_proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                probe_proc.kill()
                stdout_bytes, _ = probe_proc.communicate()
            probe_latency = parse_probe_output(
                stdout_bytes.decode(errors="replace"))
            prom_sys_append_probe(prom_path, probe_latency, label_str)
            log_info(f"Probe: {probe_latency['samples']} samples, "
                     f"median={probe_latency['median_us']}us, "
                     f"p99={probe_latency['p99_us']}us, "
                     f"worst={probe_latency['worst_us']}us")

        # Stop scheduler, flush remaining ticks + knobs
        sched_stdout = stop_and_wait(guard)
        if is_pandemonium and sched_stdout:
            ticks = parse_tick_lines(sched_stdout)
            if len(ticks) > ticks_written:
                now_ms = int(time.time() * 1000)
                prom_sys_append_ticks(
                    prom_path, ticks[ticks_written:],
                    label_str, now_ms)
                ticks_written = len(ticks)
            knobs = parse_knobs_line(sched_stdout)
            prom_sys_append_knobs(prom_path, knobs, label_str)

        # Console summary
        print()
        log_info(f"SESSION: {sched_display}, {max_cpus} CPUs, "
                 f"{ticks_written} ticks")
        log_info(f"METRICS: {prom_path}")

        dmesg.save(stamp)
        fix_ownership()

    except Exception:
        log_error("Cleanup failed:")
        traceback.print_exc()
        if guard is not None:
            try:
                guard.stop()
            except Exception:
                pass

    signal.signal(signal.SIGINT, prev_handler)

    # Restart PANDEMONIUM service if it was enabled
    ret = subprocess.run(["systemctl", "is-enabled", "pandemonium"],
                         capture_output=True).returncode
    if ret == 0:
        log_info("Re-starting PANDEMONIUM service...")
        subprocess.run(["sudo", "systemctl", "start", "pandemonium"],
                       capture_output=True)
        if wait_for_activation(5.0):
            log_info("PANDEMONIUM service restored")
        else:
            log_warn("Failed to restart PANDEMONIUM service")

    return 0


# BENCH-TRACE WORKLOAD GENERATORS

class _StressWorkers:
    """Background CPU stress saturating all cores."""

    def __init__(self, n_cpus):
        self.procs = []
        self.n = n_cpus

    def start(self):
        script = (
            "import hashlib\n"
            "d = b'stress' * 1000\n"
            "while True:\n"
            "    d = hashlib.sha256(d).digest()\n"
        )
        for _ in range(self.n):
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.procs.append(p)

    def stop(self):
        for p in self.procs:
            p.kill()
        for p in self.procs:
            p.wait()
        self.procs.clear()


class _LatencyProbe:
    """Wakeup latency via sleep/wake cycles."""

    def __init__(self, duration_secs):
        self.duration = duration_secs
        self.proc = None

    def start(self):
        # Rename the probe's comm to 'pand-cont' (prctl PR_SET_NAME) so a
        # montauk --trace pand-cont targets exactly this latency-sensitive task
        # under the storm -- the batch workers create the contention; this is the
        # wakee whose wake-to-run we want, without tracing the orchestrator.
        script = (
            f"import time, sys, ctypes, ctypes.util\n"
            f"ctypes.CDLL(ctypes.util.find_library('c')).prctl("
            f"15, b'pand-cont', 0, 0, 0)\n"
            f"end = time.monotonic() + {self.duration}\n"
            "while time.monotonic() < end:\n"
            "    t0 = time.monotonic()\n"
            "    time.sleep(0.001)\n"
            "    lat = (time.monotonic() - t0 - 0.001) * 1e6\n"
            "    if lat > 0:\n"
            "        print(f'{lat:.0f}', flush=True)\n"
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

    def collect(self) -> list[float]:
        if self.proc is None:
            return []
        out, _ = self.proc.communicate(timeout=self.duration + 10)
        return [float(x) for x in out.strip().splitlines() if x.strip()]


class _LongRunners:
    """Persistent CPU-bound processes that count work iterations."""

    def __init__(self, count):
        self.count = count
        self.procs = []

    def start(self, duration_secs):
        script = (
            f"import hashlib, time, sys\n"
            f"end = time.monotonic() + {duration_secs}\n"
            "iters = 0\n"
            "d = b'longrun' * 1000\n"
            "while time.monotonic() < end:\n"
            "    for _ in range(100):\n"
            "        d = hashlib.sha256(d).digest()\n"
            "    iters += 100\n"
            "print(iters, flush=True)\n"
        )
        for _ in range(self.count):
            p = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            self.procs.append(p)

    def collect(self, timeout=60) -> list[int]:
        results = []
        for p in self.procs:
            try:
                out, _ = p.communicate(timeout=timeout)
                results.append(int(out.strip()))
            except (subprocess.TimeoutExpired, ValueError):
                p.kill()
                results.append(0)
        self.procs.clear()
        return results


def _burst_processes(count):
    """Spawn count short-lived CPU-bound processes."""
    script = (
        "import hashlib\n"
        "d = b'burst' * 1000\n"
        "for _ in range(2000):\n"
        "    d = hashlib.sha256(d).digest()\n"
    )
    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    return procs




# SCHEDULER LIFECYCLE HELPERS (shared by prism-contention)

def _trace_start_scheduler(nr_cpus=None):
    """Start PANDEMONIUM for a trace through the ONE canonical activation path
    (start_and_wait): stdout/stderr to FILES, never a PIPE -- a PIPE deadlocks the
    --verbose scheduler the instant its output fills the 64KB buffer (the trace-path
    hang) -- plus the SchedulerProcess guard, stale-registration detection,
    wait_for_activation, and the _ACTIVE_GUARDS eject-on-interrupt. Returns the guard
    (a SchedulerProcess, .proc for the Popen) or None. No hand-rolled start."""
    cmd = [str(BINARY), "--verbose"]
    if nr_cpus is not None:
        cmd.extend(["--nr-cpus", str(nr_cpus)])
    return start_and_wait(cmd, "PANDEMONIUM", settle_secs=3.0)


# (trace stop folded into the canonical stop_and_wait)






# BENCH-CONTENTION PHASES

def _contention_phase_regime_sweep(nr_cpus, dmesg, sched_alive_fn, duration=30):
    """Force regime transitions under load: LIGHT -> HEAVY -> MIXED -> LIGHT, 3 cycles."""
    cycles = 3
    log_info(f"PHASE: regime-sweep ({cycles} cycles, {duration}s)")
    per_phase = duration // (cycles * 3)

    for cycle in range(1, cycles + 1):
        if not sched_alive_fn():
            return {"survived": False, "cycles": cycle - 1}

        # LIGHT: IDLE
        log_info(f"  cycle {cycle}/{cycles}: LIGHT ({per_phase}s idle)")
        time.sleep(per_phase)
        if dmesg.check():
            return {"survived": False, "cycles": cycle - 1}

        # HEAVY: SATURATE ALL CPUS
        log_info(f"  cycle {cycle}/{cycles}: HEAVY ({per_phase}s saturated)")
        stress = _StressWorkers(nr_cpus)
        stress.start()
        time.sleep(per_phase)
        if dmesg.check() or not sched_alive_fn():
            stress.stop()
            return {"survived": False, "cycles": cycle - 1}

        # MIXED: KILL HALF
        half = max(1, nr_cpus // 2)
        log_info(f"  cycle {cycle}/{cycles}: MIXED (kill {half}/{nr_cpus} stress)")
        for p in stress.procs[:half]:
            p.kill()
            p.wait()
        time.sleep(per_phase)
        if dmesg.check() or not sched_alive_fn():
            stress.stop()
            return {"survived": False, "cycles": cycle - 1}

        stress.stop()

    log_info(f"  regime-sweep: {cycles} cycles complete, scheduler alive")
    alive = sched_alive_fn()
    return {"survived": alive, "cycles": cycles}


def _contention_phase_deficit_storm(nr_cpus, dmesg, sched_alive_fn, duration=20):
    """Saturate deficit counter: ncpu interactive + ncpu*2 batch."""
    n_interactive = nr_cpus
    n_batch = nr_cpus * 2
    log_info(f"PHASE: deficit-storm ({n_interactive} interactive + {n_batch} batch, {duration}s)")

    # WARMUP: LET SCHEDULER CLASSIFY WORKLOADS BEFORE MAXIMUM STRESS
    time.sleep(2)

    # BATCH: TIGHT CPU SPIN
    batch_workers = _StressWorkers(n_batch)
    batch_workers.start()

    # INTERACTIVE: 1MS SLEEP CYCLES (HIGH WAKEUP RATE)
    interactive_probe = _LatencyProbe(duration)
    interactive_probe.start()

    # ALSO SPAWN EXTRA INTERACTIVE THREADS FOR WAKE PRESSURE
    extra_script = (
        f"import time\n"
        f"end = time.monotonic() + {duration}\n"
        "while time.monotonic() < end:\n"
        "    time.sleep(0.001)\n"
    )
    extras = []
    for _ in range(n_interactive - 1):
        p = subprocess.Popen(
            [sys.executable, "-c", extra_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        extras.append(p)

    samples = interactive_probe.collect()
    batch_workers.stop()
    for p in extras:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()

    if dmesg.check():
        return {"survived": False, "samples": 0}

    result = {"survived": sched_alive_fn(), "samples": len(samples)}
    if samples:
        p99 = percentile(samples, 99)
        med = percentile(samples, 50)
        result["median_us"] = med
        result["p99_us"] = p99
        log_info(f"  deficit-storm: {len(samples)} samples, median={med:.0f}us P99={p99:.0f}us")
    else:
        log_info("  deficit-storm: no latency samples collected")

    return result


def _contention_phase_sojourn_pressure(nr_cpus, dmesg, sched_alive_fn, duration=15):
    """Deep batch queuing to stress sojourn rescue."""
    n_batch = nr_cpus * 4
    log_info(f"PHASE: sojourn-pressure ({n_batch} batch, {duration}s)")

    # PHASE A: PURE BATCH FLOOD (10S)
    batch_duration = duration - 5
    runners = _LongRunners(n_batch)
    runners.start(batch_duration)
    time.sleep(batch_duration - 5)

    if dmesg.check() or not sched_alive_fn():
        runners.collect(timeout=5)
        return {"survived": False, "samples": 0}

    # PHASE B: ADD 4 INTERACTIVE PROBES INTO THE BATCH FLOOD (5S)
    log_info(f"  adding 4 interactive probes into batch flood")
    probe = _LatencyProbe(5)
    probe.start()
    samples = probe.collect()
    work = runners.collect(timeout=batch_duration + 10)

    if dmesg.check():
        return {"survived": False, "samples": 0}

    min_work = min(work) if work else 0
    max_work = max(work) if work else 0
    result = {
        "survived": sched_alive_fn(), "samples": len(samples),
        "work_min": min_work, "work_max": max_work,
    }
    if samples:
        p99 = percentile(samples, 99)
        result["p99_us"] = p99
        log_info(f"  sojourn-pressure: P99={p99:.0f}us, batch_work=[{min_work}..{max_work}]")
    else:
        log_info(f"  sojourn-pressure: no latency samples, batch_work=[{min_work}..{max_work}]")

    return result


def _contention_phase_longrun_interactive(nr_cpus, dmesg, sched_alive_fn, duration=20):
    """Sustained long-runners + interactive probe. Triggers longrun_mode."""
    n_runners = max(2, nr_cpus // 2)
    log_info(f"PHASE: longrun-interactive ({n_runners} runners + probe, {duration}s)")

    runners = _LongRunners(n_runners)
    runners.start(duration)

    # LET LONGRUN_MODE ACTIVATE (NEEDS >2S OF SUSTAINED BATCH)
    time.sleep(3)
    if dmesg.check() or not sched_alive_fn():
        runners.collect(timeout=5)
        return {"survived": False, "samples": 0}

    # INTERACTIVE PROBE DURING LONGRUN MODE
    probe_duration = duration - 5
    probe = _LatencyProbe(probe_duration)
    probe.start()
    samples = probe.collect()
    work = runners.collect(timeout=duration + 10)

    if dmesg.check():
        return {"survived": False, "samples": 0}

    min_work = min(work) if work else 0
    max_work = max(work) if work else 0
    fairness = min_work / max_work if max_work > 0 else 0

    result = {
        "survived": sched_alive_fn(), "samples": len(samples),
        "work_min": min_work, "work_max": max_work, "fairness": fairness,
    }
    if samples:
        p99 = percentile(samples, 99)
        med = percentile(samples, 50)
        result["median_us"] = med
        result["p99_us"] = p99
        log_info(f"  longrun-interactive: median={med:.0f}us P99={p99:.0f}us "
                 f"work=[{min_work}..{max_work}] fairness={fairness:.2f}")
    else:
        log_info(f"  longrun-interactive: no samples, work=[{min_work}..{max_work}]")

    return result


def _contention_phase_burst_recovery(nr_cpus, dmesg, sched_alive_fn):
    """Burst with explicit recovery verification."""
    burst_size = nr_cpus * 8
    log_info(f"PHASE: burst-recovery ({burst_size} burst processes)")

    # BASELINE (5S)
    baseline_probe = _LatencyProbe(5)
    baseline_probe.start()
    baseline_samples = baseline_probe.collect()
    if dmesg.check() or not sched_alive_fn():
        return {"survived": False}

    baseline_p99 = percentile(baseline_samples, 99) if baseline_samples else 0
    log_info(f"  baseline: P99={baseline_p99:.0f}us ({len(baseline_samples)} samples)")

    # FIRE BURST
    log_info(f"  firing {burst_size} burst processes")
    burst_procs = _burst_processes(burst_size)
    burst_probe = _LatencyProbe(10)
    burst_probe.start()
    burst_samples = burst_probe.collect()
    for p in burst_procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()

    if dmesg.check() or not sched_alive_fn():
        return {"survived": False}

    burst_p99 = percentile(burst_samples, 99) if burst_samples else 0
    log_info(f"  burst: P99={burst_p99:.0f}us ({len(burst_samples)} samples)")

    # RECOVERY (5S)
    recovery_probe = _LatencyProbe(5)
    recovery_probe.start()
    recovery_samples = recovery_probe.collect()

    if dmesg.check():
        return {"survived": False}

    recovery_p99 = percentile(recovery_samples, 99) if recovery_samples else 0
    within_2x = recovery_p99 <= max(baseline_p99 * 2, 500)
    log_info(f"  recovery: P99={recovery_p99:.0f}us "
             f"(baseline*2={baseline_p99*2:.0f}us) {'OK' if within_2x else 'ELEVATED'}")

    return {
        "survived": sched_alive_fn(),
        "baseline_p99_us": baseline_p99, "baseline_samples": len(baseline_samples),
        "burst_p99_us": burst_p99, "burst_samples": len(burst_samples),
        "recovery_p99_us": recovery_p99, "recovery_samples": len(recovery_samples),
        "recovery_within_2x": within_2x,
    }


def _contention_phase_mixed_storm(nr_cpus, dmesg, sched_alive_fn, duration=30):
    """Everything at once: long-runners + burst + interactive + deadline."""
    n_runners = max(2, nr_cpus // 2)
    burst_size = nr_cpus * 4
    n_interactive = nr_cpus
    log_info(f"PHASE: mixed-storm ({n_runners} longrun + {burst_size} burst + "
             f"{n_interactive} interactive + deadline, {duration}s)")

    # LONG-RUNNERS (FULL DURATION)
    runners = _LongRunners(n_runners)
    runners.start(duration)

    # INTERACTIVE PROBES
    probe = _LatencyProbe(duration)
    probe.start()

    # EXTRA INTERACTIVE THREADS
    extra_script = (
        f"import time\n"
        f"end = time.monotonic() + {duration}\n"
        "while time.monotonic() < end:\n"
        "    time.sleep(0.001)\n"
    )
    extras = []
    for _ in range(n_interactive - 1):
        p = subprocess.Popen(
            [sys.executable, "-c", extra_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        extras.append(p)

    # DEADLINE THREAD (16.6MS FRAME TARGET)
    deadline_script = (
        f"import time, sys\n"
        f"target_ns = 16_666_667\n"
        f"misses = 0\n"
        f"total = 0\n"
        f"end = time.monotonic() + {duration}\n"
        "while time.monotonic() < end:\n"
        "    t0 = time.monotonic()\n"
        "    time.sleep(target_ns / 1e9)\n"
        "    actual = (time.monotonic() - t0) * 1e9\n"
        "    jitter = actual - target_ns\n"
        "    total += 1\n"
        "    if jitter > 500_000:\n"
        "        misses += 1\n"
        "print(f'{misses}/{total}', flush=True)\n"
    )
    deadline_proc = subprocess.Popen(
        [sys.executable, "-c", deadline_script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )

    # WAIT 5S, THEN FIRE BURST INTO THE STORM
    time.sleep(5)
    if dmesg.check() or not sched_alive_fn():
        runners.collect(timeout=5)
        for p in extras:
            p.kill()
        deadline_proc.kill()
        return {"survived": False}

    log_info(f"  firing {burst_size} burst into storm")
    burst_procs = _burst_processes(burst_size)

    # WAIT FOR REMAINING DURATION
    time.sleep(max(1, duration - 10))

    # COLLECT EVERYTHING
    samples = probe.collect()
    work = runners.collect(timeout=duration + 15)
    for p in burst_procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
    for p in extras:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()

    deadline_out = ""
    try:
        deadline_out, _ = deadline_proc.communicate(timeout=duration + 10)
    except subprocess.TimeoutExpired:
        deadline_proc.kill()

    if dmesg.check():
        return {"survived": False}

    # REPORT
    min_work = min(work) if work else 0
    max_work = max(work) if work else 0
    result = {
        "survived": sched_alive_fn(), "samples": len(samples),
        "work_min": min_work, "work_max": max_work,
    }
    if samples:
        p99 = percentile(samples, 99)
        med = percentile(samples, 50)
        result["median_us"] = med
        result["p99_us"] = p99
        log_info(f"  mixed-storm: median={med:.0f}us P99={p99:.0f}us "
                 f"work=[{min_work}..{max_work}]")
    else:
        log_info(f"  mixed-storm: no samples, work=[{min_work}..{max_work}]")

    if deadline_out.strip():
        log_info(f"  deadline: {deadline_out.strip()}")
        try:
            parts = deadline_out.strip().split("/")
            result["deadline_misses"] = int(parts[0])
            result["deadline_total"] = int(parts[1])
            if int(parts[1]) > 0:
                result["deadline_miss_ratio"] = int(parts[0]) / int(parts[1])
        except (ValueError, IndexError):
            pass

    return result


# BENCH-CONTENTION ORCHESTRATOR

def _contention_run_iteration(iteration, total, nr_cpus):
    """Run one full contention iteration. Returns (survived: bool, phase_results: dict)."""
    phase_results = {}

    label = f"[{iteration}/{total}] " if total > 1 else ""
    log_info(f"{label}Starting scheduler")

    sched_proc = _trace_start_scheduler(nr_cpus=nr_cpus)
    if sched_proc is None:
        return False, phase_results

    dmesg = DmesgMonitor()

    def sched_alive():
        return (sched_proc is not None and sched_proc.proc.poll() is None
                and scheduler_active())

    log_info(f"{label}Starting contention sequence at {nr_cpus}C")

    phases = [
        ("regime-sweep",        lambda: _contention_phase_regime_sweep(nr_cpus, dmesg, sched_alive)),
        ("deficit-storm",       lambda: _contention_phase_deficit_storm(nr_cpus, dmesg, sched_alive)),
        ("sojourn-pressure",    lambda: _contention_phase_sojourn_pressure(nr_cpus, dmesg, sched_alive)),
        ("longrun-interactive", lambda: _contention_phase_longrun_interactive(nr_cpus, dmesg, sched_alive)),
        ("burst-recovery",      lambda: _contention_phase_burst_recovery(nr_cpus, dmesg, sched_alive)),
        ("mixed-storm",         lambda: _contention_phase_mixed_storm(nr_cpus, dmesg, sched_alive)),
    ]

    crashed = False
    for name, fn in phases:
        result = fn()
        phase_results[name] = result
        if not result.get("survived", False):
            crashed = True
            if dmesg.crashed:
                log_error(f"{label}CRASH DETECTED during '{name}': {dmesg.crash_msg}")
            else:
                log_error(f"{label}Scheduler died during '{name}' (no dmesg crash)")
            break
        log_info(f"  '{name}' passed, scheduler alive")
    else:
        log_info(f"{label}ALL PHASES COMPLETE -- scheduler survived")

    stop_and_wait(sched_proc)
    dmesg.save()

    return not crashed, phase_results


def _write_contention_prometheus(version, git, stamp, max_cpus, iterations,
                                  core_counts, results, all_phase_data) -> Path:
    """Write Prometheus exposition format (.prom) for prism-contention."""
    pb = PrometheusBuilder("contention")

    def gauge(name, help_text, value, labels=None):
        pb.gauge(name.replace("pandemonium_contention_", "", 1), value,
                 help=help_text, labels=labels)

    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    gauge("pandemonium_contention_iterations", "Iterations per core count", iterations)
    gauge("pandemonium_contention_max_cpus", "Maximum CPUs available", max_cpus)

    for nr_cpus in sorted(results.keys()):
        s, c = results[nr_cpus]
        cl = {"cores": str(nr_cpus)}
        gauge("pandemonium_contention_survived", "Iterations survived", s, cl)
        gauge("pandemonium_contention_crashed", "Iterations crashed", c, cl)

        phases = all_phase_data.get(nr_cpus, {})
        for phase_name, pd in phases.items():
            pl = {"cores": str(nr_cpus), "phase": phase_name}
            survived = 1 if pd.get("survived") else 0
            gauge("pandemonium_contention_phase_survived",
                  "Phase survived (1=OK, 0=CRASH)", survived, pl)

            if "samples" in pd:
                gauge("pandemonium_contention_phase_samples",
                      "Latency samples collected", pd["samples"], pl)
            if "p99_us" in pd:
                gauge("pandemonium_contention_phase_p99_us",
                      "P99 wakeup latency", pd["p99_us"], pl)
            if "median_us" in pd:
                gauge("pandemonium_contention_phase_median_us",
                      "Median wakeup latency", pd["median_us"], pl)
            if "work_min" in pd:
                gauge("pandemonium_contention_phase_work_min",
                      "Minimum work by any worker", pd["work_min"], pl)
            if "work_max" in pd:
                gauge("pandemonium_contention_phase_work_max",
                      "Maximum work by any worker", pd["work_max"], pl)
            if "fairness" in pd:
                gauge("pandemonium_contention_phase_fairness",
                      "Work fairness ratio (min/max)", f"{pd['fairness']:.4f}", pl)
            if "baseline_p99_us" in pd:
                gauge("pandemonium_contention_phase_baseline_p99_us",
                      "Baseline P99 before burst", pd["baseline_p99_us"], pl)
            if "burst_p99_us" in pd:
                gauge("pandemonium_contention_phase_burst_p99_us",
                      "P99 during burst", pd["burst_p99_us"], pl)
            if "recovery_p99_us" in pd:
                gauge("pandemonium_contention_phase_recovery_p99_us",
                      "P99 during post-burst recovery", pd["recovery_p99_us"], pl)
            if "recovery_within_2x" in pd:
                gauge("pandemonium_contention_phase_recovery_ok",
                      "Recovery within 2x baseline (1=OK, 0=ELEVATED)",
                      1 if pd["recovery_within_2x"] else 0, pl)
            if "deadline_misses" in pd:
                gauge("pandemonium_contention_phase_deadline_misses",
                      "Frame deadline misses", pd["deadline_misses"], pl)
            if "deadline_total" in pd:
                gauge("pandemonium_contention_phase_deadline_total",
                      "Total frame cycles", pd["deadline_total"], pl)
            if "deadline_miss_ratio" in pd:
                gauge("pandemonium_contention_phase_deadline_miss_ratio",
                      "Fraction of frames missed", f"{pd['deadline_miss_ratio']:.4f}", pl)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"contention-{version}-{stamp}.prom"
    path.write_text(pb.render())
    return path


def trace_contention_storm(stamp: str, n_cpus: int, duration: int,
                           schedulers: str = "", all_scx: bool = False,
                           pandemonium_only: bool = False,
                           phase: str = "deficit-storm") -> int:
    """One montauk-traced contention phase, hard-capped at `duration`s. Runs the
    requested phase -- deficit-storm (ncpu interactive + ncpu*2 batch) or
    sojourn-pressure (ncpu*4 deep batch flood + interactive probes) -- under each
    scheduler while montauk records the latency probe's wake-to-run (comm
    pand-cont): the batch workers create the runqueue pressure, the probe is the
    wakee we measure. montauk pins a drain core (cores are saturated)."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    phase_fns = {
        "deficit-storm": _contention_phase_deficit_storm,
        "sojourn-pressure": _contention_phase_sojourn_pressure,
    }
    phase_fn = phase_fns.get(phase, _contention_phase_deficit_storm)
    drain = max(0, n_cpus - 1)
    log_info(f"[contention/{phase}] tracing {n_cpus}C for {duration}s "
             f"(montauk on cpu{drain}, comm pand-cont)")

    def body(rec_dir):
        dmesg = DmesgMonitor()
        phase_fn(n_cpus, dmesg, lambda: True, duration=duration)
        return None

    # Field on the identical storm (default EEVDF+PANDEMONIUM; widened by
    # --schedulers/--all-scx). The probe renames its comm to pand-cont under
    # every scheduler, so montauk targets it for all arms. The recording label is
    # the phase, so per-phase per-width captures stay distinct in the digest.
    arms = field_arms(n_cpus, schedulers, all_scx, pandemonium_only)
    traced = 0
    for sched_name, activate_cmd in arms:
        rec_dir, _ = trace_workload(sched_name, activate_cmd,
                                    "pand-cont", f"{phase}-{n_cpus}c", stamp, body,
                                    events=True, pin_cpu=drain)
        if rec_dir is None:
            log_error(f"[contention/{phase}] {sched_name} failed to activate -- skipped")
            continue
        log_info(f"[contention/{phase}] {sched_name} montauk recording -> {rec_dir}")
        traced += 1
    return 0 if traced else 1


def cmd_bench_contention(args) -> int:
    """Contention stress test targeting v5.4.x adaptive features.

    6 phases per core count: regime-sweep, deficit-storm, sojourn-pressure,
    longrun-interactive, burst-recovery, mixed-storm. Each phase targets
    a specific adaptive mechanism.
    """

    warm_sudo()

    nuke_stale_build()

    if not build():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.core_counts:
        core_counts = [int(c.strip()) for c in args.core_counts.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = compute_core_counts(max_cpus)

    if not core_counts:
        log_error(f"no valid core counts (host has {max_cpus} CPUs, minimum is 2)")
        return 1

    # FILTER PHASES IF --phase SPECIFIED
    phase_filter = getattr(args, "phase", None)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # --trace: capped montauk recordings of the contention phase at EVERY core
    # width (the suite's scaling), each its own capture, so the width-specific
    # fault is visible -- sojourn-pressure blows up at 8C, deficit-storm floors
    # at 2C. --phase picks which (default deficit-storm). restrict the online
    # CPUs per width like the matrix path, restore after.
    if getattr(args, "trace", False):
        trace_phase = phase_filter or "deficit-storm"
        if not log.child:
            log_info(f"prism-contention v{get_version()} --trace {trace_phase}  "
                     f"core counts: {core_counts}")
        rc = 0
        try:
            for nr in core_counts:
                # restrict_cpus only offlines -- restore first so an ascending
                # sweep actually widens instead of staying at the narrowest width.
                restore_all_cpus(max_cpus)
                if not restrict_cpus(nr, max_cpus):
                    log_warn(f"[contention/{trace_phase}] could not restrict to "
                             f"{nr}C -- skipped")
                    continue
                rc |= trace_contention_storm(stamp, nr, args.duration,
                                             getattr(args, "schedulers", "") or "",
                                             getattr(args, "all_scx", False),
                                             getattr(args, "pandemonium_only", False),
                                             phase=trace_phase)
        finally:
            restore_all_cpus(max_cpus)
        return rc

    ver = get_version()
    git = get_git_info()
    dirty = " (dirty)" if git["dirty"] else ""
    log_info(f"prism-contention v{ver} [{git['commit']}{dirty}], "
             f"core_counts={core_counts}, iterations={args.iterations}, "
             f"host_cpus={max_cpus}")
    if phase_filter:
        log_info(f"Phase filter: {phase_filter}")
    print()

    results = {}
    all_phase_data = {}
    total_survived = 0
    total_crashed = 0

    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] Restricting to {nr_cpus} cores")
            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] failed to offline CPUs, skipping")
                    results[nr_cpus] = (0, args.iterations)
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(2)

            survived = 0
            crashed = 0
            core_phases = {}

            for i in range(1, args.iterations + 1):
                if args.iterations > 1:
                    log_info(f"[{nr_cpus}C] ITERATION {i}/{args.iterations}")
                ok, phase_results = _contention_run_iteration(i, args.iterations, nr_cpus)
                if ok:
                    survived += 1
                else:
                    crashed += 1
                # KEEP LAST ITERATION'S PHASE DATA FOR THIS CORE COUNT
                core_phases = phase_results
                if i < args.iterations:
                    log_info("Settling 3s before next iteration...")
                    time.sleep(3)

            results[nr_cpus] = (survived, crashed)
            all_phase_data[nr_cpus] = core_phases
            log_info(f"[{nr_cpus}C] RESULTS: {survived}/{args.iterations} survived")

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
    finally:
        restore_all_cpus(max_cpus)

        if results:
            print()
            log_info("SUMMARY")
            for nr_cpus in sorted(results.keys()):
                s, c = results[nr_cpus]
                total_survived += s
                total_crashed += c
                status = "PASS" if c == 0 else "FAIL"
                log_info(f"  {nr_cpus:>3}C: {s}/{s+c} survived  {status}")
            log_info(f"  TOTAL: {total_survived}/{total_survived+total_crashed}")

        # WRITE PROMETHEUS .prom
        prom_path = _write_contention_prometheus(
            ver, git, stamp, max_cpus, args.iterations,
            core_counts, results, all_phase_data,
        )
        log_info(f"METRICS: {prom_path}")

        # WRITE HUMAN-READABLE .log
        report_path = LOG_DIR / f"prism-contention-{stamp}.log"
        report_lines = [f"prism-contention v{ver} [{git['commit']}]",
                        f"cores: {core_counts}  iterations: {args.iterations}  host: {max_cpus}C",
                        ""]
        for nr_cpus in sorted(results.keys()):
            s, c = results[nr_cpus]
            status = "PASS" if c == 0 else "FAIL"
            report_lines.append(f"{nr_cpus:>3}C: {s}/{s+c} survived  {status}")
            phases = all_phase_data.get(nr_cpus, {})
            for phase_name, pd in phases.items():
                surv = "OK" if pd.get("survived") else "CRASH"
                extras = []
                if "p99_us" in pd:
                    extras.append(f"P99={pd['p99_us']:.0f}us")
                if "median_us" in pd:
                    extras.append(f"med={pd['median_us']:.0f}us")
                if "samples" in pd:
                    extras.append(f"n={pd['samples']}")
                if "work_min" in pd:
                    extras.append(f"work=[{pd['work_min']}..{pd.get('work_max', 0)}]")
                if "fairness" in pd:
                    extras.append(f"fair={pd['fairness']:.2f}")
                if "baseline_p99_us" in pd:
                    extras.append(f"base={pd['baseline_p99_us']:.0f}us")
                    extras.append(f"burst={pd.get('burst_p99_us', 0):.0f}us")
                    extras.append(f"recov={pd.get('recovery_p99_us', 0):.0f}us")
                if "deadline_misses" in pd:
                    extras.append(f"dl={pd['deadline_misses']}/{pd['deadline_total']}")
                detail = "  ".join(extras)
                report_lines.append(f"    {phase_name}: {surv}  {detail}")
        report_lines.append("")
        report_lines.append(f"TOTAL: {total_survived}/{total_survived+total_crashed}")
        report = "\n".join(report_lines) + "\n"
        report_path.write_text(report)
        log_info(f"REPORT: {report_path}")

    return 0 if total_crashed == 0 else 1


# BENCH-TRACE: BPF TRACE CAPTURE FOR EXTERNAL WORKLOADS

TRACE_CAPTURE_S = 120     # DEFAULT CAPTURE DURATION
TRACE_LAUNCH_TIMEOUT = 120  # MAX WAIT FOR --target PROCESS TO APPEAR
GAP_THRESH_MS = 50      # SCHEDULING GAPS ABOVE THIS ARE FLAGGED (1 FRAME @ 20FPS)


def _find_process(name: str) -> int | None:
    """Find PID of a running process by name. Returns None if not found."""
    try:
        r = subprocess.run(["pgrep", "-x", name],
                           capture_output=True, text=True)
        if r.returncode == 0:
            pids = r.stdout.strip().splitlines()
            return int(pids[0]) if pids else None
    except (ValueError, FileNotFoundError):
        pass
    return None


def _wait_for_process(name: str, timeout: float) -> int | None:
    """Poll until process appears. Returns PID or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _find_process(name)
        if pid is not None:
            return pid
        time.sleep(0.5)
    return None


def _kill_process(name: str):
    """Gracefully kill a process by name (SIGTERM, then SIGKILL)."""
    pid = _find_process(name)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _find_process(name) is None:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _extract_panic_context(stderr_text: str, stdout_text: str) -> list[str]:
    """Pull Rust panic / BPF exit / abort signatures out of the scheduler's
    captured stderr+stdout. Returns the matching lines plus a few lines of
    surrounding context. Empty list when nothing panic-shaped is found.

    Patterns recognized:
      thread '...' panicked at ...
      note: run with `RUST_BACKTRACE=...
      BPF exit: kind=... / BPF exit reason: ... / BPF exit msg: ...
      assertion failed: ... / assertion `...` failed
      fatal runtime error
    """
    if not stderr_text and not stdout_text:
        return []
    combined = (stderr_text or "") + "\n" + (stdout_text or "")
    lines = combined.splitlines()
    markers = (
        "thread '", "panicked at", "RUST_BACKTRACE",
        "BPF exit:", "BPF exit reason:", "BPF exit msg:",
        "assertion failed", "assertion `", "fatal runtime error",
        "stack backtrace:",
    )
    hits: list[int] = []
    for i, line in enumerate(lines):
        if any(m in line for m in markers):
            hits.append(i)
    if not hits:
        return []
    # Coalesce hits + 2 lines of surrounding context, dedupe.
    keep: set[int] = set()
    for h in hits:
        for j in range(max(0, h - 1), min(len(lines), h + 3)):
            keep.add(j)
    out = [lines[i] for i in sorted(keep) if lines[i].strip()]
    return out[:40]


SCX_CI_FAIL_PATTERNS = ["BUG:", "WARNING:"]
SCX_CI_FAIL_ICASE = ["error", "stall", "timeout"]
SCX_CI_FALSE_POSITIVES = [
    "Speculative Return Stack Overflow",
    "RETBleed",
    "spectre",
    "retbleed",
    "mitigation",
]

def _scx_ci_check_output(text: str) -> list[str]:
    """Scan scheduler output for scx CI failure patterns.
    Returns list of matching failure lines (empty = pass)."""
    failures = []
    for line in text.splitlines():
        # SKIP KNOWN FALSE POSITIVES
        if any(fp.lower() in line.lower() for fp in SCX_CI_FALSE_POSITIVES):
            continue
        # EXACT CASE PATTERNS
        for pat in SCX_CI_FAIL_PATTERNS:
            if pat in line:
                failures.append(line.strip())
                break
        else:
            # CASE-INSENSITIVE PATTERNS
            lower = line.lower()
            for pat in SCX_CI_FAIL_ICASE:
                if pat in lower:
                    failures.append(line.strip())
                    break
    return failures


def cmd_bench_scx(args) -> int:
    """scx CI compatibility test.

    Mimics the upstream sched-ext/scx GitHub Actions CI:
      functional: Run scheduler for 30s, scan output for failure patterns
      stress:     Run stress-ng with affinity pinning for 31s under scheduler

    Pass criteria match .github/include/scripts/test_sched and
    .github/include/scripts/run_stress_tests from sched-ext/scx.
    """
    warm_sudo()
    nuke_stale_build()
    if not build():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.core_counts:
        core_counts = [int(c.strip()) for c in args.core_counts.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = compute_core_counts(max_cpus)

    version = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # FUNCTIONAL TEST DURATION (scx CI uses 30s)
    func_duration = args.duration
    # STRESS TEST DURATION (scx CI uses 31s stress-ng + 45s timeout)
    stress_duration = args.stress_duration

    log_info(f"PANDEMONIUMv{version} BENCH-SCX (CI COMPAT)")
    log_info(f"Core counts: {core_counts}")
    log_info(f"Functional: {func_duration}s, Stress: {stress_duration}s")

    all_results = {}
    overall_pass = True

    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] scx CI compatibility tests")

            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] Failed to restrict CPUs")
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(1)

            core_results = {"functional": {}, "stress": {}}

            # TEST 1: FUNCTIONAL (test_sched equivalent)
            # Run scheduler for func_duration seconds. Scan combined
            # stdout+stderr for BUG:/WARNING:/error/stall/timeout.
            log_info(f"[{nr_cpus}C] Functional: running {func_duration}s")
            dmesg = DmesgMonitor()

            guard = start_and_wait(
                [str(BINARY), "--nr-cpus", str(nr_cpus)],
                "PANDEMONIUM", settle_secs=2)
            if guard is None:
                log_error(f"[{nr_cpus}C] Functional: scheduler failed to start")
                core_results["functional"] = {
                    "pass": False, "survived": False, "failures": ["STARTUP FAILED"],
                }
                overall_pass = False
            else:
                time.sleep(func_duration)

                crashed = dmesg.check()
                alive = guard.proc.poll() is None and scheduler_active()

                stdout = guard.drain_stdout()
                stderr = guard.read_stderr(limit=64000)
                combined = stdout + "\n" + stderr

                failures = _scx_ci_check_output(combined)
                if crashed:
                    failures.append(f"DMESG CRASH: {dmesg.crash_msg}")
                if not alive:
                    failures.append(
                        f"SCHEDULER EXITED (code {guard.proc.returncode})")

                func_pass = len(failures) == 0 and alive
                core_results["functional"] = {
                    "pass": func_pass,
                    "survived": alive,
                    "failures": failures,
                    "duration_s": func_duration,
                }
                if not func_pass:
                    overall_pass = False
                    for f in failures[:10]:
                        log_error(f"[{nr_cpus}C] Functional FAIL: {f}")

                dmesg.save()
                stop_and_wait(guard)
                time.sleep(2)

            # TEST 2: STRESS (run_stress_tests equivalent)
            # Start scheduler, then run stress-ng with affinity pinning.
            # Pass = scheduler survives (no crash, no non-zero exit).
            log_info(f"[{nr_cpus}C] Stress: stress-ng {stress_duration}s "
                     f"w/ affinity pinning")
            dmesg2 = DmesgMonitor()

            guard = start_and_wait(
                [str(BINARY), "--nr-cpus", str(nr_cpus)],
                "PANDEMONIUM", settle_secs=2)
            if guard is None:
                log_error(f"[{nr_cpus}C] Stress: scheduler failed to start")
                core_results["stress"] = {
                    "pass": False, "survived": False,
                    "failures": ["STARTUP FAILED"],
                }
                overall_pass = False
            else:
                # stress-ng command from scx stress_tests.ini
                stress_cmd = [
                    "stress-ng",
                    "-t", str(stress_duration),
                    "--aggressive",
                    "-M",
                    "-c", str(nr_cpus),
                    "-f", str(nr_cpus),
                    "--affinity", "1",
                    "--affinity-delay", "1",
                    "--affinity-pin",
                ]
                log_info(f"  {' '.join(stress_cmd)}")
                stress_timeout = stress_duration + 15

                try:
                    stress_ret = subprocess.run(
                        stress_cmd,
                        timeout=stress_timeout,
                        capture_output=True,
                        text=True,
                    )
                    stress_exit = stress_ret.returncode
                except subprocess.TimeoutExpired:
                    log_warn(f"[{nr_cpus}C] stress-ng timed out at "
                             f"{stress_timeout}s")
                    stress_exit = 143  # SIGTERM equivalent

                crashed = dmesg2.check()
                alive = guard.proc.poll() is None and scheduler_active()

                # scx CI: exit 0 or 143 (SIGTERM from timeout) = pass
                stress_ok = stress_exit in (0, 143)
                stress_failures = []
                if not stress_ok:
                    stress_failures.append(
                        f"stress-ng exit code {stress_exit}")
                if crashed:
                    stress_failures.append(
                        f"DMESG CRASH: {dmesg2.crash_msg}")
                if not alive:
                    stress_failures.append(
                        f"SCHEDULER DIED (code {guard.proc.returncode})")

                stress_pass = len(stress_failures) == 0 and alive
                core_results["stress"] = {
                    "pass": stress_pass,
                    "survived": alive,
                    "stress_exit": stress_exit,
                    "failures": stress_failures,
                    "duration_s": stress_duration,
                }
                if not stress_pass:
                    overall_pass = False
                    for f in stress_failures[:10]:
                        log_error(f"[{nr_cpus}C] Stress FAIL: {f}")

                dmesg2.save()
                stop_and_wait(guard)
                time.sleep(2)

            all_results[nr_cpus] = core_results

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
        overall_pass = False
    finally:
        restore_all_cpus(max_cpus)

    # REPORT
    log_info("")
    log_info(f"PANDEMONIUMv{version} BENCH-SCX RESULTS")
    log_info("")

    for nr_cpus in sorted(all_results.keys()):
        cr = all_results[nr_cpus]
        log_info(f"  {nr_cpus}C:")
        for test_name in ["functional", "stress"]:
            r = cr.get(test_name, {})
            label = "PASS" if r.get("pass", False) else "FAIL"
            detail = ""
            if test_name == "functional":
                detail = f"duration={r.get('duration_s', '?')}s"
                n_fail = len(r.get("failures", []))
                if n_fail:
                    detail += f" violations={n_fail}"
            elif test_name == "stress":
                detail = (f"duration={r.get('duration_s', '?')}s "
                          f"exit={r.get('stress_exit', '?')}")
            log_info(f"    {test_name:20s} {label}  {detail}")

    log_info("")
    if overall_pass:
        log_info("OVERALL: PASS (scx CI compatible)")
    else:
        log_info("OVERALL: FAIL")

    # PROMETHEUS OUTPUT (unified schema; metadata as real gauges, not comments)
    prom_path = LOG_DIR / f"prism-scx-{stamp}.prom"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pb = PrometheusBuilder("scx")
    git = get_git_info()
    pb.info(version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    for nr_cpus in sorted(all_results.keys()):
        cr = all_results[nr_cpus]
        for test_name in ["functional", "stress"]:
            r = cr.get(test_name, {})
            lbl = {"test": test_name, "cpus": str(nr_cpus)}
            pb.gauge("pass", 1 if r.get("pass", False) else 0,
                     help="per-test pass (1) / fail (0)", labels=lbl)
            pb.gauge("survived", 1 if r.get("survived", False) else 0,
                     help="scheduler survived the test (1) / crashed (0)", labels=lbl)
            if test_name == "functional":
                pb.gauge("violations", len(r.get("failures", [])),
                         help="functional violations", labels=lbl)
            elif test_name == "stress":
                pb.gauge("stress_exit", r.get("stress_exit", -1),
                         help="stress-ng exit code", labels=lbl)
    prom_path.write_text(pb.render())
    log_info(f"METRICS: {prom_path}")

    return 0 if overall_pass else 1


# LOW-CPU DEADLINE REGRESSION TEST
#
# REPRODUCES THE 2026-04-20 BENCH-SCALE FINDING: ADAPTIVE MODE MISSED
# 87.1% OF DEADLINES AT 4C (318MS JITTER P99) DUE TO BURST DETECTORS
# (BPF wake_burst, MWU fork_storm) LATCHING ON UNDER NORMAL LOAD AT LOW
# CPU COUNTS. v5.7.0 GATED THEM BEHIND THE OSCILLATOR'S RESCUE-DELTA
# SIGNAL. THIS TEST GUARDS AGAINST REINTRODUCTION OF THE BUG.

def cmd_low_cpu_deadline(args) -> int:
    """Low-CPU ADAPTIVE deadline regression guard (v5.7.0).

    Restricts to 4 (and optionally 2) CPUs, runs ADAPTIVE mode, and
    asserts periodic deadline miss ratio < threshold. Before the v5.7.0
    fix: 87% misses at 4C, 39% at 2C. After: single digits expected.
    """
    warm_sudo()
    nuke_stale_build()
    if not build():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.core_counts:
        core_counts = [int(c.strip()) for c in args.core_counts.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = [c for c in (2, 4) if c <= max_cpus]

    if not core_counts:
        log_error("No valid core counts (need max_cpus >= 2)")
        return 1

    threshold = args.miss_threshold
    version = get_version()
    log_info(f"PANDEMONIUMv{version} LOW-CPU-DEADLINE REGRESSION")
    log_info(f"Core counts: {core_counts}, miss threshold: {threshold:.1%}")

    overall_pass = True
    results: dict = {}

    with CpuGuard(max_cpus):
        restore_all_cpus(max_cpus)
        time.sleep(0.5)

        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] Starting ADAPTIVE deadline test")

            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] CPU hotplug failed -- skipping")
                    restore_all_cpus(max_cpus)
                    time.sleep(0.5)
                    overall_pass = False
                    continue
            time.sleep(2)

            guard = start_and_wait(
                [str(BINARY), "--verbose"], "PANDEMONIUM (ADAPTIVE)",
                settle_secs=3)
            if guard is None:
                log_error(f"[{nr_cpus}C] Scheduler failed to start")
                overall_pass = False
                continue

            try:
                r = measure_deadline(BINARY, nr_cpus,
                                     target_fps=60,
                                     duration_secs=args.duration,
                                     threshold_us=500)
            finally:
                guard.stop()
                wait_for_deactivation(5.0)

            miss_ratio = r.get("miss_ratio", 1.0)
            jitter_p99 = r.get("jitter_p99_us", 0)
            jitter_worst = r.get("jitter_worst_us", 0)
            passed = miss_ratio < threshold
            verdict = "PASS" if passed else "FAIL"

            log_info(f"[{nr_cpus}C] miss_ratio={miss_ratio:.1%} "
                     f"(threshold={threshold:.1%}) "
                     f"jitter_p99={jitter_p99}us worst={jitter_worst}us "
                     f"{verdict}")

            results[nr_cpus] = {
                "miss_ratio": miss_ratio,
                "jitter_p99_us": jitter_p99,
                "jitter_worst_us": jitter_worst,
                "pass": passed,
            }
            if not passed:
                overall_pass = False

    log_info("SUMMARY")
    for nr_cpus, r in results.items():
        status = "PASS" if r["pass"] else "FAIL"
        log_info(f"  {nr_cpus}C: miss={r['miss_ratio']:.1%} "
                 f"p99={r['jitter_p99_us']}us  {status}")

    return 0 if overall_pass else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PANDEMONIUM test orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    bench = sub.add_parser("prism-scale",
                           help="Unified throughput + latency benchmark")
    bench.add_argument("--cmd", type=str, default=None,
                       help="Custom workload command (default: self-build)")
    bench.add_argument("--clean-cmd", type=str, default=None,
                       help="Clean command between iterations")
    bench.add_argument("--iterations", type=int, default=1,
                       help="Iterations per scheduler (default: 1)")
    bench.add_argument("--schedulers", type=str,
                       default=",".join(DEFAULT_EXTERNALS),
                       help=f"Comma-separated external schedulers "
                            f"(default: {','.join(DEFAULT_EXTERNALS)})")
    bench.add_argument("--core-counts", type=str, default=None,
                       help="Comma-separated core counts "
                            "(default: auto 2,4,8,...,max)")
    bench.add_argument("--burst", action="store_true",
                       help="Burst-only mode: skip latency and throughput, "
                            "run only burst measurement")
    bench.add_argument("--longrun", action="store_true",
                       help="Long-run only mode: skip latency, throughput, "
                            "and burst; run only long-running process test")
    bench.add_argument("--mixed", action="store_true",
                       help="Mixed-only mode: skip latency, throughput; "
                            "run only burst+longrun combined test")
    bench.add_argument("--deadline", action="store_true",
                       help="Deadline-only mode: run only periodic frame "
                            "scheduling jitter test")
    bench.add_argument("--ipc", action="store_true",
                       help="IPC-only mode: run only pipe round-trip "
                            "latency test")
    bench.add_argument("--launch", action="store_true",
                       help="Launch-only mode: run only fork+exec latency "
                            "test under load")
    bench.add_argument("--pandemonium-only", action="store_true",
                       help="Skip EEVDF and external schedulers, run only "
                            "PANDEMONIUM (BPF) and PANDEMONIUM (ADAPTIVE)")
    bench.add_argument("--trace", action="store_true",
                       help="Wrap each IPC measurement in a montauk --trace "
                            "pand-ipc recording (raw per-event log) to "
                            "/tmp/pandemonium -- resolves individual RTTs at the "
                            "tick floor. Diagnostic; latency numbers are "
                            "contaminated. Pairs with --ipc (also via prism-ipc).")


    contention_bench = sub.add_parser("prism-contention",
                                      help="Contention stress test for v5.4.x adaptive features")
    contention_bench.add_argument("--iterations", type=int, default=1,
                                  help="Full workload iterations per core count (default: 1)")
    contention_bench.add_argument("--core-counts", type=str, default=None,
                                  help="Comma-separated core counts "
                                       "(default: auto 2,4,8,...,max)")
    contention_bench.add_argument("--phase", type=str, default=None,
                                  help="Run single phase: regime-sweep, deficit-storm, "
                                       "sojourn-pressure, longrun-interactive, "
                                       "burst-recovery, mixed-storm")
    contention_bench.add_argument("--trace", action="store_true",
                                  help="Capture one montauk eBPF recording of the "
                                       "contention storm (PANDEMONIUM), capped at "
                                       "--duration, instead of the full matrix")
    contention_bench.add_argument("--duration", type=int, default=20,
                                  help="With --trace: capture window seconds "
                                       "(default: 20)")
    contention_bench.add_argument("--schedulers", type=str, default="",
                                  help="With --trace: comma-separated scheduler "
                                       "field (EEVDF baseline always; PANDEMONIUM "
                                       "only if named)")
    contention_bench.add_argument("--all-scx", action="store_true",
                                  help="With --trace: loop the full installed scx "
                                       "field (EEVDF + PANDEMONIUM + every external)")
    contention_bench.add_argument("--pandemonium-only", action="store_true",
        help="With --trace: PANDEMONIUM arm only (skip the EEVDF baseline and externals)")

    sys_bench = sub.add_parser("prism-sys",
                               help="Live system telemetry capture")
    sys_bench.add_argument("--scheduler", type=str, default="adaptive",
                           help="Scheduler to run: adaptive (default), "
                                "no-adaptive, eevdf, or external name "
                                "(e.g. scx_bpfland)")
    sys_bench.add_argument("--with-probe", action="store_true",
                           help="Run latency probe during session")
    sys_bench.add_argument("--compositor", action="append",
                           help="Additional compositor process names "
                                "(PANDEMONIUM modes only)")

    pcpu_bench = sub.add_parser("prism-pcpu",
                                help="Per-CPU DSQ visibility stress test (v5.4.8)")
    pcpu_bench.add_argument("--iterations", type=int, default=1,
                            help="Iterations per core count (default: 1)")
    pcpu_bench.add_argument("--core-counts", type=str, default=None,
                            help="Comma-separated core counts "
                                 "(default: auto 2,4,8,...,max)")
    pcpu_bench.add_argument("--trace", action="store_true",
                            help="Capture one montauk eBPF recording of the "
                                 "burst-starvation load (PANDEMONIUM), capped at "
                                 "--duration, instead of the full matrix")
    pcpu_bench.add_argument("--duration", type=int, default=20,
                            help="With --trace: capture window seconds (default: 20)")
    pcpu_bench.add_argument("--schedulers", type=str, default="",
                            help="With --trace: comma-separated scheduler field "
                                 "(EEVDF baseline always; PANDEMONIUM only if named)")
    pcpu_bench.add_argument("--all-scx", action="store_true",
                            help="With --trace: loop the full installed scx field "
                                 "(EEVDF + PANDEMONIUM + every external)")
    pcpu_bench.add_argument("--pandemonium-only", action="store_true",
                            help="With --trace: PANDEMONIUM arm only (skip the "
                                 "EEVDF baseline and externals)")

    coldwake_bench = sub.add_parser("prism-coldwake",
        help="Cold-wake latency vs frequency-at-wake (idle->bare-wake, powersave)")
    coldwake_bench.add_argument("--trace", action="store_true",
        help="Capture montauk recordings of the cold-wake cycle (the only mode)")
    coldwake_bench.add_argument("--duration", type=int, default=60,
        help="Cold-wake cycle window seconds per scheduler arm (default: 60)")
    coldwake_bench.add_argument("--dwell", type=int, default=2,
        help="Idle seconds per cycle before the core goes cold (default: 2)")
    coldwake_bench.add_argument("--sizes", type=str,
        default="100000,500000,1000000,4000000,16000000,50000000",
        help="CPU burst sizes (iteration counts) swept cold->warm; small ~sub-ms "
             "(cursor/keypress) to large ~50ms (default spans both)")
    coldwake_bench.add_argument("--mem-sizes", dest="mem_sizes", type=str,
        default="32768,262144,2097152,8388608,33554432,134217728",
        help="Memory working sets (bytes) for the cold-cache pointer-chase, L1 to "
             "DRAM (default 32KB,256KB,2MB,8MB,32MB,128MB)")
    coldwake_bench.add_argument("--core-counts", type=str, default=None,
        help="Accepted for suite uniformity; cold-wake pins one core regardless")
    coldwake_bench.add_argument("--schedulers", type=str, default="",
        help="Comma-separated scheduler field (EEVDF baseline always; "
             "PANDEMONIUM only if named)")
    coldwake_bench.add_argument("--all-scx", action="store_true",
        help="Loop the full installed scx field")
    coldwake_bench.add_argument("--storm", action="store_true",
        help="cpu_release kick-storm reproducer (powersave) instead of the "
             "cold-cache sweep -- recreates the reboot live-lock and scores storm "
             "fraction + real-IPI-vs-IDLE-churn from the scheduler tick log")
    coldwake_bench.add_argument("--busy-per-cpu", dest="busy_per_cpu", type=int,
        default=4, help="storm mode: busy sched_ext workers per CPU (queue depth)")
    coldwake_bench.add_argument("--rt-sleep-us", dest="rt_sleep_us", type=int,
        default=100, help="storm mode: SCHED_FIFO sleep period us (release rate)")
    coldwake_bench.add_argument("--rt-spin-us", dest="rt_spin_us", type=int,
        default=10, help="storm mode: SCHED_FIFO spin us per wake (RT duty)")
    coldwake_bench.add_argument("--pandemonium-only", action="store_true",
        help="PANDEMONIUM arm only (skip the EEVDF baseline and externals)")
    coldwake_bench.add_argument("--iterations", type=int, default=1,
        help="Accepted for suite uniformity; cold-wake samples over time, not trials")

    scx_bench = sub.add_parser("prism-scx",
                                help="scx CI compatibility test")
    scx_bench.add_argument("--duration", type=int, default=30,
                           help="Functional test duration in seconds (default: 30)")
    scx_bench.add_argument("--stress-duration", type=int, default=31,
                           help="Stress test duration in seconds (default: 31)")
    scx_bench.add_argument("--core-counts", type=str, default=None,
                           help="Comma-separated core counts "
                                "(default: auto 2,4,8,...,max)")
    scx_bench.add_argument("--pandemonium-only", action="store_true",
        help="Accepted for suite uniformity; prism-scx exercises PANDEMONIUM only already")
    scx_bench.add_argument("--trace", action="store_true",
        help="Accepted for suite uniformity; prism-scx has no capture mode")
    scx_bench.add_argument("--iterations", type=int, default=1,
        help="Accepted for suite uniformity; the scx CI matrix is single-pass")

    lcd = sub.add_parser("low-cpu-deadline",
                         help="v5.7.0 regression guard: ADAPTIVE deadline "
                              "misses at low core counts (2-4C)")
    lcd.add_argument("--core-counts", type=str, default=None,
                     help="Comma-separated core counts (default: 2,4)")
    lcd.add_argument("--duration", type=int, default=15,
                     help="Measurement duration per core count (default: 15s)")
    lcd.add_argument("--miss-threshold", type=float, default=0.10,
                     help="Max allowed miss ratio (default: 0.10 = 10%%)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if hasattr(args, "schedulers") and isinstance(args.schedulers, str):
        args.schedulers = [s.strip() for s in args.schedulers.split(",")
                           if s.strip()]

    if args.command == "prism-scale":
        return cmd_bench_scale(args)
    if args.command == "prism-contention":
        return cmd_bench_contention(args)
    if args.command == "prism-sys":
        return cmd_bench_sys(args)
    if args.command == "prism-pcpu":
        return cmd_bench_pcpu(args)
    if args.command == "prism-coldwake":
        return cmd_bench_coldwake(args)
    if args.command == "prism-scx":
        return cmd_bench_scx(args)
    if args.command == "low-cpu-deadline":
        return cmd_low_cpu_deadline(args)

    log_error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
