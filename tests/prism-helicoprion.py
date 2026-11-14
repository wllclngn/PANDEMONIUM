#!/usr/bin/env python3
"""PANDEMONIUM prism-helicoprion: helicoprion under each arm of the field.

helicoprion is Piotr Gorski's scheduler-focused CPU benchmark
(https://github.com/Seafoam-Labs/helicoprion, MIT or Apache-2.0): Thread pairs
pass a token through a private futex, on an idle box and with every CPU held by
a hog, and each round trip is filed by whether its receiver really slept. prism
runs the installed package's program and carries none of it.

Devario packages it. Where the program is missing, or the installed package is
older than the one this box can get, prism offers to install it and never does
so unasked: Where a configured repository carries the package, through Shelly,
Devario's package manager, or through pacman where Shelly is not installed;
and otherwise by building Devario's own PKGBUILD with makepkg, so pacman
installs the real package. The versions compared are the package's, as pacman
records them, never the program's own: The 0.2.1 package's program calls itself
0.2.0. A run with no terminal to ask on installs nothing: It refuses a missing
program and runs an older one as it stands.

helicoprion's own matrix switches schedulers through scxcli and scx.service.
prism's arms are binaries it loads itself, so here the switching is prism's and
the measuring is helicoprion's: Each arm is loaded, one `helicoprion run`
session runs under it, one measured run a workload after helicoprion's own
warm-up, and the result file helicoprion writes is read back for every metric
it names, with its unit and direction. A run helicoprion marks invalid is no
sample.

Iterations interleave across arms and rotate their order, as prism-frame's do,
and each writes its own .prom for montauk's population lane, which gives the
significance. Single-sample extremes, informational metrics and a population
under 1% of a run's round trips are listed and never scored, as helicoprion
itself never scores them. Its result files stay in the samples directory,
where `helicoprion compare` reads them.

--trace is a diagnostic pass: One montauk capture per arm around one session.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    table_header, table_row,
    start_and_wait, stop_and_wait, clear_loaded_scheduler,
    LOG_DIR, add_field_flags, field_of,
    get_git_info, get_version,
    is_scx_active, log, log_error, log_info, log_warn,
    mean_stdev, montauk_available, montauk_trace, scx_scheduler_name, MONTAUK,
    wait_for_deactivation, PrometheusBuilder, get_online_cpus,
    _chown_to_invoking_user,
)

SOURCE = "https://github.com/Seafoam-Labs/helicoprion"
PACKAGE = "helicoprion"
AUTHOR = "Piotr Gorski"
# DEVARIO'S RECIPE FOR THE PACKAGE, READ WHERE IT LIVES AND NEVER COPIED HERE.
RECIPE = ("https://raw.githubusercontent.com/Seafoam-Labs/devario-custom-packagbuilds/"
          "HEAD/devario-development/helicoprion")
DEFAULT_ITERATIONS = 1
VERDICT_MIN = 4              # smallest per-arm run count that can reach p < 0.05
DEFAULT_DURATION_S = 5.0     # helicoprion's own default
DEFAULT_WARMUP_RUNS = 1      # helicoprion's own default
COOLDOWN_S = 1.0             # helicoprion's own default, between its runs
MINOR_SHARE = 0.01           # helicoprion's --min-share default
ARROW = {"lower": "↓", "higher": "↑", "neither": "info"}


def _capture(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return r.returncode, r.stdout


def package_source(which=shutil.which, capture=_capture) -> str:
    """What installs the package on this box. Where a configured repository
    carries it, Shelly if that is installed and pacman otherwise; where none
    does, makepkg on Devario's recipe; without pacman, nothing."""
    if not which("pacman"):
        return ""
    if capture(["pacman", "-Si", PACKAGE])[0] == 0:
        return "shelly" if which("shelly") else "pacman"
    return "makepkg" if which("makepkg") else ""


def _fetch(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def installed_version(capture=_capture) -> str | None:
    """The installed package's version as pacman records it, or None where the
    program on PATH is not that package's."""
    rc, out = capture(["pacman", "-Q", PACKAGE])
    parts = out.split()
    return parts[1] if rc == 0 and len(parts) == 2 else None


def packaged_version(source: str, capture=_capture, fetch=_fetch) -> str | None:
    """The version SOURCE would install: the sync database's where a repository
    carries the package, else the one Devario's recipe names. None when it
    cannot be read."""
    if source != "makepkg":
        for line in capture(["pacman", "-Si", PACKAGE])[1].splitlines():
            key, _, val = line.partition(":")
            if key.strip() == "Version":
                return val.strip() or None
        return None
    info = dict(line.strip().split(" = ", 1)
                for line in (fetch(f"{RECIPE}/.SRCINFO") or "").splitlines()
                if " = " in line)
    if "pkgver" not in info:
        return None
    ver = f"{info['pkgver']}-{info.get('pkgrel', '1')}"
    return f"{info['epoch']}:{ver}" if info.get("epoch") else ver


def behind(have: str, latest: str, capture=_capture) -> bool:
    """Whether HAVE is an older package version than LATEST, in pacman's own
    ordering. An answer vercmp does not give is no."""
    rc, out = capture(["vercmp", have, latest])
    return rc == 0 and out.strip() == "-1"


def _as_user(cmd: list[str]) -> list[str]:
    """makepkg refuses root: Under sudo, run it as the user who invoked it."""
    user = os.environ.get("SUDO_USER")
    return ["sudo", "-u", user, *cmd] if os.geteuid() == 0 and user else cmd


def install_package(source: str) -> bool:
    """Install or update the package the way SOURCE does it. The package
    manager asks its own questions; nothing is passed to answer them."""
    if source == "shelly":
        return subprocess.run(["shelly", "-Is", PACKAGE]).returncode == 0
    if source == "pacman":
        return subprocess.run(["sudo", "pacman", "-S", PACKAGE]).returncode == 0
    if source != "makepkg":
        return False
    recipe = _fetch(f"{RECIPE}/PKGBUILD")
    if recipe is None:
        log_error(f"Devario's PKGBUILD could not be read from {RECIPE}")
        return False
    # A FRESH DIRECTORY THE INVOKING USER OWNS, GONE WHEN THE BUILD ENDS.
    with tempfile.TemporaryDirectory(prefix="prism-helicoprion-") as td:
        (Path(td) / "PKGBUILD").write_text(recipe)
        user = os.environ.get("SUDO_USER")
        if os.geteuid() == 0 and user:
            shutil.chown(td, user)
            shutil.chown(Path(td) / "PKGBUILD", user)
        return subprocess.run(_as_user(["makepkg", "-si"]), cwd=td).returncode == 0


def _prompt_yn(question: str) -> bool:
    ts = datetime.now().strftime("[%H:%M:%S]")
    try:
        return input(f"{ts} [INFO]   {question} [Y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


HOW = {"shelly": "Shelly, Devario's package manager",
       "pacman": "pacman, from a configured repository",
       "makepkg": "makepkg, from Devario's PKGBUILD"}


def ensure_helicoprion(asked: str, interactive: bool, *, which=shutil.which,
                       source_of=package_source, latest_of=packaged_version,
                       installed_of=installed_version, older=behind,
                       install=install_package, ask=_prompt_yn) -> str | None:
    """The program to run, or None when there is none and none was installed.
    A path the user names is run as it stands. The default, the package's
    program, is offered from Devario's packages when it is missing or its
    package is behind the one this box can get; nothing installs without a
    yes."""
    if asked != PACKAGE:
        prog = which(asked)
        if prog is None:
            log_error(f"{asked!r} is not a program")
        return prog
    prog, source = which(PACKAGE), source_of()
    if prog is None:
        log_warn(f"helicoprion is not installed. It is {AUTHOR}'s scheduler "
                 f"benchmark, packaged by Devario: {SOURCE}")
        if not source:
            log_error("no package source for it on this box: Shelly, a pacman "
                      "repository that carries it, or pacman with makepkg")
            return None
        if not interactive:
            log_error(f"no terminal to ask on -- install the {PACKAGE} package "
                      f"({HOW[source]}) and run again")
            return None
        if not ask(f"Install helicoprion now with {HOW[source]}"):
            log_error("helicoprion is this workload -- nothing to run without it")
            return None
        if not install(source) or which(PACKAGE) is None:
            log_error("helicoprion was not installed")
            return None
        return which(PACKAGE)
    have, latest = installed_of(), latest_of(source) if source else None
    if have and latest and older(have, latest):
        if not interactive:
            log_warn(f"helicoprion {have} is installed and the package is {latest}; "
                     "running the installed one")
        elif ask(f"helicoprion {have} is installed and the package is {latest}. "
                 f"Update it now with {HOW[source]}"):
            if not install(source):
                log_warn(f"the update failed; running helicoprion {have}")
            prog = which(PACKAGE) or prog
    return prog


def tool_version(prog: str) -> str:
    r = subprocess.run([prog, "--version"], capture_output=True, text=True)
    return r.stdout.strip().removeprefix("helicoprion").strip() or "unknown"


def session_cmd(prog: str, suite: str, warmup: int, duration: float, label: str,
                out: Path) -> list[str]:
    """One helicoprion session: one measured run a workload after its warm-up."""
    return [prog, "run", "--suite", suite, "--runs", "1", "--warmup", str(warmup),
            "--duration", str(duration), "--cooldown", str(COOLDOWN_S),
            "--label", label, "--output", str(out)]


def session_metrics(result: dict) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """A helicoprion result file as {workload: {metric: {value, unit, better,
    extreme, minor}}} over its valid run, and why each invalid run was dropped."""
    out: dict[str, dict[str, dict]] = {}
    dropped: list[str] = []
    for w in result.get("workloads", []):
        for run in w.get("runs", []):
            if not run.get("valid"):
                dropped.append(f"{w['name']}: {run.get('invalid_reason') or 'invalid'}")
                continue
            out[w["name"]] = {
                name: {"value": float(m["value"]), "unit": m.get("unit", ""),
                       "better": m.get("better", "neither"),
                       "extreme": bool(m.get("extreme", False)),
                       "minor": m.get("share") is not None and m["share"] < MINOR_SHARE}
                for name, m in run.get("metrics", {}).items()}
    return out, dropped


def run_session(prog: str, suite: str, warmup: int, duration: float, label: str,
                out: Path, nworkloads: int = 2) -> dict[str, dict[str, dict]] | None:
    """Run one session and return its metrics, or None when helicoprion failed
    or wrote nothing readable. Its own progress lines are kept beside the file."""
    budget = nworkloads * (warmup + 1) * (duration + COOLDOWN_S)
    try:
        r = subprocess.run(session_cmd(prog, suite, warmup, duration, label, out),
                           capture_output=True, text=True, timeout=2 * budget + 120)
    except subprocess.TimeoutExpired:
        log_error("    helicoprion did not finish -- session discarded")
        return None
    out.with_suffix(".log").write_text(r.stderr)
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-1:] or ["no output"]
        log_error(f"    helicoprion rc={r.returncode}: {tail[0]}")
        return None
    try:
        metrics, dropped = session_metrics(json.loads(out.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as e:
        log_error(f"    helicoprion's result file is unreadable: {e}")
        return None
    for line in r.stderr.splitlines():
        if " run 1/1" in line:
            log_info(f"    {line.strip()}")
    for why in dropped:
        log_warn(f"    run dropped -- {why}")
    return metrics or None


def crashed(guard, expected_ops: str) -> str | None:
    if guard is None:
        return None
    if guard.proc.poll() is not None:
        return f"process exited (rc={guard.proc.returncode})"
    name = scx_scheduler_name()
    if not name:
        return "fell through to EEVDF"
    if expected_ops and name != expected_ops:
        return f"fell through to '{name}' (expected '{expected_ops}')"
    return None


def prom_name(metric: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", metric)


def scored(m: dict) -> bool:
    return m["better"] in ("lower", "higher") and not m["extreme"] and not m["minor"]


def _rows(results: dict, sched_names: list[str], workload: str) -> list[str]:
    """The workload's metrics in helicoprion's order, each seen on any arm."""
    seen: dict[str, None] = {}
    for s in sched_names:
        for run in results[s].get(workload, []):
            seen.update(dict.fromkeys(run))
    return list(seen)


def _cell(runs: list[dict], metric: str) -> str:
    ms = [r[metric] for r in runs if metric in r]
    if not ms:
        return "-"
    m, sd = mean_stdev([x["value"] for x in ms])
    prec = 2 if abs(m) < 100 else 1 if abs(m) < 100000 else 0
    return f"{m:.{prec}f} ±{sd:.{prec}f}" + ("°" if any(x["minor"] for x in ms) else "")


def _label(results: dict, sched_names: list[str], workload: str, metric: str) -> str:
    for s in sched_names:
        for run in results[s].get(workload, []):
            if metric in run:
                m = run[metric]
                tag = f"{m['unit']} {ARROW.get(m['better'], 'info')}".strip()
                return f"{metric} [{tag}{' single sample' if m['extreme'] else ''}]"
    return metric


def write_report(ver, git, stamp, ncpus, tool, suite, duration, warmup, results,
                 sched_names, iterations, skipped, rawdir, population="") -> Path:
    workloads: dict[str, None] = {}
    for s in sched_names:
        workloads.update(dict.fromkeys(results[s]))
    lines = [
        "PANDEMONIUM PRISM-HELICOPRION",
        f"VERSION:     {ver}",
        f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
        f"TIMESTAMP:   {stamp}",
        f"CPUS:        {ncpus}",
        f"ITERATIONS:  {iterations}",
        f"HELICOPRION: {tool}, suite {suite}, {duration:g}s runs after {warmup} warm-up",
        f"             by {AUTHOR}, {SOURCE}",
        "",
    ]
    minor = False
    for w in workloads:
        lines.append(f"WORKLOAD {w}")
        lines.append(table_header("METRIC", sched_names, 46, 22))
        for metric in _rows(results, sched_names, w):
            cols = [_cell(results[s].get(w, []), metric) for s in sched_names]
            minor = minor or any(c.endswith("°") for c in cols)
            lines.append(table_row(_label(results, sched_names, w, metric), cols, 46, 22))
        if "EEVDF" in sched_names:
            base = results["EEVDF"].get(w, [])
            for metric in _rows(results, sched_names, w):
                bv = [r[metric] for r in base if metric in r]
                if not bv or not all(scored(x) for x in bv):
                    continue
                bm = mean_stdev([x["value"] for x in bv])[0]
                cols = []
                for s in sched_names:
                    sv = [r[metric] for r in results[s].get(w, []) if metric in r]
                    if s == "EEVDF" or not sv or not bm or not all(scored(x) for x in sv):
                        cols.append("")
                        continue
                    sm = mean_stdev([x["value"] for x in sv])[0]
                    cols.append(f"{100.0 * (sm - bm) / bm:+.1f}%")
                lines.append(table_row(f"vs EEVDF {metric}", cols, 46, 22))
        lines.append("")
    lines.append("vs EEVDF rows are mean deltas of the scored metrics; significance is "
                 "montauk's, below.")
    if minor:
        lines.append("° a population under 1% of the run's round trips: listed, never scored.")
    for s, reason in skipped.items():
        lines.append(f"SKIPPED: {s} -- {reason}")
    lines.append(f"helicoprion's own result files are in {rawdir}; "
                 "`helicoprion compare` reads them.")
    if population:
        lines += ["", "POPULATION (montauk, per-iteration .prom files)", population.rstrip()]
    path = LOG_DIR / f"prism-helicoprion-{stamp}.log"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_prometheus(ver, git, stamp, ncpus, tool, suite, results, skipped) -> Path:
    path = LOG_DIR / f"prism-helicoprion-{stamp}.prom"
    pb = PrometheusBuilder("helicoprion")
    try:
        ts = int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        ts = None
    pb.info(ts=ts, version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False),
            helicoprion_version=tool, suite=suite)
    pb.gauge("cpus", ncpus, help="CPUs available")
    for sched, reason in skipped.items():
        pb.gauge("scheduler_skipped", 1,
                 help="scheduler crashed/ejected/fell through to EEVDF (not measured)",
                 labels={"scheduler": sched, "reason": reason.replace('"', "'")})
    for sched, by_w in results.items():
        for w, runs in by_w.items():
            for metric in dict.fromkeys(k for r in runs for k in r):
                ms = [r[metric] for r in runs if metric in r]
                m, sd = mean_stdev([x["value"] for x in ms])
                lab = {"scheduler": sched, "workload": w}
                pb.gauge(prom_name(metric), f"{m:.6f}",
                         help=f"helicoprion {metric} [{ms[0]['unit']}], "
                              f"better {ms[0]['better']}",
                         labels={**lab, "stat": "mean"})
                pb.gauge(prom_name(metric), f"{sd:.6f}", labels={**lab, "stat": "stdev"})
    path.write_text(pb.render())
    return path


def write_iteration_prom(path: Path, ver, git, it_results: dict) -> None:
    """One file per iteration, one raw value per (scheduler, workload), split by
    direction so montauk ranks each family the right way up. Only what
    helicoprion scores is written."""
    out = []
    for bench, better in (("helicoprion_higher", "higher"), ("helicoprion_lower", "lower")):
        pb = PrometheusBuilder(bench)
        pb.info(version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False))
        for (sched, w), run in it_results.items():
            for metric, m in run.items():
                if m["better"] == better and scored(m):
                    pb.gauge(prom_name(metric), f"{m['value']:.6f}",
                             labels={"scheduler": sched, "workload": w})
        out.append(pb.render())
    path.write_text("".join(out))


def population(proms: list[Path]) -> str:
    """montauk's population comparison over the iteration files, both families."""
    if len(proms) < 2 or not montauk_available():
        return ""
    text = []
    for bench, extra in (("helicoprion_higher", ["--higher-better"]), ("helicoprion_lower", [])):
        r = subprocess.run([MONTAUK, "--analyze", *map(str, proms), "--by", "scheduler",
                            "--pairs", "vs-best", "--metric", f"pandemonium_{bench}_",
                            "--no-emit", *extra],
                           capture_output=True, text=True)
        text.append(r.stdout.strip() or r.stderr.strip())
    return "\n\n".join(t for t in text if t)


def _safe(sched_name: str) -> str:
    return sched_name.replace(" ", "-").replace("(", "").replace(")", "")


def run_trace(prog, entries, suite, warmup, duration, stamp) -> int:
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    drain = max(0, get_online_cpus() - 1)
    outdir = LOG_DIR / f"prism-helicoprion-{stamp}" / "trace"
    outdir.mkdir(parents=True, exist_ok=True)
    recs = 0
    try:
        for sched_name, cmd in entries:
            guard = start_and_wait(cmd, sched_name) if cmd else None
            if cmd and guard is None:
                log_error(f"[{sched_name}] failed to activate -- SKIPPED")
                continue
            try:
                log_info(f"  [{sched_name}] tracing one session (montauk on cpu{drain})")
                with montauk_trace("helicoprion", f"helicoprion-{_safe(sched_name)}", stamp,
                                   events=True, pin_cpu=drain, sched_detail=True,
                                   scx_dsq=True, syscalls="futex") as rec:
                    run_session(prog, suite, warmup, duration, sched_name,
                                outdir / f"{_safe(sched_name)}.json")
                recs += 1
                log_info(f"    traced, timings not comparable -> {rec.dir}")
            finally:
                if guard is not None:
                    stop_and_wait(guard)
            print()
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)
        _chown_to_invoking_user(outdir.parent)
    log_info(f"TRACE COMPLETE: {recs} recording(s) under /tmp/pandemonium")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PANDEMONIUM prism-helicoprion: "
                                             f"{AUTHOR}'s helicoprion under each arm")
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                    help=f"interleaved iterations per arm (default {DEFAULT_ITERATIONS})")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                    help=f"helicoprion's seconds per run (default {DEFAULT_DURATION_S:g})")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_RUNS,
                    help="helicoprion's unrecorded warm-up runs per workload "
                         f"(default {DEFAULT_WARMUP_RUNS})")
    ap.add_argument("--suite", type=str, default="quick",
                    help="helicoprion's workload suite (default quick)")
    ap.add_argument("--helicoprion", type=str, default=PACKAGE, metavar="PATH",
                    help="a helicoprion program to run as it stands (default: the "
                         "installed package's, offered from Devario's packages "
                         "when it is missing or behind)")
    add_field_flags(ap)
    ap.add_argument("--trace", action="store_true",
                    help="diagnostic pass: one montauk capture per arm around one "
                         "session; timings are traced and not comparable")
    ap.add_argument("--cores", type=str, default=None,
                    help="accepted for suite uniformity; runs at native width")
    args = ap.parse_args()
    if args.iterations < 1 or args.warmup < 0 or args.duration <= 0:
        log_error("--iterations must be at least 1, --warmup 0 or more and "
                  "--duration above 0")
        return 2

    # SETTLED BEFORE ELEVATION: makepkg REFUSES ROOT, AND A BOX WITHOUT THE
    # PROGRAM IS ASKED BEFORE IT IS ASKED FOR A PASSWORD.
    prog = ensure_helicoprion(args.helicoprion, sys.stdin.isatty())
    if prog is None:
        return 2
    prog = str(Path(prog).resolve())

    # Root for sched_ext activation and montauk.
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

    ncpus = get_online_cpus()
    ver, git = get_version(), get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tool = tool_version(prog)
    package = installed_version() if args.helicoprion == PACKAGE else None
    if package:
        tool = f"{tool} (package {package})"

    entries = field_of(args)
    if not entries:
        log_error("no scheduler arm to run: nothing named by --only/--schedulers "
                  "is on PATH")
        return 2

    log_info(f"helicoprion {tool} is {AUTHOR}'s benchmark ({SOURCE}); prism runs "
             "the installed package's program and carries none of it.")
    if not args.trace and args.iterations < VERDICT_MIN:
        log_warn(f"fewer than {VERDICT_MIN} iterations is an overview, not a verdict; "
                 f"montauk's permutation test cannot reach p < 0.05 below "
                 f"{VERDICT_MIN} runs per arm")
    if not log.child:
        dirty = " (dirty)" if git["dirty"] else ""
        log_info(f"prism-helicoprion v{ver} [{git['commit']}{dirty}]")
        log_info(f"CPUs: {ncpus}  iterations: {args.iterations}  suite: {args.suite}  "
                 f"run: {args.warmup} warm-up + {args.duration:g}s")
        log_info(f"schedulers: {', '.join(n for n, _ in entries)}")

    if not clear_loaded_scheduler():
        return 1
    time.sleep(1)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if args.trace:
        return run_trace(prog, entries, args.suite, args.warmup, args.duration, stamp)

    rawdir = LOG_DIR / f"prism-helicoprion-{stamp}"
    results: dict[str, dict[str, list[dict]]] = {n: {} for n, _ in entries}
    skipped: dict[str, str] = {}
    iter_proms: list[Path] = []
    rawdir.mkdir(parents=True, exist_ok=True)
    try:
        for it in range(args.iterations):
            it_results: dict[tuple[str, str], dict] = {}
            k = it % len(entries)
            for sched_name, cmd in entries[k:] + entries[:k]:
                if sched_name in skipped:
                    continue
                log_info(f"[{sched_name}] iteration {it + 1}/{args.iterations}")
                guard, expected = None, ""
                if cmd is not None:
                    guard = start_and_wait(cmd, sched_name)
                    if guard is None:
                        skipped[sched_name] = "failed to activate"
                        log_error(f"[{sched_name}] failed to activate -- SKIPPED")
                        continue
                    expected = scx_scheduler_name()
                    if not expected:
                        skipped[sched_name] = "fell through to EEVDF at activation"
                        stop_and_wait(guard)
                        continue
                try:
                    session = run_session(prog, args.suite, args.warmup, args.duration,
                                          sched_name,
                                          rawdir / f"{_safe(sched_name)}-{it + 1}.json")
                    reason = crashed(guard, expected)
                    if reason:
                        skipped[sched_name] = f"{reason} (iteration {it + 1})"
                        log_error(f"[{sched_name}] CRASHED -- {reason}")
                        session = None
                    for w, run in (session or {}).items():
                        results[sched_name].setdefault(w, []).append(run)
                        it_results[(sched_name, w)] = run
                finally:
                    if guard is not None:
                        stop_and_wait(guard)
                time.sleep(2)
            if it_results:
                prom = rawdir / f"iter-{it + 1}.prom"
                write_iteration_prom(prom, ver, git, it_results)
                iter_proms.append(prom)
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    # A crash discards only the session it happened in; earlier iterations
    # measured the scheduler as itself and stand.
    names = [n for n, _ in entries if results[n]]
    if not names:
        log_error("no scheduler produced a result")
        return 1
    report = write_report(ver, git, stamp, ncpus, tool, args.suite, args.duration,
                          args.warmup, results, names, args.iterations, skipped,
                          rawdir, population(iter_proms))
    prom = write_prometheus(ver, git, stamp, ncpus, tool, args.suite,
                            {n: results[n] for n in names}, skipped)
    _chown_to_invoking_user(report, prom, rawdir)
    print()
    log.report(report.read_text())
    log_info(f"REPORT: {report}")
    log_info(f"METRICS: {prom}")
    log_info(f"SAMPLES: {rawdir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
