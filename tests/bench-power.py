#!/usr/bin/env python3
"""
PANDEMONIUM bench-power: scheduler energy efficiency comparison.

Compares EEVDF, PANDEMONIUM (BPF), PANDEMONIUM (ADAPTIVE), and any
installed external scx schedulers on three workload classes:

  idle-floor   30s of no workload. Measures scheduler restlessness via
               idle-window package energy. A scheduler that lets idle
               cores reach C6 and stay there shows lower idle wattage
               and (when turbostat is available) higher %C6 residency.

  messaging    perf bench sched messaging -t -g 24 -l 6000. Fork-storm
               + IPC-heavy workload. Measures J/message under
               reproducible high-cadence wake load.

  schbench     schbench -m N/4 -t 4 -r 10. Latency-bounded synthetic
               workload at 4 messengers per worker over 10s. Measures
               J/sample under controlled wakeup pattern.

Per (scheduler, workload) the bench runs N independent iterations
(default 5) with a configurable cooldown between runs (default 30s).
Each run is wrapped in `perf stat -a -e power/energy-pkg/,...,cycles,
instructions` to capture run-integral package energy alongside
instructions and cycles for IPC and EPI derivation. When turbostat is
installed the bench also samples Avg_MHz, %C6 residency, PkgWatt, and
PkgTmp throughout the run.

Output: human-readable column report at
~/.cache/pandemonium/bench-power-<stamp>.log plus a sibling Prometheus
.prom matching the bench-scale emission shape so the file folds into
the existing reporting pipeline.

Usage:
    sudo ./tests/bench-power.py
    sudo ./tests/bench-power.py --workload idle-floor
    sudo ./tests/bench-power.py --runs 10 --cooldown 60
    sudo ./tests/bench-power.py --schedulers scx_lavd,scx_bpfland
    sudo ./tests/bench-power.py --pandemonium-only
    sudo ./tests/bench-power.py --no-build
"""

import argparse
import multiprocessing
import os
import re
import shutil
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
    mean_stdev,
    is_scx_active, scx_scheduler_name,
    wait_for_deactivation,
    ensure_build,
)


def _warm_sudo() -> None:
    """Prompt for sudo password if not cached. Subsequent sudo calls in
    this script will use the cached credentials. Mirrors the pattern
    used in tests/pandemonium-tests.py."""
    r = subprocess.run(["sudo", "true"])
    if r.returncode != 0:
        log_error("sudo authentication failed")
        sys.exit(1)


def _refresh_sudo() -> None:
    """Refresh cached sudo credentials. Called between long workloads to
    avoid timeout in the middle of a measurement."""
    subprocess.run(["sudo", "-v"], capture_output=True)

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler


# CONFIGURATION

DEFAULT_RUNS = 5
DEFAULT_COOLDOWN_SECS = 30
IDLE_FLOOR_SECS = 30
MESSAGING_GROUPS = 24
MESSAGING_LOOPS = 6000
SCHBENCH_RUNTIME_SECS = 10
SCHBENCH_THREADS_PER_MSG = 4

WORKLOADS = ("idle-floor", "messaging", "schbench")
DEFAULT_EXTERNALS = []

# RAPL events to probe in priority order. The first that works defines
# the primary energy reading; cores/ram are best-effort and not all
# CPUs expose them (Zen 2 / 3 typically expose pkg only).
RAPL_EVENT_CANDIDATES = [
    "power/energy-pkg/",
    "power/energy-cores/",
    "power/energy-ram/",
]

# Turbostat columns we care about. Filtered down so the subprocess
# emits a small, parsable table.
TURBOSTAT_COLUMNS = "CPU,Avg_MHz,Busy%,IRQ,POLL%,C1%,C1E%,C3%,C6%,Pkg%pc6,PkgWatt,CorWatt,PkgTmp"


# CAPABILITY PROBES

def detect_cpu_vendor() -> str:
    """Read /proc/cpuinfo, return 'AMD' / 'Intel' / 'unknown'."""
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return "unknown"
    if "AuthenticAMD" in text:
        return "AMD"
    if "GenuineIntel" in text:
        return "Intel"
    return "unknown"


def detect_cpu_model() -> str:
    """First 'model name' from cpuinfo, trimmed."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def detect_perf_power_events() -> list[str]:
    """Probe each candidate RAPL event with a tiny `sleep 0.05` workload.
    Returns the events that perf accepted. Empty list -> RAPL unavailable.
    Runs under sudo because perf_event_paranoid=2 (default) blocks RAPL
    reads from the user PMU."""
    available = []
    for ev in RAPL_EVENT_CANDIDATES:
        r = subprocess.run(
            ["sudo", "perf", "stat", "-a", "-e", ev, "--", "sleep", "0.05"],
            capture_output=True, text=True,
        )
        # perf prints "<not supported>" when the event exists in the PMU but
        # the CPU doesn't expose it; "Invalid argument" when the event isn't
        # registered. Either way, skip.
        if r.returncode == 0 and "<not supported>" not in r.stderr \
                and "Invalid argument" not in r.stderr:
            available.append(ev)
    return available


def detect_turbostat() -> str | None:
    return shutil.which("turbostat")


def detect_schbench() -> str | None:
    return shutil.which("schbench")


# PERF STAT WRAPPER

def parse_perf_stat_output(stderr_text: str) -> dict:
    """Parse perf stat -a output (everything goes to stderr).

    perf prints one event per line, in formats like:
        12.34 Joules power/energy-pkg/
        1,234,567 cycles
        2,345,678 instructions
    Locale separators (commas) are stripped before float() conversion.
    """
    counters: dict[str, float] = {}
    for raw in stderr_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Skip section headers / metadata
        if line.startswith("Performance counter"):
            continue
        if line.startswith("seconds time elapsed"):
            continue
        # Greedy match: number, optional unit word, event name (rest of line
        # up to whitespace).
        m = re.match(r"^([\d,]+(?:\.\d+)?)\s+(?:[A-Za-z%/]+\s+)?(\S+)", line)
        if not m:
            continue
        val_str = m.group(1).replace(",", "")
        name = m.group(2)
        try:
            val = float(val_str)
        except ValueError:
            continue
        # perf may suffix events with :u or :k for user/kernel filters
        name = name.rstrip(":u").rstrip(":k")
        counters[name] = val
    return counters


def run_perf_workload(workload_cmd: list[str], power_events: list[str],
                      timeout: float = 600.0) -> tuple[float, dict] | None:
    """Run a workload under `perf stat -a` with the given RAPL events plus
    cycles + instructions. Returns (wall_s, counters_dict) or None on
    error.

    counters_dict keys are perf event names: 'power/energy-pkg/',
    'cycles', 'instructions', etc."""
    events = ",".join(power_events + ["cycles", "instructions"])
    cmd = ["sudo", "perf", "stat", "-a", "-e", events, "--"] + workload_cmd
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log_error(f"perf stat workload timed out after {timeout}s")
        return None
    wall_s = time.monotonic() - t0
    if r.returncode != 0:
        log_error(f"perf stat exit {r.returncode}: {r.stderr.strip()[:300]}")
        return None
    counters = parse_perf_stat_output(r.stderr)
    return wall_s, counters


# TURBOSTAT (OPTIONAL BACKGROUND SAMPLER)

class TurbostatSampler:
    """Run turbostat in the background, capture rows to a tempfile, parse
    aggregates on stop. Skip silently if turbostat is missing.

    turbostat must be SIGINT'd to flush its output -- it does not buffer
    cleanly under SIGTERM.
    """

    def __init__(self, turbostat_path: str | None, label: str):
        self.path = turbostat_path
        self.label = label
        self.proc: subprocess.Popen | None = None
        self.out_file = None
        self.out_path: Path | None = None

    def start(self) -> None:
        if self.path is None:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.out_path = LOG_DIR / f"turbostat-{self.label}-{os.getpid()}.tmp"
        self.out_file = open(self.out_path, "w")
        cmd = [
            "sudo", self.path, "--quiet",
            "--interval", "1",
            "--show", TURBOSTAT_COLUMNS,
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=self.out_file,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp,
            )
        except (FileNotFoundError, PermissionError) as e:
            log_warn(f"turbostat failed to start: {e}")
            self.proc = None
            self.out_file.close()
            self.out_file = None

    def stop(self) -> dict:
        if self.proc is None:
            return {}
        # turbostat runs as root via sudo and ignores SIGTERM in some
        # versions; SIGINT triggers its clean-flush path. Cross-uid
        # signaling requires sudo. Send to the negative pgid so both the
        # sudo wrapper and turbostat receive it.
        try:
            pgid = os.getpgid(self.proc.pid)
            subprocess.run(
                ["sudo", "kill", "-INT", "--", f"-{pgid}"],
                capture_output=True,
            )
        except (ProcessLookupError, OSError):
            pgid = None
        try:
            self.proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                subprocess.run(
                    ["sudo", "kill", "-KILL", "--", f"-{pgid}"],
                    capture_output=True,
                )
            self.proc.wait()
        if self.out_file:
            self.out_file.close()
        if self.out_path and self.out_path.exists():
            text = self.out_path.read_text()
            try:
                self.out_path.unlink()
            except OSError:
                pass
            return parse_turbostat_output(text)
        return {}


def parse_turbostat_output(text: str) -> dict:
    """Parse turbostat stdout. We want package-summary rows (CPU == '-')
    and average across them.

    turbostat emits a header row, then per-CPU rows, then a summary row
    where the CPU column is '-'. Each sample interval emits a fresh
    block.
    """
    if not text:
        return {}
    header_cols: list[str] | None = None
    summaries: list[dict[str, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # First non-empty line that includes "CPU" is the header
        if header_cols is None:
            if "CPU" in line and "Avg_MHz" in line:
                header_cols = line.split()
            continue
        # Re-detection of repeated headers between sample blocks
        if line.startswith("CPU\t") or line.split()[0] == "CPU":
            header_cols = line.split()
            continue
        cols = line.split()
        if not cols or len(cols) != len(header_cols):
            continue
        # Summary row: CPU column == '-'
        if cols[0] != "-":
            continue
        row: dict[str, float] = {}
        for name, raw_val in zip(header_cols, cols):
            try:
                row[name] = float(raw_val)
            except ValueError:
                pass
        summaries.append(row)

    if not summaries:
        return {}

    # Average each numeric column across the sample summaries
    keys = set()
    for row in summaries:
        keys.update(row.keys())
    out: dict[str, float] = {}
    for key in keys:
        vals = [row[key] for row in summaries if key in row]
        if vals:
            out[key] = sum(vals) / len(vals)
    out["_samples"] = len(summaries)
    return out


# WORKLOAD RUNNERS

def run_idle_floor(power_events: list[str], secs: int,
                   turbostat_path: str | None) -> dict:
    """Run nothing for `secs` seconds under perf stat -a + turbostat.

    work_unit = elapsed seconds (since there's no other notion of work
    here -- we want average wattage during idle)."""
    sampler = TurbostatSampler(turbostat_path, "idle")
    sampler.start()
    result = run_perf_workload(["sleep", str(secs)], power_events,
                               timeout=secs + 30)
    ts = sampler.stop()
    if result is None:
        return {}
    wall_s, counters = result
    return {
        "wall_s": wall_s,
        "counters": counters,
        "turbostat": ts,
        "work_unit": wall_s,
        "work_unit_label": "idle-second",
    }


def run_messaging(power_events: list[str],
                  turbostat_path: str | None) -> dict:
    """perf bench sched messaging under perf stat. Work unit = number of
    messages exchanged. Each group has 1 sender talking to 20 receivers
    by default, looped NR_LOOPS times. We treat the work unit as
    (groups * loops * 20 * 2) -- each loop is a send+receive in both
    directions."""
    cmd = [
        "perf", "bench", "-f", "simple", "sched", "messaging",
        "-t",  # threads instead of processes (lighter weight)
        "-g", str(MESSAGING_GROUPS),
        "-l", str(MESSAGING_LOOPS),
    ]
    sampler = TurbostatSampler(turbostat_path, "messaging")
    sampler.start()
    result = run_perf_workload(cmd, power_events, timeout=600)
    ts = sampler.stop()
    if result is None:
        return {}
    wall_s, counters = result
    # 20 receivers per group is the perf bench default
    work_unit = MESSAGING_GROUPS * MESSAGING_LOOPS * 20 * 2
    return {
        "wall_s": wall_s,
        "counters": counters,
        "turbostat": ts,
        "work_unit": work_unit,
        "work_unit_label": "message",
    }


def run_schbench(power_events: list[str], n_cpus: int,
                 turbostat_path: str | None) -> dict:
    """schbench -m <msgr> -t <thr> -r <secs> -F128 -n5
    msgr = max(1, n_cpus // 4) so we don't oversubscribe; threads
    SCHBENCH_THREADS_PER_MSG.
    Work unit = total samples reported by schbench."""
    msgr = max(1, n_cpus // 4)
    cmd = [
        "schbench",
        "-m", str(msgr),
        "-t", str(SCHBENCH_THREADS_PER_MSG),
        "-r", str(SCHBENCH_RUNTIME_SECS),
        "-F", "128",
        "-n", "5",
    ]
    sampler = TurbostatSampler(turbostat_path, "schbench")
    sampler.start()
    # schbench emits its histogram on stderr; we want to capture it for
    # the sample count. Use Popen + perf wrapper manually so we get both
    # streams.
    events = ",".join(power_events + ["cycles", "instructions"])
    full_cmd = ["sudo", "perf", "stat", "-a", "-e", events, "--"] + cmd
    t0 = time.monotonic()
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True,
                           timeout=SCHBENCH_RUNTIME_SECS + 60)
    except subprocess.TimeoutExpired:
        log_error("schbench timed out")
        sampler.stop()
        return {}
    wall_s = time.monotonic() - t0
    ts = sampler.stop()
    if r.returncode != 0:
        log_error(f"schbench exit {r.returncode}: {r.stderr.strip()[:300]}")
        return {}
    counters = parse_perf_stat_output(r.stderr)
    samples = parse_schbench_samples(r.stdout + "\n" + r.stderr)
    return {
        "wall_s": wall_s,
        "counters": counters,
        "turbostat": ts,
        "work_unit": samples or 1,
        "work_unit_label": "sample",
    }


def parse_schbench_samples(text: str) -> int:
    """schbench prints something like 'Wakeup Latencies: ... samples=1234'
    or just a histogram header with a sample count. Parse the total."""
    for line in text.splitlines():
        m = re.search(r"samples\s*[:=]\s*(\d+)", line)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s+samples", line)
        if m:
            return int(m.group(1))
    return 0


# COOLDOWN

def cooldown(secs: int) -> None:
    """Sleep with periodic dot output so the user knows we're alive."""
    if secs <= 0:
        return
    log_info(f"  cooldown: {secs}s")
    elapsed = 0
    while elapsed < secs:
        step = min(5, secs - elapsed)
        time.sleep(step)
        elapsed += step


# AGGREGATION

def aggregate_runs(runs: list[dict]) -> dict:
    """Take a list of per-run result dicts (from run_idle_floor / etc.) and
    return aggregated mean/stddev across runs. Empty/failed runs are
    excluded."""
    valid = [r for r in runs if r and "counters" in r]
    if not valid:
        return {"runs": 0}

    out: dict = {"runs": len(valid)}

    # Wall time
    walls = [r["wall_s"] for r in valid]
    out["wall_s_mean"], out["wall_s_stdev"] = mean_stdev(walls)

    # Work unit (assume same label across runs)
    out["work_unit_label"] = valid[0].get("work_unit_label", "op")
    out["work_unit_mean"] = sum(r.get("work_unit", 0) for r in valid) / len(valid)

    # Energy and counter aggregation
    counter_names: set[str] = set()
    for r in valid:
        counter_names.update(r.get("counters", {}).keys())
    for cn in counter_names:
        vals = [r["counters"][cn] for r in valid if cn in r.get("counters", {})]
        if not vals:
            continue
        m, sd = mean_stdev(vals)
        out[f"{cn}_mean"] = m
        out[f"{cn}_stdev"] = sd

    # Turbostat aggregation
    ts_keys: set[str] = set()
    for r in valid:
        ts_keys.update(r.get("turbostat", {}).keys())
    for tk in ts_keys:
        if tk == "_samples":
            continue
        vals = [r["turbostat"][tk] for r in valid if tk in r.get("turbostat", {})]
        if not vals:
            continue
        m, sd = mean_stdev(vals)
        out[f"ts_{tk}_mean"] = m
        out[f"ts_{tk}_stdev"] = sd

    # Derived: J/work_unit, IPC, EPI (joules per instruction)
    j_pkg = out.get("power/energy-pkg/_mean", 0.0)
    ins = out.get("instructions_mean", 0.0)
    cyc = out.get("cycles_mean", 0.0)
    work = out["work_unit_mean"]
    if j_pkg > 0 and work > 0:
        out["j_per_op_mean"] = j_pkg / work
    if ins > 0 and cyc > 0:
        out["ipc_mean"] = ins / cyc
    if j_pkg > 0 and ins > 0:
        out["epi_mean"] = j_pkg / ins
    if j_pkg > 0 and out["wall_s_mean"] > 0:
        out["avg_watts_mean"] = j_pkg / out["wall_s_mean"]

    return out


# REPORT

def _fmt(val, width: int, prec: int = 2, suffix: str = "") -> str:
    if val is None:
        return f"{'n/a':>{width}}"
    if isinstance(val, str):
        return f"{val:>{width}}"
    if abs(val) < 0.001 and val != 0:
        return f"{val:>{width}.4g}{suffix}"
    return f"{val:>{width}.{prec}f}{suffix}"


def write_report(version: str, git: dict, stamp: str, ncpus: int,
                 vendor: str, model: str, power_events: list[str],
                 turbostat_available: bool, runs: int,
                 cooldown_secs: int, all_results: dict) -> Path:
    """Render a column-table summary suitable for committing into the
    archive."""
    R: list[str] = []
    dirty = " (dirty)" if git["dirty"] else ""
    R.append(f"PANDEMONIUM bench-power v{version} [{git['commit']}{dirty}]")
    R.append(f"cpu:        {model} ({vendor})")
    R.append(f"ncpus:      {ncpus}")
    R.append(f"runs:       {runs} per (scheduler, workload)")
    R.append(f"cooldown:   {cooldown_secs}s between runs")
    R.append(f"rapl events: {', '.join(power_events) or 'none'}")
    R.append(f"turbostat:  {'available' if turbostat_available else 'missing (install linux-cpupower for full profile)'}")
    R.append("")

    workloads = sorted({wl for results in all_results.values() for wl in results})
    if not workloads:
        R.append("(no successful workload runs)")
        path = LOG_DIR / f"bench-power-{stamp}.log"
        path.write_text("\n".join(R) + "\n")
        return path

    sched_order = list(all_results.keys())
    eevdf_per_workload: dict[str, dict] = {}
    if "EEVDF" in all_results:
        eevdf_per_workload = all_results["EEVDF"]

    for wl in workloads:
        R.append(f"[{wl.upper()}]")
        # ENERGY TABLE
        header = (f"{'SCHEDULER':<26} {'RUNS':>5} {'WALL_S':>10} "
                  f"{'J_PKG':>10} {'AVG_W':>8} {'J/OP':>14} {'IPC':>7} "
                  f"{'EPI_pJ':>9}")
        R.append(header)
        for sn in sched_order:
            sd = all_results[sn].get(wl)
            if not sd or sd.get("runs", 0) == 0:
                R.append(f"{sn:<26} {'FAIL':>5}")
                continue
            row = f"{sn:<26} {sd['runs']:>5d}"
            row += f" {_fmt(sd.get('wall_s_mean'), 9, 2)}s"
            row += f" {_fmt(sd.get('power/energy-pkg/_mean'), 9, 2)}J"
            row += f" {_fmt(sd.get('avg_watts_mean'), 7, 2)}W"
            j_per_op = sd.get("j_per_op_mean")
            if j_per_op is not None:
                # Scale to convenient unit: nJ for high-rate ops, mJ otherwise
                if j_per_op < 1e-6:
                    row += f" {j_per_op*1e9:>11.2f}nJ"
                elif j_per_op < 1e-3:
                    row += f" {j_per_op*1e6:>11.2f}uJ"
                elif j_per_op < 1:
                    row += f" {j_per_op*1e3:>11.2f}mJ"
                else:
                    row += f" {j_per_op:>11.2f}J "
            else:
                row += f" {'n/a':>14}"
            row += f" {_fmt(sd.get('ipc_mean'), 7, 3)}"
            epi = sd.get("epi_mean")
            if epi is not None:
                row += f" {epi*1e12:>7.2f}pJ"
            else:
                row += f" {'n/a':>9}"
            R.append(row)
        R.append("")

        # FREQUENCY / IDLE TABLE (turbostat)
        ts_avail = any(
            "ts_Avg_MHz_mean" in (all_results[sn].get(wl) or {})
            for sn in sched_order
        )
        if ts_avail:
            R.append(f"{'SCHEDULER':<26} {'AVG_MHZ':>9} {'BUSY%':>7} "
                     f"{'C1%':>7} {'C6%':>7} {'PKG_PC6%':>9} {'PKGTMP':>8}")
            for sn in sched_order:
                sd = all_results[sn].get(wl) or {}
                if "ts_Avg_MHz_mean" not in sd:
                    R.append(f"{sn:<26} {'n/a':>9}")
                    continue
                row = f"{sn:<26}"
                row += f" {sd.get('ts_Avg_MHz_mean', 0):>9.0f}"
                row += f" {sd.get('ts_Busy%_mean', 0):>7.1f}"
                row += f" {sd.get('ts_C1%_mean', 0):>7.1f}"
                row += f" {sd.get('ts_C6%_mean', 0):>7.1f}"
                row += f" {sd.get('ts_Pkg%pc6_mean', 0):>9.1f}"
                row += f" {sd.get('ts_PkgTmp_mean', 0):>7.1f}C"
                R.append(row)
            R.append("")

        # RELATIVE TO EEVDF
        eevdf_sd = eevdf_per_workload.get(wl) if eevdf_per_workload else None
        if eevdf_sd and eevdf_sd.get("runs", 0) > 0:
            R.append(f"{'SCHEDULER':<26} {'WALL_VS_EEVDF':>14} "
                     f"{'J_VS_EEVDF':>12} {'J/OP_VS_EEVDF':>15}")
            base_wall = eevdf_sd.get("wall_s_mean", 0)
            base_j = eevdf_sd.get("power/energy-pkg/_mean", 0)
            base_jop = eevdf_sd.get("j_per_op_mean", 0)
            for sn in sched_order:
                if sn == "EEVDF":
                    R.append(f"{sn:<26} {'(baseline)':>14}")
                    continue
                sd = all_results[sn].get(wl)
                if not sd or sd.get("runs", 0) == 0:
                    R.append(f"{sn:<26} {'FAIL':>14}")
                    continue
                row = f"{sn:<26}"
                w = sd.get("wall_s_mean", 0)
                j = sd.get("power/energy-pkg/_mean", 0)
                jop = sd.get("j_per_op_mean", 0)
                if base_wall > 0 and w > 0:
                    d = (w - base_wall) / base_wall * 100
                    sign = "+" if d > 0 else ""
                    row += f" {sign}{d:>12.1f}%"
                else:
                    row += f" {'n/a':>14}"
                if base_j > 0 and j > 0:
                    d = (j - base_j) / base_j * 100
                    sign = "+" if d > 0 else ""
                    row += f" {sign}{d:>10.1f}%"
                else:
                    row += f" {'n/a':>12}"
                if base_jop > 0 and jop > 0:
                    d = (jop - base_jop) / base_jop * 100
                    sign = "+" if d > 0 else ""
                    row += f" {sign}{d:>13.1f}%"
                else:
                    row += f" {'n/a':>15}"
                R.append(row)
            R.append("")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bench-power-{stamp}.log"
    path.write_text("\n".join(R) + "\n")
    return path


# PROMETHEUS

def write_prometheus(version: str, git: dict, stamp: str, ncpus: int,
                     vendor: str, model: str, power_events: list[str],
                     runs: int, cooldown_secs: int,
                     all_results: dict) -> Path:
    lines: list[str] = []
    emitted: set[str] = set()

    def gauge(name: str, help_text: str, value, labels: dict | None = None) -> None:
        if name not in emitted:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            emitted.add(name)
        if labels:
            ls = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{ls}}} {value}")
        else:
            lines.append(f"{name} {value}")

    dirty = "true" if git["dirty"] else "false"
    gauge("pandemonium_bench_power_info", "Build and run metadata", 1,
          {"version": version, "git_commit": git["commit"], "git_dirty": dirty,
           "vendor": vendor, "model": model})
    gauge("pandemonium_bench_power_timestamp_seconds", "Test start time",
          int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()))
    gauge("pandemonium_bench_power_cpus", "CPUs available", ncpus)
    gauge("pandemonium_bench_power_runs", "Iterations per cell", runs)
    gauge("pandemonium_bench_power_cooldown_seconds", "Cooldown between runs",
          cooldown_secs)
    gauge("pandemonium_bench_power_rapl_events", "RAPL events used",
          len(power_events))

    for sched_name, by_workload in all_results.items():
        for wl, sd in by_workload.items():
            if not sd or sd.get("runs", 0) == 0:
                continue
            ls = {"scheduler": sched_name, "workload": wl}
            gauge("pandemonium_bench_power_iterations",
                  "Successful run count for this cell",
                  sd["runs"], ls)
            gauge("pandemonium_bench_power_wall_seconds",
                  "Wall-clock time (mean)",
                  f"{sd.get('wall_s_mean', 0):.4f}", ls)
            gauge("pandemonium_bench_power_wall_seconds_stdev",
                  "Wall-clock time (stddev)",
                  f"{sd.get('wall_s_stdev', 0):.4f}", ls)
            gauge("pandemonium_bench_power_joules_pkg",
                  "Package energy (mean joules)",
                  f"{sd.get('power/energy-pkg/_mean', 0):.4f}", ls)
            gauge("pandemonium_bench_power_joules_pkg_stdev",
                  "Package energy (stddev joules)",
                  f"{sd.get('power/energy-pkg/_stdev', 0):.4f}", ls)
            cores = sd.get("power/energy-cores/_mean")
            if cores is not None:
                gauge("pandemonium_bench_power_joules_cores",
                      "Cores energy (mean joules)",
                      f"{cores:.4f}", ls)
            ram = sd.get("power/energy-ram/_mean")
            if ram is not None:
                gauge("pandemonium_bench_power_joules_ram",
                      "RAM energy (mean joules)",
                      f"{ram:.4f}", ls)
            if "avg_watts_mean" in sd:
                gauge("pandemonium_bench_power_avg_watts",
                      "Average package wattage during run",
                      f"{sd['avg_watts_mean']:.4f}", ls)
            if "j_per_op_mean" in sd:
                gauge("pandemonium_bench_power_joules_per_op",
                      "Joules per work unit (label varies per workload)",
                      f"{sd['j_per_op_mean']:.6e}", ls)
                gauge("pandemonium_bench_power_work_unit_count",
                      "Total work units in run (mean)",
                      f"{sd.get('work_unit_mean', 0):.0f}", ls)
            if "ipc_mean" in sd:
                gauge("pandemonium_bench_power_ipc",
                      "Instructions per cycle (mean)",
                      f"{sd['ipc_mean']:.4f}", ls)
            if "epi_mean" in sd:
                gauge("pandemonium_bench_power_epi_joules",
                      "Energy per instruction (mean joules)",
                      f"{sd['epi_mean']:.4e}", ls)
            for ts_key in ("Avg_MHz", "Busy%", "C1%", "C6%", "Pkg%pc6",
                           "PkgWatt", "PkgTmp"):
                key = f"ts_{ts_key}_mean"
                if key not in sd:
                    continue
                safe = ts_key.replace("%", "_pct").replace("/", "_")
                gauge(f"pandemonium_bench_power_turbostat_{safe}",
                      f"turbostat {ts_key} averaged across run samples",
                      f"{sd[key]:.4f}", ls)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"bench-power-{version}-{stamp}.prom"
    path.write_text("\n".join(lines) + "\n")
    return path


# SCHEDULER ENTRIES

def build_entries(args, has_schbench: bool) -> list[tuple[str, list[str] | None]]:
    """Returns ordered list of (display_name, cmd_or_None) entries.
    None command means EEVDF (default kernel scheduler)."""
    entries: list[tuple[str, list[str] | None]] = []
    if not args.no_eevdf and not args.pandemonium_only:
        entries.append(("EEVDF", None))
    if not args.no_pandemonium:
        # Production-realistic activation: no --verbose, the periodic
        # logging would itself contaminate idle-floor measurements.
        entries.append(("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]))
        entries.append(("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]))
    if not args.pandemonium_only:
        for name in args.schedulers:
            path = find_scheduler(name)
            if path:
                log_info(f"Found: {name} ({path})")
                entries.append((name, [name]))
            else:
                log_warn(f"SKIPPING {name} (not installed)")
    return entries


def select_workloads(args, has_schbench: bool) -> list[str]:
    if args.workload != "all":
        if args.workload == "schbench" and not has_schbench:
            log_warn("schbench not installed; skipping")
            return []
        return [args.workload]
    out = ["idle-floor", "messaging"]
    if has_schbench:
        out.append("schbench")
    else:
        log_warn("schbench not installed; skipping schbench workload")
    return out


# MAIN

def run_workload(wl: str, power_events: list[str], n_cpus: int,
                 turbostat_path: str | None) -> dict:
    if wl == "idle-floor":
        return run_idle_floor(power_events, IDLE_FLOOR_SECS, turbostat_path)
    if wl == "messaging":
        return run_messaging(power_events, turbostat_path)
    if wl == "schbench":
        return run_schbench(power_events, n_cpus, turbostat_path)
    log_error(f"unknown workload: {wl}")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workload", choices=("all",) + WORKLOADS, default="all",
                    help="Which workload to run (default: all)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"Iterations per (scheduler, workload) cell (default {DEFAULT_RUNS})")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_SECS,
                    help=f"Seconds between runs (default {DEFAULT_COOLDOWN_SECS})")
    ap.add_argument("--schedulers", default=",".join(DEFAULT_EXTERNALS),
                    help="Comma-separated external scx schedulers")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="Only test PANDEMONIUM (BPF + ADAPTIVE), no EEVDF or external scx")
    ap.add_argument("--no-eevdf", action="store_true",
                    help="Skip EEVDF baseline")
    ap.add_argument("--no-pandemonium", action="store_true",
                    help="Skip PANDEMONIUM (debugging external schedulers)")
    ap.add_argument("--no-build", action="store_true",
                    help="Skip ensure_build (use existing binary)")
    args = ap.parse_args()
    args.schedulers = [s.strip() for s in args.schedulers.split(",") if s.strip()]

    _warm_sudo()
    if not shutil.which("perf"):
        log_error("perf not found. Install with: sudo pacman -S perf")
        return 1

    if not args.no_build and not args.no_pandemonium:
        ensure_build()

    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n_cpus = multiprocessing.cpu_count()
    vendor = detect_cpu_vendor()
    model = detect_cpu_model()

    log_info(f"bench-power v{ver} [{git['commit']}{' (dirty)' if git['dirty'] else ''}]")
    log_info(f"CPU: {model} ({vendor})  ncpus: {n_cpus}")

    power_events = detect_perf_power_events()
    if not power_events:
        log_error("No RAPL events accessible via perf. Verify CONFIG_X86_RAPL "
                  "and that perf can read MSRs as root.")
        return 1
    log_info(f"RAPL events: {', '.join(power_events)}")

    turbostat_path = detect_turbostat()
    if turbostat_path:
        log_info(f"turbostat: {turbostat_path}")
    else:
        log_warn("turbostat not installed; install linux-cpupower for "
                 "frequency / C-state / temperature profile")

    schbench_path = detect_schbench()
    if schbench_path:
        log_info(f"schbench: {schbench_path}")
    else:
        log_warn("schbench not installed; schbench workload will be skipped")

    workloads = select_workloads(args, schbench_path is not None)
    if not workloads:
        log_error("No workloads to run")
        return 1
    log_info(f"workloads: {', '.join(workloads)}  runs/cell: {args.runs}  "
             f"cooldown: {args.cooldown}s")
    print()

    # Disengage any active sched_ext before starting (matches bench-fork-thread)
    if is_scx_active():
        active = scx_scheduler_name()
        log_warn(f"sched_ext active ({active}) -- stopping pandemonium service")
        subprocess.run(["sudo", "systemctl", "stop", "pandemonium"],
                       capture_output=True)
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext")
            return 1
    time.sleep(1)

    entries = build_entries(args, schbench_path is not None)
    if not entries:
        log_error("No schedulers selected")
        return 1
    log_info(f"schedulers: {', '.join(name for name, _ in entries)}")
    print()

    all_results: dict[str, dict[str, dict]] = {}

    try:
        for sched_name, cmd in entries:
            all_results[sched_name] = {}
            log_info(f"[{sched_name}] starting...")
            _refresh_sudo()

            guard = None
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"{sched_name}: failed to activate, skipping")
                    print()
                    continue

            try:
                for wl in workloads:
                    log_info(f"  [{wl}] {args.runs} runs")
                    runs: list[dict] = []
                    for i in range(args.runs):
                        log_info(f"    run {i+1}/{args.runs}...")
                        result = run_workload(wl, power_events, n_cpus,
                                              turbostat_path)
                        if result and "counters" in result:
                            j = result["counters"].get("power/energy-pkg/", 0)
                            log_info(f"      wall={result['wall_s']:.2f}s "
                                     f"J_pkg={j:.2f}")
                        else:
                            log_warn("      run failed")
                        runs.append(result)
                        if i < args.runs - 1:
                            cooldown(args.cooldown)

                    agg = aggregate_runs(runs)
                    all_results[sched_name][wl] = agg
                    if agg.get("runs", 0) > 0:
                        log_info(f"  [{wl}] mean: "
                                 f"wall={agg.get('wall_s_mean', 0):.2f}s "
                                 f"J_pkg={agg.get('power/energy-pkg/_mean', 0):.2f} "
                                 f"avg_W={agg.get('avg_watts_mean', 0):.2f}")
                    if wl != workloads[-1]:
                        cooldown(args.cooldown)
            finally:
                if guard is not None:
                    stop_and_wait(guard)
                # Inter-scheduler cooldown to drain residual state
                cooldown(args.cooldown)
            print()
    except KeyboardInterrupt:
        log_warn("interrupted")
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    if any(by_wl for by_wl in all_results.values()):
        print()
        report_path = write_report(ver, git, stamp, n_cpus, vendor, model,
                                   power_events, turbostat_path is not None,
                                   args.runs, args.cooldown, all_results)
        prom_path = write_prometheus(ver, git, stamp, n_cpus, vendor, model,
                                     power_events, args.runs, args.cooldown,
                                     all_results)
        print(report_path.read_text())
        log_info(f"Report: {report_path}")
        log_info(f"Prometheus: {prom_path}")
    else:
        log_error("No successful results to report")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log_warn("Interrupted")
        sys.exit(130)
