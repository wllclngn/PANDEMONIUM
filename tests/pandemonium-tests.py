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
import os
import traceback
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
import pandemonium_common
from pandemonium_common import (
    SCRIPT_DIR, TARGET_DIR, LOG_DIR, ARCHIVE_DIR, BINARY, SOURCE_PATTERNS,
    get_version, get_git_info, DmesgMonitor, add_field_flags, field_of, pandemonium_arms,
    scheduler_field, entries_for_cores, fields_pandemonium, clear_loaded_scheduler,
    log, log_info, log_warn, log_error, 
    build,
    is_scx_active, scx_scheduler_name,
    wait_for_activation, wait_for_deactivation, 
    restrict_cpus, restore_all_cpus, CpuGuard,
    get_possible_cpus, get_online_cpus, compute_core_counts,
    mean_stdev, percentile, median, mean as sub_mean, variance as sub_variance,
    montauk_trace, montauk_available, 
    PrometheusBuilder, table_header, table_row,
    stall_susceptibility,
    install_exit_guard, note_scheduler_activated, register_exit_cleanup,
    montauk_envelope, envelope_report, envelope_capture, envelope_verdict,
    envelope_offenders, envelope_gauges, envelope_gauge_by_label, montauk_report,
    cpu_max_c, box_outsiders, probe_tool,
    warm_sudo,
    find_scheduler, stop_systemd_scheduler,
    start_and_wait, sched_ejected, stop_and_wait,
    trace_workload,
    parse_knobs_line,
    _write_cross_domain_marker_text, _write_chaos_markers_text,
    _ACTIVE_GUARDS, HIST_BUCKETS_US,
)
from ipc_workload import (
    measure_ipc_cell, IPC_DEFAULT_ROUNDS, IPC_RTT_PRIMS, IPC_COMM,
)


# CONFIGURATION


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


def parse_probe_output(stdout_text: str, keep_values: bool = False) -> dict:
    """Parse probe stdout (one overshoot_us per line) into latency stats.
    keep_values leaves the raw samples under `values` for a caller that pools
    iterations (scale's phases); every other caller stores the stats alone."""
    values = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if line and line.lstrip("-").isdigit():
            values.append(float(line))
    stats = probe_stats(values)
    if not keep_values:
        stats.pop("values", None)
    return stats


def probe_stats(values: list[float]) -> dict:
    """Latency stats from raw probe samples. The samples ride along under
    `values` so iterations of one phase can be pooled (pool_phase_runs) and
    are dropped from the stored result there."""
    if not values:
        return {"samples": 0, "median_us": 0, "p99_us": 0, "worst_us": 0,
                "values": []}
    return {
        "samples": len(values),
        "median_us": int(percentile(values, 50)),
        "p99_us": int(percentile(values, 99)),
        "worst_us": int(max(values)),
        "hist": histogram(values),
        "values": values,
    }


def pool_phase_runs(runs: list[dict]) -> dict:
    """One result from N iterations of a scale phase, pooled the way the
    latency phase pools: Every probe sample set (baseline, burst, recovery,
    latency) is concatenated and its percentiles recomputed over the pool,
    deadline jitter and launch samples the same, work counts are listed per
    process across all iterations with the total as the per-iteration mean,
    and survived is true only if every iteration survived. The raw sample
    lists do not reach the stored result."""
    if not runs:
        return {}
    if len(runs) == 1:
        return _strip_values(runs[0])
    out = dict(runs[0])
    out["iterations"] = len(runs)
    out["survived"] = all(r.get("survived", True) for r in runs)
    for key in ("baseline", "burst", "recovery", "latency"):
        if isinstance(runs[0].get(key), dict) and "values" in runs[0][key]:
            pooled = [v for r in runs for v in (r.get(key) or {}).get("values", [])]
            out[key] = probe_stats(pooled)
    if "work_per_process" in runs[0]:
        per = [w for r in runs for w in r.get("work_per_process", [])]
        out["work_per_process"] = per
        out["work_total"] = int(sum(r.get("work_total", 0) for r in runs) / len(runs))
        out["work_min"] = min(per) if per else 0
        out["work_max"] = max(per) if per else 0
    if "jitter_values" in runs[0]:
        jit = [j for r in runs for j in r.get("jitter_values", [])]
        thr = runs[0].get("threshold_us", 0)
        missed = sum(1 for j in jit if j > thr)
        out.update({
            "total_frames": len(jit),
            "missed_frames": missed,
            "miss_ratio": round(missed / len(jit), 4) if jit else 0.0,
            "jitter_median_us": int(percentile(jit, 50)) if jit else 0,
            "jitter_p99_us": int(percentile(jit, 99)) if jit else 0,
            "jitter_worst_us": int(max(jit)) if jit else 0,
            "jitter_hist": histogram([float(j) for j in jit]),
            "jitter_values": jit,
        })
    if "launch_values" in runs[0]:
        lv = [x for r in runs for x in r.get("launch_values", [])]
        m, _std = mean_stdev([float(x) for x in lv]) if lv else (0.0, 0.0)
        out.update({
            "launches": len(lv),
            "launch_mean_us": int(m),
            "launch_median_us": int(percentile(lv, 50)) if lv else 0,
            "launch_p99_us": int(percentile(lv, 99)) if lv else 0,
            "launch_worst_us": int(max(lv)) if lv else 0,
            "hist": histogram([float(x) for x in lv]),
            "launch_values": lv,
        })
    return _strip_values(out)


def _strip_values(d: dict) -> dict:
    """The stored result without the raw sample lists."""
    out = {}
    for k, v in d.items():
        if k in ("values", "jitter_values", "launch_values"):
            continue
        out[k] = _strip_values(v) if isinstance(v, dict) else v
    return out


def measure_latency(tool: Path, n_cpus: int, iterations: int = 1,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup probe (discard output, let scheduler classify workload)
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
    probe_pid = None
    for i in range(iterations):
        log_info(f"Latency iteration {i + 1}/{iterations}: {duration_secs}s")
        probe = subprocess.Popen(
            [str(tool), "probe"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        probe_pid = probe.pid
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
            "probe_pid": probe_pid,
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


def measure_burst(tool: Path, n_cpus: int, burst_size: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup (discard output)
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(baseline_secs)
    baseline_probe.send_signal(signal.SIGINT)
    try:
        baseline_out, _ = baseline_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        baseline_probe.kill()
        baseline_out, _ = baseline_probe.communicate()
    baseline = parse_probe_output(baseline_out.decode(errors="replace"), keep_values=True)
    log_info(f"Baseline: {baseline['samples']} samples, "
             f"median={baseline['median_us']}us, "
             f"p99={baseline['p99_us']}us")

    # Burst measurement: start probe, fire burst, measure during + after
    burst_probe = subprocess.Popen(
        [str(tool), "probe"],
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
    burst_stats = parse_probe_output(burst_out.decode(errors="replace"), keep_values=True)
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
        [str(tool), "probe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)
    recovery_probe.send_signal(signal.SIGINT)
    try:
        recovery_out, _ = recovery_probe.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        recovery_probe.kill()
        recovery_out, _ = recovery_probe.communicate()
    recovery_stats = parse_probe_output(recovery_out.decode(errors="replace"), keep_values=True)
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


def measure_longrun(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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
    latency = parse_probe_output(probe_out.decode(errors="replace"), keep_values=True)

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
        "probe_pid": probe.pid,
        "work_total": total_work,
        "work_min": min_work,
        "work_max": max_work,
        "work_per_process": work_counts,
    }


# MIXED WORKLOAD MEASUREMENT (BURST + LONGRUN SIMULTANEOUS)

def measure_mixed(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # Warmup
    log_info(f"Warmup: {warmup_secs}s")
    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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
    latency = parse_probe_output(probe_out.decode(errors="replace"), keep_values=True)

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
        "probe_pid": probe.pid,
        "work_total": total_work,
        "work_min": min_work,
        "work_max": max_work,
        "work_per_process": work_counts,
    }


# PERIODIC DEADLINE MEASUREMENT

def measure_deadline(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
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
        "jitter_values": all_jitter,
    }

    log_info(f"Deadline: {total} frames, {missed} missed "
             f"({miss_ratio:.1%}), "
             f"jitter p99={result['jitter_p99_us']}us, "
             f"worst={result['jitter_worst_us']}us")
    return result


# IPC ROUND-TRIP MEASUREMENT

def _med(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def ipc_pair_from_recording(rec) -> dict:
    """The ipc pair's OWN wake2run, not the capture's.

    EVERY PER-EVENT REPORT POOLS EVERY TASK IN THE CAPTURE, and on a traced ipc
    cell that is the round-trip pair, the stress workers, and montauk -- which is
    the top CPU holder in those captures (67.8ms, 20.3ms, 19.3ms at 12C, three of
    the top six held_by entries). Narrowed to the two tids the `waits` report
    names as the semaphore pair:

        aggregate   225k wake2run   p50 6us  p99 12us  p999 902us  worst 1994us
        tid 12081    20.4k          p50 6us  p99 14us  p999 865us  worst 1944us
        tid 12078    20.0k          p50 6us  p99 10us  p999 651us  worst 1646us

    THE PAIR IS 18% OF THE SAMPLES. Here it happens to track the aggregate, and
    nothing guaranteed that -- the whole week's wake2run figures were quoted
    without anyone knowing whether they described the workload or the tracer.
    Both go in the result so the divergence is visible rather than assumed.

    The pair is identified from the capture, not configured: `waits` publishes
    completions per (tid,obj) and the two busiest are the two sides.
    """
    out = {}
    if rec is None:
        return out
    target = getattr(rec, "events_path", None)
    if not target:
        return out
    env = montauk_envelope(target, report="waits")
    if env is None:
        return out
    wt = envelope_report(env, "waits")
    if not wt:
        return out
    by = envelope_gauge_by_label(wt, "montauk_analysis_waits_total", "tid")
    pair = sorted(((float(v), t) for t, v in by.items() if v is not None),
                  reverse=True)[:2]
    if not pair:
        return out
    out["pair_tids"] = ",".join(t for _, t in pair)
    tot_n = tot_p50 = 0.0
    worst = 0.0
    for _, t in pair:
        try:
            e = montauk_envelope(target, report="sched", tid=int(t))
        except (TypeError, ValueError):
            continue
        if e is None:
            continue
        sched = envelope_report(e, "sched")
        w = sched.get("wake2run") or {}
        n = w.get("count")
        if not n:
            continue
        tot_n += float(n)
        tot_p50 += float(w.get("p50_us") or 0.0) * float(n)
        worst = max(worst, float(w.get("worst_us") or 0.0))
    if tot_n:
        out["pair_wake2run_n"] = tot_n
        out["pair_wake2run_p50_us"] = tot_p50 / tot_n
        out["pair_wake2run_worst_us"] = worst
    return out


def ipc_contention_from_recording(rec) -> dict:
    """The four reports that name the sem defect, none of which the suite read.

    THE BOARD'S OWN CONSTRAINT SAYS THE sem GAP IS A FUTEX LIVELOCK, and it was
    established by running montauk BY HAND on two captures. `futex`, `spins`,
    `waits` and `wakers` have never been folded into a result, so no .prom in the
    78-run archive carries them and the history that would show WHEN the mode
    flips does not exist. Every reading of it has been a one-off.

    THEY DISCRIMINATE. On one traced run, ipc, BPF arm:

        2C  futex NO-FUTEX-BLOCKED  spins NONE      waits 20.9k
        4C  futex FUTEX-SPIN        spins NONE      waits 22.2k
        8C  futex FUTEX-SPIN        spins LIVELOCK  waits 39.9k
       12C  futex FUTEX-SPIN        spins LIVELOCK  waits 39.9k

    39.9k is 2 x 19.9k, the full round-trip count -- every sem_wait blocked into
    the futex, the userspace fast path dead. ~21k is that path firing. `spins` is
    the binary form of the same thing. Which cells sit where MOVES between runs,
    which is exactly why it needs to be in the archive rather than in a terminal
    scrollback.
    """
    out = {}
    if rec is None:
        return out
    target = getattr(rec, "events_path", None)
    if not target:
        return out
    env = montauk_envelope(target, report="futex,spins,waits,wakers")
    if env is None:
        return out

    fx = envelope_report(env, "futex")
    if fx:
        klass, _ = envelope_verdict(fx)
        out["futex_class"] = klass
        g = envelope_gauges(fx)
        v = g.get("montauk_analysis_futex_blocked_threads")
        if v is not None:
            out["futex_blocked_threads"] = float(v)

    sp = envelope_report(env, "spins")
    if sp:
        klass, _ = envelope_verdict(sp)
        out["spins_class"] = klass
        # The binary the mode split reads as: LIVELOCK or not.
        out["spin_livelock"] = 1.0 if klass == "LIVELOCK" else 0.0

    wt = envelope_report(env, "waits")
    if wt:
        # waits_total is published PER (tid,obj), so the sum is the workload's
        # blocked-wait count and the max is the dominant side of the pair.
        by = envelope_gauge_by_label(wt, "montauk_analysis_waits_total", "tid")
        vals = [float(v) for v in by.values() if v is not None]
        if vals:
            out["waits_total"] = sum(vals)
            out["waits_dominant"] = max(vals)

    wk = envelope_report(env, "wakers")
    if wk:
        klass, _ = envelope_verdict(wk)
        out["wakers_class"] = klass
        g = envelope_gauges(wk)
        for src, dst in (("waker_pids", "waker_pids"),
                         ("waker_hot_pids", "waker_hot_pids"),
                         ("waker_monogamy_ratio", "waker_monogamy")):
            v = g.get(f"montauk_analysis_{src}")
            if v is not None:
                out[dst] = float(v)
        q = envelope_gauge_by_label(wk, "montauk_analysis_waker_run_length",
                                    "quantile")
        if q.get("0.99") is not None:
            out["waker_run_p99"] = float(q["0.99"])
    return out


def ipc_hop_from_recording(rec) -> dict:
    """Read the scheduler-side half of one IPC hop back out of the recording the
    --trace path already writes.

    An RTT is two hops, and a hop is: The waker runs ops.select_cpu and
    ops.enqueue in its OWN context, then kicks; the target CPU then enters
    dispatch, drains the DSQ and the wakee runs. montauk's WAKE2RUN comes from
    tp/sched/sched_wakeup, which fires AFTER enqueue -- so it measures the second
    half only, and the first half is invisible to it by construction. That
    asymmetry is the whole reason this is worth reading: RTT/2 minus wake2run
    minus the run leaves the waker-side path cost as a residual, and the two
    halves have completely different suspects. Dispatch-side is the waterfall,
    the kick and the DSQ pop; waker-side is the map-lookup walk through
    warm_seat_pick, phi_warm_target and pick_pcpu_dsq_with_spill.

    The capture was already being written and analysed by nothing -- the same gap
    fork-thread had, where the recording existed and the report read three
    numbers out of it and stopped.
    """
    out = {}
    if rec is None or not getattr(rec, "dir", None):
        return out
    env = montauk_envelope(rec.dir)
    if env is None:
        return out
    out.update(envelope_capture(env))
    sched = envelope_report(env, "sched")
    w = sched.get("wake2run") or {}
    if w.get("p50_us") is not None:
        out["wake2run_p50_us"] = float(w["p50_us"])
    # The denominator for the pair share below: how many of the capture's
    # wake2run samples exist at all, so "the pair is 18% of them" is a number
    # rather than an assertion.
    if w.get("count") is not None:
        out["wake2run_n"] = float(w["count"])
    klass, _ = envelope_verdict(sched)
    if klass:
        out["sched_class"] = klass

    # A DIGEST CARRIES THREE REPORTS; THE STREAM CARRIES TWENTY-NINE. `--digest`
    # folds sched, dispatch-stall and kstrand, so slice, service and every klass
    # past those two are absent from it entirely. Read them off the .events
    # stream that sits beside the recording dir.
    ev_path = getattr(rec, "events_path", None) or Path(str(rec.dir) + ".events")
    if not Path(ev_path).is_file():
        return out
    ev = montauk_envelope(ev_path)
    if ev is None:
        return out

    # SLICE IS WHAT THE ORDERING KEY'S WARP IS COMPUTED FROM
    # (codel_target_ns - last_run_ns), so the spread of dispatched run lengths
    # bounds how far the warp can reorder at all. fork-thread reads it; the IPC
    # capture carries the same distribution and nothing looked.
    sq = envelope_gauge_by_label(envelope_report(ev, "slice"),
                                 "montauk_analysis_slice_us", "quantile")
    for lab, k in (("0.5", "slice_p50_us"), ("0.99", "slice_p99_us"),
                   ("worst", "slice_worst_us")):
        if lab in sq:
            out[k] = float(sq[lab])
    sl = envelope_gauges(envelope_report(ev, "slice"))
    if "montauk_analysis_slice_trajectory_inversion" in sl:
        out["slice_inversion"] = float(
            sl["montauk_analysis_slice_trajectory_inversion"])
    sg = envelope_gauges(envelope_report(ev, "service"))
    for g, k in (("montauk_analysis_service_top1_pct", "service_top1_pct"),
                 ("montauk_analysis_service_pids", "service_pids")):
        if g in sg:
            out[k] = float(sg[g])

    # THE KLASS TOKENS, WHICH ARE THE COMPARABLE HALF. montauk keeps klass apart
    # from the verdict sentence so it can be compared exactly; collect them per
    # arm so the caller can print only where two arms DISAGREE.
    out["classes"] = {}
    for rn in ("sched", "locality", "dispatch-stall", "placement-race",
               "work-conservation", "slice", "service", "seat"):
        k, _ = envelope_verdict(envelope_report(ev, rn))
        if k:
            out["classes"][rn] = k
    return out


def reduce_ipc_runs(runs: list) -> dict:
    """Collapse N measure_ipc results into one: MEDIAN per metric, with the
    across-run spread kept beside it.

    Percentiles do not average, and pooling the raw samples would erase the one
    thing repetition exists to expose -- how far a cell moves between IDENTICAL
    runs. So each run keeps its own distribution, the median is the robust point
    estimate an A/B is read from, and spread[cell.metric] = [lo, hi] IS the
    measured noise floor for that cell.
    """
    if not runs:
        return {"survived": True, "pairs": 0}
    if len(runs) == 1:
        return runs[0]
    out = dict(runs[0])
    out["iterations"] = len(runs)
    cells = [r.get("cell") or {} for r in runs]
    keys = set()
    for c in cells:
        keys.update(c.keys())
    merged, spread = {}, {}
    for k in sorted(keys):
        dists = [c[k] for c in cells if c.get(k)]
        if not dists:
            merged[k] = None
            continue
        d = {}
        for metric in ("p50", "p99", "p999", "worst", "n"):
            vals = [x[metric] for x in dists if metric in x]
            if not vals:
                continue
            d[metric] = int(_med(vals))
            if metric != "n" and len(vals) > 1:
                spread[f"{k}.{metric}"] = [min(vals), max(vals)]
        merged[k] = d
    out["cell"] = merged
    out["spread"] = spread
    pipe = merged.get("rtt_pipe") or {}
    out["total_ops"] = pipe.get("n", 0)
    out["rtt_median_us"] = pipe.get("p50", 0)
    out["rtt_p99_us"] = pipe.get("p99", 0)
    out["rtt_p999_us"] = pipe.get("p999", 0)
    out["rtt_worst_us"] = pipe.get("worst", 0)
    out["survived"] = all(r.get("survived", True) for r in runs)
    return out


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
    # THIS USED TO SCRAPE montauk's PROSE. It ran the analyzer for TEXT and then
    # walked the output looking for "REPORT " headers and "VERDICT:" prefixes,
    # re-deriving from a rendered page what the tool publishes as typed data. A
    # verdict sentence carries numbers and is free to be reworded; the envelope's
    # klass is a fixed token precisely so a caller never has to do this.
    res: dict = {}
    env = montauk_envelope(events_path)
    if env is None:
        return res
    for name, key in (("sched", "wake2run"), ("locality", "locality"),
                      ("fractal", "fractal")):
        rep = envelope_report(env, name)
        klass, verdict = envelope_verdict(rep)
        if verdict:
            res[key] = verdict
        if klass:
            res[f"{key}_class"] = klass
    stall = envelope_report(env, "dispatch-stall")
    sklass, sverdict = envelope_verdict(stall)
    if sverdict:
        res["holder"] = sverdict
    if sklass:
        res["holder_class"] = sklass
    # The ranked entities montauk already names, which this suite has printed
    # tables around without ever saying WHO.
    offs = envelope_offenders(stall)
    if offs:
        res["holder_offenders"] = offs[:3]
    return res


# APPLICATION LAUNCH MEASUREMENT

def measure_launch(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
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
        "launch_values": latencies_us,
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

        # REGIME: [BPF], [LIGHT], [MIXED] or [HEAVY]
        regime_match = re.search(
            r'\[(BPF|Light|Mixed|Heavy|LIGHT|MIXED|HEAVY)\]', line)
        if regime_match:
            tick["regime"] = regime_match.group(1)

            if tick["regime"] != "BPF":
                m = re.search(r"p99:\s*(\d+)us", line)
                if m:
                    tick["p99_us"] = int(m.group(1))
                m = re.search(r"p99:.*?\[B:(\d+)\s*I:(\d+)\s*L:(\d+)\]", line)
                if m:
                    tick["tier_p99_batch"] = int(m.group(1))
                    tick["tier_p99_interactive"] = int(m.group(2))
                    tick["tier_p99_latcrit"] = int(m.group(3))
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

            # The kernel's sched_ext event counters at the arm's stop
            for event, count in sorted((sched_data.get("scx_events") or {}).items()):
                gauge("pandemonium_bench_scx_event",
                      "Kernel sched_ext event counter when the arm was stopped",
                      count, {**labels, "event": event})

            # Package temperature at the arm's start and end
            th = sched_data.get("thermal") or {}
            for edge in ("start", "end"):
                if th.get(f"{edge}_c") is not None:
                    gauge("pandemonium_bench_cpu_max_c",
                          "Package max temperature at the arm's edge (C)",
                          th[f"{edge}_c"], {**labels, "edge": edge})

            # Recorded draws of the probe cells (--trace with the cell named)
            for cell, draw in (sched_data.get("probe_trace") or {}).items():
                for key, _label, _fmt in PROBE_ROWS:
                    if key in draw:
                        gauge(f"pandemonium_bench_recorded_{key}",
                              f"Recorded probe draw, {key}",
                              draw[key], {**labels, "cell": cell})

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

                for _k, _help in (
                        ("spin_livelock",
                         "1 = montauk's spins report read LIVELOCK"),
                        ("futex_blocked_threads",
                         "Threads blocked on a futex at trace end"),
                        ("waits_total",
                         "Blocked wait completions summed over (tid,obj)"),
                        ("waits_dominant",
                         "Blocked wait completions on the dominant tid/obj"),
                        ("waker_pids", "Distinct waker pids"),
                        ("waker_monogamy",
                         "Fraction of wakes that extend a same-waker run"),
                        ("waker_run_p99",
                         "P99 consecutive wakes by one waker"),
                        ("pair_wake2run_n",
                         "wake2run samples belonging to the ipc pair alone"),
                        ("pair_wake2run_p50_us",
                         "Median wake2run of the ipc pair alone (us)"),
                        ("pair_wake2run_worst_us",
                         "Worst wake2run of the ipc pair alone (us)"),
                        # CAPTURE QUALITY. envelope_capture has folded these
                        # since it was written and nothing published them, so a
                        # cell whose capture LOST events looked exactly like one
                        # that did not. montauk reports absence rather than zero
                        # when a capture predates drop accounting, so a missing
                        # gauge here means unknown loss, not proven clean.
                        ("dropped", "Events the capture ring dropped"),
                        ("observed", "Events the capture observed")):
                    if ipc.get(_k) is not None:
                        gauge(f"pandemonium_bench_ipc_{_k}", _help,
                              ipc[_k], labels)

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

                # TOTAL STEALS AND DISPATCHES, WHICH THE cross_domain_* FAMILY
                # CANNOT SEE. Those count only the CROSS-domain half, and on a
                # two-domain box most moves are same-domain -- so a steal share
                # read from them alone is a share of the minority. nr_steal is
                # every successful STEP 1 peer move_to_local, cross or not, and
                # against nr_dispatches it is the drain side's actual share of
                # the migration count.
                for tk, tdesc in (("steal", "Total STEP 1 peer steals"),
                                  ("steal_lost",
                                   "STEP 1 moves lost to another thief"),
                                  ("steal_base_held",
                                   "Steal heads the live base cost held past tau's"),
                                  ("spill", "Total sibling spills"),
                                  ("kick_declined",
                                   "Requeue kicks the price refused outright"),
                                  ("sleepless_cut",
                                   "Non-cutting wakes seated by the cut on a "
                                   "resident that does not sleep"),
                                  ("sleepless_miss",
                                   "Non-cutting wakes that found no such resident"),
                                  ("dispatches", "Total dispatches")):
                    if tk in knobs:
                        gauge(f"pandemonium_bench_{tk}_total", tdesc,
                              knobs[tk], telem_labels)

                # OSCILLATOR ENVELOPE PARKS: the idle-quiescence collapse
                # detector. Zero after an idle-heavy arm means the envelope
                # never parked, so the control effort kept recomputing through
                # the quiet -- the counter to read beside an idle power delta.
                if "osc_park" in knobs:
                    gauge("pandemonium_bench_osc_park",
                          "Oscillator envelope park entries",
                          knobs["osc_park"], telem_labels)

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
    slug = bench_slug(data)
    # prism-scale keeps the bare "{version}-{stamp}" archive name it has always
    # had -- baseline_gate selects on it. Every other mode takes
    # the prism-<bench>-<version>-<stamp> form the other prism-* benches use.
    # SO DOES A FULL PASS WITH NO PANDEMONIUM ARM IN IT (--only, --arm): It is
    # not this version's scale run, and under the bare name baseline_gate took
    # another scheduler's pass for the candidate and found nothing to compare.
    pand_ran = any("PANDEMONIUM" in name
                   for schedulers in data.get("results", {}).values()
                   for name in schedulers)
    path = ARCHIVE_DIR / (f"{version}-{stamp}.prom" if slug == "prism-scale" and pand_ran
                          else f"{slug}-{version}-{stamp}.prom")
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
        ("pandemonium_sys_osc_park", "Oscillator envelope park entries"),
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
        if "osc_park" in knobs:
            f.write(f"pandemonium_sys_osc_park{{{label_str}}} "
                    f"{knobs['osc_park']}\n")


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


# prism-scale's --ipc/--deadline/--launch/--mixed/--longrun/--burst modes are
# separate benches to anyone reading the cache, so they get separate filenames.
# Sharing prism-scale's was not only confusing, it was wrong: the archive prom
# is named "{version}-{stamp}.prom", and baseline_gate globs "[0-9]*.prom", so
# an IPC-only run dropped a file it read as that version's full scale run. The metric FAMILY stays
# pandemonium_scale_* -- one schema, and renaming it would orphan every archived
# run -- only the file is named for the bench that produced it.
_BENCH_SLUGS = (
    ("deadline_only", "prism-deadline"),
    ("ipc_only",      "prism-ipc"),
    ("launch_only",   "prism-launch"),
    ("mixed_only",    "prism-mixed"),
    ("longrun_only",  "prism-longrun"),
    ("burst_only",    "prism-burst"),
    ("compile_only",  "prism-compile"),
    ("latency_only",  "prism-latency"),
)

# THE CELLS OF A SCALE PASS, IN THE ORDER IT RUNS THEM UNDER ONE SCHEDULER
# START. --cells NAMES ANY OF THEM; NONE NAMED IS THE FULL PASS.
SCALE_CELLS = ("latency", "compile", "burst", "longrun", "mixed", "deadline",
               "ipc", "launch")


def scale_cells(args) -> tuple[str, ...] | None:
    """The cells a prism-scale run is asked for, in the bench's own order, or
    None for the full pass (nothing named, or all of them). ValueError names a
    cell the bench does not have."""
    named = [c.strip() for c in (getattr(args, "cells", None) or "").split(",") if c.strip()]
    unknown = [c for c in named if c not in SCALE_CELLS]
    if unknown:
        raise ValueError(f"unknown cell(s): {', '.join(unknown)} "
                         f"({', '.join(SCALE_CELLS)})")
    if not named or len(set(named)) == len(SCALE_CELLS):
        return None
    return tuple(c for c in SCALE_CELLS if c in named)


def bench_slug(data: dict) -> str:
    """Output basename for a prism-scale run, by mode. One cell is named for
    the cell; several, short of the full pass, are prism-cells, so neither is
    read as that version's full scale run."""
    for key, slug in _BENCH_SLUGS:
        if data.get(key):
            return slug
    if data.get("cells"):
        return "prism-cells"
    return "prism-scale"


def format_scx_events(schedulers: dict, width: int = 100) -> list[str]:
    """The kernel's sched_ext event counters each arm stood at when it was
    stopped, every counter, zeros included: A zero is the reading. Wrapped under
    the arm's name. Empty when no arm carries any."""
    lines = []
    for sched_name, sched_data in schedulers.items():
        events = sched_data.get("scx_events") or {}
        if not events:
            continue
        if not lines:
            lines += ["", "SCHED_EXT EVENTS AT EACH ARM'S STOP (THE KERNEL'S OWN COUNTERS)"]
        row = f"{sched_name:<28}"
        for key in sorted(events):
            cell = f" {key}={events[key]}"
            if len(row) + len(cell) > width and row.strip():
                lines.append(row)
                row = " " * 28
            row += cell
        lines.append(row)
    return lines


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
    elif data.get("latency_only"):
        mode = "LATENCY ONLY"
    elif data.get("compile_only"):
        mode = "COMPILE ONLY"
    elif data.get("cells"):
        mode = "CELLS " + ", ".join(data["cells"]).upper()
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

        # Package temperature at each arm's start and end
        has_thermal = any((s.get("thermal") or {}).get("start_c") is not None
                          for s in schedulers.values())
        if has_thermal:
            lines.append("")
            lines.append(f"{'SCHEDULER':<28} {'START C':>8} {'END C':>8}")
            for sched_name, sched_data in schedulers.items():
                th = sched_data.get("thermal") or {}
                if th.get("start_c") is None:
                    continue
                end = th.get("end_c")
                lines.append(f"{sched_name:<28} {th['start_c']:>8.0f} "
                             f"{(end if end is not None else float('nan')):>8.0f}")

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
            # A median with no spread beside it is the single-draw number under a
            # new name. When the cell was repeated, print what it ranged over --
            # that range IS this cell's noise floor, and an A/B delta smaller than
            # it is not a result.
            _rep = max((s.get("ipc", {}).get("iterations", 1)
                        for s in schedulers.values()), default=1)
            _hdr = (f"{'SCHEDULER':<28} {'STATUS':>8} {'PRIM':>8} "
                    f"{'P50':>9} {'P99':>9} {'P99.9':>9} {'WORST':>9}")
            if _rep > 1:
                _hdr += f" {'P99 RANGE (n=' + str(_rep) + ')':>22}"
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
                    if _rep > 1:
                        _sp = (ipc.get("spread") or {}).get(f"{key}.p99")
                        row += (f" {str(_sp[0]) + '-' + str(_sp[1]) + 'us':>22}"
                                if _sp else f" {'--':>22}")
                    if _thru:
                        # implied round-trips/sec from the median RTT (1 RT ~= p50)
                        rts = int(1e6 / d['p50']) if d.get('p50') else 0
                        row += f" {rts:>10}"
                    lines.append(row)
                # WHERE THE HOP'S MICROSECONDS GO. An RTT is two hops. A hop is the
                # waker running select_cpu and enqueue in its OWN context, then the
                # kick, then the target CPU's dispatch, then the wakee running.
                # WAKE2RUN fires after enqueue, so it covers the SECOND half only,
                # and RTT/2 minus it is the waker-side path -- the map-lookup walk
                # through warm_seat_pick, phi_warm_target and
                # pick_pcpu_dsq_with_spill. The two halves have different suspects,
                # which is the whole reason to split them.
                _w2r = ipc.get("wake2run_p50_us")
                _hp50 = ipc.get("hop_pipe_p50_us")
                if _w2r is not None and _hp50:
                    _half = _hp50 / 2.0
                    # REST, NOT waker-side. A hop is waker-side + wake2run + the
                    # wakee's own run, and only wake2run is measured here -- so what
                    # is left over is the other TWO terms, not one of them. The run
                    # is small for a pipe hop and it is not zero, and montauk's
                    # slice p50 cannot separate it because that distribution is over
                    # every task in the capture including the stress workers.
                    _rest = _half - _w2r
                    lines.append(
                        f"{'':<28} {'':>8} {'hop':>8} "
                        f"{'half-RTT ' + f'{_half:.1f}us':>18}"
                        f"{'  wake2run ' + f'{_w2r:.1f}us':>20}"
                        f"{'  rest ' + f'{_rest:.1f}us':>16}"
                        f"   (iter 1; rest = waker-side + run)")
                # THE PAIR ROW. The aggregate above pools every task in the
                # capture -- the pair, the stress workers and montauk, which is
                # the top CPU holder in these recordings. This is the pair alone,
                # and the sample share says how much of the aggregate was ever
                # about the workload.
                _pn = ipc.get("pair_wake2run_n")
                _pp50 = ipc.get("pair_wake2run_p50_us")
                if _pn and _pp50 is not None:
                    _all = ipc.get("wake2run_n")
                    _share = (f" ({100.0 * _pn / _all:.0f}% of capture)"
                              if _all else "")
                    lines.append(
                        f"{'':<28} {'':>8} {'pair':>8} "
                        f"{'tids ' + str(ipc.get('pair_tids', '-')):>18}"
                        f"{'  wake2run ' + f'{_pp50:.2f}us':>20}"
                        f"{'  n ' + f'{_pn:.0f}' + _share:>28}")
                # THE CONTENTION ROW. The sem fast path either fires or it does
                # not, and waits_total says which: 2 x the round-trip count means
                # every sem_wait blocked into the futex. Silent without --trace.
                if ipc.get("waits_total") or ipc.get("spins_class"):
                    _wt = ipc.get("waits_total") or 0.0
                    _fx = ipc.get("futex_class", "-")
                    _sp = ipc.get("spins_class", "-")
                    _mono = ipc.get("waker_monogamy")
                    lines.append(
                        f"{'':<28} {'':>8} {'sem':>8} "
                        f"{'waits ' + f'{_wt:.0f}':>18}"
                        f"{'  futex ' + _fx:>26}"
                        f"{'  spins ' + _sp:>18}"
                        + (f"{'  monogamy ' + f'{_mono:.3f}':>20}"
                           if _mono is not None else ""))
                elif _thru and ipc.get("completeness") is not None:
                    # A recording was read and it carried no wake2run. Absence of a
                    # recording is silent by design -- a run without --trace owes
                    # no hop row.
                    lines.append(f"{'':<28} {'':>8} {'hop':>8} "
                                 f"  wake2run unavailable -- recording analysed but "
                                 f"carried no sched stream")
                # RUN LENGTH AND SERVICE, the two distributions the warp is
                # computed from and measured against. Silent when the capture
                # carried neither, which is every run without --trace.
                _sp50, _sp99 = ipc.get("slice_p50_us"), ipc.get("slice_p99_us")
                _top1, _pids = (ipc.get("service_top1_pct"),
                                ipc.get("service_pids"))
                if _sp50 is not None or _top1 is not None:
                    _seg = []
                    if _sp50 is not None:
                        _seg.append(f"slice p50 {_sp50:.1f}us")
                    if _sp99 is not None:
                        _seg.append(f"p99 {_sp99:.1f}us")
                    if ipc.get("slice_inversion") is not None:
                        _seg.append(f"inversion {ipc['slice_inversion']:.3f}")
                    if _top1 is not None:
                        _seg.append(f"top-1 PID {_top1:.1f}%")
                    if _pids is not None:
                        _seg.append(f"of {_pids:.0f} PIDs")
                    lines.append(f"{'':<28} {'':>8} {'run':>8} "
                                 + "  ".join(_seg))
                # montauk deep analysis of this scheduler's capture (--ipc only)
                # (hop absolutes carry the tracer's own per-event cost, which is a
                #  real fraction of a 7-15us hop -- read the RATIO between arms,
                #  never the absolute. Both arms pay the same tax.)
                if _thru:
                    mon = ipc.get("montauk") or {}
                    for mk in ("wake2run", "locality", "holder", "fractal"):
                        mv = mon.get(mk)
                        if mv:
                            lines.append(f"{'':<20}{mk}: {mv}")

            # WHERE THE ARMS DISAGREE, and only there. montauk publishes a klass
            # token per report precisely so it can be compared exactly rather
            # than parsed back out of a verdict sentence. Agreement is not a
            # finding and is not printed.
            _cls = {sn: (sd.get("ipc", {}).get("classes") or {})
                    for sn, sd in schedulers.items()}
            _cls = {sn: c for sn, c in _cls.items() if c}
            if len(_cls) > 1:
                _arms = list(_cls)
                _rows = [
                    r for r in sorted(set().union(*(set(c) for c in _cls.values())))
                    if len({_cls[a].get(r, "") for a in _arms}) > 1]
                if _rows:
                    lines.append("")
                    lines.append("CLASS DISAGREEMENT  (montauk verdict class, per arm)")
                    lines.append(f"{'REPORT':<28}"
                                 + "".join(f" {a:>22}" for a in _arms))
                    for r in _rows:
                        lines.append(f"{r:<28}"
                                     + "".join(f" {_cls[a].get(r, '--'):>22}"
                                               for a in _arms))

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

        lines += format_scx_events(schedulers)
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

    lines += format_probe_trace(results)

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


def measure_burst_starvation(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers.append(p)

    # WARMUP: LET SCHEDULER CLASSIFY WORKLOAD
    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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


def measure_work_stealing(tool: Path, n_cpus: int) -> dict:
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sym_workers.append(p)

    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        asym_workers.append(p)

    warmup = subprocess.Popen(
        [str(tool), "probe"],
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
        [str(tool), "probe"],
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


def measure_sojourn_ceiling(tool: Path, n_cpus: int,
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
            [str(tool), "stress-worker", "--cpu", str(cpu)],
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


def measure_pcpu_starvation(n_cpus: int, duration_secs: float = 20.0) -> dict:
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



def field_arms(n_cpus: int, schedulers="", all_scx: bool = False,
               pandemonium: str | None = None, only: str = ""):
    """The (sched_name, activate_cmd) arms of a traced field run: the field of
    scheduler_field. A field run carries one PANDEMONIUM arm, ADAPTIVE as
    "PANDEMONIUM", unless the selection names one arm."""
    pand = pandemonium_arms(pandemonium, verbose=False)
    if pandemonium not in ("adaptive", "bpf"):
        pand = [("PANDEMONIUM", pandemonium_arms("adaptive", verbose=False)[0][1])]
    arms = entries_for_cores(
        scheduler_field(pandemonium, pand, only or schedulers, all_scx, exact=bool(only)),
        n_cpus)
    if not arms:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is installed")
    return arms


def trace_pcpu_burst(stamp: str, n_cpus: int, duration: int,
                     schedulers: str = "", all_scx: bool = False,
                     pandemonium: str | None = None,
                     only: str = "") -> int:
    """One montauk-traced burst-starvation capture, hard-capped at `duration`s.
    Saturates every CPU, then detonates fork/exec bursts for the window while
    montauk records the per-event wake-to-run -- so the worst burst wakeup is in
    the .events for montauk --analyze --digest. Trace IS the artifact; no
    probe/baseline/recovery measurement here. The stress workers are the suite's
    probe tool (comm pand-stress), so montauk targets them and the scheduler.
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
                       [str(probe_tool()), "stress-worker", "--cpu", str(c)],
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
    # the same body. The stress workers are pand-stress under every scheduler,
    # so montauk targets that comm and the scheduler's for all arms.
    arms = field_arms(n_cpus, schedulers, all_scx, pandemonium, only)
    traced = 0
    for sched_name, activate_cmd in arms:
        # sched_detail: this arm's ENTIRE question is why a burst wake sat on the
        # tick floor (60%+ of wakes here), and without CPU_IDLE + PICK streaming
        # dispatch-stall can only say PREEMPT-STARVED and stop -- it cannot split
        # DARK (the CPU sat idle with the task queued) from HELD (a hog held it),
        # and placement-race / kick-latency / slice return "re-capture to
        # resolve". The ~6x event cost buys the attribution this stage exists for.
        # pipewire IS TRACED BESIDE THE WORKLOAD: A BURST IS HEARD THROUGH THE
        # AUDIO SERVER's CYCLE, AND ITS WAKES ARE IN THE CAPTURE ONLY IF ITS
        # TREE IS.
        rec_dir, _ = trace_workload(sched_name, activate_cmd,
                                    "pandemonium,pand-stress,pipewire",
                                    f"pcpu-burst-{n_cpus}c", stamp,
                                    body, events=True, pin_cpu=drain,
                                    sched_detail=True)
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
                         schedulers="", all_scx=False, pandemonium=None,
                         only="") -> int:
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
        arms = field_arms(n_cpus, schedulers, all_scx, pandemonium, only)
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
    if not getattr(args, "trace", False):
        log_error("prism-coldwake is a capture, cold-wake and --storm alike: "
                  "run it with --trace")
        return 2
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
    # Without this the EEVDF arm traces under whatever is loaded and every
    # other arm wedges on "stale scheduler ... still registered".
    if not clear_loaded_scheduler():
        return 1
    n_cpus = get_online_cpus()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if getattr(args, "storm", False):
        return trace_storm_cycle(stamp, n_cpus, args.duration,
                                 busy_per_cpu=args.busy_per_cpu,
                                 sleep_us=args.rt_sleep_us, spin_us=args.rt_spin_us,
                                 schedulers=args.schedulers, all_scx=args.all_scx,
                                 pandemonium=getattr(args, "pandemonium", None),
                                 only=getattr(args, "only", ""))
    return trace_coldwake_cycle(stamp, n_cpus, args.duration, dwell=args.dwell,
                                sizes=args.sizes, mem_sizes=args.mem_sizes,
                                schedulers=args.schedulers, all_scx=args.all_scx,
                                pandemonium=getattr(args, "pandemonium", None),
                                only=getattr(args, "only", ""))


def _storm_captured_env(env):
    """montauk v7.17.0+: the envelope carries an explicit
    montauk_analysis_storm_captured gauge (0 = storm probes unattached, the
    surface was never measured). Returns True/False, or None when there is no
    envelope (older analyzer) so the caller falls back to the text verdict.

    Takes the PARSED envelope rather than a JSON string: the caller already has
    one from montauk_report, and re-parsing a second analyzer run to get it was
    the reason this took text at all.
    """
    if env is None:
        return None
    for r in env.get("reports", []):
        if "montauk_analysis_storm_captured" in envelope_gauges(r):
            return bool(envelope_gauges(r)["montauk_analysis_storm_captured"])
    return None


def _parse_storm_report(text: str) -> dict:
    """Parse `montauk --analyze --report storm` output into a verdict dict. montauk --
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
                      schedulers="", all_scx=False, pandemonium=None,
                      only="") -> int:
    """cpu_release kick-storm reproducer -- the test that actually recreates the
    reboot live-lock (cold-wake measures cold caches, a different cost). Under
    powersave, stormwork drives the boot condition: a busy sched_ext population
    plus one SCHED_FIFO thread per CPU whose every wake yanks its CPU from scx
    (cpu_release -> reenqueue_local). montauk traces the flood and its StormReport
    (`montauk --analyze --report storm`) names the storm fraction and -- via fentry
    counters on scx_bpf_kick_cpu/reenqueue_local -- REAL IPI storm vs IDLE re-enqueue
    churn. EEVDF cannot storm (no Tier-0 kick loop), so the scored arms are the
    field's sched_ext arms: PANDEMONIUM's two unless a field flag names others."""
    src = Path(__file__).resolve().parent
    swbin = f"/tmp/stormwork-{stamp}"   # name kept so montauk's comm trace target is unchanged
    cc = subprocess.run(["gcc", "-O2", "-march=native", "-pthread", "-o", swbin,
                         str(src / "loadgen.c"), str(src / "loadgen_common.c")],
                        capture_output=True, text=True)
    if cc.returncode != 0:
        log_error(f"[storm] stormwork build failed: {cc.stderr.strip()}")
        return 1

    if schedulers or only or all_scx or pandemonium_common._ARMS:
        arms = [a for a in field_arms(n_cpus, schedulers, all_scx,
                                      pandemonium, only) if a[1] is not None]
    else:
        arms = pandemonium_arms(pandemonium)
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
            # The kick probes ARE the storm surface, so this capture asks the
            # shared launcher for them; without them the StormReport has no kick
            # counters to read and renders "not captured".
            log_info(f"[storm] {sched_name}: flooding {duration}s under montauk")
            with montauk_trace("stormwork", f"storm-{tag}", stamp, events=True,
                               scx_storm=True) as rec:
                subprocess.run(["sudo", swbin, "storm", str(duration),
                                str(busy_per_cpu), str(sleep_us), str(spin_us)],
                               capture_output=True, text=True)
            trace = rec.events_path
            guard.stop()
            wait_for_deactivation(5.0)
            # BOTH FACES: montauk_report returns the text and the envelope, one
            # analyzer pass each, because --json replaces the text rather than
            # adding to it. The verdict's interval counts, reenqueue p50/peak and
            # preempt rate are text-only in montauk's StormReport, so
            # _parse_storm_report reads the text until montauk emits them as
            # gauges.
            _storm_text, _storm_env = montauk_report(trace, "storm")
            # v7.17.0: query the captured bit as structured data and stamp it
            # into the artifact NOW -- the trace is deleted below, so this is
            # the only moment absence-vs-zero can be recorded. An unmeasured
            # storm surface must never fossilize as an empty .storm file.
            cap = _storm_captured_env(_storm_env)
            body = _storm_text
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


PCPU_TESTS = ("burst", "steal", "sojourn", "pcpu_starve")
# THE NUMBERS A TEST REPORTS: (path into its result, report label, unit, gauge)
PCPU_READINGS = {
    "burst": ((("max_delay_s",), "max_delay", "s", "burst_max_delay_s"),
              (("burst_latency", "p99_us"), "burst_p99", "us", "burst_p99_us")),
    "steal": ((("ratio",), "ratio", "x", "steal_ratio"),
              (("asymmetric", "p99_us"), "asym_p99", "us", "steal_asym_p99_us")),
    "sojourn": ((("max_delay_s",), "max_delay", "s", "sojourn_max_delay_s"),
                (("p99_delay_s",), "p99_delay", "s", "sojourn_p99_delay_s")),
    "pcpu_starve": ((("emitter_elapsed_s",), "emitter_elapsed", "s", "starve_elapsed_s"),),
}


def _pcpu_reading(results: list[dict], path: tuple) -> float | None:
    """The median of one reading over a test's iterations, a skipped one left
    out; None when no iteration carries it."""
    vals = []
    for r in results:
        v = r
        for key in path:
            v = v.get(key) if isinstance(v, dict) else None
        if isinstance(v, (int, float)) and not r.get("skip"):
            vals.append(float(v))
    return median(vals) if vals else None


def _pcpu_passed(r: dict) -> bool:
    return bool(r.get("pass", False) or r.get("skip", False))


def format_pcpu_report(version, git, iterations, all_results) -> tuple[str, bool]:
    """(report text, whether every arm passed every test at every width)."""
    lines = ["PANDEMONIUM PCPU",
             f"VERSION:     {version}",
             f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
             f"ITERATIONS:  {iterations}",
             ""]
    overall = bool(all_results)
    for nr_cpus in sorted(all_results):
        lines.append(f"[{nr_cpus} CORES]")
        for sched_name, runs in all_results[nr_cpus].items():
            lines.append(sched_name)
            for test in PCPU_TESTS:
                results = runs[test]
                passes = sum(1 for r in results if _pcpu_passed(r))
                ok = bool(results) and passes == len(results)
                overall = overall and ok
                detail = []
                for path, label, unit, _gauge in PCPU_READINGS[test]:
                    v = _pcpu_reading(results, path)
                    if v is not None:
                        detail.append(f"{label}={v:g}{unit}")
                lines.append(f"  {test:<14} {'PASS' if ok else 'FAIL'} "
                             f"({passes}/{len(results)})  {' '.join(detail)}")
        lines.append("")
    lines.append(f"OVERALL: {'PASS' if overall else 'FAIL'}")
    return "\n".join(lines) + "\n", overall


def write_pcpu_prometheus(version, git, stamp, all_results) -> Path:
    pb = PrometheusBuilder("pcpu")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    for nr_cpus in sorted(all_results):
        for sched_name, runs in all_results[nr_cpus].items():
            for test in PCPU_TESTS:
                results = runs[test]
                lbl = {"scheduler": sched_name, "cores": str(nr_cpus), "test": test}
                pb.gauge("iterations", len(results),
                         help="iterations the test ran", labels=lbl)
                pb.gauge("passed", sum(1 for r in results if _pcpu_passed(r)),
                         help="iterations the test passed or skipped", labels=lbl)
                for path, _label, _unit, gauge in PCPU_READINGS[test]:
                    v = _pcpu_reading(results, path)
                    if v is not None:
                        pb.gauge(gauge, v, help="median over the test's iterations",
                                 labels=lbl)
    path = LOG_DIR / f"prism-pcpu-{stamp}.prom"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(pb.render())
    return path


def cmd_bench_pcpu(args) -> int:
    """Per-CPU DSQ visibility stress test, every arm of the field.

    Four tests attack per-CPU DSQ reachability:
      burst-starvation: Reproduces the CS2 starvation scenario
      work-stealing:    Tests L2 work stealing under asymmetric load
      sojourn-ceiling:  Tests per-CPU sojourn rescue under full saturation
      pcpu-starvation:  Regression test for v5.6.0 watchdog crashes
                        (kworker/0:2, systemd-journal[430]). Pins a
                        never-yielding CPU hog and a high-wakeup emitter
                        to the same CPU and asserts the emitter runs.

    Requires sudo. Sweeps the suite's widths by default.
    """
    entries = field_of(args)
    if not entries:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is installed")
        return 2
    warm_sudo()
    if fields_pandemonium(entries):
        nuke_stale_build()
        if not build():
            return 1
    if not clear_loaded_scheduler():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.cores:
        core_counts = [int(c.strip()) for c in args.cores.split(",")]
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
                                       getattr(args, "pandemonium", None),
                                       getattr(args, "only", ""))
        finally:
            restore_all_cpus(max_cpus)
        return rc

    log_info(f"prism-pcpu v{version} [{git['commit']}{' (dirty)' if git['dirty'] else ''}]")
    log_info(f"Schedulers: {', '.join(name for name, _ in entries)}")
    log_info(f"Core counts: {core_counts}")
    log_info(f"Iterations: {args.iterations}")

    # all_results[cores][scheduler][test] = ONE RESULT AN ITERATION
    all_results: dict = {}
    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] Starting per-CPU DSQ tests")

            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] Failed to restrict CPUs")
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(2)

            all_results[nr_cpus] = {}
            steps = (
                ("burst", lambda: measure_burst_starvation(
                    probe_tool(), nr_cpus, burst_size=max(nr_cpus * 20, 100))),
                ("steal", lambda: measure_work_stealing(probe_tool(), nr_cpus)),
                ("sojourn", lambda: measure_sojourn_ceiling(probe_tool(), nr_cpus)),
                ("pcpu_starve", lambda: measure_pcpu_starvation(nr_cpus)),
            )
            for sched_name, sched_cmd in entries_for_cores(entries, nr_cpus):
                runs: dict = {test: [] for test in PCPU_TESTS}
                all_results[nr_cpus][sched_name] = runs

                for iteration in range(1, args.iterations + 1):
                    log_info(f"[{nr_cpus}C] {sched_name} iteration "
                             f"{iteration}/{args.iterations}")

                    guard = None
                    if sched_cmd is not None:
                        guard = start_and_wait(sched_cmd, sched_name, settle_secs=3)
                        if guard is None:
                            log_error(f"[{nr_cpus}C] {sched_name} failed to start")
                            for test in PCPU_TESTS:
                                runs[test].append({"survived": False, "pass": False})
                            continue

                    dmesg = DmesgMonitor()
                    try:
                        for i, (test, fn) in enumerate(steps):
                            if i:
                                time.sleep(3)
                            r = fn()
                            runs[test].append(r)
                            dmesg.check()
                            if dmesg.crashed or sched_ejected(guard):
                                log_error(f"[{nr_cpus}C] {sched_name} did not survive "
                                          f"the {test} test")
                                r["survived"] = False
                                r["pass"] = False
                                r.pop("skip", None)
                                break
                    finally:
                        dmesg.save()
                        stop_and_wait(guard)
                        time.sleep(2)

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
    finally:
        restore_all_cpus(max_cpus)

    report, overall_pass = format_pcpu_report(version, git, args.iterations, all_results)
    print()
    log.report(report)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LOG_DIR / f"prism-pcpu-{stamp}.log"
    report_path.write_text(report)
    log_info(f"REPORT: {report_path}")
    log_info(f"METRICS: {write_pcpu_prometheus(version, git, stamp, all_results)}")

    return 0 if overall_pass else 1


# BENCH-SCALE COMMAND

def cmd_bench_scale(args) -> int:
    """Unified benchmark: throughput + latency at each core count."""

    warm_sudo()

    dmesg = DmesgMonitor()

    base_entries = field_of(args)
    if args.pandemonium:
        log_info(f"PANDEMONIUM {args.pandemonium.upper()}: "
                 f"{', '.join(n for n, _ in base_entries)}")
    if not base_entries:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is installed")
        return 2

    # THE TREE IS BUILT WHATEVER THE FIELD: THE IPC CELL's WORKLOAD IS THIS
    # BINARY AND THE COMPILE CELL's IS THIS BUILD.
    nuke_stale_build()

    if not build():
        return 1

    if not clear_loaded_scheduler():
        return 1

    # Restore all CPUs (previous run may have left some offline)
    possible = get_possible_cpus()
    restore_all_cpus(possible)
    time.sleep(0.5)

    # Pre-flight, when PANDEMONIUM is fielded: verify it can load BPF and activate
    if fields_pandemonium(base_entries):
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

    # Workload
    workload_cmd = args.cmd or f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
    clean_cmd = args.clean_cmd
    if not args.cmd:
        clean_cmd = f"cargo clean --target-dir {TARGET_DIR}"

    # Core counts (use possible, not online -- previous crash may have left CPUs offline)
    max_cpus = get_possible_cpus()
    if args.cores:
        core_counts = [int(c.strip()) for c in args.cores.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        if max_cpus not in core_counts:
            core_counts.append(max_cpus)
        core_counts.sort()
    else:
        core_counts = compute_core_counts(max_cpus)

    try:
        cells = scale_cells(args)
    except ValueError as e:
        log_error(str(e))
        return 2
    single = cells[0] if cells and len(cells) == 1 else None

    def want(cell: str) -> bool:
        return cells is None or cell in cells

    print()
    log_info(f"Schedulers: {', '.join(name for name, _ in base_entries)}")
    log_info(f"Core counts: {core_counts}")
    if single == "deadline":
        log_info("Mode: DEADLINE ONLY (periodic frame jitter)")
    elif single == "ipc":
        log_info("Mode: IPC ONLY (pipe round-trip latency)")
    elif single == "launch":
        log_info("Mode: LAUNCH ONLY (fork+exec latency)")
    elif single == "mixed":
        log_info("Mode: MIXED ONLY (burst + long-run combined)")
    elif single == "longrun":
        log_info("Mode: LONG-RUN ONLY (skipping latency, throughput, burst)")
    elif single == "burst":
        log_info("Mode: BURST ONLY (skipping latency + throughput)")
    elif single == "compile":
        log_info(f"Mode: COMPILE ONLY ({workload_cmd})")
    elif single == "latency":
        log_info("Mode: LATENCY ONLY (the probe under one stress worker a CPU)")
    elif cells:
        log_info(f"Mode: CELLS ({', '.join(cells)})")
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
        "cells": list(cells) if cells and not single else None,
        "burst_only": single == "burst",
        "longrun_only": single == "longrun",
        "mixed_only": single == "mixed",
        "deadline_only": single == "deadline",
        "ipc_only": single == "ipc",
        "launch_only": single == "launch",
        "compile_only": single == "compile",
        "latency_only": single == "latency",
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

                # THE PACKAGE TEMPERATURE AT THE ARM's START AND END. THE BOX
                # READS 96 C UNDER A FULL 12-THREAD LOAD, AND A CELL THAT RAN
                # HOT IS READ WITH THAT BESIDE IT.
                sched_result["thermal"] = {"start_c": cpu_max_c()}

                # EVERY PHASE REPEATS UNDER --iterations AND POOLS ITS SAMPLES
                # THE WAY THE LATENCY PHASE DOES. ONE DRAW CANNOT SHOW A HELD
                # LATENCY, AND A CELL THAT READ 60us IN ONE RUN AND 959us IN
                # THE NEXT WAS ONE DRAW EACH TIME.
                _iters = max(1, getattr(args, "iterations", 1))

                def repeat_phase(label, fn):
                    runs = []
                    for _it in range(_iters):
                        if _iters > 1:
                            log_info(f"{label} iteration {_it + 1}/{_iters} "
                                     f"[{sched_name}] {n}C")
                        r = fn()
                        if sched_ejected(guard):
                            r["survived"] = False
                            log_error(f"{sched_name} CRASHED during {label} "
                                      f"(exit {guard.proc.returncode})")
                            runs.append(r)
                            break
                        runs.append(r)
                    return pool_phase_runs(runs)

                # --trace WITH A PROBE CELL NAMED RECORDS ONE DRAW OF IT AHEAD OF
                # THE TIMED ONES. THE FULL PASS RECORDS NONE: THREE CAPTURES AN
                # ARM AT EVERY WIDTH IS TENS OF GIGABYTES OF TRACE DIRECTORY.
                def record_probe(cell):
                    if not (cells is not None and getattr(args, "trace", False)
                            and montauk_available()):
                        return
                    draw = traced_probe_draw(cell, sched_name, n, stamp)
                    if draw:
                        sched_result.setdefault("probe_trace", {})[cell] = draw

                if want("latency"):
                    # Latency measurement
                    record_probe("latency")
                    latency = measure_latency(probe_tool(), n,
                                              iterations=args.iterations)
                    sched_result["latency"] = latency

                if want("compile"):
                    # Throughput measurement. Under --trace with the cell named,
                    # the FIRST build of each arm records under montauk, the build's whole
                    # process tree with the DSQ probes armed, so where the compile
                    # spends its time can be read against EEVDF's on the same
                    # width. The traced build's wall carries the tracing tax and
                    # is withheld from the timing; the repeats time clean.
                    times = []
                    _traced = (cells is not None and getattr(args, "trace", False)
                               and montauk_available())
                    for i in range(args.iterations + (1 if _traced else 0)):
                        if _traced and i == 0:
                            safe = (sched_name.replace(" ", "-")
                                    .replace("(", "").replace(")", ""))
                            with montauk_trace("cargo", f"compile-{safe}-{n}c",
                                               stamp, events=True, scx_dsq=True,
                                               sched_detail=True,
                                               trace_classes=COMPILE_TRACE_CLASSES
                                               ) as _rec:
                                timed_run(workload_cmd, clean_cmd)
                            log_info(f"[{sched_name}] {n}C compile montauk trace "
                                     f"-> {_rec.dir}")
                            continue
                        log_info(f"Throughput iteration "
                                 f"{i + (0 if _traced else 1)}/{args.iterations}")
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

                if want("burst"):
                    # Burst measurement (app launch under full load)
                    burst_size = n * 4
                    if burst_size < 8:
                        burst_size = 8
                    sched_result["burst"] = repeat_phase(
                        "burst", lambda: measure_burst(probe_tool(), n, burst_size))

                if want("longrun"):
                    # Long-running process test
                    record_probe("longrun")
                    longrun_count = max(4, n // 2)
                    sched_result["longrun"] = repeat_phase(
                        "long-run", lambda: measure_longrun(
                            probe_tool(), n, longrun_count=longrun_count))

                if want("mixed"):
                    # Mixed workload test (burst + longrun combined)
                    record_probe("mixed")
                    mixed_burst_size = n * 4
                    if mixed_burst_size < 8:
                        mixed_burst_size = 8
                    sched_result["mixed"] = repeat_phase(
                        "mixed", lambda: measure_mixed(
                            probe_tool(), n, longrun_count=max(4, n // 2),
                            burst_size=mixed_burst_size))

                if want("deadline"):
                    # Periodic deadline (frame scheduling jitter)
                    sched_result["deadline"] = repeat_phase(
                        "deadline", lambda: measure_deadline(probe_tool(), n))

                _ipc_rec = None
                if want("ipc"):
                    # IPC round-trip (pipe ping-pong). Under --trace, wrap the
                    # measurement in a montauk pand-ipc recording with the raw
                    # per-event log (events=True) -- the only instrument fine
                    # enough to resolve a single RTT landing on the CONFIG_HZ tick
                    # floor. The IPC engine is one clean handoff pair per primitive
                    # (cores unsaturated), so montauk needs no pinned drain core.
                    # --iterations REPEATS THE CELL. It used to be accepted and
                    # ignored on every single-mode branch, so every IPC number
                    # ever published was a single draw against a standing
                    # constraint that demands N>=2. Under --trace only the FIRST
                    # iteration records: a capture is ~500MB and the repeats
                    # exist for the distribution, not for N more event streams.
                    _iters = max(1, getattr(args, "iterations", 1))
                    _runs = []
                    for _i in range(_iters):
                        if _iters > 1:
                            log_info(f"IPC iteration {_i + 1}/{_iters} "
                                     f"[{sched_name}] {n}C")
                        if (_i == 0 and getattr(args, "trace", False)
                                and montauk_available()):
                            safe = (sched_name.replace(" ", "-")
                                    .replace("(", "").replace(")", ""))
                            with montauk_trace(IPC_COMM, f"ipc-{safe}-{n}c",
                                               stamp, events=True,
                                               sched_detail=True) as _rec:
                                _r = measure_ipc(BINARY, n)
                            _ipc_rec = _rec
                            _r["events"] = (str(_rec.events_path)
                                            if _rec.events_path else None)
                            log_info(f"[{sched_name}] {n}C IPC montauk trace -> "
                                     f"{_rec.dir}")
                        else:
                            _r = measure_ipc(BINARY, n)
                        _runs.append(_r)
                    ipc_result = reduce_ipc_runs(_runs)
                    # THE RECORDING WAS BEING WRITTEN AND READ BY NOTHING. Fold the
                    # scheduler-side half of the hop in beside the RTT so the table
                    # can say WHERE the microseconds go, not only how many.
                    _hop = ipc_hop_from_recording(_ipc_rec)
                    _hop.update(ipc_contention_from_recording(_ipc_rec))
                    _hop.update(ipc_pair_from_recording(_ipc_rec))
                    if _hop and _runs:
                        # SAME SAMPLE OR NO SAMPLE. wake2run comes from the
                        # recording, and only the FIRST iteration records -- so the
                        # half-RTT it is differenced against has to be that same
                        # iteration's, not the median of all of them. Subtracting a
                        # single draw from a 3-run median is two samples wearing one
                        # subtraction sign.
                        _p50_0 = ((_runs[0].get("cell") or {})
                                  .get("rtt_pipe") or {}).get("p50")
                        if _p50_0:
                            _hop["hop_pipe_p50_us"] = float(_p50_0)
                    ipc_result.update(_hop)
                    if sched_ejected(guard):
                        ipc_result["survived"] = False
                        log_error(f"{sched_name} CRASHED during IPC test "
                                  f"(exit {guard.proc.returncode})")
                    sched_result["ipc"] = ipc_result

                if want("launch"):
                    # Application launch under load. Under --trace one extra
                    # draw records ahead of the timed ones: the harness that
                    # forks each launch, the launches, the stress workers and
                    # the scheduler as one tree, so a slow launch is read by
                    # the wait that held it. Its timings carry the tracing tax
                    # and are withheld.
                    if getattr(args, "trace", False) and montauk_available():
                        safe = (sched_name.replace(" ", "-")
                                .replace("(", "").replace(")", ""))
                        with montauk_trace(LAUNCH_TRACE_PATTERN, f"launch-{safe}-{n}c",
                                           stamp, events=True, sched_detail=True,
                                           trace_classes=PROBE_TRACE_CLASSES) as _rec:
                            measure_launch(probe_tool(), n)
                        log_info(f"[{sched_name}] {n}C launch montauk trace -> "
                                 f"{_rec.dir}")
                    sched_result["launch"] = repeat_phase(
                        "launch", lambda: measure_launch(probe_tool(), n))

                sched_result["thermal"]["end_c"] = cpu_max_c()

                # Stop scheduler, capture telemetry
                stdout = stop_and_wait(guard)
                if guard is not None:
                    sched_result["scx_events"] = dict(guard.events)
                if stdout and "PANDEMONIUM" in sched_name:
                    ticks = parse_tick_lines(stdout)
                    knobs = parse_knobs_line(stdout)
                    # The IPC capture uses montauk_trace (not trace_workload), so the
                    # generic marker never ran -- write the cross-domain + migration
                    # marker into its recording dir from the drained shutdown stdout.
                    if _ipc_rec is not None:
                        _write_cross_domain_marker_text(stdout, _ipc_rec.dir)
                    # The chaos/graph summary must survive a run with NO recording
                    # dir. _ipc_rec is only set under --trace, so on a plain
                    # `--dev ipc` there was nowhere to put it and the coupling
                    # spread went unread even though the telemetry was in stdout
                    # the whole time. Fall back to the archive, which is where
                    # every other durable per-run artifact already lives.
                    _chaos_dst = _ipc_rec.dir if _ipc_rec is not None else None
                    if _chaos_dst is None:
                        _sn = sched_name.split()[-1].strip("()")
                        _chaos_dst = (ARCHIVE_DIR / "chaos" /
                                      f"{_sn}-{n}c-{stamp}.prom")
                    _write_chaos_markers_text(stdout, _chaos_dst)

                    sched_result["telemetry"] = {
                        "tick_count": len(ticks),
                        "tick_aggregate": aggregate_ticks(ticks),
                        "knobs": knobs,
                    }

                # COMPREHENSIVE IPC: fold montauk's deep analysis of the capture
                # (wake2run, cross-CCX locality, holder, fractal) into the result.
                # A named ipc cell only; the full pass leaves the capture untouched.
                if cells is not None and "ipc" in cells:
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
    report_path = LOG_DIR / f"{bench_slug(data)}-{stamp}.log"
    report_path.write_text(report)
    log_info(f"REPORT: {report_path}")

    # Write Prometheus metrics
    prom_path = write_prometheus(data, stamp)
    log_info(f"METRICS: {prom_path}")

    # Dmesg
    dmesg.save(stamp)

    fix_ownership()

    if not data["results"]:
        return 1
    return 0


# THE PROBE CELLS, RECORDED (prism-scale --trace)
#
# scale's latency, longrun and mixed cells carry the probe under a saturated
# box, and a capture of them needs what their measurement does not: One CPU
# left to montauk so the capture keeps up, the per-switch stream on so a floored
# wake can be attributed, and the probe, the stress workers and the long-runners
# traced as one tree. Under --trace a named probe cell takes one draw ahead of
# its timed ones, on every CPU but the drain core, and the probe's wakes are
# read back through montauk by pid. The draw's timings carry the tracing tax
# and are reported apart from the cell's own.

PROBE_TRACE_PATTERN = "pand-probe,pand-stress,pandemonium,python3"
PROBE_TRACE_CLASSES = "sched,fork,exec,exit,comm"
# THE LAUNCH CELL's TREE: THE HARNESS FORKS EACH LAUNCH, SO python3 CARRIES THEM.
LAUNCH_TRACE_PATTERN = "pand-stress,pandemonium,python3"
# A COMPILE IS THOUSANDS OF SHORT rustc AND cc PROCESSES AND A FLOOD OF FILE IO.
# THE QUESTION IS WHERE ITS CPU TIME WAITS, SO ONLY THE SCHEDULER AND THE
# PROCESS-TREE CLASSES ARE RESERVED.
COMPILE_TRACE_CLASSES = "sched,fork,exec,exit,comm"
PROBE_CELLS = ("latency", "longrun", "mixed")
# (reading key, report label, cell format)
PROBE_ROWS = [
    ("probe_p50_us",           "PROBE p50 us",          "{:.0f}"),
    ("probe_p99_us",           "PROBE p99 us",          "{:.0f}"),
    ("probe_worst_us",         "PROBE WORST us",        "{:.0f}"),
    ("wake2run_p50_us",        "WAKE2RUN p50 us",       "{:.1f}"),
    ("wake2run_p99_us",        "WAKE2RUN p99 us",       "{:.0f}"),
    ("wake2run_p999_us",       "WAKE2RUN p99.9 us",     "{:.0f}"),
    ("wake2run_tickfloor_pct", "TICK-FLOOR WAKES %",    "{:.2f}"),
    ("preempt_starved_pct",    "PREEMPT-STARVED %",     "{:.0f}"),
    ("order_starved_pct",      "ORDER-STARVED %",       "{:.0f}"),
    ("passover_p99",           "PASS-OVERS p99",        "{:.0f}"),
    ("dark_pct",               "DARK %",                "{:.0f}"),
    ("migration_rate_hz",      "MIGRATIONS/s, BOX",     "{:.0f}"),
    ("cross_l3_pct",           "CROSS-L3 MOVES %",      "{:.1f}"),
    ("work_total",             "LONG-RUN WORK",         "{:.0f}"),
    ("cpu_max_c_start",        "CPU MAX C, START",      "{:.0f}"),
    ("cpu_max_c_end",          "CPU MAX C, END",        "{:.0f}"),
    ("outsider_cpu_pct",       "BOX OUTSIDER CPU %",    "{:.0f}"),
    ("completeness",           "CAPTURE COMPLETENESS",  "{:.3f}"),
]


def _probe_readings(events: Path | None, probe_pid: int | None) -> dict:
    """montauk's reading of one capture: The probe's wake-to-run and the
    attribution of its floored wakes, narrowed to its pid, and the box's
    migration rate and locality over the whole stream."""
    out: dict = {}
    if events is None or not Path(events).is_file():
        return out
    whole = montauk_envelope(events)
    narrow = montauk_envelope(events, report="sched,dispatch-stall", pid=probe_pid) if probe_pid else whole
    if whole:
        out.update(envelope_capture(whole))
        loc = envelope_report(whole, "locality")
        lg = envelope_gauges(loc)
        if "montauk_analysis_locality_migration_rate_hz" in lg:
            out["migration_rate_hz"] = float(
                lg["montauk_analysis_locality_migration_rate_hz"])
        if "montauk_analysis_locality_local_pct" in lg:
            out["local_pct"] = float(lg["montauk_analysis_locality_local_pct"])
        tiers = envelope_gauge_by_label(
            loc, "montauk_analysis_locality_tier_moves", "tier")
        total = sum(float(v) for v in tiers.values()) if tiers else 0.0
        if total > 0:
            far = sum(float(v) for k, v in tiers.items()
                      if k in ("same_socket", "cross_socket"))
            out["cross_l3_pct"] = 100.0 * far / total
        out["locality_class"], out["locality_verdict"] = envelope_verdict(loc)
    if narrow:
        w = envelope_report(narrow, "sched").get("wake2run") or {}
        for k in ("count", "p50_us", "p99_us", "p999_us", "worst_us",
                  "tickfloor_pct"):
            if w.get(k) is not None:
                out[f"wake2run_{k}"] = float(w[k])
        stall = envelope_report(narrow, "dispatch-stall")
        out["stall_class"], out["stall_verdict"] = envelope_verdict(stall)
        sg = envelope_gauges(stall)
        for g, k in (("montauk_analysis_dispatch_preempt_pct", "preempt_starved_pct"),
                     ("montauk_analysis_dispatch_order_pct", "order_starved_pct"),
                     ("montauk_analysis_dispatch_dark_pct", "dark_pct"),
                     ("montauk_analysis_dispatch_held_pct", "held_pct"),
                     ("montauk_analysis_dispatch_avg_passovers", "passovers_avg"),
                     ("montauk_analysis_dispatch_passover_p99", "passover_p99")):
            if g in sg:
                out[k] = float(sg[g])
    return out


def traced_probe_draw(cell: str, sched_name: str, n: int, stamp: str) -> dict:
    """One recorded draw of a probe cell at width n: The load on every online
    CPU but the last, montauk alone on the last. {} when the width leaves no
    drain core."""
    online = sorted(os.sched_getaffinity(0))
    if len(online) < 3:
        log_info(f"[{sched_name}] {n}C {cell}: not recorded, a capture takes two "
                 f"CPUs for the load and one for montauk")
        return {}
    # THE DRAIN CORE IS EXCLUSIVE. Under a saturated load an unpinned montauk is
    # starved by its own subject and drops the windows worth measuring, and a
    # montauk pinned to a CPU the load also covers contends with it there.
    drain, load = online[-1], len(online) - 1
    safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
    c_start = cpu_max_c()
    out_cpu, out_names = box_outsiders()
    log_info(f"[{sched_name}] {n}C {cell}: recording one draw, load on {load} CPUs, "
             f"montauk on cpu{drain}" + (f" (beside {out_names})" if out_names else ""))
    # EVERYTHING THE DRAW SPAWNS INHERITS THE LOAD MASK; montauk's taskset SETS
    # ITS OWN. THE KICK AND DSQ PROBES ARE ARMED: A FLOORED WAKE's QUESTION IS
    # WHICH QUEUE IT SAT ON AND WHETHER ITS KICK WAS ISSUED AND ANSWERED.
    os.sched_setaffinity(0, set(online[:-1]))
    try:
        with montauk_trace(PROBE_TRACE_PATTERN, f"probe-{cell}-{safe}-{n}c", stamp,
                           events=True, pin_cpu=drain, sched_detail=True,
                           scx_storm=True, scx_dsq=True,
                           trace_classes=PROBE_TRACE_CLASSES) as rec:
            if cell == "latency":
                res = measure_latency(probe_tool(), load, iterations=1)
            elif cell == "longrun":
                res = measure_longrun(probe_tool(), load,
                                      longrun_count=max(4, load // 2))
            else:
                res = measure_mixed(probe_tool(), load,
                                    longrun_count=max(4, load // 2),
                                    burst_size=max(8, load * 4))
    finally:
        os.sched_setaffinity(0, set(online))
    lat = res.get("latency") or res
    draw: dict = {
        "probe_p50_us": float(lat.get("median_us", 0)),
        "probe_p99_us": float(lat.get("p99_us", 0)),
        "probe_worst_us": float(lat.get("worst_us", 0)),
        "work_total": float(res.get("work_total", 0)),
        "outsider_cpu_pct": out_cpu,
    }
    c_end = cpu_max_c()
    if c_start is not None:
        draw["cpu_max_c_start"] = c_start
    if c_end is not None:
        draw["cpu_max_c_end"] = c_end
    draw.update(_probe_readings(rec.events_path, res.get("probe_pid")))
    w99 = draw.get("wake2run_p99_us")
    log_info(f"[{sched_name}] {n}C {cell}: recorded probe p99 {draw['probe_p99_us']:.0f}us"
             + (f", wake2run p99 {w99:.0f}us" if w99 is not None else "")
             + f" -> {rec.dir}")
    return draw


def format_probe_trace(results: dict) -> list[str]:
    """The recorded draws of scale's probe cells, a table a cell at each width:
    montauk's reading of each arm's capture."""
    lines: list[str] = []
    for cores_str in sorted(results, key=int):
        schedulers = results[cores_str]
        for cell in PROBE_CELLS:
            arms = [s for s, d in schedulers.items()
                    if (d.get("probe_trace") or {}).get(cell)]
            if not arms:
                continue
            draws = {s: schedulers[s]["probe_trace"][cell] for s in arms}
            lines.append(f"RECORDED {cell.upper()}, {cores_str}C "
                         f"(TRACED: NOT COMPARABLE TO THE TIMED CELL)")
            lines.append(table_header("METRIC", arms, 26, 20))
            for key, label, fmt in PROBE_ROWS:
                cols = [fmt.format(draws[s][key]) if key in draws[s] else "-"
                        for s in arms]
                if all(c == "-" for c in cols):
                    continue
                lines.append(table_row(label, cols, 26, 20))
            # montauk's own verdicts, verbatim: It is the instrument.
            for s in arms:
                if draws[s].get("stall_verdict"):
                    lines.append(f"dispatch-stall, probe, {s}: {draws[s]['stall_verdict']}")
                if draws[s].get("locality_verdict"):
                    lines.append(f"locality, box, {s}: {draws[s]['locality_verdict']}")
            lines.append("")
    return lines


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
            [str(probe_tool()), "probe"],
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
        note_scheduler_activated()
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
    """Sustained long-runners + interactive probe."""
    n_runners = max(2, nr_cpus // 2)
    log_info(f"PHASE: longrun-interactive ({n_runners} runners + probe, {duration}s)")

    runners = _LongRunners(n_runners)
    runners.start(duration)

    # LET THE LONG-RUNNERS SATURATE THEIR CPUS BEFORE THE PROBE
    time.sleep(3)
    if dmesg.check() or not sched_alive_fn():
        runners.collect(timeout=5)
        return {"survived": False, "samples": 0}

    # INTERACTIVE PROBE UNDER SUSTAINED LONG-RUNNERS
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

CONTENTION_PHASES = {
    "regime-sweep": _contention_phase_regime_sweep,
    "deficit-storm": _contention_phase_deficit_storm,
    "sojourn-pressure": _contention_phase_sojourn_pressure,
    "longrun-interactive": _contention_phase_longrun_interactive,
    "burst-recovery": _contention_phase_burst_recovery,
    "mixed-storm": _contention_phase_mixed_storm,
}
# THE PHASES A CAPTURE CAN BE CAPPED TO: THE TWO THAT TAKE A DURATION AND
# CARRY THE LATENCY PROBE THROUGH ALL OF IT.
CONTENTION_TRACED = ("deficit-storm", "sojourn-pressure")
# THE NUMBERS A PHASE REPORTS: (result key, report label, format)
CONTENTION_ROWS = (
    ("p99_us", "P99", "{:.0f}us"),
    ("median_us", "med", "{:.0f}us"),
    ("samples", "n", "{:.0f}"),
    ("work_min", "work_min", "{:.0f}"),
    ("work_max", "work_max", "{:.0f}"),
    ("fairness", "fair", "{:.2f}"),
    ("baseline_p99_us", "base", "{:.0f}us"),
    ("burst_p99_us", "burst", "{:.0f}us"),
    ("recovery_p99_us", "recov", "{:.0f}us"),
    ("deadline_misses", "dl_miss", "{:.0f}"),
    ("deadline_total", "dl_total", "{:.0f}"),
    ("deadline_miss_ratio", "dl_ratio", "{:.4f}"),
)


def _contention_run_iteration(label, nr_cpus, sched_name, sched_cmd, phases):
    """One arm through the phases named, in order, stopping at the first it
    does not survive: (survived, {phase: result}). EEVDF has no command and
    nothing to eject, so under it a phase fails only on a dmesg fault."""
    phase_results: dict = {}
    guard = None
    if sched_cmd is not None:
        guard = start_and_wait(sched_cmd, sched_name, settle_secs=3.0)
        if guard is None:
            return False, phase_results

    dmesg = DmesgMonitor()

    def sched_alive():
        return not sched_ejected(guard)

    log_info(f"{label}{sched_name}: contention sequence at {nr_cpus}C")
    survived = True
    for name in phases:
        result = CONTENTION_PHASES[name](nr_cpus, dmesg, sched_alive)
        phase_results[name] = result
        if not result.get("survived", False):
            survived = False
            if dmesg.crashed:
                log_error(f"{label}{sched_name}: CRASH DETECTED during '{name}': "
                          f"{dmesg.crash_msg}")
            else:
                log_error(f"{label}{sched_name}: did not survive '{name}' "
                          f"(no dmesg crash)")
            break
        log_info(f"  '{name}' passed")
    else:
        log_info(f"{label}{sched_name}: every phase complete")

    stop_and_wait(guard)
    dmesg.save()

    return survived, phase_results


def _contention_reading(runs: list[dict], phase: str, key: str) -> float | None:
    """The median of one phase reading over the iterations that carry it."""
    vals = []
    for r in runs:
        v = (r.get(phase) or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            vals.append(float(v))
    return median(vals) if vals else None


def format_contention_report(ver, git, iterations, max_cpus, results) -> tuple[str, int]:
    """(report text, iterations that did not survive, over every arm)."""
    lines = ["PANDEMONIUM CONTENTION",
             f"VERSION:     {ver}",
             f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
             f"ITERATIONS:  {iterations}",
             f"MAX CPUS:    {max_cpus}",
             ""]
    crashed_total = 0
    for nr_cpus in sorted(results):
        lines.append(f"[{nr_cpus} CORES]")
        for sched_name, arm in results[nr_cpus].items():
            crashed_total += arm["crashed"]
            n = arm["survived"] + arm["crashed"]
            lines.append(f"{sched_name}: {arm['survived']}/{n} survived  "
                         f"{'PASS' if n and not arm['crashed'] else 'FAIL'}")
            for phase in CONTENTION_PHASES:
                reached = [r[phase] for r in arm["runs"] if phase in r]
                if not reached:
                    continue
                ok = sum(1 for r in reached if r.get("survived"))
                detail = []
                for key, label, fmt in CONTENTION_ROWS:
                    v = _contention_reading(arm["runs"], phase, key)
                    if v is not None:
                        detail.append(f"{label}={fmt.format(v)}")
                lines.append(f"  {phase:<20} {ok}/{len(reached)} survived  "
                             f"{'  '.join(detail)}")
        lines.append("")
    lines.append(f"ITERATIONS NOT SURVIVED: {crashed_total}")
    return "\n".join(lines) + "\n", crashed_total


def write_contention_prometheus(ver, git, stamp, max_cpus, iterations, results) -> Path:
    pb = PrometheusBuilder("contention")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=ver, git_commit=git["commit"], git_dirty=git["dirty"])
    pb.gauge("iterations", iterations, help="Iterations per arm at each width")
    pb.gauge("max_cpus", max_cpus, help="Maximum CPUs available")
    for nr_cpus in sorted(results):
        for sched_name, arm in results[nr_cpus].items():
            al = {"scheduler": sched_name, "cores": str(nr_cpus)}
            pb.gauge("survived", arm["survived"], help="Iterations survived", labels=al)
            pb.gauge("crashed", arm["crashed"], help="Iterations not survived", labels=al)
            for phase in CONTENTION_PHASES:
                reached = [r[phase] for r in arm["runs"] if phase in r]
                if not reached:
                    continue
                pl = {**al, "phase": phase}
                pb.gauge("phase_iterations", len(reached),
                         help="Iterations that reached the phase", labels=pl)
                pb.gauge("phase_survived", sum(1 for r in reached if r.get("survived")),
                         help="Iterations that survived the phase", labels=pl)
                if any("recovery_within_2x" in r for r in reached):
                    pb.gauge("phase_recovery_ok",
                             sum(1 for r in reached if r.get("recovery_within_2x")),
                             help="Iterations whose recovery held within 2x baseline",
                             labels=pl)
                for key, _label, _fmt in CONTENTION_ROWS:
                    v = _contention_reading(arm["runs"], phase, key)
                    if v is not None:
                        pb.gauge(f"phase_{key}", v,
                                 help="median over the phase's iterations", labels=pl)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-contention-{stamp}.prom"
    path.write_text(pb.render())
    return path


def trace_contention_storm(stamp: str, n_cpus: int, duration: int,
                           schedulers: str = "", all_scx: bool = False,
                           pandemonium: str | None = None,
                           phase: str = "deficit-storm",
                           only: str = "") -> int:
    """One montauk-traced contention phase, hard-capped at `duration`s. Runs the
    requested phase -- deficit-storm (ncpu interactive + ncpu*2 batch) or
    sojourn-pressure (ncpu*4 deep batch flood + interactive probes) -- under each
    scheduler while montauk records the latency probe's wake-to-run (comm
    pand-cont): the batch workers create the runqueue pressure, the probe is the
    wakee we measure. montauk pins a drain core (cores are saturated)."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    phase_fn = CONTENTION_PHASES[phase]
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
    arms = field_arms(n_cpus, schedulers, all_scx, pandemonium, only)
    traced = 0
    for sched_name, activate_cmd in arms:
        # sched_detail for the same reason pcpu-burst takes it: the measured
        # quantity is how long a probe sat behind a deep batch flood, and the
        # DARK-vs-HELD split plus the pick-order attribution need CPU_IDLE and
        # PICK on the stream. The probe is one thread, so the extra volume is
        # bounded by the batch workers' switches, not by a fan-out.
        rec_dir, _ = trace_workload(sched_name, activate_cmd,
                                    "pand-cont", f"{phase}-{n_cpus}c", stamp, body,
                                    events=True, pin_cpu=drain,
                                    sched_detail=True)
        if rec_dir is None:
            log_error(f"[contention/{phase}] {sched_name} failed to activate -- skipped")
            continue
        log_info(f"[contention/{phase}] {sched_name} montauk recording -> {rec_dir}")
        traced += 1
    return 0 if traced else 1


def cmd_bench_contention(args) -> int:
    """Contention stress test, every arm of the field.

    6 phases per core count: regime-sweep, deficit-storm, sojourn-pressure,
    longrun-interactive, burst-recovery, mixed-storm. Each was written against
    one of PANDEMONIUM's adaptive mechanisms and runs the same load under any
    arm. --phase runs one of them.
    """
    phase_filter = getattr(args, "phase", None)
    if phase_filter and phase_filter not in CONTENTION_PHASES:
        log_error(f"--phase {phase_filter}: not a phase ({', '.join(CONTENTION_PHASES)})")
        return 2
    entries = field_of(args)
    if not entries:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is installed")
        return 2

    warm_sudo()
    if fields_pandemonium(entries):
        nuke_stale_build()
        if not build():
            return 1
    if not clear_loaded_scheduler():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.cores:
        core_counts = [int(c.strip()) for c in args.cores.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = compute_core_counts(max_cpus)

    if not core_counts:
        log_error(f"no valid core counts (host has {max_cpus} CPUs, minimum is 2)")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # --trace: capped montauk recordings of the contention phase at EVERY core
    # width (the suite's scaling), each its own capture, so the width-specific
    # fault is visible -- sojourn-pressure blows up at 8C, deficit-storm floors
    # at 2C. --phase picks which (default deficit-storm). restrict the online
    # CPUs per width like the matrix path, restore after.
    if getattr(args, "trace", False):
        trace_phase = phase_filter or "deficit-storm"
        if trace_phase not in CONTENTION_TRACED:
            log_error(f"--trace records {' or '.join(CONTENTION_TRACED)}, "
                      f"not {trace_phase}")
            return 2
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
                                             getattr(args, "pandemonium", None),
                                             phase=trace_phase,
                                             only=getattr(args, "only", ""))
        finally:
            restore_all_cpus(max_cpus)
        return rc

    ver = get_version()
    git = get_git_info()
    phases = [phase_filter] if phase_filter else list(CONTENTION_PHASES)
    log_info(f"prism-contention v{ver} [{git['commit']}{' (dirty)' if git['dirty'] else ''}], "
             f"core_counts={core_counts}, iterations={args.iterations}, "
             f"host_cpus={max_cpus}")
    log_info(f"Schedulers: {', '.join(name for name, _ in entries)}")
    log_info(f"Phases: {', '.join(phases)}")
    print()

    # results[cores][scheduler] = ITS SURVIVED AND CRASHED COUNTS AND ONE
    # {phase: result} AN ITERATION
    results: dict = {}
    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] Restricting to {nr_cpus} cores")
            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] failed to offline CPUs, skipping")
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(2)

            results[nr_cpus] = {}
            for sched_name, sched_cmd in entries_for_cores(entries, nr_cpus):
                arm = {"survived": 0, "crashed": 0, "runs": []}
                results[nr_cpus][sched_name] = arm
                for i in range(1, args.iterations + 1):
                    label = f"[{nr_cpus}C {i}/{args.iterations}] "
                    ok, phase_results = _contention_run_iteration(
                        label, nr_cpus, sched_name, sched_cmd, phases)
                    arm["survived" if ok else "crashed"] += 1
                    arm["runs"].append(phase_results)
                    if i < args.iterations:
                        log_info("Settling 3s before next iteration...")
                        time.sleep(3)
                log_info(f"[{nr_cpus}C] {sched_name}: "
                         f"{arm['survived']}/{args.iterations} survived")

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
    finally:
        restore_all_cpus(max_cpus)

    report, crashed = format_contention_report(ver, git, args.iterations, max_cpus, results)
    print()
    log.report(report)
    report_path = LOG_DIR / f"prism-contention-{stamp}.log"
    report_path.write_text(report)
    log_info(f"REPORT: {report_path}")
    log_info(f"METRICS: "
             f"{write_contention_prometheus(ver, git, stamp, max_cpus, args.iterations, results)}")

    return 0 if results and not crashed else 1


# SCX CI COMPATIBILITY (prism-scx)
#
# What sched-ext/scx's CI asks of a scheduler, asked of every sched_ext arm in
# the field. Functional: Loaded with no workload it stays the scheduler and
# says nothing CI's scan fails on (.github/include/scripts/test_sched). Stress:
# It survives stress-ng's affinity churn (run_stress_tests, the command from
# stress_tests.ini). The functional leg is also the read of a width or a
# hotplug (--disturb) with nothing else in the way of it, so the report
# carries what the arm left: the kernel's sched_ext event counters at its stop
# and its own output from first line to last.

SCX_CI_FAIL_PATTERNS = ["BUG:", "WARNING:"]
SCX_CI_FAIL_ICASE = ["error", "stall", "timeout"]
SCX_CI_FALSE_POSITIVES = [
    "Speculative Return Stack Overflow",
    "RETBleed",
    "spectre",
    "retbleed",
    "mitigation",
]
SCX_OUTPUT_HEAD = 40
SCX_OUTPUT_TAIL = 24


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


def _scx_functional(sched_name: str, sched_cmd: list[str], duration: float) -> dict:
    """The functional leg: Hold the arm for `duration` with no workload, scan
    what it said while it ran, stop it and keep what it left."""
    dmesg = DmesgMonitor()
    guard = start_and_wait(sched_cmd, sched_name, settle_secs=0.0)
    if guard is None:
        said = pandemonium_common.last_activation_failure
        return {"pass": False, "status": "FAILED", "held_s": 0.0,
                "failures": ["STARTUP FAILED"], "scx_events": {}, "log": None,
                "output": said.splitlines()}
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration and not sched_ejected(guard):
        time.sleep(0.2)
    held = time.monotonic() - t0
    ejected = sched_ejected(guard)
    crashed = dmesg.check()
    failures = _scx_ci_check_output(guard.drain_stdout() + "\n"
                                    + guard.read_stderr(limit=64000))
    if crashed:
        failures.append(f"DMESG CRASH: {dmesg.crash_msg}")
    if ejected:
        failures.append(f"SCHEDULER EXITED (code {guard.proc.returncode})")
    dmesg.save()
    stdout = stop_and_wait(guard)
    return {"pass": not failures, "status": "EJECTED" if ejected else "OK",
            "held_s": held, "failures": failures, "scx_events": dict(guard.events),
            "log": guard.stdout_path, "output": stdout.splitlines()}


def _scx_stress(sched_name: str, sched_cmd: list[str], n_cpus: int,
                duration: int) -> dict:
    """The stress leg: stress-ng with affinity pinning under the arm. CI passes
    an exit of 0 or of 143, the SIGTERM its timeout sends."""
    dmesg = DmesgMonitor()
    guard = start_and_wait(sched_cmd, sched_name, settle_secs=2)
    if guard is None:
        return {"pass": False, "failures": ["STARTUP FAILED"], "stress_exit": None}
    stress_cmd = ["stress-ng", "-t", str(duration), "--aggressive", "-M",
                  "-c", str(n_cpus), "-f", str(n_cpus),
                  "--affinity", "1", "--affinity-delay", "1", "--affinity-pin"]
    log_info(f"  {' '.join(stress_cmd)}")
    try:
        stress_exit = subprocess.run(stress_cmd, timeout=duration + 15,
                                     capture_output=True, text=True).returncode
    except subprocess.TimeoutExpired:
        log_warn(f"stress-ng timed out at {duration + 15}s")
        stress_exit = 143
    crashed = dmesg.check()
    ejected = sched_ejected(guard)
    failures = []
    if stress_exit not in (0, 143):
        failures.append(f"stress-ng exit code {stress_exit}")
    if crashed:
        failures.append(f"DMESG CRASH: {dmesg.crash_msg}")
    if ejected:
        failures.append(f"SCHEDULER DIED (code {guard.proc.returncode})")
    dmesg.save()
    stop_and_wait(guard)
    return {"pass": not failures, "failures": failures, "stress_exit": stress_exit}


def format_scx_report(version, git, func_s, stress_s, disturb, results) -> tuple[str, bool]:
    """(report text, whether every arm passed every leg at every width)."""
    lines = ["PANDEMONIUM SCX CI COMPATIBILITY",
             f"VERSION:     {version}",
             f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
             f"FUNCTIONAL:  {func_s:g}s an arm, no workload",
             f"STRESS:      {f'{stress_s}s of stress-ng an arm' if stress_s else 'not run'}"]
    if disturb:
        lines.append(f"DISTURB:     {disturb}")
    lines.append("")
    overall = bool(results)
    for nr_cpus in sorted(results):
        lines.append(f"[{nr_cpus} CORES]")
        lines.append(f"{'SCHEDULER':<28} {'FUNCTIONAL':>12} {'STRESS':>10} {'HELD':>8}")
        last: dict = {}
        for sched_name, runs in results[nr_cpus].items():
            func = [r["functional"] for r in runs]
            stress = [r["stress"] for r in runs if "stress" in r]
            f_ok = sum(1 for r in func if r["pass"])
            s_ok = sum(1 for r in stress if r["pass"])
            overall = overall and bool(func) and f_ok == len(func) and s_ok == len(stress)
            held = func[-1]["held_s"] if func else 0.0
            lines.append(f"{sched_name:<28} {f'{f_ok}/{len(func)}':>12} "
                         f"{f'{s_ok}/{len(stress)}' if stress else '--':>10} {held:>7.1f}s")
            if func:
                last[sched_name] = func[-1]
        lines += format_scx_events(last)
        for sched_name, runs in results[nr_cpus].items():
            for i, r in enumerate(runs, 1):
                for leg in ("functional", "stress"):
                    for failure in (r.get(leg) or {}).get("failures", []):
                        lines.append(f"{sched_name} iteration {i}, {leg}: {failure}")
        for sched_name, r in last.items():
            out = r.get("output") or []
            if r.get("log"):
                lines += ["", f"{sched_name}: ITS OWN OUTPUT ({r['log']})"]
            elif out:
                lines += ["", f"{sched_name}: WHAT IT SAID AS IT FAILED"]
            if len(out) > SCX_OUTPUT_HEAD + SCX_OUTPUT_TAIL:
                cut = len(out) - SCX_OUTPUT_HEAD - SCX_OUTPUT_TAIL
                out = (out[:SCX_OUTPUT_HEAD] + [f"... {cut} more line(s) in the file ..."]
                       + out[-SCX_OUTPUT_TAIL:])
            lines += [f"  {line}" for line in out]
        lines.append("")
    lines.append(f"OVERALL: {'PASS (scx CI compatible)' if overall else 'FAIL'}")
    return "\n".join(lines) + "\n", overall


def write_scx_prometheus(version, git, stamp, results) -> Path:
    pb = PrometheusBuilder("scx")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    for nr_cpus in sorted(results):
        for sched_name, runs in results[nr_cpus].items():
            al = {"scheduler": sched_name, "cores": str(nr_cpus)}
            for leg in ("functional", "stress"):
                legs = [r[leg] for r in runs if leg in r]
                if not legs:
                    continue
                ll = {**al, "test": leg}
                pb.gauge("iterations", len(legs), help="iterations the leg ran", labels=ll)
                pb.gauge("passed", sum(1 for r in legs if r["pass"]),
                         help="iterations the leg passed", labels=ll)
                pb.gauge("violations", sum(len(r["failures"]) for r in legs),
                         help="failures over the leg's iterations", labels=ll)
            func = [r["functional"] for r in runs]
            if func:
                pb.gauge("held_seconds", round(func[-1]["held_s"], 1),
                         help="Seconds the arm was held in the last functional leg",
                         labels=al)
                for event, count in sorted(func[-1]["scx_events"].items()):
                    pb.gauge("scx_event", count,
                             help="Kernel sched_ext event counter when the arm was stopped",
                             labels={**al, "event": event})
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-scx-{stamp}.prom"
    path.write_text(pb.render())
    return path


def cmd_bench_scx(args) -> int:
    """scx CI compatibility test, every sched_ext arm of the field.

    functional: Hold the arm for --duration with no workload, scan what it
                said for CI's failure patterns
    stress:     stress-ng with affinity pinning for --stress-duration under
                the arm; 0 skips the leg
    """
    entries = [(name, cmd) for name, cmd in field_of(args) if cmd is not None]
    if not entries:
        log_error("no sched_ext arm to check: EEVDF has nothing to load, and nothing "
                  "else named by --arm/--only/--schedulers is installed")
        return 2
    func_duration = max(0.0, args.duration)
    stress_duration = max(0, args.stress_duration)
    if stress_duration and not shutil.which("stress-ng"):
        log_error("stress-ng not found: install it, or skip the stress leg with "
                  "--stress-duration 0")
        return 1

    warm_sudo()
    if fields_pandemonium(entries):
        nuke_stale_build()
        if not build():
            return 1
    if not clear_loaded_scheduler():
        return 1

    max_cpus = get_possible_cpus()
    restore_all_cpus(max_cpus)

    if args.cores:
        core_counts = [int(c.strip()) for c in args.cores.split(",")]
        core_counts = [c for c in core_counts if 2 <= c <= max_cpus]
        core_counts = sorted(set(core_counts))
    else:
        core_counts = compute_core_counts(max_cpus)

    version = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    iterations = max(1, args.iterations)

    log_info(f"prism-scx v{version} [{git['commit']}{' (dirty)' if git['dirty'] else ''}]")
    log_info(f"Schedulers: {', '.join(name for name, _ in entries)}")
    log_info(f"Core counts: {core_counts}")
    log_info(f"Functional: {func_duration:g}s, Stress: "
             f"{f'{stress_duration}s' if stress_duration else 'not run'}, "
             f"Iterations: {iterations}")

    # results[cores][scheduler] = ONE {"functional": ..., "stress": ...} AN ITERATION
    results: dict = {}
    try:
        for nr_cpus in core_counts:
            log_info(f"[{nr_cpus}C] scx CI compatibility tests")

            if nr_cpus < max_cpus:
                if not restrict_cpus(nr_cpus, max_cpus):
                    log_error(f"[{nr_cpus}C] Failed to restrict CPUs")
                    restore_all_cpus(max_cpus)
                    continue
            time.sleep(1)

            results[nr_cpus] = {}
            for sched_name, sched_cmd in entries_for_cores(entries, nr_cpus):
                runs: list = []
                results[nr_cpus][sched_name] = runs
                for i in range(1, iterations + 1):
                    tag = f"[{nr_cpus}C] {sched_name}" + (
                        f" {i}/{iterations}" if iterations > 1 else "")
                    log_info(f"{tag}: functional, held {func_duration:g}s with no workload")
                    run = {"functional": _scx_functional(sched_name, sched_cmd, func_duration)}
                    for failure in run["functional"]["failures"][:10]:
                        log_error(f"{tag}: functional FAIL: {failure}")
                    time.sleep(2)
                    if stress_duration:
                        log_info(f"{tag}: stress, stress-ng {stress_duration}s "
                                 f"with affinity pinning")
                        run["stress"] = _scx_stress(sched_name, sched_cmd, nr_cpus,
                                                    stress_duration)
                        for failure in run["stress"]["failures"][:10]:
                            log_error(f"{tag}: stress FAIL: {failure}")
                        time.sleep(2)
                    runs.append(run)

            if nr_cpus < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(2)

    except KeyboardInterrupt:
        log.interrupted()
    finally:
        restore_all_cpus(max_cpus)

    report, overall_pass = format_scx_report(version, git, func_duration, stress_duration,
                                             getattr(args, "disturb", None), results)
    print()
    log.report(report)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LOG_DIR / f"prism-scx-{stamp}.log"
    report_path.write_text(report)
    log_info(f"REPORT: {report_path}")
    log_info(f"METRICS: {write_scx_prometheus(version, git, stamp, results)}")

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
    bench.add_argument("--cores", type=str, default=None,
                       help="Comma-separated core counts "
                            "(default: auto 2,4,8,...,max)")
    bench.add_argument("--cells", type=str, default=None, metavar="LIST",
                       help="Comma-separated cells to run and nothing else, in "
                            "the bench's own order under one scheduler start: "
                            + ", ".join(SCALE_CELLS) + ". Default: every cell, "
                            "the full pass")
    add_field_flags(bench)
    bench.add_argument("--trace", action="store_true",
                       help="Record montauk captures to /tmp/pandemonium beside "
                            "the measurement: the IPC cell's first iteration, "
                            "and one extra draw of launch. With --cells naming "
                            "them, one extra draw each of compile, latency, "
                            "longrun and mixed. A recorded draw's timings carry "
                            "the tracing tax and are kept apart from the cell's.")


    contention_bench = sub.add_parser("prism-contention",
                                      help="Contention stress test, six load phases")
    contention_bench.add_argument("--iterations", type=int, default=1,
                                  help="Iterations of the phases per arm at each "
                                       "core count (default: 1)")
    contention_bench.add_argument("--cores", type=str, default=None,
                                  help="Comma-separated core counts "
                                       "(default: auto 2,4,8,...,max)")
    contention_bench.add_argument("--phase", type=str, default=None,
                                  help="Run one phase: " + ", ".join(CONTENTION_PHASES))
    contention_bench.add_argument("--trace", action="store_true",
                                  help="Instead of the phases, one montauk recording "
                                       "an arm of --phase (deficit-storm unless named, "
                                       "or sojourn-pressure), capped at --duration")
    contention_bench.add_argument("--duration", type=int, default=20,
                                  help="With --trace: capture window seconds "
                                       "(default: 20)")
    add_field_flags(contention_bench)

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
                                help="Per-CPU DSQ visibility stress test, four tests")
    pcpu_bench.add_argument("--iterations", type=int, default=1,
                            help="Iterations of the tests per arm at each core "
                                 "count (default: 1)")
    pcpu_bench.add_argument("--cores", type=str, default=None,
                            help="Comma-separated core counts "
                                 "(default: auto 2,4,8,...,max)")
    pcpu_bench.add_argument("--trace", action="store_true",
                            help="Instead of the tests, one montauk recording an "
                                 "arm of the burst-starvation load, capped at "
                                 "--duration")
    pcpu_bench.add_argument("--duration", type=int, default=20,
                            help="With --trace: capture window seconds (default: 20)")
    add_field_flags(pcpu_bench)

    coldwake_bench = sub.add_parser("prism-coldwake",
        help="Cold-wake latency vs frequency-at-wake (idle->bare-wake, powersave)")
    coldwake_bench.add_argument("--trace", action="store_true",
        help="Capture montauk recordings of the cold-wake or storm cycle. "
             "Required: this bench is a capture")
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
    coldwake_bench.add_argument("--cores", type=str, default=None,
        help="Accepted for suite uniformity; cold-wake pins one core regardless")
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
    add_field_flags(coldwake_bench)
    coldwake_bench.add_argument("--iterations", type=int, default=1,
        help="Accepted for suite uniformity; cold-wake samples over time, not trials")

    scx_bench = sub.add_parser("prism-scx",
                                help="scx CI compatibility test, every sched_ext arm")
    scx_bench.add_argument("--duration", type=float, default=30.0,
                           help="Functional leg: seconds each arm is held with no "
                                "workload (default: 30); under --disturb, longer "
                                "than its START and HOLD together")
    scx_bench.add_argument("--stress-duration", type=int, default=31,
                           help="Stress leg: seconds of stress-ng (default: 31); "
                                "0 skips the leg")
    scx_bench.add_argument("--cores", type=str, default=None,
                           help="Comma-separated core counts "
                                "(default: auto 2,4,8,...,max)")
    add_field_flags(scx_bench)
    scx_bench.add_argument("--iterations", type=int, default=1,
                           help="Times both legs run per arm at each core count "
                                "(default: 1)")
    scx_bench.add_argument("--trace", action="store_true",
                           help="No effect: prism-scx records no capture")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

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

    log_error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
