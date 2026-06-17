#!/usr/bin/env python3
"""bench-enduser: turn a scheduler problem into one shareable file.

For when the system stutters or lags under load. It runs a short fixed profile
(cachyos + fork-thread + ipc) under your scheduler and EEVDF as a neutral
reference, traces each with montauk, and assembles the recordings into one
KB-scale report: your system specs, the misbehaving items ranked BY NAME (hot
CPUs, livelocking tasks, unsignaled waiters, idle strands), then the key
latency metrics. The maintainer ingests the same ranked list you see, so a bug
report carries its own evidence. montauk is the only data source -- this script
orchestrates and assembles, nothing more. Process names are hashed and the raw
traces stay local; you share only the small report file.

Already isolated the problem to one program? Skip the fixed profile and capture
THAT instead: `--workload "<command>"` runs your command under the loaded
scheduler and reports on it, or `--attach <comm> --duration <s>` traces an
already-running program. Same report, captured on your real workload.
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pandemonium_common import (  # noqa: E402
    log, get_version, get_git_info, log_info, log_warn, log_error,
    MONTAUK, montauk_available, DmesgMonitor, montauk_trace, install_hint,
    TRACE_DIR, LOG_DIR, get_online_cpus,
    is_scx_active, scx_scheduler_name, wait_for_deactivation,
    stall_susceptibility,
)

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The report needs both the tracer (montauk) and the analyzer (montauk_analyze).
MONTAUK_INSTALLED = Path(MONTAUK)
MONTAUK_ANALYZE_INSTALLED = MONTAUK_INSTALLED.with_name("montauk_analyze")

# montauk is not bundled. When it is missing, bench-enduser clones it from the
# canonical repo and drives montauk's OWN installer -- it is a wrapper, not a
# packager. The clone is a fresh shallow checkout each run.
MONTAUK_REPO = "https://github.com/wllclngn/montauk.git"
MONTAUK_CLONE = Path("/tmp/montauk")

# Per-distro install commands for the prerequisites the montauk clone+build
# needs. Keyed by the distros the project supports; install_hint() leads with
# the user's own. git fetches the repo; the toolchain bundle builds it (cmake +
# a C++23 compiler + clang/LLVM for the BPF target + libbpf + bpftool + make).
GIT_HINTS = {
    "cachyos": "sudo pacman -S git",
    "arch": "sudo pacman -S git",
    "gentoo": "sudo emerge dev-vcs/git",
    "opensuse": "sudo zypper install git",
    "ubuntu": "sudo apt install git",
    "nixos": "nix-shell -p git   (or add git to configuration.nix)",
}
MONTAUK_BUILD_HINTS = {
    "cachyos": "sudo pacman -S --needed base-devel cmake clang llvm libbpf bpf",
    "arch": "sudo pacman -S --needed base-devel cmake clang llvm libbpf bpf",
    "gentoo": "sudo emerge dev-build/cmake sys-devel/clang sys-devel/llvm dev-libs/libbpf dev-util/bpftool",
    "opensuse": "sudo zypper install -t pattern devel_basis; sudo zypper install cmake clang llvm libbpf-devel bpftool",
    "ubuntu": "sudo apt install build-essential cmake clang llvm libbpf-dev linux-tools-generic",
    "nixos": "nix-shell -p cmake clang llvm libbpf bpftool gnumake",
}

# Frozen profile: short, native-core, traced. Kept stable so a report compares
# against the archived bench-enduser baseline (and across users).
PROFILE_ITERATIONS = 3
# Light cachyos workloads ONLY -- the suite's ffmpeg/kernel-build workloads are
# far too heavy for an end-user report. Cache-relevant and fast.
PROFILE_CACHYOS_WORKLOADS = "stress-ng-cpu-cache-mem,xz-compression,primes"

# Display labels for the profile phases -- proper-cased names + the "TRACE" verb,
# so the section headers read uniformly (CachyOS Benchmark: TRACE, IPC: TRACE, ...).
_PROFILE_LABELS = {
    "cachyos": "CachyOS Benchmark",
    "fork-thread": "fork-thread",
    "ipc": "IPC",
    "burst-starvation": "burst-starvation",
    "sojourn-pressure": "sojourn-pressure",
    "cold-wake": "Cold-Wake",
    "storm": "storm",
}


def _analyze_bin() -> str:
    """Prefer the freshly-built montauk_analyze from the clone (it matches the
    montauk we just installed); fall back to the installed path."""
    clone_a = MONTAUK_CLONE / "build" / "montauk_analyze"
    return str(clone_a) if clone_a.is_file() else str(MONTAUK_ANALYZE_INSTALLED)


def ensure_trace_dir() -> None:
    """montauk writes recordings under TRACE_DIR; a prior sudo run can leave it
    root-owned, which then breaks a user-side mkdir. Create it (user-owned) if
    absent, or chown it back if a stale run left it root-owned."""
    if not TRACE_DIR.exists():
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        return
    if not os.access(TRACE_DIR, os.W_OK):
        user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
        if user:
            log_warn(f"{TRACE_DIR} not writable (stale root-owned) -- chowning")
            subprocess.run(["sudo", "chown", "-R", f"{user}:", str(TRACE_DIR)])


def _prompt_yn(question: str) -> bool:
    """A Y/N prompt rendered in the suite's [HH:MM:SS] [INFO] format so consent
    lines sit flush with the surrounding log output."""
    ts = datetime.now().strftime("[%H:%M:%S]")
    try:
        return input(f"{ts} [INFO]   {question} [Y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def welcome(trace_mode: bool = False) -> None:
    log_info("bench-enduser: turn a scheduler problem into one shareable file")
    if trace_mode:
        log_info("  you have isolated the problem to one program -- this traces")
        log_info("  THAT program under the scheduler you are running now and")
        log_info("  names what misbehaves, captured on your real workload.")
    else:
        log_info("  for when the system stutters or lags under load: it runs a")
        log_info("  short profile under your scheduler and EEVDF (a neutral")
        log_info("  reference) and names what misbehaves under each.")
    log_info("  montauk measures it; the report ranks the offenders by name --")
    log_info("  hot CPUs, livelocking tasks, waiters left unsignaled -- over")
    log_info("  your specs and the key latency metrics, in ONE small file.")
    log_info("  the maintainer reads the same ranked list you do, so nothing")
    log_info("  is lost in translation. names are hashed, nothing is uploaded,")
    log_info("  raw traces stay local. share the one file it prints at the end.")
    if not trace_mode:
        log_info("  this may take five-plus minutes depending on your system "
                 "architecture.")
    print()


def _as_user(cmd: list[str]) -> list[str]:
    """Drop a command back to the invoking user. We run as root (the main()
    self-elevate), but montauk's clone/build/install.py must NOT run as root --
    install.py refuses (it poisons the build dir with root-owned files and
    manages its own sudo for the privileged steps). SUDO_USER is set by the
    re-exec; sudo->same-user needs no password from root."""
    su = os.environ.get("SUDO_USER")
    if su and su != "root" and os.geteuid() == 0:
        return ["sudo", "-u", su, "-H", *cmd]
    return cmd


def _check_build_prereqs() -> bool:
    """Before cloning + building montauk, confirm the tools are present. A
    missing one is not a dead end: tell the user the exact install command for
    their distro (and the others). Returns False with a hint printed if blocked."""
    if shutil.which("git") is None:
        install_hint("git", GIT_HINTS)
        return False
    missing = [t for t in ("cmake", "clang", "bpftool", "make")
               if shutil.which(t) is None]
    if missing:
        log_error(f"montauk's build needs: {', '.join(missing)}")
        install_hint("the montauk build toolchain", MONTAUK_BUILD_HINTS)
        return False
    return True


def _clone_montauk() -> bool:
    """Fresh shallow clone of the montauk repo into /tmp/montauk, owned by the
    invoking user so install.py can build there without running as root."""
    subprocess.run(["rm", "-rf", str(MONTAUK_CLONE)])
    log_info(f"cloning montauk -> {MONTAUK_CLONE}")
    r = subprocess.run(_as_user(["git", "clone", "--depth", "1", MONTAUK_REPO,
                                 str(MONTAUK_CLONE)]))
    if r.returncode != 0:
        log_error(f"git clone failed ({MONTAUK_REPO})")
        return False
    return True


def _version_tuple(s: str) -> tuple:
    """'7.5.0' -> (7, 5, 0); tolerant of junk so comparison never throws."""
    out = []
    for p in s.strip().split("."):
        digits = "".join(c for c in p if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _installed_montauk_version() -> tuple | None:
    """The installed montauk's version via `montauk_analyze --version`. None if
    it is too old to have the flag (pre-7.5.0 printed nothing) or unreadable --
    either way, a candidate for upgrade."""
    if not MONTAUK_ANALYZE_INSTALLED.is_file():
        return None
    try:
        r = subprocess.run([str(MONTAUK_ANALYZE_INSTALLED), "--version"],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if r.returncode == 0 and re.match(r"^\d+\.\d+", out):
            return _version_tuple(out)
    except Exception:
        pass
    return None


def _repo_montauk_version() -> tuple | None:
    """The latest montauk version, read from the repo's CMakeLists at runtime --
    NEVER hardcoded here, so it tracks montauk automatically. Cheap: one raw
    fetch of CMakeLists (HEAD = the repo's default branch, no branch guess), no
    clone. None when the repo is unreachable -- then we never force an upgrade,
    because a report on the installed montauk beats no report at all."""
    base = MONTAUK_REPO[:-4] if MONTAUK_REPO.endswith(".git") else MONTAUK_REPO
    url = base.replace("github.com", "raw.githubusercontent.com") + \
        "/HEAD/CMakeLists.txt"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"project\(montauk VERSION ([0-9.]+)", text)
    return _version_tuple(m.group(1)) if m else None


def ensure_montauk() -> tuple[bool, bool, bool]:
    """(available, installed_by_us, uninstall_after). Asks both consent prompts
    up front, clones the repo, and drives montauk's OWN installer -- bench-enduser
    wraps montauk's install, it does not package binaries. A KEEP install runs
    `install.py` (permanent, capped, to /usr/local); an ephemeral one builds the
    clone and copies the two binaries into place for a clean two-file removal.

    An already-installed montauk is version-gated: if it is older than the repo's
    current version (read dynamically, not hardcoded) it is upgraded -- uninstall
    old, clone + install latest -- so the report is never assembled against a
    montauk too old to surface a crash front and center."""
    have_analyze = MONTAUK_ANALYZE_INSTALLED.is_file()
    upgrade = False
    if montauk_available() and have_analyze:
        cur = _installed_montauk_version()
        latest = _repo_montauk_version()
        # Force the (destructive) upgrade ONLY when we can POSITIVELY read an
        # installed version older than the repo's. Three keep-cases:
        #   latest is None -> repo unreachable; a report on the installed montauk
        #                     beats no report.
        #   cur is None    -> installed montauk does not answer --version (a build
        #                     that predates the flag). It is present and working;
        #                     do NOT nuke it. The old code upgraded here on EVERY
        #                     run, which uninstalled a functional montauk and then
        #                     dropped into montauk's module hot-swap (rmmod of the
        #                     in-use montauk.ko -> "Device or resource busy" ->
        #                     failed reinstall, leaving NO tracer installed).
        #   cur >= latest  -> already current.
        if latest is None or cur is None or cur >= latest:
            return True, False, False
        upgrade = True
        log_warn(f"installed montauk {'.'.join(map(str, cur))} is behind the repo "
                 f"{'.'.join(map(str, latest))} -- the report's front-and-center "
                 f"crash/clean-room block needs the newer montauk.")
        if not _prompt_yn("Upgrade montauk now"):
            log_warn("keeping the older montauk; a crash may not lead the report.")
            return True, False, False
        uninstall_after = False  # upgrading a permanent install -> keep it
    else:
        log_warn("montauk is not installed; it will be cloned and built from source,")
        log_warn("required for this report, removable afterward.")
        if not _prompt_yn("Install montauk now"):
            log_error("montauk is required for the report -- aborting.")
            return False, False, False
        uninstall_after = _prompt_yn("Uninstall montauk afterward")

    if not _check_build_prereqs():
        return False, False, False
    if not _clone_montauk():
        return False, False, False
    installer = MONTAUK_CLONE / "install.py"
    if not installer.is_file():
        log_error(f"montauk installer not found at {installer}")
        return False, False, False

    if upgrade:
        # Clean uninstall -> install, using the freshly cloned installer so the
        # removal matches the new layout (binaries, trace tools, man, caps).
        log_info("removing the older montauk before installing the latest")
        subprocess.run(_as_user([sys.executable, str(installer), "uninstall"]),
                       cwd=MONTAUK_CLONE)

    if uninstall_after:
        # Ephemeral: build the clone (as the user), then place just the two
        # binaries (no caps/man/theme) so removal is a clean rm. montauk --trace
        # still works -- the report flow runs as root (the main() self-elevate).
        r = subprocess.run(_as_user([sys.executable, str(installer), "build",
                                     "--bpf", "--no-kernel"]), cwd=MONTAUK_CLONE)
        build = MONTAUK_CLONE / "build"
        ok = r.returncode == 0
        for name, dst in (("montauk", MONTAUK_INSTALLED),
                          ("montauk_analyze", MONTAUK_ANALYZE_INSTALLED)):
            src = build / name
            if not src.is_file() or subprocess.run(
                    ["install", "-m755", str(src), str(dst)]).returncode != 0:
                ok = False
    else:
        # Keep: run montauk's own installer (as the user; it sudo's its own
        # privileged steps). Builds, installs to /usr/local, applies trace caps,
        # man page, theme -- the full, permanent treatment. A KEEP install is
        # permanent, so it also enables montauk's kernel module: its headline
        # perf feature (~0.1% CPU vs 2-5%, zero /proc reads, sub-ms detection).
        # That needs the running kernel's headers -- without them
        # `install.py --kernel` aborts the whole install, so probe first and
        # fall back to --no-kernel with a heads-up instead of failing the report.
        kver = os.uname().release
        if Path(f"/lib/modules/{kver}/build").exists():
            kflag = "--kernel"
        else:
            kflag = "--no-kernel"
            log_warn(f"kernel headers for {kver} not found -- installing montauk "
                     f"without its kernel module. To add it later, install "
                     f"linux-headers and run montauk's installer WITHOUT sudo: "
                     f"./install.py --kernel")
        ok = subprocess.run(_as_user([sys.executable, str(installer),
                            "--bpf", kflag]), cwd=MONTAUK_CLONE).returncode == 0

    if not (ok and montauk_available() and MONTAUK_ANALYZE_INSTALLED.is_file()):
        log_error("montauk install failed.")
        return False, False, False
    log_info(f"montauk installed -> {MONTAUK_INSTALLED.parent}")
    return True, True, uninstall_after


def remove_montauk_if_ours(installed_by_us: bool, uninstall_after: bool) -> None:
    """Remove montauk iff we installed it AND the user opted to uninstall after
    (decided up front, not re-prompted). The ephemeral install placed only the
    two binaries, so removal is a clean rm + dropping the clone. A KEEP install
    is left in /usr/local; only the transient clone is cleared."""
    if not installed_by_us:
        return
    if not uninstall_after:
        subprocess.run(["rm", "-rf", str(MONTAUK_CLONE)])
        log_info(f"keeping montauk at {MONTAUK_INSTALLED.parent}.")
        return
    subprocess.run(["rm", "-f", str(MONTAUK_INSTALLED),
                    str(MONTAUK_ANALYZE_INSTALLED)])
    subprocess.run(["rm", "-rf", str(MONTAUK_CLONE)])
    log_info("montauk removed.")


def _sched_flags(bench: str, schedulers: str, all_scx: bool) -> list[str]:
    """External scheduler selection threaded to each bench's trace path. Every
    bench now fields. --schedulers L runs EEVDF vs EXACTLY L (PANDEMONIUM only if
    named); --all-scx runs the full installed field; neither runs EEVDF +
    PANDEMONIUM. Each bench gets only the flag form it parses:
      cachyos / fork-thread / burst-starvation / sojourn-pressure : --all-scx | --schedulers
      ipc (-> bench-scale)                                        : --schedulers"""
    takes_all = bench in ("cachyos", "fork-thread", "burst-starvation",
                          "sojourn-pressure", "cold-wake")
    takes_list = bench in ("cachyos", "fork-thread", "ipc", "burst-starvation",
                           "sojourn-pressure", "cold-wake")
    if all_scx and takes_all:
        return ["--all-scx"]
    if schedulers and takes_list:
        return ["--schedulers", schedulers]
    return []


def _stop_running_scheduler() -> None:
    """Mirror the rest of the suite: before the profile activates EEVDF /
    PANDEMONIUM per bench, make sure no sched_ext scheduler is already
    registered -- above all the systemd `pandemonium` service. If one is, every
    bench's activation wedges on it ("waiting for scheduler cleanup: pandemonium
    still registered"). The service is Restart=on-failure, so a clean stop sticks
    for the whole run. We already run as root (the main() self-elevate)."""
    if not is_scx_active():
        return
    log_warn(f"sched_ext active ({scx_scheduler_name()}) -- stopping the "
             f"pandemonium service for the profile")
    subprocess.run(["systemctl", "stop", "pandemonium"], capture_output=True)
    if not wait_for_deactivation(10.0):
        log_warn("a scheduler is still registered after stop -- benches may "
                 "fail to activate (clear it: sudo systemctl stop pandemonium)")


def _eject_scheduler() -> None:
    """On Ctrl+C, leave NO sched_ext scheduler registered -- the user interrupted
    and expects the box back on stock EEVDF, not stranded under a bench's
    scheduler. Ctrl+C already SIGINT'd the bench subprocess and the scheduler it
    spawned (shared process group); the systemd pandemonium service is NOT in
    that group, so stop it explicitly, then wait. If a scheduler ignored the
    signal, force-kill it by exact comm (safe -- never matches the harness)."""
    if not is_scx_active():
        return
    name = scx_scheduler_name()
    log_warn(f"interrupted -- ejecting active scheduler ({name})")
    subprocess.run(["systemctl", "stop", "pandemonium"], capture_output=True)
    if wait_for_deactivation(8.0):
        log_info("scheduler ejected -- back on stock EEVDF")
        return
    # An external scheduler (scx_cake etc.) loaded by a child bench is a bare
    # process that systemctl does not own, and its sched_ext OPS name ("cake")
    # differs from its process comm ("scx_cake") -- so a pkill on the OPS name
    # alone misses it. Try the ops name, the scx_-prefixed comm, then a cmdline
    # match, killing whichever the running scheduler actually goes by.
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


def run_profile(schedulers: str, all_scx: bool, ultra: bool = False) -> list[Path]:
    """Run the frozen profile, traced. Returns fresh recording dirs. With no flag
    the field is EEVDF + PANDEMONIUM; --all-scx adds every installed scx (keeps
    PANDEMONIUM); --schedulers L compares EEVDF vs exactly the named schedulers
    (NO PANDEMONIUM) across every bench -- fork-thread and ipc now field too.

    The two width-specific faults are pinned to native core width by default. With
    ultra=True they sweep EVERY width (2,4,8,...,max) -- more coverage, but one
    trace dir per width per scheduler (the file-count blowup --ultra warns about)."""
    py = sys.executable
    ncpus = get_online_cpus()
    ensure_trace_dir()
    start = datetime.now().timestamp()
    # Default pins the width benches to native width (one capture each); --ultra
    # drops the pin so they fall to the suite's full compute_core_counts() sweep.
    width_flags = [] if ultra else ["--core-counts", str(ncpus)]
    benches = [
        ("cachyos", [py, str(TESTS_DIR / "bench-cachyos.py"),
                     "--trace", "--runs", "1",
                     "--workloads", PROFILE_CACHYOS_WORKLOADS]),
        ("fork-thread", [py, str(TESTS_DIR / "bench-fork-thread.py"),
                         "--quick", "--trace", "--compare-eevdf",
                         "--iterations", str(PROFILE_ITERATIONS)]),
        ("ipc", [py, str(TESTS_DIR / "bench-ipc.py"),
                 "--trace", "--core-counts", str(ncpus)]),
        # The two width-specific faults: burst-starvation (bench-pcpu's per-CPU
        # DSQ "probe during burst") bites at 2C, sojourn-pressure (bench-
        # contention's deep-batch rescue phase) blows up at 8C. Native width by
        # default; --ultra sweeps all widths so the cadence is visible. Each
        # capture is capped to its natural test-suite duration (15s), not longer.
        ("burst-starvation", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                              "bench-pcpu", "--trace", "--duration", "15",
                              *width_flags]),
        ("sojourn-pressure", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                              "bench-contention", "--phase", "sojourn-pressure",
                              "--trace", "--duration", "15", *width_flags]),
        # Cold-wake: cycle a pinned core idle->bare-wake under powersave so
        # montauk's COLD-WAKE block separates a frequency-ramp (governor /
        # architecture) from a dispatch delay (the scheduler) on the first wake
        # off a deep-idle core -- the cost side of the envelope's idle win.
        ("cold-wake", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                       "bench-coldwake", "--trace", "--duration", "60"]),
        # Storm: the cpu_release kick-storm reproducer (powersave). Drives the
        # boot/heavy-fork condition synthetically and scores storm% + real-IPI vs
        # IDLE-churn from the scheduler tick log -- the failure mode the other
        # benches (warm, controlled) never trigger. BPF arm only.
        ("storm", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                   "bench-coldwake", "--storm", "--duration", "30"]),
    ]
    for name, cmd in benches:
        # Stop any registered scheduler before EVERY bench, not just once: some
        # benches restore the systemd pandemonium service when they finish (ipc /
        # bench-scale logs "PANDEMONIUM service restored"), which would re-wedge
        # the next bench's activation. A clean stop sticks (Restart=on-failure).
        _stop_running_scheduler()
        log.section(f"{_PROFILE_LABELS.get(name, name)}: TRACE")
        # Child mode: the sub-bench suppresses its own banner/preamble/report/
        # dmesg so the profile reads as one uniform progress log (TERMINAL_STYLE).
        child_env = {**os.environ, "PANDEMONIUM_CHILD": "1"}
        r = subprocess.run(cmd + _sched_flags(name, schedulers, all_scx),
                           cwd=TESTS_DIR, env=child_env)
        if r.returncode != 0:
            log_warn(f"[{name}] exited {r.returncode} -- continuing")
    # Recordings created this run (montauk --trace writes dirs under TRACE_DIR).
    recs = []
    if TRACE_DIR.is_dir():
        for d in sorted(TRACE_DIR.glob("montauk-*")):
            if d.is_dir() and d.stat().st_mtime >= start:
                recs.append(d)
    return recs


def _comm_for(cmd_str: str) -> str:
    """montauk --trace matches on comm (TASK_COMM_LEN-1 = 15 chars). Derive it
    from the workload command's program name."""
    try:
        argv0 = shlex.split(cmd_str)[0]
    except ValueError:
        toks = cmd_str.split()
        argv0 = toks[0] if toks else cmd_str
    return os.path.basename(argv0)[:15]


def run_workload_trace(workload: str | None, attach: str | None,
                       duration: float, stamp: str) -> Path | None:
    """Trace the user's OWN isolated workload under the live scheduler and return
    its recording dir. Two modes:
      workload: launch the command and trace it until it exits (or `duration`).
      attach:   trace an already-running program (comm pattern) for `duration`.
    Whatever scheduler is currently loaded is the one measured -- the user has
    already reproduced the issue under it; we capture that, we do not switch."""
    ensure_trace_dir()
    ncpus = get_online_cpus()
    drain = max(0, ncpus - 1)  # keep montauk off cpu0; best-effort drain core

    if attach:
        comm = attach[:15]
        log_info(f"tracing running program '{comm}' for {duration:.0f}s "
                 f"(montauk on cpu{drain}) ...")
        with montauk_trace(comm, f"workload-{comm}", stamp,
                           events=True, pin_cpu=drain) as rec:
            time.sleep(duration)
        return rec.dir

    comm = _comm_for(workload)
    argv = shlex.split(workload)
    log_info(f"tracing workload '{comm}' (montauk on cpu{drain}) ...")
    with montauk_trace(comm, f"workload-{comm}", stamp,
                       events=True, pin_cpu=drain) as rec:
        try:
            proc = subprocess.Popen(argv)
        except (OSError, ValueError) as e:
            log_error(f"could not launch workload: {e}")
            return None
        try:
            if duration > 0:
                try:
                    proc.wait(timeout=duration)
                except subprocess.TimeoutExpired:
                    log_info(f"duration {duration:.0f}s reached -- stopping workload")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            else:
                proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            raise
    return rec.dir


def _clean_label(rec_dir_name: str) -> str:
    """Recording dir -> clean workload label, e.g.
    montauk-fork-thread-PANDEMONIUM-BPF-20260616-113529 -> fork-thread-PANDEMONIUM-BPF."""
    s = rec_dir_name
    if s.startswith("montauk-"):
        s = s[len("montauk-"):]
    parts = s.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        s = parts[0]
    return s


def capture_cleanroom() -> dict:
    """Snapshot the host state that decides whether the numbers are trustworthy:
    load-per-cpu and uptime (the soak that poisons single-run tails). Stamped
    CLEAN/NOISY so the digest leads with it -- a NOISY run says 'reboot first'
    instead of letting the reader trust contaminated tails."""
    try:
        up_h = float(Path("/proc/uptime").read_text().split()[0]) / 3600
    except Exception:
        up_h = 0.0
    try:
        la1 = float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        la1 = 0.0
    ncpu = os.cpu_count() or 1
    noisy = (la1 / ncpu) >= 0.5 or up_h >= 2.0
    return {"verdict": "NOISY" if noisy else "CLEAN",
            "detail": f"load {la1:.1f}/{ncpu}cpu, uptime {up_h:.1f}h"}


def _prom_label_safe(s: str) -> str:
    """Sanitize a string for a prometheus label value (no backslash, quote, or
    newline) and bound its length."""
    return s.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()[:160]


def write_stability_markers(rec_dir: Path, dmesg: "DmesgMonitor",
                            cleanroom: dict) -> None:
    """Write the markers montauk's digest leads with: the clean-room verdict
    always, and an ejection line when dmesg caught a fault-shaped disable. The
    digest scrapes these from the recording dir and prints SCHEDULER STABILITY
    front and center, above SYSTEM -- a crash invalidates every number below it,
    so it must be the first thing the reader sees."""
    out = [f'montauk_cleanroom{{verdict="{cleanroom["verdict"]}",'
           f'detail="{_prom_label_safe(cleanroom["detail"])}"}} 1']
    if dmesg.crashed and dmesg.crash_msg:
        out.append('montauk_scx_ejected{scheduler="pandemonium",'
                   f'reason="{_prom_label_safe(dmesg.crash_msg)}"}} 1')
    try:
        (rec_dir / "stability.prom").write_text("\n".join(out) + "\n")
    except OSError as e:
        log_warn(f"could not write stability markers to {rec_dir.name}: {e}")


def _split_digest(digest: str) -> tuple[str, str, str]:
    """Split a digest into (stability, system, rest). SCHEDULER STABILITY +
    CLEAN-ROOM and SYSTEM are machine-wide -- printed once at the report top;
    THERMAL/POWER + POORLY-BEHAVING + KEY METRICS stay per-workload."""
    s = digest.lstrip("\n")
    body_markers = ("\nSYSTEM", "\nTHERMAL/POWER", "\nPOORLY-BEHAVING",
                    "\nKEY METRICS")
    stability = ""
    if s.startswith("SCHEDULER STABILITY"):
        end = min([len(s)] + [m for m in (s.find(x) for x in body_markers) if m != -1])
        stability = s[:end].rstrip()
        s = s[end:].lstrip("\n")
    system = ""
    if s.startswith("SYSTEM"):
        end = min([len(s)] + [m for m in (s.find(x) for x in body_markers[1:])
                              if m != -1])
        system = s[:end].rstrip()
        s = s[end:].lstrip("\n")
    return stability, system, s


# THERMAL/POWER lines worth keeping in an end-user report -- the signal a user
# acts on. cpu clock / energy / ctx-sw / migrations / branch-mis are detail that
# stays in the recording, not the report.
_THERMAL_KEEP = ("cpu temp", "power", "idle")

# Profile families and schedulers, longest-match-first so PANDEMONIUM-BPF is not
# shadowed by PANDEMONIUM. Used to parse a clean recording label into (family,
# scheduler) for the comparison summary.
_FAMILIES = ["fork-thread", "sojourn-pressure", "pcpu-burst", "cachyos", "ipc"]
_SCHEDS = ["PANDEMONIUM-ADAPTIVE", "PANDEMONIUM-BPF", "PANDEMONIUM", "EEVDF"]
# Order traced families appear in the comparison (cachyos has no trace -> absent).
_COMPARE_ORDER = ["fork-thread", "ipc", "pcpu-burst", "sojourn-pressure"]


def _parse_label_meta(label: str) -> tuple[str | None, str | None]:
    """clean label (e.g. fork-thread-PANDEMONIUM-BPF) -> (family, scheduler)."""
    fam = next((f for f in _FAMILIES if label.startswith(f)), None)
    sched = next((s for s in _SCHEDS if s in label), None)
    return fam, sched


def _parse_p99(body: str) -> int | None:
    """The sched-report p99 wake2run (us) from a digest body, or None if untraced.
    Matches `p99 <N>us` (the trailing space excludes the p999 token)."""
    m = re.search(r"p99 (\d+)us", body)
    return int(m.group(1)) if m else None


def _sched_short(sched: str) -> str:
    return {"PANDEMONIUM-BPF": "BPF", "PANDEMONIUM-ADAPTIVE": "ADAPTIVE"}.get(
        sched, sched)


def _build_comparison(meta: list[tuple[str | None, str | None, int | None]]) -> str:
    """Turn the per-recording (family, scheduler, p99) tuples into the headline
    the report exists to give: EEVDF vs PANDEMONIUM p99, with the ratio. Only
    traced families (those with a p99) appear; cachyos has no trace, so it is
    absent here. lower p99 is better, so ratio = EEVDF / PANDEMONIUM."""
    fam_p99: dict[str, dict[str, int]] = {}
    for fam, sched, p99 in meta:
        if fam and sched and p99 is not None:
            fam_p99.setdefault(fam, {})[sched] = p99
    if not fam_p99:
        return ""
    out = ["COMPARISON  (p99 wake2run, lower is better)"]
    for fam in _COMPARE_ORDER:
        m = fam_p99.get(fam)
        if not m:
            continue
        base = m.get("EEVDF")
        parts = []
        for sched in ("PANDEMONIUM-BPF", "PANDEMONIUM-ADAPTIVE", "PANDEMONIUM"):
            if sched not in m:
                continue
            v = m[sched]
            if base and v > 0:
                ratio = base / v
                verdict = (f"{ratio:.1f}x better" if ratio >= 1
                           else f"{1 / ratio:.1f}x worse")
                parts.append(f"{_sched_short(sched)} {v}us ({verdict})")
            else:
                parts.append(f"{_sched_short(sched)} {v}us")
        base_str = f"EEVDF {base}us" if base is not None else "EEVDF n/a"
        out.append(f"  {fam:18} {base_str} -> " + ", ".join(parts))
    return "\n".join(out) if len(out) > 1 else ""


def _consolidate_body(body: str, is_cachyos: bool = False) -> str:
    """Trim the digest body to what an end-user report needs. The full montauk
    digest still lives in the recording; this is the consolidated view the report
    carries. Drops:
      - the "not analyzed (no per-event trace)" KEY METRICS non-result;
      - an all-zero dispatch-stall block (no pass-overs) -- pure boilerplate; the
        sched VERDICT already carries the tick-floor %. A real stall is kept;
      - THERMAL/POWER detail -> kept to temp/power/idle; for cachyos (no latency,
        not the runtime) the THERMAL/POWER block is dropped entirely."""
    lines = body.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        st = ln.strip()
        if st == "KEY METRICS: not analyzed (no per-event trace)":
            i += 1
            continue
        if st == "REPORT dispatch-stall":
            verdict = lines[i + 1] if i + 1 < n else ""
            if "avg 0.0 pass-overs" in verdict and "p99 0 pass-overs" in verdict:
                break
        if st == "THERMAL/POWER":
            j = i + 1
            block = []
            while j < n and lines[j].startswith("  "):
                block.append(lines[j])
                j += 1
            if is_cachyos:
                # Drop the whole block (header + lines) and a trailing blank.
                i = j + 1 if (j < n and lines[j].strip() == "") else j
                continue
            out.append(ln)  # header
            for bl in block:
                if any(bl.strip().startswith(k) for k in _THERMAL_KEEP):
                    out.append(bl)
            i = j
            continue
        out.append(ln)
        i += 1
    return "\n".join(out).rstrip()


def _coldwork_ramp(rec_dir: Path) -> str | None:
    """Read coldwork-quanta.txt (SIZE / MEM rows) into a one-line cold-wake summary.
    The felt cost off a cold core is COLD CACHES, not the CPU clock: the MEM
    pointer-chase shows the cold/warm multiple (milliseconds for L3-sized work),
    which dwarfs the CPU frequency ramp. Reports the worst cold-cache multiple and
    the CPU ramp beside it. None when the recording has no coldwork rows."""
    f = rec_dir / "coldwork-quanta.txt"
    if not f.is_file():
        return None
    cpu: dict[int, tuple[list, list]] = {}   # iters -> (cold_ns, warm_ns)
    mem: dict[int, tuple[list, list]] = {}   # bytes -> (cold_ns, warm_ns)
    try:
        for ln in f.read_text().splitlines():
            p = ln.split()
            if len(p) == 8 and p[2] == "COLD" and p[5] == "WARM" and p[0] in ("SIZE", "MEM"):
                grp = cpu if p[0] == "SIZE" else mem
                d = grp.setdefault(int(p[1]), ([], []))
                d[0].append(int(p[3])); d[1].append(int(p[6]))
    except (OSError, ValueError):
        return None
    if not cpu and not mem:
        return None
    def p50(v: list) -> int:
        s = sorted(v)
        return s[len(s) // 2] if s else 0
    parts = []
    if mem:
        wmult, wlbl, wcold = 1.0, "", 0.0
        for by, (cn, wn) in mem.items():
            cold, warm = p50(cn), p50(wn)
            mult = (cold / warm) if warm else 1.0
            if mult > wmult:
                lbl = f"{by >> 20}MB" if by >= 1 << 20 else f"{by >> 10}KB"
                wmult, wlbl, wcold = mult, lbl, cold / 1e6
        parts.append(f"cold-cache worst {wmult:.1f}x at {wlbl} ({wcold:.2f}ms cold)")
    if cpu:
        wpen = 0.0
        for it, (cn, wn) in cpu.items():
            cold, warm = p50(cn), p50(wn)
            pen = (100.0 * (cold - warm) / warm) if warm else 0.0
            wpen = max(wpen, pen)
        parts.append(f"CPU freq ramp worst {wpen:+.0f}%")
    return "  COLD-WAKE: " + "; ".join(parts)


def _storm_section() -> str:
    """Score the storm reproducer's tick log into a report block. bench-coldwake
    --storm writes storm-*.tick to LOG_DIR; the newest is this run's. The storm is
    the one failure the warm/controlled benches never trigger, so the shareable
    report has to carry its verdict -- storm% and real-IPI-vs-IDLE-churn, plus a
    WATCHDOG-ABORT flag if the monitor starved and the scheduler self-ejected."""
    try:
        import storm_score
    except Exception:
        return ""
    ticks = sorted(LOG_DIR.glob("storm-*.tick"), key=lambda p: p.stat().st_mtime)
    if not ticks:
        return ""
    d = storm_score.score(str(ticks[-1]))
    if "error" in d:
        note = " -- WATCHDOG ABORT (scheduler self-ejected)" if d.get("aborted") else ""
        return f"STORM  (cpu_release flood, powersave)\n  {d['error']}{note}"
    out = ["STORM  (cpu_release flood, powersave -- lower storm% is better)"]
    if d.get("aborted"):
        out.append("  WATCHDOG ABORT -- monitor starved >10s, scheduler self-ejected")
    out.append(f"  storm        {d['pct']:.1f}% ({d['storm']}/{d['ticks']} ticks)  "
               f"d/s p50={d['p50']} max={d['dmax']}")
    if d["sample"]:
        out.append(f"  kicks        {d['real']} real-IPI / {d['churn']} idle-churn "
                   f"-- {d['kind']}")
    else:
        out.append("  kicks        no storm ticks -- clean")
    return "\n".join(out)


def build_report(recs: list[Path], tag: str, stamp: str,
                 dmesg: DmesgMonitor, cleanroom: dict) -> Path:
    """Assemble the shareable report from montauk's digest of each recording."""
    ver = get_version()
    git = get_git_info()
    dirty = "-dirty" if git.get("dirty") else ""
    # Header matches the suite convention: <name> v<ver> [<commit><dirty>] [<mode>]
    # then a meta line; no decorative separators anywhere.
    lines = [f"bench-enduser v{ver} [{git['commit']}{dirty}] [{tag}]",
             f"captured: {stamp}",
             ""]
    analyze = _analyze_bin()
    # First pass: digest each recording, hoist the machine-wide blocks once, and
    # collect each workload's consolidated body + (family, scheduler, p99) so the
    # comparison can lead the report. The COMPARISON section is the headline -- it
    # turns 19 isolated blocks into the EEVDF-vs-PANDEMONIUM answer.
    stab_text = ""
    sys_text = ""
    workloads = []   # (label, body)
    meta = []        # (family, scheduler, p99)
    for d in recs:
        write_stability_markers(d, dmesg, cleanroom)
        out = subprocess.run(
            [analyze, str(d), "--digest", "--redact"],
            capture_output=True, text=True).stdout
        stab, sysblock, body = _split_digest(out)
        if stab and not stab_text:
            stab_text = stab
        if sysblock and not sys_text:
            sys_text = sysblock
        label = _clean_label(d.name)
        fam, sched = _parse_label_meta(label)
        meta.append((fam, sched, _parse_p99(body)))
        cbody = _consolidate_body(body, is_cachyos=(fam == "cachyos"))
        ramp = _coldwork_ramp(d)   # cold-wake recordings carry coldwork-quanta.txt
        if ramp:
            cbody = (cbody + "\n" + ramp) if cbody.strip() else ramp
        workloads.append((label, cbody))

    if stab_text:
        lines += [stab_text, ""]
    if sys_text:
        lines += [sys_text, ""]
    # Stall-susceptibility profile: every shared report carries whether THIS box
    # can produce the dark-CPU dispatch strand (HZ, tickless, cores, C-states),
    # so a maintainer can diff a user's machine against their own and see at a
    # glance why one reproduces a stall and another never does.
    susc_lines, _ = stall_susceptibility()
    lines += susc_lines + [""]
    comparison = _build_comparison(meta)
    if comparison:
        lines += [comparison, ""]
    storm = _storm_section()
    if storm:
        lines += [storm, ""]
    for label, body in workloads:
        lines.append("")
        lines.append(f"WORKLOAD {label}")
        lines.append(body if body.strip() else "  (no analyzable data)")
    # Fallback only if the front-and-center stability block did not render (e.g.
    # an older montauk_analyze without scx_stability_block): never lose a crash.
    if dmesg.crashed and not stab_text:
        lines += ["", f"SCHEDULER CRASH: {dmesg.crash_msg}"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"enduser-report-{ver}-{stamp}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schedulers", default="", metavar="LIST",
                    help="comma-separated scx schedulers to add to the cachyos "
                         "suite, e.g. scx_rusty,scx_lavd (EEVDF + PANDEMONIUM are "
                         "always in the field). Default: none")
    ap.add_argument("--all-scx", action="store_true",
                    help="add the full installed scx field to the cachyos suite "
                         "instead of a named list. Cannot be combined with "
                         "--schedulers")
    ap.add_argument("--workload", metavar="CMD",
                    help="trace YOUR isolated workload: run CMD under the live "
                         "scheduler and report on it (instead of the fixed profile)")
    ap.add_argument("--attach", metavar="COMM",
                    help="trace an already-running program by comm name for "
                         "--duration seconds (instead of launching one)")
    ap.add_argument("--duration", type=float, default=0.0, metavar="S",
                    help="cap a --workload run / set --attach window (seconds); "
                         "default: run until the workload exits")
    ap.add_argument("--iterations", type=int, default=1, metavar="N",
                    help="run the profile N times (one report per run), to see "
                         "past per-run noise (default: 1)")
    ap.add_argument("--ultra", action="store_true",
                    help="trace the width-specific faults (burst-starvation, "
                         "sojourn-pressure) at EVERY core width (2,4,8,...,max) "
                         "instead of native width only. Surfaces width-cadence "
                         "bugs but writes one trace dir per width per scheduler -- "
                         "many files, sizes from KB to hundreds of MB")
    args = ap.parse_args()

    # --schedulers and --all-scx are two ways to pick the SAME thing (the
    # external field); running both is ambiguous. Warn with a usage hint rather
    # than silently favoring one.
    if args.all_scx and args.schedulers:
        log_warn("--schedulers and --all-scx cannot be used together -- pick one:")
        log_warn("  --schedulers scx_rusty,scx_lavd   compare against a chosen set")
        log_warn("  --all-scx                          compare against the full field")
        return 1

    if os.geteuid() != 0:
        # SELF-ELEVATE: the report flow needs root end-to-end (montauk eBPF
        # attach + sched_ext load + RAPL/PMU), so re-exec under sudo rather than
        # make the user type it -- matches bench-fork-thread and the rest of the
        # suite. Everything below then inherits root; recordings chown back via
        # SUDO_USER and the report lands in the invoking user's ~/.cache.
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])

    trace_mode = bool(args.workload or args.attach)
    if args.attach and args.duration <= 0:
        args.duration = 20.0  # an attach needs a finite window
    if trace_mode:
        tag = f"trace={(args.attach or _comm_for(args.workload))[:15]}"
    elif args.all_scx:
        tag = "sched=all-scx"
    elif args.schedulers:
        tag = f"sched=+{args.schedulers}"
    else:
        tag = "sched=eevdf+pandemonium"

    ver = get_version()
    git = get_git_info()
    dirty = "-dirty" if git.get("dirty") else ""
    log_info(f"bench-enduser v{ver} [{git['commit']}{dirty}] [{tag}]")
    print()
    welcome(trace_mode)

    # --ultra only changes the fixed-profile width sweep; it has no effect on a
    # --workload / --attach capture. Warn on the path it actually changes.
    if args.ultra and not trace_mode:
        log_warn('WARNING: Utilizing "--ultra" will produce many files that vary '
                 "in size. Please use responsibly...")
        print()
    elif args.ultra and trace_mode:
        log_warn("--ultra has no effect with --workload/--attach (no width sweep)")

    available, installed_by_us, uninstall_after = ensure_montauk()
    if not available:
        return 1

    iters = max(1, args.iterations)
    # Clean-room state at start of the session -- leads the digest so a NOISY
    # box (high load / long uptime) is flagged before anyone trusts the tails.
    cleanroom = capture_cleanroom()
    if cleanroom["verdict"] == "NOISY":
        log_warn(f"CLEAN-ROOM: NOISY ({cleanroom['detail']}). Single-run tails "
                 "are background-contaminated; a reboot gives trustworthy numbers.")
    reports: list[Path] = []
    try:
        for it in range(iters):
            if iters > 1:
                print()
                log_info(f"iteration {it + 1}/{iters}")
            # Per-iteration stamp; the index suffix keeps reports distinct even
            # for sub-second --workload runs in the same wall-clock second.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if iters > 1:
                stamp += f"-{it + 1}"
            dmesg = DmesgMonitor()
            if trace_mode:
                rec = run_workload_trace(args.workload, args.attach,
                                         args.duration, stamp)
                dmesg.check()
                if not rec or not rec.is_dir():
                    log_error("no report: the workload was not traced")
                    return 1
                print()
                report = build_report([rec], tag, stamp, dmesg, cleanroom)
            else:
                recs = run_profile(args.schedulers, args.all_scx, args.ultra)
                dmesg.check()
                if not recs:
                    # No-load fallback: the scheduler never produced a recording
                    # (it may have failed to activate). Still hand back something
                    # actionable.
                    log_warn("no recordings produced -- scheduler may have failed "
                             "to activate; saving a dmesg-only report")
                    dmesg.save(stamp)
                    log_error("no report: nothing was traced (see dmesg above)")
                    return 1
                print()
                report = build_report(recs, tag, stamp, dmesg, cleanroom)
            # Absorb the digest into the file -- the terminal stays the progress
            # log; the full consolidated report is the artifact the user opens and
            # shares. No verbatim dump of the report to the terminal.
            log_info(f"REPORT: {report}")
            reports.append(report)
        if len(reports) == 1:
            log_info("Share this file -- it is small, redacted, and self-contained.")
        else:
            log_info(f"{len(reports)} reports written -- each small, redacted, and "
                     "self-contained.")
    except KeyboardInterrupt:
        # Respect Ctrl+C: the profile activates its own schedulers, so eject
        # whatever is registered and leave the box on stock EEVDF. Trace mode
        # rides the user's CURRENT scheduler -- never eject that.
        print()
        if not trace_mode:
            _eject_scheduler()
        raise
    finally:
        remove_montauk_if_ours(installed_by_us, uninstall_after)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
