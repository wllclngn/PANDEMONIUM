#!/usr/bin/env python3
# prism-golden: behavioral goldens over PANDEMONIUM's own workloads.
#
# montauk owns the gate; this owns the workload and the bookkeeping. A recording
# carries both lanes -- the .events stream gives each report a categorical CLASS
# (compared exactly, no tolerance) and the montauk_*.prom scrapes give gauges
# (compared within a tolerance band, and the only place the deterministic PMU
# tier lives). So every mode here captures a RECORDING DIRECTORY, never a bare
# event stream.
#
# THREE MODES, and the first decides the third:
#   --stability   N captures, unchanged build, per-report flip rate. A class the
#                 run flips is not a golden candidate -- it belongs outside the
#                 frozen set with its reason recorded, the way montauk refuses to
#                 golden its own version string. Running this FIRST is the whole
#                 discipline: a frozen set chosen without knowing which reports
#                 flip is a gate that fails at random and gets switched off.
#   --freeze      one capture -> a golden under --goldens, labelled.
#   (default)     one capture checked against the stored golden.
#                 Exit 0 pass, 1 a fact moved, 2 DECLINED. DECLINED is NOT green:
#                 a capture under 95% complete, or of unknown completeness, means
#                 the gate could not run, which is a different fact from passing.
#
# Standalone (run under sudo) and part of the prism-* family.

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    LOG_DIR, get_version, get_git_info,
    log_info, log_warn, log_error,
    check_sources_changed, build,
    is_scx_active, scx_scheduler_name, get_online_cpus,
    montauk_trace, montauk_available,
    PrometheusBuilder, table_header, table_row,
)

MSG_COMM = "sched-messaging"
GOLDEN_DIR = Path(__file__).parent / "goldens"

# Reports whose class is a property of the CAPTURE rather than the workload.
# montauk refuses to freeze placement-race = NO-IDLE-STREAM on its own; the rest
# are here because its own stability run found them run-dependent on a workload
# whose placement is arbitrary by construction. PANDEMONIUM's --stability mode
# exists to decide this list for PANDEMONIUM's workloads rather than inherit it.
# Reports excluded from PANDEMONIUM's frozen set, each for a MEASURED reason.
# Decided 2026-08-03 from ten 30s captures of the sched-messaging storm on one
# unchanged build (--stability), not inherited from another project's workload:
#   wakers          flipped HOT-WAKERS / NO-HOT-WAKERS across the ten. Whether a
#                   waker crosses the hot threshold is a rate sitting near a
#                   boundary under a storm that varies run to run.
#   placement-race  a capture limitation on this workload (NO-IDLE-STREAM), not a
#                   finding. montauk records it as `skipped` rather than aborting
#                   the freeze; excluding it keeps the artifact honest about what
#                   was a decision versus what the capture could not answer.
# Everything else held one token 10/10 -- including dispatch-stall at
# PREEMPT-STARVED, which flips on a CPU burner and does NOT flip here. That is the
# report this whole capability exists to gate, and it is freezable on our
# workloads precisely because they are not a CPU burner.
DEFAULT_EXCLUDE = ("wakers", "placement-race")

# The deterministic tier: cumulative PMU counters, invariant to clock and thermals
# on fixed hardware, frozen with `last` as the reduction. This is the lane that
# would have caught the fork-thread migration regression -- cpu_migrations_total
# went 1.38M to 3.16M against a stable EEVDF control while wake2run p99 IMPROVED,
# so the counter is loud exactly where the latency gauge is silent.
DEFAULT_WATCH = ("montauk_pmu",)


def resolve_analyze() -> str:
    env = os.environ.get("MONTAUK_ANALYZE")
    if env and Path(env).exists():
        return env
    found = shutil.which("montauk_analyze")
    if found:
        return found
    log_error("montauk_analyze not found -- set MONTAUK_ANALYZE or install montauk")
    sys.exit(2)


# WORKLOAD. Default is the migration-heavy sched-messaging storm (the same load
# prism-locality uses), in its own process group so one killpg takes the loop and
# every perf child with it. --workload overrides with any shell command.

def start_load(cmd: str | None, groups: int, loops: int) -> subprocess.Popen:
    if cmd is None:
        cmd = (f"while true; do perf bench sched messaging -g {groups} -l {loops} "
               f">/dev/null 2>&1 || sleep 0.2; done")
    return subprocess.Popen(["bash", "-c", cmd], preexec_fn=os.setsid)


def stop_load(p: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def capture(comm: str, label: str, duration: float, drain: int,
            cmd: str | None, groups: int, loops: int) -> Path | None:
    """One COMPLETE recording directory around one run of the workload.

    MontaukTrace writes the .prom scrapes into `.dir` but the --trace-out event
    stream to a SIBLING `.events` file, so `.dir` alone is a .prom-only recording
    -- montauk refuses the functional lane on one of those, correctly, because no
    event stream means no classes. Both lanes need them together, so the stream is
    moved in once the capture closes. A rename on the same filesystem, not a copy;
    these are tens of MB per capture.
    """
    load = start_load(cmd, groups, loops)
    time.sleep(2.0)                      # let the load ramp before the window
    rec_dir = events = None
    try:
        with montauk_trace(comm, label, time.strftime("%Y%m%d-%H%M%S"),
                           events=True, pin_cpu=drain) as rec:
            time.sleep(duration)
            rec_dir, events = rec.dir, rec.events_path
    finally:
        stop_load(load)
    if rec_dir is None or not Path(rec_dir).is_dir():
        return None
    rec_dir = Path(rec_dir)
    if events is not None and Path(events).is_file():
        try:
            Path(events).replace(rec_dir / Path(events).name)
        except OSError as e:                     # cross-device, or a racing reader
            log_warn(f"could not move the event stream into the recording ({e}) -- "
                     "the functional lane will refuse")
    return rec_dir


# GOLDEN FILE. Plain text, one frozen fact per line, format v2:
#   class <report> <TOKEN>
#   gauge <name> <value> <reduction> ...
# Only the class lines are needed to answer the stability question.

def classes_of(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if line.startswith("class "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                out[parts[1]] = parts[2].strip()
    return out


def freeze(analyze: str, rec: Path, dest: Path, label: str,
           exclude: list, watch: list, tolerance: float | None,
           floor: float | None, reduce_mode: str | None,
           lane: str | None = None, allow_unknown: bool = False) -> tuple[int, str]:
    """Freeze a golden from a recording. Returns (rc, combined output).

    A report whose class is a capture limitation is recorded as `skipped` by the
    freeze rather than aborting it, so --exclude here is for reports that cannot
    DISCRIMINATE on this workload -- a different judgment, and ours to make.
    """
    cmd = [analyze, str(rec), "--golden", str(dest), "--update", "--label", label]
    if lane:
        cmd.append(f"--{lane}")
    if allow_unknown:
        cmd.append("--allow-unknown")
    if exclude:
        cmd += ["--exclude", ",".join(exclude)]
    for pat in watch:
        cmd += ["--watch", pat]
    if tolerance is not None:
        cmd += ["--tolerance", str(tolerance)]
    if floor is not None:
        cmd += ["--floor", str(floor)]
    if reduce_mode:
        cmd += ["--reduce", reduce_mode]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def check(analyze: str, rec: Path, golden: Path, lane: str | None,
          allow_unknown: bool) -> tuple[int, str]:
    cmd = [analyze, str(rec), "--golden", str(golden)]
    if lane:
        cmd.append(f"--{lane}")
    if allow_unknown:
        cmd.append("--allow-unknown")
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def mode_stability(args, analyze: str, drain: int, sched: str, online: int) -> int:
    """N captures of one unchanged build -> per-report flip rate.

    Deliberately not pass/fail. The output is the split; the decision about
    which reports enter a frozen set is the operator's, made with it in hand.
    """
    tmp = Path(args.outdir or "/tmp/pandemonium/golden-stability")
    tmp.mkdir(parents=True, exist_ok=True)
    seen = defaultdict(list)
    kept = 0

    for i in range(args.captures):
        log_info(f"stability capture {i + 1}/{args.captures} "
                 f"({args.duration:.0f}s under load)")
        rec = capture(args.comm, f"golden-stab{i}", args.duration, drain,
                      args.workload, args.groups, args.loops)
        if rec is None:
            log_warn(f"capture {i + 1} produced no recording -- skipped")
            continue
        g = tmp / f"cap{i}.golden"
        rc, out = freeze(analyze, rec, g, f"stab{i}", args.exclude,
                         [], None, None, None)
        if rc != 0 or not g.exists():
            log_warn(f"capture {i + 1} did not freeze (rc={rc}): "
                     f"{out.strip().splitlines()[-1] if out.strip() else 'no output'}")
            continue
        kept += 1
        for report, token in classes_of(g).items():
            seen[report].append(token)

    if kept < 2:
        log_error(f"only {kept} usable capture(s) -- need >= 2 to measure a flip rate")
        return 2

    stable, unstable = [], []
    for report, tokens in sorted(seen.items()):
        distinct = sorted(set(tokens))
        partial = len(tokens) < kept          # report absent from some captures
        if len(distinct) == 1 and not partial:
            stable.append((report, distinct[0], len(tokens)))
        else:
            unstable.append((report, distinct, len(tokens), partial))

    print()
    log_info(f"prism-golden stability  (scheduler={sched or 'none'}, cores={online}, "
             f"{kept}/{args.captures} usable captures)")
    print()
    print(table_header("report", ["verdict", "value(s)", "n"]))
    for r, tok, n in stable:
        print(table_row(r, ["STABLE", tok, str(n)]))
    for r, toks, n, partial in unstable:
        print(table_row(r, ["PARTIAL" if partial else "UNSTABLE",
                            ",".join(toks), str(n)]))
    print()
    log_info(f"{len(stable)} stable, {len(unstable)} unstable/partial. The stable set "
             "is the golden candidate; an unstable report belongs OUTSIDE the frozen "
             "set with its reason recorded, not treated as flakiness.")

    git = get_git_info()
    pb = PrometheusBuilder("golden_stability")
    pb.info(version=get_version(), git_commit=git["commit"], git_dirty=git["dirty"])
    base = {"scheduler": sched or "none", "cpus": str(online)}
    pb.gauge("captures", kept, help="usable captures in the stability run", labels=base)
    pb.gauge("reports_stable", len(stable), help="classes holding one token", labels=base)
    pb.gauge("reports_unstable", len(unstable),
             help="classes that flipped or were absent from some captures", labels=base)
    for r, _tok, _n in stable:
        pb.gauge("report_stable", 1, help="1 = class held across every capture",
                 labels={**base, "report": r})
    for r, _toks, _n, _p in unstable:
        pb.gauge("report_stable", 0, help="1 = class held across every capture",
                 labels={**base, "report": r})
    out = LOG_DIR / f"golden-stability-{time.strftime('%Y%m%d-%H%M%S')}.prom"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pb.render())
    log_info(f"prometheus -> {out}")
    return 0


# ARCHIVE MODE. The benchmark .prom archive is a golden source in its own right,
# and a better one than a live capture for release numbers: no root, no eBPF, no
# ring pressure, and every release since v0.9.8 is already on disk. montauk treats
# a .prom-only directory as a legitimate recording -- gauges alone are a real
# golden -- so the performance lane runs over it unchanged. Completeness is
# UNKNOWN by construction (a bench .prom was never a trace and carries no drop
# accounting), which is honest rather than a workaround, so --allow-unknown is
# passed here and only here.

# The load-bearing cells: what the README reports and what any scheduler change
# is most likely to move. Narrow ON PURPOSE -- freezing a whole run pins
# timestamps and sample counts, and a golden nobody trusts is a golden nobody
# reads.
ARCHIVE_METRICS = (
    "pandemonium_scale_latency_p99_us",
    "pandemonium_scale_deadline_miss_ratio",
    "pandemonium_scale_ipc_rtt_p99_us",
    "pandemonium_scale_burst_p99_us",
    "pandemonium_scale_longrun_latency_p99_us",
    "pandemonium_scale_mixed_latency_p99_us",
)


def archive_runs(archive: Path, version: str, must_contain: str = "") -> list:
    """Runs of one version, newest first, optionally only those carrying a metric.

    NEWEST IS NOT MOST COMPLETE. One version has many .prom -- a full width sweep,
    a focused ipc run, a power run -- and the newest is often the narrowest. A
    golden frozen from whichever landed last would silently watch a different set
    of cells each release, so the run is chosen by what it CONTAINS.
    """
    hits = sorted((p for p in archive.glob(f"{version}-*.prom") if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not must_contain:
        return hits
    return [p for p in hits if must_contain in p.read_text()]


def mode_archive(args, analyze: str) -> int:
    """Freeze or check release numbers straight from the .prom archive.

    A cross-version check only means something when both runs carry the same
    cells; montauk declines by name when the golden freezes a gauge the run did
    not emit, which is the right failure and not a bug to work around. So the
    watch set is per-scheduler-arm and per-width, and a version whose bench
    config differs simply declines rather than silently comparing a different
    machine's numbers.
    """
    archive = Path(args.archive)
    probe = f'{ARCHIVE_METRICS[0]}{{scheduler="PANDEMONIUM (BPF)",cores="{args.cores}"}}'
    runs = archive_runs(archive, args.version, probe)
    if not runs:
        total = len(archive_runs(archive, args.version))
        log_error(f"no {args.version} run in {archive} carries {ARCHIVE_METRICS[0]} "
                  f"at {args.cores} cores ({total} run(s) of this version exist, none "
                  "with that width -- a focused bench, not a width sweep)")
        return 2
    src = runs[0]
    log_info(f"archive {args.version}: {len(runs)} run(s) with {args.cores}C cells, "
             f"using {src.name}")

    # Stage in a private temp dir, NOT under TRACE_DIR: archive mode is
    # unprivileged by design, and TRACE_DIR is root-owned wherever a capture has
    # run. montauk wants a directory, so one .prom gets its own.
    staged = Path(tempfile.mkdtemp(prefix=f"golden-archive-{args.version}-"))
    shutil.copy(src, staged)

    watch = args.watch if args.watch != list(DEFAULT_WATCH) else [
        f'{m}{{scheduler="{arm}",cores="{args.cores}"}}'
        for m in ARCHIVE_METRICS
        for arm in ("PANDEMONIUM (BPF)", "PANDEMONIUM (ADAPTIVE)", "EEVDF")
    ]
    dest = Path(args.goldens) / f"{args.label}.golden"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        return _archive_run(args, analyze, staged, dest, watch)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _archive_run(args, analyze: str, staged: Path, dest: Path, watch: list) -> int:
    if args.freeze:
        rc, out = freeze(analyze, staged, dest, args.label, [], watch,
                         args.tolerance if args.tolerance is not None else 10.0,
                         args.floor, args.reduce,
                         lane="performance", allow_unknown=True)
        print(out.rstrip())
        if rc != 0:
            return rc
        log_info(f"golden -> {dest}")
        return 0

    if not dest.is_file():
        log_error(f"no golden at {dest} -- run with --archive --freeze first")
        return 2
    rc, out = check(analyze, staged, dest, "performance", True)
    print(out.rstrip())
    if rc == 0:
        log_info("archive golden PASS")
    elif rc == 1:
        log_error("archive golden FAIL -- a release number moved")
    else:
        log_error("archive golden DECLINED -- the gate could not run")
    return rc


def mode_freeze(args, analyze: str, drain: int, sched: str) -> int:
    dest = Path(args.goldens) / f"{args.label}.golden"
    dest.parent.mkdir(parents=True, exist_ok=True)
    log_info(f"freezing '{args.label}' from one {args.duration:.0f}s capture")
    rec = capture(args.comm, f"golden-{args.label}", args.duration, drain,
                  args.workload, args.groups, args.loops)
    if rec is None:
        log_error("no recording produced -- cannot freeze")
        return 2
    rc, out = freeze(analyze, rec, dest, args.label, args.exclude,
                     args.watch, args.tolerance, args.floor, args.reduce)
    print(out.rstrip())
    if rc != 0:
        log_error(f"freeze refused (rc={rc}) -- a refusal is a finding, not a failure: "
                  "a class that reflects the capture rather than the workload must not "
                  "be frozen. Exclude that report and re-run.")
        return rc
    log_info(f"golden -> {dest}")
    return 0


def mode_check(args, analyze: str, drain: int, sched: str) -> int:
    golden = Path(args.goldens) / f"{args.label}.golden"
    if not golden.is_file():
        log_error(f"no golden at {golden} -- run with --freeze --label {args.label} first")
        return 2
    log_info(f"checking against '{args.label}'")
    rec = capture(args.comm, f"golden-check-{args.label}", args.duration, drain,
                  args.workload, args.groups, args.loops)
    if rec is None:
        log_error("no recording produced -- cannot check")
        return 2
    lane = "functional" if args.functional else ("performance" if args.performance else None)
    rc, out = check(analyze, rec, golden, lane, args.allow_unknown)
    print(out.rstrip())
    if rc == 0:
        log_info("golden PASS")
    elif rc == 1:
        log_error("golden FAIL -- a frozen fact moved. If the change is intended, "
                  "re-freeze deliberately; the accept command is printed above.")
    else:
        log_error("golden DECLINED -- the gate could not run, which is NOT a pass. "
                  "Usually an incomplete capture; re-capture rather than accepting.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="behavioral goldens over PANDEMONIUM workloads (montauk is the gate)")
    ap.add_argument("--stability", action="store_true",
                    help="N captures of an unchanged build -> per-report flip rate. "
                         "Run this BEFORE choosing a frozen set")
    ap.add_argument("--freeze", action="store_true", help="freeze a golden from one capture")
    ap.add_argument("--label", default="default", help="golden name (freeze/check)")
    ap.add_argument("--goldens", default=str(GOLDEN_DIR), help="golden directory")
    ap.add_argument("--captures", type=int, default=10, help="captures for --stability")
    ap.add_argument("--duration", type=float, default=30.0, help="capture window seconds")
    ap.add_argument("--workload", default=None,
                    help="shell command to run under capture (default: a sustained "
                         "sched-messaging storm)")
    ap.add_argument("--comm", default=MSG_COMM, help="comm pattern montauk traces")
    ap.add_argument("--groups", type=int, default=24, help="sched-messaging groups")
    ap.add_argument("--loops", type=int, default=6000, help="sched-messaging loops")
    ap.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDE),
                    help="report to exclude from the frozen set (repeatable)")
    ap.add_argument("--watch", action="append", default=list(DEFAULT_WATCH),
                    help="gauge pattern for the performance lane (repeatable)")
    ap.add_argument("--tolerance", type=float, default=None, help="performance band pct")
    ap.add_argument("--floor", type=float, default=None, help="performance absolute floor")
    ap.add_argument("--reduce", choices=["last", "mean", "max", "min"], default=None,
                    help="override the gauge reduction for a recording series")
    ap.add_argument("--functional", action="store_true", help="functional lane only")
    ap.add_argument("--performance", action="store_true", help="performance lane only")
    ap.add_argument("--allow-unknown", action="store_true",
                    help="compare despite UNKNOWN capture completeness (archived captures)")
    ap.add_argument("--archive", nargs="?", const=str(LOG_DIR), default=None,
                    metavar="DIR",
                    help="freeze/check release numbers from the .prom benchmark "
                         "archive instead of a live capture (default ~/.cache/pandemonium). "
                         "No root, no eBPF, no capture -- the performance lane over "
                         "gauges alone")
    ap.add_argument("--version", default=None,
                    help="archive mode: which release's .prom to use (default: this tree's)")
    ap.add_argument("--cores", default="12", help="archive mode: which width to freeze")
    ap.add_argument("--outdir", default=None, help="scratch for --stability goldens")
    ap.add_argument("--no-build", action="store_true", help="skip the rebuild check")
    ap.add_argument("--trace", action="store_true",
                    help="Accepted for suite uniformity; this bench always captures")
    ap.add_argument("--iterations", type=int, default=1,
                    help="Accepted for suite uniformity; use --captures for repeats")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="Accepted for suite uniformity; this bench has no EEVDF arm")
    args = ap.parse_args()

    # ARCHIVE MODE NEEDS NO PRIVILEGE AND NO CAPTURE: it reads .prom off disk, so
    # it runs before the root and montauk-attach gates rather than under them.
    if args.archive is not None:
        if args.version is None:
            args.version = get_version()
        if args.label == "default":
            args.label = f"archive-{args.version}-{args.cores}c"
        return mode_archive(args, resolve_analyze())

    if os.geteuid() != 0:
        log_error("must run as root (montauk needs CAP_SYS_ADMIN) -- re-run under sudo")
        return 2
    if not montauk_available():
        log_error("montauk not found -- install montauk")
        return 2
    analyze = resolve_analyze()
    if args.workload is None and subprocess.run(
            ["which", "perf"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode != 0:
        log_error("perf not found -- install perf, or pass --workload")
        return 2
    if not args.no_build and check_sources_changed():
        log_info("sources changed -- rebuilding")
        if not build():
            log_error("build failed")
            return 2

    sched = scx_scheduler_name()
    if not (is_scx_active() and sched == "pandemonium"):
        log_warn(f"active scheduler is '{sched or 'none'}' -- the golden describes THAT, "
                 "not pandemonium")
    online = get_online_cpus()
    if online < 2:
        log_error("need >= 2 online CPUs (one is the montauk drain core)")
        return 2
    drain = online - 1

    if args.stability:
        return mode_stability(args, analyze, drain, sched, online)
    if args.freeze:
        return mode_freeze(args, analyze, drain, sched)
    return mode_check(args, analyze, drain, sched)


if __name__ == "__main__":
    sys.exit(main())
