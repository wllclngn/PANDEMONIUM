#!/usr/bin/env python3
"""
PANDEMONIUM prism-fork-thread: scheduler IPC throughput + hot-path profiling.

Cycles through EEVDF, PANDEMONIUM (BPF), PANDEMONIUM (ADAPTIVE), and
scx_bpfland, running `perf bench sched messaging -t -g 24 -l 6000` under
perf stat to capture both elapsed time and hardware counters.

Usage:
    ./pandemonium.py prism-fork-thread
"""

import multiprocessing
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    LOG_DIR, ARCHIVE_DIR, BINARY,
    get_version, get_git_info,
    log, log_info, log_warn, log_error,
    is_scx_active, scx_scheduler_name,
    wait_for_deactivation,
    montauk_available, MONTAUK_LOG_INTERVAL_MS, MONTAUK, montauk_analyze_argv,
    table_header, table_row, LABEL_W, PrometheusBuilder,
    mean_stdev, median, get_online_cpus,
)

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler
trace_workload = _tests.trace_workload

NUM_GROUPS = 24
NR_LOOPS_FULL = 6000
NR_LOOPS_QUICK = 1000
NR_LOOPS = NR_LOOPS_FULL  # SET BY main() BASED ON --quick FLAG

# PHASE 1 -- short traced burst. A brief montauk capture per arm for the two
# scheduling-quality axes the perf-stat timing run is blind to: wake2run latency
# and placement locality (cross-domain scatter). Short on purpose -- like a
# prism profile, the burst answers "how late / how scattered" fast; the
# full iteration below answers "how cheap". Fused, they read latency | cause |
# cost for one workload in one report.
BURST_LOOPS = 1500               # short messaging burst for the traced phase
BURST_WINDOW = 12.0              # hard cap on each traced burst (s)
ANALYZE = montauk_analyze_argv(shutil.which("montauk") or MONTAUK)

# WORKLOAD SWEEP -- a cell is one shape of `perf bench sched messaging`: a mode
# (thread = shared address space; process = separate, the actual fork arm) and a
# group count (pressure width). The bench reads every cell on all three axes, so
# "fork-thread" finally has a fork and a curve, not one threaded point.
from collections import namedtuple
Cell = namedtuple("Cell", "label threaded groups")
DEFAULT_MODES = "thread,process"   # default cell set: the fork/thread split
DEFAULT_GROUPS = "24"              # single group count by default; sweep with --groups


def build_cells(modes, groups):
    cells = []
    for g in groups:
        for mode in modes:
            threaded = (mode == "thread")
            tag = "thread" if threaded else "proc"
            cells.append(Cell(f"{tag}/g{g}", threaded, g))
    return cells

PERF_EVENTS = [
    "cycles",
    "instructions",
    "cache-misses",
    "cache-references",
    "context-switches",
    "cpu-migrations",
    "branch-misses",
    "task-clock",
]


def _raise_fd_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = max(soft, 4096)
    if target > hard:
        target = hard
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))


def _messaging_cmd(groups, threaded, loops):
    # `perf bench sched messaging` -t = threads sharing one address space (locality
    # matters); without -t = processes, separate address spaces (the fork arm, where
    # placement scatter costs nothing because there is no shared working set). -g is
    # the group count (pressure width). One builder so the timing run and the traced
    # burst issue the IDENTICAL workload per cell.
    cmd = ["perf", "bench", "-f", "simple", "sched", "messaging"]
    if threaded:
        cmd.append("-t")
    cmd += ["-g", str(groups), "-l", str(loops)]
    return cmd


def run_perf_bench(groups=NUM_GROUPS, threaded=True, loops=None):
    """Run perf stat wrapping perf bench. Returns (elapsed_s, counters) or (None, None)."""
    nloops = loops if loops is not None else NR_LOOPS
    events = ",".join(PERF_EVENTS)
    cmd = ["perf", "stat", "-e", events, "--",
           *_messaging_cmd(groups, threaded, nloops)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    elapsed = None
    try:
        elapsed = float(result.stdout.strip())
    except (ValueError, AttributeError):
        log_error(f"Could not parse perf bench output: {result.stdout.strip()}")
        return None, None

    counters = {}
    for line in result.stderr.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Performance"):
            continue
        m = re.match(r'^([\d,\.]+)\s+(?:msec\s+)?(\S+)', line)
        if m:
            val_str = m.group(1).replace(",", "")
            name = m.group(2).rstrip(":u").rstrip(":k")
            try:
                counters[name] = float(val_str)
            except ValueError:
                pass

    return elapsed, counters


def _fmt_count(val):
    if val >= 1_000_000_000:
        return f"{val/1e9:.2f}G"
    elif val >= 1_000_000:
        return f"{val/1e6:.2f}M"
    elif val >= 1_000:
        return f"{val/1e3:.2f}K"
    return f"{val:.0f}"


def _spread_stats(xs):
    # Per-iteration spread for an --iterations run. The median stays the headline
    # (continuity with the historical archives); the spread is what separates a
    # real regression from run-to-run noise. None if there are no samples.
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    n = len(xs)
    m, sd = mean_stdev(xs)
    return {
        "n": n,
        "mean": m,
        "stddev": sd,
        "min": min(xs),
        "max": max(xs),
        "median": median(xs),
    }


def write_prometheus(version, git, stamp, ncpus, cells_data, loops):
    pb = PrometheusBuilder("fork_thread")
    pb.info(ts=int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()),
            version=version, git_commit=git["commit"], git_dirty=git["dirty"])
    pb.gauge("cpus", ncpus, help="CPUs available")
    pb.gauge("loops", loops, help="loops per sender per receiver")

    # One cell = one workload shape (mode x groups). Every series carries mode and
    # groups labels, so the archive holds the whole sweep -- a population run can
    # read the curve (where the bet flips), not just one point.
    for cell, all_results, all_spreads, trace_results in cells_data:
        wl = {"mode": "thread" if cell.threaded else "process",
              "groups": str(cell.groups)}

        for sched_name, (elapsed, counters) in all_results.items():
            sl = dict(wl, scheduler=sched_name)
            if elapsed is None:
                pb.gauge("seconds", "-1", help="elapsed time", labels=sl)
                continue
            pb.gauge("seconds", f"{elapsed:.4f}", help="elapsed time", labels=sl)
            if counters:
                for cn, cv in counters.items():
                    safe = cn.replace("-", "_").replace(".", "_")
                    pb.gauge(safe, f"{cv:.0f}", help=f"perf stat {cn}", labels=sl)
                cyc = counters.get("cycles", 0)
                ins = counters.get("instructions", 0)
                cm = counters.get("cache-misses", 0)
                cr = counters.get("cache-references", 0)
                if cyc > 0 and ins > 0:
                    pb.gauge("ipc", f"{ins / cyc:.3f}",
                             help="instructions per cycle", labels=sl)
                if cr > 0:
                    pb.gauge("cache_miss_rate", f"{cm / cr:.6f}",
                             help="cache miss rate", labels=sl)
            sp = (all_spreads or {}).get(sched_name)
            cs = sp.get("cache_miss_rate") if sp else None
            if cs:
                for k in ("mean", "stddev", "min", "max"):
                    pb.gauge(f"cache_miss_rate_{k}", f"{cs[k]:.6f}",
                             help=f"cache miss rate {k} across iterations", labels=sl)
                pb.gauge("cache_miss_rate_n", cs["n"],
                         help="iterations contributing to the cache miss rate", labels=sl)

        # PHASE 1 readings (latency + cause) beside the cost counters.
        for sched_name, tr in (trace_results or {}).items():
            if not tr:
                continue
            sl = dict(wl, scheduler=sched_name)
            if tr.get("p99_us") is not None:
                pb.gauge("burst_wake2run_p99_us", f"{tr['p99_us']}",
                         help="traced-burst wake2run p99 latency (us)", labels=sl)
            if tr.get("cross_domain_pct") is not None:
                pb.gauge("burst_cross_domain_pct", f"{tr['cross_domain_pct']:.4f}",
                         help="traced-burst cross-domain placement scatter (%)", labels=sl)
            loc = tr.get("locality") or {}
            if loc.get("migrations") is not None:
                pb.gauge("burst_migrations", f"{loc['migrations']}",
                         help="traced-burst cross-CPU migrations", labels=sl)
            for tier, mv in (loc.get("tiers") or {}).items():
                pb.gauge("burst_locality_tier_pct", f"{mv[1]:.4f}",
                         help="traced-burst migrations at this cache-tier distance (%)",
                         labels=dict(sl, tier=tier))

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"prism-fork-thread-{version}-{stamp}.prom"
    path.write_text(pb.render())
    return path


def _cell_block(report, cell, loops, all_results, all_spreads, trace_results):
    """One workload cell's section: header, the fused TRADEOFF table (latency |
    cause | cost), the raw COUNTER table (where 'instructions identical, cycles up'
    shows), and the LOCALITY tiers when a multi-domain part populates them."""
    mode = ("thread -- shared address space, locality matters"
            if cell.threaded
            else "process -- separate address spaces, the fork arm (no shared "
                 "working set, so scatter is free)")
    cmd_str = ("perf bench sched messaging "
               + ("-t " if cell.threaded else "")
               + f"-g {cell.groups} -l {loops}")
    report.append(f"WORKLOAD {cell.label}  [{mode}]")
    report.append(f"  {cmd_str}")
    report.append("")

    eevdf_elapsed = all_results.get("EEVDF", (None, None))[0]
    has_trace = bool(trace_results)

    def _miss_ipc(sn):
        _, counters = all_results.get(sn, (None, None))
        if not counters:
            return "N/A", "N/A"
        cm, cr = counters.get("cache-misses"), counters.get("cache-references")
        cyc, ins = counters.get("cycles"), counters.get("instructions")
        miss = f"{cm / cr * 100:.2f}%" if cm and cr else "N/A"
        ipc = f"{ins / cyc:.3f}" if cyc and ins else "N/A"
        # ±stddev when --iterations gave a spread, so signal/noise is in the table
        sp = (all_spreads or {}).get(sn)
        cs = sp.get("cache_miss_rate") if sp else None
        if cs and cs.get("n", 1) > 1 and cm and cr:
            miss = f"{cm / cr * 100:.2f}±{cs['stddev'] * 100:.2f}%"
        return miss, ipc

    def _time_vs(sched_name, elapsed):
        if elapsed is None:
            return "FAILED", ""
        if sched_name == "EEVDF":
            return f"{elapsed:.3f}s", "baseline"
        if eevdf_elapsed and eevdf_elapsed > 0:
            d = (elapsed - eevdf_elapsed) / eevdf_elapsed * 100
            return f"{elapsed:.3f}s", f"{'+' if d > 0 else ''}{d:.1f}%"
        return f"{elapsed:.3f}s", ""

    if has_trace:
        report.append("TRADEOFF  (latency | cause | cost -- lower time/miss%, higher IPC is better)")
        report.append(table_header("SCHEDULER",
                      ["WAKE2RUN p99", "CROSS-DOM", "TIME", "VS EEVDF",
                       "CACHE MISS%", "IPC"]))
    else:
        report.append("COST  (no montauk -- timing + counters only)")
        report.append(table_header("SCHEDULER",
                      ["TIME", "VS EEVDF", "CACHE MISS%", "IPC"]))
    for sched_name, (elapsed, _) in all_results.items():
        ts, vs = _time_vs(sched_name, elapsed)
        miss, ipc = _miss_ipc(sched_name)
        if has_trace:
            tr = (trace_results or {}).get(sched_name) or {}
            p99 = tr.get("p99_us")
            xd = tr.get("cross_domain_pct")
            p99s = f"{p99}us" if p99 is not None else "N/A"
            xds = f"{xd:.1f}%" if xd is not None else "N/A"
            report.append(table_row(sched_name, [p99s, xds, ts, vs, miss, ipc]))
        else:
            report.append(table_row(sched_name, [ts, vs, miss, ipc]))
    report.append("")

    # RAW COUNTERS -- instructions/cycles/misses/branches per arm. The diagnostic
    # gold: identical instructions with higher cycles means the cost is pure
    # locality, not extra work.
    counter_names = ["cycles", "instructions", "cache-misses", "cache-references",
                     "cpu-migrations", "branch-misses"]
    sched_names = list(all_results.keys())
    header = f"{'COUNTER':<20}"
    for sn in sched_names:
        header += f" {sn:>18}"
    report.append(header)
    for cn in counter_names:
        row = f"{cn:<20}"
        for sn in sched_names:
            _, counters = all_results[sn]
            cell_val = _fmt_count(counters[cn]) if counters and cn in counters else "N/A"
            row += f" {cell_val:>18}"
        report.append(row)
    report.append("")

    if has_trace:
        tiers_present = any((tr or {}).get("locality", {}).get("tiers")
                            for tr in trace_results.values())
        if tiers_present:
            report.append("LOCALITY  (traced-burst migrations by cache-tier distance)")
            report.append(table_header("SCHEDULER",
                          ["MIGRATIONS", *[t.upper() for t in _LOC_TIERS]]))
            for sched_name in all_results:
                loc = (trace_results.get(sched_name) or {}).get("locality") or {}
                mig = loc.get("migrations")
                cells = [str(mig) if mig is not None else "N/A"]
                for t in _LOC_TIERS:
                    tv = loc.get("tiers", {}).get(t)
                    cells.append(f"{tv[1]:.1f}%" if tv else "-")
                report.append(table_row(sched_name, cells))
            report.append("")


def write_report(version, git, stamp, ncpus, cells_data, loops):
    report = []
    report.append(f"prism-fork-thread v{version} [{git['commit']}]")
    report.append(f"cpus: {ncpus}  cells: {len(cells_data)}  loops: {loops}")
    if any(td[3] for td in cells_data):
        report.append(f"burst: -l {BURST_LOOPS} traced under montauk "
                      "(wake2run latency + placement locality)")
    if len(cells_data) > 1:
        report.append("sweep: read DOWN a column for the tradeoff, ACROSS cells "
                      "for where the bet flips (thread<->process, group pressure)")
    report.append("")

    for cell, all_results, all_spreads, trace_results in cells_data:
        _cell_block(report, cell, loops, all_results, all_spreads, trace_results)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-fork-thread-{stamp}.log"
    path.write_text("\n".join(report) + "\n")
    return path


# ---- montauk eBPF trace capture (--trace) ----
# Record a per-thread dispatch flight-recording of the storm under montauk (the
# shared MontaukTrace in pandemonium_common). montauk comes up first (incl. a
# quiet idle baseline) so the storm onset is captured, not missed.

TRACE_PATTERN = "perf"            # perf bench tree; children/threads auto-tracked
BASELINE_SECONDS = 3.0            # quiet idle recorded before the storm, for contrast
LOAD_SAFETY_TIMEOUT = 180.0       # hard cap so a wedged scheduler can't hang the run


def _run_load_traced(timeout, groups=NUM_GROUPS, threaded=True, loops=None,
                     avoid_cpu=None):
    """avoid_cpu: keep the storm OFF montauk's drain core.

    Pinning montauk was only half the isolation. The storm saturates every CPU,
    so a montauk pinned to cpuN still contends with the storm ON cpuN -- it can
    no longer migrate, which is worse than nothing under a load that fills the
    machine. Measured on a 20-core box with the pin alone, the arms still came
    back 8.6 / 15.0 / 15.4% complete, a 1.8x spread that the report then compared
    head to head. The drain core has to be EXCLUSIVE, which means constraining
    the load, not just the tracer."""
    nloops = loops if loops is not None else NR_LOOPS
    cmd = _messaging_cmd(groups, threaded, nloops)
    if avoid_cpu is not None:
        online = get_online_cpus()
        others = [c for c in range(online) if c != avoid_cpu]
        if others:
            cmd = ["taskset", "-c", ",".join(map(str, others))] + cmd
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return time.time() - t0


def trace_capture(sched_name, cmd, stamp, duration,
                  groups=NUM_GROUPS, threaded=True, loops=None):
    load_timeout = duration if duration > 0 else LOAD_SAFETY_TIMEOUT
    # PIN THE DRAIN CORE. The messaging storm saturates every CPU, and an
    # unpinned montauk gets scheduled against its own subject: it stops draining
    # the ring and sheds events exactly in the windows worth measuring. Measured
    # unpinned: 19.1M events dropped, 5.70% capture completeness, and a per-arm
    # completeness spread (5.70 / 8.24 / 8.44) that biased the arms unequally
    # against each other. Every other prism stage already pins.
    drain = max(0, get_online_cpus() - 1)

    def body(rec_dir):
        log_info(f"[{sched_name}] running storm (montauk alone on cpu{drain}, "
                 f"storm on the rest, window {load_timeout:.0f}s)...")
        elapsed = _run_load_traced(load_timeout, groups, threaded, loops,
                                   avoid_cpu=drain)
        if elapsed is not None:
            log_info(f"[{sched_name}] load completed in {elapsed:.3f}s")
        else:
            log_info(f"[{sched_name}] capture window "
                     f"({load_timeout:.0f}s) elapsed -- load cut")
        return elapsed

    rec_dir, _ = trace_workload(sched_name, cmd, TRACE_PATTERN, "fork-thread",
                                stamp, body, baseline_s=BASELINE_SECONDS,
                                events=True, pin_cpu=drain)
    if rec_dir is not None:
        log_info(f"[{sched_name}] recording: {rec_dir}")
    return rec_dir


def run_trace(args):
    if os.geteuid() != 0:
        # SELF-ELEVATE: the trace flow needs root end-to-end (montauk eBPF attach
        # + sched_ext load), so re-exec under sudo rather than make the user type
        # it -- matches how every other command elevates its own privileged steps.
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    if not montauk_available():
        log_error(f"montauk not found at {MONTAUK}")
        return 1

    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if not log.child:
        log_info(f"prism-fork-thread --trace v{ver} [{git['commit']}]  "
                 f"trace='{TRACE_PATTERN}'  interval={MONTAUK_LOG_INTERVAL_MS}ms  "
                 f"load=perf bench sched messaging -t -g {NUM_GROUPS} -l {NR_LOOPS}")
        log.blank()

    if is_scx_active():
        log_warn(f"sched_ext active ({scx_scheduler_name()}) -- stopping pandemonium")
        _tests.stop_systemd_scheduler()
        wait_for_deactivation(5.0)
    time.sleep(1)

    # Field: EEVDF baseline + PANDEMONIUM (BPF + ADAPTIVE) by default. --all-scx
    # adds every installed scx; --schedulers L runs EEVDF vs EXACTLY L (PANDEMONIUM
    # only if named) -- matching field_arms / prism-cachyos. --pandemonium-only
    # drops EEVDF and externals. EEVDF rides along whenever a field is requested.
    _sched = getattr(args, "schedulers", "") or ""
    _all = getattr(args, "all_scx", False)
    _field_only = bool(_sched) and not _all and not args.pandemonium_only
    _named = {s.strip().lower() for s in _sched.split(",")} if _sched else set()
    entries: list[tuple[str, list[str] | None]] = []
    if not args.pandemonium_only and (args.compare_eevdf or _sched or _all):
        entries.append(("EEVDF", None))
    if (not _field_only) or (_named & {"pandemonium", "scx_pandemonium"}):
        entries.append(("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]))
        entries.append(("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]))
    if not args.pandemonium_only:
        ext = _tests.SCX_FIELD if _all else [s.strip() for s in _sched.split(",")]
        for e in ext:
            if not e or e.lower() in ("pandemonium", "scx_pandemonium", "eevdf"):
                continue
            if find_scheduler(e):
                entries.append((e, [e]))
            else:
                log_warn(f"  external scheduler {e} not found in PATH, skipping")

    recs = {}
    try:
        for name, cmd in entries:
            recs[name] = trace_capture(name, cmd, stamp, args.duration)
            time.sleep(2)
            print()
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    print()
    log_info("Recordings (inspect montauk_trace_thread_* over montauk_scrape_timestamp_ms):")
    for n, r in recs.items():
        log_info(f"  {n}: {r}")
    return 0


# ---- phase 1: short traced burst -> wake2run latency + placement locality ----
# Reuses `montauk --analyze` the same way prism does: --digest carries the
# wake2run p99 and the cross-domain scatter on one line; --report locality breaks
# the migrations into cache-tier distances. The perf-stat timing run (phase 2)
# cannot see either -- it only measures cost. Phase 1 supplies latency and cause.

_LOC_TIERS = ("same-L2", "same-L3", "same-socket", "cross-socket")


def _json_out(argv):
    """Parsed --json envelope from a `montauk --analyze` invocation, or None."""
    import json as _json
    r = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return _json.loads(r.stdout)
    except ValueError:
        log_warn(f"unparseable envelope from {' '.join(argv[1:3])}")
        return None


def _locality_from_env(env):
    """Locality tiers out of the locality report's gauges. tier_moves carries one
    gauge per cache-tier distance; the mapped-migration total is their sum, and
    the tier percentages are shares of it. No cache_topology in the recording
    means no tier gauges, which reads back as the empty result the report renders
    as N/A."""
    out = {"migrations": None, "tiers": {}}
    if not env:
        return out
    rep = next((r for r in (env.get("reports") or [])
                if r.get("name") == "locality"), {})
    moves = {}
    for g in rep.get("gauges") or []:
        if g.get("name") != "montauk_analysis_locality_tier_moves":
            continue
        m = re.search(r'tier="([^"]+)"', g.get("labels", "") or "")
        if m:
            moves[m.group(1)] = g.get("value", 0)
    if not moves:
        return out
    total = sum(moves.values())
    out["migrations"] = int(total)
    # Gauge labels are underscored (same_l2); the report's column headings are
    # the hyphenated tier names.
    for tier in _LOC_TIERS:
        v = moves.get(tier.replace("-", "_").lower())
        if v is not None:
            out["tiers"][tier] = (int(v), (100.0 * v / total) if total else 0.0)
    return out


def analyze_trace(rec_dir):
    """Phase-1 readings from one montauk recording: wake2run p99 (latency) and
    placement locality / cross-domain scatter (cause). {} on missing recording or
    analyzer -- the report then renders those columns as N/A and keeps the cost
    side intact.

    Both readings come from montauk's --json envelopes. The regexes that used to
    pull them back out of the rendered text are gone: the digest publishes
    wake2run as typed quantiles and locality publishes per-tier gauges, so
    scraping the prose was re-deriving what the tool already states."""
    out = {}
    if rec_dir is None or not Path(rec_dir).exists():
        return out
    try:
        dig = _json_out([*ANALYZE, str(rec_dir), "--digest", "--json"])
        if dig:
            sched = next((r for r in (dig.get("reports") or [])
                          if r.get("name") == "sched"), {})
            w = sched.get("wake2run") or {}
            if w.get("p99_us") is not None:
                out["p99_us"] = int(round(w["p99_us"]))
            if w.get("crossdomain_pct") is not None:
                out["cross_domain_pct"] = float(w["crossdomain_pct"])
        # --report reads the EVENT STREAM, not the recording dir. This used to
        # pass the dir, which montauk rejects with "short read on header", so the
        # locality columns had never once been populated -- they rendered N/A on
        # every run. The stream sits beside the dir as <dir>.events, or inside it
        # as events.bin for a freeze-archive layout; try both, the way the digest
        # does.
        events = Path(str(rec_dir) + ".events")
        if not events.is_file():
            events = Path(rec_dir) / "events.bin"
        if events.is_file():
            out["locality"] = _locality_from_env(
                _json_out([*ANALYZE, str(events), "--report", "locality",
                           "--json"]))
    except (subprocess.TimeoutExpired, OSError) as e:
        log_warn(f"montauk --analyze failed on {rec_dir}: {e}")
    return out


def run_burst_phase(entries, stamp, cell):
    """PHASE 1 for one workload cell -- the short traced burst per arm. For each
    scheduler, capture a brief montauk recording of THIS cell's storm (mode +
    groups) at BURST_LOOPS and fold it to {p99_us, cross_domain_pct, locality}.
    The full perf-stat iteration of the same cell runs after. Returns
    {sched_name: readings}."""
    log_info(f"PHASE 1 [{cell.label}]: traced burst (-l {BURST_LOOPS}, "
             f"<= {BURST_WINDOW:.0f}s/arm, montauk) -- wake2run + locality")
    print()
    trace_results = {}
    for name, cmd in entries:
        rec_dir = trace_capture(name, cmd, stamp, BURST_WINDOW,
                                groups=cell.groups, threaded=cell.threaded,
                                loops=BURST_LOOPS)
        readings = analyze_trace(rec_dir)
        trace_results[name] = readings
        if readings:
            p99 = readings.get("p99_us")
            xd = readings.get("cross_domain_pct")
            log_info(f"[{name}] burst: wake2run p99="
                     f"{(str(p99) + 'us') if p99 is not None else 'N/A'}  "
                     f"cross-domain={(f'{xd:.1f}%') if xd is not None else 'N/A'}")
        time.sleep(2)
        print()
    return trace_results


def _measure(sched_name, cell, iterations):
    """PHASE 2 for one (scheduler, cell): run the full perf-stat timing N times,
    return ((median_elapsed, median_counters), spreads) or ((None, None), None).
    The median is robust to a poisoned sched_ext iteration; the per-iteration
    cache-miss spread is kept so a delta can be judged against the noise."""
    samples = []
    for it in range(iterations):
        el, ct = run_perf_bench(cell.groups, cell.threaded)
        if el is not None:
            samples.append((el, ct))
            msg = f"[{sched_name}] {cell.label} iter {it + 1}/{iterations}: {el:.3f}s"
            if ct and ct.get("cache-misses"):
                msg += f"  cache-misses={_fmt_count(ct['cache-misses'])}"
            log_info(msg)
        else:
            log_error(f"[{sched_name}] {cell.label} iter {it + 1}/{iterations} "
                      "perf bench failed")
    if not samples:
        log_error(f"[{sched_name}] {cell.label}: all {iterations} iterations failed")
        return (None, None), None

    elapsed = median([s[0] for s in samples])
    counters, keys = {}, set()
    for _, ct in samples:
        if ct:
            keys.update(ct.keys())
    for k in keys:
        vals = [ct[k] for _, ct in samples if ct and k in ct]
        if vals:
            counters[k] = median(vals)
    rates = [ct["cache-misses"] / ct["cache-references"]
             for _, ct in samples
             if ct and ct.get("cache-references", 0) > 0]
    spreads = {"cache_miss_rate": _spread_stats(rates),
               "seconds": _spread_stats([s[0] for s in samples])}
    cyc, ins = counters.get("cycles", 0), counters.get("instructions", 0)
    ipc = ins / cyc if cyc > 0 else 0
    cs = spreads["cache_miss_rate"]
    spread_msg = (f"  miss%={cs['mean'] * 100:.2f}±{cs['stddev'] * 100:.2f}"
                  if cs else "")
    log_info(f"[{sched_name}] {cell.label} MEDIAN {elapsed:.3f}s  IPC={ipc:.3f}  "
             f"cache-misses={_fmt_count(counters.get('cache-misses', 0))}  "
             f"(n={len(samples)}/{iterations}){spread_msg}")
    return (elapsed, counters), spreads


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Quick mode: 1000 loops instead of 6000")
    ap.add_argument("--no-burst", action="store_true",
                    help="Skip phase 1 (the short traced burst that captures "
                         "wake2run latency + placement locality) and run only the "
                         "full perf-stat cost iteration. Default runs both.")
    ap.add_argument("--modes", type=str, default=DEFAULT_MODES,
                    help="Comma list of workload modes to sweep: thread (shared "
                         "address space) and/or process (separate -- the fork arm). "
                         f"Default '{DEFAULT_MODES}' runs the fork/thread split.")
    ap.add_argument("--groups", type=str, default=DEFAULT_GROUPS,
                    help="Comma list of -g group counts (pressure width) to sweep, "
                         f"e.g. '2,8,24'. Default '{DEFAULT_GROUPS}'. modes x groups "
                         "is the cell grid; every cell is read on all three axes.")
    ap.add_argument("--trace", action="store_true",
                    help="Capture a montauk eBPF trace of the storm (per-thread "
                         "dispatch flight recording) instead of the timing run. Root.")
    ap.add_argument("--compare-eevdf", action="store_true",
                    help="With --trace: also record EEVDF as the clean reference")
    ap.add_argument("--duration", type=float, default=0,
                    help="With --trace: capture window seconds (0 = bench to "
                         "completion, hard-capped at 180s)")
    ap.add_argument("--iterations", type=int, default=1,
                    help="Run each scheduler N times. The MEDIAN is the headline "
                         "(robust to a poisoned outlier run); the per-iteration "
                         "cache-miss spread (mean/stddev/min/max/n) is recorded in "
                         "the report and archive so a delta can be judged against "
                         "the noise. Default 1.")
    ap.add_argument("--schedulers", type=str, default="",
                    help="With --trace: comma-separated external scx field "
                         "(EEVDF baseline always; PANDEMONIUM only if named). "
                         "Runs EEVDF vs exactly the named schedulers.")
    ap.add_argument("--all-scx", action="store_true",
                    help="Also run the full installed scx scheduler field "
                         "(scx_bpfland, scx_rusty, scx_lavd, scx_flow, "
                         "scx_rustland, scx_p2dq, scx_tickless, scx_cosmos, "
                         "scx_cake, scx_flash, scx_beerland, scx_layered). "
                         "Default: EEVDF + PANDEMONIUM (BPF + ADAPTIVE) only.")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="Run only PANDEMONIUM entries -- drop EEVDF and any "
                         "external scx schedulers from the field.")
    ap.add_argument("--phi-sweep", type=str, nargs="?", const="0", default=None,
                    metavar="VALUES",
                    help="Phi A/B: instead of the full scx field, run PANDEMONIUM "
                         "(BPF mode) across phi_dist_scale_q16 values (comma list; "
                         "0 = Phi off) plus the topology default and EEVDF. Bare "
                         "--phi-sweep tests {0, default}. Isolates Phi's marginal "
                         "effect on this CPU's CCX layout.")
    args = ap.parse_args()
    if args.iterations < 1:
        args.iterations = 1

    global NR_LOOPS
    NR_LOOPS = NR_LOOPS_QUICK if args.quick else NR_LOOPS_FULL

    if args.trace:
        return run_trace(args)

    # PHASE 1 gate: the traced burst (wake2run latency + placement locality) runs
    # by default, but not for --phi-sweep (a focused cost A/B), not under --no-burst,
    # and not without montauk. It needs root end-to-end (montauk eBPF attach +
    # sched_ext load), so self-elevate once up front -- matches --trace and the
    # RUN-BARE convention (the bench acquires its own root; the user never sudo's).
    run_burst = (not args.no_burst and args.phi_sweep is None
                 and montauk_available())
    if run_burst and os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    if not args.no_burst and args.phi_sweep is None and not montauk_available():
        log_warn("montauk not found -- skipping phase 1 (traced burst); running "
                 "cost-only. Install montauk for the wake2run + locality axes.")

    if not shutil.which("perf"):
        log_error("perf not found. Install with: sudo pacman -S perf")
        return 1

    _raise_fd_limit()

    ncpus = multiprocessing.cpu_count()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirty = " (dirty)" if git["dirty"] else ""
    mode = "QUICK" if args.quick else "FULL"

    log_info(f"prism-fork-thread v{ver} [{git['commit']}{dirty}] [{mode}]")
    log_info(f"CPUs: {ncpus}  loops: {NR_LOOPS}  perf stat + perf bench sched messaging")
    print()

    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping pandemonium service")
        _tests.stop_systemd_scheduler()
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext")
            return 1
    time.sleep(1)

    if args.phi_sweep is not None:
        # PHI A/B: hold everything constant, vary only phi_dist_scale_q16 via the
        # scheduler's --phi-scale override. BPF mode (matches the established BPF
        # anchor; no adaptive loop to perturb). EEVDF for the VS-EEVDF column, the
        # topology default (no override), then one run per requested value (0 = off).
        # Single-shape cost A/B -- the threaded default cell, no sweep.
        vals = [v.strip() for v in args.phi_sweep.split(",") if v.strip() != ""]
        entries = [
            ("EEVDF", None),
            ("PANDEMONIUM (phi=default)", [str(BINARY), "--no-adaptive"]),
        ]
        for v in vals:
            entries.append(
                (f"PANDEMONIUM (phi={v})", [str(BINARY), "--no-adaptive", "--phi-scale", v])
            )
        log_info(f"PHI SWEEP: topology default + values {vals} (BPF mode)")
        cells = [Cell(f"thread/g{NUM_GROUPS}", True, NUM_GROUPS)]
    else:
        entries = [
            ("EEVDF", None),
            ("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]),
            ("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]),
        ]

        # FULL FIELD: every installed production scx scheduler in the ring against
        # EEVDF and PANDEMONIUM. Gated behind --all-scx (opt-in) to keep the default
        # run fast -- the EEVDF + PANDEMONIUM (BPF + ADAPTIVE) trio is what we iterate
        # on most. scx_chaos is excluded (fault-injection test scheduler, not a
        # contender). scx_layered needs a layer spec and may self-skip without one.
        if args.all_scx:
            scx_field = [
                "scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
                "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
                "scx_beerland", "scx_layered",
            ]
            for s in scx_field:
                if find_scheduler(s):
                    entries.append((s, [s]))
                else:
                    log_warn(f"{s} not found, skipping")

        # WORKLOAD CELLS: modes x groups. The default fork/thread split, sweepable.
        modes = [m.strip() for m in args.modes.split(",")
                 if m.strip() in ("thread", "process")]
        if not modes:
            log_warn(f"--modes '{args.modes}' has no valid mode -- using thread")
            modes = ["thread"]
        try:
            groups = [int(g) for g in args.groups.split(",") if g.strip()]
        except ValueError:
            log_warn(f"--groups '{args.groups}' not all integers -- using {NUM_GROUPS}")
            groups = [NUM_GROUPS]
        cells = build_cells(modes, groups or [NUM_GROUPS])

    if args.pandemonium_only:
        entries = [(n, c) for n, c in entries if "PANDEMONIUM" in n]

    log_info("cells: " + ", ".join(c.label for c in cells)
             + (f"  (x{args.iterations} iter)" if args.iterations > 1 else ""))
    print()

    # PHASE 1 -- the short traced burst per cell, before the full iteration. Each
    # cell's burst gives the latency + cause axes per arm; phase 2 gives cost.
    trace_by_cell = {c.label: {} for c in cells}
    if run_burst:
        for cell in cells:
            trace_by_cell[cell.label] = run_burst_phase(entries, stamp, cell)
            if is_scx_active():
                wait_for_deactivation(5.0)

    # PHASE 2 -- the full perf-stat iteration. ARM-OUTER, CELL-INNER: activate each
    # scheduler once and run every cell under it, so a sweep does not re-load the
    # scheduler per cell.
    results_by_cell = {c.label: {} for c in cells}
    spreads_by_cell = {c.label: {} for c in cells}

    try:
        for sched_name, cmd in entries:
            log_info(f"[{sched_name}] starting...")
            guard = None
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"[{sched_name}] failed to activate, skipping")
                    for c in cells:
                        results_by_cell[c.label][sched_name] = (None, None)
                    continue

            for cell in cells:
                res, spreads = _measure(sched_name, cell, args.iterations)
                results_by_cell[cell.label][sched_name] = res
                if spreads:
                    spreads_by_cell[cell.label][sched_name] = spreads

            if guard is not None:
                stop_and_wait(guard)
            time.sleep(2)
            print()

    except KeyboardInterrupt:
        log.interrupted()
    finally:
        # DISENGAGE AT END (MATCHES OTHER BENCHES). EACH PER-SCHEDULER
        # stop_and_wait() ALREADY TORE DOWN ITS OWN INSTANCE; WE JUST MAKE
        # SURE NOTHING LINGERS BY WAITING FOR FULL DEACTIVATION.
        if is_scx_active():
            wait_for_deactivation(5.0)

    cells_data = [(cell, results_by_cell[cell.label],
                   spreads_by_cell[cell.label], trace_by_cell.get(cell.label, {}))
                  for cell in cells]
    if any(results_by_cell[c.label] for c in cells):
        print()
        prom_path = write_prometheus(ver, git, stamp, ncpus, cells_data, NR_LOOPS)
        report_path = write_report(ver, git, stamp, ncpus, cells_data, NR_LOOPS)

        log.report(report_path.read_text())
        log_info(f"REPORT: {report_path}")
        log_info(f"METRICS: {prom_path}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
