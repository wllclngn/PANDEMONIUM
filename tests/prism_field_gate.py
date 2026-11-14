#!/usr/bin/env python3
"""prism_field_gate -- which build, which test, which width, without a run.

WHAT IT GUARDS. Three axes every prism bench is composed along, and the two
things that ride on an arm:

  WHICH BUILD   --arm NAME=COMMAND and the field it makes (parse_arm,
                scheduler_field), and PANDEMONIUM's two arms in their one
                spelling (pandemonium_arms, entries_for_cores)
  WHICH TEST    prism-scale's cells as one list (scale_cells, bench_slug)
  WHICH WIDTH   the widths prism makes for a workload that does not sweep
                (dev_widths)
  THE EVIDENCE  the kernel's event counters as a report prints them, zeros
                included (format_scx_events)
  THE DISTURB   offline:CPU@START+HOLD (parse_disturb)
  THE VERDICTS  what pcpu, contention and scx conclude from an arm's runs, and
                a recorded probe draw as scale's report prints it

All of it is pure: argument text in, a field or a list out. So it is checked
here, with no root, no scheduler and no workload, where a mistake costs a
second instead of a bench that ran the wrong arms for twenty minutes.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def note(msg):
    print(f"[field] {msg}")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


class Args:
    """A parsed prism-scale namespace with only what scale_cells reads."""
    def __init__(self, cells=None):
        self.cells = cells


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def main() -> int:
    import pandemonium_common as pc
    pt = load("pt", ROOT / "tests" / "pandemonium-tests.py")
    prism = load("prism", ROOT / "tests" / "prism.py")
    fails = []

    def check(what, got, want):
        if got == want:
            note(f"PASS {what}")
        else:
            fails.append(f"{what}: got {got!r}, want {want!r}")

    # WHICH BUILD. A path is made absolute; a bare name is found on PATH.
    sh = "/bin/sh"
    check("arm by path", pc.parse_arm(f"old={sh} -c true"), ("old", [sh, "-c", "true"]))
    check("arm by name on PATH", pc.parse_arm("shell=sh")[1][0].endswith("/sh"), True)
    check("arm refuses a file that is not there", raises(pc.parse_arm, "x=/no/such/file"), True)
    check("arm refuses a label with a space", raises(pc.parse_arm, f"a b={sh}"), True)
    check("arm refuses the kernel's own name", raises(pc.parse_arm, f"EEVDF={sh}"), True)
    check("arm refuses a spec with no command", raises(pc.parse_arm, "lonely"), True)

    pand = [("PANDEMONIUM (BPF)", ["p", "--no-adaptive"]), ("PANDEMONIUM (ADAPTIVE)", ["p"])]
    names = lambda field: [n for n, _ in field]
    saved = list(pc._ARMS)
    try:
        pc._ARMS[:] = []
        check("no arms, no names: EEVDF and PANDEMONIUM, as before",
              names(pc.scheduler_field(None, pand)),
              ["EEVDF", "PANDEMONIUM (BPF)", "PANDEMONIUM (ADAPTIVE)"])
        pc._ARMS[:] = [("old", [sh]), ("new", [sh, "-c", "true"])]
        check("arms alone are the whole field",
              pc.scheduler_field(None, pand), [("old", [sh]), ("new", [sh, "-c", "true"])])
        check("arms beside --only eevdf",
              names(pc.scheduler_field(None, pand, "eevdf", exact=True)), ["EEVDF", "old", "new"])
        check("arms beside a PANDEMONIUM arm flag",
              names(pc.scheduler_field("bpf", pand[:1])), ["PANDEMONIUM (BPF)", "old", "new"])
        check("arms beside --schedulers keep EEVDF",
              names(pc.scheduler_field(None, pand, "no-such-scheduler-xyz")), ["EEVDF", "old", "new"])
        field = pc.scheduler_field(None, pand)
        field[0][1].append("--mutated")
        check("a field's argv is its own copy", pc._ARMS[0][1], [sh])
    finally:
        pc._ARMS[:] = saved

    # PANDEMONIUM's ARMS, ONE SPELLING. BPF first, --verbose on both unless a
    # bench measures what that line costs; the width flag is theirs alone.
    binary = str(pc.BINARY)
    check("both arms, BPF first", pc.pandemonium_arms(None),
          [("PANDEMONIUM (BPF)", [binary, "--verbose", "--no-adaptive"]),
           ("PANDEMONIUM (ADAPTIVE)", [binary, "--verbose"])])
    check("--bpf-only is the one arm", names(pc.pandemonium_arms("bpf")), ["PANDEMONIUM (BPF)"])
    check("--adaptive-only is the one arm",
          names(pc.pandemonium_arms("adaptive")), ["PANDEMONIUM (ADAPTIVE)"])
    check("verbose off drops the flag from both", pc.pandemonium_arms(None, verbose=False),
          [("PANDEMONIUM (BPF)", [binary, "--no-adaptive"]), ("PANDEMONIUM (ADAPTIVE)", [binary])])
    field = [("EEVDF", None), ("PANDEMONIUM (BPF)", [binary]), ("scx_other", ["scx_other"])]
    check("the width flag goes to PANDEMONIUM's arms alone", pc.entries_for_cores(field, 4),
          [("EEVDF", None), ("PANDEMONIUM (BPF)", [binary, "--nr-cpus", "4"]),
           ("scx_other", ["scx_other"])])
    check("a width leaves the field it was made from alone", field[1][1], [binary])
    check("a field with this tree's binary needs the build", pc.fields_pandemonium(field), True)
    check("a field without it does not",
          pc.fields_pandemonium([("EEVDF", None), ("scx_other", ["scx_other"])]), False)

    # THE DISTURB.
    check("disturb", pc.parse_disturb("offline:11@3+12"), (11, 3.0, 12.0))
    check("disturb with fractions", pc.parse_disturb(" offline:5@0.5+2.5 "), (5, 0.5, 2.5))
    check("disturb refuses cpu0", raises(pc.parse_disturb, "offline:0@1+1"), True)
    check("disturb refuses no hold", raises(pc.parse_disturb, "offline:3@1+0"), True)
    check("disturb refuses another shape", raises(pc.parse_disturb, "online:3@1+1"), True)

    # WHICH TEST.
    check("no cell named is the full pass", pt.scale_cells(Args()), None)
    check("cells come back in the bench's order",
          pt.scale_cells(Args("deadline, latency,burst")), ("latency", "burst", "deadline"))
    check("a cell named twice is one cell", pt.scale_cells(Args("ipc,ipc")), ("ipc",))
    check("every cell named is the full pass",
          pt.scale_cells(Args(",".join(pt.SCALE_CELLS))), None)
    check("an unknown cell is refused", raises(pt.scale_cells, Args("latency,nope")), True)
    check("slug: full pass", pt.bench_slug({}), "prism-scale")
    check("slug: one cell", pt.bench_slug({"ipc_only": True}), "prism-ipc")
    check("slug: the latency probe alone", pt.bench_slug({"latency_only": True}), "prism-latency")
    check("slug: several cells", pt.bench_slug({"cells": ["latency", "deadline"]}), "prism-cells")

    # WHICH WIDTH.
    check("prism's widths, ascending and once", prism.dev_widths("8,4,4", 12), [4, 8])
    check("a width the box cannot be is left out", prism.dev_widths("2,64", 12), [2])
    check("no --cores leaves the box alone", prism.dev_widths(None, 12), [])
    check("a width that is not a number is refused", raises(prism.dev_widths, "4,x", 12), True)

    # THE EVIDENCE. A zero is a reading and is printed.
    lines = pt.format_scx_events({"a": {"scx_events": {"DISPATCH_LOCAL_DSQ_OFFLINE": 0,
                                                       "SELECT_CPU_FALLBACK": 7}},
                                  "EEVDF": {}})
    check("event counters print their zeros",
          any("DISPATCH_LOCAL_DSQ_OFFLINE=0" in l and "SELECT_CPU_FALLBACK=7" in l for l in lines),
          True)
    check("an arm with no counters prints no row", any(l.startswith("EEVDF") for l in lines), False)
    check("no counters at all prints nothing", pt.format_scx_events({"EEVDF": {}}), [])

    # THE VERDICTS. What pcpu, contention and scx conclude from an arm's runs.
    git = {"commit": "0000000", "dirty": False}
    check("scx scan: CI's patterns fail a line",
          pt._scx_ci_check_output("ok\nBUG: x\nwatchdog Timeout y\nRETBleed: WARNING: z\n"),
          ["BUG: x", "watchdog Timeout y"])
    arm = lambda ok: {"burst": [{"pass": ok, "max_delay_s": 0.5}, {"pass": True, "max_delay_s": 1.5},
                                {"pass": True, "max_delay_s": 1.0}],
                      "steal": [{"skip": True}] * 3,
                      "sojourn": [{"pass": True}] * 3, "pcpu_starve": [{"pass": True}] * 3}
    text, ok = pt.format_pcpu_report("0", git, 3, {4: {"EEVDF": arm(True), "a": arm(True)}})
    check("pcpu: every arm passing passes, a skip counted with them", ok, True)
    check("pcpu: a reading is the median of its iterations", "max_delay=1s" in text, True)
    check("pcpu: one arm failing one iteration fails the run",
          pt.format_pcpu_report("0", git, 3, {4: {"EEVDF": arm(True), "a": arm(False)}})[1], False)
    check("pcpu: no result is not a pass", pt.format_pcpu_report("0", git, 1, {})[1], False)
    runs = [{"deficit-storm": {"survived": True, "p99_us": 10, "recovery_within_2x": True}},
            {"deficit-storm": {"survived": True, "p99_us": 30}},
            {"deficit-storm": {"survived": True, "p99_us": 20}}, {}]
    check("contention: a reading is the median over the iterations that carry it",
          pt._contention_reading(runs, "deficit-storm", "p99_us"), 20.0)
    check("contention: a flag is not a reading",
          pt._contention_reading(runs, "deficit-storm", "recovery_within_2x"), None)
    text, crashed = pt.format_contention_report(
        "0", git, 4, 12, {4: {"a": {"survived": 3, "crashed": 1, "runs": runs}}})
    check("contention: the iterations not survived are counted", crashed, 1)
    check("contention: a phase row counts who reached it", "3/3 survived" in text, True)
    leg = lambda ok: {"pass": ok, "status": "OK", "held_s": 5.0, "failures": [] if ok else ["x"],
                      "scx_events": {"SELECT_CPU_FALLBACK": 0}, "log": None, "output": ["said"]}
    check("scx: both legs passing passes",
          pt.format_scx_report("0", git, 5.0, 31, None,
                               {4: {"a": [{"functional": leg(True), "stress": leg(True)}]}})[1], True)
    check("scx: a failed leg fails the run",
          pt.format_scx_report("0", git, 5.0, 31, None,
                               {4: {"a": [{"functional": leg(True), "stress": leg(False)}]}})[1], False)
    text, ok = pt.format_scx_report("0", git, 5.0, 0, "offline:3@1+2",
                                    {4: {"a": [{"functional": leg(True)}]}})
    check("scx: the functional leg alone passes when the stress leg is skipped", ok, True)
    check("scx: the report carries what the arm left",
          all(s in text for s in ("SELECT_CPU_FALLBACK=0", "  said", "offline:3@1+2")), True)
    draws = {4: {"a": {"probe_trace": {"mixed": {"probe_p99_us": 42.0, "stall_verdict": "v"}}},
                 "EEVDF": {}}}
    check("scale: a recorded draw is a table of its own",
          [l for l in pt.format_probe_trace(draws) if "PROBE p99" in l or l.startswith("dispatch-stall")],
          [pc.table_row("PROBE p99 us", ["42"], 26, 20), "dispatch-stall, probe, a: v"])
    check("scale: no recorded draw prints nothing", pt.format_probe_trace({4: {"a": {}}}), [])

    for f in fails:
        note(f"FAIL {f}")
    note("GATE PASSED: the field, the cells, the widths, the evidence, the disturb "
         "and the verdicts" if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
