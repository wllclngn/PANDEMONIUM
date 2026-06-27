#!/usr/bin/env python3
# prism-strand: per-CPU kthread dispatch-strand validation.
#
# Induces the writeback-freeze condition -- every non-drain core saturated while
# a fsync/writeback storm keeps the per-CPU block-I/O-completion and writeback
# kthreads (PF_KTHREAD, nr_cpus_allowed == 1) runnable. Those threads can ONLY be
# dispatched by their one CPU; if the scheduler holds that CPU on a long slice or
# lets it idle tickless, they strand until the 30s watchdog ejects -- the desktop
# freezes (audio/input survive, anything touching disk wedges).
#
# montauk is the instrument: a --trace-out capture across the window, folded by
# `montauk_analyze --report kstrand`, which ranks the worst-stranded kthreads and
# splits each wait HELD (CPU busy through it -- a genuine scheduler strand) from
# DARK (CPU idle/tickless -- no rescue scan). A HELD strand in the 100ms+ band is
# the freeze signature. This bench asserts that signature is ABSENT under
# PANDEMONIUM's pinned-task work-conservation (the enqueue fast-path that routes a
# 1-CPU task straight to its own per-CPU DSQ and KICK_PREEMPTs it).
#
# Standalone (run directly under sudo) and part of the prism-* family.

import argparse
import ctypes
import multiprocessing as mp
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    LOG_DIR, get_version, get_git_info,
    log_info, log_warn, log_error,
    run_cmd_capture, check_sources_changed, build,
    is_scx_active, scx_scheduler_name, get_online_cpus,
    montauk_trace, montauk_available,
    PrometheusBuilder, table_header, table_row,
)

MONTAUK_ANALYZE = "/usr/local/bin/montauk_analyze"
WORKER_COMM = "pand-strand"          # montauk --trace anchor (kstrand is system-wide)
FREEZE_SIGNATURE_MS = 100.0          # HELD strand at/above this = the writeback freeze


# LOAD WORKERS

def _set_comm(name: str) -> None:
    # prctl(PR_SET_NAME) so `montauk --trace pand-strand` attaches to the load.
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(15, name.encode()[:15], 0, 0, 0)
    except OSError:
        pass


def _cpu_hog(core: int) -> None:
    # Peg one core so every per-CPU kthread bound to it must be DISPATCHED against
    # a running hog -- the HELD-strand condition. Pinned so saturation is even.
    _set_comm(WORKER_COMM)
    try:
        os.sched_setaffinity(0, {core})
    except OSError:
        pass
    x = 0
    while True:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF


def _io_writer(path: str) -> None:
    # write + fsync in a tight loop: each fsync drives the block layer and forces
    # writeback, keeping the per-CPU completion/writeback kthreads runnable. These
    # are the strand victims. Floats across the saturated cores.
    _set_comm(WORKER_COMM)
    buf = b"\0" * 65536
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError:
        return
    off = 0
    while True:
        try:
            os.pwrite(fd, buf, off)
            os.fsync(fd)
        except OSError:
            break
        off += len(buf)
        if off > 256 * 1024 * 1024:
            off = 0


def _io_writer_bursty(path: str, idle_s: float) -> None:
    # DARK-mode writer: a short fsync burst, then SLEEP so the CPU goes idle and
    # tickless. The per-CPU writeback/completion kthread then becomes runnable on
    # an idle, un-ticked CPU -- the DARK strand condition (no tick-driven rescue
    # scan ever fires there). With the pinned-task enqueue kick, the kthread must
    # still be dispatched promptly; without it, it strands until the next tick that
    # never comes. No CPU hogs in this mode -- the cores MUST be free to idle.
    _set_comm(WORKER_COMM)
    buf = b"\0" * 65536
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError:
        return
    off = 0
    while True:
        for _ in range(8):                 # a short burst of fsync'd writes
            try:
                os.pwrite(fd, buf, off)
                os.fsync(fd)
            except OSError:
                return
            off += len(buf)
            if off > 64 * 1024 * 1024:
                off = 0
        time.sleep(idle_s)                 # let the CPU idle + go tickless


def start_load(n_hogs: int, n_writers: int, scratch: Path,
               dark: bool = False, idle_s: float = 0.15) -> list:
    procs = []
    # DARK mode runs NO hogs -- cores must be free to go idle/tickless.
    if not dark:
        for c in range(n_hogs):
            p = mp.Process(target=_cpu_hog, args=(c,), daemon=True)
            p.start()
            procs.append(p)
    for w in range(n_writers):
        if dark:
            p = mp.Process(target=_io_writer_bursty,
                           args=(str(scratch / f"wb-{w}.dat"), idle_s), daemon=True)
        else:
            p = mp.Process(target=_io_writer,
                           args=(str(scratch / f"wb-{w}.dat"),), daemon=True)
        p.start()
        procs.append(p)
    return procs


def stop_load(procs: list, scratch: Path) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        p.join(timeout=3)
    for f in scratch.glob("wb-*.dat"):
        try:
            f.unlink()
        except OSError:
            pass


# KSTRAND REPORT PARSE
# Matches montauk trace_analyze.cpp's kstrand emit() exactly:
#   "VERDICT: no per-CPU kthread strands over threshold ..."   (clean)
#   "N strands across M per-CPU kthreads ..."
#   "worst HELD strand X.Xms (CPU busy through the wait ...)"
#   table: kthread cpu strands max_ms p99_ms held dark
# montauk also emits montauk_analysis_kstrand_worst_held_ms as a prom metric --
# the machine-readable source if a future --prom path is wired; the text report
# is the guaranteed surface today.

def parse_kstrand(report: str) -> dict:
    out = {"strands": 0, "kthreads": 0, "worst_held_ms": 0.0,
           "total_dark": 0, "total_held": 0, "offenders": [], "clean": False}
    if "no per-CPU kthread strands over threshold" in report:
        out["clean"] = True
        return out
    m = re.search(r"(\d+)\s+strands across\s+(\d+)\s+per-CPU kthreads", report)
    if m:
        out["strands"] = int(m.group(1))
        out["kthreads"] = int(m.group(2))
    m = re.search(r"worst HELD strand\s+([\d.]+)ms", report)
    if m:
        out["worst_held_ms"] = float(m.group(1))
    in_table = False
    for line in report.splitlines():
        if line.startswith("kthread") and "max_ms" in line:
            in_table = True
            continue
        if in_table:
            f = line.split()
            if len(f) != 7:
                in_table = False
                continue
            try:
                row = {"comm": f[0], "cpu": int(f[1]), "strands": int(f[2]),
                       "max_ms": float(f[3]), "p99_ms": float(f[4]),
                       "held": int(f[5]), "dark": int(f[6])}
            except ValueError:
                in_table = False
                continue
            out["offenders"].append(row)
            out["total_held"] += row["held"]
            out["total_dark"] += row["dark"]
    # montauk emits worst_held_ms but not a worst_dark_ms (a montauk gap worth
    # closing -- see the report note). Derive it from kthreads whose strands were
    # ALL dark, so their max_ms is unambiguously a dark strand. In DARK mode (no
    # hogs) every strand is dark, so this is the true worst dark wait.
    out["worst_dark_ms"] = max(
        (o["max_ms"] for o in out["offenders"] if o["dark"] > 0 and o["held"] == 0),
        default=0.0)
    return out


def analyze(events: Path) -> tuple[str, dict]:
    rc, so, se = run_cmd_capture(
        [MONTAUK_ANALYZE, str(events), "--report", "kstrand"])
    if rc != 0:
        log_error(f"montauk_analyze failed (rc={rc}): {se.strip()[:200]}")
        return so + se, {}
    return so, parse_kstrand(so)


# PROBE: one mode = one load shape + one montauk capture + one verdict.
# held: saturated cores -> HELD strands (busy-CPU). dark: idle/tickless cores
# between fsync bursts -> DARK strands (the original tickless bug). The fix must
# clear BOTH axes; the verdict keys on the axis the mode actually exercises.

def run_probe(mode: str, args, sched: str, online: int, drain: int,
              scratch: Path, stamp: str) -> int:
    dark = (mode == "dark")
    n_hogs = 0 if dark else online - 1
    if dark:
        log_info(f"strand probe [DARK]: 0 hogs (cores idle/tickless), "
                 f"{args.writers} bursty writers, {args.idle_ms:.0f}ms idle gaps, "
                 f"drain core {drain}")
    else:
        log_info(f"strand probe [HELD]: {n_hogs} CPU hogs (cores 0..{max(n_hogs - 1, 0)}), "
                 f"{args.writers} fsync writers, drain core {drain}")
    log_info(f"holding load {args.duration:.0f}s under montauk capture")

    procs = start_load(n_hogs, args.writers, scratch, dark=dark,
                       idle_s=args.idle_ms / 1000.0)
    time.sleep(2.0)                    # let the load ramp before the window opens
    events = None
    try:
        with montauk_trace(WORKER_COMM, f"strand-{mode}", stamp,
                           events=True, pin_cpu=drain) as rec:
            time.sleep(args.duration)
            events = rec.events_path
    finally:
        stop_load(procs, scratch)

    if events is None or not Path(events).exists():
        log_error("montauk produced no --trace-out capture -- cannot analyze")
        return 2
    report_text, parsed = analyze(Path(events))

    held = parsed.get("worst_held_ms", 0.0)
    darkw = parsed.get("worst_dark_ms", 0.0)
    thr = args.held_threshold_ms

    # VERDICT -- key on the axis this mode exercises.
    if parsed.get("clean", False):
        verdict, rc = "PASS", 0
        log_info(f"kstrand [{mode}]: no per-CPU kthread strands over threshold -- clean")
    elif dark:
        if darkw >= thr:
            verdict, rc = "FAIL", 1
            log_error(f"kstrand [dark]: worst DARK strand {darkw:.1f}ms >= {thr:.0f}ms -- "
                      "tickless-idle strand present (the enqueue kick is not waking the CPU)")
        elif parsed.get("total_dark", 0) > 0:
            verdict, rc = "PASS", 0
            log_warn(f"kstrand [dark]: DARK strands present but worst {darkw:.1f}ms < "
                     f"{thr:.0f}ms -- below the freeze band")
        else:
            verdict, rc = "PASS", 0
            log_info("kstrand [dark]: no DARK strands -- the enqueue kick woke every "
                     "tickless CPU")
    else:
        if held >= thr:
            verdict, rc = "FAIL", 1
            log_error(f"kstrand [held]: worst HELD strand {held:.1f}ms >= {thr:.0f}ms -- "
                      "writeback-freeze signature present")
        elif parsed.get("total_held", 0) > 0:
            verdict, rc = "PASS", 0
            log_warn(f"kstrand [held]: HELD strands present but worst {held:.1f}ms < "
                     f"{thr:.0f}ms -- below the freeze band")
        else:
            verdict, rc = "PASS", 0
            log_info("kstrand [held]: no HELD strands -- every pinned kthread dispatched")

    # REPORT
    print()
    log_info(f"prism-strand [{mode}] {verdict}  (scheduler={sched or 'none'}, "
             f"cores={online}, duration={args.duration:.0f}s)")
    log_info(f"strands {parsed.get('strands', 0)} across {parsed.get('kthreads', 0)} "
             f"kthreads | worst HELD {held:.1f}ms DARK {darkw:.1f}ms | "
             f"HELD {parsed.get('total_held', 0)} DARK {parsed.get('total_dark', 0)}")
    offenders = parsed.get("offenders", [])
    if offenders:
        print(table_header("kthread", ["cpu", "strands", "max_ms", "held", "dark"]))
        for o in offenders[:10]:
            print(table_row(o["comm"],
                            [str(o["cpu"]), str(o["strands"]),
                             f"{o['max_ms']:.1f}", str(o["held"]), str(o["dark"])]))
    # montauk is the instrument -- show its full verdict text.
    print()
    print(report_text.rstrip())

    # PROMETHEUS
    git = get_git_info()
    pb = PrometheusBuilder("strand")
    pb.info(version=get_version(), git_commit=git["commit"], git_dirty=git["dirty"])
    labels = {"scheduler": sched or "none", "cpus": str(online), "mode": mode}
    pb.gauge("worst_held_ms", held, help="worst HELD per-CPU kthread strand", labels=labels)
    pb.gauge("worst_dark_ms", darkw, help="worst DARK (tickless) kthread strand", labels=labels)
    pb.gauge("held_strands", parsed.get("total_held", 0),
             help="HELD strand count (busy-CPU scheduler strands)", labels=labels)
    pb.gauge("dark_strands", parsed.get("total_dark", 0),
             help="DARK strand count (idle/tickless strands)", labels=labels)
    pb.gauge("pass", 1 if rc == 0 else 0,
             help="1 = no freeze-signature strand on this mode's axis", labels=labels)
    out = LOG_DIR / f"strand-{mode}-{stamp}.prom"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pb.render())
    log_info(f"prometheus -> {out}")
    return rc


# MAIN

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate the per-CPU kthread strand fix via montauk kstrand.")
    ap.add_argument("--mode", choices=["held", "dark", "both"], default="held",
                    help="held: saturated cores (HELD strands); dark: idle/tickless "
                         "cores between fsync bursts (DARK strands); both: run each "
                         "(default held)")
    ap.add_argument("--duration", type=float, default=90.0,
                    help="seconds to hold the load under capture (default 90)")
    ap.add_argument("--idle-ms", type=float, default=150.0,
                    help="DARK mode: idle gap between fsync bursts so cores go "
                         "tickless (default 150)")
    ap.add_argument("--writers", type=int, default=4,
                    help="concurrent fsync/writeback writers (default 4)")
    ap.add_argument("--held-threshold-ms", type=float, default=FREEZE_SIGNATURE_MS,
                    help="HELD strand at/above this fails the bench (default 100)")
    ap.add_argument("--kstrand-thresh-ms", type=float, default=5.0,
                    help="montauk strand-capture threshold (default 5ms)")
    ap.add_argument("--scratch", default="/tmp/pandemonium",
                    help="writeback scratch directory")
    ap.add_argument("--no-build", action="store_true",
                    help="skip the source-change rebuild check")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="accepted for `prism --dev` parity; this bench is "
                         "PANDEMONIUM-only already (no EEVDF arm), so it is a no-op")
    ap.add_argument("--coldwake", action="store_true",
                    help="also run prism-coldwake after the strand probe -- co-located "
                         "suite (both exercise cold/idle-core dispatch + montauk capture)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        log_error("must run as root (montauk + scheduler need CAP_SYS_ADMIN) -- "
                  "re-run under sudo")
        return 2
    if not montauk_available():
        log_error("montauk not found at /usr/local/bin/montauk -- install montauk")
        return 2

    if not args.no_build and check_sources_changed():
        log_info("sources changed -- rebuilding")
        if not build():
            log_error("build failed")
            return 2

    sched = scx_scheduler_name()
    if not is_scx_active():
        log_warn("no sched_ext scheduler active -- the result reflects the "
                 "in-kernel default, not PANDEMONIUM")
    elif sched != "pandemonium":
        log_warn(f"active scheduler is '{sched}', not pandemonium -- the kstrand "
                 f"result characterizes '{sched}'")
    else:
        log_info("active scheduler: pandemonium")

    online = get_online_cpus()
    if online < 2:
        log_error("need >= 2 online CPUs (one is reserved as the montauk drain core)")
        return 2
    drain = online - 1                 # montauk pins here so it never drops events
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.environ["MONTAUK_KSTRAND_THRESH_NS"] = str(int(args.kstrand_thresh_ms * 1_000_000))

    modes = ["held", "dark"] if args.mode == "both" else [args.mode]
    rc = 0
    for m in modes:
        rc = max(rc, run_probe(m, args, sched, online, drain, scratch, stamp))

    # Co-located suite: prism-coldwake runs its own (unmodified) cold-core ramp
    # after the strand probe -- one command, both behaviors, neither blended.
    if args.coldwake:
        print()
        log_info("co-located suite: running prism-coldwake (cold-core ramp)...")
        cw = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "pandemonium-tests.py"),
             "prism-coldwake"],
            cwd=Path(__file__).parent.parent)
        rc = max(rc, cw.returncode)
    return rc


if __name__ == "__main__":
    sys.exit(main())
