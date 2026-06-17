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
# montauk --trace recordings (.prom snapshots + .events raw logs) can be large.
# Keep them in /tmp per the project log convention, off the home cache.
TRACE_DIR = Path("/tmp/pandemonium")
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
# One logger for the whole suite. Every line is `[HH:MM:SS] [LEVEL]   message`,
# the message flush at a fixed column -- FLAT: any leading whitespace a caller
# typed is stripped, so indentation is never carried in the text. Grouping is a
# SECTION line plus a single blank line, never indent. See tests/TERMINAL_STYLE.md.

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
# Pad after the level tag so the message column is identical for every level:
# [INFO]/[WARN] + 3 spaces, [ERROR]/[DEBUG] + 2 spaces.
_TAG_PAD = {"INFO": "   ", "WARN": "   ", "ERROR": "  ", "DEBUG": "  "}


def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


class _Log:
    """The suite's single logger. Free functions log_info/warn/error/debug
    delegate here, so every existing call site gains flat output, level gating,
    and child mode without changing. New affordances: section(), report(),
    blank(), interrupted()."""

    def __init__(self):
        # Terminal floor: INFO by default; verbose lowers to DEBUG, quiet raises
        # to WARN. DEBUG is for the /tmp logs and -v.
        self.floor = _LEVELS["INFO"]
        # Child mode: a sub-bench spawned under another (PANDEMONIUM_CHILD=1)
        # suppresses banners/preamble/verbatim reports/dmesg so a profile run
        # reads as one uniform progress log.
        self.child = os.environ.get("PANDEMONIUM_CHILD") == "1"

    def set_verbosity(self, quiet: bool = False, verbose: bool = False) -> None:
        self.floor = _LEVELS["WARN"] if quiet else (
            _LEVELS["DEBUG"] if verbose else _LEVELS["INFO"])

    def _emit(self, level: str, msg) -> None:
        if _LEVELS[level] < self.floor:
            return
        # FLAT: strip leading whitespace -- indentation is never in the text.
        print(f"{_timestamp()} [{level}]{_TAG_PAD[level]}{str(msg).lstrip()}",
              flush=True)

    def info(self, msg) -> None:
        self._emit("INFO", msg)

    def warn(self, msg) -> None:
        self._emit("WARN", msg)

    def error(self, msg) -> None:
        self._emit("ERROR", msg)

    def debug(self, msg) -> None:
        self._emit("DEBUG", msg)

    def blank(self) -> None:
        """The only separator. A single blank line between sections."""
        print(flush=True)

    def section(self, title: str) -> None:
        """A phase header: one blank line, then the title. The one grammar for
        every phase (replaces PHASE:/Scheduler:/[NC]/[x] running/WORKLOAD)."""
        self.blank()
        self.info(title)

    def report(self, text: str) -> None:
        """A pre-formatted block (a table, a montauk_analyze digest) printed
        verbatim -- no timestamps -- framed by one blank line. Suppressed in
        child mode (the parent owns the final report)."""
        if self.child:
            return
        self.blank()
        print(text.rstrip("\n"), flush=True)
        self.blank()

    def interrupted(self) -> None:
        """The one Ctrl+C line, everywhere."""
        self.warn("interrupted -- cleaning up")


log = _Log()


def log_info(msg: str) -> None:
    log.info(msg)


def log_warn(msg: str) -> None:
    log.warn(msg)


def log_error(msg: str) -> None:
    log.error(msg)


def log_debug(msg: str) -> None:
    log.debug(msg)


# Distros the project supports; each `install_hint` map is keyed by these so a
# missing dependency is never a dead end -- the user is told the exact command
# for their system (detected first).
SUPPORTED_DISTROS = ("cachyos", "arch", "gentoo", "opensuse", "ubuntu", "nixos")


def detect_distro() -> str:
    """Best-effort distro key from /etc/os-release (ID, then ID_LIKE). Maps
    families (suse->opensuse, debian->ubuntu) onto a supported key; "" if none."""
    try:
        data = Path("/etc/os-release").read_text()
    except OSError:
        return ""
    cand = ""
    for line in data.splitlines():
        k, _, v = line.partition("=")
        if k in ("ID", "ID_LIKE"):
            cand += " " + v.strip().strip('"').lower()
    for key in ("cachyos", "arch", "gentoo", "opensuse", "suse", "ubuntu",
                "debian", "nixos"):
        if key in cand:
            return {"suse": "opensuse", "debian": "ubuntu"}.get(key, key)
    return ""


def install_hint(what: str, hints: dict) -> None:
    """Tell the user how to install a missing dependency on every supported
    distro, leading with the one detected on this box. `hints` maps a distro key
    (see SUPPORTED_DISTROS) to its install command."""
    log_error(f"{what} is required but not installed.")
    log_info("install it with:")
    distro = detect_distro()
    ordered = ([distro] if distro in hints else
               []) + [d for d in SUPPORTED_DISTROS if d != distro and d in hints]
    for d in ordered:
        mark = "  <- your system" if d == distro else ""
        log_info(f"    {d:9} {hints[d]}{mark}")


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
    if not log.child:
        log_info(f"Kernel {release} OK (>= {MIN_KERNEL[0]}.{MIN_KERNEL[1]})")
    return True


def ensure_vmlinux_h() -> bool:
    """Check vmlinux.h cache. Generated by bpftool on first build if missing."""
    if VMLINUX_CACHE.exists() and VMLINUX_CACHE.stat().st_size > 1000:
        if not log.child:
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
            if not log.child:
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
# Per project doctrine these post-collection stats route through the sublimation
# CLI (>= 7.5.0: `mean`, `stdev`, `quantile --nearest`), which reproduces the
# definitions below bit-for-bit. The pure-Python paths remain as the fallback
# when sublimation is absent or errors, so the suite never depends on it being
# installed and a bad invocation can never crash a bench. These are called once
# per measurement cell (post-collection), NEVER on a per-sample hot path, so the
# subprocess cost is immaterial and never perturbs a live latency probe.

_SUBLIMATION = shutil.which("sublimation")


def _sub_numeric(values: list[float], args: list[str]):
    """Pipe `values` to `sublimation <args>` and return the parsed result (int
    when integral, else float), or None on any failure so the caller falls back
    to pure Python."""
    if not _SUBLIMATION or not values:
        return None
    try:
        r = subprocess.run(
            [_SUBLIMATION, *args],
            input="\n".join(repr(v) for v in values),
            capture_output=True, text=True, timeout=10,
        )
        s = r.stdout.strip()
        if r.returncode != 0 or not s:
            return None
        return int(s) if not any(c in s for c in ".eEnN") else float(s)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def mean_stdev(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    m = _sub_numeric(values, ["mean"])
    if m is None:
        m = sum(values) / n
        if n < 2:
            return m, 0.0
        variance = sum((x - m) ** 2 for x in values) / (n - 1)
        return m, math.sqrt(variance)
    m = float(m)
    if n < 2:
        return m, 0.0
    sd = _sub_numeric(values, ["stdev"])
    if sd is None:
        variance = sum((x - m) ** 2 for x in values) / (n - 1)
        sd = math.sqrt(variance)
    return m, float(sd)


def percentile(values: list[float], pct: float) -> float:
    """Compute percentile (0-100) using nearest-rank method."""
    if not values:
        return 0.0
    r = _sub_numeric(values, ["quantile", repr(pct / 100.0), "--nearest"])
    if r is not None:
        return r
    s = sorted(values)
    k = max(0, min(int(math.ceil(pct / 100.0 * len(s))) - 1, len(s) - 1))
    return s[k]


# REPORT TABLES
# Canonical aesthetic (source: bench-scale). One header row of plain column
# names, then aligned data rows: label column left-justified, numeric columns
# right-justified to a fixed width, units carried inline in the cell strings.
# No decorative separators, no box-drawing -- a single blank line separates
# sections. Every emitter in the suite builds tables through these two helpers
# so widths and alignment stay identical across benches.

LABEL_W = 28   # canonical label (first) column width
COL_W = 10     # canonical numeric column width


def table_header(label: str, columns: list[str],
                 label_w: int = LABEL_W, col_w: int = COL_W) -> str:
    """A header row: left-justified label, right-justified column names."""
    body = "".join(f" {c:>{col_w}}" for c in columns)
    return f"{label:<{label_w}}{body}"


def table_row(label: str, cells: list[str],
              label_w: int = LABEL_W, col_w: int = COL_W) -> str:
    """A data row. `cells` are pre-formatted strings (caller owns precision and
    units) so the same helper serves times, counts, percentages, and ratios."""
    body = "".join(f" {c:>{col_w}}" for c in cells)
    return f"{label:<{label_w}}{body}"


# PROMETHEUS
# UNIFIED METRIC SCHEMA (ratified -- all emitters follow it, no exceptions):
#   name:       pandemonium_<bench>_<metric>   (bench in: scale, contention,
#               pcpu, scx, fork_thread, cachyos, power, enduser)
#   metadata:   exactly ONE pandemonium_<bench>_info{version,git_commit,
#               git_dirty,...} gauge = 1, plus ONE pandemonium_<bench>_
#               timestamp_seconds gauge. NEVER repeat version/commit per metric
#               line (bench-cachyos did; that is removed).
#   labels:     scheduler=, cores= or cpus=, workload=, phase= -- lowercase,
#               quoted. Numeric scope (core count) is a label, not part of name.
#   HELP/TYPE:  emitted once per distinct metric name, before its first sample.
# The builder enforces all of the above; do not hand-format .prom lines.

class PrometheusBuilder:
    """Single source of .prom emission for the whole suite. Replaces the four
    forked write_prometheus() implementations. Usage:
        pb = PrometheusBuilder("fork_thread")
        pb.info(version=ver, git_commit=git["commit"], git_dirty=git["dirty"])
        pb.gauge("time_seconds", 6.245, help="wall time", labels={"scheduler": "eevdf"})
        pb.hist("latency_us", buckets, total, summ)   # cumulative buckets
        Path(out).write_text(pb.render())
    """

    def __init__(self, bench: str):
        self.bench = bench
        self.prefix = f"pandemonium_{bench}"
        self._declared: set[str] = set()
        self._lines: list[str] = []

    @staticmethod
    def _fmt_labels(labels: dict | None) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return "{" + inner + "}"

    def _declare(self, name: str, help_text: str, mtype: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        if help_text:
            self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")

    def gauge(self, metric: str, value, help: str = "",
              labels: dict | None = None) -> None:
        name = f"{self.prefix}_{metric}"
        self._declare(name, help, "gauge")
        self._lines.append(f"{name}{self._fmt_labels(labels)} {value}")

    def info(self, ts: int | None = None, **labels) -> None:
        """The single metadata gauge + a single timestamp gauge. git_dirty
        coerced to 0/1; everything else stringified. `ts` is the run's unix time
        (pass the stamp-derived value); defaults to now. Call once, first."""
        if "git_dirty" in labels:
            labels["git_dirty"] = 1 if labels["git_dirty"] else 0
        self.gauge("info", 1, help="build/run metadata (value always 1)",
                   labels=labels)
        self.gauge("timestamp_seconds", int(ts) if ts is not None else int(time.time()),
                   help="unix time of the run")

    def hist(self, metric: str, buckets: list[tuple[float, int]],
             count: int, total_sum: float, help: str = "",
             labels: dict | None = None) -> None:
        """Cumulative histogram. `buckets` is [(le, cumulative_count), ...]
        already accumulated; +Inf is appended automatically."""
        name = f"{self.prefix}_{metric}"
        self._declare(name, help, "histogram")
        base = self._fmt_labels(labels)
        ljoin = (base[:-1] + ",") if base else "{"
        for le, c in buckets:
            self._lines.append(f'{name}_bucket{ljoin}le="{le}"}} {c}')
        self._lines.append(f'{name}_bucket{ljoin}le="+Inf"}} {count}')
        self._lines.append(f"{name}_sum{base} {total_sum}")
        self._lines.append(f"{name}_count{base} {count}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


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


class DmesgMonitor:
    """Active crash detection via dmesg polling.

    Snapshots dmesg at construction, .check() polls for crash patterns,
    .save() writes new lines to log file with keyword-filtered summary.
    """

    CRASH_PATTERNS = [
        "failed to run for",
        "runnable task stall",
    ]
    # Disable lines that indicate a real fault. "(unregistered from user
    # space)" is a clean userspace shutdown -- not a crash.
    DISABLE_FAULT_MARKERS = [
        "(runtime error)",
        "(timeout)",
        "errored",
    ]
    KEYWORDS = ["sched_ext", "pandemonium", "non-existent DSQ", "zero slice",
                "panic", "BUG:", "RIP:", "Oops", "Call Trace"]

    def __init__(self):
        r = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True)
        self.baseline = len(r.stdout.splitlines()) if r.returncode == 0 else 0
        self.crashed = False
        self.crash_msg = ""
        # Track every sched_ext disable line, even clean ones, for reporting.
        self.disable_msg = ""

    def _new_lines(self) -> list[str]:
        r = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True)
        if r.returncode != 0:
            return []
        lines = r.stdout.splitlines()
        return lines[self.baseline:] if self.baseline < len(lines) else []

    def check(self) -> bool:
        """Poll for crash patterns. Returns True if a real crash is detected.
        Records clean shutdown messages in self.disable_msg without flagging
        as a crash."""
        # dmesg is evidence, not progress: the per-event sched_ext lines are NOT
        # streamed to the terminal (TERMINAL_STYLE) -- they land in the dmesg-*.log
        # artifact and save() prints the one-line summary. check() only scans for
        # a real crash here.
        for line in self._new_lines():
            for pattern in self.CRASH_PATTERNS:
                if pattern in line:
                    self.crashed = True
                    self.crash_msg = line.strip()
                    return True
            if "disabled" in line and "sched_ext" in line:
                self.disable_msg = line.strip()
                # Only flag as crash for fault-shaped disable reasons.
                if any(m in line for m in self.DISABLE_FAULT_MARKERS):
                    self.crashed = True
                    self.crash_msg = line.strip()
                    return True
                # "unregistered from user space" / clean disable: no crash.
        return False

    def save(self, stamp: str | None = None) -> None:
        """Save new dmesg lines to log file, print keyword-filtered summary."""
        new_lines = self._new_lines()
        if not new_lines:
            log_info("dmesg: no new kernel messages")
            return

        if stamp is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        dmesg_path = LOG_DIR / f"dmesg-{stamp}.log"
        dmesg_path.write_text("\n".join(new_lines) + "\n")

        filtered = [l for l in new_lines
                    if any(kw in l for kw in self.KEYWORDS)]

        if not filtered:
            log_info(f"dmesg: {len(new_lines)} messages, no scheduler issues")
            return

        crashes = sum(1 for l in filtered
                      if "non-existent DSQ" in l or "runtime error" in l)
        zero_slices = sum(1 for l in filtered if "zero slice" in l)
        panics = sum(1 for l in filtered
                     if "panic" in l or "BUG:" in l or "RIP:" in l)

        if panics:
            log_error(f"dmesg: KERNEL PANIC/BUG -- see {dmesg_path}")
        if crashes:
            log_warn(f"dmesg: {crashes} scheduler crash(es)")
        if zero_slices:
            log_warn(f"dmesg: {zero_slices} zero-slice warning(s)")

        for line in filtered:
            log_info(f"  {line.strip()}")

        log_info(f"dmesg: {len(new_lines)} messages saved to {dmesg_path}")


class MontaukTrace:
    """Context manager driving a montauk eBPF recording around a workload.

    __enter__ launches `montauk --trace PATTERN --log DIR`, waits for attach
    (first montauk_*.prom), then records an optional quiet baseline. __exit__
    stops montauk the way Ctrl+C would (SIGINT, escalating to TERM/KILL) and
    chowns the recording back to the sudo invoker. `.dir` is the recording
    directory, ready for `montauk_analyze`.

    The caller owns the scheduler and the workload; this owns only montauk.
    """

    def __init__(self, pattern, label, stamp, log_dir=TRACE_DIR,
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


def montauk_trace(pattern, label, stamp, log_dir=TRACE_DIR, **kw) -> "MontaukTrace":
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


def kernel_config():
    """Read the running kernel's config (zcat /proc/config.gz, else /boot)."""
    import gzip
    try:
        with gzip.open("/proc/config.gz", "rt") as f:
            return f.read()
    except OSError:
        pass
    try:
        return Path(f"/boot/config-{os.uname().release}").read_text()
    except OSError:
        return ""


def stall_susceptibility():
    """Profile the factors that decide whether the dark-CPU dispatch strand can
    even HAPPEN on this machine -- the answer to 'why does box X reproduce and box
    Y never does.' The strand needs a CPU to go tickless-idle with a pinned task
    queued on it; the width of that dark window is set by HZ, the tickless mode,
    C-state depth, core count and any nohz_full/isolcpus. Returns (lines, score)
    where higher score = wider dark window = more likely to reproduce. Read-only.
    Shared by bench-coldwake and bench-enduser so every report carries it and two
    machines can be diffed directly."""
    cfg = kernel_config()
    def cfg_get(key):
        for ln in cfg.splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
        return ""
    hz = cfg_get("CONFIG_HZ") or "?"
    nohz = ("NO_HZ_FULL" if cfg_get("CONFIG_NO_HZ_FULL") == "y"
            else "NO_HZ_IDLE" if cfg_get("CONFIG_NO_HZ_IDLE") == "y"
            else "periodic")
    preempt = ("PREEMPT_RT" if cfg_get("CONFIG_PREEMPT_RT") == "y"
               else "PREEMPT_DYNAMIC" if cfg_get("CONFIG_PREEMPT_DYNAMIC") == "y"
               else "PREEMPT" if cfg_get("CONFIG_PREEMPT") == "y"
               else "VOLUNTARY/NONE")
    try:
        cmdline = Path("/proc/cmdline").read_text().strip()
    except OSError:
        cmdline = ""
    nohz_full = next((t.split("=", 1)[1] for t in cmdline.split()
                      if t.startswith("nohz_full=")), "")
    isolcpus = next((t.split("=", 1)[1] for t in cmdline.split()
                     if t.startswith("isolcpus=")), "")
    ncpu = os.cpu_count() or 1
    # Deepest idle state available on cpu0 (deeper = longer wake from idle).
    deepest, ncstates = "none", 0
    base = Path("/sys/devices/system/cpu/cpu0/cpuidle")
    if base.is_dir():
        states = sorted(base.glob("state*"))
        ncstates = len(states)
        for s in states:
            try:
                nm = (s / "name").read_text().strip()
                if (s / "disable").read_text().strip() == "0":
                    deepest = nm
            except OSError:
                pass
    gov = ""
    try:
        gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        pass

    # Score the dark-window width. Each factor that widens it adds weight. The
    # nohz_full CONFIG being present does not mean any core is actually tickless
    # at runtime unless nohz_full= designates one -- so the raw factors below are
    # what to read, not just the score.
    score, why = 0, []
    try:
        hz_n = int(hz)
        if hz_n <= 100: score += 3; why.append(f"HZ={hz} (wide ticks)")
        elif hz_n <= 300: score += 2; why.append(f"HZ={hz}")
        elif hz_n >= 1000: why.append(f"HZ={hz} (tight ticks -- narrows window)")
    except ValueError:
        pass
    if nohz == "NO_HZ_FULL": score += 3; why.append("NO_HZ_FULL (fully tickless)")
    elif nohz == "NO_HZ_IDLE": score += 1
    if nohz_full: score += 3; why.append(f"nohz_full={nohz_full} (forced tickless cores)")
    if isolcpus: score += 2; why.append(f"isolcpus={isolcpus}")
    if ncpu >= 16: score += 3; why.append(f"{ncpu} CPUs (many idle at once)")
    elif ncpu >= 8: score += 2; why.append(f"{ncpu} CPUs")
    elif ncpu <= 4: why.append(f"{ncpu} CPUs (few idle -- narrows window)")
    dl = deepest.upper()
    if any(d in dl for d in ("C6", "C8", "C10")): score += 2; why.append(f"deep idle {deepest}")
    if preempt in ("VOLUNTARY/NONE",): score += 1; why.append(f"{preempt} preempt")

    verdict = ("HIGH -- this box should reproduce" if score >= 7
               else "MODERATE" if score >= 4
               else "LOW -- this box may NOT reproduce the strand")
    lines = [
        "STALL SUSCEPTIBILITY",
        f"  kernel     {os.uname().release}",
        f"  tick       HZ={hz}  {nohz}  preempt={preempt}",
        f"  cpus       {ncpu}  governor={gov or '?'}  deepest-idle={deepest} ({ncstates} states)",
        f"  cmdline    nohz_full={nohz_full or '-'}  isolcpus={isolcpus or '-'}",
        f"  verdict    score {score} -> {verdict}",
    ]
    if why:
        lines.append("  factors    " + "; ".join(why))
    return lines, score
