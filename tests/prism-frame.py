#!/usr/bin/env python3
"""PANDEMONIUM prism-frame: frame pacing under a synthetic game.

framebench.c runs the task graph Igalia measured on Proton games under sched_ext
(LPC 2024 "Using sched_ext to improve frame rates on the SteamDeck", LPC 2025
"Steps Towards a Gaming-Optimized Scheduler"): a main thread fanning jobs out to
a worker pool and joining them by futex, a render thread one frame behind it, a
submit thread and a GPU thread chained by futex_wait, a wineserver-style epoll
hub every thread round-trips to over pipes, a 1 kHz input timer and a SCHED_FIFO
audio period. Work is a fixed instruction count calibrated once on EEVDF before
any arm loads, so every arm executes the identical frames and the frame rate is
the scheduler's result.

Scenarios:
  heavy      uncapped, the pool sized ncpus-2: average FPS and the lows
  contended  heavy plus CPU hogs on half the CPUs and a /bin/true spawned and
             reaped every 20 ms (the child-exit wake every launcher produces)
  paced      capped at 144 Hz: a partially loaded box, the late-frame share

Metrics follow the gaming convention the LPC talk states (average FPS is
throughput, the 1% low is p99 latency). Both 1% low definitions in use are
reported: the p99 frame time as FPS, and the average of the slowest 1% of
frames as FPS. Stutters follow CapFrameX: a frame over 2.5x the moving average
of the 20 before it. Job wake latency is schbench's wakeup latency: release to
first instruction on the worker.

A plain run is one iteration per arm: a quick overview of a change. A verdict
needs --iterations 4 or more, because montauk's permutation test enumerates
every split of the runs and with fewer than 4 per arm its smallest possible p
is above 0.05. Iterations interleave across arms and rotate their order (A B C, B C A, C A B,
...): an A/A run with a fixed order measured two identical arms 3% apart at
p=0.03, because the arm that always ran first always ran faster. Rotation puts
every arm in every position alike. The calibration is repeated at the end and a
drift beyond 5% is reported. Each iteration writes its own .prom, and the
significance is montauk's population lane over those files (permutation p,
Cliff's delta, multiple comparison with the best), embedded in the report.

--trace is a diagnostic pass, not a benchmark: one montauk capture per
(scheduler, scenario) with the decision events and CPU_IDLE stream, for
`montauk --analyze`. The timings it prints are traced and not comparable.
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    table_header, table_row,
    start_and_wait, stop_and_wait, find_scheduler, stop_systemd_scheduler,
    BINARY, LOG_DIR,
    get_git_info, get_version,
    is_scx_active, log, log_error, log_info, log_warn,
    mean_stdev, percentile, montauk_available, montauk_trace, scx_scheduler_name, MONTAUK,
    wait_for_deactivation, PrometheusBuilder, get_online_cpus,
    _chown_to_invoking_user,
)

TESTS = Path(__file__).parent.resolve()
DEFAULT_ITERATIONS = 1
VERDICT_MIN = 4              # smallest per-arm run count that can reach p < 0.05
DEFAULT_DURATION_S = 15.0
DEFAULT_WARMUP_S = 3.0
TRACE_DURATION_S = 8.0
PACED_HZ = 144
LATE_FACTOR = 1.10          # a paced frame later than 110% of its period is late
DRIFT_WARN = 0.05
# The metrics montauk's population lane tests, by direction.
POP_HIGHER = ("fps", "low1", "low1_avg")
POP_LOWER = ("ft_p99", "stutter_pk", "late_pct", "wake_p99", "audio_p99")
ALL_SCX = ["scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
           "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
           "scx_beerland", "scx_layered"]

# (key, label, unit, higher_is_better)
METRICS = [
    ("fps",        "AVG FPS",                 "",   True),
    ("low1",       "1% LOW FPS (p99)",        "",   True),
    ("low1_avg",   "1% LOW FPS (avg)",        "",   True),
    ("low01",      "0.1% LOW FPS (p99.9)",    "",   True),
    ("ft_p50",     "FRAME TIME p50",          "ms", False),
    ("ft_p99",     "FRAME TIME p99",          "ms", False),
    ("ft_max",     "FRAME TIME MAX",          "ms", False),
    ("stutter_pk", "STUTTERS PER 1K FRAMES",  "",   False),
    ("late_pct",   "LATE FRAMES",             "%",  False),
    ("wake_p50",   "JOB WAKE p50",            "us", False),
    ("wake_p99",   "JOB WAKE p99",            "us", False),
    ("wake_p999",  "JOB WAKE p99.9",          "us", False),
    ("audio_p99",  "AUDIO LATE p99",          "us", False),
    ("audio_max",  "AUDIO LATE MAX",          "us", False),
]


def scenarios(ncpus: int) -> dict[str, list[str]]:
    workers = max(2, ncpus - 2)
    base = ["--workers", str(workers), "--jobs", str(2 * workers)]
    return {
        "heavy": base,
        "contended": base + ["--bg-hogs", str(max(1, ncpus // 2)), "--bg-spawn-ms", "20"],
        "paced": base + ["--cap-hz", str(PACED_HZ)],
    }


def build(stamp: str) -> Path | None:
    # Unique per run: a fixed path left root-owned by an earlier sudo run is
    # unwritable later. The basename is the comm montauk traces.
    out = Path(f"/tmp/framebench-{stamp}") / "framebench"
    out.parent.mkdir(parents=True, exist_ok=True)
    cc = subprocess.run(["gcc", "-std=c23", "-O2", "-march=native", "-pthread",
                         "-Wall", "-Wextra", "-o", str(out),
                         str(TESTS / "framebench.c"), str(TESTS / "loadgen_common.c")],
                        capture_output=True, text=True)
    if cc.returncode != 0:
        log_error(f"framebench build failed: {cc.stderr.strip()}")
        return None
    return out


def calibrate(fb: Path) -> float | None:
    r = subprocess.run([str(fb), "--calibrate"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("ipu="):
            return float(line[4:])
    log_error(f"framebench calibration failed: {r.stderr.strip()}")
    return None


def _read(path: Path) -> list[float]:
    try:
        return [float(x) for x in path.read_text().split()]
    except FileNotFoundError:
        return []


def _summary(stdout: str) -> dict[str, float]:
    out = {}
    for line in stdout.splitlines():
        k, _, v = line.partition("=")
        if v:
            out[k] = float(v)
    return out


def run_once(fb: Path, ipu: float, args: list[str], duration: float, warmup: float,
             outdir: Path, tag: str) -> dict | None:
    """One framebench run. Returns the metric dict, or None on failure."""
    ft_p, wk_p, au_p = (outdir / f"{tag}.{s}" for s in ("frames", "wakes", "audio"))
    cmd = [str(fb), "--ipu", f"{ipu:.3f}", "--duration", str(duration),
           "--warmup", str(warmup), *args,
           "--out", str(ft_p), "--wake-out", str(wk_p), "--audio-out", str(au_p)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=duration + warmup + 60)
    if r.returncode != 0:
        log_error(f"    framebench rc={r.returncode}: {r.stderr.strip()}")
        return None
    s = _summary(r.stdout)
    ft = _read(ft_p)
    if len(ft) < duration * 10:
        log_error(f"    only {len(ft)} frames in {duration:.0f}s -- run discarded")
        return None
    wk, au = _read(wk_p), _read(au_p)
    p99, p999 = percentile(ft, 99), percentile(ft, 99.9)
    slow_mean, _ = mean_stdev([x for x in ft if x >= p99])
    m = {
        "fps":        len(ft) / s["measured_s"],
        "low1":       1e9 / p99,
        "low1_avg":   1e9 / slow_mean,
        "low01":      1e9 / p999,
        "ft_p50":     percentile(ft, 50) / 1e6,
        "ft_p99":     p99 / 1e6,
        "ft_max":     percentile(ft, 100) / 1e6,
        "stutter_pk": 1000.0 * s["stutters"] / len(ft),
        "wake_p50":   percentile(wk, 50) / 1e3 if wk else 0.0,
        "wake_p99":   percentile(wk, 99) / 1e3 if wk else 0.0,
        "wake_p999":  percentile(wk, 99.9) / 1e3 if wk else 0.0,
        "audio_p99":  percentile(au, 99) / 1e3 if au else 0.0,
        "audio_max":  percentile(au, 100) / 1e3 if au else 0.0,
        "audio_rt":   s.get("audio_rt", 0.0),
    }
    if "--cap-hz" in args:
        budget = LATE_FACTOR * 1e9 / float(args[args.index("--cap-hz") + 1])
        m["late_pct"] = 100.0 * sum(1 for x in ft if x > budget) / len(ft)
    return m


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


def _cell(vals: list[float], unit: str) -> str:
    if not vals:
        return "-"
    m, sd = mean_stdev(vals)
    prec = 2 if unit in ("ms", "%") or m < 100 else 1
    return f"{m:.{prec}f} ±{sd:.{prec}f}"


def write_report(ver, git, stamp, ncpus, ipu, drift, results, sched_names, scen_names,
                 iterations, skipped, population="") -> Path:
    lines = [
        "PANDEMONIUM PRISM-FRAME",
        f"VERSION:     {ver}",
        f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
        f"TIMESTAMP:   {stamp}",
        f"CPUS:        {ncpus}",
        f"ITERATIONS:  {iterations}",
        f"CALIBRATION: {ipu:.3f} iterations/us"
        + (f"  (end-of-run drift {drift * 100:+.1f}%)" if drift is not None else ""),
        "",
    ]
    for scen in scen_names:
        lines.append(f"SCENARIO {scen.upper()}")
        lines.append(table_header("METRIC", sched_names, 26, 20))
        for key, label, unit, _ in METRICS:
            cols = [_cell([r[key] for r in results[s].get(scen, []) if key in r], unit)
                    for s in sched_names]
            if all(c == "-" for c in cols):
                continue
            lines.append(table_row(f"{label}{' ' + unit if unit else ''}", cols, 26, 20))
        if "EEVDF" in sched_names:
            base = results["EEVDF"].get(scen, [])
            for key, label, unit, higher in METRICS:
                bv = [r[key] for r in base if key in r]
                if not bv:
                    continue
                cols = []
                for s in sched_names:
                    sv = [r[key] for r in results[s].get(scen, []) if key in r]
                    if s == "EEVDF" or not sv:
                        cols.append("")
                        continue
                    bm, sm = mean_stdev(bv)[0], mean_stdev(sv)[0]
                    pct = 100.0 * (sm - bm) / bm if bm else 0.0
                    cols.append(f"{pct:+.1f}%")
                if key in ("fps", "low1", "low1_avg", "ft_p99", "wake_p99", "late_pct"):
                    lines.append(table_row(f"vs EEVDF {label}", cols, 26, 20))
        lines.append("")
    lines.append("vs EEVDF rows are mean deltas; significance is montauk's, below.")
    audio = {r.get("audio_rt") for s in results.values() for rs in s.values() for r in rs}
    if audio and audio != {1.0}:
        lines.append("AUDIO: the audio thread did not get SCHED_FIFO on every run")
    for s, reason in skipped.items():
        lines.append(f"SKIPPED: {s} -- {reason}")
    if population:
        lines += ["", "POPULATION (montauk, per-iteration .prom files)", population.rstrip()]
    text = "\n".join(lines) + "\n"
    path = LOG_DIR / f"prism-frame-{stamp}.log"
    path.write_text(text)
    return path


def write_prometheus(ver, git, stamp, ncpus, ipu, results, skipped) -> Path:
    path = LOG_DIR / f"prism-frame-{stamp}.prom"
    pb = PrometheusBuilder("frame")
    try:
        ts = int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        ts = None
    pb.info(ts=ts, version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False))
    pb.gauge("cpus", ncpus, help="CPUs available")
    pb.gauge("calibration_ipu", f"{ipu:.3f}", help="cpu_quantum iterations per microsecond")
    for sched, reason in skipped.items():
        pb.gauge("scheduler_skipped", 1,
                 help="scheduler crashed/ejected/fell through to EEVDF (not measured)",
                 labels={"scheduler": sched, "reason": reason.replace('"', "'")})
    for sched, by_scen in results.items():
        for scen, runs in by_scen.items():
            for key, _, unit, _ in METRICS:
                vals = [r[key] for r in runs if key in r]
                if not vals:
                    continue
                m, sd = mean_stdev(vals)
                lab = {"scheduler": sched, "scenario": scen}
                pb.gauge(key, f"{m:.6f}", help=f"frame bench {key} {unit}".strip(),
                         labels={**lab, "stat": "mean"})
                pb.gauge(key, f"{sd:.6f}", labels={**lab, "stat": "stdev"})
    path.write_text(pb.render())
    return path


def write_iteration_prom(path: Path, ver, git, it_results: dict) -> None:
    """One file per iteration, one raw value per (scheduler, scenario), split by
    direction so montauk ranks each family the right way up."""
    out = []
    for bench, keys in (("frame_higher", POP_HIGHER), ("frame_lower", POP_LOWER)):
        pb = PrometheusBuilder(bench)
        pb.info(version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False))
        for (sched, scen), m in it_results.items():
            for k in keys:
                if k in m:
                    pb.gauge(k, f"{m[k]:.6f}", labels={"scheduler": sched, "scenario": scen})
        out.append(pb.render())
    path.write_text("".join(out))


def population(proms: list[Path]) -> str:
    """montauk's population comparison over the iteration files, both families."""
    if len(proms) < 2 or not montauk_available():
        return ""
    text = []
    for bench, extra in (("frame_higher", ["--higher-better"]), ("frame_lower", [])):
        r = subprocess.run([MONTAUK, "--analyze", *map(str, proms), "--by", "scheduler",
                            "--pairs", "vs-best", "--metric", f"pandemonium_{bench}_",
                            "--no-emit", *extra],
                           capture_output=True, text=True)
        text.append(r.stdout.strip() or r.stderr.strip())
    return "\n\n".join(t for t in text if t)


def run_trace(fb, ipu, entries, scen, stamp, scx_storm=False) -> int:
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1
    drain = max(0, get_online_cpus() - 1)
    recs = 0
    outdir = LOG_DIR / f"prism-frame-{stamp}" / "trace"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        for sched_name, cmd in entries:
            guard = start_and_wait(cmd, sched_name) if cmd else None
            if cmd and guard is None:
                log_error(f"[{sched_name}] failed to activate -- SKIPPED")
                continue
            safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
            try:
                for name, sargs in scen.items():
                    log_info(f"  [{sched_name}] tracing {name} (montauk on cpu{drain})")
                    with montauk_trace("framebench", f"frame-{safe}-{name}", stamp,
                                       events=True, pin_cpu=drain,
                                       sched_detail=True,
                                       scx_storm=scx_storm) as rec:
                        m = run_once(fb, ipu, sargs, TRACE_DURATION_S, 1.0,
                                     outdir, f"{safe}-{name}")
                    recs += 1
                    fps = f"{m['fps']:.0f} fps (traced)" if m else "failed"
                    log_info(f"    {name}: {fps} -> {rec.dir}")
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
    ap = argparse.ArgumentParser(description="PANDEMONIUM prism-frame: frame pacing "
                                             "under a synthetic game")
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                    help=f"interleaved iterations per arm (default {DEFAULT_ITERATIONS})")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                    help=f"measured seconds per run (default {DEFAULT_DURATION_S:.0f})")
    ap.add_argument("--warmup", type=float, default=DEFAULT_WARMUP_S,
                    help=f"discarded seconds before measuring (default {DEFAULT_WARMUP_S:.0f})")
    ap.add_argument("--scenarios", type=str, default="",
                    help="comma-separated subset of heavy,contended,paced (default all)")
    ap.add_argument("--schedulers", type=str, default="",
                    help="comma-separated external scx schedulers; EEVDF vs exactly "
                         "these (PANDEMONIUM only if named)")
    ap.add_argument("--all-scx", action="store_true",
                    help="the full installed scx field; overrides --schedulers")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="skip EEVDF and external schedulers")
    ap.add_argument("--no-eevdf", action="store_true", help="skip the EEVDF baseline")
    ap.add_argument("--trace", action="store_true",
                    help="diagnostic pass: one montauk capture per (scheduler, "
                         "scenario); timings are traced and not comparable")
    ap.add_argument("--scx-storm", action="store_true",
                    help="with --trace: arm montauk's sched_ext kick probes, so the "
                         "kick-latency and storm reports have data")
    ap.add_argument("--cores", type=str, default=None,
                    help="accepted for suite uniformity; runs at native width")
    args = ap.parse_args()

    # Root for the SCHED_FIFO audio thread, sched_ext activation and montauk.
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

    ncpus = get_online_cpus()
    ver, git = get_version(), get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    all_scen = scenarios(ncpus)
    want = [s.strip() for s in args.scenarios.split(",") if s.strip()] or list(all_scen)
    unknown = [s for s in want if s not in all_scen]
    if unknown:
        log_error(f"unknown scenario(s): {', '.join(unknown)} (heavy, contended, paced)")
        return 2
    scen = {k: all_scen[k] for k in want}

    entries: list[tuple[str, list[str] | None]] = []
    if not args.pandemonium_only and not args.no_eevdf:
        entries.append(("EEVDF", None))
    named = {s.strip().lower() for s in args.schedulers.split(",") if s.strip()}
    field_only = bool(named) and not args.all_scx and not args.pandemonium_only
    if not field_only or named & {"pandemonium", "scx_pandemonium"}:
        entries.append(("PANDEMONIUM (BPF)", [str(BINARY), "--verbose", "--no-adaptive"]))
        entries.append(("PANDEMONIUM (ADAPTIVE)", [str(BINARY), "--verbose"]))
    if not args.pandemonium_only:
        for ext in (ALL_SCX if args.all_scx else sorted(named)):
            if ext in ("pandemonium", "scx_pandemonium", "eevdf"):
                continue
            if find_scheduler(ext):
                entries.append((ext, [ext]))
            else:
                log_warn(f"  external scheduler {ext} not found in PATH, skipping")

    log_info("PANDEMONIUM's frame Test is based off and adapted from work done by "
             "Devs at Igalia for scx_lavd.")
    if not args.trace and args.iterations < VERDICT_MIN:
        log_warn(f"fewer than {VERDICT_MIN} iterations is an overview, not a verdict; "
                 f"montauk's permutation test cannot reach p < 0.05 below "
                 f"{VERDICT_MIN} runs per arm")
    if not log.child:
        dirty = " (dirty)" if git["dirty"] else ""
        log_info(f"prism-frame v{ver} [{git['commit']}{dirty}]")
        log_info(f"CPUs: {ncpus}  iterations: {args.iterations}  "
                 f"run: {args.warmup:.0f}s warmup + {args.duration:.0f}s")
        log_info(f"scenarios: {', '.join(scen)}")
        log_info(f"schedulers: {', '.join(n for n, _ in entries)}")

    if is_scx_active():
        log_warn(f"sched_ext is active ({scx_scheduler_name()}) -- stopping pandemonium service")
        stop_systemd_scheduler()
        if not wait_for_deactivation(5.0):
            log_error("could not deactivate sched_ext")
            return 1
    time.sleep(1)

    fb = build(stamp)
    if fb is None:
        return 1
    ipu = calibrate(fb)
    if ipu is None:
        return 1
    log_info(f"calibration on EEVDF: {ipu:.3f} iterations/us")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if args.trace:
        return run_trace(fb, ipu, entries, scen, stamp, args.scx_storm)

    rawdir = LOG_DIR / f"prism-frame-{stamp}"
    results: dict[str, dict[str, list[dict]]] = {n: {s: [] for s in scen} for n, _ in entries}
    skipped: dict[str, str] = {}
    iter_proms: list[Path] = []
    drift = None
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
                safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
                outdir = rawdir / safe
                outdir.mkdir(parents=True, exist_ok=True)
                try:
                    for name, sargs in scen.items():
                        m = run_once(fb, ipu, sargs, args.duration, args.warmup,
                                     outdir, f"{name}-{it + 1}")
                        reason = crashed(guard, expected)
                        if reason:
                            skipped[sched_name] = f"{reason} (during {name})"
                            log_error(f"[{sched_name}] CRASHED during {name} -- {reason}")
                            break
                        if m is None:
                            continue
                        results[sched_name][name].append(m)
                        it_results[(sched_name, name)] = m
                        extra = f"  late {m['late_pct']:.2f}%" if "late_pct" in m else ""
                        log_info(f"    {name:<10} {m['fps']:7.1f} fps  "
                                 f"1% low {m['low1']:6.1f}  p99 {m['ft_p99']:.2f}ms  "
                                 f"wake p99 {m['wake_p99']:.0f}us{extra}")
                finally:
                    if guard is not None:
                        stop_and_wait(guard)
                time.sleep(2)
            if it_results:
                prom = rawdir / f"iter-{it + 1}.prom"
                write_iteration_prom(prom, ver, git, it_results)
                iter_proms.append(prom)
        end_ipu = calibrate(fb)
        if end_ipu:
            drift = end_ipu / ipu - 1.0
            if abs(drift) > DRIFT_WARN:
                log_warn(f"calibration drifted {drift * 100:+.1f}% over the run "
                         "(thermal or frequency); compare arms with care")
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    # A crash discards only the run it happened in; earlier iterations measured
    # the scheduler as itself and stand.
    names = [n for n, _ in entries if any(results[n].values())]
    if not names:
        log_error("no scheduler produced a result")
        return 1
    report = write_report(ver, git, stamp, ncpus, ipu, drift, results, names,
                          list(scen), args.iterations, skipped, population(iter_proms))
    prom = write_prometheus(ver, git, stamp, ncpus, ipu,
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
