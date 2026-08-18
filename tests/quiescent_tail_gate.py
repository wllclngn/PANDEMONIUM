#!/usr/bin/env python3
"""quiescent_tail_gate -- MontaukTrace records quiet AFTER the workload stops.

WHY THE ORDER IS THE TEST. The tail is only in the capture if montauk is still
recording while it elapses. Sleep after signalling and the code looks correct,
the log line still prints, the run still takes three seconds longer -- and the
capture ends exactly where it did before. That failure is invisible in every
artifact except the one number it was meant to fix, so it is asserted here
directly: The sleep must be observed BEFORE the first signal reaches montauk.

The rest is the shape around it. A run unwinding from an exception skips the
tail, because a failed workload has nothing to prove about its strands and three
seconds of teardown on every error path is a poor trade. A zero tail is off.

Uses a recording stand-in for the montauk process, so this needs no montauk, no
root and no capture -- it tests the lifecycle, which is where the defect lives.

    python3 quiescent_tail_gate.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandemonium_common as pc  # noqa: E402


class FakeProc:
    """Stands in for the montauk subprocess, recording what happened when."""

    def __init__(self, events):
        self.events = events
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, sig):
        self.events.append(("signal", time.monotonic()))
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _trace(tmp, events, **kw):
    t = pc.MontaukTrace("pat", "label", "stamp", log_dir=tmp, **kw)
    t.proc = FakeProc(events)
    t._out = None
    return t


def run_case(name, tmp, expect_tail, **kw):
    events = []
    t = _trace(tmp, events, **kw)
    real_sleep = time.sleep

    def spy(sec):
        events.append(("sleep", sec, time.monotonic()))
        real_sleep(min(sec, 0.01))      # keep the suite fast; order is the point

    pc.time.sleep = spy
    try:
        if kw.pop("_via_exception", False):
            t.__exit__(RuntimeError, RuntimeError("x"), None)
        else:
            t.stop()
    finally:
        pc.time.sleep = real_sleep

    sleeps = [e for e in events if e[0] == "sleep"]
    signals = [e for e in events if e[0] == "signal"]
    problems = []
    if expect_tail:
        if not sleeps:
            problems.append("no quiescent tail was recorded at all")
        elif not signals:
            problems.append("montauk was never signalled")
        elif sleeps[0][2] > signals[0][1]:
            problems.append("tail slept AFTER the signal -- the quiet period is "
                            "outside the capture and the fix does nothing")
    else:
        if sleeps:
            problems.append(f"expected no tail, slept {sleeps[0][1]}s")
    for p in problems:
        print(f"FAIL [{name}]: {p}")
    if not problems:
        print(f"ok: {name}")
    return problems


def main() -> int:
    tmp = Path("/tmp/claude-1000/quiescent-tail-gate")
    tmp.mkdir(parents=True, exist_ok=True)
    fails = []
    fails += run_case("default tail is recorded before the signal", tmp,
                      expect_tail=True)
    fails += run_case("explicit tail is recorded", tmp, expect_tail=True,
                      quiesce_s=2.0)
    fails += run_case("zero tail is disabled", tmp, expect_tail=False,
                      quiesce_s=0.0)

    # Exception path: __exit__ must pass quiesce=False through to stop().
    events = []
    t = _trace(tmp, events)
    real_sleep = time.sleep
    pc.time.sleep = lambda s: events.append(("sleep", s, time.monotonic()))
    try:
        t.__exit__(RuntimeError, RuntimeError("boom"), None)
    finally:
        pc.time.sleep = real_sleep
    if [e for e in events if e[0] == "sleep"]:
        print("FAIL [exception path]: tail ran while unwinding from an exception")
        fails.append(1)
    else:
        print("ok: exception path skips the tail")

    if pc.MONTAUK_QUIESCE_S <= 0:
        print("FAIL: MONTAUK_QUIESCE_S default is not positive -- every stage "
              "silently loses the tail")
        fails.append(1)
    else:
        print(f"ok: default tail is {pc.MONTAUK_QUIESCE_S:.0f}s")

    print(f"\n{'FAILED' if fails else 'all passed'} ({len(fails)} failure(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
