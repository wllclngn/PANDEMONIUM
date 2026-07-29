#!/usr/bin/env python3
"""baseline_gate -- the standing regression gate against the best-measured release.

A scheduler change is an improvement only when it beats the best release the
benchmark archive has ever recorded, on the cell it targets. Measuring a change
against its immediately-prior state instead lets a slow decline pass review one
defensible step at a time, because each step really does beat the step before it.
This gate makes the comparison mechanical: it reads the .prom archive and
compares a candidate against the baseline release on the load-bearing IPC cells,
running no benchmark of its own.

THE DRIFT CORRECTION, which is the whole reason this is not a plain ratio.
EEVDF appears in the same cells as PANDEMONIUM and its code never changes
between PANDEMONIUM releases, so any cross-era shift in an EEVDF cell is pure
measurement environment -- kernel condition, box thermals, background load. A
raw candidate/baseline ratio silently hands the older era a head start whenever
the box has slowed since. So every candidate ratio is divided by the matching
cell's EEVDF drift before judgment, and a cell with no EEVDF twin falls back to
the geometric mean of the drifts that do exist. The corrected ratio is the
verdict; the raw ratio and the drift are reported beside it so the correction is
never invisible.

Two modes:
  status (default): compare a version already in the archive against the baseline.
      python3 baseline_gate.py [--candidate 5.17.0]
  gate a fresh candidate: fold a directory of just-benched .prom into the
      analysis as its own group, isolated from the archive's stale same-version
      pool.
      python3 baseline_gate.py --candidate-dir /path/to/fresh/runs

Self-test (no archive, no montauk, no hardware -- pure arithmetic over a
synthetic fixture, gated byte-exact against a golden):
      python3 baseline_gate.py --self-test
      python3 baseline_gate.py --self-test --update   # refreeze, shows the diff

montauk is the only analyzer. This shells montauk_analyze (its fixed --by
version comparator + --alias to bridge the bench_->scale_ family rename) and
reads the structured montauk_pop_mean gauges it emits.
"""
import argparse
import difflib
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASELINE = "5.14.0"
DEFAULT_ARCHIVE = Path.home() / ".cache" / "pandemonium"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "baseline_gate.pop.prom"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "baseline_gate.golden"
FIXTURE_CANDIDATE = "5.17.0"   # the candidate group the fixture carries

# Load-bearing IPC families (lower is better). bench_ (pre-5.15) and scale_
# (post-5.15) are aliased to one neutral name so the comparison spans the
# family rename. Each is the metric the corresponding protection governs.
IPC_METRICS = {
    "ipc_p99_us": ("pandemonium_bench_ipc_rtt_p99_us",
                   "pandemonium_scale_ipc_rtt_p99_us"),
    "ipc_p50_us": ("pandemonium_bench_ipc_rtt_p50_us",
                   "pandemonium_scale_ipc_rtt_p50_us"),
    "ipc_worst_us": ("pandemonium_bench_ipc_rtt_worst_us",
                     "pandemonium_scale_ipc_rtt_worst_us"),
}
# A cell must clear both to count as a real regression: a ratio past this AND an
# absolute gap past the floor, so noise on tiny values never trips the gate.
DEFAULT_TOL = 0.15       # candidate may sit up to 15% above baseline before failing
ABS_FLOOR_US = 25.0      # ignore sub-25us absolute deltas (measurement noise)


def resolve_analyzer() -> str:
    env = os.environ.get("MONTAUK_ANALYZE")
    if env and Path(env).exists():
        return env
    found = shutil.which("montauk_analyze")
    if found:
        return found
    sys.exit("baseline_gate: montauk_analyze not found (set MONTAUK_ANALYZE or "
             "install montauk)")


def parse_pop_mean(prom_text: str) -> dict:
    """{(metric, cell): {group: mean}} from the emitted montauk_pop_mean gauges."""
    out: dict = {}
    for line in prom_text.splitlines():
        if not line.startswith("montauk_pop_mean{"):
            continue
        try:
            labels = line[line.index("{") + 1: line.index("}")]
            value = float(line[line.index("}") + 1:].strip())
        except (ValueError, IndexError):
            continue
        fields = {}
        for part in labels.split('",'):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip().strip('"')
        m, cell, grp = fields.get("metric"), fields.get("cell"), fields.get("group")
        if m and cell and grp:
            out.setdefault((m, cell), {})[grp] = value
    return out


def pair_groups(pop: dict, peak_key: str, cand_key: str, metric=None) -> dict:
    """{(metric, cell): {'peak', 'cand'}} for every cell present in BOTH groups.

    metric overrides the parsed name when the caller aliased a family to a
    neutral one; otherwise the parsed metric is kept.
    """
    out = {}
    for (m, cell), groups in pop.items():
        if peak_key in groups and cand_key in groups:
            out[(metric or m, cell)] = {"peak": groups[peak_key],
                                        "cand": groups[cand_key]}
    return out


def cell_base(cell: str) -> str:
    """The cell identity minus the scheduler, so a PANDEMONIUM cell and its
    EEVDF control twin resolve to the same key."""
    return cell.split(" scheduler=")[0]


def geomean(rs) -> float:
    return math.exp(sum(math.log(r) for r in rs) / len(rs)) if rs else 1.0


def judge(cells: dict, tol: float, abs_floor: float, baseline: str) -> list:
    """The verdict. Pure: no I/O, no clock, no environment -- same cells in,
    same lines out, which is what makes it gateable against a golden.

    cells maps (metric, cell) -> {"peak": float, "cand": float}. PANDEMONIUM is
    the subject; EEVDF cells are consumed as the drift control and never judged.
    """
    lines = []

    drift = {}
    for (metric, cell), mv in cells.items():
        if "EEVDF" in cell and mv["peak"] > 0:
            drift[(metric, cell_base(cell))] = mv["cand"] / mv["peak"]
    global_drift = geomean(list(drift.values()))

    regressions, raw_ratios, corr_ratios = [], [], []
    for (metric, cell), mv in sorted(cells.items()):
        peak, cand = mv["peak"], mv["cand"]
        if peak <= 0 or "PANDEMONIUM" not in cell:
            continue
        d = drift.get((metric, cell_base(cell)), global_drift)
        raw = cand / peak
        corrected = raw / d if d > 0 else raw
        raw_ratios.append(raw)
        corr_ratios.append(corrected)
        if corrected > 1.0 + tol and (cand - peak * d) > abs_floor:
            regressions.append((corrected, metric, cell, peak, cand, d))

    if not corr_ratios:
        lines.append("[gate] no shared PANDEMONIUM cells between baseline and "
                     "candidate -- cannot gate")
        return lines

    corr_geo, raw_geo = geomean(corr_ratios), geomean(raw_ratios)
    lines.append(f"[gate] PANDEMONIUM cells: {len(corr_ratios)}   "
                 f"CORRECTED geomean vs baseline: {corr_geo:.2f}x "
                 f"({'BEHIND' if corr_geo > 1.0 else 'AT-OR-AHEAD OF'} baseline)")
    lines.append(f"[gate] raw geomean {raw_geo:.2f}x   EEVDF drift "
                 f"{global_drift:.2f}x over {len(drift)} control cells "
                 f"(divided out per cell before judgment)")

    regressions.sort(reverse=True)
    if regressions:
        lines.append(f"[gate] {len(regressions)} PANDEMONIUM cell(s) regressed past "
                     f"{tol:.0%} vs the baseline, drift-corrected (worst first):")
        for corrected, metric, cell, peak, cand, d in regressions[:12]:
            lines.append(f"    {corrected:5.2f}x  {metric:12s}  {cell}: "
                         f"baseline {peak:.0f} -> cand {cand:.0f} us "
                         f"(drift {d:.2f}x)")
        if len(regressions) > 12:
            lines.append(f"    ... and {len(regressions) - 12} more")

    passed = not regressions
    lines.append(f"[gate] {'PASS' if passed else 'FAIL'}: "
                 + (f"no load-bearing PANDEMONIUM IPC cell regresses vs v{baseline}, "
                    "drift-corrected" if passed else
                    f"{len(regressions)} PANDEMONIUM cell(s) below the baseline"))
    return lines


def run_analyze(analyzer, args, cache_dir) -> str:
    env = dict(os.environ, XDG_CACHE_HOME=str(cache_dir))
    subprocess.run([analyzer, *args], env=env, capture_output=True, text=True,
                   check=False)
    hits = list((Path(cache_dir) / "montauk").glob("analysis-pop-*.prom"))
    return "\n".join(p.read_text() for p in hits)


def collect(analyzer, archive, candidate, candidate_dir, tmp, baseline) -> dict:
    """Return {(metric, cell): {'peak': m, 'cand': m}} for every load-bearing cell."""
    result: dict = {}
    for neutral, (bench, scale) in IPC_METRICS.items():
        alias = ["--alias", f"{bench}={neutral}", "--alias", f"{scale}={neutral}"]
        cache = Path(tmp) / f"cache-{neutral}"
        if candidate_dir:
            # Isolate the fresh candidate from the archive's stale same-version
            # pool: copy the baseline's own runs out, group both explicitly.
            peakdir = Path(tmp) / f"peak-{neutral}"
            peakdir.mkdir(exist_ok=True)
            for f in Path(archive).glob(f"{baseline}-*.prom"):
                shutil.copy(f, peakdir)
            if not any(peakdir.iterdir()):
                continue
            # Under --by group the version/commit labels stay in the cell
            # identity, which would fragment baseline from candidate so they
            # never share a cell. Drop both so cells match on
            # scheduler/cores/primitive alone.
            text = run_analyze(analyzer, [
                "--group", f"peak={peakdir}", "--group", f"cand={candidate_dir}",
                "--drop-label", "version", "--drop-label", "commit",
                "--metric", neutral, *alias, "--pairs", "all"], cache)
            peak_key, cand_key = "peak", "cand"
        else:
            text = run_analyze(analyzer, [
                str(archive), "--by", "version", "--metric", neutral, *alias,
                "--pairs", "all"], cache)
            peak_key, cand_key = baseline, candidate
        result.update(pair_groups(parse_pop_mean(text), peak_key, cand_key,
                                  neutral))
    return result


def newest_version(archive: Path, baseline: str) -> str:
    vers = set()
    for f in archive.glob("[0-9]*.prom"):
        stem = f.name.split("-2026")[0]
        if stem.count(".") == 2 and stem != baseline:
            vers.add(stem)

    def key(v):
        return [int(x) if x.isdigit() else x for x in v.split(".")]
    return max(vers, key=key) if vers else baseline


def self_test(update: bool) -> int:
    """Run judge() over the synthetic fixture and gate it against the golden.

    The fixture is hand-authored to exercise every branch the drift correction
    has: a cell the correction rescues (raw past tolerance, corrected under it),
    a genuine regression, a ratio past tolerance that the absolute floor
    correctly ignores as noise, a cell with no EEVDF twin that falls back to the
    global drift, and a zero-baseline cell that must be skipped rather than
    divided by.
    """
    if not FIXTURE.is_file():
        print(f"[gate] FAIL self-test: fixture missing ({FIXTURE})")
        return 1
    cells = pair_groups(parse_pop_mean(FIXTURE.read_text()),
                        BASELINE, FIXTURE_CANDIDATE)
    if not cells:
        print(f"[gate] FAIL self-test: fixture parsed to zero cells ({FIXTURE})")
        return 1
    got = "\n".join(judge(cells, DEFAULT_TOL, ABS_FLOOR_US, BASELINE)) + "\n"

    if update:
        # A refreeze canonizes whatever the code printed. Show the diff it is
        # about to stamp as truth, so a regression cannot be frozen in silently.
        if GOLDEN.is_file() and GOLDEN.read_text() != got:
            print("[gate] refreezing golden -- diff being canonized:")
            sys.stdout.writelines(difflib.unified_diff(
                GOLDEN.read_text().splitlines(keepends=True),
                got.splitlines(keepends=True),
                fromfile="golden", tofile="got"))
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(got)
        print(f"[gate] updated golden ({len(got)} bytes, "
              f"{got.count(chr(10))} lines)")
        return 0

    if not GOLDEN.is_file():
        print(f"[gate] FAIL self-test: golden missing ({GOLDEN}) -- "
              "run with --update to freeze it")
        return 1
    want = GOLDEN.read_text()
    if got == want:
        print(f"[gate] PASS self-test: verdict matches golden "
              f"({got.count(chr(10))} lines, {len(cells)} fixture cells)")
        return 0
    print("[gate] FAIL self-test: verdict diverged from golden:")
    sys.stdout.writelines(difflib.unified_diff(
        want.splitlines(keepends=True), got.splitlines(keepends=True),
        fromfile="golden", tofile="got"))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE,
                    help="benchmark .prom archive (default ~/.cache/pandemonium)")
    ap.add_argument("--candidate", default=None,
                    help="version in the archive to gate (default: newest)")
    ap.add_argument("--candidate-dir", type=Path, default=None,
                    help="a directory of fresh .prom runs to gate vs the baseline")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL,
                    help=f"allowed regression ratio above baseline (default {DEFAULT_TOL})")
    ap.add_argument("--baseline", default=BASELINE,
                    help=f"baseline version to gate against (default {BASELINE}; "
                         "for archaeology, not for lowering the bar)")
    ap.add_argument("--self-test", action="store_true",
                    help="gate judge() against the synthetic fixture and golden "
                         "(no archive, no montauk, no hardware)")
    ap.add_argument("--update", action="store_true",
                    help="with --self-test: refreeze the golden, showing the diff")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args.update)
    if args.update:
        sys.exit("baseline_gate: --update only applies with --self-test")

    analyzer = resolve_analyzer()
    if not args.archive.exists():
        sys.exit(f"baseline_gate: archive not found: {args.archive}")
    candidate = args.candidate or (
        "cand" if args.candidate_dir else newest_version(args.archive, args.baseline))

    label = (f"fresh runs in {args.candidate_dir}" if args.candidate_dir
             else f"version {candidate}")
    print(f"[gate] analyzer: {analyzer}")
    print(f"[gate] baseline: v{args.baseline}   candidate: {label}")

    with tempfile.TemporaryDirectory(prefix="baseline-gate-") as tmp:
        cells = collect(analyzer, args.archive, candidate, args.candidate_dir,
                        tmp, args.baseline)

    if not cells:
        print("[gate] no shared IPC cells between baseline and candidate -- "
              "cannot gate (is the candidate benched on the same primitives?)")
        return 1

    lines = judge(cells, args.tol, ABS_FLOOR_US, args.baseline)
    for line in lines:
        print(line)
    return 0 if any(line.startswith("[gate] PASS") for line in lines) else 1


if __name__ == "__main__":
    sys.exit(main())
