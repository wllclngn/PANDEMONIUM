#!/usr/bin/env python3
# vmm.py -- the vm-matrix command line.
#
#   vmm.py check            the host can build and boot a matrix
#   vmm.py show TICKET      the effective ticket and each cell's expectation
#   vmm.py build TICKET     every kernel, scheduler and initramfs the matrix needs
#   vmm.py run TICKET       build, boot every cell (kernel-major, reps inner), report
#   vmm.py report TICKET    re-judge the recorded runs against the ticket
#
# TICKET is a .toml path, or - with --ticket-id ID for a matrix given only by
# --kernel and --scheduler flags. run and report exit 0 only when every cell
# MATCHes.

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rig import report, spec  # noqa: E402
from rig.common import BUILD, CACHE, MONTAUK, SIBLING, log, slug  # noqa: E402

GiB = 1 << 30
BUILD_WARN_GIB = 15
CACHE_WARN_GIB = 5
SIBLING_NEEDS = ("Dockerfile.builder", "guest/pand-strand-load.c", "guest/pand-io-flood.c",
                 "guest/pand-idle-ping.c", "guest/pand-lock-block.c")


def _labels(values) -> list[str]:
    return [x for v in values or () for x in v.split(",") if x]


def effective(args) -> spec.Ticket:
    if args.ticket == "-":
        if not args.ticket_id:
            raise spec.SpecError("TICKET - needs --ticket-id")
        t = spec.Ticket(id=args.ticket_id)
    else:
        t = spec.load(Path(args.ticket))
        if args.ticket_id:
            t = replace(t, id=args.ticket_id)
    kernels = [spec.parse_kernel_arg(a) for a in args.kernel or ()]
    schedulers = [spec.parse_scheduler_arg(a) for a in args.scheduler or ()]
    only_k, only_s = _labels(args.only_kernel), _labels(args.only_scheduler)

    all_k = [k.label for k in t.kernels] + [k.label for k in kernels]
    all_s = [s.label for s in t.schedulers] + [s.label for s in schedulers]
    for flag, only, known in (("--only-kernel", only_k, all_k), ("--only-scheduler", only_s, all_s)):
        bad = sorted(set(only) - set(known))
        if bad:
            raise spec.SpecError(f"{flag}: unknown labels {bad}; known: {', '.join(dict.fromkeys(known))}")

    run = {"reps": args.reps, "duration": args.duration, "smp": args.smp,
           "threads": args.threads, "numa": args.numa, "mem": args.mem,
           "timeout": args.timeout, "stall_s": args.stall,
           "phases": tuple(_labels([args.phases])) if args.phases is not None else None,
           "kvm": False if args.no_kvm else None,
           "pin_vcpus": True if args.pin_vcpus else None,
           "stream": True if args.stream else None}
    return spec.with_overrides(t, kernels, schedulers, only_k, only_s, **run)


def narrowed(args) -> tuple[str, str]:
    """(file tag, words) for a report over --only-* labels; ("", "") for the
    whole matrix."""
    only_k, only_s = _labels(args.only_kernel), _labels(args.only_scheduler)
    if not (only_k or only_s):
        return "", ""
    tag = f"{slug('+'.join(only_k) or 'all')}__{slug('+'.join(only_s) or 'all')}"
    return tag, (f"kernels {', '.join(only_k) or 'all'} and "
                 f"schedulers {', '.join(only_s) or 'all'}")


def _existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _mount(p: Path) -> tuple[str, str]:
    """(mount point, fs type) of the filesystem holding p."""
    target = str(_existing(p).resolve())
    best = ("/", "?")
    for line in Path("/proc/self/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])
        inside = target == mnt or target.startswith(mnt.rstrip("/") + "/")
        if inside and len(mnt) >= len(best[0]):
            best = (mnt, fields[2])
    return best


def _free(p: Path) -> float:
    st = os.statvfs(_existing(p))
    return st.f_bavail * st.f_frsize / GiB


def _version(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"({type(e).__name__})"
    return (r.stdout.strip().splitlines() or [""])[0]


def cmd_check(args) -> int:
    rows = []

    def add(status: str, name: str, detail: str) -> None:
        rows.append((status, name, detail))

    if not shutil.which("docker"):
        add("FAIL", "docker", "not on PATH")
    else:
        try:
            r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                add("ok", "docker", f"server {r.stdout.strip()}")
            else:
                add("FAIL", "docker", "daemon unreachable: "
                    + ((r.stderr.strip().splitlines() or ["no output"])[-1]))
        except subprocess.TimeoutExpired:
            add("FAIL", "docker", "daemon gave no answer in 20 s")

    kvm = Path("/dev/kvm")
    if not kvm.exists():
        add("FAIL", "/dev/kvm", "missing (load kvm_amd or kvm_intel, or run with --no-kvm)")
    elif os.access(kvm, os.R_OK | os.W_OK):
        add("ok", "/dev/kvm", "read-write")
    else:
        add("FAIL", "/dev/kvm", f"not read-write for uid {os.getuid()} (join the kvm group)")

    for tool in ("git", "cargo", "clang"):
        path = shutil.which(tool)
        if path:
            add("ok", tool, f"{path}, {_version([tool, '--version'])}")
        else:
            add("FAIL", tool, "not on PATH")

    if MONTAUK.is_file() and os.access(MONTAUK, os.X_OK):
        add("ok", "montauk", str(MONTAUK))
    else:
        add("FAIL", "montauk", f"{MONTAUK} missing or not executable")

    for name, path, warn in (("build space", BUILD, BUILD_WARN_GIB), ("cache space", CACHE, CACHE_WARN_GIB)):
        mnt, fstype = _mount(path)
        free = _free(path)
        detail = f"{free:.1f} GiB free on {fstype} {mnt} ({path})"
        if free < warn:
            add("WARN", name, f"{detail}, under {warn} GiB")
        else:
            add("ok", name, detail)

    missing = [n for n in SIBLING_NEEDS if not (SIBLING / n).is_file()]
    if not SIBLING.is_dir():
        add("FAIL", "sibling suite", f"{SIBLING} missing")
    elif missing:
        add("FAIL", "sibling suite", f"{SIBLING} lacks {', '.join(missing)}")
    else:
        add("ok", "sibling suite", str(SIBLING))

    print("\n".join(report.table(("status", "check", "detail"), rows, "")))
    n = {s: sum(1 for r in rows if r[0] == s) for s in ("ok", "WARN", "FAIL")}
    print(f"checks: {n['ok']} ok, {n['WARN']} warned, {n['FAIL']} failed")
    return 1 if n["FAIL"] else 0


def cmd_show(args) -> int:
    t = effective(args)
    sys.stdout.write(spec.to_toml(t))
    rows = []
    for k, s in t.cells():
        e = t.expectation(k.label, s.label)
        rows.append((k.label, s.label, e.veristat or "-", ", ".join(e.fail_progs) or "-",
                     e.error or "-", e.attach or "-", e.phases or "-", e.detach or "-"))
    # Comments, so the whole output still loads as a ticket.
    print(f"\n# cells: {len(rows)}, each with its merged expectation")
    for line in report.table(("kernel", "scheduler", "veristat", "fail_progs", "error",
                              "attach", "phases", "detach"), rows, ""):
        print(f"# {line}".rstrip())
    return 0


def build_all(t: spec.Ticket, keep_build: bool):
    from rig import guest, kernels, schedulers

    bk = {}
    for k in t.kernels:
        bk[k.label] = kernels.ensure(k, keep_build=keep_build)
    bs = {}
    for s in t.schedulers:
        bs[s.label] = schedulers.ensure(s, keep_build=keep_build)
        log(f"scheduler {s.label}: {bs[s.label].kind}, {len(bs[s.label].objects)} BPF objects")
    initrds = {}
    for k, s in t.cells():
        initrds[(k.label, s.label)] = guest.initramfs(bk[k.label], bs[s.label])
        log(f"initramfs {k.label} x {s.label}: {initrds[(k.label, s.label)][0]}")
    return bk, bs, initrds


def cmd_build(args) -> int:
    t = effective(args)
    build_all(t, args.keep_build)
    log(f"{t.id}: {len(t.kernels)} kernels, {len(t.schedulers)} schedulers, "
        f"{len(t.cells())} initramfs images ready")
    return 0


def _finish(t: spec.Ticket, args) -> int:
    txt, md, all_match = report.build(t, *narrowed(args))
    sys.stdout.write(report.overview(txt))
    print(f"report:  {txt}\nsummary: {md}")
    return 0 if all_match else 1


def cmd_run(args) -> int:
    t = effective(args)
    from rig import run as rrun

    bk, bs, initrds = build_all(t, args.keep_build)
    failed = 0
    for k, s in t.cells():
        initrd, ident = initrds[(k.label, s.label)]
        for rep in range(t.run.reps):
            log(f"cell {k.label} x {s.label} rep{rep}: booting")
            # One cell that cannot run does not cost the matrix its report.
            try:
                r = rrun.run_cell(t, bk[k.label], bs[s.label], initrd, ident, rep)
            except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as e:
                failed += 1
                log(f"cell {k.label} x {s.label} rep{rep}: could not run: "
                    f"{type(e).__name__}: {e}", "ERROR")
                continue
            v = report.evaluate(t, r)["verdict"]
            log(f"cell {k.label} x {s.label} rep{rep}: {v} ({r.reason} after {r.seconds:.0f} s) {r.dir}",
                "INFO" if v == "MATCH" else "WARN")
    rc = _finish(t, args)
    return 1 if failed else rc


def cmd_report(args) -> int:
    return _finish(effective(args), args)


def cmd_bisect(args) -> int:
    from rig import bisect

    t = effective(args)
    r = bisect.bisect(t, args.sched, args.good, args.bad, args.url, tuple(args.mirror or ()),
                      tuple(args.path or ()), args.prog, args.error or "")
    for s in r["steps"]:
        print(f"  {s['verdict']:4}  {s['subject']}  ({s['why']})")
    if r["first_bad"]:
        print(f"\nfirst bad commit: {r['first_bad']}\n")
        print(r["first_bad_show"])
        return 0
    log(f"bisect stopped without a first bad commit: {r['stopped']}", "WARN")
    return 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vmm.py", description="QEMU/KVM test matrix for sched_ext schedulers")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="host prerequisites, one line each")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("ticket", metavar="TICKET", help="ticket .toml, or - with --ticket-id")
    common.add_argument("--ticket-id", metavar="ID", help="ticket id for -, or a new id for a .toml")
    common.add_argument("--kernel", action="append", metavar="SPEC",
                        help="LABEL=git:URL@SHA[#REF][,mirror=URL][,config=CONFIG_X=y][,cc=clang] "
                             "or LABEL=image:PATH[,modules=DIR]; options repeat, values carry no comma")
    common.add_argument("--scheduler", action="append", metavar="SPEC",
                        help="LABEL=cargo:DIR or LABEL=cargo:REPO|URL@COMMIT[:SUBDIR] with "
                             "[,bin=,pkg=,ops=,ref=,arg=,sha256=REL:HEX,mirror=,input=], "
                             "LABEL=binary:PATH[,obj=,arg=,ops=,sha256=] or LABEL=none[,arg=,ops=]")
    common.add_argument("--only-kernel", action="append", metavar="L", help="narrow to these kernel labels")
    common.add_argument("--only-scheduler", action="append", metavar="L",
                        help="narrow to these scheduler labels")
    common.add_argument("--reps", type=int)
    common.add_argument("--duration", type=int, metavar="S", help="seconds per load phase")
    common.add_argument("--smp", type=int)
    common.add_argument("--threads", type=int, help="SMT threads per guest core")
    common.add_argument("--numa", type=int, help="guest NUMA nodes")
    common.add_argument("--mem", metavar="SIZE")
    common.add_argument("--phases", metavar="A,B", help=f"subset of {','.join(spec.PHASES)}")
    common.add_argument("--timeout", type=int, metavar="S", help="wall seconds per cell")
    common.add_argument("--stall", type=int, metavar="S",
                        help=f"reap a guest silent on every port this long; 0 off, else >= {spec.MIN_STALL}")
    common.add_argument("--stream", action="store_true",
                        help="guest montauk also streams to ttyS2, so a capture survives a hang "
                             "(montauk then keeps events at the port's rate)")
    common.add_argument("--no-kvm", action="store_true", help="TCG instead of KVM")
    common.add_argument("--pin-vcpus", action="store_true")
    common.add_argument("--keep-build", action="store_true", help="keep kernel worktrees and objects")
    for name, text in (("show", "the effective ticket and each cell's expectation"),
                       ("build", "build every kernel, scheduler and initramfs"),
                       ("run", "build, boot every cell, report"),
                       ("report", "re-judge the recorded runs")):
        sub.add_parser(name, parents=[common], help=text)
    b = sub.add_parser("bisect", parents=[common],
                       help="find the kernel commit that turns a program's veristat verdict")
    b.add_argument("--sched", required=True, metavar="L", help="the ticket scheduler whose objects are verified")
    b.add_argument("--good", required=True, metavar="SHA", help="full commit the program verifies on")
    b.add_argument("--bad", required=True, metavar="SHA", help="full commit it is rejected on")
    b.add_argument("--url", required=True, help="kernel repository the commits come from")
    b.add_argument("--mirror", action="append", metavar="URL", help="remote that serves fetch-by-SHA")
    b.add_argument("--path", action="append", metavar="P", help="limit candidates to commits touching P")
    b.add_argument("--prog", required=True, metavar="NAME", help="the BPF program judged")
    b.add_argument("--error", metavar="TEXT", help="a rejection without this text is skipped, not bad")
    return p


def main() -> int:
    args = parser().parse_args()
    cmds = {"check": cmd_check, "show": cmd_show, "build": cmd_build,
            "run": cmd_run, "report": cmd_report, "bisect": cmd_bisect}
    try:
        return cmds[args.cmd](args)
    except spec.SpecError as e:
        log(str(e), "ERROR")
        return 2
    except RuntimeError as e:
        log(str(e), "ERROR")
        return 1
    except subprocess.CalledProcessError as e:
        argv = e.cmd if isinstance(e.cmd, str) else " ".join(map(str, e.cmd))
        log(f"{argv} exited {e.returncode}", "ERROR")
        return 1
    except (OSError, ValueError) as e:
        log(f"{type(e).__name__}: {e}", "ERROR")
        return 1
    except KeyboardInterrupt:
        log("interrupted", "WARN")
        return 130


if __name__ == "__main__":
    sys.exit(main())
