#!/usr/bin/env python3
"""prism-plot: render a prism-scale .prom into a shareable PNG.

Two styles:
  --style bars  (default)  vertical grouped bars of ONE metric across core
                           counts, one bar per scheduler. Dark theme.
  --style cachyos          horizontal grouped bars in the CachyOS comparison
                           format: workloads as rows at a fixed core count, one
                           bar per scheduler, value labels, light theme.

  ./tests/prism-plot.py                                  # newest .prom, latency, bars
  ./tests/prism-plot.py FILE --metric ipc_p99            # a specific metric
  ./tests/prism-plot.py FILE --style cachyos --exclude scx_cosmos
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pandemonium_common import log_info, log_error  # noqa: E402

CACHE_DIR = (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
             / "pandemonium")

# key -> (gauge, y-axis label, chart title)
METRICS = {
    "latency_p99":         ("pandemonium_bench_latency_p99_us",         "P99 wakeup latency (us)",   "P99 Wakeup Latency"),
    "ipc_p99":             ("pandemonium_bench_ipc_rtt_p99_us",         "P99 IPC round-trip (us)",   "P99 IPC Round-Trip"),
    "mixed_p99":           ("pandemonium_bench_mixed_latency_p99_us",   "P99 mixed latency (us)",    "P99 Mixed Latency"),
    "burst_p99":           ("pandemonium_bench_burst_p99_us",           "P99 burst latency (us)",    "P99 Burst Latency"),
    "longrun_p99":         ("pandemonium_bench_longrun_latency_p99_us", "P99 long-run latency (us)", "P99 Long-Run Latency"),
    "launch_p99":          ("pandemonium_bench_launch_p99_us",          "P99 app-launch (us)",       "P99 App-Launch"),
    "deadline_jitter_p99": ("pandemonium_bench_deadline_jitter_p99_us", "P99 deadline jitter (us)",  "P99 Deadline Jitter"),
    "throughput":          ("pandemonium_bench_throughput_seconds",     "build wall-time (s)",       "Build Throughput"),
}

# CachyOS-style chart: workloads (rows), all P99 latency in us.
CACHYOS_WORKLOADS = [
    ("Wakeup P99",          "pandemonium_bench_latency_p99_us"),
    ("Burst P99",           "pandemonium_bench_burst_p99_us"),
    ("Burst-recovery P99",  "pandemonium_bench_burst_recovery_p99_us"),
    ("Long-run P99",        "pandemonium_bench_longrun_latency_p99_us"),
    ("Mixed P99",           "pandemonium_bench_mixed_latency_p99_us"),
    ("Deadline jitter P99", "pandemonium_bench_deadline_jitter_p99_us"),
    ("IPC round-trip P99",  "pandemonium_bench_ipc_rtt_p99_us"),
    ("App-launch P99",      "pandemonium_bench_launch_p99_us"),
]

_METRIC_RE = re.compile(r'^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)\s*$')
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_prom(path, wanted):
    vals = {}
    meta = {"version": "?", "iterations": "?"}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, lbl, val = m.group(1), m.group(2) or "", m.group(3)
        labels = dict(_LABEL_RE.findall(lbl))
        if name == "pandemonium_bench_iterations":
            try:
                meta["iterations"] = str(int(float(val)))
            except ValueError:
                pass
            continue
        if name == "pandemonium_bench_info":
            meta["version"] = labels.get("version", meta["version"])
            continue
        if name in wanted:
            sched, cores = labels.get("scheduler"), labels.get("cores")
            if sched and cores:
                try:
                    vals.setdefault(name, {})[(sched, int(cores))] = float(val)
                except ValueError:
                    pass
    return vals, meta


def is_pande(s):
    return "PANDEMONIUM" in s


def short_name(s):
    if is_pande(s) and "ADAPTIVE" in s:
        return "scx_pandemonium (adaptive)"
    if is_pande(s) and "BPF" in s:
        return "scx_pandemonium (bpf)"
    return s


def sched_version(s, meta):
    """Version string for a scheduler label, so the chart is self-documenting.
    EEVDF -> kernel release; scx_pandemonium -> the run's version from the .prom;
    scx_* -> the installed binary's --version."""
    if "EEVDF" in s:
        try:
            return os.uname().release
        except Exception:
            return ""
    if is_pande(s):
        return f"v{meta.get('version', '')}"
    try:
        out = subprocess.run([s, "--version"], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r"\b(\d+\.\d+\.\d+)\b", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def sched_order(s):
    # EEVDF baseline first, then the two PANDEMONIUM variants, then the rest.
    if "EEVDF" in s:
        return (0, s)
    if is_pande(s) and "BPF" in s:
        return (1, s)
    if is_pande(s) and "ADAPTIVE" in s:
        return (2, s)
    return (3, s)


# Echoes the CachyOS comparison palette: baseline blue, pandemonium green,
# cake orange, the scx field in red/purple. PANDEMONIUM variants greens.
SCHED_COLOR = {
    "EEVDF":                  "#1f77b4",
    "PANDEMONIUM (BPF)":      "#2ca02c",
    "PANDEMONIUM (ADAPTIVE)": "#17becf",
    "scx_bpfland":            "#d62728",
    "scx_cake":               "#ff7f0e",
    "scx_lavd":               "#9467bd",
    "scx_rusty":              "#8c564b",
    "scx_layered":            "#e377c2",
}
_FALLBACK = ["#7f7f7f", "#bcbd22", "#aec7e8", "#ffbb78", "#98df8a"]


def color_for(s, idx):
    return SCHED_COLOR.get(s, _FALLBACK[idx % len(_FALLBACK)])


def cpu_model():
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.startswith("model name"):
                return ln.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def schedulers_at(vals, gauges, cores, exclude):
    found = set()
    for g in gauges:
        for (s, c) in vals.get(g, {}):
            if c == cores and not any(e in s for e in exclude):
                found.add(s)
    return sorted(found, key=sched_order)


# STYLE: vertical grouped bars, one metric across core counts (dark theme).
def render_bars(path, args):
    gauge, ylabel, title = METRICS[args.metric]
    vals, meta = parse_prom(path, {gauge})
    cell = vals.get(gauge, {})
    if not cell:
        log_error(f"metric '{args.metric}' ({gauge}) not present in {path.name}")
        return None
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    scheds = sorted({s for (s, _) in cell if not any(e in s for e in exclude)},
                    key=sched_order)
    cores = sorted({c for (_, c) in cell})

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(11, 6), dpi=140)
    bg = "#11111b"
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    n = max(len(scheds), 1)
    width = 0.82 / n
    x = np.arange(len(cores))
    for i, s in enumerate(scheds):
        ys = [cell.get((s, c), np.nan) for c in cores]
        ec, lw = ("white", 0.9) if is_pande(s) else ("none", 0)
        ax.bar(x + i * width - 0.41 + width / 2, ys, width, label=short_name(s),
               color=color_for(s, i), edgecolor=ec, linewidth=lw, zorder=3)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{c}C" for c in cores])
    ax.set_ylabel(ylabel, color="#cdd6f4")
    ax.set_xlabel("core count", color="#cdd6f4")
    if args.log:
        ax.set_yscale("log")
    ax.grid(axis="y", color="#313244", linewidth=0.6, zorder=0)
    ax.tick_params(colors="#cdd6f4")
    for spine in ax.spines.values():
        spine.set_color("#313244")
    ax.set_title(f"PANDEMONIUM v{meta['version']} — {title}   (lower is better)",
                 fontsize=14, fontweight="bold", color="#cdd6f4")
    cpu = cpu_model()
    cap = f"{path.name}  ·  N={meta['iterations']} iter" + (f"  ·  {cpu}" if cpu else "")
    fig.text(0.5, 0.012, cap, ha="center", color="#6c7086", fontsize=8)
    ax.legend(facecolor="#181825", edgecolor="#313244", labelcolor="#cdd6f4",
              fontsize=9, ncol=2, loc="best")
    fig.tight_layout(rect=[0, 0.04, 1, 1])
    out = args.out or (f"prism-{args.metric}-{meta['version']}.png")
    return fig, out, f"{len(scheds)} schedulers, cores={cores}"


# STYLE: horizontal grouped bars, all workloads at one core count (CachyOS format).
def render_cachyos(path, args):
    gauges = {g for _, g in CACHYOS_WORKLOADS}
    vals, meta = parse_prom(path, gauges)
    all_cores = sorted({c for g in gauges for (_, c) in vals.get(g, {})})
    if not all_cores:
        log_error("no workload data found")
        return None
    cores = args.cores if args.cores else all_cores[-1]
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    scheds = schedulers_at(vals, gauges, cores, exclude)
    if not scheds:
        log_error(f"no schedulers at {cores}C")
        return None

    labels = [lbl for lbl, _ in CACHYOS_WORKLOADS]
    data = {(lbl, s): vals.get(g, {}).get((s, cores), np.nan)
            for (lbl, g) in CACHYOS_WORKLOADS for s in scheds}

    plt.style.use("default")
    nrows, nsched = len(labels), len(scheds)
    fig, ax = plt.subplots(figsize=(12, max(7.5, nrows * nsched * 0.30)), dpi=140)
    y = np.arange(nrows)[::-1]          # first workload on top
    h = 0.80 / nsched
    for j, s in enumerate(scheds):
        xs = [data[(lbl, s)] for lbl in labels]
        off = (j - (nsched - 1) / 2) * h
        ec, lw = ("black", 0.7) if is_pande(s) else ("none", 0)
        ax.barh(y + off, xs, height=h, label=short_name(s),
                color=color_for(s, j), edgecolor=ec, linewidth=lw, zorder=3)
        for yi, xv in zip(y + off, xs):
            if not np.isnan(xv):
                ax.text(xv, yi, f" {xv:.0f}", va="center", ha="left", fontsize=7)
    ax.set_yticks(np.arange(nrows)[::-1])
    ax.set_yticklabels(labels)
    ax.set_xlabel("P99 latency (us).  Less is better")
    if args.log:
        ax.set_xscale("log")
    ax.grid(axis="x", color="#cccccc", linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    cpu = cpu_model()
    ax.set_title(f"PANDEMONIUM v{meta['version']} — Scheduler Latency Comparison "
                 f"({cores}C, all-core)\n"
                 f"{cpu}  ·  N={meta['iterations']} iter  ·  {path.name}",
                 fontsize=12)
    ax.legend(loc="lower right", fontsize=9)
    fig.tight_layout()
    out = args.out or (f"prism-cachyos-{cores}C-{meta['version']}.png")
    return fig, out, f"{nsched} schedulers at {cores}C, {nrows} workloads"


def parse_cachyos_suite(path):
    # prism-cachyos suite .prom: pandemonium_bench_cachyos_seconds{scheduler,
    # workload,stat="mean"|"stdev"}. Returns {scheduler: {workload: mean_s}}, meta.
    data, meta = {}, {"version": "?"}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m or m.group(1) != "pandemonium_bench_cachyos_seconds":
            continue
        labels = dict(_LABEL_RE.findall(m.group(2) or ""))
        if labels.get("stat") != "mean":
            continue
        sched, wl = labels.get("scheduler"), labels.get("workload")
        if not sched or not wl:
            continue
        try:
            data.setdefault(sched, {})[wl] = float(m.group(3))
        except ValueError:
            continue
        meta["version"] = labels.get("version", meta["version"])
    return data, meta


# STYLE: prism-cachyos suite total wall-time, one bar per scheduler, fastest on
# top, PANDEMONIUM outlined. The full-field "dominance" chart -- lower is better.
def render_cachyos_suite(path, args):
    data, meta = parse_cachyos_suite(path)
    if not data:
        log_error("no prism-cachyos suite data (expected pandemonium_cachyos_seconds)")
        return None
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    nwl = max((len(w) for w in data.values()), default=0)
    totals = {}
    for s, wls in data.items():
        if any(e in s for e in exclude):
            continue
        if len(wls) < nwl:   # only schedulers that ran the full set get a fair total
            log_error(f"{s}: {len(wls)}/{nwl} workloads -- excluded from total")
            continue
        totals[s] = sum(wls.values())
    if not totals:
        log_error("no scheduler ran the full workload set")
        return None
    scheds = sorted(totals, key=lambda s: totals[s])   # fastest first

    plt.style.use("default")
    n = len(scheds)
    fig, ax = plt.subplots(figsize=(11, max(4.5, n * 0.55)), dpi=140)
    y = np.arange(n)[::-1]
    fastest = totals[scheds[0]]
    for i, s in enumerate(scheds):
        ec, lw = ("black", 1.1) if is_pande(s) else ("none", 0)
        ax.barh(y[i], totals[s], height=0.72, color=color_for(s, i),
                edgecolor=ec, linewidth=lw, zorder=3)
        mult = "" if i == 0 else f"  ({totals[s] / fastest:.2f}x)"
        ax.text(totals[s], y[i], f" {totals[s]:.1f}s{mult}",
                va="center", ha="left", fontsize=8)
    ax.set_yticks(y)
    labels = []
    for s in scheds:
        v = sched_version(s, meta)
        labels.append(f"{short_name(s)}  {v}" if v else short_name(s))
    ax.set_yticklabels(labels)
    ax.set_xlabel(f"Total wall-time across {nwl} workloads (s).  Lower is better")
    if args.log:
        ax.set_xscale("log")
    ax.grid(axis="x", color="#cccccc", linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.margins(x=0.14)
    cpu = cpu_model()
    ax.set_title(f"scx_pandemonium v{meta['version']} — CachyOS Suite Total Wall-Time\n"
                 f"{cpu}  ·  {nwl} workloads  ·  {os.uname().release}  ·  {path.name}",
                 fontsize=12)
    fig.tight_layout()
    out = args.out or f"prism-cachyos-suite-{meta['version']}.png"
    return fig, out, f"{n} schedulers, {nwl} workloads"


def main():
    ap = argparse.ArgumentParser(description="Render a prism-scale .prom into a PNG.")
    ap.add_argument("file", nargs="?", help="explicit .prom (default: newest in cache)")
    ap.add_argument("--style", default="bars",
                    choices=["bars", "cachyos", "cachyos-suite"],
                    help="cachyos-suite: prism-cachyos total wall-time per scheduler "
                         "(the full-field dominance chart)")
    ap.add_argument("--metric", default="latency_p99", choices=list(METRICS),
                    help="(bars style only)")
    ap.add_argument("--cores", type=int, default=0,
                    help="(cachyos style) core count to chart; default = max present")
    ap.add_argument("--exclude", default="", help="comma list of schedulers to drop")
    ap.add_argument("--log", action="store_true", help="log-scale value axis")
    ap.add_argument("--out", default="", help="output PNG (default: repo root)")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    args = ap.parse_args()

    if args.file:
        path = Path(args.file)
    elif args.style == "cachyos-suite":
        # newest prism-cachyos suite .prom (not version-prefixed like prism-scale)
        cands = sorted(Path(args.cache_dir).glob("prism-cachyos-*.prom"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            log_error("no prism-cachyos .prom found")
            return 1
        path = cands[0]
    else:
        cands = [p for p in sorted(Path(args.cache_dir).glob("*.prom"),
                                   key=lambda p: p.stat().st_mtime, reverse=True)
                 if not p.name.startswith("analysis-")
                 and re.match(r"\d+\.\d+\.\d+-", p.name)]
        if not cands:
            log_error("no prism-scale .prom found")
            return 1
        path = cands[0]

    if args.style == "cachyos-suite":
        result = render_cachyos_suite(path, args)
    elif args.style == "cachyos":
        result = render_cachyos(path, args)
    else:
        result = render_bars(path, args)
    if result is None:
        return 1
    fig, out_name, note = result
    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / out_name
    fig.savefig(out, facecolor=fig.get_facecolor())
    log_info(f"wrote {out}  ({note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
