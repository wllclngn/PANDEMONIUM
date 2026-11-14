#!/usr/bin/env python3
"""
PANDEMONIUM prism-power: scheduler energy efficiency comparison.

Compares EEVDF, PANDEMONIUM (BPF), PANDEMONIUM (ADAPTIVE), and any
installed external scx schedulers on four workload classes:

  idle-floor   30s of no workload. Measures scheduler restlessness via
               idle-window package energy. A scheduler that lets idle
               cores reach their deepest idle state and stay there shows
               lower idle wattage and more residency in that state.

  messaging    perf bench sched messaging -t -g 24 -l 6000. Fork-storm
               + IPC-heavy workload. Measures J/message under
               reproducible high-cadence wake load.

  paced        30s of prism-frame's paced scenario: framebench capped at
               144 fps, its work per frame calibrated once on EEVDF before
               any arm loads. Fixed work at a fixed rate on a partly loaded
               box, a game, where placement decides how many cores wake, how
               deep idle goes and how high clocks run. Measures J/frame on
               the same row as the frame bench's late-frame share and 1% low.

  schbench     schbench -m N/4 -t 4 -r 10. Latency-bounded synthetic
               workload at 4 messengers per worker over 10s. Measures
               J/sample under controlled wakeup pattern.

Per (scheduler, workload) the bench runs N independent iterations
(default 5) with a configurable cooldown between runs (default 30s).
montauk records each run in a directory of its own, scraping every 250ms
with its thermal collector sampling at the same rate. Package energy is
the run's delta of montauk's cumulative RAPL counter, instructions and
cycles come from its system-wide PMU for IPC and EPI derivation, and the
same recording carries the cpufreq clock, the CPU temperature and each
cpuidle state's residency. montauk --analyze reduces every recording.

Output: human-readable column report at
~/.cache/pandemonium/prism-power-<stamp>.log plus a sibling Prometheus
.prom matching the prism-scale emission shape so the file folds into
the existing reporting pipeline. montauk's recordings stay under
~/.cache/pandemonium/power/<stamp>/, one directory per run.

Usage:
    sudo ./tests/prism-power.py
    sudo ./tests/prism-power.py --workload idle-floor
    sudo ./tests/prism-power.py --workload paced
    sudo ./tests/prism-power.py --iterations 10 --cooldown 60
    sudo ./tests/prism-power.py --schedulers scx_lavd,scx_bpfland
    sudo ./tests/prism-power.py --pandemonium-only
    sudo ./tests/prism-power.py --no-build
"""

import argparse
import importlib.util
import multiprocessing
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    start_and_wait, stop_and_wait, clear_loaded_scheduler,
    LOG_DIR, ARCHIVE_DIR, add_field_flags, field_of,
    get_version, get_git_info,
    log, log_info, log_warn, log_error,
    mean_stdev, mean,
    is_scx_active,
    wait_for_deactivation, get_online_cpus,
    ensure_build, PrometheusBuilder,
    MONTAUK, montauk_available, montauk_has_flag, montauk_analyze_argv,
    montauk_envelope, run_cmd_capture, _chown_to_invoking_user,
    MONTAUK_RECORD_MS, MONTAUK_RECORD_FLAGS, MontaukRecorder, stop_recorders,
    record_run, montauk_population, counter_delta,
 warm_sudo, refresh_sudo,)

sys.path.insert(0, str(Path(__file__).parent.resolve()))


def load_frame():
    """prism-frame, whose framebench build, calibration, paced scenario and
    run parse the paced workload runs through. A hyphenated name, so it is
    loaded by path."""
    spec = importlib.util.spec_from_file_location(
        "prism_frame", str(Path(__file__).parent / "prism-frame.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


FRAME = load_frame()


# CONFIGURATION

DEFAULT_RUNS = 5
DEFAULT_COOLDOWN_SECS = 30
IDLE_FLOOR_SECS = 30
MESSAGING_GROUPS = 24
MESSAGING_LOOPS = 6000
SCHBENCH_RUNTIME_SECS = 10
SCHBENCH_THREADS_PER_MSG = 4
# idle-floor's 30s. prism-frame's 15s window suits a throughput read; here
# each end of the energy delta sits within one 250ms scrape of the run's, so
# the longer run halves that share, and at 144 fps it leaves 43 frames in
# the slowest 1% the 1% low is read from.
PACED_SECS = 30

WORKLOADS = ("idle-floor", "messaging", "paced", "schbench")

POWER_DIR = LOG_DIR / "power"


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


def detect_schbench() -> str | None:
    return shutil.which("schbench")


def montauk_version() -> str:
    rc, so, _se = run_cmd_capture([*montauk_analyze_argv(), "--version"])
    return so.strip() if rc == 0 and so.strip() else "unknown"


# MONTAUK RECORDING

_CSTATE = re.compile(r"(?:^| )state=(\S+)")


def cstate_means(cells: list[tuple[str, list[dict]]]) -> dict[str, float]:
    """{cpuidle state: mean residency %} over the recording's scrapes. Each
    group's mean is repeated once per scrape it covers, so a run split across
    two groups weighs every scrape alike."""
    by_state: dict[str, list[float]] = {}
    for cell, groups in cells:
        m = _CSTATE.search(cell)
        if m is None:
            continue
        vals = by_state.setdefault(m.group(1), [])
        for g in groups:
            vals += [float(g["mean"])] * int(g["n"])
    return {s: mean(v) for s, v in by_state.items() if v}


def reduce_recording(rec: MontaukRecorder) -> tuple[dict, dict] | None:
    """(numbers, cstates) for one run from its montauk recording, or None when
    the recording carries no package energy."""
    tp = (montauk_envelope(rec.dir, digest=True) or {}).get("thermal_power") or {}
    if "energy_joules_total" not in tp:
        log_error(f"montauk recorded no package energy in {rec.dir} "
                  f"(see {rec.stdout_path})")
        return None
    out = {"j_pkg": float(tp["energy_joules_total"])}
    for key in ("power_avg_w", "power_peak_w", "cpu_clock_avg_mhz",
                "cpu_temp_avg_c", "cpu_temp_peak_c"):
        if key in tp:
            out[key] = float(tp[key])
    pmu = montauk_population(rec.dir, "montauk_pmu_available")
    out["pmu_available"] = 1.0 if any(g["max"] > 0 for _c, gs in pmu
                                      for g in gs) else 0.0
    if out["pmu_available"]:
        for key, metric in (("instructions", "montauk_pmu_instructions_total"),
                            ("cycles", "montauk_pmu_cycles_total")):
            delta = counter_delta(montauk_population(rec.dir, metric))
            if delta:
                out[key] = delta
    else:
        log_warn(f"      montauk's system-wide PMU was not up, so IPC and EPI "
                 f"read n/a (montauk says why in {rec.stdout_path})")
    cstates = cstate_means(montauk_population(
        rec.dir, "montauk_cstate_residency_percent"))
    return out, cstates


def record_workload(rec_dir: Path, cmd: list[str], timeout: float):
    """Run CMD under the shared montauk recorder. Returns (result,
    CompletedProcess), result carrying wall_s, montauk's numbers and the
    cpuidle residency, or None on failure."""
    ran = record_run(rec_dir, cmd, timeout)
    if ran is None:
        return None
    rec, wall_s, r = ran
    reduced = reduce_recording(rec)
    if reduced is None:
        return None
    numbers, cstates = reduced
    return {"wall_s": wall_s, "montauk": numbers, "cstates": cstates}, r


# WORKLOAD RUNNERS

def run_idle_floor(rec_dir: Path, secs: int) -> dict:
    """Run nothing for `secs` seconds under a montauk recording.

    work_unit = elapsed seconds (since there's no other notion of work
    here -- we want average wattage during idle)."""
    recorded = record_workload(rec_dir, ["sleep", str(secs)], timeout=secs + 30)
    if recorded is None:
        return {}
    result, _proc = recorded
    result["work_unit"] = result["wall_s"]
    result["work_unit_label"] = "idle-second"
    return result


def run_messaging(rec_dir: Path) -> dict:
    """perf bench sched messaging under a montauk recording. Work unit =
    number of messages exchanged. Each group has 1 sender talking to 20
    receivers by default, looped NR_LOOPS times. We treat the work unit as
    (groups * loops * 20 * 2) -- each loop is a send+receive in both
    directions."""
    cmd = [
        "perf", "bench", "-f", "simple", "sched", "messaging",
        "-t",  # threads instead of processes (lighter weight)
        "-g", str(MESSAGING_GROUPS),
        "-l", str(MESSAGING_LOOPS),
    ]
    recorded = record_workload(rec_dir, cmd, timeout=600)
    if recorded is None:
        return {}
    result, _proc = recorded
    # 20 receivers per group is the perf bench default
    result["work_unit"] = MESSAGING_GROUPS * MESSAGING_LOOPS * 20 * 2
    result["work_unit_label"] = "message"
    return result


def run_paced(rec_dir: Path, fb: Path, ipu: float, args: list[str]) -> dict:
    """prism-frame's paced scenario for PACED_SECS under a montauk recording.
    Work unit = frames presented, with the frame bench's late-frame share and
    1% low beside it.

    The warmup is zero: framebench counts no frame before its warmup ends, so
    a warmup's frames would be in the recording's energy and missing from the
    count. With none, it records every frame time from its first present to
    its last, the whole process the recording covers. The samples land beside
    the recording, as its montauk stdout does."""
    out, tag = rec_dir.parent, rec_dir.name
    cmd = FRAME.run_cmd(fb, ipu, args, PACED_SECS, 0, out, tag)
    recorded = record_workload(rec_dir, cmd, timeout=PACED_SECS + 60)
    if recorded is None:
        return {}
    result, r = recorded
    m = FRAME.run_metrics(r.stdout, args, PACED_SECS, out, tag)
    if m is None:
        return {}
    result["work_unit"] = m["frames"]
    result["work_unit_label"] = "frame"
    result["late_pct"] = m["late_pct"]
    result["low1"] = m["low1"]
    return result


def run_schbench(rec_dir: Path, n_cpus: int) -> dict:
    """schbench -m <msgr> -t <thr> -r <secs> -F128 -n5 under a montauk
    recording.
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
    recorded = record_workload(rec_dir, cmd, timeout=SCHBENCH_RUNTIME_SECS + 60)
    if recorded is None:
        return {}
    result, r = recorded
    # schbench emits its histogram on stderr
    samples = parse_schbench_samples(r.stdout + "\n" + r.stderr)
    result["work_unit"] = samples or 1
    result["work_unit_label"] = "sample"
    return result


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
    valid = [r for r in runs if r and "montauk" in r]
    if not valid:
        return {"runs": 0}

    out: dict = {"runs": len(valid)}

    # Wall time
    walls = [r["wall_s"] for r in valid]
    out["wall_s_mean"], out["wall_s_stdev"] = mean_stdev(walls)

    # Work unit (assume same label across runs)
    out["work_unit_label"] = valid[0].get("work_unit_label", "op")
    out["work_unit_mean"] = sum(r.get("work_unit", 0) for r in valid) / len(valid)

    # montauk's numbers: Package energy, the PMU deltas, the clock, the
    # temperature and the package power it sampled
    names: set[str] = set()
    for r in valid:
        names.update(r["montauk"])
    for name in names:
        vals = [r["montauk"][name] for r in valid if name in r["montauk"]]
        m, sd = mean_stdev(vals)
        out[f"{name}_mean"] = m
        out[f"{name}_stdev"] = sd

    states: set[str] = set()
    for r in valid:
        states.update(r["cstates"])
    out["cstates"] = {s: mean([r["cstates"][s] for r in valid if s in r["cstates"]])
                      for s in states}

    # A paced cell's frame quality, the frame bench's own numbers
    for key in ("late_pct", "low1"):
        vals = [r[key] for r in valid if key in r]
        if vals:
            out[f"{key}_mean"], out[f"{key}_stdev"] = mean_stdev(vals)

    # Derived: J/work_unit, IPC, EPI (joules per instruction). IPC and EPI
    # only for a cell whose every run had the PMU up: Over fewer runs the
    # instruction mean would not share J_PKG's run set.
    pmu = all(r["montauk"]["pmu_available"] for r in valid)
    j_pkg = out.get("j_pkg_mean", 0.0)
    ins = out.get("instructions_mean", 0.0) if pmu else 0.0
    cyc = out.get("cycles_mean", 0.0) if pmu else 0.0
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
                 vendor: str, model: str, montauk_ver: str, runs: int,
                 cooldown_secs: int, all_results: dict,
                 cold: dict | None = None) -> Path:
    """Render a column-table summary suitable for committing into the
    archive."""
    R: list[str] = []
    dirty = " (dirty)" if git["dirty"] else ""
    R.append(f"PANDEMONIUM prism-power v{version} [{git['commit']}{dirty}]")
    R.append(f"cpu:        {model} ({vendor})")
    R.append(f"ncpus:      {ncpus}")
    R.append(f"runs:       {runs} per (scheduler, workload)")
    R.append(f"cooldown:   {cooldown_secs}s between runs")
    R.append(f"instrument: montauk {montauk_ver}, one recording per run at "
             f"{MONTAUK_RECORD_MS}ms")
    R.append("")

    workloads = sorted({wl for results in all_results.values() for wl in results})
    if not workloads:
        R.append("(no successful workload runs)")
        path = LOG_DIR / f"prism-power-{stamp}.log"
        path.write_text("\n".join(R) + "\n")
        return path

    sched_order = list(all_results.keys())
    eevdf_per_workload: dict[str, dict] = {}
    if "EEVDF" in all_results:
        eevdf_per_workload = all_results["EEVDF"]

    for wl in workloads:
        R.append(f"[{wl.upper()}]")
        # ENERGY TABLE, a paced cell's frame quality on the same row
        quality = any("late_pct_mean" in (all_results[sn].get(wl) or {})
                      for sn in sched_order)
        header = (f"{'SCHEDULER':<26} {'RUNS':>5} {'WALL_S':>10} "
                  f"{'J_PKG':>10} {'AVG_W':>8} {'J/OP':>14} {'IPC':>7} "
                  f"{'EPI_pJ':>9}")
        if quality:
            header += f" {'LATE%':>7} {'LOW1':>7}"
        R.append(header)
        for sn in sched_order:
            sd = all_results[sn].get(wl)
            if not sd or sd.get("runs", 0) == 0:
                R.append(f"{sn:<26} {'FAIL':>5}")
                continue
            row = f"{sn:<26} {sd['runs']:>5d}"
            row += f" {_fmt(sd.get('wall_s_mean'), 9, 2)}s"
            row += f" {_fmt(sd.get('j_pkg_mean'), 9, 2)}J"
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
            if quality:
                row += (f" {_fmt(sd.get('late_pct_mean'), 7, 2)}"
                        f" {_fmt(sd.get('low1_mean'), 7, 1)}")
            R.append(row)
        R.append("")

        # FREQUENCY / IDLE TABLE: montauk's cpufreq clock (scaling_cur_freq
        # across online CPUs, not an APERF/MPERF reading), each cpuidle
        # state's residency by name and the CPU temperature
        cells = [all_results[sn].get(wl) or {} for sn in sched_order]
        states = sorted({s for sd in cells for s in sd.get("cstates", {})},
                        key=lambda s: (s != "POLL", s))
        if any("cpu_clock_avg_mhz_mean" in sd or sd.get("cstates") for sd in cells):
            R.append(f"{'SCHEDULER':<26} {'CPUFREQ_MHZ':>11}"
                     + "".join(f" {s + '%':>7}" for s in states)
                     + f" {'PKGTMP':>8}")
            for sn, sd in zip(sched_order, cells):
                if sd.get("runs", 0) == 0:
                    R.append(f"{sn:<26} {'n/a':>11}")
                    continue
                row = f"{sn:<26} {_fmt(sd.get('cpu_clock_avg_mhz_mean'), 11, 0)}"
                for s in states:
                    row += f" {_fmt(sd['cstates'].get(s), 7, 1)}"
                temp = sd.get("cpu_temp_avg_c_mean")
                row += f" {temp:>7.1f}C" if temp is not None else f" {'n/a':>8}"
                R.append(row)
            R.append("")

        # RELATIVE TO EEVDF
        eevdf_sd = eevdf_per_workload.get(wl) if eevdf_per_workload else None
        if eevdf_sd and eevdf_sd.get("runs", 0) > 0:
            R.append(f"{'SCHEDULER':<26} {'WALL_VS_EEVDF':>14} "
                     f"{'J_VS_EEVDF':>12} {'J/OP_VS_EEVDF':>15}")
            base_wall = eevdf_sd.get("wall_s_mean", 0)
            base_j = eevdf_sd.get("j_pkg_mean", 0)
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
                j = sd.get("j_pkg_mean", 0)
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

    if cold is not None:
        R.append("COLD-WAKE  (idle-exit cost -- the other half of the idle trade)")
        if cold.get("rc") == 0:
            R.append(f"  ran {cold.get('duration')}s per scheduler arm under "
                     f"powersave; montauk's COLD-WAKE block separates the")
            R.append("  governor frequency ramp from scheduler dispatch delay. "
                     "See the prism-coldwake log and recording.")
        else:
            R.append(f"  DID NOT COMPLETE (exit {cold.get('rc')}). The energy "
                     f"numbers above stand; the cost side is missing.")
        R.append("")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-power-{stamp}.log"
    path.write_text("\n".join(R) + "\n")
    return path


# PROMETHEUS

def write_prometheus(version: str, git: dict, stamp: str, ncpus: int,
                     vendor: str, model: str, montauk_ver: str,
                     runs: int, cooldown_secs: int,
                     all_results: dict) -> Path:
    pb = PrometheusBuilder("power")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=git["commit"], git_dirty=git["dirty"],
            vendor=vendor, model=model, montauk_version=montauk_ver)
    pb.gauge("cpus", ncpus, help="CPUs available")
    pb.gauge("runs", runs, help="iterations per cell")
    pb.gauge("cooldown_seconds", cooldown_secs, help="cooldown between runs")
    pb.gauge("montauk_interval_ms", MONTAUK_RECORD_MS,
             help="montauk's scrape and thermal-sample interval in every run's recording")

    for sched_name, by_workload in all_results.items():
        for wl, sd in by_workload.items():
            if not sd or sd.get("runs", 0) == 0:
                continue
            ls = {"scheduler": sched_name, "workload": wl}
            pb.gauge("iterations", sd["runs"],
                     help="successful run count for this cell", labels=ls)
            pb.gauge("wall_seconds", f"{sd.get('wall_s_mean', 0):.4f}",
                     help="wall-clock time (mean)", labels=ls)
            pb.gauge("wall_seconds_stdev", f"{sd.get('wall_s_stdev', 0):.4f}",
                     help="wall-clock time (stddev)", labels=ls)
            pb.gauge("joules_pkg", f"{sd.get('j_pkg_mean', 0):.4f}",
                     help="package energy (mean joules)", labels=ls)
            pb.gauge("joules_pkg_stdev", f"{sd.get('j_pkg_stdev', 0):.4f}",
                     help="package energy (stddev joules)", labels=ls)
            if "avg_watts_mean" in sd:
                pb.gauge("avg_watts", f"{sd['avg_watts_mean']:.4f}",
                         help="average package wattage during run", labels=ls)
            if "j_per_op_mean" in sd:
                pb.gauge("joules_per_op", f"{sd['j_per_op_mean']:.6e}",
                         help="joules per work unit (label varies per workload)", labels=ls)
                pb.gauge("work_unit_count", f"{sd.get('work_unit_mean', 0):.0f}",
                         help="total work units in run (mean)", labels=ls)
            if sd.get("work_unit_label") == "frame":
                pb.gauge("frames", f"{sd.get('work_unit_mean', 0):.0f}",
                         help="frames a paced run presented (mean)", labels=ls)
            for key, metric, help_text in (
                    ("late_pct", "frame_late_pct",
                     f"% of paced frames later than {FRAME.LATE_FACTOR:.0%} of "
                     f"the {FRAME.PACED_HZ} fps period"),
                    ("low1", "frame_low1_fps",
                     "1% low of a paced run, its p99 frame time as FPS")):
                if f"{key}_mean" in sd:
                    pb.gauge(metric, f"{sd[f'{key}_mean']:.4f}",
                             help=f"{help_text} (mean)", labels=ls)
                    pb.gauge(f"{metric}_stdev", f"{sd[f'{key}_stdev']:.4f}",
                             help=f"{help_text} (stddev)", labels=ls)
            if "ipc_mean" in sd:
                pb.gauge("ipc", f"{sd['ipc_mean']:.4f}",
                         help="instructions per cycle (mean)", labels=ls)
            if "epi_mean" in sd:
                pb.gauge("epi_joules", f"{sd['epi_mean']:.4e}",
                         help="energy per instruction (mean joules)", labels=ls)
            pb.gauge("pmu_available", f"{sd.get('pmu_available_mean', 0):.4f}",
                     help="fraction of runs whose montauk recording had the "
                          "system-wide PMU up", labels=ls)
            for key, metric, help_text in (
                    ("power_avg_w_mean", "sampled_watts",
                     "package power, mean of montauk's scrapes (mean across runs)"),
                    ("power_peak_w_mean", "peak_watts",
                     "peak package power montauk sampled (mean across runs)"),
                    ("cpu_clock_avg_mhz_mean", "cpu_clock_mhz",
                     "cpufreq clock across online CPUs (mean)"),
                    ("cpu_temp_avg_c_mean", "cpu_temp_celsius",
                     "CPU temperature, montauk's hottest CPU sensor (mean)"),
                    ("cpu_temp_peak_c_mean", "cpu_temp_peak_celsius",
                     "peak CPU temperature (mean across runs)")):
                if key in sd:
                    pb.gauge(metric, f"{sd[key]:.4f}", help=help_text, labels=ls)
            for state, pct in sorted(sd.get("cstates", {}).items()):
                pb.gauge("cstate_residency_pct", f"{pct:.4f}",
                         help="cpuidle residency per state across online CPUs (mean)",
                         labels={**ls, "state": state})

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"prism-power-{version}-{stamp}.prom"
    path.write_text(pb.render())
    return path


# SCHEDULER ENTRIES

def build_entries(args) -> list[tuple[str, list[str] | None]]:
    """The (display_name, cmd_or_None) arms, EEVDF's cmd None: the field of
    field_of."""
    # Production-realistic activation: NO --verbose HERE, AND THAT IS DELIBERATE.
    # Every other bench passes it because its per-second line is the only
    # surface for the enqueue-tier counters. This bench measures WATTS, so it is
    # the one arm where the cost of a per-second print is the quantity under test
    # rather than a rounding error. If the enqueue split is ever wanted here, it
    # is worth measuring the flag's own draw first.
    entries = field_of(args, verbose=False)
    if not entries:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is installed")
    return entries


def select_workloads(args, has_schbench: bool) -> list[str]:
    if args.workload != "all":
        if args.workload == "schbench" and not has_schbench:
            log_warn("schbench not installed; skipping")
            return []
        return [args.workload]
    out = ["idle-floor", "messaging", "paced"]
    if has_schbench:
        out.append("schbench")
    else:
        log_warn("schbench not installed; skipping schbench workload")
    return out


# MAIN

def run_workload(wl: str, n_cpus: int, rec_dir: Path, paced: tuple | None) -> dict:
    if wl == "idle-floor":
        return run_idle_floor(rec_dir, IDLE_FLOOR_SECS)
    if wl == "messaging":
        return run_messaging(rec_dir)
    if wl == "paced":
        return run_paced(rec_dir, *paced)
    if wl == "schbench":
        return run_schbench(rec_dir, n_cpus)
    log_error(f"unknown workload: {wl}")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workload", choices=("all",) + WORKLOADS, default="all",
                    help="Which workload to run (default: all)")
    ap.add_argument("--iterations", type=int, default=DEFAULT_RUNS,
                    help=f"Iterations per (scheduler, workload) cell (default {DEFAULT_RUNS})")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_SECS,
                    help=f"Seconds between runs (default {DEFAULT_COOLDOWN_SECS})")
    ap.add_argument("--trace", action="store_true",
                    help="Also run the cold-wake arm, a montauk capture of "
                         "idle-exit latency. Without it the run is the energy "
                         "workloads' montauk recordings alone")
    add_field_flags(ap)
    ap.add_argument("--no-build", action="store_true",
                    help="Skip ensure_build (use existing binary)")
    # COLD-WAKE IS POWER'S FOURTH ARM. The energy workloads measure what idle
    # SAVES; cold-wake measures what idle COSTS -- the dispatch delay on the
    # first wake off a deep-idle core, separated from the governor's frequency
    # ramp. Reporting the win without the cost is half the trade, which is why
    # these were never two benches. prism-coldwake keeps its own entry point
    # because prism's default pass runs it, and its --storm reproducer, by name.
    ap.add_argument("--no-cold-wake", action="store_true",
                    help="Skip the cold-wake arm that --trace adds (energy "
                         "workloads only)")
    ap.add_argument("--cold-wake-duration", type=int, default=60,
                    help="Cold-wake cycle window per scheduler arm (default 60)")
    ap.add_argument("--cores", type=str, default=None,
                    help="Accepted for suite uniformity; RAPL is measured whole-package, not per width")
    args = ap.parse_args()
    warm_sudo()
    if args.workload in ("all", "messaging") and not shutil.which("perf"):
        log_error("perf not found; the messaging workload is perf bench sched "
                  "messaging. Install with: sudo pacman -S perf")
        return 1
    if not montauk_available():
        log_error(f"montauk not found at {MONTAUK}; it records every run. "
                  f"Install montauk")
        return 1
    missing = [f for f in MONTAUK_RECORD_FLAGS if not montauk_has_flag(f)]
    if missing:
        log_error(f"the installed montauk has no {', '.join(missing)}, which "
                  f"every run's recording passes. Install the newer montauk")
        return 1

    entries = build_entries(args)
    if not args.no_build and any(n.startswith("PANDEMONIUM") for n, _ in entries):
        ensure_build()

    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n_cpus = multiprocessing.cpu_count()
    vendor = detect_cpu_vendor()
    model = detect_cpu_model()

    log_info(f"prism-power v{ver} [{git['commit']}{' (dirty)' if git['dirty'] else ''}]")
    log_info(f"CPU: {model} ({vendor})  ncpus: {n_cpus}")
    montauk_ver = montauk_version()
    log_info(f"montauk: {montauk_ver} at {MONTAUK}")

    schbench_path = detect_schbench()
    if schbench_path:
        log_info(f"schbench: {schbench_path}")
    else:
        log_warn("schbench not installed; schbench workload will be skipped")

    workloads = select_workloads(args, schbench_path is not None)
    if not workloads:
        log_error("No workloads to run")
        return 1
    log_info(f"workloads: {', '.join(workloads)}  runs/cell: {args.iterations}  "
             f"cooldown: {args.cooldown}s")
    print()

    if not clear_loaded_scheduler():
        return 1
    time.sleep(1)

    # On EEVDF, before any arm loads, so every arm runs the same work per frame
    paced = None
    if "paced" in workloads:
        fb = FRAME.build(stamp)
        ipu = FRAME.calibrate(fb) if fb is not None else None
        if ipu is None:
            return 1
        log_info(f"calibration on EEVDF: {ipu:.3f} iterations/us")
        paced = (fb, ipu, FRAME.scenarios(get_online_cpus())["paced"])

    if not entries:
        log_error("No schedulers selected")
        return 1
    log_info(f"schedulers: {', '.join(name for name, _ in entries)}")
    print()

    all_results: dict[str, dict[str, dict]] = {}
    rec_root = POWER_DIR / stamp

    try:
        for sched_name, cmd in entries:
            all_results[sched_name] = {}
            log_info(f"[{sched_name}] starting...")
            refresh_sudo()
            safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")

            guard = None
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"{sched_name}: failed to activate, skipping")
                    print()
                    continue

            try:
                for wl in workloads:
                    log_info(f"  [{wl}] {args.iterations} runs")
                    runs: list[dict] = []
                    for i in range(args.iterations):
                        log_info(f"    run {i+1}/{args.iterations}...")
                        result = run_workload(
                            wl, n_cpus, rec_root / f"{safe}-{wl}-{i+1}", paced)
                        if result and "montauk" in result:
                            log_info(f"      wall={result['wall_s']:.2f}s "
                                     f"J_pkg={result['montauk']['j_pkg']:.2f}")
                        else:
                            log_warn("      run failed")
                        runs.append(result)
                        if i < args.iterations - 1:
                            cooldown(args.cooldown)

                    agg = aggregate_runs(runs)
                    all_results[sched_name][wl] = agg
                    if agg.get("runs", 0) > 0:
                        log_info(f"  [{wl}] mean: "
                                 f"wall={agg.get('wall_s_mean', 0):.2f}s "
                                 f"J_pkg={agg.get('j_pkg_mean', 0):.2f} "
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
        stop_recorders()
        _chown_to_invoking_user(POWER_DIR)
        if is_scx_active():
            wait_for_deactivation(5.0)

    cold = None
    if args.trace and not args.no_cold_wake:
        print()
        log_info("COLD-WAKE ARM: idle-exit latency, the cost side of the idle win")
        _cw = [sys.executable, str(Path(__file__).parent / "pandemonium-tests.py"),
               "prism-coldwake", "--trace",
               "--duration", str(args.cold_wake_duration)]
        try:
            _rc = subprocess.run(_cw).returncode
            cold = {"rc": _rc, "duration": args.cold_wake_duration}
            if _rc != 0:
                log_warn(f"cold-wake arm exited {_rc}; energy results stand alone")
        except Exception as e:
            log_warn(f"cold-wake arm did not run: {e}")

    if any(by_wl for by_wl in all_results.values()):
        print()
        report_path = write_report(ver, git, stamp, n_cpus, vendor, model,
                                   montauk_ver, args.iterations, args.cooldown,
                                   all_results, cold=cold)
        prom_path = write_prometheus(ver, git, stamp, n_cpus, vendor, model,
                                     montauk_ver, args.iterations, args.cooldown,
                                     all_results)
        log.report(report_path.read_text())
        log_info(f"REPORT: {report_path}")
        log_info(f"METRICS: {prom_path}")
        log_info(f"RECORDINGS: {rec_root}")
    else:
        log_error("No successful results to report")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
