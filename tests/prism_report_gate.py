#!/usr/bin/env python3
"""prism_report_gate -- invariants for the numbers PRISM puts in front of a reader.

Four defects from the 2026-08-05 field report, each of which produced a report
that looked entirely correct. None was a crash, a traceback or a wrong
computation; all four were a true number presented so that it read as something
it was not. That is the class this gate exists to hold shut.

  COMPARISON MUST BE ABLE TO SHOW A LOSS. The section iterated a curated family
  list, so a workload absent from it could never appear no matter what it
  measured. cold-wake-starve regressed 5.2x and sat in the report body while the
  summary above it showed four straight wins. A summary that structurally cannot
  print a regression is not a summary, it is an advertisement.

  A RATIO ACROSS UNLIKE CAPTURES IS NOT A RATIO. montauk states the rule in the
  report itself -- compare arms only at like completeness -- and the headline
  "5.7x better" was computed across arms at 8.6% and 15.4%. The caveat printed
  faithfully, two sections away from the number it disqualified.

  THE VERSION MUST MATCH WHAT SHIPPED. The header read v5.17.0 beside the
  v5.17.1 commit hash, because the version is read from Cargo.toml and Cargo.toml
  was never bumped. The string was not wrong so much as unmaintained, which no
  amount of reading the code reveals.

  SUSCEPTIBILITY MUST SCORE RUNTIME STATE. CONFIG_NO_HZ_FULL=y was scored as
  "fully tickless" on a box whose cmdline designated no tickless CPU at all.
  Nearly every distro kernel ships the symbol, so this inflated the score on
  essentially every machine a report will ever come from.

    python3 prism_report_gate.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRISM = ROOT / "tests" / "prism.py"
COMMON = ROOT / "pandemonium_common.py"
CARGO = ROOT / "Cargo.toml"
COMMIT = ROOT / "COMMIT_MESSAGE.txt"


def _load_comparison():
    src = PRISM.read_text()
    ns = {"_sched_short": lambda s: {"PANDEMONIUM-BPF": "BPF",
                                     "PANDEMONIUM-ADAPTIVE": "ADAPTIVE"}.get(s, s)}
    for name in ("_COMPARE_ORDER", "_COMPLETENESS_RATIO_MAX"):
        m = re.search(rf"^{name} = (.+)$", src, re.M)
        if not m:
            raise RuntimeError(f"{name} missing from prism.py")
        ns[name] = eval(m.group(1))
    body = src[src.index("def _build_comparison("):src.index("def _coldwork_ramp")]
    exec(compile(body, "prism", "exec"), ns)
    return ns["_build_comparison"]


def _load_scorer():
    src = COMMON.read_text()
    block = src[src.index("    score, why = 0, []"):src.index("    verdict = (")]
    code = compile(block.replace("    ", "", 1).replace("\n    ", "\n"), "c", "exec")

    def score(hz, nohz, nohz_full, ncpu, isolcpus="", deepest="C3_ACPI",
              preempt="PREEMPT_DYNAMIC"):
        ns = dict(hz=hz, nohz=nohz, nohz_full=nohz_full, isolcpus=isolcpus,
                  ncpu=ncpu, deepest=deepest, preempt=preempt)
        exec(code, ns)
        return ns["score"], " | ".join(ns["why"])
    return score


def check() -> list[str]:
    bad = []
    cmp_fn = _load_comparison()

    # A family outside the curated order must still be reported.
    out = cmp_fn([("cold-wake-starve", "EEVDF", 20, 1.0),
                  ("cold-wake-starve", "PANDEMONIUM", 104, 1.0)])
    if "cold-wake-starve" not in out:
        bad.append("COMPARISON drops families outside _COMPARE_ORDER -- a "
                   "regression in one can never reach the summary")
    elif "worse" not in out:
        bad.append("COMPARISON reported a regression without naming it a loss")

    # Unlike completeness must withhold the ratio but keep both measurements.
    out = cmp_fn([("fork-thread", "EEVDF", 19685, 0.086),
                  ("fork-thread", "PANDEMONIUM-BPF", 3451, 0.154)])
    if "withheld" not in out:
        bad.append("COMPARISON printed a ratio across arms at 8.6% and 15.4% "
                   "completeness -- montauk's own rule forbids it")
    if "3451us" not in out or "19685us" not in out:
        bad.append("COMPARISON suppressed the MEASUREMENTS, not just the ratio")

    # Like completeness must still compare.
    out = cmp_fn([("ipc", "EEVDF", 10, 0.995), ("ipc", "PANDEMONIUM-BPF", 2, 0.950)])
    if "5.0x better" not in out:
        bad.append("COMPARISON withheld a ratio between comparable arms")

    # Version must agree with the release being assembled.
    cargo = re.search(r'^version\s*=\s*"([^"]+)"', CARGO.read_text(), re.M)
    subject = re.match(r"PANDEMONIUMv([0-9.]+):", COMMIT.read_text())
    if not cargo or not subject:
        bad.append("cannot read version from Cargo.toml or COMMIT_MESSAGE.txt")
    elif cargo.group(1) != subject.group(1):
        bad.append(f"Cargo.toml says {cargo.group(1)} but COMMIT_MESSAGE.txt is "
                   f"assembling v{subject.group(1)} -- every report will carry "
                   f"the stale one beside a current commit hash")

    # Susceptibility must not credit an unused build symbol.
    score = _load_scorer()
    s_off, why_off = score("1000", "NO_HZ_FULL", "", 20)
    s_on, why_on = score("1000", "NO_HZ_FULL", "1-11", 12)
    if "fully tickless" in why_off:
        bad.append("stall susceptibility calls a box 'fully tickless' with no "
                   "nohz_full= mask on the cmdline")
    if s_off >= s_on:
        bad.append(f"a box with NO tickless CPUs scores {s_off}, at or above the "
                   f"{s_on} of one with nohz_full=1-11")
    if "idle CPUs go tickless" not in why_off:
        bad.append("stall susceptibility no longer credits idle-dynticks, which "
                   "is the actual DARK entry condition")
    return bad


def main() -> int:
    try:
        bad = check()
    except Exception as e:                       # a moved symbol is a failure too
        print(f"FAIL: gate could not run: {e}")
        return 1
    for b in bad:
        print(f"FAIL: {b}")
    if bad:
        print(f"\n{len(bad)} failure(s)")
        return 1
    print("ok: comparison reports losses, withholds ratios across unlike "
          "captures, version agrees with the release, susceptibility scores "
          "runtime state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
