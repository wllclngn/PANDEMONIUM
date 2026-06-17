#!/usr/bin/env python3
"""
PANDEMONIUM bench-cachyos: CachyOS Mini-Benchmarker-style application suite.

Models the Mini-Benchmarker workload set the CachyOS project uses for
cross-kernel scheduler comparisons. Each workload runs N iterations under
each scheduler, captures wall-clock time, and reports mean + stddev
alongside per-row winners.

Workloads (12 total, 8 runnable from standard packages, 4 require manual
asset setup):

    stress-ng-cpu-cache-mem   stress-ng cache stressor, fixed ops.
    perf-sched-msg-fork-thread perf bench sched messaging, fixed loops.
    perf-memcpy               perf bench mem memcpy, fixed size.
    argon2-hashing            argon2 -t T -m M -p P.
    xz-compression            xz -3 on a stable test corpus.
    primes                    stress-ng cpu prime method, fixed ops.
    x265-encoding             ffmpeg encode of lavfi testsrc clip.
    ffmpeg-compilation        git clone (once) then make -j$(nproc).

    blender-render            requires blender + scene file (skip)
    kernel-defconfig          requires kernel source clone (skip)

Usage:
    ./tests/bench-cachyos.py
    ./tests/bench-cachyos.py --runs 5
    ./tests/bench-cachyos.py --workloads xz-compression,primes
    ./tests/bench-cachyos.py --schedulers scx_cake
    ./tests/bench-cachyos.py --all-scx
    ./tests/bench-cachyos.py --pandemonium-only

Prompts for sudo on entry; credentials are cached for the run and
refreshed between schedulers.
"""

import argparse
import multiprocessing
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
from pandemonium_common import (
    ARCHIVE_DIR, BINARY, LOG_DIR,
    get_git_info, get_version,
    is_scx_active, log, log_error, log_info, log_warn,
    mean_stdev, montauk_available, montauk_trace, scx_scheduler_name,
    wait_for_deactivation, PrometheusBuilder,
)

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler


def _warm_sudo() -> None:
    """Prompt for sudo password if not cached. Subsequent sudo calls in
    this script will use the cached credentials. Mirrors bench-power.py."""
    r = subprocess.run(["sudo", "true"])
    if r.returncode != 0:
        log_error("sudo authentication failed")
        sys.exit(1)


def _refresh_sudo() -> None:
    """Refresh cached sudo credentials between long workloads."""
    subprocess.run(["sudo", "-v"], capture_output=True)

# ASSET DIRECTORY. PERSISTENT TEST CORPUS, GENERATED FFMPEG CLONE,
# AND GENERATED XZ INPUT FILE LIVE HERE BETWEEN INVOCATIONS.
ASSET_DIR = LOG_DIR / "bench-cachyos-assets"
XZ_CORPUS = ASSET_DIR / "xz-corpus.bin"
FFMPEG_SRC = ASSET_DIR / "ffmpeg-src"
X265_OUTPUT = ASSET_DIR / "x265-output.hevc"

# DEFAULTS
DEFAULT_RUNS = 3
DEFAULT_TIMEOUT_S = 600


@dataclass
class Workload:
    name: str
    label: str
    probe: Callable[[], bool]
    probe_hint: str
    run: Callable[[], Optional[float]]


# WORKLOAD IMPLEMENTATIONS

def _run_timed(cmd: list[str], timeout: int = DEFAULT_TIMEOUT_S,
               cwd: Optional[Path] = None) -> Optional[float]:
    """Run a command, return wall-clock elapsed seconds (None on failure)."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        log_error(f"  timeout after {timeout}s: {' '.join(cmd[:4])}...")
        return None
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").splitlines()[-3:]
        log_error(f"  exit {result.returncode}: {' '.join(cmd[:4])}...")
        for ln in tail:
            log_error(f"    {ln}")
        return None
    return elapsed


def run_stress_ng_cache(ncpus: int) -> Optional[float]:
    # CACHE STRESSOR. FIXED OPS FOR REPRODUCIBLE WORK.
    # --cache-enable-all TURNS ON FENCE / FLUSH / PREFETCH / SFENCE
    # AND THE x86 cldemote/clflushopt/clwb VARIANTS IN ONE FLAG.
    ops = max(50000, 500000 // max(1, ncpus // 4))
    return _run_timed([
        "stress-ng", "--cache", str(ncpus),
        "--cache-enable-all",
        "--metrics-brief", "--cache-ops", str(ops),
    ], timeout=300)


def run_perf_sched(ncpus: int) -> Optional[float]:
    # PERF BENCH SCHED MESSAGING, FIXED-WORK INVOCATION.
    # MATCHES THE bench-fork-thread.py DEFAULT (-g 24 -l 6000) AT 12C
    # BUT SCALES THE LOOP COUNT DOWN AT LOWER CORE COUNTS.
    groups = 24
    loops = 6000
    cmd = ["perf", "bench", "-f", "simple", "sched", "messaging",
           "-t", "-g", str(groups), "-l", str(loops)]
    start = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log_error("  perf sched timed out")
        return None
    elapsed = time.monotonic() - start
    # PERF BENCH SIMPLE PRINTS THE ELAPSED TIME ON STDOUT.
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        if result.returncode != 0:
            log_error(f"  perf sched exit {result.returncode}")
            return None
        return elapsed


def run_perf_memcpy(_ncpus: int) -> Optional[float]:
    # PERF MEM MEMCPY, FIXED 1GB SIZE OVER 16 ITERATIONS.
    cmd = ["perf", "bench", "-f", "simple", "mem", "memcpy",
           "-s", "1GB", "-l", "16"]
    start = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log_error("  perf memcpy timed out")
        return None
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        log_error(f"  perf memcpy exit {result.returncode}")
        return None
    return elapsed


def run_argon2(_ncpus: int) -> Optional[float]:
    # ARGON2 HASHING. FIXED ITERATIONS / MEMORY / PARALLELISM.
    proc = subprocess.Popen(
        ["argon2", "pandemonium-salt", "-i",
         "-t", "100", "-m", "16", "-p", "4", "-l", "32"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    start = time.monotonic()
    try:
        _, err = proc.communicate(input="pandemonium-test-password\n",
                                  timeout=DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        log_error("  argon2 timed out")
        return None
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        log_error(f"  argon2 exit {proc.returncode}: {err.strip()}")
        return None
    return elapsed


def _ensure_xz_corpus() -> bool:
    """Generate a stable 256MB test corpus from /usr binaries on first run."""
    if XZ_CORPUS.exists() and XZ_CORPUS.stat().st_size > 200 * 1024 * 1024:
        return True
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    log_info(f"generating xz corpus at {XZ_CORPUS} (~256MB)...")
    # CONCATENATE /usr/bin BINARIES UNTIL WE HIT ~256MB. STABLE INPUT
    # ACROSS RUNS BECAUSE THE BINARY SET DOESN'T CHANGE BETWEEN BENCHES.
    target_bytes = 256 * 1024 * 1024
    written = 0
    try:
        with XZ_CORPUS.open("wb") as out:
            for d in ("/usr/bin", "/usr/lib"):
                if not Path(d).is_dir():
                    continue
                for entry in sorted(Path(d).iterdir()):
                    if not entry.is_file():
                        continue
                    try:
                        data = entry.read_bytes()
                    except (PermissionError, OSError):
                        continue
                    out.write(data)
                    written += len(data)
                    if written >= target_bytes:
                        break
                if written >= target_bytes:
                    break
    except OSError as e:
        log_error(f"  failed to build xz corpus: {e}")
        return False
    log_info(f"  xz corpus ready: {written // (1024*1024)} MB")
    return True


def run_xz(_ncpus: int) -> Optional[float]:
    if not _ensure_xz_corpus():
        return None
    # COMPRESS THE CORPUS TO /dev/null. -T 0 USES ALL THREADS;
    # -3 IS A MID-LEVEL SETTING THAT FINISHES IN A FEW SECONDS.
    cmd = ["xz", "-z", "-k", "-c", "-T", "0", "-3", str(XZ_CORPUS)]
    start = time.monotonic()
    try:
        with open("/dev/null", "wb") as devnull:
            result = subprocess.run(cmd, stdout=devnull, stderr=subprocess.PIPE,
                                    timeout=DEFAULT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log_error("  xz timed out")
        return None
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        log_error(f"  xz exit {result.returncode}")
        return None
    return elapsed


def run_primes(ncpus: int) -> Optional[float]:
    # STRESS-NG PRIME METHOD, FIXED OPS PER WORKER.
    ops = max(10000, 200000 // max(1, ncpus // 4))
    return _run_timed([
        "stress-ng", "--cpu", str(ncpus), "--cpu-method", "prime",
        "--cpu-ops", str(ops), "--metrics-brief",
    ], timeout=300)


def run_x265(_ncpus: int) -> Optional[float]:
    # X265 ENCODE OF A GENERATED 10S 1080P30 TESTSRC CLIP.
    # NO SOURCE VIDEO REQUIRED -- LAVFI SYNTHESIZES FRAMES.
    if X265_OUTPUT.exists():
        X265_OUTPUT.unlink()
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:duration=10:rate=30",
        "-c:v", "libx265", "-preset", "medium", "-x265-params", "log-level=error",
        "-y", str(X265_OUTPUT),
    ]
    return _run_timed(cmd, timeout=DEFAULT_TIMEOUT_S)


def _ensure_ffmpeg_src() -> bool:
    if (FFMPEG_SRC / "configure").is_file():
        return True
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    log_info(f"cloning ffmpeg source to {FFMPEG_SRC} (one-time)...")
    cmd = ["git", "clone", "--depth", "1",
           "https://git.ffmpeg.org/ffmpeg.git", str(FFMPEG_SRC)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log_error("  ffmpeg clone timed out")
        return False
    if result.returncode != 0:
        log_error(f"  ffmpeg clone failed: {result.stderr.strip()[:200]}")
        return False
    # ONE-TIME CONFIGURE. CACHED FOR ALL SUBSEQUENT make INVOCATIONS.
    log_info("  running ffmpeg ./configure (one-time)...")
    cfg = subprocess.run(
        ["./configure", "--disable-debug", "--disable-doc",
         "--disable-x86asm", "--enable-pthreads", "--disable-stripping"],
        capture_output=True, text=True, cwd=FFMPEG_SRC, timeout=600,
    )
    if cfg.returncode != 0:
        log_error(f"  ffmpeg configure failed: {cfg.stderr.strip()[:200]}")
        return False
    return True


def run_ffmpeg_compile(ncpus: int) -> Optional[float]:
    if not _ensure_ffmpeg_src():
        return None
    # CLEAN PRIOR OBJECTS FIRST (NOT TIMED).
    subprocess.run(["make", "clean", "-s"], cwd=FFMPEG_SRC,
                   capture_output=True, timeout=60)
    cmd = ["make", f"-j{ncpus}", "-s"]
    return _run_timed(cmd, cwd=FFMPEG_SRC, timeout=1800)


# SKIPPED WORKLOADS: SHIPPED AS PROBES THAT EXPLAIN WHAT'S MISSING.

def _probe_blender() -> bool:
    return (shutil.which("blender") is not None
            and Path.home().joinpath("blender-bench/scene.blend").is_file())


def _probe_kernel_defconfig() -> bool:
    return Path.home().joinpath("linux-bench/Makefile").is_file()


def run_blender(_ncpus: int) -> Optional[float]:
    scene = str(Path.home() / "blender-bench" / "scene.blend")
    return _run_timed(["blender", "-b", scene, "-f", "1"], timeout=900)


def run_kernel_defconfig(ncpus: int) -> Optional[float]:
    src = Path.home() / "linux-bench"
    subprocess.run(["make", "clean", "-s"], cwd=src,
                   capture_output=True, timeout=60)
    subprocess.run(["make", "defconfig", "-s"], cwd=src,
                   capture_output=True, timeout=60)
    return _run_timed(["make", f"-j{ncpus}", "-s"], cwd=src, timeout=1800)


# WORKLOAD REGISTRY

def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


WORKLOADS = [
    Workload("stress-ng-cpu-cache-mem", "stress-ng cpu-cache-mem",
             lambda: _have("stress-ng"),
             "install stress-ng",
             run_stress_ng_cache),
    Workload("perf-sched-msg-fork-thread", "perf sched msg fork thread",
             lambda: _have("perf"),
             "install perf (linux-tools)",
             run_perf_sched),
    Workload("perf-memcpy", "perf memcpy",
             lambda: _have("perf"),
             "install perf (linux-tools)",
             run_perf_memcpy),
    Workload("argon2-hashing", "argon2 hashing",
             lambda: _have("argon2"),
             "install argon2",
             run_argon2),
    Workload("xz-compression", "xz compression",
             lambda: _have("xz"),
             "install xz",
             run_xz),
    Workload("primes", "calculating prime numbers",
             lambda: _have("stress-ng"),
             "install stress-ng",
             run_primes),
    Workload("x265-encoding", "x265 encoding",
             lambda: _have("ffmpeg"),
             "install ffmpeg (with libx265 enabled)",
             run_x265),
    Workload("ffmpeg-compilation", "ffmpeg compilation",
             lambda: _have("git") and _have("make") and _have("gcc"),
             "install git, make, gcc",
             run_ffmpeg_compile),
    Workload("blender-render", "blender render",
             _probe_blender,
             "install blender; place scene at ~/blender-bench/scene.blend",
             run_blender),
    Workload("kernel-defconfig", "kernel defconfig",
             _probe_kernel_defconfig,
             "git clone --depth 1 https://git.kernel.org/.../linux.git "
             "~/linux-bench",
             run_kernel_defconfig),
]


# RUN HARNESS

def run_workload_n_times(wl: Workload, n: int, ncpus: int) -> list[float]:
    """Run a workload N times. Returns the list of elapsed-seconds samples."""
    samples: list[float] = []
    for i in range(n):
        elapsed = wl.run(ncpus)
        if elapsed is None:
            log_warn(f"  iter {i+1}/{n}: failed")
            continue
        log_info(f"  iter {i+1}/{n}: {elapsed:.3f}s")
        samples.append(elapsed)
        time.sleep(1)
    return samples


def write_report(ver: str, git: dict, stamp: str, ncpus: int,
                 results: dict, runs: int, wl_order: list[str]) -> Path:
    """Aligned-column text report. No box-drawing, no separators."""
    lines = [
        "PANDEMONIUM BENCH-CACHYOS",
        f"VERSION:     {ver}",
        f"COMMIT:      {git['commit']}{' (dirty)' if git['dirty'] else ''}",
        f"TIMESTAMP:   {stamp}",
        f"CPUS:        {ncpus}",
        f"ITERATIONS:  {runs}",
        "",
    ]
    sched_names = list(results.keys())
    # PER-WORKLOAD COMPARISON TABLE: WORKLOAD x SCHEDULER.
    header = f"{'WORKLOAD':<32}" + "".join(f"{s:>22}" for s in sched_names)
    lines.append(header)
    for wl_name in wl_order:
        row = f"{wl_name:<32}"
        # FIND WINNER FOR THIS ROW (LOWEST MEAN).
        winners = []
        means = {}
        for s in sched_names:
            v = results[s].get(wl_name)
            if v and v[0] is not None:
                means[s] = v[0]
        if means:
            best = min(means.values())
        for s in sched_names:
            v = results[s].get(wl_name)
            if v is None or v[0] is None:
                cell = "skip/fail"
                row += f"{cell:>22}"
            else:
                mean, sd = v
                tag = "*" if (mean - best) < 1e-9 and mean == best else " "
                cell = f"{mean:8.3f}s ±{sd:6.3f}{tag}"
                row += f"{cell:>22}"
        lines.append(row)
    # TOTALS ROW (SUM OF PER-WORKLOAD MEANS).
    lines.append("")
    totals_row = f"{'TOTAL TIME':<32}"
    totals = {}
    for s in sched_names:
        total = 0.0
        ok = True
        for wl_name in wl_order:
            v = results[s].get(wl_name)
            if v is None or v[0] is None:
                ok = False
                break
            total += v[0]
        if ok:
            totals[s] = total
    if totals:
        best = min(totals.values())
        for s in sched_names:
            if s not in totals:
                cell = "incomplete"
                totals_row += f"{cell:>22}"
            else:
                tag = "*" if (totals[s] - best) < 1e-9 and totals[s] == best else " "
                cell = f"{totals[s]:8.3f}s{tag}"
                totals_row += f"{cell:>22}"
        lines.append(totals_row)
        lines.append("")
        lines.append("* MARKS WINNER (LOWEST MEAN) PER ROW")
    text = "\n".join(lines) + "\n"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bench-cachyos-{stamp}.log"
    path.write_text(text)
    return path


def write_prometheus(ver: str, git: dict, stamp: str, ncpus: int,
                     results: dict, wl_order: list[str],
                     skipped: Optional[dict] = None) -> Path:
    """Prometheus textfile-collector style emission."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"bench-cachyos-{stamp}.prom"
    pb = PrometheusBuilder("cachyos")
    # Metadata lives in ONE _info gauge -- version/commit are no longer repeated
    # on every sample line.
    try:
        ts = int(datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        ts = None
    pb.info(ts=ts, version=ver, git_commit=git["commit"], git_dirty=git.get("dirty", False))
    pb.gauge("cpus", ncpus, help="CPUs available")
    # SKIPPED schedulers: crashed / ejected / fell through to EEVDF. Emitted so
    # a missing scheduler is explicit, never mistaken for an EEVDF re-measure.
    for sched, reason in (skipped or {}).items():
        rl = reason.replace('"', "'")
        pb.gauge("scheduler_skipped", 1,
                 help="scheduler crashed/ejected/fell through to EEVDF (not measured)",
                 labels={"scheduler": sched, "reason": rl})
    for sched, by_wl in results.items():
        for wl_name in wl_order:
            v = by_wl.get(wl_name)
            if v is None or v[0] is None:
                continue
            mean, sd = v
            pb.gauge("seconds", f"{mean:.6f}",
                     help="wall-clock seconds per CachyOS-suite workload",
                     labels={"scheduler": sched, "workload": wl_name, "stat": "mean"})
            pb.gauge("seconds", f"{sd:.6f}",
                     labels={"scheduler": sched, "workload": wl_name, "stat": "stdev"})
    path.write_text(pb.render())
    return path


def _watch_sched(guard, expected_ops, sched_name, stop_evt, out):
    """Background poll (1s): log the instant the scheduler ejects, so a crash
    that HANGS a workload -- or a Ctrl+C on that hang -- surfaces immediately
    instead of waiting for a workload that never returns. Records once."""
    while not stop_evt.wait(1.0):
        r = _sched_crashed(guard, expected_ops)
        if r:
            out["reason"] = r
            log_error(f"[{sched_name}] CRASHED mid-run -- {r}")
            return


def _sched_crashed(guard, expected_ops=""):
    """Crash / EEVDF fall-through detector for the scheduler-matrix loop.
    Returns a reason string if the scheduler died, ejected, or silently fell
    back to EEVDF (measuring it further would just re-measure EEVDF), else None.
    EEVDF runs with guard=None (kernel default) and never falls through.
    expected_ops = sched_ext ops name captured right after activation."""
    if guard is None:
        return None
    if guard.proc.poll() is not None:
        return f"process exited (rc={guard.proc.returncode})"
    name = scx_scheduler_name()
    if not name:
        return "fell through to EEVDF (sched_ext slot empty -- ejected?)"
    if expected_ops and name != expected_ops:
        return f"fell through to '{name}' (expected '{expected_ops}')"
    return None


# montauk comm to trace per workload (the binary doing the actual computation).
# Build workloads (compilation/defconfig) are process TREES -- trace the build
# driver and montauk fork-tracking follows it down to the gcc/cc1 children.
WORKLOAD_TRACE_COMM = {
    "stress-ng-cpu-cache-mem": "stress-ng",
    "perf-sched-msg-fork-thread": "perf",
    "perf-memcpy": "perf",
    "argon2-hashing": "argon2",
    "xz-compression": "xz",
    "primes": "stress-ng",
    "x265-encoding": "ffmpeg",
    "ffmpeg-compilation": "make",
    "blender-render": "blender",
    "kernel-defconfig": "make",
}


def run_trace(entries, active, stamp, ncpus) -> int:
    """`bench-cachyos --trace`: cycle the full matrix (every scheduler x every
    workload) with montauk recording each workload's actual computation -- one
    recording per (scheduler, workload), patterned on that workload's comm.

    Not a benchmark: montauk trace overhead makes the wall-times unusable, and
    each scheduler is activated ONCE around its whole workload run. The recordings
    (preempt_*, migrations_cross_ccx, per-thread state) are the point; they land in
    /tmp/pandemonium and can be sizable on the long workloads (ffmpeg, kernel)."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1

    recs: dict[str, Path] = {}
    try:
        for sched_name, cmd in entries:
            guard = None
            if cmd is not None:
                log_info(f"[{sched_name}] activating...")
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"[{sched_name}] failed to activate -- SKIPPED")
                    continue
            safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
            try:
                for wl in active:
                    comm = WORKLOAD_TRACE_COMM.get(wl.name, wl.name)
                    _refresh_sudo()
                    log_info(f"  [{sched_name}] tracing {wl.label} (comm='{comm}')")
                    with montauk_trace(comm, f"cachyos-{safe}-{wl.name}",
                                       stamp) as rec:
                        elapsed = wl.run(ncpus)
                    recs[f"{sched_name}/{wl.name}"] = rec.dir
                    tag = f"{elapsed:.3f}s" if elapsed is not None else "no timing"
                    log_info(f"    {wl.label}: {tag} (traced) -> {rec.dir}")
            finally:
                if guard is not None:
                    stop_and_wait(guard)
            print()
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    log_info(f"TRACE COMPLETE: {len(recs)} recording(s) under /tmp/pandemonium")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PANDEMONIUM bench-cachyos: CachyOS Mini-Benchmarker-style suite",
    )
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"Iterations per workload (default: {DEFAULT_RUNS})")
    ap.add_argument("--workloads", type=str, default="",
                    help="Comma-separated workload names (default: all available)")
    ap.add_argument("--schedulers", type=str, default="",
                    help="Comma-separated external scx schedulers to add to the "
                         "EEVDF + PANDEMONIUM field (e.g. scx_rusty,scx_lavd). "
                         "Default: none")
    ap.add_argument("--all-scx", action="store_true",
                    help="Run the full installed scx scheduler field "
                         "(scx_bpfland, scx_rusty, scx_lavd, scx_flow, "
                         "scx_rustland, scx_p2dq, scx_tickless, scx_cosmos, "
                         "scx_cake, scx_flash, scx_beerland, scx_layered) "
                         "instead of --schedulers. Each is skipped if not "
                         "installed. Overrides --schedulers.")
    ap.add_argument("--pandemonium-only", action="store_true",
                    help="Skip EEVDF and external schedulers")
    ap.add_argument("--no-eevdf", action="store_true",
                    help="Skip EEVDF baseline")
    ap.add_argument("--trace", action="store_true",
                    help="Diagnostic pass (not a benchmark): cycle the full "
                         "scheduler x workload matrix with montauk recording each "
                         "workload's computation (preempt/migration/thread data) "
                         "to /tmp/pandemonium. Wall-times are contaminated; ignored.")
    args = ap.parse_args()

    _warm_sudo()

    ncpus = multiprocessing.cpu_count()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirty = " (dirty)" if git["dirty"] else ""

    if not log.child:
        log_info(f"bench-cachyos v{ver} [{git['commit']}{dirty}]")
        log_info(f"CPUs: {ncpus}  Iterations: {args.runs}")

    # WORKLOAD SELECTION + AVAILABILITY PROBE.
    wl_filter = set(w.strip() for w in args.workloads.split(",") if w.strip())
    active: list[Workload] = []
    for wl in WORKLOADS:
        if wl_filter and wl.name not in wl_filter:
            continue
        if not wl.probe():
            log_warn(f"  skipping {wl.label}: {wl.probe_hint}")
            continue
        active.append(wl)
    if not active:
        log_error("No workloads available; exiting.")
        return 1
    if not log.child:
        log_info(f"workloads ({len(active)}): {', '.join(w.label for w in active)}")

    # SCHEDULER MATRIX. EEVDF, PANDEMONIUM BPF, PANDEMONIUM ADAPTIVE,
    # PLUS ANY EXTERNALS THE USER REQUESTED.
    entries: list[tuple[str, Optional[list[str]]]] = []
    if not args.pandemonium_only and not args.no_eevdf:
        entries.append(("EEVDF", None))
    # A named --schedulers field is EEVDF vs EXACTLY those (PANDEMONIUM only if
    # named) -- matching field_arms -- so PANDEMONIUM is not auto-added to a run
    # that named someone else. Default and --all-scx / --pandemonium-only keep it.
    _field_only = bool(args.schedulers) and not args.all_scx and not args.pandemonium_only
    _named = ({s.strip().lower() for s in args.schedulers.split(",")}
              if args.schedulers else set())
    if (not _field_only) or (_named & {"pandemonium", "scx_pandemonium"}):
        entries.append(("PANDEMONIUM (BPF)", [str(BINARY), "--no-adaptive"]))
        entries.append(("PANDEMONIUM (ADAPTIVE)", [str(BINARY)]))
    if not args.pandemonium_only:
        # --all-scx runs the full installed production scx field (same set as
        # bench-fork-thread). scx_chaos is excluded (fault-injection test
        # scheduler, not a contender); scx_layered needs a layer spec and may
        # self-skip without one. Otherwise honor --schedulers.
        if args.all_scx:
            ext_list = [
                "scx_bpfland", "scx_rusty", "scx_lavd", "scx_flow", "scx_rustland",
                "scx_p2dq", "scx_tickless", "scx_cosmos", "scx_cake", "scx_flash",
                "scx_beerland", "scx_layered",
            ]
        else:
            ext_list = [s.strip() for s in args.schedulers.split(",")]
        for ext in ext_list:
            if not ext or ext.strip().lower() in ("pandemonium", "scx_pandemonium",
                                                  "eevdf"):
                continue  # baseline / handled above, not an external to resolve
            p = find_scheduler(ext)
            if p:
                entries.append((ext, [ext]))
            else:
                log_warn(f"  external scheduler {ext} not found in PATH, skipping")

    if not log.child:
        log_info(f"schedulers ({len(entries)}): {', '.join(n for n, _ in entries)}")
    print()

    # DEACTIVATE ANY RUNNING SCX SCHEDULER BEFORE STARTING.
    if is_scx_active():
        name = scx_scheduler_name()
        log_warn(f"sched_ext is active ({name}) -- stopping pandemonium service")
        subprocess.run(["sudo", "systemctl", "stop", "pandemonium"], capture_output=True)
        if not wait_for_deactivation(5.0):
            log_error("Could not deactivate sched_ext")
            return 1
    time.sleep(1)

    if args.trace:
        return run_trace(entries, active, stamp, ncpus)

    # MAIN MATRIX LOOP.
    results: dict[str, dict[str, tuple[Optional[float], float]]] = {}
    skipped: dict[str, str] = {}
    try:
        for sched_name, cmd in entries:
            log_info(f"[{sched_name}] starting...")
            guard = None
            expected_ops = ""
            if cmd is not None:
                guard = start_and_wait(cmd, sched_name)
                if guard is None:
                    log_error(f"[{sched_name}] FAILED to activate -- SKIPPED "
                              f"(no point re-measuring EEVDF)")
                    skipped[sched_name] = "failed to activate"
                    continue
                expected_ops = scx_scheduler_name()
                if not expected_ops:
                    log_error(f"[{sched_name}] not attached after activate -- "
                              f"SKIPPED (fell through to EEVDF)")
                    skipped[sched_name] = "fell through to EEVDF at activation"
                    stop_and_wait(guard)
                    continue
            results[sched_name] = {}
            stop_evt = threading.Event()
            watch = {}
            if guard is not None:
                threading.Thread(target=_watch_sched, daemon=True,
                                 args=(guard, expected_ops, sched_name,
                                       stop_evt, watch)).start()
            for wl in active:
                _refresh_sudo()
                log_info(f"  [{sched_name}] {wl.label}")
                samples = run_workload_n_times(wl, args.runs, ncpus)
                reason = watch.get("reason") or _sched_crashed(guard, expected_ops)
                if reason:
                    log_error(f"[{sched_name}] CRASHED during {wl.label} -- {reason}")
                    log_warn(f"[{sched_name}] SKIPPING remaining workloads -- "
                             f"would just re-measure EEVDF")
                    skipped[sched_name] = f"{reason} (during {wl.name})"
                    break   # discard the EEVDF-tainted samples, stop this sched
                if samples:
                    mean, sd = mean_stdev(samples)
                    log_info(f"    mean={mean:.3f}s ±{sd:.3f}")
                    results[sched_name][wl.name] = (mean, sd)
                else:
                    results[sched_name][wl.name] = (None, 0.0)
                # Incremental .prom: rewrite from results-so-far after every cell
                # so the run is watchable live (matches the rest of the suite).
                # The authoritative final write happens after the loop.
                write_prometheus(ver, git, stamp, ncpus, results,
                                 [w.name for w in active], skipped)
                time.sleep(1)
            stop_evt.set()
            if watch.get("reason") and sched_name not in skipped:
                skipped[sched_name] = watch["reason"]
            if guard is not None:
                stop_and_wait(guard)
            time.sleep(2)
            print()
    except KeyboardInterrupt:
        log.interrupted()
    finally:
        if is_scx_active():
            wait_for_deactivation(5.0)

    if skipped:
        log_error(f"{len(skipped)} scheduler(s) CRASHED / fell through to EEVDF "
                  f"-- SKIPPED (not measured as EEVDF):")
        for s, r in skipped.items():
            log_error(f"  {s} -- {r}")
    else:
        log_info("no scheduler crashes -- all schedulers ran as themselves")

    if results:
        wl_order = [w.name for w in active]
        report_path = write_report(ver, git, stamp, ncpus, results, args.runs, wl_order)
        prom_path = write_prometheus(ver, git, stamp, ncpus, results, wl_order, skipped)
        print()
        log.report(report_path.read_text())
        log_info(f"REPORT: {report_path}")
        log_info(f"METRICS: {prom_path}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
