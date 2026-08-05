#!/usr/bin/env python3
"""
PANDEMONIUM prism-cachyos: CachyOS Mini-Benchmarker-style application suite.

Models the Mini-Benchmarker workload set the CachyOS project uses for
cross-kernel scheduler comparisons. Each workload runs N iterations under
each scheduler, captures wall-clock time, and reports mean + stddev
alongside per-row winners.

Workloads (12, all auto-acquiring -- nothing needs manual setup; each fetches
and caches its asset on first run):

    stress-ng-cpu-cache-mem   stress-ng cache stressor, fixed ops.
    perf-sched-msg-fork-thread perf bench sched messaging, fixed loops.
    perf-memcpy               perf bench mem memcpy, fixed size.
    argon2-hashing            argon2 -t T -m M -p P.
    xz-compression            xz -3 on a stable test corpus.
    primes                    stress-ng cpu prime method, fixed ops.
    x265-encoding             ffmpeg encode of lavfi testsrc clip.
    ffmpeg-compilation        git clone (once) then make -j$(nproc).
    namd-apoa1                NAMD 3.0b6 apoa1 (92K atoms), all cores.
    y-cruncher-pi-1b          y-cruncher pi to 1 billion digits.
    blender-render            blender render of the bmw_cpu_mod scene.
    kernel-defconfig          linux-6.14.7 defconfig, build vmlinux.

Usage:
    ./tests/prism-cachyos.py
    ./tests/prism-cachyos.py --iterations 5
    ./tests/prism-cachyos.py --workloads xz-compression,primes
    ./tests/prism-cachyos.py --schedulers scx_cake
    ./tests/prism-cachyos.py --all-scx
    ./tests/prism-cachyos.py --pandemonium-only

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
    wait_for_deactivation, PrometheusBuilder, get_online_cpus,
 warm_sudo, refresh_sudo,)

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from importlib import import_module
_tests = import_module("pandemonium-tests")
start_and_wait = _tests.start_and_wait
stop_and_wait = _tests.stop_and_wait
find_scheduler = _tests.find_scheduler

# ASSET DIRECTORY. PERSISTENT TEST CORPUS, GENERATED FFMPEG CLONE,
# AND GENERATED XZ INPUT FILE LIVE HERE BETWEEN INVOCATIONS.
ASSET_DIR = LOG_DIR / "prism-cachyos-assets"
XZ_CORPUS = ASSET_DIR / "xz-corpus.bin"
FFMPEG_SRC = ASSET_DIR / "ffmpeg-src"
X265_OUTPUT = ASSET_DIR / "x265-output.hevc"

# KERNEL-DEFCONFIG ASSET. linux-6.14.7 matches the CachyOS Mini-Benchmarker's
# KERNVER, so the kernel-compile number is apples-to-apples with their chart.
# Auto-acquired (download + extract + defconfig) on first run, then cached.
KERNEL_VER = "6.14.7"
KERNEL_URL = (f"https://cdn.kernel.org/pub/linux/kernel/v6.x/"
              f"linux-{KERNEL_VER}.tar.xz")
KERNEL_TARBALL = ASSET_DIR / f"linux-{KERNEL_VER}.tar.xz"
KERNEL_SRC = ASSET_DIR / f"linux-{KERNEL_VER}"

# NAMD 92K-atom (apoa1) -- precompiled multicore binary + example, matches the
# benchmarker's NAMD 3.0b6 + apoa1.
NAMD_URL = ("http://www.ks.uiuc.edu/Research/namd/3.0b6/download/120834/"
            "NAMD_3.0b6_Linux-x86_64-multicore.tar.gz")
NAMD_APOA1_URL = "https://www.ks.uiuc.edu/Research/namd/utilities/apoa1.tar.gz"
NAMD_DIR = ASSET_DIR / "namd"
NAMD_BIN_DIR = NAMD_DIR / "NAMD_3.0b6_Linux-x86_64-multicore"

# y-cruncher pi 1b -- static binary, matches the benchmarker's version. The
# extracted directory name carries a space, exactly as upstream ships it.
YCRUNCHER_VER = "0.8.6.9545"
YCRUNCHER_URL = (f"https://github.com/Mysticial/y-cruncher/releases/download/"
                 f"v{YCRUNCHER_VER}/y-cruncher.v{YCRUNCHER_VER}-static.tar.xz")
YCRUNCHER_TARBALL = ASSET_DIR / f"y-cruncher.v{YCRUNCHER_VER}-static.tar.xz"
YCRUNCHER_DIR = ASSET_DIR / f"y-cruncher v{YCRUNCHER_VER}-static"

# blender render -- the benchmarker's bmw_cpu_mod.blend scene.
BLENDER_SCENE_URL = ("https://gitlab.com/torvic9/mini-benchmarker/-/raw/master/"
                     "bmw_cpu_mod.blend")
BLENDER_SCENE = ASSET_DIR / "bmw_cpu_mod.blend"

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
    # MATCHES THE prism-fork-thread.py DEFAULT (-g 24 -l 6000) AT 12C
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


def _download(url: str, dest: Path, min_bytes: int = 0) -> bool:
    """Stream a URL to dest (cached). urllib, stdlib -- no wget/curl dep. Writes a
    .part sidecar then renames, so a half download never looks complete."""
    if dest.is_file() and dest.stat().st_size > min_bytes:
        return True
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request
    part = dest.parent / (dest.name + ".part")
    log_info(f"downloading {dest.name} (one-time)...")
    try:
        urllib.request.urlretrieve(url, part)
        part.replace(dest)
    except Exception as e:
        log_error(f"  download failed ({dest.name}): {e}")
        part.unlink(missing_ok=True)
        return False
    return True


def _ensure_namd() -> bool:
    if not ((NAMD_BIN_DIR / "namd3").is_file()
            and (NAMD_DIR / "apoa1" / "apoa1.namd").is_file()):
        NAMD_DIR.mkdir(parents=True, exist_ok=True)
        tgz = ASSET_DIR / "namd.tar.gz"
        apoa1 = ASSET_DIR / "apoa1.tar.gz"
        if not _download(NAMD_URL, tgz, 50 * 1024 * 1024):
            return False
        if not _download(NAMD_APOA1_URL, apoa1, 1024 * 1024):
            return False
        for src in (tgz, apoa1):
            r = subprocess.run(["tar", "-C", str(NAMD_DIR), "-xf", str(src)],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                log_error(f"  namd extract failed: {r.stderr.strip()[:200]}")
                return False
    # Upstream apoa1.namd writes outputName to /usr/tmp, which does not exist on
    # modern systems -- NAMD dies with FATAL ERROR opening apoa1-out.xsc. Redirect
    # all /usr/tmp paths into the writable apoa1 asset dir. Idempotent, so it also
    # repairs an already-extracted (unpatched) cache on the next run.
    namd_cfg = NAMD_DIR / "apoa1" / "apoa1.namd"
    try:
        text = namd_cfg.read_text()
        patched = text.replace("/usr/tmp", str(NAMD_DIR / "apoa1"))
        if patched != text:
            namd_cfg.write_text(patched)
    except OSError as e:
        log_error(f"  namd config patch failed: {e}")
        return False
    return True


def run_namd(ncpus: int) -> Optional[float]:
    if not _ensure_namd():
        return None
    # NAMD apoa1 (92K atoms), all cores, matching the benchmarker invocation.
    return _run_timed(
        ["./namd3", f"+p{ncpus}", "+setcpuaffinity",
         str(NAMD_DIR / "apoa1" / "apoa1.namd")],
        cwd=NAMD_BIN_DIR, timeout=DEFAULT_TIMEOUT_S)


def _ensure_ycruncher() -> bool:
    if (YCRUNCHER_DIR / "y-cruncher").is_file():
        return True
    if not _download(YCRUNCHER_URL, YCRUNCHER_TARBALL, 1024 * 1024):
        return False
    r = subprocess.run(["tar", "-xf", str(YCRUNCHER_TARBALL)], cwd=ASSET_DIR,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log_error(f"  y-cruncher extract failed: {r.stderr.strip()[:200]}")
        return False
    return True


def run_ycruncher(_ncpus: int) -> Optional[float]:
    if not _ensure_ycruncher():
        return None
    # bench 1b = pi to 1 billion digits; -od:0 keeps it in RAM (no disk output).
    return _run_timed(
        ["./y-cruncher", "bench", "1b", "-od:0", "-o", str(ASSET_DIR)],
        cwd=YCRUNCHER_DIR, timeout=DEFAULT_TIMEOUT_S)


# AUTO-ACQUIRED ASSET WORKLOADS: blender scene + kernel source fetched on demand.

def _probe_blender() -> bool:
    # Scene auto-acquired (bmw_cpu_mod.blend); just needs blender installed.
    return shutil.which("blender") is not None


def _probe_kernel_defconfig() -> bool:
    # Source is auto-acquired (download + extract + defconfig) on first run, like
    # ffmpeg-src -- no manual clone. Needs the full kernel-build toolchain: bc
    # (timeconst.h in prepare0), flex and bison (kconfig) are not all shipped by
    # default on CachyOS, so a missing one must skip cleanly, not die at Error 2.
    return all(shutil.which(t) for t in ("make", "gcc", "tar", "bc", "flex", "bison"))


def run_blender(ncpus: int) -> Optional[float]:
    if not _download(BLENDER_SCENE_URL, BLENDER_SCENE, 1024):
        return None
    out = str(ASSET_DIR / "blenderbmw.jpg")
    return _run_timed(
        ["blender", "-b", str(BLENDER_SCENE), "-o", out, "-f", "1",
         "--verbose", "0", "-t", str(ncpus)], timeout=900)


def _ensure_kernel_src() -> bool:
    """Download + extract + defconfig linux-6.14.7 once (cached in ASSET_DIR),
    matching the CachyOS Mini-Benchmarker. urllib download (stdlib, no wget dep),
    then a one-time `make defconfig` so each timed build is config-stable."""
    if (KERNEL_SRC / "Makefile").is_file() and (KERNEL_SRC / ".config").is_file():
        return True
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    if (not KERNEL_TARBALL.is_file()
            or KERNEL_TARBALL.stat().st_size < 100 * 1024 * 1024):
        log_info(f"downloading linux-{KERNEL_VER} (~140MB, one-time)...")
        import urllib.request
        part = KERNEL_TARBALL.with_suffix(".part")
        try:
            urllib.request.urlretrieve(KERNEL_URL, part)
            part.replace(KERNEL_TARBALL)
        except Exception as e:
            log_error(f"  kernel download failed: {e}")
            part.unlink(missing_ok=True)
            return False
    if not (KERNEL_SRC / "Makefile").is_file():
        log_info(f"extracting linux-{KERNEL_VER} (one-time)...")
        r = subprocess.run(["tar", "-xf", str(KERNEL_TARBALL)], cwd=ASSET_DIR,
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log_error(f"  kernel extract failed: {r.stderr.strip()[:200]}")
            return False
    # ONE-TIME defconfig (not timed), distclean first -- matches the benchmarker.
    log_info("  make defconfig (one-time prep)...")
    subprocess.run(["make", "-s", "distclean"], cwd=KERNEL_SRC,
                   capture_output=True, timeout=120)
    d = subprocess.run(["make", "-s", "defconfig"], cwd=KERNEL_SRC,
                       capture_output=True, text=True, timeout=120)
    if d.returncode != 0:
        log_error(f"  make defconfig failed (need flex/bison/libelf/bc?): "
                  f"{d.stderr.strip()[:200]}")
        return False
    return True


def run_kernel_defconfig(ncpus: int) -> Optional[float]:
    if not _ensure_kernel_src():
        return None
    # distclean + defconfig before each iteration -- matches the OFFICIAL CachyOS
    # benchmarker (make -s distclean && make -s defconfig), so every timed build is
    # a full from-scratch compile INCLUDING the config-prepare stage (syncconfig,
    # timeconst.h via bc) -- not a `make clean` that keeps .config and skips prepare,
    # which under-reports vs their chart. TIMED build matches the official:
    # make KCFLAGS='-Wno-error' -sj$N vmlinux.
    subprocess.run(["make", "-s", "distclean"], cwd=KERNEL_SRC,
                   capture_output=True, timeout=120)
    subprocess.run(["make", "-s", "defconfig"], cwd=KERNEL_SRC,
                   capture_output=True, timeout=120)
    return _run_timed(["make", "KCFLAGS=-Wno-error", f"-j{ncpus}", "-s", "vmlinux"],
                      cwd=KERNEL_SRC, timeout=3600)


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
    Workload("namd-apoa1", "namd 92K atoms",
             lambda: _have("tar"),
             "auto-downloads NAMD 3.0b6 + apoa1 (needs tar)",
             run_namd),
    Workload("y-cruncher-pi-1b", "y-cruncher pi 1b",
             lambda: _have("tar"),
             "auto-downloads y-cruncher static (needs tar)",
             run_ycruncher),
    Workload("blender-render", "blender render",
             _probe_blender,
             "install blender; place scene at ~/blender-bench/scene.blend",
             run_blender),
    Workload("kernel-defconfig", "kernel defconfig",
             _probe_kernel_defconfig,
             "install make, gcc, tar (+ flex bison libelf bc for the kernel build)",
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
    path = LOG_DIR / f"prism-cachyos-{stamp}.log"
    path.write_text(text)
    return path


def write_prometheus(ver: str, git: dict, stamp: str, ncpus: int,
                     results: dict, wl_order: list[str],
                     skipped: Optional[dict] = None) -> Path:
    """Prometheus textfile-collector style emission."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-cachyos-{stamp}.prom"
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
    "namd-apoa1": "namd3",
    "y-cruncher-pi-1b": "y-cruncher",
    "blender-render": "blender",
    "kernel-defconfig": "make",
}


def run_trace(entries, active, stamp, ncpus) -> int:
    """`prism-cachyos --trace`: cycle the full matrix (every scheduler x every
    workload) with montauk recording each workload's actual computation -- one
    recording per (scheduler, workload), patterned on that workload's comm.

    Not a benchmark: montauk trace overhead makes the wall-times unusable, and
    each scheduler is activated ONCE around its whole workload run. The recordings
    are the point -- the .prom scrapes (preempt_*, migrations_cross_ccx, per-thread
    state) plus the per-event stream the analyzer needs for a wake2run verdict.
    They land in /tmp/pandemonium and the .events sibling dominates the size on the
    long workloads (ffmpeg, kernel): budget hundreds of MB per recording."""
    if not montauk_available():
        log_error("montauk not found -- cannot --trace")
        return 1

    recs: dict[str, Path] = {}
    # events=True is what makes these recordings ANALYZABLE. Without the
    # per-event stream a recording carries the .prom scrapes only, so montauk's
    # digest degrades to "KEY METRICS: not analyzed (no per-event trace)" and the
    # workload contributes one l2_miss_share row to the report -- no wake2run
    # verdict, no dispatch-stall attribution, no offenders. pin_cpu keeps montauk
    # off the saturated set so it drains its ring instead of shedding events.
    drain = max(0, get_online_cpus() - 1)
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
                    refresh_sudo()
                    log_info(f"  [{sched_name}] tracing {wl.label} "
                             f"(comm='{comm}', montauk on cpu{drain})")
                    with montauk_trace(comm, f"cachyos-{safe}-{wl.name}",
                                       stamp, events=True,
                                       pin_cpu=drain) as rec:
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
        description="PANDEMONIUM prism-cachyos: CachyOS Mini-Benchmarker-style suite",
    )
    ap.add_argument("--iterations", type=int, default=DEFAULT_RUNS,
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

    warm_sudo()

    ncpus = multiprocessing.cpu_count()
    ver = get_version()
    git = get_git_info()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirty = " (dirty)" if git["dirty"] else ""

    if not log.child:
        log_info(f"prism-cachyos v{ver} [{git['commit']}{dirty}]")
        log_info(f"CPUs: {ncpus}  Iterations: {args.iterations}")

    # WORKLOAD SELECTION + AVAILABILITY PROBE.
    wl_filter = set(w.strip() for w in args.workloads.split(",") if w.strip())
    active: list[Workload] = []
    unavailable: list[Workload] = []
    for wl in WORKLOADS:
        if wl_filter and wl.name not in wl_filter:
            continue
        (active if wl.probe() else unavailable).append(wl)
    # PREFLIGHT: surface every missing dependency UP FRONT with its fix, before the
    # (long) suite runs -- a missing tool should be a clear heads-up here, not a
    # cryptic mid-run failure three benchmarks deep.
    if unavailable and not log.child:
        log_warn(f"PREFLIGHT: {len(unavailable)} workload(s) will be SKIPPED -- "
                 "missing dependencies:")
        for wl in unavailable:
            log_warn(f"    {wl.label} -- {wl.probe_hint}")
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
        # prism-fork-thread). scx_chaos is excluded (fault-injection test
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
        _tests.stop_systemd_scheduler()
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
                refresh_sudo()
                log_info(f"  [{sched_name}] {wl.label}")
                samples = run_workload_n_times(wl, args.iterations, ncpus)
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
        report_path = write_report(ver, git, stamp, ncpus, results, args.iterations, wl_order)
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
