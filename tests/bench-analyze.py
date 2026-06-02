#!/usr/bin/env python3
"""PANDEMONIUM bench-analyze: statistical analysis of bench-scale .prom logs.

Reads one or more Prometheus .prom files (each file is one replication / run)
and answers what the raw tables cannot:

  POWER  -- how many runs are needed to call a scheduler difference real, given
            the run-to-run spread (Monte-Carlo).
  REAL?  -- per (workload x core-count) cell, is the difference signal or noise.
  BEST?  -- across schedulers, is one best (or tied): Hsu's MCB.

Two input layers, both consumed:

  * PER-RUN P99 (summary gauges, present in EVERY file): the run-to-run
    inferential unit. Power, the run-level permutation test, Cliff's delta, and
    MCB all run on this -- so multiple runs of the same version work with no
    re-run, even on files that predate the histograms.
  * WITHIN-RUN DISTRIBUTION (histograms, v5.12.0+): adds sample-level
    tail-shape detail -- QANOVA (quantile permutation), Games-Howell, and a
    within-run Cliff's delta -- on top.

Results print as a report and re-emit as their own .prom
(pandemonium_analysis_*), keeping the loop inside Prometheus. numpy + scipy.

  --trace  -- analyze a montauk --trace recording instead of bench-scale: the
             intra- vs cross-CCX MIGRATION split (the imbalance/cache signal the
             Phi work is measured by) and the per-CCX L2/L3 cache split, for a
             PANDEMONIUM recording vs an EEVDF reference. A montauk recording is
             a concatenation of per-scrape expositions (LogWriter), a different
             schema than the bench-scale histograms, so it has its own parser.
"""

import argparse
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import studentized_range, t as t_dist


CACHE_DIR = (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
             / "pandemonium")

HIST_PREFIX = "pandemonium_bench_"

# WORKLOADS CARRYING A us LATENCY DISTRIBUTION (LOWER IS BETTER), REPORT ORDER.
# THE HISTOGRAM FAMILY IS HIST_PREFIX+wl; THE PER-RUN P99 GAUGE IS
# HIST_PREFIX+wl+"_p99_us".
WORKLOADS = [
    "latency", "burst", "burst_baseline", "burst_recovery",
    "longrun_latency", "mixed_latency", "deadline_jitter",
    "ipc_rtt", "launch",
]
P99_GAUGE = {HIST_PREFIX + wl + "_p99_us": wl for wl in WORKLOADS}

# CLOSED-LOOP BY CONSTRUCTION: TAILS MAY UNDER-REPORT (COORDINATED OMISSION).
CLOSED_LOOP = {"ipc_rtt", "launch"}

LARGEST_FINITE_LE = 1_000_000.0
MIN_RUNS_FOR_RUNVEC = 2


def log(level, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}]   {msg}")


# PROMETHEUS PARSING

_METRIC_RE = re.compile(r'^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)\s*$')
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_prom(path):
    """Parse a .prom file into (hist, gauges, meta).

    hist[family][labelkey] = {"buckets","sum","count"} (histogram series).
    gauges[workload][labelkey] = per-run P99 value (summary gauge).
    labelkey is a sorted tuple of (k, v) excluding `le`.
    """
    types = {}
    hist = {}
    gauges = {}
    meta = {"version": "unknown", "path": str(path)}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# TYPE"):
            parts = line.split()
            if len(parts) >= 4:
                types[parts[2]] = parts[3]
            continue
        if line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, labelstr, valstr = m.group(1), m.group(2) or "", m.group(3)
        labels = dict(_LABEL_RE.findall(labelstr))
        try:
            val = float(valstr)
        except ValueError:
            continue
        if name == "pandemonium_bench_info":
            meta["version"] = labels.get("version", meta["version"])
            continue
        fam = role = None
        for suffix in ("_bucket", "_sum", "_count"):
            base = name[: -len(suffix)]
            if name.endswith(suffix) and types.get(base) == "histogram":
                fam, role = base, suffix[1:]
                break
        if fam is not None:
            key = tuple(sorted((k, v) for k, v in labels.items() if k != "le"))
            h = hist.setdefault(fam, {}).setdefault(
                key, {"buckets": {}, "sum": 0.0, "count": 0})
            if role == "bucket":
                h["buckets"][labels.get("le", "+Inf")] = int(val)
            elif role == "sum":
                h["sum"] = val
            else:
                h["count"] = int(val)
        elif name in P99_GAUGE and val > 0:
            key = tuple(sorted(labels.items()))
            gauges.setdefault(P99_GAUGE[name], {})[key] = val
    return hist, gauges, meta


def _le_key(le):
    return math.inf if le == "+Inf" else float(le)


def reconstruct(h):
    """Histogram dict -> np.array of representative samples (geometric-midpoint
    placement; +Inf overflow at the largest finite boundary)."""
    buckets = sorted(h["buckets"].items(), key=lambda kv: _le_key(kv[0]))
    samples = []
    prev_cum = 0
    prev_le = 0.0
    for le, cum in buckets:
        n = cum - prev_cum
        if n > 0:
            if le == "+Inf":
                rep = prev_le if prev_le > 0 else 1.0
            else:
                le_f = float(le)
                rep = le_f if prev_le <= 0 else math.sqrt(prev_le * le_f)
            samples.extend([rep] * n)
        prev_cum = cum
        if le != "+Inf":
            prev_le = float(le)
    return np.asarray(samples, dtype=float)


# STATISTICS

def cliffs_delta(a, b):
    """Cliff's delta in [-1, 1]. >0: `a` tends to exceed `b` (worse for
    latency). Tie-correct via searchsorted."""
    a = np.asarray(a, float)
    bs = np.sort(np.asarray(b, float))
    nb = len(bs)
    if len(a) == 0 or nb == 0:
        return 0.0
    less = np.searchsorted(bs, a, side="left")
    greater = nb - np.searchsorted(bs, a, side="right")
    return float((less - greater).sum() / (len(a) * nb))


def cliff_magnitude(d):
    a = abs(d)
    if a < 0.147:
        return "negligible"
    if a < 0.33:
        return "small"
    if a < 0.474:
        return "medium"
    return "large"


def diff_perm_test(a, b, statfn, rng, n_perm=2000):
    """Permutation test on |statfn(a) - statfn(b)|. Distribution-free."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    obs = abs(statfn(a) - statfn(b))
    pool = np.concatenate([a, b])
    na = len(a)
    ge = 0
    for _ in range(n_perm):
        p = rng.permutation(pool)
        if abs(statfn(p[:na]) - statfn(p[na:])) >= obs:
            ge += 1
    return obs, (ge + 1) / (n_perm + 1)


def games_howell(groups):
    """Pairwise Games-Howell (unequal variance/n) via the studentized range.
    Returns {(name_i, name_j): (mean_diff, p)}."""
    names = list(groups)
    k = len(names)
    out = {}
    for i in range(k):
        for j in range(i + 1, k):
            x, y = groups[names[i]], groups[names[j]]
            ni, nj = len(x), len(y)
            if ni < 2 or nj < 2:
                out[(names[i], names[j])] = (x.mean() - y.mean(), 1.0)
                continue
            mi, mj = x.mean(), y.mean()
            vi, vj = x.var(ddof=1), y.var(ddof=1)
            se = math.sqrt(vi / ni + vj / nj)
            if se == 0:
                out[(names[i], names[j])] = (mi - mj, 1.0)
                continue
            t = abs(mi - mj) / se
            df = (vi / ni + vj / nj) ** 2 / (
                (vi / ni) ** 2 / (ni - 1) + (vj / nj) ** 2 / (nj - 1))
            p = float(studentized_range.sf(t * math.sqrt(2.0), k, df))
            out[(names[i], names[j])] = (mi - mj, min(1.0, p))
    return out


def bootstrap_mcb(groups, statfn, rng, lower_is_better=True, B=2000):
    """Hsu's MCB via bootstrap simultaneous CIs on theta_i - best-of-rest.
    Returns {name: (ci_lo, ci_hi, verdict)}; verdict in best/tied/not-best."""
    names = list(groups)
    boot = {n: np.empty(B) for n in names}
    for b in range(B):
        s = {n: statfn(rng.choice(groups[n], len(groups[n]), replace=True))
             for n in names}
        for n in names:
            others = [s[o] for o in names if o != n]
            if not others:
                boot[n][b] = 0.0
                continue
            ref = min(others) if lower_is_better else max(others)
            boot[n][b] = s[n] - ref
    res = {}
    for n in names:
        lo, hi = (float(v) for v in np.percentile(boot[n], [2.5, 97.5]))
        if lower_is_better:
            verdict = "best" if hi < 0 else ("not-best" if lo > 0 else "tied")
        else:
            verdict = "best" if lo > 0 else ("not-best" if hi < 0 else "tied")
        res[n] = (lo, hi, verdict)
    return res


def mc_power(pa, pb, rng, alpha=0.05, target=0.8, n_grid=range(2, 21), B=2000):
    """Sweep run-count n; at each, draw B experiments of n per-run statistics
    from each pool and test the difference with a Welch t (vectorized over all
    B at once). Returns (n_needed | None, {n: power})."""
    pa, pb = np.asarray(pa, float), np.asarray(pb, float)
    curve = {}
    chosen = None
    for n in n_grid:
        xa = rng.choice(pa, size=(B, n))
        xb = rng.choice(pb, size=(B, n))
        sa = xa.var(1, ddof=1) / n
        sb = xb.var(1, ddof=1) / n
        se = np.sqrt(sa + sb)
        with np.errstate(divide="ignore", invalid="ignore"):
            tval = np.where(se > 0, np.abs(xa.mean(1) - xb.mean(1)) / se, 0.0)
            df = np.where(se > 0,
                          (sa + sb) ** 2 / (sa ** 2 / (n - 1) + sb ** 2 / (n - 1)),
                          1.0)
        power = float(np.mean(2.0 * t_dist.sf(tval, df) < alpha))
        curve[n] = power
        if chosen is None and power >= target:
            chosen = n
    return chosen, curve


def power_pools(va, vb, sa, sb, statfn, rng, K=400):
    """Choose the per-run statistic distribution for power. Prefer the real
    run-to-run vectors; else bootstrap the within-run distribution as a proxy."""
    if len(va) >= MIN_RUNS_FOR_RUNVEC and len(vb) >= MIN_RUNS_FOR_RUNVEC:
        return np.asarray(va, float), np.asarray(vb, float), f"run-vec N={len(va)}/{len(vb)}"
    if sa is not None and sb is not None:
        pa = np.array([statfn(rng.choice(sa, len(sa), replace=True)) for _ in range(K)])
        pb = np.array([statfn(rng.choice(sb, len(sb), replace=True)) for _ in range(K)])
        return pa, pb, "within-run proxy (N<2)"
    return np.asarray(va, float), np.asarray(vb, float), "degenerate"


# DATA GATHER

def short_sched(name):
    n = name.lower()
    if "pandemonium" in n and "bpf" in n:
        return "bpf"
    if "pandemonium" in n and "adaptive" in n:
        return "adaptive"
    if "eevdf" in n:
        return "eevdf"
    return re.sub(r'[^a-z0-9]+', '_', n).strip('_') or "sched"


def gather(files):
    """-> data[version][workload][cores][sched] = {'p99_runs', 'samples'}.

    p99_runs: per-file P99 (from the gauges; one per file, any file).
    samples : pooled reconstructed within-run distribution, or None if no file
              for this cell carried a histogram.
    """
    data = {}

    def cell(ver, wl, cores, sched):
        c = (data.setdefault(ver, {}).setdefault(wl, {})
             .setdefault(int(cores), {}))
        return c.setdefault(sched, {"p99_runs": [], "chunks": []})

    for f in files:
        hist, gauges, meta = parse_prom(f)
        ver = meta["version"]
        for wl, series in gauges.items():
            for key, val in series.items():
                labels = dict(key)
                sched, cores = labels.get("scheduler"), labels.get("cores")
                if sched is None or cores is None:
                    continue
                cell(ver, wl, cores, sched)["p99_runs"].append(float(val))
        for fam, series in hist.items():
            if not fam.startswith(HIST_PREFIX):
                continue
            wl = fam[len(HIST_PREFIX):]
            for key, h in series.items():
                labels = dict(key)
                sched, cores = labels.get("scheduler"), labels.get("cores")
                if sched is None or cores is None or not h["buckets"]:
                    continue
                samp = reconstruct(h)
                if samp.size:
                    cell(ver, wl, cores, sched)["chunks"].append(samp)
    for ver in data.values():
        for wl in ver.values():
            for cores in wl.values():
                for slot in cores.values():
                    slot["samples"] = (np.concatenate(slot["chunks"])
                                       if slot["chunks"] else None)
                    del slot["chunks"]
    return data


# ANALYSIS

def analyze(data, rng, q=99.0):
    prom = []
    report = []

    def p99(x):
        return float(np.percentile(x, q))

    for ver in sorted(data):
        report.append(f"VERSION {ver}")
        for wl in WORKLOADS:
            if wl not in data[ver]:
                continue
            report.append(f"  {wl.upper()}")
            for cores in sorted(data[ver][wl]):
                cell = data[ver][wl][cores]
                scheds = list(cell)
                report.append(f"    [{cores}C]")
                if len(scheds) < 2:
                    report.append(f"      only {short_sched(scheds[0])} -- no comparison")
                    continue

                has_hist = any(cell[s]["samples"] is not None for s in scheds)

                # RUN-LEVEL INFERENCE (per-run P99 vectors; old + new files).
                for i in range(len(scheds)):
                    for j in range(i + 1, len(scheds)):
                        si, sj = scheds[i], scheds[j]
                        va, vb = cell[si]["p99_runs"], cell[sj]["p99_runs"]
                        if not va or not vb:
                            continue
                        ma, mb = float(np.mean(va)), float(np.mean(vb))
                        rng_a = f"{min(va):.0f}..{max(va):.0f}"
                        rng_b = f"{min(vb):.0f}..{max(vb):.0f}"
                        rdelta = cliffs_delta(va, vb)
                        _, rp = diff_perm_test(va, vb, np.mean, rng)
                        pa, pb, mode = power_pools(
                            va, vb, cell[si]["samples"], cell[sj]["samples"],
                            p99, rng)
                        npow, _ = mc_power(pa, pb, rng)
                        pwr = str(npow) if npow else ">20"
                        report.append(
                            f"      {short_sched(si)} vs {short_sched(sj)} "
                            f"[run-level, N={len(va)}/{len(vb)}]: "
                            f"p99 {ma:.0f} ({rng_a}) vs {mb:.0f} ({rng_b}) | "
                            f"cliff {rdelta:+.2f} ({cliff_magnitude(rdelta)}) | "
                            f"perm p={rp:.3f} | power {pwr} runs [{mode}]")
                        labels = {"version": ver, "workload": wl,
                                  "cores": str(cores),
                                  "pair": f"{short_sched(si)}__{short_sched(sj)}"}
                        prom.append(("pandemonium_analysis_runlevel_cliff", labels, round(rdelta, 5)))
                        prom.append(("pandemonium_analysis_runlevel_perm_p", labels, round(rp, 5)))
                        prom.append(("pandemonium_analysis_power_n", labels, npow if npow else 0))

                        # WITHIN-RUN TAIL DETAIL (histograms only).
                        sa, sb = cell[si]["samples"], cell[sj]["samples"]
                        if sa is not None and sb is not None:
                            _, pq = diff_perm_test(sa, sb, lambda x: np.percentile(x, q), rng)
                            wdelta = cliffs_delta(sa, sb)
                            report.append(
                                f"        within-run: q{q:.0f} "
                                f"{p99(sa):.0f} vs {p99(sb):.0f} | "
                                f"qanova p={pq:.3f} | "
                                f"cliff {wdelta:+.2f} ({cliff_magnitude(wdelta)})")
                            prom.append(("pandemonium_analysis_qanova_p", labels, round(pq, 5)))
                            prom.append(("pandemonium_analysis_withinrun_cliff", labels, round(wdelta, 5)))

                # GAMES-HOWELL ON THE WITHIN-RUN DISTRIBUTIONS (if histograms).
                if has_hist:
                    hg = {s: cell[s]["samples"] for s in scheds
                          if cell[s]["samples"] is not None}
                    if len(hg) >= 2:
                        for (si, sj), (_, ghp) in games_howell(hg).items():
                            labels = {"version": ver, "workload": wl,
                                      "cores": str(cores),
                                      "pair": f"{short_sched(si)}__{short_sched(sj)}"}
                            prom.append(("pandemonium_analysis_gameshowell_p", labels, round(ghp, 5)))

                # MCB ON RUN-LEVEL P99 MEANS ("is one best across runs").
                rg = {s: np.asarray(cell[s]["p99_runs"], float)
                      for s in scheds if cell[s]["p99_runs"]}
                if len(rg) >= 2:
                    mcb = bootstrap_mcb(rg, np.mean, rng)
                    verdicts = ", ".join(f"{short_sched(s)}={v}"
                                         for s, (lo, hi, v) in mcb.items())
                    report.append(f"      MCB (run-level P99, lower=better): {verdicts}")
                    for s, (lo, hi, v) in mcb.items():
                        labels = {"version": ver, "workload": wl,
                                  "cores": str(cores), "scheduler": s}
                        prom.append(("pandemonium_analysis_mcb_lower", labels, round(lo, 2)))
                        prom.append(("pandemonium_analysis_mcb_upper", labels, round(hi, 2)))
                        prom.append(("pandemonium_analysis_mcb_is_best", labels,
                                     {"best": 1, "tied": 0, "not-best": -1}[v]))

                # MEASUREMENT SANITY (within-run only; CO caveat on closed-loop).
                if has_hist:
                    sanity = []
                    for s in scheds:
                        samp = cell[s]["samples"]
                        if samp is None:
                            continue
                        med = float(np.percentile(samp, 50)) or 1.0
                        p_ = float(np.percentile(samp, 99))
                        ovf = float(np.mean(samp >= LARGEST_FINITE_LE))
                        sanity.append(f"{short_sched(s)} p99/med={p_ / med:.1f} ovf={ovf:.3f}")
                        labels = {"version": ver, "workload": wl,
                                  "cores": str(cores), "scheduler": s}
                        prom.append(("pandemonium_analysis_tail_ratio", labels, round(p_ / med, 3)))
                        prom.append(("pandemonium_analysis_overflow_frac", labels, round(ovf, 5)))
                    if sanity:
                        tag = " [CLOSED-LOOP: P99 may under-report (CO)]" if wl in CLOSED_LOOP else ""
                        report.append(f"      sanity: {' | '.join(sanity)}{tag}")
    return report, prom


# PROMETHEUS OUTPUT

_ANALYSIS_HELP = {
    "pandemonium_analysis_runlevel_cliff":
        "Cliff's delta on per-run P99 vectors (>0: first scheduler worse), pair",
    "pandemonium_analysis_runlevel_perm_p":
        "Permutation-test p on the per-run P99 mean difference, pair",
    "pandemonium_analysis_power_n":
        "MC runs for 0.8 power to detect the P99 difference (0=not reached <=20)",
    "pandemonium_analysis_qanova_p":
        "Within-run quantile (P99) permutation-test p-value, pair",
    "pandemonium_analysis_withinrun_cliff":
        "Cliff's delta on within-run distributions (>0: first worse), pair",
    "pandemonium_analysis_gameshowell_p":
        "Games-Howell pairwise p on the within-run mean, pair",
    "pandemonium_analysis_mcb_lower":
        "Hsu MCB CI lower bound on run-level P99 (theta_i - best-of-rest)",
    "pandemonium_analysis_mcb_upper":
        "Hsu MCB CI upper bound on run-level P99",
    "pandemonium_analysis_mcb_is_best":
        "MCB verdict: 1=best, 0=tied-for-best, -1=not best",
    "pandemonium_analysis_tail_ratio":
        "Within-run P99/median tail ratio",
    "pandemonium_analysis_overflow_frac":
        "Fraction of within-run samples beyond the largest finite bucket",
}


def write_analysis_prom(prom, stamp, out_dir):
    lines = []
    emitted = set()
    for name, labels, val in prom:
        if name not in emitted:
            lines.append(f"# HELP {name} {_ANALYSIS_HELP.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            emitted.add(name)
        lab = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{lab}}} {val}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"analysis-{stamp}.prom"
    path.write_text("\n".join(lines) + "\n")
    return path


# MONTAUK --trace RECORDING ANALYSIS
#
# A montauk --trace recording is a different artifact than a bench-scale .prom:
# it is a concatenation of per-scrape Prometheus expositions (the LogWriter,
# ~100ms cadence) carrying the global intra/cross-CCX migration counters and the
# PMU collector's L2/L3 split. Migration counters are cumulative (take the last
# value); PMU metrics are per-interval deltas (aggregated over the busy scrapes --
# the storm, where L2-misses are above 30% of peak). This is the imbalance /
# cache-locality signal the fork-thread / Phi work is measured by.

_TRACE_DELIM = "# HELP montauk_cpu_usage_percent"
_TRACE_LINE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-+0-9.eE]+)\s*$')


def _fmt_count(n):
    n = float(n)
    for u in ("", "K", "M", "G"):
        if abs(n) < 1000:
            return f"{n:,.0f}{u}" if u else f"{n:,.0f}"
        n /= 1000
    return f"{n:,.1f}T"


def parse_trace_prom(text):
    """Concatenated montauk scrapes -> aggregated trace metrics dict."""
    scrapes, cur = [], []
    for ln in text.splitlines():
        if ln.startswith(_TRACE_DELIM) and cur:
            scrapes.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        scrapes.append(cur)
    parsed = []
    for s in scrapes:
        bare, lab = {}, {}
        for ln in s:
            # Skip the per-thread firehose; only the global migration + PMU lines.
            if not (ln.startswith("montauk_trace_migrations_")
                    or ln.startswith("montauk_pmu_")):
                continue
            m = _TRACE_LINE.match(ln)
            if not m:
                continue
            name, labelstr, valstr = m.group(1), m.group(2), m.group(3)
            try:
                val = float(valstr)
            except ValueError:
                continue
            if labelstr:
                d = dict(_LABEL_RE.findall(labelstr))
                key = d.get("ccx", next(iter(d.values()), ""))
                lab.setdefault(name, {})[key] = val
            else:
                bare[name] = val
        if bare or lab:
            parsed.append((bare, lab))

    mig = {"intra": 0.0, "cross": 0.0, "unknown": 0.0}
    for bare, _ in parsed:
        for k, nm in (("intra", "montauk_trace_migrations_intra_ccx"),
                      ("cross", "montauk_trace_migrations_cross_ccx"),
                      ("unknown", "montauk_trace_migrations_unknown_ccx")):
            if bare.get(nm) is not None:
                mig[k] = bare[nm]

    l2_series = [b.get("montauk_pmu_l2_misses_interval", 0.0) for b, _ in parsed]
    peak = max(l2_series) if l2_series else 0.0
    busy = [i for i, v in enumerate(l2_series) if v > 0.3 * peak] if peak > 0 else []
    pmu_avail = any(b.get("montauk_pmu_available", 0) >= 1 for b, _ in parsed)
    l2_total = sum(l2_series[i] for i in busy)
    cyc = sorted(parsed[i][0].get("montauk_pmu_cycles_per_l2_miss", 0.0) for i in busy)
    cyc_med = cyc[len(cyc) // 2] if cyc else 0.0
    l3_avail = any(b.get("montauk_pmu_l3_available", 0) >= 1 for b, _ in parsed)
    l3_miss, l3_acc = {}, {}
    for i in busy:
        _, lb = parsed[i]
        for c, v in lb.get("montauk_pmu_l3_misses_interval", {}).items():
            l3_miss[c] = l3_miss.get(c, 0.0) + v
        for c, v in lb.get("montauk_pmu_l3_accesses_interval", {}).items():
            l3_acc[c] = l3_acc.get(c, 0.0) + v
    return {"scrapes": len(parsed), "busy": len(busy), "mig": mig,
            "pmu": pmu_avail, "l2_total": l2_total, "cyc_per_l2_miss": cyc_med,
            "l3": l3_avail, "l3_miss": l3_miss, "l3_acc": l3_acc}


def load_trace_dir(d):
    """Read + concat all montauk_*.prom in a recording dir, parse to metrics."""
    if d is None or not Path(d).is_dir():
        return None
    files = sorted(Path(d).glob("montauk_*.prom"))
    if not files:
        return None
    text = "".join(f.read_text(errors="replace") for f in files)
    rec = parse_trace_prom(text)
    rec["dir"] = Path(d).name
    return rec


def find_trace_dirs(log_dir):
    """Freshest EEVDF + PANDEMONIUM montauk recording dirs in the cache dir."""
    def newest(prefix):
        best, bt = None, -1.0
        for d in Path(log_dir).glob(prefix + "*"):
            if d.is_dir() and d.stat().st_mtime > bt:
                best, bt = d, d.stat().st_mtime
        return best
    return (newest("montauk-fork-thread-EEVDF-"),
            newest("montauk-fork-thread-PANDEMONIUM-BPF-"))


def analyze_trace(recs):
    """recs: {'EEVDF': rec|None, 'PANDEMONIUM': rec|None}. Print the report."""
    names = [n for n in ("EEVDF", "PANDEMONIUM") if recs.get(n)]
    if not names:
        log("ERROR", "no montauk --trace recordings found "
                     "(montauk-fork-thread-*/montauk_*.prom)")
        return 1
    for n in names:
        log("INFO", f"{n:<11} -> {recs[n]['dir']} ({recs[n]['scrapes']} scrapes)")

    def total(r):
        return r["mig"]["intra"] + r["mig"]["cross"] + r["mig"]["unknown"]

    def row(label, fn):
        print(f"{label:<26}" + "".join(f"{fn(recs[n]):>16}" for n in names))

    print()
    print(f"{'MIGRATIONS (cumulative)':<26}" + "".join(f"{n:>16}" for n in names))
    row("intra-CCX", lambda r: _fmt_count(r["mig"]["intra"]))
    row("cross-CCX", lambda r: _fmt_count(r["mig"]["cross"]))
    row("unknown",   lambda r: _fmt_count(r["mig"]["unknown"]))
    row("total",     lambda r: _fmt_count(total(r)))
    row("cross-CCX fraction",
        lambda r: f"{100 * r['mig']['cross'] / total(r):.1f}%" if total(r) else "n/a")

    if any(recs[n]["pmu"] for n in names):
        print()
        row("L2 misses (storm)",       lambda r: _fmt_count(r["l2_total"]))
        row("cycles/L2-miss (median)", lambda r: f"{r['cyc_per_l2_miss']:.0f}")
        if any(recs[n]["l3"] for n in names):
            ccxs = sorted({c for n in names for c in recs[n]["l3_miss"]})
            for c in ccxs:
                row(f"L3 misses CCX[{c}]",
                    lambda r, c=c: _fmt_count(r["l3_miss"].get(c, 0)))
            row("L3 misses total", lambda r: _fmt_count(sum(r["l3_miss"].values())))
        else:
            log("WARN", "amd_l3 PMU absent in recording -- no per-CCX L3 split "
                        "(load amd_uncore + perf_event_paranoid<=1 before capture)")
    else:
        log("WARN", "core PMU absent in recording -- migration split only "
                    "(capture as root with perf_event_paranoid<=1)")

    print()
    pan, eev = recs.get("PANDEMONIUM"), recs.get("EEVDF")
    if pan and total(pan) > 0:
        pi, pc, pt = pan["mig"]["intra"], pan["mig"]["cross"], total(pan)
        if eev and total(eev) > 0:
            log("INFO", f"PANDEMONIUM migrates {pt / total(eev):.1f}x EEVDF "
                        f"(intra {pi / max(eev['mig']['intra'], 1):.1f}x, "
                        f"cross {pc / max(eev['mig']['cross'], 1):.1f}x)")
        log("INFO", f"PANDEMONIUM split: {100 * pi / pt:.0f}% intra-CCX, "
                    f"{100 * pc / pt:.0f}% cross-CCX")
        if pan["pmu"] and pan["l3"]:
            l3t = sum(pan["l3_miss"].values())
            log("INFO", f"L2 {_fmt_count(pan['l2_total'])} vs L3 {_fmt_count(l3t)} "
                        "-- low L3 share => warm-L3 intra-CCX (placement) misses dominate")
    elif pan:
        log("WARN", "PANDEMONIUM recorded zero migrations -- "
                    "did --trace match the workload, and was it the bench group?")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Statistical analysis of bench-scale .prom logs.")
    ap.add_argument("files", nargs="*",
                    help="explicit .prom files (default: glob by --version)")
    ap.add_argument("--version",
                    help="analyze all <version>-*.prom in the cache dir")
    ap.add_argument("--log-dir", default=str(CACHE_DIR),
                    help="cache dir for .prom input/output")
    ap.add_argument("--quantile", type=float, default=99.0,
                    help="within-run quantile to test (default 99)")
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--no-emit", action="store_true",
                    help="do not write the analysis .prom")
    ap.add_argument("--trace", action="store_true",
                    help="analyze montauk --trace recordings (intra/cross-CCX "
                         "migration split + L2/L3) instead of bench-scale stats")
    ap.add_argument("--trace-dir", nargs="+",
                    help="explicit montauk recording dir(s) for --trace "
                         "(default: freshest EEVDF + PANDEMONIUM in the cache dir)")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)

    if args.trace or args.trace_dir:
        if args.trace_dir:
            recs = {}
            for d in args.trace_dir:
                rec = load_trace_dir(d)
                if rec is None:
                    log("WARN", f"no montauk_*.prom in {d}")
                    continue
                recs["EEVDF" if "EEVDF" in Path(d).name else "PANDEMONIUM"] = rec
        else:
            e, p = find_trace_dirs(log_dir)
            log("INFO", "trace mode: freshest recordings in the cache dir")
            recs = {"EEVDF": load_trace_dir(e), "PANDEMONIUM": load_trace_dir(p)}
        return analyze_trace(recs)

    files = [Path(f) for f in args.files]
    if not files:
        pat = f"{args.version}-*.prom" if args.version else "*.prom"
        files = [f for f in sorted(log_dir.glob(pat))
                 if not f.name.startswith("analysis-")]
    if not files:
        log("ERROR", f"no .prom files (dir={log_dir}, version={args.version})")
        return 1

    log("INFO", f"{len(files)} file(s): {', '.join(f.name for f in files)}")
    rng = np.random.default_rng(args.seed)
    data = gather(files)
    if not data:
        log("ERROR", "no usable data in inputs (need per-run P99 gauges)")
        return 1
    n_hist = sum(1 for ver in data.values() for wl in ver.values()
                 for c in wl.values() for s in c.values()
                 if s["samples"] is not None)
    if n_hist == 0:
        log("INFO", "no histograms found -- run-level analysis only "
                    "(re-run bench-scale on v5.12.0+ for within-run detail)")

    report, prom = analyze(data, rng, q=args.quantile)
    print()
    print("\n".join(report))
    print()
    if not args.no_emit and prom:
        path = write_analysis_prom(
            prom, datetime.now().strftime("%Y%m%d-%H%M%S"), log_dir)
        log("INFO", f"analysis metrics -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
