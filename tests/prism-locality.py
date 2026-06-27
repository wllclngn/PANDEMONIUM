#!/usr/bin/env python3
# prism-locality: placement-locality probe via montauk's generic `locality` report.
#
# Runs a migration-heavy load (perf bench sched messaging -- the same storm the
# fork-thread gate uses), captures it under montauk (--trace-out per-event stream
# + the embedded cache_topology snapshot), and folds montauk_analyze --report
# locality: every migration becomes a cache-tier distance (same-L2 / same-L3 /
# same-socket / cross-socket) and the report prints the distribution + the
# tier-to-tier decay. That decay is the screening signal T4 reads; on a multi-L3
# part the cross-L3/cross-socket tiers populate and the decay vs phi is the gate.
#
# The harness owns the montauk and load lifecycles -- no manual --trace / pkill.
# Standalone (run under sudo) and part of the prism-* family.

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    LOG_DIR, get_version, get_git_info,
    log_info, log_warn, log_error, run_cmd_capture,
    check_sources_changed, build,
    is_scx_active, scx_scheduler_name, get_online_cpus,
    montauk_trace, montauk_available,
    PrometheusBuilder, table_header, table_row,
)

MONTAUK_ANALYZE = "/usr/local/bin/montauk_analyze"
MSG_COMM = "sched-messaging"   # perf bench sched messaging names its workers this


# LOAD: a sustained sched-messaging storm in its own process group, so a single
# killpg tears down the loop AND every perf child -- no stragglers, no manual kill.

def start_messaging_load(groups: int, loops: int) -> subprocess.Popen:
    cmd = (f"while true; do perf bench sched messaging -g {groups} -l {loops} "
           f">/dev/null 2>&1 || sleep 0.2; done")
    return subprocess.Popen(["bash", "-c", cmd], preexec_fn=os.setsid)


def stop_messaging_load(p: subprocess.Popen) -> None:
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
    subprocess.run(["pkill", "-9", "-x", MSG_COMM],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# LOCALITY REPORT PARSE -- matches trace_analyze.cpp LocalityReport::emit() exactly:
#   "VERDICT: no cache_topology snapshot ..." | "VERDICT: no cross-CPU migrations ..."
#   "N migrations (M unmapped)"
#   table: tier moves pct  (same-L2 / same-L3 / same-socket / cross-socket)
#   "decay (tier_{k+1}/tier_k): a b c"
#   "VERDICT: migration density <decays monotonically|does NOT decay ...> ..."

def parse_locality(report: str) -> dict:
    out = {"migrations": 0, "unmapped": 0, "tiers": {}, "decay": [],
           "monotonic": None, "no_topo": False, "no_migrations": False}
    if "no cache_topology snapshot" in report:
        out["no_topo"] = True
        return out
    if "no cross-CPU migrations" in report:
        out["no_migrations"] = True
        return out
    m = re.search(r"(\d+)\s+migrations\s+\((\d+)\s+unmapped\)", report)
    if m:
        out["migrations"] = int(m.group(1))
        out["unmapped"] = int(m.group(2))
    for tier in ("same-L2", "same-L3", "same-socket", "cross-socket"):
        m = re.search(rf"^{re.escape(tier)}\s+(\d+)\s+([\d.]+)%", report, re.M)
        if m:
            out["tiers"][tier] = (int(m.group(1)), float(m.group(2)))
    m = re.search(r"decay \(tier_\{k\+1\}/tier_k\):\s*([\d. ]+)", report)
    if m:
        out["decay"] = [float(x) for x in m.group(1).split()]
    if "decays monotonically" in report:
        out["monotonic"] = True
    elif "does NOT decay monotonically" in report:
        out["monotonic"] = False
    return out


def analyze(events: Path) -> tuple[str, dict]:
    rc, so, se = run_cmd_capture([MONTAUK_ANALYZE, str(events), "--report", "locality"])
    if rc != 0:
        log_error(f"montauk_analyze failed (rc={rc}): {se.strip()[:200]}")
        return so + se, {}
    return so, parse_locality(so)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Placement-locality probe via montauk's locality report.")
    ap.add_argument("--duration", type=float, default=45.0,
                    help="seconds to hold the storm under capture (default 45)")
    ap.add_argument("--groups", type=int, default=24,
                    help="perf bench sched messaging -g (task groups, default 24)")
    ap.add_argument("--loops", type=int, default=1000,
                    help="perf bench sched messaging -l (messages/loop, default 1000)")
    ap.add_argument("--no-build", action="store_true",
                    help="skip the source-change rebuild check")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="accepted for `prism --dev` parity; this bench is "
                         "PANDEMONIUM-only already (no EEVDF arm), so it is a no-op")
    args = ap.parse_args()

    if os.geteuid() != 0:
        log_error("must run as root (montauk needs CAP_SYS_ADMIN) -- re-run under sudo")
        return 2
    if not montauk_available():
        log_error("montauk not found -- install montauk (>= 7.8.0 for the locality report)")
        return 2
    if subprocess.run(["which", "perf"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        log_error("perf not found -- install perf (the sched-messaging storm needs it)")
        return 2
    if not args.no_build and check_sources_changed():
        log_info("sources changed -- rebuilding")
        if not build():
            log_error("build failed")
            return 2

    sched = scx_scheduler_name()
    if is_scx_active() and sched == "pandemonium":
        log_info("active scheduler: pandemonium")
    else:
        log_warn(f"active scheduler is '{sched or 'none'}' -- locality reflects that, "
                 "not pandemonium")

    online = get_online_cpus()
    if online < 2:
        log_error("need >= 2 online CPUs (one is the montauk drain core)")
        return 2
    drain = online - 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_info(f"locality probe: sched-messaging -g {args.groups} -l {args.loops}, "
             f"{args.duration:.0f}s under capture, montauk drain core {drain}")

    load = start_messaging_load(args.groups, args.loops)
    time.sleep(2.0)                 # let the storm ramp before the window opens
    events = None
    try:
        with montauk_trace(MSG_COMM, "locality", stamp,
                           events=True, pin_cpu=drain) as rec:
            time.sleep(args.duration)
            events = rec.events_path
    finally:
        stop_messaging_load(load)

    if events is None or not Path(events).exists():
        log_error("montauk produced no --trace-out capture -- cannot analyze")
        return 2
    report_text, p = analyze(Path(events))

    # VERDICT
    if p.get("no_topo"):
        log_error("locality: no cache_topology snapshot -- montauk is pre-v7.8.0; "
                  "reinstall montauk")
        rc = 2
    elif p.get("no_migrations"):
        log_warn("locality: no cross-CPU migrations captured -- the storm may not "
                 "have migrated traced tasks, or the scheduler pinned them")
        rc = 0
    else:
        rc = 0
        log_info(f"locality: {p['migrations']} migrations ({p['unmapped']} unmapped) | "
                 f"decay {p.get('decay')} | "
                 f"{'monotonic (local)' if p.get('monotonic') else 'NOT monotonic (scatter)'}")

    # REPORT
    print()
    log_info(f"prism-locality  (scheduler={sched or 'none'}, cores={online}, "
             f"duration={args.duration:.0f}s)")
    if p.get("tiers"):
        print(table_header("tier", ["moves", "pct"]))
        for tier in ("same-L2", "same-L3", "same-socket", "cross-socket"):
            mv, pct = p["tiers"].get(tier, (0, 0.0))
            print(table_row(tier, [str(mv), f"{pct:.1f}%"]))
    print()
    print(report_text.rstrip())     # montauk is the instrument -- show its verdict

    # PROMETHEUS
    git = get_git_info()
    pb = PrometheusBuilder("locality")
    pb.info(version=get_version(), git_commit=git["commit"], git_dirty=git["dirty"])
    labels = {"scheduler": sched or "none", "cpus": str(online), "workload": "sched_messaging"}
    pb.gauge("migrations", p.get("migrations", 0), help="total cross-CPU migrations", labels=labels)
    for tier in ("same-L2", "same-L3", "same-socket", "cross-socket"):
        mv, _ = p.get("tiers", {}).get(tier, (0, 0.0))
        pb.gauge("tier_moves", mv, help="migrations at this cache-tier distance",
                 labels={**labels, "tier": tier})
    out = LOG_DIR / f"locality-{stamp}.prom"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pb.render())
    log_info(f"prometheus -> {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
