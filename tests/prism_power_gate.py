#!/usr/bin/env python3
"""prism_power_gate -- prism-power's montauk reducer and the shared recorder,
offline.

WHAT IT GUARDS. prism-power reads every number it publishes out of montauk:
The digest's thermal_power block for package energy, power, clock and
temperature, and population mode for the PMU counters and each cpuidle state's
residency. Those are reductions over montauk's JSON, testable without root, a
scheduler or a workload, and a mis-keyed field there does not raise: It reads as
an absent number and the column prints n/a, or as zero and J/OP, IPC and EPI
quietly leave the .prom.

THE PROPERTIES.

1. THE REDUCER READS WHAT montauk PUBLISHES. Envelopes in montauk's shape go
   in; package energy, the instruction and cycle deltas and each state's
   residency come out, for a run split across two hourly files too.
2. NO PMU, NO IPC. A recording whose montauk_pmu_available reads 0 yields no
   instruction or cycle delta, and the report prints IPC and EPI as n/a.
3. THE REPORT AND THE .prom CARRY montauk's COLUMNS AND GAUGES, and nothing of
   perf stat or turbostat.
4. THE RECORDER ALWAYS STOPS montauk. Against a stand-in montauk: A SIGINT
   stop is clean and leaves the final scrape on disk; one that ignores SIGINT
   is killed at the bound and the stop says so; a failed, timed-out or
   unstopped run leaves no recorder running; a reused directory is refused.
5. A PACED ROW PAIRS J/FRAME WITH ITS FRAME QUALITY. A stand-in framebench
   result goes through run_paced's parse: The argv is prism-frame's paced
   scenario at warmup 0, so the frame count spans the recording; the frames
   are the work unit; a run under 10 fps is discarded; the late share and 1%
   low reach the aggregate with their stdev, the paced ENERGY table alone
   carries LATE% and LOW1, and the .prom carries frames, frame_late_pct and
   frame_low1_fps for the paced cells only.

    python3 prism_power_gate.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandemonium_common as pc

H21, H22 = "montauk_2026-11-07_21", "montauk_2026-11-07_22"
LABELS = "commit=unknown version=8.15.0"

DIGEST = {"schema_version": 1,
          "digest": {"dir": "rec", "has_events": False},
          "thermal_power": {"cpu_temp_peak_c": 78.5, "cpu_temp_avg_c": 71.25,
                            "power_avg_w": 31.0, "power_peak_w": 44.0,
                            "energy_joules_total": 930.0,
                            "cpu_clock_avg_mhz": 2950.0,
                            "cpu_clock_peak_mhz": 4200.0,
                            "dominant_cstate": "C2",
                            "dominant_cstate_pct": 70.0}}

FAKE_MONTAUK = """#!{python}
import signal, sys, time
from pathlib import Path
d = Path(sys.argv[sys.argv.index("--log") + 1])
prom = d / "{h21}.prom"
if "{mode}" == "die":
    sys.exit(0)
prom.write_text("# montauk_scrape_timestamp_ms 1\\n")
if "{mode}" == "ignore":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
stop = []
signal.signal(signal.SIGINT, lambda *_: stop.append(1))
while not stop:
    time.sleep(0.02)
with open(prom, "a") as f:
    f.write("# montauk_scrape_timestamp_ms 2 final\\n")
"""


def note(msg):
    print(f"[power] {msg}")


def load_power():
    spec = importlib.util.spec_from_file_location(
        "prism_power", str(ROOT / "tests" / "prism-power.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def group(name, n, mean, lo, hi):
    return {"group": name, "n": n, "mean": mean, "min": lo, "max": hi}


def population(metric, cells):
    return {"schema_version": 1,
            "population": {"files": 2, "compare_axis": "capture",
                           "better": "lower",
                           "metrics": [{"metric": metric, "cells": [
                               {"cell": c, "groups": gs} for c, gs in cells]}]}}


def pops(pmu_on: bool) -> dict:
    out = {
        "montauk_pmu_available": population("montauk_pmu_available", [
            (LABELS, [group(H21, 80, float(pmu_on), float(pmu_on), float(pmu_on)),
                      group(H22, 40, float(pmu_on), float(pmu_on), float(pmu_on))])]),
        "montauk_cstate_residency_percent": population(
            "montauk_cstate_residency_percent", [
                ("commit=unknown state=C1 version=8.15.0",
                 [group(H21, 80, 2.0, 0.0, 9.0), group(H22, 40, 5.0, 0.0, 9.0)]),
                ("commit=unknown state=C2 version=8.15.0",
                 [group(H21, 80, 60.0, 0.0, 99.0), group(H22, 40, 90.0, 0.0, 99.0)]),
                ("commit=unknown state=POLL version=8.15.0",
                 [group(H21, 80, 0.01, 0.0, 1.0), group(H22, 40, 0.04, 0.0, 1.0)])]),
    }
    if pmu_on:
        out["montauk_pmu_instructions_total"] = population(
            "montauk_pmu_instructions_total",
            [(LABELS, [group(H21, 80, 5e10, 1e9, 9e10),
                       group(H22, 40, 1.1e11, 9.1e10, 1.31e11)])])
        out["montauk_pmu_cycles_total"] = population(
            "montauk_pmu_cycles_total",
            [(LABELS, [group(H21, 80, 3e10, 2e9, 6e10),
                       group(H22, 40, 8e10, 6.1e10, 1.02e11)])])
    return out


def stub_montauk(m, pmu_on: bool) -> None:
    """Point prism-power's montauk calls at the synthetic envelopes: The digest
    it opens itself, and the population reads the shared reducers make."""
    table = pops(pmu_on)

    def run_cmd_capture(argv, cwd=None, env=None):
        if "--version" in argv:
            return 0, "8.15.0\n", ""
        metric = argv[argv.index("--metric") + 1]
        if metric not in table:
            return 1, "", "no usable gauges in 1 file(s)"
        return 0, json.dumps(table[metric]), ""

    m.run_cmd_capture = pc.run_cmd_capture = run_cmd_capture
    m.montauk_envelope = lambda target, digest=None, **kw: DIGEST


def close(a, b) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(b))


def check_reducer(m, tmp: Path, fails: list) -> tuple:
    stub_montauk(m, pmu_on=True)
    on = m.reduce_recording(m.MontaukRecorder(tmp / "reduce-on"))
    if on is None:
        fails.append("reducer: returned None for a recording with energy")
        return None, None
    numbers, cstates = on
    want = {"j_pkg": 930.0, "instructions": 1.3e11, "cycles": 1.0e11,
            "pmu_available": 1.0, "power_avg_w": 31.0, "power_peak_w": 44.0,
            "cpu_clock_avg_mhz": 2950.0, "cpu_temp_avg_c": 71.25,
            "cpu_temp_peak_c": 78.5}
    for k, v in want.items():
        if k not in numbers or not close(numbers[k], v):
            fails.append(f"reducer: {k} = {numbers.get(k)!r}, want {v}")
    for state, v in (("C1", 3.0), ("C2", 70.0), ("POLL", 0.02)):
        if state not in cstates or not close(cstates[state], v):
            fails.append(f"reducer: {state} residency = {cstates.get(state)!r}, "
                         f"want {v} (scrape-weighted across both hourly files)")

    stub_montauk(m, pmu_on=False)
    off = m.reduce_recording(m.MontaukRecorder(tmp / "reduce-off"))
    if off is None:
        fails.append("reducer: PMU-off recording returned None; energy stands alone")
        return on, None
    if off[0]["pmu_available"] != 0.0 or "instructions" in off[0] or "cycles" in off[0]:
        fails.append(f"reducer: PMU-off recording read {off[0]!r}")

    m.montauk_envelope = lambda target, digest=None, **kw: {"thermal_power": {}}
    if m.reduce_recording(m.MontaukRecorder(tmp / "reduce-none")) is not None:
        fails.append("reducer: a recording with no package energy was accepted")
    if not fails:
        note("PASS reducer reads energy, PMU deltas and per-state residency")
    return on, off


def run(reduced, wall, work):
    numbers, cstates = reduced
    return {"wall_s": wall, "montauk": dict(numbers), "cstates": dict(cstates),
            "work_unit": work, "work_unit_label": "message"}


def check_outputs(m, tmp: Path, on, off, fails: list) -> None:
    before = len(fails)
    work = 5_760_000
    eevdf = m.aggregate_runs([run(on, 15.0, work), run(on, 15.2, work)])
    bpf = m.aggregate_runs([run(off, 15.1, work), {}])
    mixed = m.aggregate_runs([run(on, 15.0, work), run(off, 15.0, work)])
    for k, v in (("j_pkg_mean", 930.0), ("ipc_mean", 1.3),
                 ("epi_mean", 930.0 / 1.3e11), ("j_per_op_mean", 930.0 / work),
                 ("avg_watts_mean", 930.0 / 15.1)):
        if k not in eevdf or not close(eevdf[k], v):
            fails.append(f"aggregate: {k} = {eevdf.get(k)!r}, want {v}")
    for name, agg in (("PMU-off", bpf), ("mixed PMU", mixed)):
        if "ipc_mean" in agg or "epi_mean" in agg:
            fails.append(f"aggregate: {name} cell carries IPC/EPI")
    if bpf.get("runs") != 1:
        fails.append(f"aggregate: a failed run was counted ({bpf.get('runs')})")

    m.LOG_DIR = m.ARCHIVE_DIR = tmp
    results = {"EEVDF": {"messaging": eevdf},
               "PANDEMONIUM (BPF)": {"messaging": bpf}}
    git = {"commit": "abc1234", "dirty": False}
    stamp = "20261107-120000"
    report = m.write_report("5.25.0", git, stamp, 8, "AMD", "Ryzen", "8.15.0",
                            2, 30, results).read_text()
    prom = m.write_prometheus("5.25.0", git, stamp, 8, "AMD", "Ryzen", "8.15.0",
                              2, 30, results).read_text()

    lines = report.splitlines()
    if "instrument: montauk 8.15.0, one recording per run at 250ms" not in lines:
        fails.append("report: header does not name montauk and its version")
    freq = next((l for l in lines if "CPUFREQ_MHZ" in l), "")
    cols = freq.split()
    if cols != ["SCHEDULER", "CPUFREQ_MHZ", "POLL%", "C1%", "C2%", "PKGTMP"]:
        fails.append(f"report: frequency/idle header reads {cols}")
    rows = [l for l in lines if l.startswith("EEVDF ")]
    if not rows or rows[0].split()[-2:] != ["1.300", f"{930.0 / 1.3e11 * 1e12:.2f}pJ"]:
        fails.append(f"report: EEVDF energy row reads {rows[:1]}")
    if len(rows) < 2 or rows[1].split()[1:] != ["2950", "0.0", "3.0", "70.0", "71.2C"]:
        fails.append(f"report: EEVDF frequency/idle row reads {rows[1:2]}")
    bpf_row = next((l for l in lines if l.startswith("PANDEMONIUM (BPF)")), "")
    if bpf_row.split()[-2:] != ["n/a", "n/a"]:
        fails.append(f"report: PMU-off row reads {bpf_row!r}, want IPC and EPI n/a")

    for want in ('pandemonium_power_info{version="5.25.0",git_commit="abc1234",'
                 'git_dirty="0",vendor="AMD",model="Ryzen",montauk_version="8.15.0"} 1',
                 "pandemonium_power_montauk_interval_ms 250",
                 'pandemonium_power_joules_pkg{scheduler="EEVDF",workload="messaging"} 930.0000',
                 'pandemonium_power_ipc{scheduler="EEVDF",workload="messaging"} 1.3000',
                 'pandemonium_power_pmu_available{scheduler="EEVDF",workload="messaging"} 1.0000',
                 'pandemonium_power_pmu_available{scheduler="PANDEMONIUM (BPF)",workload="messaging"} 0.0000',
                 'pandemonium_power_cpu_clock_mhz{scheduler="EEVDF",workload="messaging"} 2950.0000',
                 'pandemonium_power_cpu_temp_celsius{scheduler="EEVDF",workload="messaging"} 71.2500',
                 'pandemonium_power_peak_watts{scheduler="EEVDF",workload="messaging"} 44.0000',
                 'pandemonium_power_cstate_residency_pct{scheduler="EEVDF",workload="messaging",state="C2"} 70.0000'):
        if want not in prom.splitlines():
            fails.append(f"prom: missing {want}")
    if 'pandemonium_power_ipc{scheduler="PANDEMONIUM (BPF)"' in prom:
        fails.append("prom: PMU-off cell carries an IPC gauge")
    for text, where in ((report, "report"), (prom, "prom")):
        for word in ("turbostat", "rapl", "perf stat"):
            if word in text.lower():
                fails.append(f"{where}: still names {word}")
    if len(fails) == before:
        note("PASS aggregate, report and .prom carry montauk's numbers")


PERIOD_NS = 6_944_444            # one 144 fps period, never late
PACED_FRAMES = 4320              # 30s at 144 fps


def fake_framebench(reduced, j_pkg: float, frames: list[int], calls: list):
    """record_workload standing in for montauk and framebench: Writes the
    samples the argv names, as framebench does at exit, and its summary."""
    def record_workload(rec_dir, cmd, timeout):
        rec_dir.mkdir(parents=True)
        calls.append((rec_dir, cmd))
        Path(cmd[cmd.index("--out") + 1]).write_text("".join(f"{x}\n" for x in frames))
        Path(cmd[cmd.index("--wake-out") + 1]).write_text("40000\n60000\n")
        Path(cmd[cmd.index("--audio-out") + 1]).write_text("1000\n")
        summary = (f"frames={len(frames)}\nmeasured_s=30.000\nstutters=0\n"
                   f"stutter_ns=0\njob_wakes=2\naudio_periods=1\naudio_rt=1\n"
                   f"wake_mode=0\nspin_us=0\n")
        numbers, cstates = reduced
        return ({"wall_s": 30.2, "montauk": {**numbers, "j_pkg": j_pkg},
                 "cstates": dict(cstates)},
                subprocess.CompletedProcess(cmd, 0, summary, ""))
    return record_workload


def late(n: int, ns: int) -> list[int]:
    return [PERIOD_NS] * (PACED_FRAMES - n) + [ns] * n


def check_paced(m, tmp: Path, on, fails: list) -> None:
    before = len(fails)
    if "paced" not in m.WORKLOADS or m.select_workloads(
            SimpleNamespace(workload="all"), True) != [
            "idle-floor", "messaging", "paced", "schbench"]:
        fails.append("paced: not a --workload choice or not part of all")
    fb, ipu = tmp / "framebench", 12.345
    args = m.FRAME.scenarios(12)["paced"]
    real = m.record_workload
    calls: list = []

    def paced(name, j_pkg, frames):
        m.record_workload = fake_framebench(on, j_pkg, frames, calls)
        return m.run_paced(tmp / "power" / name, fb, ipu, args)

    try:
        a = paced("EEVDF-paced-1", 930.0, late(100, 10_000_000))
        b = paced("EEVDF-paced-2", 930.0, late(200, 12_500_000))
        c = paced("BPF-paced-1", 837.0, late(0, 0))
        short = paced("BPF-paced-2", 837.0, [PERIOD_NS] * 100)
    finally:
        m.record_workload = real

    rec_dir, cmd = calls[0]
    i = cmd.index(args[0])
    if (cmd[0] != str(fb) or cmd[cmd.index("--ipu") + 1] != "12.345"
            or cmd[cmd.index("--duration") + 1] != str(m.PACED_SECS)
            or cmd[cmd.index("--warmup") + 1] != "0"
            or cmd[i:i + len(args)] != args or "--cap-hz" not in args):
        fails.append(f"paced: argv is not prism-frame's paced scenario at warmup 0: {cmd}")
    if Path(cmd[cmd.index("--out") + 1]) != rec_dir.parent / f"{rec_dir.name}.frames":
        fails.append("paced: samples are not written beside the recording")
    if a.get("work_unit") != PACED_FRAMES or a.get("work_unit_label") != "frame":
        fails.append(f"paced: work unit reads {a.get('work_unit')!r} "
                     f"{a.get('work_unit_label')!r}, want {PACED_FRAMES} frame")
    for r, lp, lo in ((a, 100 * 100 / PACED_FRAMES, 100.0),
                      (b, 100 * 200 / PACED_FRAMES, 80.0)):
        if not close(r.get("late_pct", -1), lp) or not close(r.get("low1", -1), lo):
            fails.append(f"paced: parse read late {r.get('late_pct')!r} low1 "
                         f"{r.get('low1')!r}, want {lp} and {lo}")
    if short:
        fails.append("paced: a run of 100 frames in 30s was not discarded")

    agg = m.aggregate_runs([a, b])
    bpf = m.aggregate_runs([c, short])
    for k, v in (("work_unit_mean", PACED_FRAMES), ("j_per_op_mean", 930.0 / PACED_FRAMES),
                 ("late_pct_mean", 100 * 150 / PACED_FRAMES), ("low1_mean", 90.0)):
        if k not in agg or not close(agg[k], v):
            fails.append(f"paced aggregate: {k} = {agg.get(k)!r}, want {v}")
    if bpf.get("runs") != 1:
        fails.append(f"paced aggregate: the discarded run was counted ({bpf.get('runs')})")
    msg = m.aggregate_runs([run(on, 15.0, 5_760_000)])
    if "late_pct_mean" in msg or "low1_mean" in msg:
        fails.append("aggregate: a messaging cell carries frame quality")

    m.LOG_DIR = m.ARCHIVE_DIR = tmp
    results = {"EEVDF": {"messaging": msg, "paced": agg},
               "PANDEMONIUM (BPF)": {"messaging": msg, "paced": bpf}}
    git = {"commit": "abc1234", "dirty": False}
    stamp = "20261107-130000"
    report = m.write_report("5.25.0", git, stamp, 12, "AMD", "Ryzen", "8.15.0",
                            2, 30, results).read_text().splitlines()
    prom = m.write_prometheus("5.25.0", git, stamp, 12, "AMD", "Ryzen", "8.15.0",
                              2, 30, results).read_text().splitlines()

    sec = {wl: report[report.index(f"[{wl.upper()}]") + 1:] for wl in ("messaging", "paced")}
    if "LATE%" in sec["messaging"][0]:
        fails.append("report: the messaging ENERGY table grew frame columns")
    if sec["paced"][0].split()[-3:] != ["EPI_pJ", "LATE%", "LOW1"]:
        fails.append(f"report: paced ENERGY header reads {sec['paced'][0]!r}")
    rows = {n: [l for l in sec["paced"] if l.startswith(n)]
            for n in ("EEVDF ", "PANDEMONIUM (BPF)")}
    eev, pan = rows["EEVDF "], rows["PANDEMONIUM (BPF)"]
    if not eev or "215.28mJ" not in eev[0] or eev[0].split()[-2:] != ["3.47", "90.0"]:
        fails.append(f"report: EEVDF paced row reads {eev[:1]}, want 215.28mJ, 3.47 and 90.0")
    if not pan or pan[0].split()[-2:] != ["0.00", "144.0"]:
        fails.append(f"report: BPF paced row reads {pan[:1]}, want 0.00 and 144.0")
    if len(pan) < 3 or pan[2].split()[-1] != "-10.0%":
        fails.append(f"report: BPF paced J/frame vs EEVDF reads {pan[2:3]}, want -10.0%")

    ls = '{scheduler="EEVDF",workload="paced"}'
    for want in (f"pandemonium_power_frames{ls} 4320",
                 f"pandemonium_power_frame_late_pct{ls} 3.4722",
                 f"pandemonium_power_frame_late_pct_stdev{ls} 1.6368",
                 f"pandemonium_power_frame_low1_fps{ls} 90.0000",
                 f"pandemonium_power_frame_low1_fps_stdev{ls} 14.1421",
                 f"pandemonium_power_joules_per_op{ls} 2.152778e-01"):
        if want not in prom:
            fails.append(f"prom: missing {want}")
    if any(l.startswith("pandemonium_power_frame") and 'workload="messaging"' in l
           for l in prom):
        fails.append("prom: a messaging cell carries frame gauges")
    if len(fails) == before:
        note("PASS paced pairs J/frame with the frame bench's late share and 1% low")


def fake_montauk(tmp: Path, mode: str) -> str:
    path = tmp / f"fake-montauk-{mode}"
    path.write_text(FAKE_MONTAUK.format(python=sys.executable, h21=H21, mode=mode))
    os.chmod(path, 0o755)
    return str(path)


def check_recorder(m, tmp: Path, fails: list) -> None:
    before = len(fails)
    pc.MONTAUK_STOP_S = 0.5

    pc.MONTAUK = fake_montauk(tmp, "clean")
    rec = pc.MontaukRecorder(tmp / "rec-clean")
    if not rec.start() or rec not in pc._LIVE_RECORDERS:
        fails.append("recorder: start did not see the first scrape or register")
    if rec.stop() is not True:
        fails.append("recorder: a SIGINT stop was not reported clean")
    if "final" not in (rec.dir / f"{H21}.prom").read_text():
        fails.append("recorder: no final scrape after SIGINT")
    if rec in pc._LIVE_RECORDERS or rec.proc is not None:
        fails.append("recorder: still registered after stop")
    try:
        pc.MontaukRecorder(tmp / "rec-clean").start()
        fails.append("recorder: a reused directory was accepted")
    except FileExistsError:
        pass

    leaked = pc.MontaukRecorder(tmp / "rec-leaked")
    leaked.start()
    m.stop_recorders()
    if pc._LIVE_RECORDERS or leaked.proc is not None:
        fails.append("recorder: stop_recorders left montauk running")

    pc.MONTAUK = fake_montauk(tmp, "ignore")
    stubborn = pc.MontaukRecorder(tmp / "rec-ignore")
    stubborn.start()
    t0 = time.monotonic()
    clean = stubborn.stop()
    took = time.monotonic() - t0
    if clean or stubborn.proc is not None or not 0.4 <= took < 5.0:
        fails.append(f"recorder: SIGINT-deaf montauk read clean={clean} after "
                     f"{took:.2f}s; want a kill at the 0.5s bound")

    pc.MONTAUK = fake_montauk(tmp, "die")
    if pc.MontaukRecorder(tmp / "rec-die").start():
        fails.append("recorder: a montauk that exited before scraping was accepted")
    m.stop_recorders()

    pc.MONTAUK = fake_montauk(tmp, "clean")
    stub_montauk(m, pmu_on=True)
    got = m.record_workload(tmp / "wl-ok", [sys.executable, "-c", "pass"], timeout=10)
    if got is None or got[0]["montauk"]["j_pkg"] != 930.0 or got[0]["wall_s"] <= 0:
        fails.append(f"record_workload: a good run returned {got!r}")
    for name, cmd, timeout in (
            ("failed", [sys.executable, "-c", "import sys; sys.exit(3)"], 10),
            ("timed-out", [sys.executable, "-c", "import time; time.sleep(5)"], 0.3)):
        d = tmp / f"wl-{name}"
        if m.record_workload(d, cmd, timeout=timeout) is not None:
            fails.append(f"record_workload: a {name} workload was accepted")
        if pc._LIVE_RECORDERS:
            fails.append(f"record_workload: a {name} workload left montauk running")
        if "final" not in (d / f"{H21}.prom").read_text():
            fails.append(f"record_workload: a {name} workload's montauk was not "
                         f"stopped on SIGINT")
    if len(fails) == before:
        note("PASS recorder stops montauk on every path")


def main() -> int:
    m = load_power()
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="prism-power-gate-") as td:
        tmp = Path(td)
        on, off = check_reducer(m, tmp, fails)
        if on is not None and off is not None:
            check_outputs(m, tmp, on, off, fails)
            check_paced(m, tmp, on, fails)
        check_recorder(m, tmp, fails)
    for f in fails:
        note(f"FAIL {f}")
    note("GATE PASSED: prism-power reads, reports and records through montauk"
         if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
