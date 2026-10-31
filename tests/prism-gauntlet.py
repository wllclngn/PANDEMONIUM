#!/usr/bin/env python3
"""
PANDEMONIUM prism-gauntlet: fixed-work applications timed under each scheduler,
with a cyclic latency probe beside every one.

Each workload runs N times under each scheduler and reports its wall time, mean
and stddev, with the winner per row and a total. Beside every run the binary's
own probe runs as cyclictest does: one periodic sleeper per CPU, unpinned, at
SCHED_NORMAL and a 1ms absolute deadline, so each row also reports the wake
latency the scheduler gave a periodic task while that work ran. A last row runs
the cyclic probe alone under the stress workers on every CPU.

Workloads (nothing downloaded; a workload whose tool is missing is skipped with
the fix named up front):

    stress-ng-cache   stress-ng cache stressor, fixed ops per worker.
    primes            stress-ng cpu prime method, fixed ops per worker.
    xz                xz -3 over a 256MB corpus built once from /usr.
    perf-memcpy       perf bench mem memcpy, 1GB over 16 loops.
    argon2            argon2 -t 100 -m 16 -p 4.
    x265              ffmpeg libx265 encode of a 10s 1080p testsrc clip.

Usage:
    ./tests/prism-gauntlet.py
    ./tests/prism-gauntlet.py --iterations 3
    ./tests/prism-gauntlet.py --workloads xz,primes
    ./tests/prism-gauntlet.py --schedulers scx_cake
    ./tests/prism-gauntlet.py --pandemonium-only
"""

import argparse
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    start_and_wait, stop_and_wait, find_scheduler, stop_systemd_scheduler,
    install_exit_guard, register_exit_cleanup, _ACTIVE_GUARDS,
    BINARY, LOG_DIR, BENCH_OWN,
    get_git_info, get_version, get_online_cpus,
    is_scx_active, scx_scheduler_name, wait_for_deactivation,
    log, log_error, log_info, log_warn,
    mean_stdev, PrometheusBuilder, table_header, table_row,
    cpu_max_c, box_uptime_s, box_governor, box_outsiders, probe_tool,
    _chown_to_invoking_user,
)

ASSET_DIR = LOG_DIR / "prism-gauntlet-assets"
XZ_CORPUS = ASSET_DIR / "xz-corpus.bin"
X265_OUTPUT = ASSET_DIR / "x265-out.mkv"

TIMEOUT_S = 600
CYCLIC_INTERVAL_US = 1000
CYCLIC_STRESS_S = 20
CYCLIC_ROW = "cyclic under stress"
# THE PROBE PRINTS WAKES PAST ITS 100MS HISTOGRAM AS AN OVERFLOW COUNT; THEY
# SORT HERE, ABOVE EVERY MEASURED BUCKET, AND READ AS ">100000".
OVER_US = 100000
COOL_BAND_C = 5.0
COOL_MAX_S = 120

# EVERY PROBE AND STRESS WORKER THIS RUN HAS STARTED AND NOT YET REAPED, SO THE
# EXIT GUARD CAN END THEM ON Ctrl+C, SIGTERM OR AN EXCEPTION.
_LIVE: list = []

# THE BENCH'S OWN PROCESSES, SO montauk's OUTSIDER READ DOES NOT COUNT THEM.
GAUNTLET_OWN = BENCH_OWN + ("stress-ng", "xz", "argon2", "ffmpeg", "x265")

SCX_FIELD = [
    "scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
    "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
    "scx_beerland", "scx_layered",
]


@dataclass
class Workload:
    name: str
    probe: Callable[[], bool]
    hint: str
    run: Callable[[int], Optional[float]]


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run_timed(cmd: list[str], timeout: int = TIMEOUT_S,
               stdin_text: Optional[str] = None) -> Optional[float]:
    """Wall seconds for one run of cmd, or None if it failed or timed out."""
    start = time.monotonic()
    try:
        r = subprocess.run(cmd, input=stdin_text, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log_error(f"  timeout after {timeout}s: {' '.join(cmd[:3])}")
        return None
    elapsed = time.monotonic() - start
    if r.returncode != 0:
        log_error(f"  exit {r.returncode}: {' '.join(cmd[:3])}")
        for ln in (r.stderr or "").splitlines()[-3:]:
            log_error(f"    {ln}")
        return None
    return elapsed


def run_stress_ng_cache(ncpus: int) -> Optional[float]:
    ops = max(50000, 500000 // max(1, ncpus // 4))
    return _run_timed(["stress-ng", "--cache", str(ncpus), "--cache-enable-all",
                       "--cache-ops", str(ops)], timeout=300)


def run_primes(ncpus: int) -> Optional[float]:
    ops = max(10000, 200000 // max(1, ncpus // 4))
    return _run_timed(["stress-ng", "--cpu", str(ncpus), "--cpu-method", "prime",
                       "--cpu-ops", str(ops)], timeout=300)


def _ensure_xz_corpus() -> bool:
    """A stable 256MB corpus from /usr binaries, built on first use."""
    target = 256 * 1024 * 1024
    if XZ_CORPUS.exists() and XZ_CORPUS.stat().st_size >= target * 3 // 4:
        return True
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    log_info(f"building the xz corpus at {XZ_CORPUS}")
    written = 0
    try:
        with XZ_CORPUS.open("wb") as out:
            for d in ("/usr/bin", "/usr/lib"):
                for entry in sorted(Path(d).iterdir()):
                    if written >= target:
                        break
                    if not entry.is_file() or entry.is_symlink():
                        continue
                    try:
                        data = entry.read_bytes()
                    except OSError:
                        continue
                    out.write(data)
                    written += len(data)
    except OSError as e:
        log_error(f"  the xz corpus could not be built: {e}")
        return False
    _chown_to_invoking_user(ASSET_DIR)
    return written > 0


def run_xz(_ncpus: int) -> Optional[float]:
    if not _ensure_xz_corpus():
        return None
    return _run_timed(["xz", "-z", "-k", "-c", "-T", "0", "-3", str(XZ_CORPUS)])


def run_perf_memcpy(_ncpus: int) -> Optional[float]:
    return _run_timed(["perf", "bench", "-f", "simple", "mem", "memcpy",
                       "-s", "1GB", "-l", "16"])


def run_argon2(_ncpus: int) -> Optional[float]:
    return _run_timed(["argon2", "pandemonium-salt", "-i", "-t", "100", "-m", "16",
                       "-p", "4", "-l", "32"], stdin_text="pandemonium-password\n")


def _have_libx265() -> bool:
    if not _have("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                       capture_output=True, text=True)
    return "libx265" in r.stdout


def run_x265(_ncpus: int) -> Optional[float]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    return _run_timed(["ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", "testsrc=size=1920x1080:duration=10:rate=30",
                       "-c:v", "libx265", "-preset", "medium",
                       "-x265-params", "log-level=error", "-y", str(X265_OUTPUT)])


WORKLOADS = [
    Workload("stress-ng-cache", lambda: _have("stress-ng"), "install stress-ng",
             run_stress_ng_cache),
    Workload("primes", lambda: _have("stress-ng"), "install stress-ng", run_primes),
    Workload("xz", lambda: _have("xz"), "install xz", run_xz),
    Workload("perf-memcpy", lambda: _have("perf"), "install perf", run_perf_memcpy),
    Workload("argon2", lambda: _have("argon2"), "install argon2", run_argon2),
    Workload("x265", _have_libx265, "install ffmpeg built with libx265", run_x265),
]


# THE CYCLIC PROBE

def cyclic_start(ncpus: int) -> subprocess.Popen:
    return _spawn(
        [str(probe_tool()), "probe", "--threads", str(ncpus),
         "--interval-us", str(CYCLIC_INTERVAL_US), "--hist"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _spawn(cmd: list[str], **kw) -> subprocess.Popen:
    p = subprocess.Popen(cmd, **kw)
    _LIVE.append(p)
    return p


def _reap(p: subprocess.Popen, timeout: float = 5) -> tuple[str, str]:
    """SIGINT p, collect its output, kill it if it does not exit."""
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    if p in _LIVE:
        _LIVE.remove(p)
    return out or "", err or ""


def cyclic_stop(p: subprocess.Popen) -> tuple[Counter, int]:
    """SIGINT the probe and read its histogram: (lateness_us -> count, max_us)."""
    out, err = _reap(p, timeout=10)
    hist: Counter = Counter()
    max_us = None
    for line in out.splitlines():
        f = line.split()
        if len(f) != 2:
            continue
        if f[0] == "max":
            max_us = int(f[1])
        elif f[0] == "over":
            hist[OVER_US] += int(f[1])
        else:
            hist[int(f[0])] += int(f[1])
    if max_us is None:
        log_error(f"  the cyclic probe returned no histogram (exit {p.returncode})")
        for ln in err.splitlines()[-3:]:
            log_error(f"    {ln}")
        return Counter(), 0
    return hist, max_us


def hist_pct(hist: Counter, pct: float) -> Optional[int]:
    n = sum(hist.values())
    if n == 0:
        return None
    need = n * pct / 100.0
    acc = 0
    for us in sorted(hist):
        acc += hist[us]
        if acc >= need:
            return us
    return max(hist)


def cyclic_under_stress(ncpus: int) -> tuple[Counter, int]:
    """The cyclic probe alone, under a pinned stress worker on every CPU."""
    workers = [_spawn([str(probe_tool()), "stress-worker", "--cpu", str(c)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for c in range(ncpus)]
    try:
        time.sleep(1.0)
        p = cyclic_start(ncpus)
        time.sleep(CYCLIC_STRESS_S)
        return cyclic_stop(p)
    finally:
        for w in workers:
            if w.poll() is None:
                w.send_signal(signal.SIGINT)
        for w in workers:
            _reap(w)


# REPORT

def _cell_wall(v: Optional[tuple], best: Optional[float]) -> str:
    if not v:
        return "skip/fail"
    mean, sd = v
    tag = "*" if best is not None and mean == best else " "
    return f"{mean:7.2f}s ±{sd:5.2f}{tag}"


def _us(v: int) -> str:
    return f">{OVER_US}" if v >= OVER_US else str(v)


def _cell_us(v: Optional[int], best: Optional[int]) -> str:
    if v is None:
        return "--"
    tag = "*" if best is not None and v == best else " "
    return f"{_us(v)}{tag}"


def write_report(ver, git, stamp, ncpus, runs, names, rows, wall, cyc, arms,
                 skipped) -> Path:
    lines = [
        "PANDEMONIUM PRISM-GAUNTLET",
        f"VERSION:     {ver}",
        f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
        f"TIMESTAMP:   {stamp}",
        f"CPUS:        {ncpus}",
        f"ITERATIONS:  {runs}",
        f"CYCLIC:      {ncpus} threads at {CYCLIC_INTERVAL_US}us, beside every "
        f"run and {CYCLIC_STRESS_S}s alone under the stress workers",
        f"BOX:         uptime {box_uptime_s() / 3600:.1f}h, governor {box_governor()}",
        "",
        "WALL, MEAN AND STDDEV (* LOWEST)",
        table_header("WORKLOAD", names),
    ]
    totals: dict[str, float] = {}
    for wl in rows:
        if wl == CYCLIC_ROW:
            continue
        means = {s: wall[s][wl][0] for s in names if wall[s].get(wl)}
        best = min(means.values()) if means else None
        lines.append(table_row(wl, [_cell_wall(wall[s].get(wl), best) for s in names]))
    for s in names:
        ws = [wall[s].get(wl) for wl in rows if wl != CYCLIC_ROW]
        if ws and all(ws):
            totals[s] = sum(w[0] for w in ws)
    if totals:
        best = min(totals.values())
        lines.append(table_row("TOTAL", [
            (f"{totals[s]:7.2f}s{'*' if totals[s] == best else ' '}"
             if s in totals else "incomplete") for s in names]))
    for title, key in (("CYCLIC p99 (us, * LOWEST)", 99.0),
                       ("CYCLIC p99.9 (us, * LOWEST)", 99.9)):
        lines += ["", title, table_header("WORKLOAD", names)]
        for wl in rows:
            vals = {s: hist_pct(cyc[s][wl][0], key) for s in names if cyc[s].get(wl)}
            got = [v for v in vals.values() if v is not None]
            best = min(got) if got else None
            lines.append(table_row(wl, [_cell_us(vals.get(s), best) for s in names]))
    lines += ["", "CYCLIC MAX (us)", table_header("WORKLOAD", names)]
    for wl in rows:
        lines.append(table_row(wl, [(_us(cyc[s][wl][1]) if cyc[s].get(wl) else "--")
                                    for s in names]))
    lines += ["", "ARM", table_header("", ["START C", "END C", "OUTSIDER CPU %"])]
    for s in names:
        a = arms.get(s, {})
        lines.append(table_row(s, [
            f"{a['start_c']:.0f}" if a.get("start_c") is not None else "--",
            f"{a['end_c']:.0f}" if a.get("end_c") is not None else "--",
            f"{a.get('outsider_cpu', 0.0):.0f}"]))
        if a.get("outsiders"):
            lines.append(f"  beside it: {a['outsiders']}")
    if skipped:
        lines.append("")
        for s, r in skipped.items():
            lines.append(f"SKIPPED: {s} -- {r}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-gauntlet-{stamp}.log"
    path.write_text("\n".join(lines) + "\n")
    _chown_to_invoking_user(path)
    return path


def write_prometheus(ver, git, stamp, ncpus, names, rows, wall, cyc, arms,
                     skipped) -> Path:
    pb = PrometheusBuilder("gauntlet")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False))
    pb.gauge("cpus", ncpus, help="CPUs online")
    pb.gauge("cyclic_interval_us", CYCLIC_INTERVAL_US, help="cyclic probe period")
    for s in names:
        for wl in rows:
            lab = {"scheduler": s, "workload": wl}
            if wall[s].get(wl):
                pb.gauge("wall_seconds", f"{wall[s][wl][0]:.4f}",
                         help="workload wall time, mean", labels=lab)
                pb.gauge("wall_stdev_seconds", f"{wall[s][wl][1]:.4f}",
                         help="workload wall time, stddev", labels=lab)
            if cyc[s].get(wl):
                h, mx = cyc[s][wl]
                for q, name in ((50.0, "p50"), (99.0, "p99"), (99.9, "p999")):
                    v = hist_pct(h, q)
                    if v is not None:
                        pb.gauge(f"cyclic_{name}_us", v,
                                 help=f"cyclic probe lateness {name}", labels=lab)
                pb.gauge("cyclic_max_us", mx, help="cyclic probe lateness max",
                         labels=lab)
        a = arms.get(s, {})
        for edge in ("start", "end"):
            if a.get(f"{edge}_c") is not None:
                pb.gauge("cpu_max_c", a[f"{edge}_c"],
                         help="package max temperature at the arm's edge",
                         labels={"scheduler": s, "edge": edge})
    for s, r in skipped.items():
        pb.gauge("scheduler_skipped", 1, help="scheduler crashed or failed to activate",
                 labels={"scheduler": s, "reason": r.replace('"', "'")})
    path = LOG_DIR / f"prism-gauntlet-{stamp}.prom"
    path.write_text(pb.render())
    _chown_to_invoking_user(path)
    return path


def _ejected(guard, expected_ops: str) -> bool:
    """The arm's scheduler is gone: its process exited, or the kernel no longer
    runs the ops it registered at activation. EEVDF has no guard."""
    if guard is None:
        return False
    return guard.proc.poll() is not None or scx_scheduler_name() != expected_ops


def _cool_to(base_c: Optional[float]) -> None:
    """Wait, at most COOL_MAX_S, for the package to come back within COOL_BAND_C
    of the run's first reading, so an arm's place in the order is not its heat."""
    if base_c is None:
        return
    t0 = time.monotonic()
    c = cpu_max_c()
    if c is None or c <= base_c + COOL_BAND_C:
        return
    log_info(f"  cooling: {c:.0f} C, waiting for {base_c + COOL_BAND_C:.0f} C")
    while time.monotonic() - t0 < COOL_MAX_S:
        time.sleep(5)
        c = cpu_max_c()
        if c is None or c <= base_c + COOL_BAND_C:
            break
    log_info(f"  cooled to {c if c is not None else 0:.0f} C in "
             f"{time.monotonic() - t0:.0f}s")


def _exit_cleanup() -> None:
    """Run by the exit guard on Ctrl+C, SIGTERM, an exception or a normal exit:
    end every probe and stress worker, stop the arm's scheduler, remove the
    encode output, and write what was measured if the final report was not."""
    for p in list(_LIVE):
        _reap(p, timeout=2)
    for g in list(_ACTIVE_GUARDS):
        try:
            g.stop()
        except Exception:
            pass
    if X265_OUTPUT.exists():
        X265_OUTPUT.unlink()
    _chown_to_invoking_user(ASSET_DIR)
    if _RUN and not _RUN.get("written"):
        write_report(**_RUN["report"])
        write_prometheus(**_RUN["prom"])


# THE RUN's STATE, FOR THE EXIT GUARD's PARTIAL REPORT.
_RUN: dict = {}


# MAIN

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=3,
                    help="Runs per workload per scheduler (default 3).")
    ap.add_argument("--workloads", type=str, default="",
                    help="Comma-separated subset: "
                         + ", ".join(w.name for w in WORKLOADS) + ".")
    ap.add_argument("--schedulers", type=str, default="",
                    help="Comma-separated external scx field; EEVDF always, "
                         "PANDEMONIUM only if named.")
    ap.add_argument("--all-scx", action="store_true",
                    help="Also run every installed production scx scheduler.")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="Run only the PANDEMONIUM arms.")
    ap.add_argument("--cores", type=str, default=None,
                    help="Accepted for suite uniformity; the gauntlet runs at "
                         "native width.")
    ap.add_argument("--trace", action="store_true",
                    help="Accepted for suite uniformity; the gauntlet is not a "
                         "capture.")
    args = ap.parse_args()
    runs = max(1, args.iterations)

    if not BINARY.exists():
        log_error(f"{BINARY} not found -- build PANDEMONIUM first")
        return 1

    ncpus = get_online_cpus()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_info(f"prism-gauntlet v{ver} [{git['commit']}{' (dirty)' if git['dirty'] else ''}]")
    log_info(f"CPUs: {ncpus}  iterations: {runs}  cyclic: {ncpus} threads "
             f"at {CYCLIC_INTERVAL_US}us")
    if args.trace:
        log_warn("--trace has no capture here; the gauntlet runs untraced")

    wanted = {w.strip() for w in args.workloads.split(",") if w.strip()}
    unknown = wanted - {w.name for w in WORKLOADS}
    if unknown:
        log_error(f"unknown workload(s): {', '.join(sorted(unknown))}")
        return 2
    active, missing = [], []
    for w in WORKLOADS:
        if wanted and w.name not in wanted:
            continue
        (active if w.probe() else missing).append(w)
    for w in missing:
        log_warn(f"SKIPPING {w.name} -- {w.hint}")
    # ONE-TIME COSTS STAY OUT OF EVERY ARM: THE CORPUS IS BUILT AND READ ONCE
    # HERE, SO NO ARM's PROBED WINDOW HOLDS THE BUILD AND NO ARM's FIRST xz RUN
    # READS IT COLD.
    if any(w.name == "xz" for w in active):
        if _ensure_xz_corpus():
            with XZ_CORPUS.open("rb") as f:
                while f.read(1 << 24):
                    pass
        else:
            active = [w for w in active if w.name != "xz"]
            log_warn("SKIPPING xz -- the corpus could not be built")
    rows = [w.name for w in active] + [CYCLIC_ROW]

    entries: list[tuple[str, Optional[list[str]]]] = [
        ("EEVDF", None),
        ("PANDEMONIUM (BPF)", [str(BINARY), "--verbose", "--no-adaptive"]),
        ("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]),
    ]
    named = [s.strip() for s in args.schedulers.split(",") if s.strip()]
    if named and not args.pandemonium_only:
        keep_pand = any(s.lower() in ("pandemonium", "scx_pandemonium") for s in named)
        entries = [e for e in entries if e[0] == "EEVDF" or keep_pand]
        for s in named:
            if s.lower() in ("pandemonium", "scx_pandemonium", "eevdf"):
                continue
            if find_scheduler(s):
                entries.append((s, [s]))
            else:
                log_warn(f"{s} not found, skipping")
    elif args.all_scx and not args.pandemonium_only:
        for s in SCX_FIELD:
            if find_scheduler(s):
                entries.append((s, [s]))
    if args.pandemonium_only:
        entries = [e for e in entries if "PANDEMONIUM" in e[0]]

    if is_scx_active():
        log_warn(f"sched_ext is active ({scx_scheduler_name()}) -- stopping the "
                 "pandemonium service")
        stop_systemd_scheduler()
        if not wait_for_deactivation(5.0):
            log_error("could not deactivate sched_ext")
            return 1
    register_exit_cleanup(_exit_cleanup)
    install_exit_guard(eject=True)

    names = [n for n, _ in entries]
    wall: dict[str, dict] = {n: {} for n in names}
    cyc: dict[str, dict] = {n: {} for n in names}
    arms: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    _RUN.update({
        "report": dict(ver=ver, git=git, stamp=stamp, ncpus=ncpus, runs=runs,
                       names=names, rows=rows, wall=wall, cyc=cyc, arms=arms,
                       skipped=skipped),
        "prom": dict(ver=ver, git=git, stamp=stamp, ncpus=ncpus, names=names,
                     rows=rows, wall=wall, cyc=cyc, arms=arms, skipped=skipped),
    })
    base_c = cpu_max_c()
    for sched_name, cmd in entries:
        print()
        log_info(f"[{sched_name}]")
        _cool_to(base_c)
        guard = None
        expected_ops = ""
        if cmd is not None:
            guard = start_and_wait(cmd, sched_name)
            if guard is None:
                skipped[sched_name] = "failed to activate"
                continue
            expected_ops = scx_scheduler_name()
        out_cpu, out_names = box_outsiders(own=GAUNTLET_OWN)
        arms[sched_name] = {"start_c": cpu_max_c(), "outsider_cpu": out_cpu,
                            "outsiders": out_names}
        for wl in active + [None]:
            label = wl.name if wl else CYCLIC_ROW
            samples: list[float] = []
            hist: Counter = Counter()
            max_us = 0
            for i in range(runs):
                if wl is None:
                    h, m = cyclic_under_stress(ncpus)
                    log_info(f"  {label} {i + 1}/{runs}: p99 {hist_pct(h, 99.0)}us")
                else:
                    p = cyclic_start(ncpus)
                    try:
                        time.sleep(0.2)
                        t = wl.run(ncpus)
                    finally:
                        h, m = cyclic_stop(p)
                    log_info(f"  {label} {i + 1}/{runs}: "
                             + (f"{t:.2f}s" if t is not None else "failed")
                             + f", cyclic p99 {hist_pct(h, 99.0)}us")
                    # A FAILED RUN FEEDS NEITHER COLUMN, SO THE WALL TIME AND
                    # THE CYCLIC TAIL COVER THE SAME RUNS.
                    if t is None:
                        h, m = Counter(), 0
                    else:
                        samples.append(t)
                hist.update(h)
                max_us = max(max_us, m)
                if _ejected(guard, expected_ops):
                    break
            if _ejected(guard, expected_ops):
                skipped[sched_name] = f"ejected during {label}"
                log_error(f"[{sched_name}] ejected during {label} -- the rest "
                          "would measure EEVDF; SKIPPED")
                break
            if samples:
                wall[sched_name][label] = mean_stdev(samples)
            if hist:
                cyc[sched_name][label] = (hist, max_us)
            write_prometheus(ver, git, stamp, ncpus, names, rows, wall, cyc,
                             arms, skipped)
        arms[sched_name]["end_c"] = cpu_max_c()
        if guard is not None:
            stop_and_wait(guard)
        time.sleep(2)

    report = write_report(ver, git, stamp, ncpus, runs, names, rows, wall, cyc,
                          arms, skipped)
    prom = write_prometheus(ver, git, stamp, ncpus, names, rows, wall, cyc, arms,
                            skipped)
    _RUN["written"] = True
    print()
    log.report(report.read_text())
    log_info(f"REPORT: {report}")
    log_info(f"METRICS: {prom}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
