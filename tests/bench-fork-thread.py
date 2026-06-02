#!/usr/bin/env python3
"""
PANDEMONIUM bench-fork-thread: scheduler IPC throughput + hot-path profiling.

Cycles through EEVDF, PANDEMONIUM (BPF), PANDEMONIUM (ADAPTIVE), and
scx_bpfland, running `perf bench sched messaging -t -g 24 -l 6000` under
perf stat to capture both elapsed time and hardware counters.

Usage:
    ./pandemonium.py bench-fork-thread
"""

import multiprocessing
import os
import re
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    LOG_DIR, ARCHIVE_DIR, BINARY,
    get_version, get_git_info,
    log_info, log_warn, log_error,
    is_scx_active, scx_scheduler_name,
    wait_for_deactivation,
)

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler

NUM_GROUPS = 24
NR_LOOPS_FULL = 6000
NR_LOOPS_QUICK = 1000
NR_LOOPS = NR_LOOPS_FULL  # SET BY main() BASED ON --quick FLAG

PERF_EVENTS = [
    "cycles",
    "instructions",
    "cache-misses",
    "cache-references",
    "context-switches",
    "cpu-migrations",
    "branch-misses",
    "task-clock",
]


def _raise_fd_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = max(soft, 4096)
    if target > hard:
        target = hard
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


def run_perf_bench():
    """Run perf stat wrapping perf bench. Returns (elapsed_s, counters) or (None, None)."""
    events = ",".join(PERF_EVENTS)
    cmd = [
        "perf", "stat", "-e", events, "--",
        "perf", "bench", "-f", "simple", "sched", "messaging",
        "-t", "-g", str(NUM_GROUPS), "-l", str(NR_LOOPS),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    elapsed = None
    try:
        elapsed = float(result.stdout.strip())
    except (ValueError, AttributeError):
        log_error(f"Could not parse perf bench output: {result.stdout.strip()}")
        return None, None

    counters = {}
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Performance"):
            continue
        m = re.match(r'^([\d,\.]+)\s+(?:msec\s+)?(\S+)', line)
        if m:
            val_str = m.group(1).replace(",", "")
            name = m.group(2).rstrip(":u").rstrip(":k")
            try:
                counters[name] = float(val_str)
            except ValueError:
                pass

    return elapsed, counters


def _fmt_count(val):
    if val >= 1_000_000_000:
        return f"{val/1e9:.2f}G"
    elif val >= 1_000_000:
        return f"{val/1e6:.2f}M"
    elif val >= 1_000:
        return f"{val/1e3:.2f}K"
    return f"{val:.0f}"


def write_prometheus(version, git, stamp, ncpus, all_results):
    lines = []
    emitted = set()

    def gauge(name, help_text, value, labels=None):
        if name not in emitted:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            emitted.add(name)
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    dirty = "true" if git["dirty"] else "false"
    gauge("pandemonium_fork_thread_info", "Build and run metadata", 1,
          {"version": version, "git_commit": git["commit"], "git_dirty": dirty})
    gauge("pandemonium_fork_thread_timestamp_seconds", "Test start time",
          int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()))
    gauge("pandemonium_fork_thread_cpus", "CPUs available", ncpus)
    gauge("pandemonium_fork_thread_groups", "Message groups", NUM_GROUPS)
    gauge("pandemonium_fork_thread_loops", "Loops per sender per receiver", NR_LOOPS)

    for sched_name, (elapsed, counters) in all_results.items():
        sl = {"scheduler": sched_name}
        if elapsed is not None:
            gauge("pandemonium_fork_thread_seconds",
                  "perf bench sched messaging elapsed time", f"{elapsed:.4f}", sl)
        else:
            gauge("pandemonium_fork_thread_seconds",
                  "perf bench sched messaging elapsed time", "-1", sl)
            continue

        if counters:
            for cn, cv in counters.items():
                safe = cn.replace("-", "_").replace(".", "_")
                gauge(f"pandemonium_fork_thread_{safe}",
                      f"perf stat {cn}", f"{cv:.0f}", sl)

            cycles = counters.get("cycles", 0)
            instructions = counters.get("instructions", 0)
            cache_miss = counters.get("cache-misses", 0)
            cache_ref = counters.get("cache-references", 0)

            if cycles > 0 and instructions > 0:
                gauge("pandemonium_fork_thread_ipc",
                      "Instructions per cycle", f"{instructions / cycles:.3f}", sl)
            if cache_ref > 0:
                gauge("pandemonium_fork_thread_cache_miss_rate",
                      "Cache miss rate", f"{cache_miss / cache_ref:.6f}", sl)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"bench-fork-thread-{version}-{stamp}.prom"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_report(version, git, stamp, ncpus, all_results):
    report = []
    report.append(f"bench-fork-thread v{version} [{git['commit']}]")
    report.append(f"cpus: {ncpus}  command: perf bench sched messaging "
                  f"-t -g {NUM_GROUPS} -l {NR_LOOPS}")
    report.append("")

    eevdf_elapsed = None
    if "EEVDF" in all_results:
        eevdf_elapsed = all_results["EEVDF"][0]

    # TIMING TABLE
    report.append(f"{'SCHEDULER':<30} {'TIME':>10}  {'VS EEVDF':>10}")
    for sched_name, (elapsed, _) in all_results.items():
        if elapsed is None:
            report.append(f"{sched_name:<30} {'FAILED':>10}")
        elif sched_name == "EEVDF":
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s  {'(baseline)':>10}")
        elif eevdf_elapsed and eevdf_elapsed > 0:
            delta = (elapsed - eevdf_elapsed) / eevdf_elapsed * 100
            sign = "+" if delta > 0 else ""
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s  {sign}{delta:>8.1f}%")
        else:
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s")
    report.append("")

    # COUNTER TABLE
    counter_names = ["cycles", "instructions", "cache-misses", "cache-references",
                     "context-switches", "cpu-migrations", "branch-misses"]
    sched_names = list(all_results.keys())
    header = f"{'COUNTER':<24}"
    for sn in sched_names:
        header += f" {sn:>18}"
    report.append(header)

    for cn in counter_names:
        row = f"{cn:<24}"
        for sn in sched_names:
            _, counters = all_results[sn]
            if counters and cn in counters:
                row += f" {_fmt_count(counters[cn]):>18}"
            else:
                row += f" {'N/A':>18}"
        report.append(row)
    report.append("")

    # DERIVED TABLE
    header2 = f"{'DERIVED':<24}"
    for sn in sched_names:
        header2 += f" {sn:>18}"
    report.append(header2)

    def derived_row(label, fn):
        row = f"{label:<24}"
        for sn in sched_names:
            elapsed, counters = all_results[sn]
            if counters and elapsed:
                try:
                    row += f" {fn(elapsed, counters):>18}"
                except (KeyError, ZeroDivisionError):
                    row += f" {'N/A':>18}"
            else:
                row += f" {'N/A':>18}"
        report.append(row)

    derived_row("IPC", lambda e, c: f"{c['instructions'] / c['cycles']:.3f}")
    derived_row("Cache miss %", lambda e, c: f"{c['cache-misses'] / c['cache-references'] * 100:.2f}%")
    derived_row("Cycles/miss", lambda e, c: f"{c['cycles'] / c['cache-misses']:,.0f}" if c['cache-misses'] > 0 else "N/A")
    report.append("")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bench-fork-thread-{stamp}.log"
    path.write_text("\n".join(report) + "\n")
    return path


# ---- montauk eBPF trace capture (--trace) ----
# Instead of the timing comparison, record a per-thread dispatch flight-recording
# of the storm under montauk. montauk comes up first (incl. a quiet idle baseline)
# so the storm onset and first-bit transient are captured, not missed.

MONTAUK = "/usr/local/bin/montauk"
TRACE_PATTERN = "perf"            # perf bench tree; children/threads auto-tracked
TRACE_LOG_INTERVAL_MS = 100       # fine cadence so the first-bit transient shows
MONTAUK_ATTACH_TIMEOUT = 10.0
BASELINE_SECONDS = 3.0            # quiet idle recorded before the storm, for contrast
LOAD_SAFETY_TIMEOUT = 180.0       # hard cap so a wedged scheduler can't hang the run


def _wait_for_montauk_log(log_dir, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(log_dir.glob("montauk_*.prom")):
            return True
        time.sleep(0.2)
    return False


def _stop_montauk(mont):
    # Stop the way Ctrl+C would (SIGINT, montauk's clean shutdown), escalating
    # only if it ignores us. No manual interrupt required.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if mont.poll() is not None:
            return
        mont.send_signal(sig)
        try:
            mont.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_load_traced(timeout):
    cmd = ["perf", "bench", "-f", "simple", "sched", "messaging",
           "-t", "-g", str(NUM_GROUPS), "-l", str(NR_LOOPS)]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return time.time() - t0


def trace_capture(sched_name, cmd, stamp, duration):
    safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
    montauk_log = LOG_DIR / f"montauk-fork-thread-{safe}-{stamp}"
    montauk_log.mkdir(parents=True, exist_ok=True)
    montauk_out = LOG_DIR / f"montauk-fork-thread-{safe}-{stamp}.stdout"

    guard = None
    if cmd is not None:
        log_info(f"[{sched_name}] activating scheduler...")
        guard = start_and_wait(cmd, sched_name)
        if guard is None:
            log_error(f"[{sched_name}] failed to activate, skipping")
            return None

    log_info(f"[{sched_name}] starting montauk trace -> {montauk_log}")
    with open(montauk_out, "w") as out:
        mont = subprocess.Popen(
            [MONTAUK, "--trace", TRACE_PATTERN, "--log", str(montauk_log),
             "--log-interval-ms", str(TRACE_LOG_INTERVAL_MS)],
            stdout=out, stderr=subprocess.STDOUT)
    try:
        if not _wait_for_montauk_log(montauk_log, MONTAUK_ATTACH_TIMEOUT):
            log_warn(f"[{sched_name}] montauk slow to start (see {montauk_out})")
        log_info(f"[{sched_name}] recording idle baseline for {BASELINE_SECONDS:.0f}s...")
        time.sleep(BASELINE_SECONDS)
        load_timeout = duration if duration > 0 else LOAD_SAFETY_TIMEOUT
        log_info(f"[{sched_name}] running storm (montauk recording, window {load_timeout:.0f}s)...")
        elapsed = _run_load_traced(load_timeout)
        if elapsed is not None:
            log_info(f"[{sched_name}] load completed in {elapsed:.3f}s")
        else:
            log_info(f"[{sched_name}] capture window ({load_timeout:.0f}s) elapsed -- load cut")
    finally:
        _stop_montauk(mont)
        if guard is not None:
            stop_and_wait(guard)

    log_info(f"[{sched_name}] recording: {montauk_log}")
    return montauk_log


def _restore_ownership(paths):
    # We self-elevated to root, so anything montauk wrote lands root-owned.
    # Hand the recordings (the trace dir + its .stdout) back to the invoking
    # user so they're viewable without sudo. No-op when not running under sudo.
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return
    for p in paths:
        for target in (Path(p), Path(f"{p}.stdout")):
            if target.exists():
                subprocess.run(["chown", "-R", f"{user}:", str(target)],
                               capture_output=True)


def run_trace(args):
    if os.geteuid() != 0:
        # SELF-ELEVATE: the trace flow needs root end-to-end (montauk eBPF attach
        # + sched_ext load), so re-exec under sudo rather than make the user type
        # it -- matches how every other command elevates its own privileged steps.
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    if not Path(MONTAUK).exists():
        log_error(f"montauk not found at {MONTAUK}")
        return 1

    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_info(f"bench-fork-thread --trace v{ver} [{git['commit']}]  "
             f"trace='{TRACE_PATTERN}'  interval={TRACE_LOG_INTERVAL_MS}ms  "
             f"load=perf bench sched messaging -t -g {NUM_GROUPS} -l {NR_LOOPS}")
    print()

    if is_scx_active():
        log_warn(f"sched_ext active ({scx_scheduler_name()}) -- stopping pandemonium")
        subprocess.run(["systemctl", "stop", "pandemonium"], capture_output=True)
        wait_for_deactivation(5.0)
    time.sleep(1)

    entries = [("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"])]
    if args.compare_eevdf:
        entries.insert(0, ("EEVDF", None))

    recs = {}
    try:
        for name, cmd in entries:
            recs[name] = trace_capture(name, cmd, stamp, args.duration)
            time.sleep(2)
            print()
    except KeyboardInterrupt:
        log_info("Interrupted")
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)
        _restore_ownership(recs.values())

    print()
    log_info("Recordings (inspect montauk_trace_thread_* over montauk_scrape_timestamp_ms):")
    for n, r in recs.items():
        log_info(f"  {n}: {r}")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Quick mode: 1000 loops instead of 6000")
    ap.add_argument("--trace", action="store_true",
                    help="Capture a montauk eBPF trace of the storm (per-thread "
                         "dispatch flight recording) instead of the timing run. Root.")
    ap.add_argument("--compare-eevdf", action="store_true",
                    help="With --trace: also record EEVDF as the clean reference")
    ap.add_argument("--duration", type=float, default=0,
                    help="With --trace: capture window seconds (0 = bench to "
                         "completion, hard-capped at 180s)")
    ap.add_argument("--iterations", type=int, default=1,
                    help="Run each scheduler N times and report the MEDIAN "
                         "(robust to a poisoned outlier run). Default 1.")
    ap.add_argument("--all-scx", action="store_true",
                    help="Also run the full installed scx scheduler field "
                         "(scx_bpfland, scx_rusty, scx_lavd, scx_flow, "
                         "scx_rustland, scx_p2dq, scx_tickless, scx_cosmos, "
                         "scx_cake, scx_flash, scx_beerland, scx_layered). "
                         "Default: EEVDF + PANDEMONIUM (BPF + ADAPTIVE) only.")
    ap.add_argument("--phi-sweep", type=str, nargs="?", const="0", default=None,
                    metavar="VALUES",
                    help="Phi A/B: instead of the full scx field, run PANDEMONIUM "
                         "(BPF mode) across phi_dist_scale_q16 values (comma list; "
                         "0 = Phi off) plus the topology default and EEVDF. Bare "
                         "--phi-sweep tests {0, default}. Isolates Phi's marginal "
                         "effect on this CPU's CCX layout.")
    args = ap.parse_args()
    if args.iterations < 1:
        args.iterations = 1

    global NR_LOOPS
    NR_LOOPS = NR_LOOPS_QUICK if args.quick else NR_LOOPS_FULL

    if args.trace:
        return run_trace(args)

    if not shutil.which("perf"):
        log_error("perf not found. Install with: sudo pacman -S perf")
        return 1

    _raise_fd_limit()

    ncpus = multiprocessing.cpu_count()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirty = " (dirty)" if git["dirty"] else ""
    mode = "QUICK" if args.quick else "FULL"

    log_info(f"bench-fork-thread v{ver} [{git['commit']}{dirty}] [{mode}]")
    log_info(f"CPUs: {ncpus}  command: perf stat + perf bench sched messaging "
             f"-t -g {NUM_GROUPS} -l {NR_LOOPS}")
    print()

    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping pandemonium service")
        subprocess.run(["sudo", "systemctl", "stop", "pandemonium"],
                       capture_output=True)
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext")
            return 1
    time.sleep(1)

    if args.phi_sweep is not None:
        # PHI A/B: hold everything constant, vary only phi_dist_scale_q16 via the
        # scheduler's --phi-scale override. BPF mode (matches the established BPF
        # anchor; no adaptive loop to perturb). EEVDF for the VS-EEVDF column, the
        # topology default (no override), then one run per requested value (0 = off).
        vals = [v.strip() for v in args.phi_sweep.split(",") if v.strip() != ""]
        entries = [
            ("EEVDF", None),
            ("PANDEMONIUM (phi=default)", [str(BINARY), "--no-adaptive"]),
        ]
        for v in vals:
            entries.append(
                (f"PANDEMONIUM (phi={v})", [str(BINARY), "--no-adaptive", "--phi-scale", v])
            )
        log_info(f"PHI SWEEP: topology default + values {vals} (BPF mode)")
    else:
        entries = [
            ("EEVDF", None),
            ("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]),
            ("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]),
        ]

        # FULL FIELD: every installed production scx scheduler in the ring against
        # EEVDF and PANDEMONIUM. Gated behind --all-scx (opt-in) to keep the default
        # run fast -- the EEVDF + PANDEMONIUM (BPF + ADAPTIVE) trio is what we iterate
        # on most. scx_chaos is excluded (fault-injection test scheduler, not a
        # contender). scx_layered needs a layer spec and may self-skip without one.
        if args.all_scx:
            scx_field = [
                "scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
                "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
                "scx_beerland", "scx_layered",
            ]
            for s in scx_field:
                if find_scheduler(s):
                    entries.append((s, [s]))
                else:
                    log_warn(f"{s} not found, skipping")

    all_results = {}

    try:
        for sched_name, cmd in entries:
            log_info(f"[{sched_name}] starting...")

            guard = None
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"[{sched_name}] failed to activate, skipping")
                    all_results[sched_name] = (None, None)
                    continue

            log_info(f"[{sched_name}] running perf stat + bench (x{args.iterations})...")
            samples = []
            for it in range(args.iterations):
                el, ct = run_perf_bench()
                if el is not None:
                    samples.append((el, ct))
                    msg = f"[{sched_name}] iter {it + 1}/{args.iterations}: {el:.3f}s"
                    if ct and ct.get("cache-misses"):
                        msg += f"  cache-misses={_fmt_count(ct['cache-misses'])}"
                    log_info(msg)
                else:
                    log_error(f"[{sched_name}] iter {it + 1}/{args.iterations} perf bench failed")

            if samples:
                # MEDIAN across iterations -- robust to a single poisoned run.
                # sched_ext state poisoning can blow one iteration up ~100x; the
                # median ignores that outlier where a mean would be wrecked by it.
                elapsed = statistics.median([s[0] for s in samples])
                counters = {}
                keys = set()
                for _, ct in samples:
                    if ct:
                        keys.update(ct.keys())
                for k in keys:
                    vals = [ct[k] for _, ct in samples if ct and k in ct]
                    if vals:
                        counters[k] = statistics.median(vals)
                cyc = counters.get("cycles", 0)
                ins = counters.get("instructions", 0)
                ipc = ins / cyc if cyc > 0 else 0
                log_info(f"[{sched_name}] MEDIAN {elapsed:.3f}s  IPC={ipc:.3f}  "
                         f"cache-misses={_fmt_count(counters.get('cache-misses', 0))}  "
                         f"(n={len(samples)}/{args.iterations})")
            else:
                elapsed, counters = None, None
                log_error(f"[{sched_name}] all {args.iterations} iterations failed")

            all_results[sched_name] = (elapsed, counters)

            if guard is not None:
                stop_and_wait(guard)

            time.sleep(2)
            print()

    except KeyboardInterrupt:
        log_info("Interrupted")
    finally:
        # DISENGAGE AT END (MATCHES OTHER BENCHES). EACH PER-SCHEDULER
        # stop_and_wait() ALREADY TORE DOWN ITS OWN INSTANCE; WE JUST MAKE
        # SURE NOTHING LINGERS BY WAITING FOR FULL DEACTIVATION.
        if is_scx_active():
            wait_for_deactivation(5.0)

    if all_results:
        print()
        prom_path = write_prometheus(ver, git, stamp, ncpus, all_results)
        report_path = write_report(ver, git, stamp, ncpus, all_results)

        print(report_path.read_text())
        log_info(f"Report: {report_path}")
        log_info(f"Prometheus: {prom_path}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
