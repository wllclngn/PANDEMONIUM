#!/usr/bin/env python3
"""
PANDEMONIUM bench-fork-thread: scheduler IPC throughput benchmark.

Cycles through EEVDF, PANDEMONIUM (BPF), PANDEMONIUM (ADAPTIVE), and
scx_bpfland, running `perf bench sched messaging -t -g 24 -l 6000` under
each (the exact CachyOS benchmark command).

Usage:
    ./pandemonium.py bench-fork-thread
"""

import multiprocessing
import os
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

# Import scheduler lifecycle from the test harness
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler

NUM_GROUPS = 24
NR_LOOPS = 6000


def _raise_fd_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = max(soft, 4096)
    if target > hard:
        target = hard
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


def run_perf_bench():
    """Run perf bench sched messaging, return elapsed seconds or None."""
    result = subprocess.run(
        ["perf", "bench", "-f", "simple", "sched", "messaging",
         "-t", "-g", str(NUM_GROUPS), "-l", str(NR_LOOPS)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        log_error(f"perf bench failed: {result.stderr.strip()}")
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        log_error(f"Could not parse perf output: {result.stdout.strip()}")
        return None


def write_prometheus(version, git, stamp, ncpus, results):
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

    for sched_name, elapsed in results.items():
        sl = {"scheduler": sched_name}
        if elapsed is not None:
            gauge("pandemonium_fork_thread_seconds",
                  "perf bench sched messaging elapsed time", f"{elapsed:.4f}", sl)
        else:
            gauge("pandemonium_fork_thread_seconds",
                  "perf bench sched messaging elapsed time", "-1", sl)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"bench-fork-thread-{version}-{stamp}.prom"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_report(version, git, stamp, ncpus, results):
    report = []
    report.append(f"bench-fork-thread v{version} [{git['commit']}]")
    report.append(f"cpus: {ncpus}  command: perf bench sched messaging "
                  f"-t -g {NUM_GROUPS} -l {NR_LOOPS}")
    report.append("")

    eevdf_time = results.get("EEVDF")

    report.append(f"{'SCHEDULER':<30} {'TIME':>10}  {'VS EEVDF':>10}")
    for sched_name, elapsed in results.items():
        if elapsed is None:
            report.append(f"{sched_name:<30} {'FAILED':>10}")
        elif sched_name == "EEVDF":
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s  {'(baseline)':>10}")
        elif eevdf_time and eevdf_time > 0:
            delta = (elapsed - eevdf_time) / eevdf_time * 100
            sign = "+" if delta > 0 else ""
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s  {sign}{delta:>8.1f}%")
        else:
            report.append(f"{sched_name:<30} {elapsed:>9.3f}s")
    report.append("")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bench-fork-thread-{stamp}.log"
    path.write_text("\n".join(report) + "\n")
    return path


def main():
    if not shutil.which("perf"):
        log_error("perf not found. Install with: sudo pacman -S perf")
        return 1

    _raise_fd_limit()

    ncpus = multiprocessing.cpu_count()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirty = " (dirty)" if git["dirty"] else ""

    log_info(f"bench-fork-thread v{ver} [{git['commit']}{dirty}]")
    log_info(f"CPUs: {ncpus}  command: perf bench sched messaging "
             f"-t -g {NUM_GROUPS} -l {NR_LOOPS}")
    print()

    # Stop any active scheduler
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

    results = {}

    try:
        for sched_name, cmd in entries:
            log_info(f"[{sched_name}] starting...")

            guard = None
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"[{sched_name}] failed to activate, skipping")
                    results[sched_name] = None
                    continue

            log_info(f"[{sched_name}] running perf bench (expect ~60-120s)...")
            elapsed = run_perf_bench()

            if elapsed is not None:
                log_info(f"[{sched_name}] {elapsed:.3f}s")
            else:
                log_error(f"[{sched_name}] perf bench failed")

            results[sched_name] = elapsed

            if guard is not None:
                stop_and_wait(guard)

            time.sleep(2)
            print()

    except KeyboardInterrupt:
        log_info("Interrupted")
    finally:
        # Restart pandemonium service
        log_info("Restarting pandemonium service...")
        subprocess.run(["sudo", "systemctl", "start", "pandemonium"],
                       capture_output=True)

    if results:
        print()
        log_info("RESULTS")
        eevdf_time = results.get("EEVDF")
        for sched_name, elapsed in results.items():
            if elapsed is None:
                log_info(f"  {sched_name:<30} FAILED")
            elif sched_name == "EEVDF" or not eevdf_time:
                log_info(f"  {sched_name:<30} {elapsed:.3f}s")
            else:
                delta = (elapsed - eevdf_time) / eevdf_time * 100
                sign = "+" if delta > 0 else ""
                log_info(f"  {sched_name:<30} {elapsed:.3f}s  ({sign}{delta:.1f}%)")
        print()

        prom_path = write_prometheus(ver, git, stamp, ncpus, results)
        report_path = write_report(ver, git, stamp, ncpus, results)
        log_info(f"Report: {report_path}")
        log_info(f"Prometheus: {prom_path}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
