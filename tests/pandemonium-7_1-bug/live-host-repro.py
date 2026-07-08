#!/usr/bin/env python3
# live-host-repro.py -- bare-metal counterpart to orchestrate-io.py, for a host
# where PANDEMONIUM is already the live sched_ext scheduler on the real
# affected kernel. Two induction workloads:
#   lockblock -- the synthetic mutex-held-across-O_DIRECT-write workload used
#                in the VM harness's deliberate reproduction.
#   kernbuild -- a real `make defconfig` kernel build (distclean, defconfig,
#                then `make -j$(nproc) vmlinux`), the same real-world workload
#                CachyOS's own benchmarker times as its "kernel defconfig"
#                benchmark. Real fork/exec churn from thousands of short-lived
#                gcc/ld/as processes and real page-cache I/O, not a synthetic
#                approximation -- this is the closer match if the actual field
#                reports this bug is based on came from that benchmark.
#
# This has no qemu/Docker safety net. A real freeze here is a real freeze of
# this machine, not a disposable guest -- there is no container to reap. Each
# repeat is bounded by a subprocess timeout, but if the whole system genuinely
# locks up, this script stops being scheduled too and nothing further gets
# logged until the machine recovers or is power-cycled. Run only with that
# understood and accepted.
#
# Signature detection uses `journalctl -k --since/--until` (no root needed on
# this host) in place of the VM harness's console.log; there is no debugfs
# events.bin/kwin_overruns.txt to extract here, so kwin_p99/wake2run_p99
# metrics are not produced -- only kernel-signature detection is.

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SRC = HERE / "guest" / "pand-lock-block.c"
BIN_DIR = Path("/tmp/pandemonium-live-repro")
BIN = BIN_DIR / "pand-lock-block"
REPORTS = Path.home() / ".cache" / "pandemonium" / "7_1-bug" / "reports" / "live"
DEFAULT_KERNEL_SRC = (Path.home() / ".cache" / "pandemonium" / "7_1-bug" /
                      "kernel-src" / "sched_ext")

SIGNATURE_GREP = [
    "watchdog failed", "soft lockup", "hard lockup", "BUG:",
    "sched_ext:.*disabled", "failed to run for",
]


def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("[%H:%M:%S]")
    print(f"{ts} [{level}]   {msg}", flush=True)


def build_binary() -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if BIN.is_file() and BIN.stat().st_mtime >= SRC.stat().st_mtime:
        return
    subprocess.run(["gcc", "-O2", "-Wall", "-o", str(BIN), str(SRC), "-lpthread"],
                    check=True)
    log(f"compiled {BIN}")


def check_prereqs() -> None:
    state = Path("/sys/kernel/sched_ext/state")
    if not state.is_file() or state.read_text().strip() != "enabled":
        sys.exit("sched_ext is not enabled on this host -- refusing to proceed")
    ops = Path("/sys/kernel/sched_ext/root/ops").read_text().strip()
    log(f"active scheduler: {ops}")
    if ops != "pandemonium":
        log(f"active scheduler is '{ops}', not pandemonium -- results may not "
            "reflect PANDEMONIUM's own dispatch path", "WARN")


def kernel_signatures_since(since: str, until: str) -> str:
    pattern = "|".join(SIGNATURE_GREP)
    r = subprocess.run(
        ["journalctl", "-k", "--since", since, "--until", until, "--no-pager",
         "-o", "short-iso"],
        capture_output=True, text=True)
    sig = subprocess.run(["grep", "-iE", pattern], input=r.stdout,
                          capture_output=True, text=True)
    return sig.stdout.strip()


def run_once(rep: int, duration: int, threads: int, block_kb: int,
             scratch: Path, iops: int) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    start = datetime.datetime.now().astimezone()
    start_str = start.isoformat()
    result = {"rep": rep, "start": start_str}
    try:
        p = subprocess.run(
            [str(BIN), str(duration), str(threads), str(block_kb), str(scratch),
             str(iops)],
            timeout=duration + 30)
        result["exit_code"] = p.returncode
        result["froze"] = False
    except subprocess.TimeoutExpired:
        # The workload process itself did not return within duration+30s.
        # On a genuine full-system freeze this code may never run at all --
        # this branch only covers the milder case where the workload hangs
        # (e.g. a stuck D-state thread) but the host and this script are
        # still scheduled.
        result["exit_code"] = None
        result["froze"] = True
        log(f"rep {rep}: workload did not return within {duration + 30}s -- "
            "treating as a hang", "WARN")
    end = datetime.datetime.now().astimezone()
    result["end"] = end.isoformat()
    sig = kernel_signatures_since(start_str, end.isoformat())
    result["signature"] = sig
    return result


def run_kernbuild_once(rep: int, kernel_src: Path, jobs: int,
                        max_minutes: int) -> dict:
    start = datetime.datetime.now().astimezone()
    start_str = start.isoformat()
    result = {"rep": rep, "start": start_str}
    # The kernel's own build scripts shell out with `$PWD` in places; PWD is
    # inherited from this process's environment, not derived from the cwd=
    # argument below, so a stale PWD from wherever this script was launched
    # (a path with spaces breaks that unquoted shell usage) leaks through
    # unless overridden explicitly here.
    build_env = dict(os.environ, PWD=str(kernel_src))
    try:
        # distclean + defconfig: real prep, not timed as the stress itself,
        # but still real fork/exec/I/O and still worth monitoring.
        subprocess.run(["make", "-s", "distclean"], cwd=kernel_src, check=True,
                        timeout=300, env=build_env)
        subprocess.run(["make", "-s", "defconfig"], cwd=kernel_src, check=True,
                        timeout=120, env=build_env)
        p = subprocess.run(
            ["make", "KCFLAGS=-Wno-error", f"-sj{jobs}", "vmlinux"],
            cwd=kernel_src, timeout=max_minutes * 60, env=build_env)
        result["exit_code"] = p.returncode
        result["froze"] = False
    except subprocess.TimeoutExpired:
        # Same caveat as run_once(): this only catches "the build didn't
        # finish in time" or "a subprocess hung but the host is still up." A
        # genuine full-system freeze means this code may never run at all.
        result["exit_code"] = None
        result["froze"] = True
        log(f"rep {rep}: kernel build did not return within the bound -- "
            "treating as a hang", "WARN")
    except subprocess.CalledProcessError as e:
        result["exit_code"] = e.returncode
        result["froze"] = False
        log(f"rep {rep}: prep step ({' '.join(e.cmd)}) failed, rc={e.returncode}",
            "WARN")
    end = datetime.datetime.now().astimezone()
    result["end"] = end.isoformat()
    sig = kernel_signatures_since(start_str, end.isoformat())
    result["signature"] = sig
    return result


def write_report(result: dict, tag: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{tag}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", choices=["lockblock", "kernbuild"],
                    default="lockblock")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--block-kb", type=int, default=4)
    ap.add_argument("--scratch", type=Path, default=BIN_DIR / "scratch")
    ap.add_argument("--iops", type=int, default=0,
                    help="throttle the shared file's write rate (0 = unthrottled); "
                         "real, untethered storage may complete each write too "
                         "fast for the mutex hold time to matter without this")
    ap.add_argument("--kernel-src", type=Path, default=DEFAULT_KERNEL_SRC,
                    help="kernel tree for --workload kernbuild; defaults to "
                         "this investigation's already-cloned sched_ext tree")
    ap.add_argument("--jobs", type=int, default=0,
                    help="parallel make jobs for --workload kernbuild "
                         "(0 = nproc)")
    ap.add_argument("--max-minutes", type=int, default=45,
                    help="upper bound on one kernbuild repeat before it's "
                         "treated as a hang")
    args = ap.parse_args()

    check_prereqs()

    hits = 0
    froze = 0
    for i in range(args.repeats):
        if args.workload == "kernbuild":
            jobs = args.jobs or len(os.sched_getaffinity(0))
            log(f"rep {i + 1}/{args.repeats}: launching kernbuild "
                f"(jobs={jobs}, src={args.kernel_src})")
            result = run_kernbuild_once(i + 1, args.kernel_src, jobs,
                                         args.max_minutes)
        else:
            build_binary()
            log(f"rep {i + 1}/{args.repeats}: launching lockblock "
                f"(duration={args.duration}s threads={args.threads} "
                f"iops={args.iops})")
            result = run_once(i + 1, args.duration, args.threads, args.block_kb,
                               args.scratch, args.iops)
        tag = f"live_{time.strftime('%Y%m%d-%H%M%S')}_{i}"
        write_report(result, tag)
        if result["froze"]:
            froze += 1
        if result["signature"]:
            hits += 1
            log(f"rep {i + 1}: SIGNATURE HIT", "WARN")
            for line in result["signature"].splitlines():
                log(f"  {line}", "WARN")
        else:
            log(f"rep {i + 1}: clean, exit_code={result['exit_code']}")
        if result["froze"]:
            log(f"rep {i + 1} did not return cleanly -- stopping rather than "
                "launching another repeat on top of a possibly-wedged host",
                "WARN")
            break

    log(f"done: {hits}/{i + 1} signature hits, {froze}/{i + 1} hung, "
        f"reports under {REPORTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
