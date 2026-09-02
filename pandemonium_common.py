"""
Shared infrastructure for PANDEMONIUM build/test scripts.

Used by pandemonium.py (build manager) and tests/pandemonium-tests.py (test orchestrator).
"""

import glob
import math
import os
import platform
import re
import shutil
import atexit
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
        """A pre-formatted block (a table, a montauk --analyze digest) printed
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


def kernel_at_least(major: int, minor: int) -> bool:
    """True if the running kernel is >= major.minor."""
    try:
        parts = platform.release().split(".")
        return (int(parts[0]), int(parts[1])) >= (major, minor)
    except (IndexError, ValueError):
        return False


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
    """Pipe `values` to `sublimation <args>` and return the parsed result (int when
    integral, else float). montauk/sublimation are a HARD dependency -- there is no
    Python fallback; a failure raises rather than silently diverge from the tool."""
    if not _SUBLIMATION:
        raise RuntimeError("sublimation is required (it ships with montauk) -- install montauk")
    r = subprocess.run(
        [_SUBLIMATION, *args],
        input="\n".join(repr(v) for v in values),
        capture_output=True, text=True, timeout=10,
    )
    s = r.stdout.strip()
    if r.returncode != 0 or not s:
        raise RuntimeError(f"sublimation {' '.join(args)} failed: "
                           f"{r.stderr.strip() or 'no output'}")
    return int(s) if not any(c in s for c in ".eEnN") else float(s)


# MONTAUK REPORT -- the one invocation for "run montauk --analyze --report over a
# capture." Returns BOTH faces of the result: the TEXT report (montauk's own
# human verdict -- callers print it verbatim, montauk is the instrument) and
# the STRUCTURED envelope from --json (the machine-read side; None when the
# installed montauk predates --json for this mode or the envelope fails to
# parse). Callers parse the ENVELOPE, never the text -- text-scraping montauk
# output is the exact anti-pattern this helper retires; the per-bench regex
# parsers survive only as the explicit fallback for a None envelope.

def montauk_report(events, report: str):
    """(text, envelope|None) for one montauk --analyze report over a capture."""
    import json as _json
    analyze = montauk_analyze_argv()
    rc, so, se = run_cmd_capture(
        [*analyze, str(events), "--report", report])
    if rc != 0:
        log_error(f"montauk --analyze failed (rc={rc}): {se.strip()[:200]}")
        return so + se, None
    rj, sj, _ej = run_cmd_capture(
        [*analyze, str(events), "--report", report, "--json"])
    envelope = None
    if rj == 0 and sj.strip():
        try:
            envelope = _json.loads(sj)
        except ValueError:
            log_warn(f"montauk --analyze --json emitted an unparseable envelope "
                     f"for {report} -- falling back to the text parser")
    return so, envelope


def envelope_report(envelope, name: str) -> dict:
    """The named report dict out of a montauk envelope ({} when absent).
    Accepts both the digest-style {'reports': [...]} wrapper and a bare
    single-report envelope."""
    if not isinstance(envelope, dict):
        return {}
    for rep in envelope.get("reports", []):
        if rep.get("name") == name:
            return rep
    # A DIGEST PUBLISHES THREE REPORTS IN FULL AND ALL 28 CONCLUSIONS BESIDE
    # THEM. montauk keeps `reports` to the headline set on purpose -- the digest
    # is the KB-scale shareable artifact -- and carries {name, class, verdict}
    # for EVERY report in `conclusions` precisely so a structured consumer picks
    # for its own audience instead of montauk picking three on its behalf. A
    # reader that only walks `reports` gets nothing for a class the envelope is
    # holding, which reads as a capture that did not carry it. It did.
    for con in envelope.get("conclusions", []):
        if con.get("name") == name:
            return con
    return envelope if envelope.get("name") == name else {}


def montauk_envelope(target, *, digest: bool | None = None,
                     report: str | None = None):
    """The montauk --json envelope for a capture, or None.

    THE ONE PLACE THE SUITE OPENS AN ENVELOPE. montauk publishes a typed result
    per report -- verdict, klass, gauges, offenders -- and the benches were each
    reaching into the JSON by hand for the two or three fields they happened to
    want. That is why 21 of montauk's 29 reports had no reader: Not a decision,
    just that every one of them would have cost another bespoke parser. This trio
    existed and only prism-locality (orphaned) and prism-strand ever called it.

    THE TARGET'S SHAPE PICKS THE MODE, because `--digest` is accepted for a
    RECORDING DIRECTORY and rejected for a bare .events FILE -- `unknown flag
    '--digest'`, which is what a caller gets for passing the sibling events path
    to a function defaulting the other way. Defaulting `digest` to the answer to
    "is this a directory" removes the one thing the caller was expected to
    remember and got wrong. Pass it explicitly only to override.

    `digest` reads the digest envelope, which carries capture completeness.
    `report` reads one named report over an event stream and never digests.
    """
    import json as _json
    if digest is None:
        digest = Path(target).is_dir()
    argv = [*montauk_analyze_argv(), str(target)]
    argv += ["--report", report] if report else (["--digest"] if digest else [])
    rc, so, se = run_cmd_capture(argv + ["--json"])
    if rc != 0 or not so.strip():
        log_warn(f"montauk --analyze produced no envelope for {target} "
                 f"(rc={rc}): {se.strip()[:160]}")
        return None
    try:
        return _json.loads(so)
    except ValueError:
        log_warn(f"montauk --analyze emitted an unparseable envelope for {target}")
        return None


def envelope_gauge_by_label(report_dict: dict, name: str, label: str) -> dict:
    """{label_value: gauge_value} for a gauge published once PER LABEL.

    montauk emits a distribution as ONE gauge name repeated across a label --
    montauk_analysis_slice_us at quantile="0.5", "0.99", "worst" -- and
    envelope_gauges() keys by name alone, so it keeps the first and drops the
    rest silently. A caller reading a p99 out of it gets the p50 and no error.
    """
    out = {}
    for g in report_dict.get("gauges") or []:
        if g.get("name") != name or "value" not in g:
            continue
        m = re.search(rf'{re.escape(label)}="([^"]+)"', g.get("labels", "") or "")
        if m:
            out[m.group(1)] = g["value"]
    return out


def envelope_verdict(report_dict: dict) -> tuple[str, str]:
    """(klass, verdict) for one report. klass is the COMPARABLE half.

    montauk keeps these separate on purpose: The verdict sentence carries numbers
    and therefore drifts, while klass is a short token from a small fixed set --
    "what a behavioral golden can compare EXACTLY, which is the whole reason it
    exists separately rather than being parsed back out of the prose". The suite
    had never compared one, so a class flipping between two arms of the same run
    (SATURATED vs PLACEMENT-MISS, ORDER-STARVED vs PREEMPT-STARVED) went unread.
    """
    return (report_dict.get("class") or "",
            report_dict.get("verdict") or "")


def envelope_offenders(report_dict: dict) -> list:
    """The ranked misbehaving entities one report names, worst first.

    montauk ranks these by severity across every report and the suite printed
    tables that never said WHO. Each entry is {kind, id, obj, metric, value, sev}.
    """
    offs = report_dict.get("offenders") or []
    return sorted(offs, key=lambda o: (-int(o.get("sev", 0)),
                                       -float(o.get("value", 0) or 0)))


def envelope_capture(envelope) -> dict:
    """Capture completeness for an envelope, however it is shaped.

    The DIGEST envelope carries these under "digest"; only the full --analyze
    envelope has a top-level "trace". Reading the wrong one reports UNKNOWN on a
    capture that knew perfectly well, which is a mistake this suite has already
    made once. Absent keys mean UNKNOWN loss, never proven-clean -- montauk
    reports absence rather than zero when a capture predates drop accounting.
    """
    if not isinstance(envelope, dict):
        return {}
    src = envelope.get("digest") if isinstance(envelope.get("digest"), dict) else None
    if src is None or "capture_completeness" not in src:
        src = envelope.get("trace") if isinstance(envelope.get("trace"), dict) else src
    if not isinstance(src, dict) or "capture_completeness" not in src:
        return {}
    return {"completeness": float(src["capture_completeness"]),
            "dropped": int(src.get("dropped_events", 0)),
            "observed": int(src.get("events_observed", src.get("events", 0)))}


def envelope_gauges(report_dict: dict) -> dict:
    """{gauge_name: value} from one envelope report's gauges list."""
    out = {}
    for g in report_dict.get("gauges", []):
        n = g.get("name")
        if n is not None and "value" in g:
            out.setdefault(n, g["value"])
    return out


# SUDO CREDENTIAL LIFECYCLE -- the one implementation. Every bench that runs
# privileged steps warms the cache once up front (one clean password prompt on
# the tty) and refreshes it between long workloads so a timestamp never expires
# mid-measurement. Replaces the per-file _warm_sudo/_refresh_sudo copies and
# the inline `sudo true` warmups.

def warm_sudo() -> None:
    """Prompt for sudo credentials if not cached; exit on auth failure."""
    if os.geteuid() == 0:
        return
    r = subprocess.run(["sudo", "true"])
    if r.returncode != 0:
        log_error("sudo authentication failed")
        sys.exit(1)


def refresh_sudo() -> None:
    """Refresh cached sudo credentials between long workloads."""
    if os.geteuid() != 0:
        subprocess.run(["sudo", "-v"], capture_output=True)


def mean_stdev(values: list[float]) -> tuple[float, float]:
    # mean + sample (n-1) stdev, both from sublimation (hard dependency, no fallback).
    if not values:
        return 0.0, 0.0
    m = float(_sub_numeric(values, ["mean"]))
    if len(values) < 2:
        return m, 0.0
    return m, float(_sub_numeric(values, ["stdev"]))


def percentile(values: list[float], pct: float) -> float:
    """Percentile (0-100), nearest-rank, via sublimation (hard dependency, no fallback)."""
    if not values:
        return 0.0
    return float(_sub_numeric(values, ["quantile", repr(pct / 100.0), "--nearest"]))


def mean(values: list[float]) -> float:
    """Mean via sublimation (hard dependency, no fallback)."""
    return float(_sub_numeric(values, ["mean"])) if values else 0.0


def median(values: list[float]) -> float:
    """Median (interpolated 0.5-quantile) via sublimation."""
    return float(_sub_numeric(values, ["quantile", "0.5"])) if values else 0.0


def variance(values: list[float]) -> float:
    """Sample (n-1) variance via sublimation."""
    return float(_sub_numeric(values, ["variance"])) if values else 0.0


# REPORT TABLES
# Canonical aesthetic (source: prism-scale). One header row of plain column
# names, then aligned data rows: label column left-justified, numeric columns
# right-justified to a fixed width, units carried inline in the cell strings.
# No decorative separators, no box-drawing -- a single blank line separates
# sections. Every emitter in the suite builds tables through these two helpers
# so widths and alignment stay identical across benches.

LABEL_W = 28   # canonical label (first) column width
COL_W = 10     # canonical numeric column width


def _table_widths(n: int, col_w):
    """Per-column widths from either one width for all, or one per column.

    A single int was the original contract and covers most tables. Benches whose
    columns are genuinely different sizes -- a 5-wide run count beside a 10-wide
    wall time -- had to hand-roll their rows instead, which is why two of them
    never adopted these helpers at all. A sequence is accepted and recycled if
    it is short, so the caller states widths once.
    """
    if isinstance(col_w, int):
        return [col_w] * n
    w = list(col_w)
    return [w[i % len(w)] for i in range(n)] if w else [COL_W] * n


def table_header(label: str, columns: list[str],
                 label_w: int = LABEL_W, col_w=COL_W) -> str:
    """A header row: left-justified label, right-justified column names."""
    ws = _table_widths(len(columns), col_w)
    body = "".join(f" {c:>{w}}" for c, w in zip(columns, ws))
    return f"{label:<{label_w}}{body}"


def table_row(label: str, cells: list[str],
              label_w: int = LABEL_W, col_w=COL_W) -> str:
    """A data row. `cells` are pre-formatted strings (caller owns precision and
    units) so the same helper serves times, counts, percentages, and ratios."""
    ws = _table_widths(len(cells), col_w)
    body = "".join(f" {c:>{w}}" for c, w in zip(cells, ws))
    return f"{label:<{label_w}}{body}"


# PROMETHEUS
# UNIFIED METRIC SCHEMA (ratified -- all emitters follow it, no exceptions):
#   name:       pandemonium_<bench>_<metric>   (bench in: scale, contention,
#               pcpu, scx, fork_thread, cachyos, power, prism)
#   metadata:   exactly ONE pandemonium_<bench>_info{version,git_commit,
#               git_dirty,...} gauge = 1, plus ONE pandemonium_<bench>_
#               timestamp_seconds gauge. NEVER repeat version/commit per metric
#               line (prism-cachyos did; that is removed).
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


def cpu_is_online(cpu: int) -> bool:
    """Read (unprivileged) whether a CPU is online. cpu0 has no online file
    (never offlinable); a missing file otherwise reads as offline."""
    if cpu == 0:
        return True
    try:
        return Path(f"/sys/devices/system/cpu/cpu{cpu}/online") \
            .read_text().strip() == "1"
    except OSError:
        return False


def set_cpu_online(cpu: int, online: bool) -> bool:
    # IDEMPOTENT AND READ-FIRST: the state read is unprivileged, so a CPU
    # already in the requested state costs no subprocess and -- critically --
    # no sudo. Before this check, the module-level exit guard's cleanup ran
    # `sudo tee` for EVERY cpu on EVERY exit, so any unprivileged invocation
    # (even --help) blocked on a password prompt at interpreter exit.
    if cpu == 0:
        return True
    if cpu_is_online(cpu) == online:
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


# CANONICAL EXIT GUARD
# The ONE Ctrl+C / exit cleanup for the whole suite. Lifted from prism: on Ctrl+C
# or any exit, eject whatever sched_ext scheduler is registered (the systemd
# service OR a bare loader, by exact comm, force-killed if it ignores the signal)
# AND re-online every CPU the hotplug arms offlined. Idempotent, safe on every
# exit path. Every entry point installs this -- no hand-rolled signal/atexit
# cleanup anywhere else. (The old per-guard SIGINT missed the systemd scheduler
# and left the box stuck on a CPU subset; this does not.)

# DID THIS PROCESS PUT A SCHEDULER ON THE BOX? The exit guard installs at IMPORT of
# the suite, and eject_scheduler stops whatever scx is active without asking who
# started it -- so any process that merely IMPORTS the suite (a unit test loading a
# bench module to check its renderer, for one) would eject a scheduler the user was
# running, on its way out. Observed 2026-09-01; it only failed to land because the
# sudo prompt had no tty. Ejecting is correct for a run that ACTIVATED something and
# wrong for one that did not, and the two are told apart by this flag, not by which
# entry point was used.
_WE_ACTIVATED = [False]


def note_scheduler_activated() -> None:
    """Record that THIS process activated a scheduler, so exit teardown may eject it."""
    _WE_ACTIVATED[0] = True


def eject_scheduler(trace_mode: bool = False, interrupted: bool = False) -> None:
    """Leave NO sched_ext scheduler registered. In trace_mode the user's CURRENT
    scheduler is theirs -- never eject it. systemctl stop, then force-kill by exact
    comm (the ops name and the scx_-prefixed comm, then a cmdline match) for a bare
    loader systemctl does not own.

    `interrupted` says WHICH exit path fired: a signal, or a normal end-of-run
    atexit. The distinction is the whole message -- this used to log
    "interrupted" unconditionally, so a clean run ended on a WARN that read as an
    aborted one and flatly contradicted the report's own "scheduler ran clean"
    verdict written seconds earlier. Same teardown either way, honest label."""
    if trace_mode or not is_scx_active():
        return
    if not _WE_ACTIVATED[0]:
        # Someone else's scheduler. Leave it exactly as found.
        return
    name = scx_scheduler_name()
    if interrupted:
        log_warn(f"interrupted -- ejecting active scheduler ({name})")
    else:
        log_info(f"exit cleanup -- ejecting active scheduler ({name})")
    # sudo, never bare systemctl: unprivileged systemctl escalates to polkit,
    # whose TTY agent prints its red AUTHENTICATING banner mid-log and times out
    # if unanswered. sudo gives one plain password prompt on the tty (or none,
    # if the timestamp is still warm) and its output stays uncaptured so the
    # prompt is actually visible.
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    if prefix:
        log_info("stopping pandemonium.service (sudo may prompt)")
    subprocess.run(prefix + ["systemctl", "stop", "pandemonium"])
    if wait_for_deactivation(8.0):
        log_info("scheduler ejected -- back on stock EEVDF")
        return
    if name:
        pats = [name] if name.startswith("scx_") else [name, f"scx_{name}"]
        for p in pats:
            subprocess.run(["pkill", "-KILL", "-x", p], capture_output=True)
        if not wait_for_deactivation(3.0):
            subprocess.run(["pkill", "-KILL", "-f", f"scx_{name}"],
                           capture_output=True)
    if wait_for_deactivation(5.0):
        log_info("scheduler force-ejected -- back on stock EEVDF")
    else:
        log_warn("scheduler still registered -- clear with: "
                 "sudo systemctl stop pandemonium  (or reboot)")


_EXIT_CLEANUPS: list = []
_GUARD_INSTALLED: list = [False]


def register_exit_cleanup(fn) -> None:
    """Add an extra cleanup the exit guard runs BEFORE the eject + CPU-restore
    (e.g. a bench's own scheduler-guard teardown). Use this instead of a private
    atexit/signal handler."""
    if fn not in _EXIT_CLEANUPS:
        _EXIT_CLEANUPS.append(fn)


def install_exit_guard(eject: bool = True, trace_mode: bool = False) -> None:
    """Install THE canonical SIGINT/SIGTERM/atexit cleanup: run registered
    cleanups, eject the scheduler (unless trace_mode), then re-online every CPU.
    Idempotent -- call once per entry point. This replaces every hand-rolled
    signal/atexit cleanup in the suite."""
    if _GUARD_INSTALLED[0]:
        return
    _GUARD_INSTALLED[0] = True

    ran = [False]

    def _cleanup(interrupted: bool = False) -> None:
        # Once. The SIGINT path runs _cleanup then re-raises, which exits the
        # interpreter and fires the atexit registration a second time -- the
        # doubled eject was two password prompts in a row on interrupt.
        if ran[0]:
            return
        ran[0] = True
        for fn in list(_EXIT_CLEANUPS):
            try:
                fn()
            except Exception:
                pass
        # Re-online every CPU BEFORE ejecting the scheduler. Disabling sched_ext
        # while a CPU is offline deadlocks cpu_hotplug_lock on 7.1.1+ (scx-disable
        # and CPU hotplug serialize on the same lock) -- a silent box-wide freeze.
        # A scheduler must only ever be unregistered with every CPU online, the way
        # the pre-PRISM suite left it.
        try:
            restore_all_cpus(get_possible_cpus())
        except Exception:
            pass
        if eject:
            try:
                eject_scheduler(trace_mode, interrupted)
            except Exception:
                pass

    def _on_signal(signum, _frame) -> None:
        _cleanup(interrupted=True)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


# MONTAUK TRACE CAPTURE
#
# THE SINGLE PLACE MONTAUK IS DRIVEN FROM. EVERY prism-* --trace PATH GOES
# THROUGH MontaukTrace INSTEAD OF REIMPLEMENTING THE LAUNCH/ATTACH/STOP/CHOWN.
# MONTAUK IS THE ONLY TRACER -- NO ftrace/trace_pipe ANYWHERE IN THE SUITE.

MONTAUK = "/usr/local/bin/montauk"

# THE ONE RESOLUTION OF THE ANALYZER AND THE DECODER.
#
# montauk v8.10.0 folded both into montauk itself as modes: montauk_analyze and
# montauk_trace_decode are GONE, not renamed and not symlinked. Every tool flag
# after the mode word is unchanged; only the invocation moved.
#
# The resolved value is an ARGV PREFIX, never a path, because a path string
# cannot carry a mode word. Call sites splat it: [*montauk_analyze_argv(), ...].
# This is deliberately the only place either mode is spelled -- the retirement
# cost 44 lines across 11 files precisely because the analyzer's name was
# resolved independently in six of them, and a name tracked by hand in more than
# one place drifts.
def montauk_analyze_argv(binary: str | None = None) -> list[str]:
    return [binary or MONTAUK, "--analyze"]


def montauk_decode_argv(binary: str | None = None) -> list[str]:
    return [binary or MONTAUK, "--decode"]


MONTAUK_ATTACH_TIMEOUT = 10.0
MONTAUK_LOG_INTERVAL_MS = 100
# BPF RING SIZE FOR EVERY TRACED CAPTURE. montauk's default is 1M and its own --help names
# sched-messaging as the workload that default was never sized against: ~2.8M events/s
# offered against ~254k/s drained. Measured here 2026-09-01 on one --dev fork-thread run,
# EEVDF captured at 37.2% completeness against the BPF arm's 81.4% -- so the locality table
# compared a 37% sample to an 81% one. The bias is systematic, not random: The faster arm
# finishes sooner, offers a higher event rate, overruns the ring harder and drops more, so
# whichever arm wins on throughput is the arm whose locality is least sampled. This lives
# on the shared launcher because every bench inherits the same offered rate.
MONTAUK_TRACE_RING = "256M"
# QUIESCENT TAIL: Seconds montauk keeps recording AFTER the workload stops.
#
# A capture that ends with its workload cannot say what a still-open strand
# means. montauk reports a wakee that never got the CPU as CENSORED with its
# duration truncated at trace end, and a benchmark that finishes leaves work
# pending as a matter of course -- so "14 of 20 CPUs dark at trace end" is both
# the signature of a real stall and what an idling machine looks like. The
# 2026-08-05 field report is the case in point: Resolved DARK was 0% in every
# stage on that box and EVERY dark strand was censored, worst 87.2ms to 352.8ms,
# with no way to tell which reading applied.
#
# Recording into a period where nothing is running removes the ambiguity. A
# strand either RESOLVES, which prices it, or survives against a demonstrably
# idle system, which is the finding. Either beats a truncation. Set 0 to
# disable; the cost is this many seconds per traced stage.
MONTAUK_QUIESCE_S = 3.0


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
        # A SUBDIRECTORY, NOT THE ARCHIVE ROOT. Every run wrote one of these
        # beside the logs an operator reads, and most carry nothing but the
        # sched_ext enable/disable pair. Keeping the content but moving it out
        # of the listing is the whole fix; a filtered snapshot is also NOT a
        # substitute for the journal, which is where a watchdog ejection this
        # capture never saw was found.
        dmesg_dir = LOG_DIR / "dmesg"
        dmesg_dir.mkdir(parents=True, exist_ok=True)
        dmesg_path = dmesg_dir / f"{stamp}.log"
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
    directory, ready for `montauk --analyze`.

    The caller owns the scheduler and the workload; this owns only montauk.
    """

    def __init__(self, pattern, label, stamp, log_dir=TRACE_DIR,
                 interval_ms=MONTAUK_LOG_INTERVAL_MS, baseline_s=0.0,
                 attach_timeout=MONTAUK_ATTACH_TIMEOUT, events=False,
                 pin_cpu=None, sched_detail=False, scx_dsq=False,
                 trace_classes=None, quiesce_s=MONTAUK_QUIESCE_S):
        # quiesce_s: the mirror of baseline_s. baseline_s records quiet BEFORE
        # the workload; this records quiet AFTER it, so a strand still open when
        # the workload stopped is observed against an idle system rather than
        # truncated at trace end. See MONTAUK_QUIESCE_S.
        self.quiesce_s = quiesce_s
        # sched_detail: stream CPU_IDLE alongside the decision events, so
        # dispatch-stall can split a PREEMPT-STARVED floored wake into DARK
        # (CPU sat idle, no rescue) vs HELD (CPU busy the whole time) instead
        # of reporting "cannot separate, recapture with a montauk that
        # streams CPU_IDLE." Off by default -- extra event volume, opt in
        # where that split is the actual question.
        self.sched_detail = sched_detail
        # trace_classes: capture ONLY the event classes this bench reads. The cost
        # of tracing is paid per event by the CPU that generates it, so a class
        # nobody reads is a tax on the measurement -- and it is not a flat tax,
        # because the arm that does more work emits more events and pays more of
        # it. Measured 2026-09-01 on fork-thread, which reads sched exclusively:
        # 38-49% of every capture was IO events, and the tax they levied pushed
        # the PANDEMONIUM arms past the burst window while EEVDF still finished
        # inside it. montauk never reserves an excluded class, so this removes the
        # cost rather than moving it. Declare what the bench READS; a report that
        # goes quiet after a change here is a class that was load-bearing.
        self.trace_classes = trace_classes
        # scx_dsq: arm montauk's placement/drain attribution probes for THIS
        # capture (MONTAUK_SCX_DSQ). Same hazard class as the storm probes and
        # scrubbed by the same rule below, so a bench that wants the
        # placement-versus-drain split asks for it here rather than by exporting
        # a variable that every other capture would inherit.
        self.scx_dsq = scx_dsq
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
        # Decode with `montauk --decode`.
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
               "--log-interval-ms", str(self.interval_ms),
               "--trace-ring-bytes", MONTAUK_TRACE_RING]
        if self.events_path is not None:
            cmd += ["--trace-out", str(self.events_path)]
        if self.sched_detail:
            cmd += ["--sched-detail"]
        if self.trace_classes:
            cmd += ["--trace-classes", self.trace_classes]
        if self.pin_cpu is not None:
            cmd = ["taskset", "-c", str(self.pin_cpu)] + cmd
        # SCRUB THE SCX PROBES FROM EVERY CAPTURE THAT DID NOT ASK FOR THEM.
        # Both sets arm fentry trampolines on sched_ext kfuncs: MONTAUK_SCX_STORM
        # on scx_bpf_kick_cpu / scx_bpf_reenqueue_local, MONTAUK_SCX_DSQ on
        # scx_bpf_dsq_insert{,_vtime} / scx_bpf_dsq_move_to_local. Each has
        # hard-locked this box under a live scx load -- the storm set on
        # 2026-07-14, the dsq set on 2026-08-25 -- and an ambient export in the
        # caller's shell is the documented route both times, because every
        # capture here inherits the environment. So the guard belongs on the
        # shared launcher, and a bench that wants either set opts in explicitly.
        # The list is the enforcement: a third probe set added to montauk and not
        # added here inherits silently, which is how this rule came to be needed.
        scx_probe_env = ("MONTAUK_SCX_STORM", "MONTAUK_SCX_DSQ")
        env = {k: v for k, v in os.environ.items() if k not in scx_probe_env}
        if self.scx_dsq:
            env["MONTAUK_SCX_DSQ"] = "1"
        self.proc = subprocess.Popen(cmd, stdout=self._out,
                                     stderr=subprocess.STDOUT, env=env)
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

    def stop(self, quiesce: bool = True) -> None:
        # The tail runs while montauk is STILL RECORDING, which is the whole
        # point -- signal it first and the quiet period is not in the capture.
        # Skipped when the run is unwinding from an exception: A failed workload
        # has nothing to prove about its strands, and three seconds of teardown
        # on every error path is a poor trade.
        if self.proc is not None and quiesce and self.quiesce_s > 0:
            log_info(f"quiescent tail: recording {self.quiesce_s:.0f}s with the "
                     f"workload stopped, so an open strand is measured against "
                     f"an idle system rather than truncated at trace end")
            time.sleep(self.quiesce_s)
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
        self.stop(quiesce=exc_type is None)
        return False


def montauk_trace(pattern, label, stamp, log_dir=TRACE_DIR, **kw) -> "MontaukTrace":
    """Factory for MontaukTrace -- use as `with montauk_trace(...) as rec:`."""
    return MontaukTrace(pattern, label, stamp, log_dir, **kw)




# IPC latency engine relocated to ipc_workload.py (a workload kernel, not
# shared scaffolding) -- imported by pandemonium-tests' prism-scale measure_ipc.


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
    Shared by prism-coldwake and prism so every report carries it and two
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

    # Score the dark-window width. Each factor that widens it adds weight.
    #
    # RUNTIME STATE, NOT BUILD CONFIG. CONFIG_NO_HZ_FULL=y only means the kernel
    # CAN run a busy CPU tickless; without a nohz_full= mask on the cmdline no
    # CPU ever does, and the kernel behaves as plain idle-dynticks. Scoring the
    # symbol credited a capability nothing had enabled, and on the 2026-08-05
    # field report it printed "NO_HZ_FULL (fully tickless)" as a contributing
    # factor two lines below `cmdline nohz_full=-`. Nearly every stock distro
    # kernel ships the symbol, so this inflated almost every box we will ever
    # see a report from.
    #
    # The idle-dynticks case still counts, and counts for the reason that
    # matters here: An IDLE CPU stops its tick under NO_HZ_IDLE alone, which is
    # the DARK entry condition. nohz_full widens the window to busy CPUs; it is
    # not what opens it.
    score, why = 0, []
    try:
        hz_n = int(hz)
        if hz_n <= 100: score += 3; why.append(f"HZ={hz} (wide ticks)")
        elif hz_n <= 300: score += 2; why.append(f"HZ={hz}")
        elif hz_n >= 1000: why.append(f"HZ={hz} (tight ticks -- narrows window)")
    except ValueError:
        pass
    if nohz == "NO_HZ_FULL" and nohz_full:
        score += 3; why.append("NO_HZ_FULL active (busy CPUs run tickless)")
    elif nohz in ("NO_HZ_FULL", "NO_HZ_IDLE"):
        score += 1
        why.append("idle CPUs go tickless (NO_HZ_IDLE behavior" +
                   ("; NO_HZ_FULL built but no nohz_full= mask, so unused)"
                    if nohz == "NO_HZ_FULL" else ")"))
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


# SCHEDULER LIFECYCLE -- THE SHARED HALF OF pandemonium-tests.
#
# These moved out of tests/pandemonium-tests.py, which was a 6,500-line ENTRY
# POINT that four other benches also imported as a LIBRARY -- for exactly seven
# functions. Reaching them meant a sys.path insert and
# `import_module("pandemonium-tests")` in every caller, because a hyphenated
# script name is not importable, and that dance existed solely to cross this
# 240-line boundary. The file keeps its twelve subcommands; it is no longer
# something other benches import.

def find_scheduler(name: str) -> str | None:
    return shutil.which(name)


# Active scheduler guards, force-ejected on interrupt or normal exit. sched_ext
# schedulers run in their OWN process group (start_scheduler: preexec_fn=os.setpgrp),
# so a Ctrl+C to prism's foreground group never reaches them, and __del__-based cleanup
# does NOT reliably run on KeyboardInterrupt or interpreter exit -- without this an
# interrupted run leaves the scheduler REGISTERED and the system stays on it. stop()
# escalates SIGINT -> SIGKILL, and a SIGKILL'd loader makes the kernel auto-unregister,
# so this ejects even a hung scheduler.
_ACTIVE_GUARDS: set = set()


def stop_systemd_scheduler() -> None:
    """Stop the systemd `pandemonium` service -- the canonical clear of a stale sched_ext
    registration. Sudo-aware: prefixes sudo only when not already root. Use this instead of
    open-coding `systemctl stop pandemonium` (it was duplicated a dozen ways across the
    suite, half with the wrong sudo handling)."""
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    subprocess.run(prefix + ["systemctl", "stop", "pandemonium"], capture_output=True)


class SchedulerProcess:
    """RAII-style guard for a running sched_ext scheduler."""

    def __init__(self, proc: subprocess.Popen, name: str,
                 stdout_path: str | None = None,
                 stderr_path: str | None = None):
        self.proc = proc
        self.name = name
        self.pgid = os.getpgid(proc.pid)
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        _ACTIVE_GUARDS.add(self)

    def stop(self):
        _ACTIVE_GUARDS.discard(self)
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.pgid, signal.SIGINT)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def drain_stdout(self) -> str:
        """Read all stdout captured to file (call after stop)."""
        if self.stdout_path:
            try:
                return Path(self.stdout_path).read_text()
            except (FileNotFoundError, PermissionError):
                pass
        return ""

    def read_stderr(self, limit: int = 4000) -> str:
        if self.stderr_path:
            try:
                return Path(self.stderr_path).read_text()[:limit]
            except (FileNotFoundError, PermissionError):
                pass
        return ""

    def cleanup(self):
        for p in [self.stdout_path, self.stderr_path]:
            if p:
                try:
                    os.unlink(p)
                except (FileNotFoundError, PermissionError):
                    pass

    def __del__(self):
        self.stop()
        self.cleanup()


def start_scheduler(cmd: list[str], name: str) -> SchedulerProcess | None:
    """Spawn a scheduler subprocess in its own process group.
    Stdout and stderr go to files to avoid pipe buffer overflow.
    Returns None if the binary cannot be found."""
    bin_path = cmd[0] if cmd else ""
    if bin_path and not os.path.exists(bin_path) and not shutil.which(bin_path):
        log_error(f"Binary not found: {bin_path}")
        return None
    # Refresh sudo credentials before spawning (prism-scale runs are long)
    subprocess.run(["sudo", "-v"], capture_output=True)
    full_cmd = ["sudo"] + cmd
    log_info(f"Starting: {' '.join(full_cmd)}")
    # From here the exit guard owns whatever lands on the box. Before this point
    # an active scheduler is the user's and teardown leaves it alone.
    note_scheduler_activated()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = str(LOG_DIR / f"sched-{name}-{os.getpid()}.stdout")
    stdout_f = open(stdout_path, "w")
    stderr_path = str(LOG_DIR / f"sched-{name}-{os.getpid()}.stderr")
    stderr_f = open(stderr_path, "w")
    proc = subprocess.Popen(
        full_cmd,
        stdout=stdout_f,
        stderr=stderr_f,
        preexec_fn=os.setpgrp,
    )
    stdout_f.close()
    stderr_f.close()
    return SchedulerProcess(proc, name, stdout_path, stderr_path)


def start_and_wait(cmd: list[str], name: str,
                   settle_secs: float = 2.0) -> SchedulerProcess | None:
    """Start a scheduler, wait for sched_ext activation. Returns None on failure."""
    # Detect stale struct_ops registration before starting
    try:
        stale = SCX_OPS.read_text().strip()
        if stale:
            log_warn(f"Stale scheduler detected: '{stale}', waiting for cleanup...")
            if not wait_for_no_scheduler(timeout=15):
                log_error("stale scheduler did not unregister")
                return None
            log_info("Stale scheduler cleared")
    except (FileNotFoundError, PermissionError):
        pass
    guard = start_scheduler(cmd, name)
    if guard is None:
        return None
    if not wait_for_activation(10.0):
        log_warn(f"{name} did not activate within 10s -- skipping")
        exited = guard.proc.poll() is not None
        if exited:
            log_error(f"{name} process exited early (code {guard.proc.returncode})")
        else:
            log_warn(f"{name} process still running but sched_ext not active")
        stderr = guard.read_stderr()
        if stderr.strip():
            for line in stderr.strip().splitlines()[:30]:
                log_error(f"  {line}")
        guard.stop()
        wait_for_deactivation(5.0)
        return None
    log_info(f"{name} is active")
    time.sleep(settle_secs)
    return guard


def sched_ejected(guard) -> bool:
    """True when the scheduler is no longer the active scx scheduler: the process died OR the
    BPF was ejected (watchdog) and the kernel fell back to EEVDF while the process lingers.
    The proc-only `guard.proc.poll()` check missed the ejection -- that was the lie. Use this
    for every 'did the scheduler survive this phase' check."""
    if guard is None:
        return False
    return guard.proc.poll() is not None or not scheduler_active()


def stop_and_wait(guard: SchedulerProcess | None) -> str:
    """Stop a scheduler, wait for deactivation. Returns captured stdout."""
    if guard is None:
        return ""
    guard.stop()
    stdout = guard.drain_stdout()
    if not wait_for_deactivation(5.0):
        log_warn(f"sched_ext still active after stopping {guard.name}")
    measure_struct_ops_cleanup()
    time.sleep(1)
    return stdout


# SCHEDULER STATE AND THE SHUTDOWN-LINE MARKER WRITERS.
# The trace driver below calls these, so they live beside it: a library that
# reaches back into its own caller for a helper is not a library.

def scheduler_active(expected: str = "pandemonium") -> bool:
    """sched_ext is registered AND it is `expected` -- the REAL kernel state, not a live
    userspace process. A watchdog ejection drops the BPF while the pandemonium process
    lingers, so proc.poll() reads alive; this reads the scx registration directly, so the
    harness can never report PANDEMONIUM while the kernel has actually fallen back to EEVDF."""
    try:
        return is_scx_active() and scx_scheduler_name() == expected
    except Exception:
        return False


def measure_struct_ops_cleanup():
    """Time how long the kernel takes to fully unregister struct_ops."""
    try:
        name = SCX_OPS.read_text().strip()
        if not name:
            return
    except (FileNotFoundError, PermissionError):
        return
    t0 = time.monotonic()
    while True:
        try:
            name = SCX_OPS.read_text().strip()
            if not name:
                elapsed_ms = (time.monotonic() - t0) * 1000
                log_info(f"  struct_ops cleanup: {elapsed_ms:.0f}ms")
                return
        except (FileNotFoundError, PermissionError):
            elapsed_ms = (time.monotonic() - t0) * 1000
            log_info(f"  struct_ops cleanup: {elapsed_ms:.0f}ms (ops disappeared)")
            return
        if time.monotonic() - t0 > 30:
            log_error("struct_ops cleanup: STILL REGISTERED AFTER 30s")
            return
        time.sleep(0.01)


def parse_migration_line(stdout_text: str) -> dict:
    """Parse the [MIGRATION] per-cause line -- ALL cross-CPU moves by XDOM_* path, not
    just the cross-domain subset the [KNOBS] line carries. Names which decision drives a
    within-L3 bounce (steal vs sel vs enq). {} when no [MIGRATION] line (EEVDF / external)."""
    for line in stdout_text.splitlines():
        if "[MIGRATION]" not in line:
            continue
        out = {}
        for m in re.finditer(r"(\w+)=(\d+)", line.split("[MIGRATION]")[1]):
            out[m.group(1)] = int(m.group(2))
        return out
    return {}


def _write_cross_domain_marker(guard, rec_dir):
    """Producer marker (mirrors prism's write_stability_markers): after a
    traced PANDEMONIUM run stops, parse its shutdown [KNOBS] line for the per-path
    cross-CCX attribution and write it into the recording dir. montauk --analyze
    --digest surfaces it as a CROSS-CCX PLACEMENT block, so a multi-CCX user can
    tell SEL_DFL (topology-blind fallback) from STEAL/STEP5 (dispatch-side) in one
    read instead of only seeing montauk's trace-derived scatter percentage. No-op
    for EEVDF / external schedulers (no [KNOBS] line)."""
    if guard is None or rec_dir is None:
        return
    try:
        output = guard.read_output()
    except Exception:
        return
    _write_cross_domain_marker_text(output, rec_dir)


def _write_cross_domain_marker_text(output, rec_dir):
    """Write the cross-domain + migration markers from already-captured scheduler
    stdout. The IPC path drains the guard via stop_and_wait, so it cannot re-read the
    guard and passes the drained text here instead."""
    if rec_dir is None or not output:
        return
    knobs = parse_knobs_line(output)
    mig = parse_migration_line(output)
    have_xdom = any(f"cross_domain_{p}" in knobs for p in _XDOM_PATHS)
    have_mig = any(p in mig for p in _XDOM_PATHS)
    if not have_xdom and not have_mig:
        return
    lines = []
    if "cross_domain_scatter_pct" in knobs:
        lines.append(f"montauk_cross_domain_scatter_pct {knobs['cross_domain_scatter_pct']}")
    for p in _XDOM_PATHS:
        k = f"cross_domain_{p}"
        if k in knobs:
            lines.append(f'montauk_cross_domain_path{{path="{p}"}} {knobs[k]}')
    # ALL-MOVES per-path attribution -- the within-L3 core-to-core bounce the
    # cross-domain subset above cannot see. montauk_migration_path names the dominant
    # cause of a migration storm (steal vs sel vs enq) instead of only its cross-CCX share.
    for p in _XDOM_PATHS:
        if p in mig:
            lines.append(f'montauk_migration_path{{path="{p}"}} {mig[p]}')
    try:
        (Path(rec_dir) / "cross_domain.prom").write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# THE ADAPTIVE LAYER'S PER-TICK CHAOS LINE, WHICH NOTHING RECORDED.
# `chaos: lam=2.13 H=0.42 det=1.00 x=7 frozen: 1 (n=42)` is printed once per
# second by the --verbose adaptive loop and was, until now, discarded with the
# rest of scheduler stdout. A full day of 2026-07-29 benchmarking produced zero
# samples of it, which is why every question about the quiescence gate -- does it
# ever fire, does DET behave as assumed, does saturation read as quiescent --
# was unanswerable from the archive rather than merely unanswered.
_CHAOS_LINE_RE = re.compile(
    r"chaos:\s*lam=(?P<lam>[-\d.]+)\s+H=(?P<h>[-\d.]+)\s+det=(?P<det>[-\d.]+)"
    r"\s+x=(?P<x>\d+)\s+frozen:\s*(?P<frozen>\d+)\s*\(n=(?P<n>\d+)\)"
)

# THE LIVE LOAD GRAPH'S SUMMARY, ON THE SAME TELEMETRY LINE.
# `graph: n=12 e=66 cpl=0.32/0.05/0.88` -- CPUs, edges that computed, then mean/
# min/max Pecora-Carroll coupling. The SPREAD is the measurement that decides
# whether the graph is worth pricing against: if min and max sit together the
# matrix is flat, the graph is complete-and-uniform, and it carries nothing the
# static topology does not already have.
_GRAPH_LINE_RE = re.compile(
    r"graph:\s*n=(?P<gn>\d+)\s+e=(?P<ge>\d+)\s+"
    r"cpl=(?P<cmean>[-\d.]+)/(?P<cmin>[-\d.]+)/(?P<cmax>[-\d.]+)"
)


def _write_chaos_markers_text(output, rec_dir):
    """Fold the adaptive layer's per-tick chaos samples into the recording.

    Emitted as a .prom beside cross_domain.prom, so the quiescence gate is
    answered from the same archive as everything else rather than by watching a
    terminal. The FROZEN FRACTION is the headline: An AND-gate whose HVG term
    never latches never freezes at all, and a zero here says so immediately.

    Records the distribution, not just the last value -- the gate's behavior is a
    time series and the final tick is the least interesting sample in it."""
    if rec_dir is None or not output:
        return
    lam, det, frozen = [], [], []
    for m in _CHAOS_LINE_RE.finditer(output):
        try:
            lam.append(float(m.group("lam")))
            det.append(float(m.group("det")))
            frozen.append(int(m.group("frozen")))
        except ValueError:
            continue
    # Graph samples come off the same line; a run predating the graph simply
    # yields none rather than zeros.
    g_edges, g_mean, g_spread = [], [], []
    for m in _GRAPH_LINE_RE.finditer(output):
        try:
            e = int(m.group("ge"))
            if e == 0:
                continue
            g_edges.append(e)
            g_mean.append(float(m.group("cmean")))
            g_spread.append(float(m.group("cmax")) - float(m.group("cmin")))
        except ValueError:
            continue

    if not lam:
        return
    n = len(lam)

    def _q(vals, q):
        s = sorted(vals)
        return s[min(len(s) - 1, int(q * len(s)))]

    lines = [
        f"pandemonium_chaos_samples {n}",
        f"pandemonium_chaos_frozen_ticks {sum(frozen)}",
        # THE ONE NUMBER CLUSTER A EXISTS TO GET. 0 MEANS THE GATE NEVER
        # FIRED ACROSS THE WHOLE RUN.
        f"pandemonium_chaos_frozen_fraction {sum(frozen) / n:.4f}",
        f"pandemonium_chaos_lambda_p50 {_q(lam, 0.50):.4f}",
        f"pandemonium_chaos_lambda_p99 {_q(lam, 0.99):.4f}",
        f"pandemonium_chaos_det_p50 {_q(det, 0.50):.4f}",
        f"pandemonium_chaos_det_p99 {_q(det, 0.99):.4f}",
        # THE TWO GATE TERMS, COUNTED SEPARATELY. AN AND-GATE THAT NEVER
        # FIRES IS DIAGNOSED BY WHICH TERM WITHHELD, NOT BY THE VERDICT.
        f"pandemonium_chaos_lambda_in_band "
        f"{sum(1 for v in lam if v <= 2.6) / n:.4f}",
        f"pandemonium_chaos_det_in_band "
        f"{sum(1 for v in det if v >= 0.90) / n:.4f}",
    ]
    if g_mean:
        gn = len(g_mean)
        lines += [
            f"pandemonium_graph_samples {gn}",
            f"pandemonium_graph_edges_p50 {_q(g_edges, 0.50)}",
            f"pandemonium_graph_coupling_mean_p50 {_q(g_mean, 0.50):.4f}",
            # THE FALSIFICATION. A spread at or near zero says every CPU pair
            # couples identically, so the live graph reduces to the static one.
            f"pandemonium_graph_coupling_spread_p50 {_q(g_spread, 0.50):.4f}",
            f"pandemonium_graph_coupling_spread_max {max(g_spread):.4f}",
        ]

    try:
        dest = Path(rec_dir)
        # A montauk recording dir gets chaos.prom beside its other artifacts; a
        # bare path ending in .prom is written directly. The second form exists
        # because the fallback used to mkdir a whole directory to hold one
        # 318-byte file, which is how 433 of them accumulated in the archive
        # root and buried every log the operator actually reads.
        if dest.suffix == ".prom":
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest = dest / "chaos.prom"
        dest.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def _write_chaos_markers(guard, rec_dir):
    if guard is None or rec_dir is None:
        return
    try:
        output = guard.read_output()
    except Exception:
        return
    _write_chaos_markers_text(output, rec_dir)


def parse_knobs_line(stdout_text: str) -> dict:
    """Parse [KNOBS] summary line from scheduler stdout."""
    for line in stdout_text.splitlines():
        if "[KNOBS]" not in line:
            continue

        knobs = {}
        for m in re.finditer(r"(\w+)=(\S+)", line.split("[KNOBS]")[1]):
            k, v = m.group(1), m.group(2)
            if v == "true":
                knobs[k] = True
            elif v == "false":
                knobs[k] = False
            else:
                try:
                    knobs[k] = int(v)
                except ValueError:
                    knobs[k] = v

        # Expand ticks=L:5/M:12/H:3
        if "ticks" in knobs and isinstance(knobs["ticks"], str):
            ticks_str = knobs.pop("ticks")
            for part in ticks_str.split("/"):
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    label = {"L": "ticks_light", "M": "ticks_mixed",
                             "H": "ticks_heavy"}.get(prefix)
                    if label:
                        try:
                            knobs[label] = int(val)
                        except ValueError:
                            pass

        # Expand l2_hit=B:75%/I:60%/L:80%
        if "l2_hit" in knobs and isinstance(knobs["l2_hit"], str):
            l2_str = knobs.pop("l2_hit")
            for part in l2_str.split("/"):
                if ":" in part:
                    prefix, val = part.split(":", 1)
                    label = {"B": "l2_hit_batch", "I": "l2_hit_interactive",
                             "L": "l2_hit_latcrit"}.get(prefix)
                    if label:
                        try:
                            knobs[label] = int(val.rstrip("%"))
                        except ValueError:
                            pass

        return knobs

    return {}


# GENERIC MONTAUK TRACE DRIVER -- the single body behind every `prism-* --trace`.
# Each bench supplies its comm `pattern`, a `label`, and a `body_fn(rec_dir)` that
# runs its workload; this owns the activate -> montauk --trace -> deactivate
# lifecycle so no bench re-implements it.
_XDOM_PATHS = ["sel_tight", "sel_sync", "sel_normal", "sel_dfl",
               "enq_t1", "enq_t2", "steal", "step5"]


def trace_workload(sched_name, activate_cmd, pattern, label, stamp, body_fn, *,
                   baseline_s=0.0, events=False, pin_cpu=None,
                   trace_activation=False, sched_detail=False,
                   trace_classes=None):
    """Record `montauk --trace pattern` around a scheduler workload.

    Default: activate `sched_name` via start_and_wait(activate_cmd), record while
    body_fn(rec_dir) runs, then stop_and_wait. Returns (rec_dir, body_result), or
    (None, None) if activation failed (nothing to trace).

    trace_activation=True: montauk records FIRST (pattern should match the
    SCHEDULER comm), then activation is attempted inside the window -- so a FAILED
    activation lands in the recording instead of vanishing. body_fn runs only if
    activation took. Returns (rec_dir, body_result) on success, (rec_dir, None) on
    activation failure (the failure IS the capture).

    The caller owns the workload; this owns montauk and the scheduler lifecycle.
    """
    safe = sched_name.replace(" ", "-").replace("(", "").replace(")", "")
    rlabel = f"{label}-{safe}"

    if trace_activation:
        with montauk_trace(pattern, rlabel, stamp, baseline_s=baseline_s,
                           events=events, pin_cpu=pin_cpu,
                           sched_detail=sched_detail,
                           trace_classes=trace_classes) as rec:
            guard = start_and_wait(activate_cmd, sched_name) if activate_cmd else None
            if activate_cmd is not None and guard is None:
                log_error(f"[{sched_name}] failed to activate "
                          f"-- captured in {rec.dir}")
                return rec.dir, None
            try:
                return rec.dir, body_fn(rec.dir)
            finally:
                if guard is not None:
                    stop_and_wait(guard)
                    _write_cross_domain_marker(guard, rec.dir)
                    _write_chaos_markers(guard, rec.dir)

    guard = None
    if activate_cmd is not None:
        log_info(f"[{sched_name}] activating scheduler...")
        guard = start_and_wait(activate_cmd, sched_name)
        if guard is None:
            log_error(f"[{sched_name}] failed to activate, skipping")
            return None, None
    rec_dir = None
    try:
        with montauk_trace(pattern, rlabel, stamp, baseline_s=baseline_s,
                           events=events, pin_cpu=pin_cpu,
                           sched_detail=sched_detail,
                           trace_classes=trace_classes) as rec:
            rec_dir = rec.dir
            return rec.dir, body_fn(rec.dir)
    finally:
        if guard is not None:
            stop_and_wait(guard)
            _write_cross_domain_marker(guard, rec_dir)
            _write_chaos_markers(guard, rec_dir)


# MEASUREMENT

# PROMETHEUS HISTOGRAM BUCKETS (us). 1-2-5 ladder per decade, 1us..1s,
# shared across every us-domain latency distribution so per-cell CDFs can be
# reconstructed from the .prom alone.
HIST_BUCKETS_US = [
    1, 2, 5, 10, 20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 20_000, 50_000,
    100_000, 200_000, 500_000, 1_000_000,
]
