#!/usr/bin/env python3
"""prism_helicoprion_gate -- prism's helicoprion workload, offline.

WHAT IT GUARDS. prism runs Piotr Gorski's helicoprion from its installed
package and carries none of it. Two things can go wrong silently: A package
installed or updated that nobody agreed to, and a result file read wrongly,
where a metric helicoprion never scores lands in a verdict.

THE PROPERTIES.

1. NOTHING INSTALLS WITHOUT A YES. A missing program with no terminal to ask
   on is refused, a missing one declined is refused, and only a yes reaches
   the installer, with the source this box has. An older package runs as it
   stands unless the update is accepted, a current one is asked nothing, a
   program no package owns is asked nothing, and a program the user names is
   never managed.
2. THE SOURCE IS THE BOX's. Where a repository carries the package, Shelly if
   it is installed and pacman otherwise; where none does, makepkg on Devario's
   recipe, Shelly installed or not; and nothing where there is no pacman.
3. THE VERSIONS COMPARED ARE THE PACKAGE's, READ AND NEVER ASSUMED: What pacman
   records as installed against the sync database's Version line or the
   recipe's epoch, pkgver and pkgrel, ordered by vercmp. The program's own
   --version is not consulted: The 0.2.1 package's program calls itself 0.2.0.
4. THE SESSION IS helicoprion's. A stand-in program is run with helicoprion's
   own flags, one measured run a workload, and the file it writes comes back
   with every metric's value, unit and direction.
5. AN INVALID RUN AND A FAILED SESSION ARE NO SAMPLE.
6. THE REPORT AND THE .prom CARRY WHAT helicoprion SCORES. Every metric is
   listed with its author credited; single-sample extremes, informational
   metrics and minor populations reach neither a vs-EEVDF row nor montauk.

    python3 prism_helicoprion_gate.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE = """#!{python}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(os.environ["FAKE_HELI_ARGV"]).write_text("\\n".join(args))
if args == ["--version"]:
    print("helicoprion 0.2.1")
    sys.exit(0)
mode = os.environ.get("FAKE_HELI_MODE", "ok")
if mode == "fail":
    sys.stderr.write("Error: no futex today\\n")
    sys.exit(1)
def metric(value, unit, better, **kw):
    return {{"value": value, "unit": unit, "better": better, **kw}}
run = {{"valid": mode != "invalid", "duration_s": 5.0, "metrics": {{
    "roundtrips_per_s": metric(1.09e6, "1/s", "higher"),
    "rtt_blocked_p50": metric(7.71, "us", "lower", share=0.93),
    "rtt_blocked_p99.9": metric(22.7, "us", "lower", share=0.93),
    "rtt_blocked_max": metric(558.0, "us", "lower", extreme=True, share=0.93),
    "rtt_blocked_same_cpu_p50": metric(16.5, "us", "lower", share=0.001),
    "hog_work_per_s": metric(2.24e6, "units/s", "neither")}}}}
if mode == "invalid":
    run["invalid_reason"] = "scheduler changed during the run"
out = Path(args[args.index("--output") + 1])
out.write_text(json.dumps({{"schema": 4, "tool_version": "0.2.1", "label": "x",
    "workloads": [{{"name": "wake-pingpong", "description": "", "params": {{}},
                   "runs": [run]}}]}}))
sys.stderr.write("  wake-pingpong          run 1/1\\u2026 rtt_blocked_p50=7.7 us\\n")
"""


def note(msg):
    print(f"[helicoprion] {msg}")


def load():
    spec = importlib.util.spec_from_file_location(
        "prism_helicoprion", str(ROOT / "tests" / "prism-helicoprion.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_consent(m, fails):
    before = len(fails)

    def case(name, *, installed, have, latest, interactive, answer, asked="helicoprion",
             want_prog, want_install, want_question):
        state = {"installed": installed, "installs": [], "questions": []}

        def which(prog):
            if prog != "helicoprion":
                return prog if prog == "/opt/h/helicoprion" else None
            return "/usr/bin/helicoprion" if state["installed"] else None

        def install(source):
            state["installs"].append(source)
            state["installed"] = True
            return True

        def ask(question):
            state["questions"].append(question)
            return answer

        prog = m.ensure_helicoprion(
            asked, interactive, which=which, source_of=lambda: "makepkg",
            latest_of=lambda source: latest, installed_of=lambda: have,
            older=lambda a, b: a < b, install=install, ask=ask)
        if prog != want_prog:
            fails.append(f"consent, {name}: program {prog!r}, wanted {want_prog!r}")
        if state["installs"] != (["makepkg"] if want_install else []):
            fails.append(f"consent, {name}: installs {state['installs']}")
        if bool(state["questions"]) != want_question:
            fails.append(f"consent, {name}: questions {state['questions']}")

    usr = "/usr/bin/helicoprion"
    case("missing, no terminal", installed=False, have=None, latest="0.2.1-1",
         interactive=False, answer=True, want_prog=None, want_install=False,
         want_question=False)
    case("missing, declined", installed=False, have=None, latest="0.2.1-1",
         interactive=True, answer=False, want_prog=None, want_install=False,
         want_question=True)
    case("missing, accepted", installed=False, have=None, latest="0.2.1-1",
         interactive=True, answer=True, want_prog=usr, want_install=True,
         want_question=True)
    case("older, no terminal", installed=True, have="0.2.0-1", latest="0.2.1-1",
         interactive=False, answer=True, want_prog=usr, want_install=False,
         want_question=False)
    case("older, declined", installed=True, have="0.2.0-1", latest="0.2.1-1",
         interactive=True, answer=False, want_prog=usr, want_install=False,
         want_question=True)
    case("older, accepted", installed=True, have="0.2.0-1", latest="0.2.1-1",
         interactive=True, answer=True, want_prog=usr, want_install=True,
         want_question=True)
    case("an older build of the same release", installed=True, have="0.2.1-1",
         latest="0.2.1-2", interactive=True, answer=True, want_prog=usr,
         want_install=True, want_question=True)
    case("current", installed=True, have="0.2.1-1", latest="0.2.1-1",
         interactive=True, answer=True, want_prog=usr, want_install=False,
         want_question=False)
    case("latest unread", installed=True, have="0.2.1-1", latest=None,
         interactive=True, answer=True, want_prog=usr, want_install=False,
         want_question=False)
    case("a program no package owns", installed=True, have=None, latest="0.2.1-1",
         interactive=True, answer=True, want_prog=usr, want_install=False,
         want_question=False)
    case("a named program", installed=False, have="0.1.0-1", latest="0.2.1-1",
         interactive=True, answer=True, asked="/opt/h/helicoprion",
         want_prog="/opt/h/helicoprion", want_install=False, want_question=False)
    if len(fails) == before:
        note("PASS nothing installs without a yes")


def check_source(m, fails):
    before = len(fails)
    cases = (({"shelly", "pacman", "makepkg"}, 0, "shelly"),
             ({"pacman", "makepkg"}, 0, "pacman"),
             ({"shelly", "pacman", "makepkg"}, 1, "makepkg"),
             ({"pacman", "makepkg"}, 1, "makepkg"),
             ({"pacman"}, 1, ""),
             ({"shelly", "makepkg"}, 0, ""),
             (set(), 1, ""))
    for tools, rc, want in cases:
        got = m.package_source(which=lambda p, tools=tools: p if p in tools else None,
                               capture=lambda cmd, rc=rc: (rc, ""))
        if got != want:
            fails.append(f"source: tools {sorted(tools)}, pacman -Si rc {rc}: "
                         f"{got!r}, wanted {want!r}")
    info = "Repository      : devario-development\nName            : helicoprion\n" \
           "Version         : 1:0.2.1-1\n"
    srcinfo = "pkgbase = helicoprion\n\tpkgver = 0.3.0\n\tpkgrel = 2\n\tarch = x86_64\n"
    got = (m.packaged_version("pacman", capture=lambda cmd: (0, info)),
           m.packaged_version("shelly", capture=lambda cmd: (0, info)),
           m.packaged_version("makepkg", fetch=lambda url: srcinfo),
           m.packaged_version("makepkg", fetch=lambda url: "\tepoch = 1\n" + srcinfo),
           m.packaged_version("makepkg", fetch=lambda url: None),
           m.packaged_version("pacman", capture=lambda cmd: (1, "")))
    if got != ("1:0.2.1-1", "1:0.2.1-1", "0.3.0-2", "1:0.3.0-2", None, None):
        fails.append(f"packaged version: {got}")
    got = (m.installed_version(capture=lambda cmd: (0, "helicoprion 0.2.1-1\n")),
           m.installed_version(capture=lambda cmd: (1, "")))
    if got != ("0.2.1-1", None):
        fails.append(f"installed version: {got}")
    asked: list[list[str]] = []

    def vercmp(answer):
        def capture(cmd):
            asked.append(cmd)
            return answer
        return capture
    got = tuple(m.behind("0.2.1-1", "0.2.1-2", capture=vercmp(a))
                for a in ((0, "-1\n"), (0, "0\n"), (0, "1\n"), (1, "")))
    if got != (True, False, False, False):
        fails.append(f"behind: {got}")
    if asked[:1] != [["vercmp", "0.2.1-1", "0.2.1-2"]]:
        fails.append(f"behind: asked {asked[:1]}")
    if len(fails) == before:
        note("PASS the source is the box's and the versions compared are the package's")


def check_session(m, tmp, fails):
    before = len(fails)
    prog = tmp / "helicoprion"
    prog.write_text(FAKE.format(python=sys.executable))
    os.chmod(prog, 0o755)
    argv = tmp / "argv"
    os.environ["FAKE_HELI_ARGV"] = str(argv)

    os.environ["FAKE_HELI_MODE"] = "ok"
    out = tmp / "arm-1.json"
    got = m.run_session(str(prog), "quick", 1, 5.0, "PANDEMONIUM (BPF)", out)
    want_argv = ["run", "--suite", "quick", "--runs", "1", "--warmup", "1",
                 "--duration", "5.0", "--cooldown", "1.0",
                 "--label", "PANDEMONIUM (BPF)", "--output", str(out)]
    if argv.read_text().splitlines() != want_argv:
        fails.append(f"session: argv {argv.read_text().splitlines()}")
    run = (got or {}).get("wake-pingpong", {})
    want = {"roundtrips_per_s": (1.09e6, "1/s", "higher", False, False),
            "rtt_blocked_p50": (7.71, "us", "lower", False, False),
            "rtt_blocked_p99.9": (22.7, "us", "lower", False, False),
            "rtt_blocked_max": (558.0, "us", "lower", True, False),
            "rtt_blocked_same_cpu_p50": (16.5, "us", "lower", False, True),
            "hog_work_per_s": (2.24e6, "units/s", "neither", False, False)}
    have = {k: (v["value"], v["unit"], v["better"], v["extreme"], v["minor"])
            for k, v in run.items()}
    if have != want:
        fails.append(f"session: metrics {have}")
    if not out.with_suffix(".log").is_file():
        fails.append("session: helicoprion's own lines were not kept")
    if m.tool_version(str(prog)) != "0.2.1":
        fails.append(f"session: version {m.tool_version(str(prog))!r}")

    for mode in ("invalid", "fail"):
        os.environ["FAKE_HELI_MODE"] = mode
        if m.run_session(str(prog), "quick", 1, 5.0, "EEVDF", tmp / f"{mode}.json"):
            fails.append(f"session: a {mode} run came back as a sample")
    if len(fails) == before:
        note("PASS the session is helicoprion's; an invalid or failed one is no sample")
    os.environ["FAKE_HELI_MODE"] = "ok"
    return m.run_session(str(prog), "quick", 1, 5.0, "EEVDF", tmp / "base.json")


def check_outputs(m, tmp, run, fails):
    before = len(fails)
    m.LOG_DIR = tmp
    slower = json.loads(json.dumps(run))
    slower["wake-pingpong"]["rtt_blocked_p50"]["value"] = 15.42
    arms = ["EEVDF", "PANDEMONIUM (BPF)"]
    results = {"EEVDF": {"wake-pingpong": [run["wake-pingpong"], run["wake-pingpong"]]},
               "PANDEMONIUM (BPF)": {"wake-pingpong": [slower["wake-pingpong"]] * 2}}
    git = {"commit": "abc1234", "dirty": False}
    report = m.write_report("5.25.0", git, "20261113-160000", 12, "0.2.1", "quick",
                            5.0, 1, results, arms, 2, {}, tmp / "raw").read_text()
    lines = report.splitlines()
    for want in ("HELICOPRION: 0.2.1, suite quick, 5s runs after 1 warm-up",
                 "             by Piotr Gorski, https://github.com/Seafoam-Labs/helicoprion",
                 "WORKLOAD wake-pingpong"):
        if want not in lines:
            fails.append(f"report: missing {want!r}")
    for label in ("rtt_blocked_p50 [us ↓]", "roundtrips_per_s [1/s ↑]",
                  "rtt_blocked_max [us ↓ single sample]", "hog_work_per_s [units/s info]"):
        if not any(l.startswith(label) for l in lines):
            fails.append(f"report: no row {label!r}")
    vs = [l for l in lines if l.startswith("vs EEVDF ")]
    if not any(l.startswith("vs EEVDF rtt_blocked_p50") and "+100.0%" in l for l in vs):
        fails.append(f"report: rtt_blocked_p50 is not +100.0% against EEVDF: {vs}")
    for unscored in ("rtt_blocked_max", "hog_work_per_s", "rtt_blocked_same_cpu_p50"):
        if any(unscored in l for l in vs):
            fails.append(f"report: {unscored} has a vs-EEVDF row")
    if not any("° a population under 1%" in l for l in lines):
        fails.append("report: the minor population is not footnoted")

    prom = m.write_prometheus("5.25.0", git, "20261113-160000", 12, "0.2.1", "quick",
                              results, {}).read_text()
    for want in ('pandemonium_helicoprion_rtt_blocked_p99_9{scheduler="EEVDF",'
                 'workload="wake-pingpong",stat="mean"} 22.700000',
                 'pandemonium_helicoprion_rtt_blocked_p50{scheduler="PANDEMONIUM (BPF)",'
                 'workload="wake-pingpong",stat="mean"} 15.420000'):
        if want not in prom.splitlines():
            fails.append(f"prom: missing {want}")
    if 'helicoprion_version="0.2.1"' not in prom:
        fails.append("prom: the info line does not carry helicoprion's version")

    it = tmp / "iter-1.prom"
    m.write_iteration_prom(it, "5.25.0", git, {("EEVDF", "wake-pingpong"): run["wake-pingpong"]})
    text = it.read_text()
    for want in ('pandemonium_helicoprion_higher_roundtrips_per_s{scheduler="EEVDF",'
                 'workload="wake-pingpong"} 1090000.000000',
                 'pandemonium_helicoprion_lower_rtt_blocked_p50{scheduler="EEVDF",'
                 'workload="wake-pingpong"} 7.710000'):
        if want not in text.splitlines():
            fails.append(f"iteration prom: missing {want}")
    for unscored in ("rtt_blocked_max", "hog_work_per_s", "rtt_blocked_same_cpu_p50"):
        if unscored in text:
            fails.append(f"iteration prom: {unscored} reached montauk")
    if len(fails) == before:
        note("PASS the report and .prom carry what helicoprion scores")


def main() -> int:
    m = load()
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="prism-helicoprion-gate-") as td:
        tmp = Path(td)
        check_consent(m, fails)
        check_source(m, fails)
        run = check_session(m, tmp, fails)
        if run:
            check_outputs(m, tmp, run, fails)
        else:
            fails.append("session: the stand-in produced no sample to report on")
    for f in fails:
        note(f"FAIL {f}")
    note("GATE PASSED: prism runs the installed helicoprion and installs nothing unasked"
         if not fails else "GATE FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
