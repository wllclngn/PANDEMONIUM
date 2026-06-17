#!/usr/bin/env python3
# storm_score: score a scheduler tick-log for the cpu_release kick-storm.
#
# Replaces the hand-grep+sublimation pass run on every rawhat log. Reads the
# verbose tick lines (d/s / idle / shared / kick H / enq R), reports the storm
# fraction and -- now that kick H counts only real SCX_KICK_PREEMPT (counting
# fix) -- classifies each storm tick as a REAL IPI storm or IDLE re-enqueue
# churn. Numeric reductions go through sublimation, not hand-rolled.
#
#   storm_score.py <tick-log> [<tick-log> ...]
#
# A tick is a storm tick when d/s >= STORM_DS (the scheduler is burning the CPU
# on dispatch/re-enqueue overhead). On a storm tick, kick H >= HARD_FRAC * enq R
# means the kicks are real preempts (IPI storm); below it, the re-enqueues are
# not firing preempts (IDLE churn -- the kicks no-op on busy CPUs).

import re
import subprocess
import sys

STORM_DS = 900_000      # d/s at/above this = the CPU is in the storm
HARD_FRAC = 0.5         # kick H >= this * enq R on a storm tick = real IPI storm

_TICK = re.compile(
    r"d/s:\s*(\d+).*?idle:\s*(\d+)%.*?shared:\s*(\d+).*?"
    r"kick:\s*H=(\d+).*?enq:\s*W=(\d+)\s+R=(\d+)"
)


def _sub(nums, *args):
    """Pipe a numeric stream through the sublimation CLI; return its stdout."""
    p = subprocess.run(
        ["sublimation", *args],
        input="\n".join(str(n) for n in nums) + "\n",
        capture_output=True, text=True,
    )
    return p.stdout.strip()


def score(text_or_path, is_text=False):
    """Score tick text (is_text=True) or a tick-log path. Returns a dict:
    ticks, storm, pct, p50, dmax, real, churn, kind, sample (or {"error": ...})."""
    if is_text:
        lines = text_or_path.splitlines()
    else:
        try:
            with open(text_or_path) as f:
                lines = f.read().splitlines()
        except OSError as e:
            return {"error": str(e)}

    ds, storms, aborted = [], [], False
    for line in lines:
        if "WATCHDOG" in line and ("stalled" in line or "aborting" in line):
            # The monitor loop starved >10s and self-ejected -- the storm's
            # terminal outcome (see watchdog.rs). The most severe signal there is.
            aborted = True
        m = _TICK.search(line)
        if not m:
            continue
        d, idle, shared, kh, w, r = (int(x) for x in m.groups())
        ds.append(d)
        if d >= STORM_DS:
            storms.append((d, idle, shared, kh, r))
    if not ds:
        return {"error": "no tick lines found", "aborted": aborted}

    n, ns = len(ds), len(storms)
    res = {
        "ticks": n,
        "storm": ns,
        "pct": 100.0 * ns / n,
        "p50": _sub(ds, "quantile", "0.5", "--nearest"),
        "dmax": _sub(ds, "sort").splitlines()[-1],
        "real": 0, "churn": 0, "kind": "clean", "sample": None,
        "aborted": aborted,
    }
    if storms:
        real = sum(1 for (_, _, _, kh, r) in storms if r and kh >= HARD_FRAC * r)
        res["real"], res["churn"] = real, ns - real
        res["kind"] = "REAL IPI storm" if real > res["churn"] else "IDLE re-enqueue churn"
        d, idle, shared, kh, r = storms[len(storms) // 2]
        res["sample"] = {"ds": d, "idle": idle, "shared": shared, "kickH": kh,
                         "enqR": r, "ratio": (kh / r if r else 0.0)}
    return res


def format_score(label, d):
    if "error" in d:
        tail = "  WATCHDOG ABORT (monitor starved -- scheduler self-ejected)" \
            if d.get("aborted") else ""
        return f"{label}: {d['error']}{tail}"
    out = [f"{label}"]
    if d.get("aborted"):
        out.append("  WATCHDOG ABORT (monitor starved >10s -- scheduler "
                   "self-ejected; the storm's terminal outcome)")
    out += [
           f"  ticks {d['ticks']}  storm {d['storm']} ({d['pct']:.1f}%)  "
           f"d/s p50={d['p50']} max={d['dmax']}"]
    if d["sample"]:
        s = d["sample"]
        out.append(f"  storm ticks: {d['real']} real-IPI / {d['churn']} "
                   f"idle-churn -> {d['kind']}")
        out.append(f"  sample storm tick: d/s={s['ds']} idle={s['idle']}% "
                   f"shared={s['shared']} kickH={s['kickH']} enqR={s['enqR']} "
                   f"(kickH/enqR={s['ratio']:.2f})")
    else:
        out.append("  no storm ticks -- clean")
    return "\n".join(out)


def main(argv):
    if len(argv) < 2:
        print("usage: storm_score.py <tick-log> [<tick-log> ...]", file=sys.stderr)
        return 2
    for p in argv[1:]:
        print(format_score(p, score(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
