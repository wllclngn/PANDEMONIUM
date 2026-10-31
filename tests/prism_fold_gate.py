#!/usr/bin/env python3
"""prism_fold_gate -- the three capture readers, against real captures.

WHAT IT GUARDS. ipc_hop_from_recording, ipc_contention_from_recording and
ipc_pair_from_recording each read a montauk envelope and fold fields into the
ipc result that reaches the .prom and the table. They are pure functions over a
capture on disk, so they are testable without running a workload, without root
and without a scheduler -- which is the whole reason to have this: the
alternative is discovering a mis-named gauge after a twenty-minute traced run.

THE TWO PROPERTIES THAT MATTER.

1. PRESENCE. Against a capture that HAS the data, every field the emitter
   publishes must appear. A silently-absent field is the failure mode here: a
   report dict carries verdict and class at the top level and everything numeric
   under `gauges`, so reading a gauge as a report key yields None rather than
   raising, and the row disappears instead of erroring.

2. NO CAPTURE, NO FIELDS. Given a recording object with no events path (every
   run without --trace), all three must return {} rather than raising.
"""
import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = Path("/tmp/pandemonium")

# Fields each reader owns, checked for presence on a capture that has the data.
EXPECT = {
    "hop": ("wake2run_p50_us", "wake2run_n"),
    "contention": ("futex_class", "spins_class", "spin_livelock",
                   "waits_total", "waits_dominant", "wakers_class",
                   "waker_pids", "waker_monogamy", "waker_run_p99"),
    "pair": ("pair_tids", "pair_wake2run_n", "pair_wake2run_p50_us",
             "pair_wake2run_worst_us"),
}


def note(msg):
    print(f"[fold] {msg}")


def load_suite():
    spec = importlib.util.spec_from_file_location(
        "pt", str(ROOT / "tests" / "pandemonium-tests.py"))
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


class Rec:
    def __init__(self, events=None, d=None):
        self.events_path = events
        self.dir = d


def readers(m):
    return (("hop", m.ipc_hop_from_recording),
            ("contention", m.ipc_contention_from_recording),
            ("pair", m.ipc_pair_from_recording))


def newest(pattern):
    hits = sorted(CAPTURE_DIR.glob(pattern), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    return hits[0] if hits else None


def main() -> int:
    m = load_suite()
    fails = []

    # 2. NO CAPTURE, NO FIELDS -- run first, it needs nothing on disk.
    for name, fn in readers(m):
        for rec in (None, Rec(None, None), Rec(None, "/nonexistent")):
            got = fn(rec)
            if got != {}:
                fails.append(f"{name}: returned {got!r} for a rec with no capture")
    note("PASS no-capture returns {} for all three" if not fails
         else "FAIL no-capture path")

    pand = newest("montauk-ipc-PANDEMONIUM-BPF-*.events")
    if pand is None:
        note(f"SKIP no PANDEMONIUM capture under {CAPTURE_DIR} -- run a "
             f"--trace ipc cell first; the no-capture check above still ran")
        return 1 if fails else 0

    # 1. PRESENCE.
    rec = Rec(str(pand), str(pand).replace(".events", ""))
    folded = {}
    for name, fn in readers(m):
        got = fn(rec)
        folded.update(got)
        missing = [k for k in EXPECT[name] if k not in got]
        if missing:
            fails.append(f"{name}: missing {', '.join(missing)} on {pand.name}")
        else:
            note(f"PASS {name}: {len(got)} field(s) on {pand.name}")

    # The emitter's key list must match what the readers actually produce.
    src = (ROOT / "tests" / "pandemonium-tests.py").read_text()
    for k in sorted(folded):
        if k.endswith("_class") or k == "pair_tids":
            continue          # strings; the table prints them, the prom does not
        if f'"{k}"' not in src:
            fails.append(f"emitter: {k} folded but never referenced")

    for f in fails:
        note(f"FAIL {f}")
    note("GATE PASSED: every capture reader folds what the emitter publishes"
         if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
