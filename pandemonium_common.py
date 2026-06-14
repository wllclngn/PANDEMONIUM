"""
Shared infrastructure for PANDEMONIUM build/test scripts.

Used by pandemonium.py (build manager) and tests/pandemonium-tests.py (test orchestrator).
"""

import glob
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# CONFIGURATION

SCRIPT_DIR = Path(__file__).parent.resolve()
_sudo_user = os.environ.get("SUDO_USER")
_real_home = Path(f"/home/{_sudo_user}") if _sudo_user else Path.home()
# CARGO_TARGET_DIR LIVES UNDER THE INVOKING USER'S HOME, NOT /tmp. /tmp IS
# WORLD-WRITABLE WITH STICKY BIT, AND A SHARED BUILD TREE THERE OPENED A
# CROSS-USER SUPPLY-CHAIN VECTOR: ANOTHER LOCAL USER COULD POISON
# `release/pandemonium` BEFORE A VICTIM RAN `./pandemonium.py install`,
# WHICH RUNS `sudo cp $BINARY /usr/local/bin/pandemonium`. CLOSED IN v5.9.0.
TARGET_DIR = _real_home / ".cache" / "pandemonium-build"
LOG_DIR = _real_home / ".cache" / "pandemonium"
ARCHIVE_DIR = LOG_DIR
BINARY = TARGET_DIR / "release" / "pandemonium"
VMLINUX_CACHE = ARCHIVE_DIR / "vmlinux.h"
MIN_KERNEL = (6, 12)

SOURCE_PATTERNS = [
    "src/**/*.rs", "src/**/*.c", "src/**/*.h",
    "tests/**/*.rs",
    "Cargo.toml", "build.rs",
]


def get_version() -> str:
    """Read version from Cargo.toml."""
    try:
        for line in (SCRIPT_DIR / "Cargo.toml").read_text().splitlines():
            if line.startswith("version"):
                return line.split('"')[1]
    except (FileNotFoundError, IndexError):
        pass
    return "?.?.?"


def get_git_info() -> dict:
    """Return git commit hash and dirty status."""
    info = {"commit": "unknown", "dirty": False}
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=SCRIPT_DIR,
        )
        if r.returncode == 0:
            info["commit"] = r.stdout.strip()
        r = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"],
            capture_output=True, cwd=SCRIPT_DIR,
        )
        info["dirty"] = r.returncode != 0
    except FileNotFoundError:
        pass
    return info


# LOGGING

def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def log_info(msg: str) -> None:
    print(f"{_timestamp()} [INFO]   {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"{_timestamp()} [WARN]   {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{_timestamp()} [ERROR]  {msg}", flush=True)


def run_cmd(cmd: list, cwd: Path | None = None,
            env: dict | None = None) -> int:
    """Run a command with real-time output to terminal."""
    print(f">>> {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env)
    return result.returncode


def run_cmd_capture(cmd: list, cwd: Path | None = None,
                    env: dict | None = None) -> tuple[int, str, str]:
    """Run a command and capture output."""
    result = subprocess.run(cmd, capture_output=True, text=True,
                            cwd=cwd, env=env)
    return result.returncode, result.stdout, result.stderr


# BUILD

def has_root_owned_files() -> bool:
    """Check if sudo left root-owned files anywhere in the build tree."""
    if not TARGET_DIR.exists():
        return False
    result = subprocess.run(
        ["find", str(TARGET_DIR), "-user", "root", "-maxdepth", "4",
         "-print", "-quit"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def clean_root_files() -> bool:
    """Prompt and nuke root-owned build artifacts. Returns True if resolved."""
    log_warn(f"Root-owned build files detected in {TARGET_DIR}")
    resp = input("CLEAN ENTIRE BUILD DIR? [Y/N] ").strip().lower()
    if resp == "y":
        log_info("Cleaning build directory...")
        run_cmd(["sudo", "rm", "-rf", str(TARGET_DIR)])
        log_info("Build directory cleaned")
        return True
    log_error("Cannot build with root-owned files, aborting")
    return False


def check_sources_changed() -> list[str]:
    """Return list of source files newer than the binary (empty = up to date)."""
    if not BINARY.exists():
        return ["(binary not found)"]
    bin_mtime = BINARY.stat().st_mtime
    changed = []
    for pattern in SOURCE_PATTERNS:
        for src in SCRIPT_DIR.glob(pattern):
            if src.stat().st_mtime > bin_mtime:
                changed.append(str(src.relative_to(SCRIPT_DIR)))
    return changed


def check_kernel_version() -> bool:
    """Verify kernel >= 6.12 (sched_ext requirement). Returns True if OK."""
    release = platform.release()
    try:
        parts = release.split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        log_error(f"Cannot parse kernel version from '{release}'")
        return False
    if (major, minor) < MIN_KERNEL:
        log_error(f"Kernel {major}.{minor} is too old. PANDEMONIUM requires {MIN_KERNEL[0]}.{MIN_KERNEL[1]}+.")
        log_error("sched_ext (CONFIG_SCHED_CLASS_EXT) was merged in Linux 6.12.")
        log_error("Upgrade your kernel to use PANDEMONIUM.")
        return False
    log_info(f"Kernel {release} OK (>= {MIN_KERNEL[0]}.{MIN_KERNEL[1]})")
    return True


def ensure_vmlinux_h() -> bool:
    """Check vmlinux.h cache. Generated by bpftool on first build if missing."""
    if VMLINUX_CACHE.exists() and VMLINUX_CACHE.stat().st_size > 1000:
        log_info(f"vmlinux.h cached ({VMLINUX_CACHE.stat().st_size // 1024} KB)")
        return True
    log_info("vmlinux.h not cached (bpftool will generate on first build)")
    return True


def _cargo_invocation(cargo_args: list[str]) -> list[str]:
    """Wrap a cargo command in `sudo -u <SUDO_USER> -H env ...` when
    running under sudo. rustup stores the default toolchain in the
    invoking user's home; running cargo as root with no root-side
    rustup config aborts with 'no default toolchain configured'. Drop
    privs back to the original user so cargo finds their toolchain.
    -H resets HOME so .cargo and .rustup are picked up; `env` forwards
    CARGO_TARGET_DIR through sudo's default env scrub."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root" and os.geteuid() == 0:
        return ["sudo", "-u", sudo_user, "-H", "env",
                f"CARGO_TARGET_DIR={TARGET_DIR}", "cargo"] + cargo_args
    return ["cargo"] + cargo_args


def build(force: bool = False) -> bool:
    """Build PANDEMONIUM release binary. Returns True on success."""
    if not check_kernel_version():
        return False
    if not ensure_vmlinux_h():
        return False

    if has_root_owned_files():
        if not clean_root_files():
            return False

    if not force:
        changed = check_sources_changed()
        if not changed:
            size = BINARY.stat().st_size // 1024
            log_info(f"Binary up to date ({size} KB), skipping build")
            return True
        if changed[0] != "(binary not found)":
            log_info(f"Source changes detected ({len(changed)} file(s))")
        else:
            log_info("No existing binary, full build required")

    if force:
        log_info("Forced rebuild, cleaning package + BPF artifacts...")
        subprocess.run(
            _cargo_invocation(["clean", "-p", "pandemonium"]),
            env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
            cwd=str(SCRIPT_DIR),
            capture_output=True,
        )
        # NUKE BPF BUILD SCRIPT OUTPUT SO SKELETON GETS REGENERATED.
        # cargo clean -p only removes Rust artifacts, not OUT_DIR.
        for d in glob.glob(str(TARGET_DIR / "release" / "build" / "pandemonium-*")):
            shutil.rmtree(d, ignore_errors=True)

    log_info("Building (release)...")
    ret = run_cmd(
        _cargo_invocation(["build", "--release"]),
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
        cwd=SCRIPT_DIR,
    )

    if ret != 0:
        log_error("Build failed!")
        return False

    if BINARY.exists():
        size = BINARY.stat().st_size // 1024
        log_info(f"Build complete: {BINARY} ({size} KB)")
    return True




def ensure_build():
    """Build from source if needed, print version banner."""
    if not build():
        print("ERROR: build failed")
        sys.exit(1)
    ver = get_version()
    git = get_git_info()
    dirty = " (dirty)" if git["dirty"] else ""
    log_info(f"PANDEMONIUM v{ver} [{git['commit']}{dirty}]")


# STATISTICS

def mean_stdev(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)


def percentile(values: list[float], pct: float) -> float:
    """Compute percentile (0-100) using nearest-rank method."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(int(math.ceil(pct / 100.0 * len(s))) - 1, len(s) - 1))
    return s[k]


# CPU MANAGEMENT

def _parse_cpu_range(path: str) -> int:
    try:
        raw = Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError):
        return os.cpu_count() or 1
    count = 0
    for r in raw.split(","):
        parts = r.split("-")
        if len(parts) == 1 and parts[0].strip().isdigit():
            count += 1
        elif len(parts) == 2:
            try:
                count += int(parts[1]) - int(parts[0]) + 1
            except ValueError:
                pass
    return count


def get_possible_cpus() -> int:
    return _parse_cpu_range("/sys/devices/system/cpu/possible")


def get_online_cpus() -> int:
    return _parse_cpu_range("/sys/devices/system/cpu/online")


def set_cpu_online(cpu: int, online: bool) -> bool:
    if cpu == 0:
        return True
    path = f"/sys/devices/system/cpu/cpu{cpu}/online"
    value = "1" if online else "0"
    ret = subprocess.run(
        ["sudo", "tee", path],
        input=value, capture_output=True, text=True,
    )
    return ret.returncode == 0


def restrict_cpus(count: int, max_cpus: int) -> bool:
    for cpu in range(count, max_cpus):
        if not set_cpu_online(cpu, False):
            log_warn(f"Failed to offline CPU {cpu}")
            return False
    return True


def restore_all_cpus(max_cpus: int):
    for cpu in range(1, max_cpus):
        set_cpu_online(cpu, True)


class CpuGuard:
    """Context manager that restores all CPUs on exit."""
    def __init__(self, max_cpus: int):
        self.max_cpus = max_cpus

    def __enter__(self):
        return self

    def __exit__(self, *args):
        restore_all_cpus(self.max_cpus)


def compute_core_counts(max_cpus: int) -> list[int]:
    points = [n for n in [2, 4, 8, 16, 32, 64] if n <= max_cpus]
    if max_cpus not in points:
        points.append(max_cpus)
    return sorted(points)


# SCHEDULER DETECTION

SCX_OPS = Path("/sys/kernel/sched_ext/root/ops")


def is_scx_active() -> bool:
    try:
        return bool(SCX_OPS.read_text().strip())
    except (FileNotFoundError, PermissionError):
        return False


def scx_scheduler_name() -> str:
    try:
        return SCX_OPS.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return ""


def wait_for_activation(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_scx_active():
            return True
        time.sleep(0.1)
    return False


def wait_for_deactivation(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_scx_active():
            return True
        time.sleep(0.2)
    return False


def wait_for_no_scheduler(timeout: float = 10.0) -> bool:
    """Wait until no sched_ext scheduler is registered (stale struct_ops detection)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            name = SCX_OPS.read_text().strip()
            if not name:
                return True
            log_info(f"  waiting for scheduler cleanup: '{name}' still registered")
        except (FileNotFoundError, PermissionError):
            return True
        time.sleep(0.5)
    log_warn("scheduler still registered after timeout")
    return False


# MONTAUK TRACE CAPTURE
#
# THE SINGLE PLACE MONTAUK IS DRIVEN FROM. EVERY bench-* --trace PATH GOES
# THROUGH MontaukTrace INSTEAD OF REIMPLEMENTING THE LAUNCH/ATTACH/STOP/CHOWN.
# MONTAUK IS THE ONLY TRACER -- NO ftrace/trace_pipe ANYWHERE IN THE SUITE.

MONTAUK = "/usr/local/bin/montauk"
MONTAUK_ATTACH_TIMEOUT = 10.0
MONTAUK_LOG_INTERVAL_MS = 100


def montauk_available() -> bool:
    return Path(MONTAUK).exists()


def _chown_to_invoking_user(*paths) -> None:
    """Hand root-owned montauk recordings back to the sudo invoker so they are
    viewable without sudo. No-op when not running under sudo."""
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return
    for path in paths:
        if Path(path).exists():
            subprocess.run(["chown", "-R", f"{user}:", str(path)],
                           capture_output=True)


class MontaukTrace:
    """Context manager driving a montauk eBPF recording around a workload.

    __enter__ launches `montauk --trace PATTERN --log DIR`, waits for attach
    (first montauk_*.prom), then records an optional quiet baseline. __exit__
    stops montauk the way Ctrl+C would (SIGINT, escalating to TERM/KILL) and
    chowns the recording back to the sudo invoker. `.dir` is the recording
    directory, ready for `bench-analyze.py --trace` / `--fractal-dir`.

    The caller owns the scheduler and the workload; this owns only montauk.
    """

    def __init__(self, pattern, label, stamp, log_dir=LOG_DIR,
                 interval_ms=MONTAUK_LOG_INTERVAL_MS, baseline_s=0.0,
                 attach_timeout=MONTAUK_ATTACH_TIMEOUT, events=False,
                 pin_cpu=None):
        # pin_cpu: taskset montauk to a dedicated CPU so it always drains its
        # ring buffer -- under a saturated workload an unpinned montauk gets
        # starved and DROPS events (400ms+ capture holes), making a per-event
        # trace useless. The drain core must be OUTSIDE the saturated set.
        self.pin_cpu = pin_cpu
        self.pattern = pattern
        self.dir = Path(log_dir) / f"montauk-{label}-{stamp}"
        self.stdout_path = Path(log_dir) / f"montauk-{label}-{stamp}.stdout"
        # RAW PER-EVENT LOG (montauk --trace-out): finer than the 100ms .log
        # snapshots -- needed to SEE a single multi-ms scheduling stall.
        # Decode with montauk/build/montauk_trace_decode.
        self.events_path = (Path(log_dir) / f"montauk-{label}-{stamp}.events"
                            if events else None)
        self.interval_ms = interval_ms
        self.baseline_s = baseline_s
        self.attach_timeout = attach_timeout
        self.proc = None
        self._out = None

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self._out = open(self.stdout_path, "w")
        cmd = [MONTAUK, "--trace", self.pattern, "--log", str(self.dir),
               "--log-interval-ms", str(self.interval_ms)]
        if self.events_path is not None:
            cmd += ["--trace-out", str(self.events_path)]
        if self.pin_cpu is not None:
            cmd = ["taskset", "-c", str(self.pin_cpu)] + cmd
        self.proc = subprocess.Popen(cmd, stdout=self._out,
                                     stderr=subprocess.STDOUT)
        if not self._wait_for_attach():
            log_warn(f"montauk slow to attach (see {self.stdout_path})")
        if self.baseline_s > 0:
            log_info(f"recording idle baseline for {self.baseline_s:.0f}s...")
            time.sleep(self.baseline_s)
        return self

    def _wait_for_attach(self) -> bool:
        deadline = time.time() + self.attach_timeout
        while time.time() < deadline:
            if any(self.dir.glob("montauk_*.prom")):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        if self.proc is not None:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                if self.proc.poll() is not None:
                    break
                self.proc.send_signal(sig)
                try:
                    self.proc.wait(timeout=8)
                    break
                except subprocess.TimeoutExpired:
                    continue
            self.proc = None
        if self._out is not None:
            self._out.close()
            self._out = None
        _chown_to_invoking_user(*[p for p in (self.dir, self.stdout_path,
                                              self.events_path) if p is not None])

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def montauk_trace(pattern, label, stamp, log_dir=LOG_DIR, **kw) -> "MontaukTrace":
    """Factory for MontaukTrace -- use as `with montauk_trace(...) as rec:`."""
    return MontaukTrace(pattern, label, stamp, log_dir, **kw)




# IPC LATENCY ENGINE (shared source of truth)
# The shared IPC engine, consumed by bench-scale's measure_ipc.
# The methodology that makes it trustworthy: ONE clean handoff pair per
# primitive looping a FIXED round count -- not many pairs contending and
# aggregated (the old bench-scale measure_ipc, whose cross-pair contention
# inflated p99/worst into noise). gc is disabled in the workload so the
# harness's own GC pauses don't pollute the latency tail; the processes rename
# themselves to IPC_COMM so `montauk --trace pand-ipc` can target them.

IPC_COMM = "pand-ipc"
IPC_RTT_PRIMS = ["pipe", "socket", "eventfd", "sem"]
IPC_DEFAULT_ROUNDS = 20000
IPC_WARMUP_SECS = 2.0

_IPC_HEAD = (
    "import os,time,ctypes,ctypes.util,gc\n"
    "gc.disable()\n"
    "libc=ctypes.CDLL(ctypes.util.find_library('c'))\n"
    f"libc.prctl(15,b'{IPC_COMM}',0,0,0)\n"
)


def ipc_rtt_script(prim, rounds):
    """One handoff pair, `rounds` round-trips over `prim`; prints per-round us."""
    if prim == "pipe":
        setup = "r1,w1=os.pipe();r2,w2=os.pipe()\n"
        cwait, cwake = "os.read(r1,1)", "os.write(w2,b'x')"
        pwake, pwait = "os.write(w1,b'x')", "os.read(r2,1)"
    elif prim == "socket":
        setup = "import socket\nA,B=socket.socketpair()\n"
        cwait, cwake = "B.recv(1)", "B.send(b'x')"
        pwake, pwait = "A.send(b'x')", "A.recv(1)"
    elif prim == "eventfd":
        setup = "e1=os.eventfd(0);e2=os.eventfd(0)\n"
        cwait, cwake = "os.eventfd_read(e1)", "os.eventfd_write(e2,1)"
        pwake, pwait = "os.eventfd_write(e1,1)", "os.eventfd_read(e2)"
    elif prim == "sem":
        setup = "from multiprocessing import Semaphore\ns1=Semaphore(0);s2=Semaphore(0)\n"
        cwait, cwake = "s1.acquire()", "s2.release()"
        pwake, pwait = "s1.release()", "s2.acquire()"
    else:
        raise ValueError(prim)
    return (_IPC_HEAD + f"N={rounds}\n" + setup +
            "pid=os.fork()\n"
            "if pid==0:\n"
            " for _ in range(N):\n"
            f"  {cwait};{cwake}\n"
            " os._exit(0)\n"
            "lat=[]\n"
            "for _ in range(N):\n"
            f" t=time.monotonic();{pwake};{pwait}\n"
            " lat.append(time.monotonic()-t)\n"
            "os.waitpid(pid,0)\n"
            "import sys\n"
            "sys.stdout.write('\\n'.join(str(int(l*1e6)) for l in lat))\n")


def ipc_fanout_script(rounds, k):
    """1 parent -> K children; each round wakes all K and waits for all K
    (the 1:N server burst). Latency = burst-completion time."""
    return (_IPC_HEAD + f"R={rounds};K={k}\n"
            "P=[(os.pipe(),os.pipe()) for _ in range(K)]\n"
            "kids=[]\n"
            "for i in range(K):\n"
            " pid=os.fork()\n"
            " if pid==0:\n"
            "  (r1,w1),(r2,w2)=P[i]\n"
            "  for _ in range(R): os.read(r1,1);os.write(w2,b'x')\n"
            "  os._exit(0)\n"
            " kids.append(pid)\n"
            "lat=[]\n"
            "for _ in range(R):\n"
            " t=time.monotonic()\n"
            " for i in range(K): os.write(P[i][0][1],b'x')\n"
            " for i in range(K): os.read(P[i][1][0],1)\n"
            " lat.append(time.monotonic()-t)\n"
            "for p in kids: os.waitpid(p,0)\n"
            "import sys\n"
            "sys.stdout.write('\\n'.join(str(int(l*1e6)) for l in lat))\n")


def ipc_start_stress(cores, reserve=()):
    """Pin one non-yielding stress worker to each online CPU < cores, skipping
    any CPU in `reserve` (bench-ipc reserves cpu0 as montauk's drain core so it
    never drops events)."""
    workers = []
    for cpu in range(cores):
        if cpu in reserve:
            continue
        workers.append(subprocess.Popen(
            [str(BINARY), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return workers


def ipc_stop_stress(workers):
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()


def ipc_run_script(script, cpu_set=None):
    cmd = [sys.executable, "-c", script]
    if cpu_set is not None:
        cmd = ["taskset", "-c", cpu_set] + cmd
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=180)
        return out
    except subprocess.TimeoutExpired:
        p.kill()
        return ""


def ipc_lat_dist(out):
    v = [int(x) for x in out.split() if x.strip().lstrip("-").isdigit()]
    if not v:
        return None
    return {"n": len(v), "p50": int(percentile(v, 50)),
            "p99": int(percentile(v, 99)),
            "p999": int(percentile(v, 99.9)), "worst": max(v)}


def measure_ipc_cell(cores, rounds=IPC_DEFAULT_ROUNDS, prims=None,
                     fanout=True):
    """Clean per-primitive IPC RTT under stress at `cores`. One pair per
    primitive (not contending pairs), `rounds` each. Returns
    {rtt_<prim>: dist, 'fanout': dist}; dist = {n,p50,p99,p999,worst} or None."""
    if prims is None:
        prims = IPC_RTT_PRIMS
    cell = {}
    workers = ipc_start_stress(cores)
    time.sleep(IPC_WARMUP_SECS)
    try:
        for prim in prims:
            cell[f"rtt_{prim}"] = ipc_lat_dist(
                ipc_run_script(ipc_rtt_script(prim, rounds)))
        if fanout:
            cell["fanout"] = ipc_lat_dist(
                ipc_run_script(ipc_fanout_script(max(1, rounds // 4), cores)))
    finally:
        ipc_stop_stress(workers)
    return cell
