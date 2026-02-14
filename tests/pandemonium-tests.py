#!/usr/bin/env python3
"""
PANDEMONIUM test orchestrator.

Usage:
    ./tests/pandemonium-tests.py bench-scale                    Full throughput + latency benchmark
    ./tests/pandemonium-tests.py bench-scale --skip-latency     Throughput only (skip Rust latency)
    ./tests/pandemonium-tests.py bench-scale --iterations 5     More iterations
    ./tests/pandemonium-tests.py bench-scale --schedulers scx_rusty,scx_bpfland
"""

import argparse
import math
import os
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
    get_version, get_git_info,
    log_info, log_warn, log_error, run_cmd,
    has_root_owned_files, clean_root_files, check_sources_changed, build,
)


# =============================================================================
# CONFIGURATION (test-specific)
# =============================================================================

SCX_OPS = Path("/sys/kernel/sched_ext/root/ops")
DEFAULT_EXTERNALS = ["scx_bpfland"]


# =============================================================================
# DMESG CAPTURE
# =============================================================================

def dmesg_baseline() -> int:
    """Snapshot current dmesg line count for later diffing."""
    r = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True)
    if r.returncode != 0:
        return 0
    return len(r.stdout.splitlines())


def capture_dmesg(baseline: int, stamp: str) -> None:
    """Capture new dmesg lines since baseline, save to file, print summary."""
    r = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True)
    if r.returncode != 0:
        log_warn("Could not capture dmesg")
        return

    lines = r.stdout.splitlines()
    new_lines = lines[baseline:] if baseline < len(lines) else lines

    if not new_lines:
        log_info("dmesg: no new kernel messages")
        return

    # Save all new lines
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    dmesg_path = LOG_DIR / f"dmesg-{stamp}.log"
    dmesg_path.write_text("\n".join(new_lines) + "\n")

    # Summarize scheduler-related issues
    keywords = ["sched_ext", "pandemonium", "non-existent DSQ", "zero slice",
                "panic", "BUG:", "RIP:", "Oops", "Call Trace"]
    filtered = [l for l in new_lines
                if any(kw in l for kw in keywords)]

    if not filtered:
        log_info(f"dmesg: {len(new_lines)} messages, no scheduler issues")
        return

    crashes = sum(1 for l in filtered
                  if "non-existent DSQ" in l or "runtime error" in l)
    zero_slices = sum(1 for l in filtered if "zero slice" in l)
    panics = sum(1 for l in filtered
                 if "panic" in l or "BUG:" in l or "RIP:" in l)

    if panics:
        log_error(f"dmesg: KERNEL PANIC/BUG -- see {dmesg_path}")
    if crashes:
        log_warn(f"dmesg: {crashes} scheduler crash(es)")
    if zero_slices:
        log_warn(f"dmesg: {zero_slices} zero-slice warning(s)")

    for line in filtered:
        log_info(f"  {line.strip()}")

    log_info(f"dmesg: {len(new_lines)} messages saved to {dmesg_path}")


# =============================================================================
# BUILD (test-specific helpers; shared build logic in pandemonium_common)
# =============================================================================

def fix_ownership():
    uid = os.getuid()
    gid = os.getgid()
    log_info(f"Fixing ownership to {uid}:{gid}...")
    for d in [TARGET_DIR, LOG_DIR]:
        if d.exists():
            subprocess.run(
                ["sudo", "chown", "-R", f"{uid}:{gid}", str(d)],
                capture_output=True,
            )


def nuke_stale_build():
    """Nuke the build dir if any source file is newer than the binary.
    Prevents stale test binaries from surviving across code changes."""
    if not TARGET_DIR.exists():
        return
    if not BINARY.exists():
        # build dir exists but no binary -- nuke it
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


def build_test(test_name: str) -> bool:
    log_info(f"Building test binary: {test_name}...")
    ret = run_cmd(
        ["cargo", "test", "--release", "--test", test_name, "--no-run"],
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
        cwd=SCRIPT_DIR,
    )
    if ret != 0:
        log_error(f"Test build failed: {test_name}")
        return False
    log_info(f"Test binary ready: {test_name}")
    return True


# =============================================================================
# SCHEDULER PROCESS MANAGEMENT
# =============================================================================

def is_scx_active() -> bool:
    try:
        return bool(SCX_OPS.read_text().strip())
    except (FileNotFoundError, PermissionError):
        return False


def scx_scheduler_name() -> str:
    try:
        return SCX_OPS.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def wait_for_activation(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_scx_active():
            return True
        time.sleep(0.1)
    return False


def wait_for_deactivation(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_scx_active():
            return True
        time.sleep(0.2)
    return False


def find_scheduler(name: str) -> str | None:
    return shutil.which(name)


class SchedulerProcess:
    """RAII-style guard for a running sched_ext scheduler."""

    def __init__(self, proc: subprocess.Popen, name: str,
                 stderr_path: str | None = None):
        self.proc = proc
        self.name = name
        self.pgid = os.getpgid(proc.pid)
        self.stderr_path = stderr_path

    def stop(self):
        if self.proc.poll() is not None:
            return
        # SIGINT → poll 500ms → SIGKILL
        try:
            os.killpg(self.pgid, signal.SIGINT)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def read_stderr(self, limit: int = 4000) -> str:
        """Read captured stderr from temp file."""
        if self.stderr_path:
            try:
                return Path(self.stderr_path).read_text()[:limit]
            except (FileNotFoundError, PermissionError):
                pass
        return ""

    def cleanup(self):
        """Remove stderr temp file."""
        if self.stderr_path:
            try:
                os.unlink(self.stderr_path)
            except (FileNotFoundError, PermissionError):
                pass

    def __del__(self):
        self.stop()
        self.cleanup()


def start_scheduler(cmd: list[str], name: str) -> SchedulerProcess:
    """Spawn a scheduler subprocess in its own process group."""
    full_cmd = ["sudo"] + cmd
    log_info(f"Starting: {' '.join(full_cmd)}")
    bin_path = cmd[0] if cmd else ""
    if bin_path and not os.path.exists(bin_path):
        log_error(f"Binary not found: {bin_path}")
    # Redirect stderr to a FILE, not PIPE.
    # BPF verifier dumps megabytes of log on failure, overflowing the
    # 64KB pipe buffer and blocking the process indefinitely.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stderr_path = str(LOG_DIR / f"sched-{name}-{os.getpid()}.stderr")
    stderr_f = open(stderr_path, "w")
    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_f,
        preexec_fn=os.setpgrp,
    )
    stderr_f.close()
    return SchedulerProcess(proc, name, stderr_path)


def start_and_wait(cmd: list[str], name: str) -> SchedulerProcess | None:
    """Start a scheduler, wait for sched_ext activation. Returns None on failure."""
    guard = start_scheduler(cmd, name)
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
    time.sleep(2)
    return guard


def stop_and_wait(guard: SchedulerProcess | None):
    """Stop a scheduler and wait for sched_ext deactivation."""
    if guard is None:
        return
    guard.stop()
    if not wait_for_deactivation(5.0):
        log_warn(f"sched_ext still active after stopping {guard.name}")
    time.sleep(1)


# =============================================================================
# CPU HOTPLUG
# =============================================================================

def _parse_cpu_range(path: str) -> int:
    try:
        raw = Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError):
        return os.cpu_count() or 1
    count = 0
    for r in raw.split(","):
        parts = r.split("-")
        if len(parts) == 1 and parts[0].strip().isdigit():
            count += 1
        elif len(parts) == 2:
            try:
                count += int(parts[1]) - int(parts[0]) + 1
            except ValueError:
                pass
    return count


def get_possible_cpus() -> int:
    return _parse_cpu_range("/sys/devices/system/cpu/possible")


def get_online_cpus() -> int:
    return _parse_cpu_range("/sys/devices/system/cpu/online")


def set_cpu_online(cpu: int, online: bool) -> bool:
    if cpu == 0:
        return True  # CPU 0 cannot be offlined
    path = f"/sys/devices/system/cpu/cpu{cpu}/online"
    value = "1" if online else "0"
    ret = subprocess.run(
        ["sudo", "tee", path],
        input=value, capture_output=True, text=True,
    )
    return ret.returncode == 0


def restrict_cpus(count: int, max_cpus: int) -> bool:
    for cpu in range(count, max_cpus):
        if not set_cpu_online(cpu, False):
            log_warn(f"Failed to offline CPU {cpu}")
            return False
    return True


def restore_all_cpus(max_cpus: int):
    for cpu in range(1, max_cpus):
        set_cpu_online(cpu, True)


class CpuGuard:
    """Context manager that restores all CPUs on exit."""
    def __init__(self, max_cpus: int):
        self.max_cpus = max_cpus

    def __enter__(self):
        return self

    def __exit__(self, *args):
        restore_all_cpus(self.max_cpus)


def compute_core_counts(max_cpus: int) -> list[int]:
    points = [n for n in [2, 4, 8, 16, 32, 64] if n <= max_cpus]
    if max_cpus not in points:
        points.append(max_cpus)
    return points


# =============================================================================
# STATISTICS
# =============================================================================

def mean_stdev(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)


# =============================================================================
# PHASE 1: N-WAY COMPARISON
# =============================================================================

def timed_run(cmd: str, clean_cmd: str | None = None) -> float | None:
    """Run a shell command, return wall-clock seconds or None on failure."""
    if clean_cmd:
        subprocess.run(["sh", "-c", clean_cmd],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_info(f"Running: {cmd}")
    start = time.monotonic()
    result = subprocess.run(["sh", "-c", cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:500]
        log_error(f"Command failed (exit {result.returncode}): {stderr}")
        return None
    log_info(f"Completed in {elapsed:.2f}s")
    return elapsed


def run_nway(entries: list[tuple[str, list[str] | None]],
             iterations: int,
             workload_cmd: str,
             clean_cmd: str | None) -> list[tuple[str, list[float]]]:
    """
    Run N-way comparison.

    entries: list of (name, cmd_to_spawn) where cmd_to_spawn is None for EEVDF.
    Returns: list of (name, [times]) for each scheduler that completed.
    """
    results = []

    for phase_idx, (name, sched_cmd) in enumerate(entries):
        log_info(f"Phase {phase_idx + 1}/{len(entries)}: {name}")

        # Start scheduler (EEVDF = no-op)
        guard = None
        if sched_cmd is not None:
            guard = start_and_wait(sched_cmd, name)
            if guard is None:
                continue

        times = []
        failed = False
        for i in range(iterations):
            log_info(f"  Iteration {i + 1}/{iterations}")
            t = timed_run(workload_cmd, clean_cmd)
            if t is None:
                log_warn(f"  Workload failed under {name} -- aborting this scheduler")
                failed = True
                break
            times.append(t)

        stop_and_wait(guard)

        if not failed and times:
            results.append((name, times))
        print()

    return results


def format_nway_report(results: list[tuple[str, list[float]]],
                       workload_cmd: str,
                       iterations: int) -> str:
    """Format N-way results into a text report."""
    lines = []
    lines.append("PANDEMONIUM BENCH-SCALE: N-WAY COMPARISON")
    lines.append(f"COMMAND:     {workload_cmd}")
    lines.append(f"ITERATIONS:  {iterations}")
    lines.append(f"CPUS:        {os.cpu_count()}")
    lines.append("")

    lines.append(f"{'SCHEDULER':<20} {'MEAN':>10} {'STDEV':>10} {'VS EEVDF':>12}")

    if not results:
        lines.append("(no results)")
        return "\n".join(lines)

    base_mean, _ = mean_stdev(results[0][1])

    for name, times in results:
        mean, std = mean_stdev(times)
        if name == results[0][0]:
            delta_str = "(baseline)"
        elif base_mean > 0:
            delta_pct = ((mean - base_mean) / base_mean) * 100.0
            delta_str = f"{delta_pct:+.1f}%"
        else:
            delta_str = "N/A"
        lines.append(f"{name:<20} {mean:>9.2f}s {std:>9.2f}s {delta_str:>12}")

    return "\n".join(lines)


def entries_for_cores(
    base_entries: list[tuple[str, list[str] | None]],
    n: int,
) -> list[tuple[str, list[str] | None]]:
    """Adjust scheduler commands for a specific core count.

    PANDEMONIUM gets --nr-cpus N so its internal formulas match.
    External schedulers see the online CPUs via kernel, no flag needed.
    EEVDF is None (no scheduler process).
    """
    adjusted = []
    for name, cmd in base_entries:
        if cmd is None:
            adjusted.append((name, None))
        elif name == "PANDEMONIUM":
            adjusted.append((name, cmd + ["--nr-cpus", str(n)]))
        else:
            adjusted.append((name, list(cmd)))
    return adjusted


def format_scaling_report(
    all_results: dict[int, list[tuple[str, list[float]]]],
    workload_cmd: str,
    iterations: int,
    max_cpus: int,
) -> str:
    """Format N-way scaling results: one table per core count + summary matrix."""
    lines = [
        "PANDEMONIUM BENCH-SCALE: N-WAY SCALING COMPARISON",
        f"COMMAND:     {workload_cmd}",
        f"ITERATIONS:  {iterations}",
        f"MAX CPUS:    {max_cpus}",
        "",
    ]

    sorted_cores = sorted(all_results.keys())

    for n in sorted_cores:
        results = all_results[n]
        lines.append(f"[{n} CORES]")
        lines.append(f"{'SCHEDULER':<20} {'MEAN':>10} {'STDEV':>10} {'VS EEVDF':>12}")

        if not results:
            lines.append("(no results)")
            lines.append("")
            continue

        base_mean, _ = mean_stdev(results[0][1])
        for name, times in results:
            m, std = mean_stdev(times)
            if name == results[0][0]:
                delta_str = "(baseline)"
            elif base_mean > 0:
                delta_pct = ((m - base_mean) / base_mean) * 100.0
                delta_str = f"{delta_pct:+.1f}%"
            else:
                delta_str = "N/A"
            lines.append(f"{name:<20} {m:>9.2f}s {std:>9.2f}s {delta_str:>12}")
        lines.append("")

    # Summary matrix: delta vs EEVDF at each core count
    all_schedulers: list[str] = []
    for n in sorted_cores:
        for name, _ in all_results[n]:
            if name not in all_schedulers:
                all_schedulers.append(name)

    if len(all_schedulers) > 1 and len(sorted_cores) > 1:
        baseline = all_schedulers[0]
        lines.append("SUMMARY: VS EEVDF (NEGATIVE = FASTER)")
        header = f"{'SCHEDULER':<20}"
        for n in sorted_cores:
            header += f" {str(n) + 'C':>8}"
        lines.append(header)

        for sched in all_schedulers:
            if sched == baseline:
                continue
            row = f"{sched:<20}"
            for n in sorted_cores:
                results = all_results[n]
                base_m = sched_m = None
                for name, times in results:
                    m, _ = mean_stdev(times)
                    if name == baseline:
                        base_m = m
                    if name == sched:
                        sched_m = m
                if base_m and sched_m and base_m > 0:
                    delta = ((sched_m - base_m) / base_m) * 100.0
                    row += f" {delta:>+7.1f}%"
                else:
                    row += f" {'--':>8}"
            lines.append(row)
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# BENCHMARK ARCHIVE
# =============================================================================

import json
import re


def _parse_us(s: str) -> int:
    """Parse a value like '+15701us', '-999us', '69us' into integer microseconds."""
    return int(re.sub(r"[us+]", "", s.strip()))


def parse_latency_report(text: str) -> tuple[dict, dict]:
    """Parse latency report text into structured dicts.

    Returns (latency_by_cores, adaptive_gain_by_cores).
    """
    latency: dict[str, dict] = {}
    adaptive: dict[str, dict] = {}

    current_cores = None
    in_adaptive = False

    for line in text.splitlines():
        # [LATENCY: 4 CORES]
        m = re.match(r"\[LATENCY:\s+(\d+)\s+CORES\]", line)
        if m:
            current_cores = m.group(1)
            latency[current_cores] = {}
            in_adaptive = False
            continue

        # [DELTA: ADAPTIVE GAIN ...]
        if "[DELTA:" in line:
            in_adaptive = True
            current_cores = None
            continue

        # End of structured data -- stop parsing
        if "[SUMMARY:" in line or "[SCHEDULER TELEMETRY]" in line:
            current_cores = None
            in_adaptive = False
            continue

        # Skip headers and empty lines
        if not line.strip() or "SCHEDULER" in line or ("CORES" in line and "MEDIAN" in line):
            continue

        if in_adaptive:
            # "    2     -9766us     -5995us     +7095us"
            parts = line.split()
            if len(parts) >= 4 and parts[0].isdigit():
                cores = parts[0]
                adaptive[cores] = {
                    "median_us": _parse_us(parts[1]),
                    "p99_us": _parse_us(parts[2]),
                    "worst_us": _parse_us(parts[3]),
                }
            continue

        if current_cores is not None:
            # "EEVDF                        1490       69us      170us     1175us"
            parts = line.split()
            if len(parts) >= 4:
                # Scheduler name may be multi-word: everything before the first numeric field
                i = 0
                while i < len(parts) and not parts[i].replace("us", "").lstrip("-+").isdigit():
                    i += 1
                if i == 0 or i >= len(parts):
                    continue
                name = " ".join(parts[:i])
                nums = parts[i:]
                if len(nums) >= 4:
                    latency[current_cores][name] = {
                        "samples": int(nums[0]),
                        "median_us": _parse_us(nums[1]),
                        "p99_us": _parse_us(nums[2]),
                        "worst_us": _parse_us(nums[3]),
                    }

    return latency, adaptive


def parse_knobs_report(text: str) -> dict:
    """Parse [KNOBS] lines from scheduler telemetry.

    Returns {cores: {phase: {key: value, ...}}} where phase is
    "PANDEMONIUM (BPF)" or "PANDEMONIUM (FULL)".
    """
    knobs: dict[str, dict] = {}
    current_cores = None
    current_phase = None
    in_telemetry = False

    for line in text.splitlines():
        if "[SCHEDULER TELEMETRY]" in line:
            in_telemetry = True
            continue

        if not in_telemetry:
            continue

        # "2 CORES: BPF-ONLY" or "2 CORES: BPF+ADAPTIVE"
        m = re.match(r"\s*(\d+)\s+CORES:\s+(BPF-ONLY|BPF\+ADAPTIVE)", line)
        if m:
            current_cores = m.group(1)
            raw_phase = m.group(2)
            current_phase = ("PANDEMONIUM (BPF)" if raw_phase == "BPF-ONLY"
                             else "PANDEMONIUM (FULL)")
            continue

        if "[KNOBS]" not in line:
            continue
        if current_cores is None or current_phase is None:
            continue

        # Parse key=value pairs from "[KNOBS] regime=MIXED slice_ns=4000000 ..."
        entry: dict = {}
        for km in re.finditer(r"(\w+)=(\S+)", line.split("[KNOBS]")[1]):
            k, v = km.group(1), km.group(2)
            if v == "true":
                entry[k] = True
            elif v == "false":
                entry[k] = False
            else:
                try:
                    entry[k] = int(v)
                except ValueError:
                    entry[k] = v

        # Expand ticks=L:5/M:12/H:3 into ticks_light, ticks_mixed, ticks_heavy
        if "ticks" in entry and isinstance(entry["ticks"], str):
            ticks_str = entry.pop("ticks")
            for part in ticks_str.split("/"):
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    label = {"L": "ticks_light", "M": "ticks_mixed",
                             "H": "ticks_heavy"}.get(prefix)
                    if label:
                        try:
                            entry[label] = int(val)
                        except ValueError:
                            pass

        if current_cores not in knobs:
            knobs[current_cores] = {}
        knobs[current_cores][current_phase] = entry

    return knobs


def write_archive(
    all_results: dict[int, list[tuple[str, list[float]]]],
    latency_text: str,
    iterations: int,
    max_cpus: int,
    stamp: str,
) -> Path | None:
    """Write structured JSON archive to ~/.cache/pandemonium/."""
    version = get_version()
    git = get_git_info()

    # Throughput: structured from all_results
    throughput: dict[str, dict] = {}
    for n, results in sorted(all_results.items()):
        core_key = str(n)
        throughput[core_key] = {}
        base_mean = None
        for name, times in results:
            m = sum(times) / len(times) if times else 0
            std = 0.0
            if len(times) >= 2:
                std = (sum((x - m) ** 2 for x in times) / (len(times) - 1)) ** 0.5
            entry = {"mean_s": round(m, 2), "stdev_s": round(std, 2)}
            if base_mean is None:
                base_mean = m
            elif base_mean > 0:
                entry["vs_eevdf_pct"] = round(((m - base_mean) / base_mean) * 100, 1)
            throughput[core_key][name] = entry

    # Latency + knobs: parsed from text
    latency, adaptive = parse_latency_report(latency_text) if latency_text else ({}, {})
    knobs = parse_knobs_report(latency_text) if latency_text else {}

    archive = {
        "version": version,
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "timestamp": stamp,
        "iterations": iterations,
        "max_cpus": max_cpus,
        "throughput": throughput,
        "latency": latency,
        "adaptive_gain": adaptive,
        "knobs": knobs,
    }

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{version}-{stamp}.json"
    path.write_text(json.dumps(archive, indent=2) + "\n")
    return path


# =============================================================================
# PHASE 2: PANDEMONIUM SCALING ANALYSIS
# =============================================================================

def run_scale_test(schedulers: list[str] | None = None) -> tuple[int, str]:
    """Run the Rust scaling test (cargo test --test scale).

    Returns (exit_code, latency_report_text).
    """
    log_info("Ensuring test binary is built before sudo...")
    if not build_test("scale"):
        return 1, ""

    print()
    ncpus = os.cpu_count()
    if ncpus:
        log_info(f"System CPUs: {ncpus}")
    log_info(f"Log directory: {LOG_DIR}/")
    log_info("Running benchmark (requires root for sched_ext + CPU hotplug)...")

    env = {**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)}
    if schedulers:
        env["PANDEMONIUM_SCX_SCHEDULERS"] = ",".join(schedulers)

    print()
    ret = run_cmd(
        ["sudo", "-E",
         f"CARGO_TARGET_DIR={TARGET_DIR}",
         "cargo", "test",
         "--test", "scale", "--release",
         "--", "--ignored", "--test-threads=1", "--nocapture"],
        env=env,
        cwd=SCRIPT_DIR,
    )
    print()

    fix_ownership()

    latency_text = ""
    if LOG_DIR.exists():
        logs = sorted(LOG_DIR.glob("scale-*.log"),
                      key=lambda p: p.stat().st_mtime)
        if logs:
            latest = logs[-1]
            size = latest.stat().st_size
            log_info(f"Latest log: {latest} ({size} bytes)")
            latency_text = latest.read_text()
        else:
            log_warn(f"No scale logs found in {LOG_DIR}")
    else:
        log_warn(f"Log directory does not exist: {LOG_DIR}")

    return ret, latency_text


# =============================================================================
# BENCH-SCALE COMMAND
# =============================================================================

def cmd_bench_scale(args) -> int:
    """Full benchmark: N-way scaling comparison + PANDEMONIUM latency scaling."""

    subprocess.run(["sudo", "true"])

    # Snapshot dmesg for post-run diffing
    dmesg_start = dmesg_baseline()

    # Nuke build dir if sources changed since last build
    nuke_stale_build()

    # Build PANDEMONIUM
    if not build():
        return 1

    # Stop any active sched_ext scheduler
    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping pandemonium service")
        subprocess.run(["sudo", "systemctl", "stop", "pandemonium"],
                       capture_output=True)
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext -- is another scheduler running?")
            return 1

    # RESTORE ALL CPUS FIRST: PREVIOUS RUN MAY HAVE LEFT CPUS OFFLINE.
    # USE get_possible_cpus() (NOT get_online_cpus()) SO WE RESTORE EVERYTHING.
    possible = get_possible_cpus()
    restore_all_cpus(possible)
    time.sleep(0.5)

    # PRE-FLIGHT: VERIFY PANDEMONIUM CAN LOAD BPF AND ACTIVATE
    log_info("Pre-flight: verifying PANDEMONIUM can activate...")
    preflight = start_and_wait([str(BINARY)], "PANDEMONIUM")
    if preflight is None:
        log_error("Pre-flight FAILED -- PANDEMONIUM cannot activate")
        log_error("Fix the error above before running bench-scale")
        capture_dmesg(dmesg_start, datetime.now().strftime("%Y%m%d-%H%M%S"))
        return 1
    stop_and_wait(preflight)
    log_info("Pre-flight PASSED")
    print()

    # Build entry list: EEVDF + PANDEMONIUM + externals
    base_entries: list[tuple[str, list[str] | None]] = [
        ("EEVDF", None),
        ("PANDEMONIUM", [str(BINARY)]),
    ]

    for name in args.schedulers:
        path = find_scheduler(name)
        if path:
            log_info(f"Found: {name} ({path})")
            base_entries.append((name, [name]))
        else:
            log_warn(f"SKIPPING {name} (not installed)")

    if len(base_entries) < 2:
        log_error("Need at least 2 schedulers to compare")
        return 1

    # Workload
    workload_cmd = args.cmd or f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
    clean_cmd = args.clean_cmd
    if not args.cmd:
        clean_cmd = f"cargo clean --target-dir {TARGET_DIR}"

    # Determine core counts
    max_cpus = get_online_cpus()
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
    log_info(f"Iterations: {args.iterations}")
    log_info(f"Workload: {workload_cmd}")
    n_runs = len(core_counts) * len(base_entries) * args.iterations
    log_info(f"Total runs: {n_runs}")
    print()

    # Phase 1: N-way scaling comparison (all schedulers x all core counts)
    log_info("PHASE 1: N-WAY SCALING COMPARISON")
    print()

    all_results: dict[int, list[tuple[str, list[float]]]] = {}

    with CpuGuard(max_cpus):
        # Restore all CPUs first (previous run may have left them offline)
        restore_all_cpus(max_cpus)
        time.sleep(0.5)

        for n in core_counts:
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
            results = run_nway(entries, args.iterations, workload_cmd, clean_cmd)
            all_results[n] = results

            # Restore CPUs for next round
            if n < max_cpus:
                restore_all_cpus(max_cpus)
                time.sleep(0.5)

    report = format_scaling_report(all_results, workload_cmd,
                                   args.iterations, max_cpus)
    print()
    print(report)
    print()

    # Phase 2: Latency scaling (Rust test with all schedulers)
    scale_ret = 0
    latency_text = ""
    if not args.skip_latency:
        log_info("PHASE 2: LATENCY SCALING")
        print()
        scale_ret, latency_text = run_scale_test(args.schedulers)
    else:
        log_info("Phase 2 skipped (--skip-latency)")

    # Save combined report (throughput + latency)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    combined = report + "\n"
    if latency_text:
        combined += "\n" + latency_text
    report_path = LOG_DIR / f"bench-scale-{stamp}.log"
    report_path.write_text(combined)
    log_info(f"Report saved to {report_path}")

    # Write structured archive for cross-build comparison
    archive_path = write_archive(all_results, latency_text,
                                 args.iterations, max_cpus, stamp)
    if archive_path:
        log_info(f"Archive saved to {archive_path}")

    # Capture dmesg diff (crashes, panics, scheduler errors)
    capture_dmesg(dmesg_start, stamp)

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

    if not all_results:
        return 1
    return scale_ret


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="PANDEMONIUM test orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    bench = sub.add_parser("bench-scale",
                           help="Full N-way + scaling benchmark")
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
    bench.add_argument("--skip-latency", action="store_true",
                       help="Skip Phase 2 (latency scaling)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    # Parse scheduler list
    if hasattr(args, "schedulers") and isinstance(args.schedulers, str):
        args.schedulers = [s.strip() for s in args.schedulers.split(",") if s.strip()]

    if args.command == "bench-scale":
        return cmd_bench_scale(args)

    log_error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
