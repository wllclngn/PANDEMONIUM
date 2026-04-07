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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Quick mode: 1000 loops instead of 6000")
    args = ap.parse_args()

    global NR_LOOPS
    NR_LOOPS = NR_LOOPS_QUICK if args.quick else NR_LOOPS_FULL

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

    entries = [
        ("EEVDF", None),
        ("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]),
        ("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]),
    ]

    bpfland = find_scheduler("scx_bpfland")
    if bpfland:
        entries.append(("scx_bpfland", ["scx_bpfland"]))
    else:
        log_warn("scx_bpfland not found, skipping")

    flow = find_scheduler("scx_flow")
    if flow:
        entries.append(("scx_flow", ["scx_flow"]))
    else:
        log_warn("scx_flow not found, skipping")

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

            log_info(f"[{sched_name}] running perf stat + bench...")
            elapsed, counters = run_perf_bench()

            if elapsed is not None:
                log_info(f"[{sched_name}] {elapsed:.3f}s")
                if counters:
                    cyc = counters.get("cycles", 0)
                    ins = counters.get("instructions", 0)
                    cmiss = counters.get("cache-misses", 0)
                    ipc = ins / cyc if cyc > 0 else 0
                    log_info(f"  cycles={_fmt_count(cyc)}  IPC={ipc:.3f}  cache-misses={_fmt_count(cmiss)}")
            else:
                log_error(f"[{sched_name}] perf bench failed")

            all_results[sched_name] = (elapsed, counters)

            if guard is not None:
                stop_and_wait(guard)

            time.sleep(2)
            print()

    except KeyboardInterrupt:
        log_info("Interrupted")
    finally:
        log_info("Restarting pandemonium service...")
        subprocess.run(["sudo", "systemctl", "start", "pandemonium"],
                       capture_output=True)

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
