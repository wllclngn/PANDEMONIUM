#!/usr/bin/env python3
"""prism_fork_thread_gate -- fork-thread's cost phase through montauk, offline.

WHAT IT GUARDS. fork-thread's cost phase runs perf bench sched messaging under
the shared montauk recorder and reads every counter out of that recording: Each
event's delta over the run from montauk's population mode, beside perf bench's
own elapsed time. A mis-mapped metric does not raise: The counter reads N/A,
IPC leaves the table and the .prom quietly loses the series.

THE PROPERTIES.

1. THE COUNTERS ARE montauk's. A stand-in perf runs under the real recorder
   against a stand-in montauk, and envelopes in montauk's population shape,
   split across two hourly files, give each event's delta. The workload is
   perf bench run directly, the elapsed time is the one perf bench printed,
   not the wall, and no event montauk has no counter for appears.
2. NO PMU, NO COUNTERS. A recording that carries no PMU series yields no
   counter and keeps its elapsed time.
3. A FAILED RUN IS NO SAMPLE. A perf that exits non-zero or prints no time
   returns nothing and leaves no recorder running.
4. EVERY ITERATION IS RECORDED APART. Two iterations of one cell get two
   recordings, named by arm, cell and iteration.
5. THE REPORT AND THE .prom CARRY montauk's COUNTERS: The pmu_ gauges and
   pmu_ipc, no cache miss rate and nothing of perf stat.

    python3 prism_fork_thread_gate.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandemonium_common as pc

H21, H22 = "montauk_2026-11-08_21", "montauk_2026-11-08_22"
LABELS = "commit=unknown version=8.16.0"
ELAPSED = 1.234

# Each counter's (min, max) in the two hourly files, and the run delta they make.
SERIES = {
    "montauk_pmu_cycles_total": (1.0e9, 4.0e10, 4.1e10, 9.1e10),
    "montauk_pmu_instructions_total": (5.0e8, 3.0e10, 3.1e10, 4.55e10),
    "montauk_pmu_cache_misses_total": (1.0e6, 2.0e9, 2.1e9, 3.001e9),
    "montauk_pmu_context_switches_total": (1000, 4_001_000, 4_002_000, 8_001_000),
    "montauk_pmu_cpu_migrations_total": (10, 300_010, 300_020, 600_010),
    "montauk_pmu_branch_misses_total": (5.0e5, 1.2e8, 1.21e8, 2.505e8),
}
WANT = {"cycles": 9.0e10, "instructions": 4.5e10, "cache-misses": 3.0e9,
        "context-switches": 8_000_000, "cpu-migrations": 600_000,
        "branch-misses": 2.5e8}

FAKE_MONTAUK = """#!{python}
import signal, sys, time
from pathlib import Path
prom = Path(sys.argv[sys.argv.index("--log") + 1]) / "{h21}.prom"
prom.write_text("# montauk_scrape_timestamp_ms 1\\n")
stop = []
signal.signal(signal.SIGINT, lambda *_: stop.append(1))
while not stop:
    time.sleep(0.02)
with open(prom, "a") as f:
    f.write("# montauk_scrape_timestamp_ms 2 final\\n")
"""

FAKE_PERF = """#!{python}
import os, sys, time
from pathlib import Path
Path(os.environ["FAKE_PERF_ARGV"]).write_text("\\n".join(sys.argv[1:]))
mode = os.environ["FAKE_PERF_MODE"]
if mode == "fail":
    sys.stderr.write("perf: messaging failed\\n")
    sys.exit(1)
time.sleep(0.3)
print("{elapsed}" if mode == "ok" else "# Running 'sched/messaging' benchmark:")
"""


def note(msg):
    print(f"[fork-thread] {msg}")


def load_fork_thread():
    spec = importlib.util.spec_from_file_location(
        "prism_fork_thread", str(ROOT / "tests" / "prism-fork-thread.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def close(a, b) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(b))


def group(name, n, lo, hi):
    return {"group": name, "n": n, "mean": (lo + hi) / 2, "min": lo, "max": hi}


def stub_population(pmu_on: bool, asked: list) -> None:
    """Serve montauk's population mode from SERIES, or nothing with the PMU
    off, recording each metric the bench asks for."""
    def run_cmd_capture(argv, cwd=None, env=None):
        metric = argv[argv.index("--metric") + 1]
        asked.append(metric)
        if not pmu_on or metric not in SERIES:
            return 1, "", "no usable gauges in 1 file(s)"
        lo1, hi1, lo2, hi2 = SERIES[metric]
        envelope = {"schema_version": 1,
                    "population": {"files": 2, "compare_axis": "capture",
                                   "better": "lower",
                                   "metrics": [{"metric": metric, "cells": [
                                       {"cell": LABELS,
                                        "groups": [group(H21, 80, lo1, hi1),
                                                   group(H22, 40, lo2, hi2)]}]}]}}
        return 0, json.dumps(envelope), ""
    pc.run_cmd_capture = run_cmd_capture


def stand_ins(tmp: Path) -> None:
    """A stand-in montauk for the recorder and a stand-in perf first on PATH."""
    bindir = tmp / "bin"
    bindir.mkdir()
    for path, text in ((tmp / "fake-montauk",
                        FAKE_MONTAUK.format(python=sys.executable, h21=H21)),
                       (bindir / "perf",
                        FAKE_PERF.format(python=sys.executable, elapsed=ELAPSED))):
        path.write_text(text)
        os.chmod(path, 0o755)
    pc.MONTAUK = str(tmp / "fake-montauk")
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
    os.environ["FAKE_PERF_ARGV"] = str(tmp / "perf-argv")


def recorded_whole(rec_dir: Path) -> bool:
    return "final" in (rec_dir / f"{H21}.prom").read_text()


def check_counters(m, tmp: Path, fails: list) -> None:
    before = len(fails)
    os.environ["FAKE_PERF_MODE"] = "ok"
    asked: list = []
    stub_population(True, asked)
    rec_dir = tmp / "runs" / "EEVDF-thread-g2-1"
    el, ct = m.run_perf_bench(rec_dir, groups=2, threaded=True, loops=100)
    argv = Path(os.environ["FAKE_PERF_ARGV"]).read_text().split("\n")
    if argv != ["bench", "-f", "simple", "sched", "messaging", "-t",
                "-g", "2", "-l", "100"]:
        fails.append(f"counters: the workload ran as perf {' '.join(argv)}, "
                     f"want perf bench run directly")
    if el != ELAPSED:
        fails.append(f"counters: elapsed reads {el!r}, want perf bench's {ELAPSED}")
    if set(asked) != set(SERIES):
        fails.append(f"counters: asked montauk for {sorted(asked)}")
    if ct is None or set(ct) != set(WANT):
        fails.append(f"counters: events read {sorted(ct or {})}, want {sorted(WANT)}")
    else:
        for k, v in WANT.items():
            if not close(ct[k], v):
                fails.append(f"counters: {k} = {ct[k]!r}, want the run delta {v}")
    if pc._LIVE_RECORDERS or not recorded_whole(rec_dir):
        fails.append("counters: the recorder was not stopped on SIGINT")

    stub_population(False, [])
    el, ct = m.run_perf_bench(tmp / "runs" / "EEVDF-thread-g2-2", groups=2)
    if el != ELAPSED or ct != {}:
        fails.append(f"counters: a PMU-off recording read {el!r} {ct!r}, want "
                     f"{ELAPSED} and no counter")
    if len(fails) == before:
        note("PASS each event is montauk's run delta beside perf bench's elapsed")


def check_failed(m, tmp: Path, fails: list) -> None:
    before = len(fails)
    stub_population(True, [])
    for mode in ("fail", "garbled"):
        os.environ["FAKE_PERF_MODE"] = mode
        rec_dir = tmp / "runs" / f"EEVDF-thread-g2-{mode}"
        got = m.run_perf_bench(rec_dir, groups=2)
        if got != (None, None):
            fails.append(f"failed run: a {mode} perf returned {got!r}")
        if pc._LIVE_RECORDERS or not recorded_whole(rec_dir):
            fails.append(f"failed run: a {mode} perf left montauk running")
    if len(fails) == before:
        note("PASS a failed or unparseable perf bench is no sample")


def check_iterations(m, tmp: Path, fails: list) -> None:
    before = len(fails)
    os.environ["FAKE_PERF_MODE"] = "ok"
    stub_population(True, [])
    root = tmp / "iter"
    (el, ct), spreads = m._measure("PANDEMONIUM (BPF)", m.Cell("thread/g2", True, 2),
                                   2, root)
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
    if dirs != ["PANDEMONIUM-BPF-thread-g2-1", "PANDEMONIUM-BPF-thread-g2-2"]:
        fails.append(f"iterations: recordings read {dirs}")
    if el != ELAPSED or not ct or not close(ct["cycles"], WANT["cycles"]):
        fails.append(f"iterations: the median reads {el!r} {ct!r}")
    if (spreads or {}).get("seconds", {}).get("n") != 2 or set(spreads) != {"seconds"}:
        fails.append(f"iterations: spreads read {spreads!r}, want the elapsed one")
    if len(fails) == before:
        note("PASS every iteration gets a recording of its own")


def check_outputs(m, tmp: Path, fails: list) -> None:
    before = len(fails)
    m.LOG_DIR = m.ARCHIVE_DIR = tmp
    untraced, traced = m.Cell("thread/g24", True, 24), m.Cell("proc/g24", False, 24)
    arms = {"EEVDF": (12.1, dict(WANT)), "PANDEMONIUM (BPF)": (11.4, {})}
    cells_data = [(untraced, arms, {"EEVDF": {"seconds": m._spread_stats([12.0, 12.2])}}, {}),
                  (traced, arms, {}, {"EEVDF": {"p99_us": 40, "cross_domain_pct": 1.5}})]
    git = {"commit": "abc1234", "dirty": False}
    stamp = "20261108-120000"
    lines = m.write_report("5.25.0", git, stamp, 12, cells_data, 1000).read_text().splitlines()
    prom = m.write_prometheus("5.25.0", git, stamp, 12, cells_data, 1000).read_text()

    if "counters: montauk's system-wide PMU, each the delta over its run's recording" not in lines:
        fails.append("report: header does not name montauk's PMU")
    for title, want in (("COST  (untraced", ["SCHEDULER", "TIME", "VS", "EEVDF", "IPC"]),
                        ("TRADEOFF  (", ["SCHEDULER", "WAKE2RUN", "p99", "CROSS-DOM",
                                         "TIME", "VS", "EEVDF", "IPC"])):
        i = next((n for n, l in enumerate(lines) if l.startswith(title)), None)
        if i is None or lines[i + 1].split() != want:
            fails.append(f"report: {title.split()[0]} header reads "
                         f"{lines[i + 1].split() if i is not None else None}")
    cost = lines.index(next(l for l in lines if l.startswith("COST  (")))
    if lines[cost + 2].split() != ["EEVDF", "12.100±0.141s", "baseline", "0.500"]:
        fails.append(f"report: EEVDF cost row reads {lines[cost + 2]!r}")
    if lines[cost + 3].split() != ["PANDEMONIUM", "(BPF)", "11.400s", "-5.8%", "N/A"]:
        fails.append(f"report: PMU-off cost row reads {lines[cost + 3]!r}")
    table = lines.index(next(l for l in lines if l.startswith("COUNTER ")))
    rows = [l.split() for l in lines[table + 1:table + 6]]
    if rows != [["cycles", "90.00G", "N/A"], ["instructions", "45.00G", "N/A"],
                ["cache-misses", "3.00G", "N/A"], ["cpu-migrations", "600.00K", "N/A"],
                ["branch-misses", "250.00M", "N/A"]] or lines[table + 6]:
        fails.append(f"report: COUNTER table reads {rows}")

    ls = '{mode="thread",groups="24",scheduler="EEVDF"}'
    for want in (f"pandemonium_fork_thread_seconds{ls} 12.1000",
                 f"pandemonium_fork_thread_seconds_n{ls} 2",
                 f"pandemonium_fork_thread_pmu_cycles{ls} 90000000000",
                 f"pandemonium_fork_thread_pmu_instructions{ls} 45000000000",
                 f"pandemonium_fork_thread_pmu_cache_misses{ls} 3000000000",
                 f"pandemonium_fork_thread_pmu_context_switches{ls} 8000000",
                 f"pandemonium_fork_thread_pmu_cpu_migrations{ls} 600000",
                 f"pandemonium_fork_thread_pmu_branch_misses{ls} 250000000",
                 f"pandemonium_fork_thread_pmu_ipc{ls} 0.500",
                 "# HELP pandemonium_fork_thread_pmu_cycles cycles over the run, "
                 "montauk's system-wide PMU"):
        if want not in prom.splitlines():
            fails.append(f"prom: missing {want}")
    if any("_pmu_" in l and 'scheduler="PANDEMONIUM (BPF)"' in l for l in prom.splitlines()):
        fails.append("prom: the PMU-off arm carries a pmu gauge")
    for name in ("pandemonium_fork_thread_cycles", "pandemonium_fork_thread_ipc"):
        if any(l.startswith(name) for l in prom.splitlines()):
            fails.append(f"prom: still publishes {name}")
    for text, where in (("\n".join(lines), "report"), (prom, "prom")):
        for word in ("perf stat", "cache miss%", "cache-references", "cache_references",
                     "cache_miss_rate", "task_clock", "task-clock"):
            if word in text.lower():
                fails.append(f"{where}: still names {word}")
    if len(fails) == before:
        note("PASS the report and .prom carry montauk's counters")


def main() -> int:
    m = load_fork_thread()
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="prism-fork-thread-gate-") as td:
        tmp = Path(td)
        stand_ins(tmp)
        check_counters(m, tmp, fails)
        check_failed(m, tmp, fails)
        check_iterations(m, tmp, fails)
        check_outputs(m, tmp, fails)
    for f in fails:
        note(f"FAIL {f}")
    note("GATE PASSED: fork-thread's cost phase reads its counters through montauk"
         if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
