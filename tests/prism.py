#!/usr/bin/env python3
"""prism: turn a scheduler problem into one shareable file.

For when the system stutters or lags under load. It runs a short fixed profile
(fork-thread, ipc and the fault probes) under your scheduler and EEVDF as a neutral
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
    PANDEMONIUM_FLAGS, add_field_flags,
    MONTAUK, montauk_available, DmesgMonitor, montauk_trace, install_hint,
    montauk_analyze_argv,
    envelope_report, 
    TRACE_DIR, LOG_DIR, get_online_cpus, get_possible_cpus,
    stall_susceptibility, median, restore_all_cpus, restrict_cpus,
    eject_scheduler, install_exit_guard, note_scheduler_activated,
    clear_loaded_scheduler,
)

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The tracer and the analyzer are ONE binary as of montauk v8.10.0 -- the
# analyzer is a mode (`--analyze`), not a separate file to check for. As of
# v8.13.0 there is a fourth mode, `--static`, which reads SOURCE rather than a
# capture; PRISM does not invoke it and does not need to. It is named here so
# the arrangement this file assumes is the whole arrangement.
MONTAUK_INSTALLED = Path(MONTAUK)
# sublimation does every stream and numeric step in this file. The ephemeral
# install used to place only the two binaries above, so a run that opted to
# uninstall afterward never got sublimation at all: it fell back to Python for
# the whole report and said so once, in a warning, on a line nobody reads. It
# ships with montauk and belongs in the ephemeral set for the same reason the
# analyzer does -- the report cannot be assembled correctly without it.
SUBLIMATION_INSTALLED = MONTAUK_INSTALLED.with_name("sublimation")

# montauk is not bundled. When it is missing, prism clones it from the
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

# Display labels for the profile phases -- proper-cased names + the "TRACE" verb,
# so the section headers read uniformly (fork-thread: TRACE, IPC: TRACE, ...).
_PROFILE_LABELS = {
    "fork-thread": "fork-thread",
    "ipc": "IPC",
    "burst-starvation": "burst-starvation",
    "sojourn-pressure": "sojourn-pressure",
    "cold-wake": "Cold-Wake",
    "storm": "storm",
}


def _analyze_argv() -> list[str]:
    """Prefer the freshly-built montauk from the clone (it matches what we just
    installed); fall back to the installed one. Returns the ARGV PREFIX, since
    the analyzer is a mode word and a path string cannot carry one."""
    clone_m = MONTAUK_CLONE / "build" / "montauk"
    binary = str(clone_m) if clone_m.is_file() else str(MONTAUK_INSTALLED)
    return montauk_analyze_argv(binary)


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
    log_info("PRISM turns a scheduler problem into one shareable file.")
    log_info("These are synthetic benchmarks -- they approximate the stutter and lag")
    log_info("you feel under load, but are not real-world usage.")
    print()
    if trace_mode:
        log_info("You isolated it to one program: this traces THAT program under your")
        log_info("current scheduler and names what misbehaves on your workload.")
    else:
        log_info("A short profile runs under your scheduler and EEVDF (a neutral")
        log_info("reference), naming what misbehaves under each.")
    log_info("montauk measures; the report ranks the offenders by name -- hot CPUs,")
    log_info("livelocking tasks, unsignaled waiters -- with your specs and the key")
    log_info("latency metrics, in one small file a maintainer reads as you do.")
    log_info("Names are hashed, nothing is uploaded, raw traces stay local; share the")
    log_info("one file it prints at the end.")
    if not trace_mode:
        log_info("This may take five-plus minutes depending on your system.")
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
    """The installed montauk's version via `montauk --analyze --version`. None
    if it is too old to have the flag (pre-7.5.0 printed nothing) or unreadable
    -- either way, a candidate for upgrade."""
    if not MONTAUK_INSTALLED.is_file():
        return None
    try:
        r = subprocess.run([*montauk_analyze_argv(), "--version"],
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
    up front, clones the repo, and drives montauk's OWN installer -- prism
    wraps montauk's install, it does not package binaries. A KEEP install runs
    `install.py` (permanent, capped, to /usr/local); an ephemeral one builds the
    clone and copies the two binaries into place for a clean two-file removal.

    An already-installed montauk is version-gated: if it is older than the repo's
    current version (read dynamically, not hardcoded) it is upgraded -- uninstall
    old, clone + install latest -- so the report is never assembled against a
    montauk too old to surface a crash front and center."""
    have_analyze = MONTAUK_INSTALLED.is_file()
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
                          ("sublimation", SUBLIMATION_INSTALLED)):
            src = build / name
            if not src.is_file() or subprocess.run(
                    ["install", "-m755", str(src), str(dst)]).returncode != 0:
                # sublimation missing is not fatal -- the Python fallbacks are
                # real, if slower and less capable -- but it must be LOUD, not a
                # warning buried under a successful install.
                if name == "sublimation":
                    log_warn("sublimation did not install; stream and numeric "
                             "analysis will fall back to Python for this entire "
                             "report. Numbers stay correct, shape verdicts get "
                             "coarser.")
                    continue
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

    if not (ok and montauk_available()):
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
    # Every binary the ephemeral install placed, or the uninstall leaves the
    # system dirtier than it found it -- sublimation was installed here and not
    # removed, which is the half-uninstall a benchmark harness must never do.
    removed = []
    for path in (MONTAUK_INSTALLED, SUBLIMATION_INSTALLED):
        if path.is_file():
            subprocess.run(["rm", "-f", str(path)])
            removed.append(path.name)
    subprocess.run(["rm", "-rf", str(MONTAUK_CLONE)])
    # Name what went, and verify rather than assume: an uninstall that reports
    # success while leaving a binary behind is worse than one that refuses.
    left = [p.name for p in (MONTAUK_INSTALLED, SUBLIMATION_INSTALLED)
            if p.is_file()]
    if left:
        log_warn(f"uninstall incomplete -- still present: {', '.join(left)}")
    else:
        log_info(f"montauk removed ({', '.join(removed) or 'nothing to remove'}).")


def _field_flags(schedulers: str, all_scx: bool, only: str, pandemonium: str | None,
                 arms=(), disturb: str | None = None) -> list[str]:
    """The field's flags as every bench takes them (pandemonium_common's
    add_field_flags), so they pass to any bench unchanged."""
    flags = [PANDEMONIUM_FLAGS[pandemonium]] if pandemonium else []
    for arm in arms or ():
        flags += ["--arm", arm]
    if disturb:
        flags += ["--disturb", disturb]
    if only:
        return flags + ["--only", only]
    if all_scx:
        return flags + ["--all-scx"]
    if schedulers:
        return flags + ["--schedulers", schedulers]
    return flags


def run_profile(schedulers: str, all_scx: bool, ultra: bool = False,
                pandemonium: str | None = None, iterations: int = 1,
                only: str = "", arms=(), disturb: str | None = None) -> list[Path]:
    """Run the profile, traced (the report implies its capture), every arm
    `iterations` times inside each bench. Returns fresh recording dirs. With no
    flag the field is EEVDF + PANDEMONIUM; --all-scx adds every installed scx
    (keeps PANDEMONIUM); --schedulers L compares EEVDF vs exactly the named
    schedulers (PANDEMONIUM only if named) across every bench.

    The two width-specific faults are pinned to native core width by default. With
    ultra=True they sweep EVERY width (2,4,8,...,max) -- more coverage, but one
    trace dir per width per scheduler (the file-count blowup --ultra warns about)."""
    py = sys.executable
    ncpus = get_online_cpus()
    ensure_trace_dir()
    # OWNERSHIP, AND WHY IT IS DECLARED HERE. eject_scheduler() refuses to touch a
    # scheduler this PROCESS did not activate, so a user's own scheduler survives a
    # prism run. The profile activates through CHILD benches, each of which sets the
    # flag in its own interpreter -- prism's copy stayed False, so main's Ctrl+C
    # handler called eject_scheduler() and it returned having done nothing, leaving
    # the scheduler registered on every interrupt. The profile owns what its children
    # activate; trace mode still short-circuits one line earlier and never ejects.
    note_scheduler_activated()
    start = datetime.now().timestamp()
    # Default pins the width benches to native width (one capture each); --ultra
    # drops the pin so they fall to the suite's full compute_core_counts() sweep.
    width_flags = [] if ultra else ["--cores", str(ncpus)]
    benches = [
        ("fork-thread", [py, str(TESTS_DIR / "prism-fork-thread.py"),
                         "--quick", "--trace"]),
        ("ipc", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                 "prism-scale", "--cells", "ipc", "--trace", "--cores", str(ncpus)]),
        # The two width-specific faults: burst-starvation (prism-pcpu's per-CPU
        # DSQ "probe during burst") bites at 2C, sojourn-pressure (prism-
        # contention's deep-batch rescue phase) blows up at 8C. Native width by
        # default; --ultra sweeps all widths so the cadence is visible. Each
        # capture is capped to its natural test-suite duration (15s), not longer.
        ("burst-starvation", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                              "prism-pcpu", "--trace", "--duration", "15",
                              *width_flags]),
        ("sojourn-pressure", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                              "prism-contention", "--phase", "sojourn-pressure",
                              "--trace", "--duration", "15", *width_flags]),
        # Cold-wake: cycle a pinned core idle->bare-wake under powersave so
        # montauk's COLD-WAKE block separates a frequency-ramp (governor /
        # architecture) from a dispatch delay (the scheduler) on the first wake
        # off a deep-idle core -- the cost side of the envelope's idle win.
        ("cold-wake", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                       "prism-coldwake", "--trace", "--duration", "60"]),
        # Storm: the cpu_release kick-storm reproducer (powersave). Drives the
        # boot/heavy-fork condition synthetically and scores storm% + real-IPI vs
        # IDLE-churn from the scheduler tick log -- the failure mode the other
        # benches (warm, controlled) never trigger. The sched_ext arms only.
        ("storm", [py, str(TESTS_DIR / "pandemonium-tests.py"),
                   "prism-coldwake", "--storm", "--trace", "--duration", "30"]),
    ]
    for name, cmd in benches:
        # Before EVERY bench, not just once: a scheduler left registered wedges
        # the next bench's activation ("waiting for scheduler cleanup").
        clear_loaded_scheduler(10.0)
        log.section(f"{_PROFILE_LABELS.get(name, name)}: TRACE")
        # Child mode: the sub-bench suppresses its own banner/preamble/report/
        # dmesg so the profile reads as one uniform progress log (TERMINAL_STYLE).
        child_env = {**os.environ, "PANDEMONIUM_CHILD": "1"}
        sflags = _field_flags(schedulers, all_scx, only, pandemonium, arms, disturb)
        r = subprocess.run(cmd + ["--iterations", str(iterations)] + sflags,
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


def digest_envelope(analyze: list[str], rec_dir: Path) -> tuple[dict | None, str]:
    """(envelope, text) for one recording's montauk digest.

    The ENVELOPE (`--digest --redact --json`) is what prism reads; the text is
    kept only so a failed parse can still print montauk's own verdict verbatim
    rather than a scraped approximation of it. This replaces the previous
    marker-hunting split of the text digest -- montauk renders both faces from
    one typed result, so parsing the prose was scraping a rendering when the data
    was one flag away."""
    import json as _json
    text = subprocess.run([*analyze, str(rec_dir), "--digest", "--redact"],
                          capture_output=True, text=True).stdout
    r = subprocess.run([*analyze, str(rec_dir), "--digest", "--redact", "--json"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        log_warn(f"montauk --analyze --digest --json produced nothing for "
                 f"{rec_dir.name} -- carrying its text digest verbatim")
        return None, text
    try:
        return _json.loads(r.stdout), text
    except ValueError:
        log_warn(f"unparseable digest envelope for {rec_dir.name} -- carrying "
                 f"its text digest verbatim")
        return None, text


# ENVELOPE RENDERERS. One per block montauk's digest emits, reproducing its own
# formats (prom_population.cpp's system_info_block / scx_stability_block /
# thermal_power_block, trace_analyze.cpp's emit_offenders_text) from the typed
# fields instead of from its prose. Same output, no string surgery.

def _render_stability(env: dict) -> str:
    st = env.get("stability") or {}
    if not st:
        return ""
    out = ["SCHEDULER STABILITY"]
    ejections = st.get("ejections") or []
    if ejections:
        for e in ejections:
            line = f"{e.get('scheduler', '?')} -- \"{e.get('reason', '')}\""
            phase, cores = e.get("phase", ""), e.get("cores", "")
            if phase or cores:
                inner = phase + ((", " if phase else "") + f"{cores}c" if cores else "")
                line += f"  (during {inner})"
            out.append(f"  EJECTED      {line}")
    else:
        out.append("  no ejection -- scheduler ran clean")
    wd = st.get("watchdog_worst_pct")
    if wd is not None:
        line = f"  watchdog     worst sojourn {wd:.0f}% of the 30s sched_ext limit"
        where = st.get("watchdog_where", "")
        if where:
            line += f"  ({where})"
        if wd >= 50:
            line += "  [NEAR-EJECTION]"
        out.append(line)
    verdict = st.get("cleanroom_verdict", "")
    if verdict:
        detail = st.get("cleanroom_detail", "")
        out += ["", "CLEAN-ROOM",
                f"  state        {verdict}" + (f" -- {detail}" if detail else "")]
    return "\n".join(out)


def _render_system(env: dict) -> str:
    sy = env.get("system") or {}
    if not sy:
        return ""
    cpu = (f"{sy.get('cpu_model', '?')} ({sy.get('physical_cores', '?')}c/"
           f"{sy.get('logical_cpus', '?')}t")
    if sy.get("cache_domains"):
        cpu += f", {sy['cache_domains']} cache domains"
    out = ["SYSTEM", f"  cpu        {cpu})",
           f"  memory     {sy.get('mem_total_gib', '?')} GiB"]
    if sy.get("gpu"):
        out.append(f"  gpu        {sy['gpu']}")
    out += [f"  kernel     {sy.get('kernel', '?')}",
            f"  scheduler  {sy.get('scheduler', '?')}"]
    return "\n".join(out)


def _render_thermal(env: dict) -> str:
    """THERMAL/POWER trimmed to what an end-user acts on: temperature, power draw
    and idle residency. Clock / energy-per-instruction / ctx-sw / migrations /
    branch-miss rates stay in the recording -- montauk's full block has them, the
    shareable report does not need them."""
    tp = env.get("thermal_power") or {}
    out = []
    if "cpu_temp_peak_c" in tp:
        out.append(f"  cpu temp   peak {tp['cpu_temp_peak_c']:.1f} C  "
                   f"avg {tp['cpu_temp_avg_c']:.1f} C")
    if "power_avg_w" in tp:
        out.append(f"  power      avg {tp['power_avg_w']:.1f} W  "
                   f"peak {tp['power_peak_w']:.1f} W")
    if tp.get("dominant_cstate"):
        out.append(f"  idle       {tp['dominant_cstate']} avg "
                   f"{tp.get('dominant_cstate_pct', 0.0):.1f}% (dominant)")
    return "\n".join(["THERMAL/POWER"] + out) if out else ""


def _render_offenders(env: dict) -> str:
    offs = env.get("offenders") or []
    if not offs:
        return "POORLY-BEHAVING ITEMS: none detected"
    out = ["POORLY-BEHAVING ITEMS (ranked)",
           f"{'kind':<14} {'id':<18} {'metric':<16} {'value':>14}  sev"]
    for o in offs:
        idobj = o.get("id", "")
        if o.get("obj"):
            idobj = f"{idobj}/{o['obj']}"
        sev = o.get("sev", 0)
        sv = "HIGH" if sev >= 2 else ("MED" if sev == 1 else "LOW")
        out.append(f"{o.get('kind', ''):<14} {idobj:<18} "
                   f"{o.get('metric', ''):<16} {o.get('value', 0.0):>14.6g}  {sv}")
    return "\n".join(out)


def _render_capture(env: dict) -> str:
    """montauk's capture-loss qualification, directly above the numbers it
    qualifies. The tracer sheds under load -- exactly when the interesting events
    happen -- so a quantile off a partial stream is a weaker claim than the same
    quantile off a whole one, and comparing two arms captured at different rates
    compares their sampling as much as their scheduling. Absent when the capture
    predates drop accounting, which montauk reports as absence rather than as
    zero, so silence here means UNKNOWN loss, never proven-clean."""
    dig = env.get("digest") or {}
    if "capture_completeness" not in dig:
        return ""
    pct = 100.0 * dig["capture_completeness"]
    dropped = int(dig.get("dropped_events", 0))
    observed = int(dig.get("events_observed", 0))
    if dropped == 0:
        return f"CAPTURE  complete -- {observed} events, none dropped"
    return (f"CAPTURE  {pct:.1f}% complete -- {observed} events kept, {dropped} "
            f"dropped at the ring\n"
            f"         counts are lower bounds and tail quantiles are biased "
            f"downward; compare arms only at like completeness")


def _render_key_metrics(env: dict) -> str:
    """KEY METRICS from the envelope's report objects. An empty verdict means the
    report had nothing to say (dispatch-stall on a trace with no floored wakes),
    which is dropped rather than printed as boilerplate."""
    if not (env.get("digest") or {}).get("has_events", False):
        return ""
    out = []
    sched = envelope_report(env, "sched")
    if sched.get("verdict"):
        out += ["REPORT sched", f"VERDICT: {sched['verdict']}"]
        stru = sched.get("structure") or {}
        if stru.get("class"):
            out.append(f"STRUCTURE: latency-over-trace {stru['class']}; "
                       f"~{stru.get('distinct_estimate', 0)} distinct values; "
                       f"inversion ratio {stru.get('inversion_ratio', 0.0):.2f}")
        regions = sched.get("located_regions") or []
        if regions:
            shown = ", ".join(f"{r.get('class', '?')}"
                              f"[{r.get('start_pct', 0.0):.0f}%.."
                              f"{r.get('end_pct', 0.0):.0f}%]"
                              for r in regions[:5])
            extra = f" +{len(regions) - 5} more" if len(regions) > 5 else ""
            out.append(f"LOCATED: {len(regions)} structured region(s) "
                       f"{shown}{extra}")
    # dispatch-stall with no pass-overs at all is boilerplate: the sched VERDICT
    # already carries the tick-floor share, and the stall report adds nothing but
    # a row of zeros. Gated on the typed gauges rather than on the shape of the
    # sentence, which is what the old text pass had to match.
    stall = envelope_report(env, "dispatch-stall")
    if stall.get("verdict"):
        g = {x.get("name"): x.get("value") for x in (stall.get("gauges") or [])}
        empty = (g.get("montauk_analysis_dispatch_avg_passovers", 0) == 0
                 and g.get("montauk_analysis_dispatch_passover_p99", 0) == 0)
        if not empty:
            out += ["", "REPORT dispatch-stall", f"VERDICT: {stall['verdict']}"]
    # kstrand only earns space when it actually names a stranded kthread: with no
    # rows its verdict is a negative, and a report of nothing found is what the
    # offender list's absence already says.
    kst = envelope_report(env, "kstrand")
    rows = kst.get("kthreads") or []
    if kst.get("verdict") and rows:
        out += ["", "REPORT kstrand", f"VERDICT: {kst['verdict']}"]
        if rows:
            out.append(f"{'kthread':<18} {'cpu':<5} {'strands':<7} {'max_ms':<9} "
                       f"{'p99_ms':<9} {'held':<6} {'dark':<6} held_by")
            for k in rows:
                hb = k.get("held_by") or {}
                held_by = (f"{hb.get('task', '')} {hb.get('coverage_pct', 0.0):.0f}%"
                           if hb else "")
                out.append(f"{k.get('kthread', ''):<18} {k.get('cpu', 0):<5} "
                           f"{k.get('strands', 0):<7} {k.get('max_ms', 0.0):<9.1f} "
                           f"{k.get('p99_ms', 0.0):<9.1f} {k.get('held', 0):<6} "
                           f"{k.get('dark', 0):<6} {held_by}")
    return "\n".join(["KEY METRICS"] + out) if out else ""


def _render_body(env: dict) -> str:
    """The per-workload body: thermal, ranked offenders, then KEY METRICS."""
    blocks = [_render_thermal(env), _render_offenders(env), _render_capture(env),
              _render_key_metrics(env)]
    return "\n\n".join(b for b in blocks if b)


def _envelope_p99(env: dict) -> int | None:
    """sched-report p99 wake2run (us), or None when the recording has no event
    stream. Read from the typed field, never regexed back out of the verdict."""
    w = (envelope_report(env, "sched").get("wake2run") or {})
    p99 = w.get("p99_us")
    return int(round(p99)) if p99 is not None else None


# Profile families and schedulers, longest-match-first so PANDEMONIUM-BPF is not
# shadowed by PANDEMONIUM. Used to parse a clean recording label into (family,
# scheduler) for the comparison summary.
_FAMILIES = ["fork-thread", "sojourn-pressure", "pcpu-burst", "ipc"]
_SCHEDS = ["PANDEMONIUM-ADAPTIVE", "PANDEMONIUM-BPF", "PANDEMONIUM", "EEVDF"]
# Order traced families appear in the comparison.
_COMPARE_ORDER = ["fork-thread", "ipc", "pcpu-burst", "sojourn-pressure"]


def _parse_label_meta(label: str) -> tuple[str | None, str | None]:
    """clean label (e.g. fork-thread-PANDEMONIUM-BPF) -> (family, scheduler)."""
    fam = next((f for f in _FAMILIES if label.startswith(f)), None)
    sched = next((s for s in _SCHEDS if s in label), None)
    return fam, sched


def _workload_cell(label: str, sched: str | None) -> str:
    """The label with the scheduler token removed: the WORKLOAD, which is what a
    scheduler comparison must hold fixed.

    The family alone is too coarse to compare on. A family that runs several
    distinct workloads under one name, keyed by family, pools unlike
    measurements into one group and reports their count as repeats of one. The
    variance in that group is workload spread, not run-to-run noise, and reading
    power off it would be reading it off the wrong thing. Keyed per workload,
    each cell is N=1 until the run is actually repeated, which is the truth."""
    cell = label
    if sched:
        cell = cell.replace(f"-{sched}-", "-").replace(f"-{sched}", "")
    return cell.strip("-") or label


def _sched_short(sched: str) -> str:
    return {"PANDEMONIUM-BPF": "BPF", "PANDEMONIUM-ADAPTIVE": "ADAPTIVE"}.get(
        sched, sched)


# POPULATION STATISTICS
# The COMPARISON block is a RATIO OF TWO POINT ESTIMATES: one run per arm, no
# effect size, no significance, no idea how many runs a verdict would need. That
# is exactly the question montauk's population face answers -- Cliff's delta,
# permutation p, the run count for 80% power and multiple-comparison-best -- and
# it consumes a .prom set keyed by the compare axis. So prism writes its
# trace-derived p99 as a labeled metric per run, then hands the set to the
# analyzer and renders its verdict. Repeat runs (--iterations N, or several
# invocations) accumulate into the same set, which is what turns an underpowered
# N=1 note into an actual verdict.

_POP_METRIC = "pandemonium_prism_wake2run_p99_us"


def write_population_prom(meta: list[tuple[str | None, str | None, int | None]],
                          stamp: str) -> Path | None:
    """One .prom per run: the traced p99 per (workload, scheduler). scheduler is
    the compare axis; the WORKLOAD is the cell, so montauk compares schedulers
    against the same workload and never pools unlike ones."""
    rows = [(c, s, p) for c, s, p in meta if c and s and p is not None]
    if not rows:
        return None
    out = [f"# HELP {_POP_METRIC} Traced wake2run p99 per workload (us)",
           f"# TYPE {_POP_METRIC} gauge"]
    for cell, sched, p99 in rows:
        out.append(f'{_POP_METRIC}{{scheduler="{sched}",workload="{cell}"}} {p99}')
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-pop-{stamp}.prom"
    try:
        path.write_text("\n".join(out) + "\n")
    except OSError as e:
        log_warn(f"could not write population metrics: {e}")
        return None
    return path


def read_prom_gauges(path: Path) -> list[tuple[str, dict, float]]:
    """(name, labels, value) per sample line in a Prometheus exposition file.
    montauk's population face has no --json, but it emits its full typed result
    as .prom -- one of its three renderings of one result -- so this reads the
    structured output rather than the human report."""
    out = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        name, _, rest = ln.partition("{")
        if not rest:
            parts = ln.split()
            if len(parts) == 2:
                try:
                    out.append((parts[0], {}, float(parts[1])))
                except ValueError:
                    pass
            continue
        labelstr, _, valstr = rest.rpartition("}")
        labels = {}
        for kv in re.findall(r'(\w+)="([^"]*)"', labelstr):
            labels[kv[0]] = kv[1]
        try:
            out.append((name, labels, float(valstr.strip())))
        except ValueError:
            pass
    return out


def _build_population(analyze: list[str], prom: Path | None) -> str:
    """Run montauk's population comparison over every prism-pop .prom on the box
    and render its verdict. The whole set is the population: one run is N=1 and
    montauk says so rather than pretending otherwise."""
    if prom is None:
        return ""
    proms = sorted(LOG_DIR.glob("prism-pop-*.prom"))
    if not proms:
        return ""
    r = subprocess.run([*analyze, *[str(p) for p in proms],
                        "--by", "scheduler", "--pairs", "all",
                        "--metric", _POP_METRIC],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log_warn("population analysis failed -- COMPARISON stands alone")
        return ""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    emitted = Path(cache) / "montauk" / f"analysis-pop-scheduler-{_POP_METRIC}.prom"
    gauges = read_prom_gauges(emitted)
    if not gauges:
        return ""
    # Pair-keyed gauges (cliff / perm_p / power_n) and group-keyed gauges
    # (n / mean / mcb_is_best) land on different label sets: the pair lines carry
    # pair="a__b", the group lines carry group="a". A pair's run count is the
    # smaller of its two groups'.
    cells: dict[tuple[str, str], dict[str, float]] = {}
    group_n: dict[tuple[str, str], int] = {}
    best: dict[str, list[str]] = {}
    for name, labels, value in gauges:
        cell, pair, group = (labels.get("cell", ""), labels.get("pair", ""),
                             labels.get("group", ""))
        if name == "montauk_pop_mcb_is_best":
            # 1 best, 0 tied-for-best, -1 not best.
            if value >= 0 and group:
                best.setdefault(cell, []).append(
                    group + ("" if value == 1 else " (tied)"))
            continue
        if name == "montauk_pop_n" and group:
            group_n[(cell, group)] = int(value)
            continue
        if not pair:
            continue
        key = name.replace("montauk_pop_", "")
        cells.setdefault((cell, pair), {})[key] = value
    if not cells:
        return ""
    fam_of = lambda cell: (re.search(r"workload=(\S+)", cell) or [None, "?"])[1] \
        if "workload=" in cell else "?"
    def pair_n(cell: str, pair: str) -> int:
        a, _, b = pair.partition("__")
        ns = [group_n.get((cell, a)), group_n.get((cell, b))]
        ns = [n for n in ns if n is not None]
        return min(ns) if ns else 0

    out = [f"POPULATION  (montauk cross-run statistics over {len(proms)} run(s), "
           f"per workload)"]
    underpowered = False
    for (cell, pair), g in sorted(cells.items(),
                                  key=lambda kv: (fam_of(kv[0][0]), kv[0][1])):
        n = pair_n(cell, pair)
        underpowered = underpowered or n < 3
        cliff, p = g.get("cliff", 0.0), g.get("perm_p", 1.0)
        power = int(g.get("power_n", 0))
        censored = g.get("power_censored", 0) == 1
        out.append(f"  {fam_of(cell):18} {pair.replace('__', ' vs ')}: "
                   f"N={n} cliff {cliff:+.2f} perm p={p:.3f} "
                   f"power n{'>' if censored else '='}{power}")
    for cell, groups in sorted(best.items()):
        out.append(f"  {fam_of(cell):18} MCB best (lower p99): "
                   + ", ".join(sorted(groups)))
    if underpowered:
        out.append("  N<3 per arm -- montauk reports the effect size but the "
                   "inference is underpowered; repeat runs accumulate here")
    return "\n".join(out)


# Arms whose capture completeness differs by more than this ratio are not
# comparable, and the report says so instead of printing the number. montauk's
# own capture-loss line already states the rule -- "compare arms only at like
# completeness" -- and the 2026-08-05 field report printed a headline "5.7x
# better" across arms at 8.6% and 15.4%, with the caveat sitting two sections
# below where nobody reads it beside the number it disqualifies. A caveat that
# does not suppress is a caveat that does not work.
_COMPLETENESS_RATIO_MAX = 1.25


def _build_comparison(
    meta: list[tuple[str | None, str | None, int | None, float | None]],
) -> str:
    """Turn the per-recording (family, scheduler, p99, completeness) tuples into
    the headline the report exists to give: EEVDF vs PANDEMONIUM p99, with the
    ratio. Only traced families (those with a p99) appear. lower p99 is better,
    so ratio = EEVDF / PANDEMONIUM.

    EVERY traced family with a baseline is reported, wins and losses alike.
    _COMPARE_ORDER fixes the ORDER of the ones it names and nothing more -- as a
    membership test it silently withheld regressions, which is how the field
    report's cold-wake-starve loss (EEVDF 20us against PANDEMONIUM 104us) reached
    us in the body of the report while the summary above it showed four straight
    wins. A summary that structurally cannot show a regression is not a summary."""
    fam_p99: dict[str, dict[str, int]] = {}
    fam_comp: dict[str, dict[str, float]] = {}
    for fam, sched, p99, comp in meta:
        if fam and sched and p99 is not None:
            fam_p99.setdefault(fam, {})[sched] = p99
            if comp is not None:
                fam_comp.setdefault(fam, {})[sched] = comp
    if not fam_p99:
        return ""
    # Named families first in their curated order, then everything else that has
    # measurements, so a new workload appears without being added to a list.
    ordered = [f for f in _COMPARE_ORDER if f in fam_p99]
    ordered += sorted(f for f in fam_p99 if f not in _COMPARE_ORDER)
    out = ["COMPARISON  (p99 wake2run, lower is better)"]
    for fam in ordered:
        m = fam_p99[fam]
        base = m.get("EEVDF")
        comps = fam_comp.get(fam, {})
        parts = []
        for sched in ("PANDEMONIUM-BPF", "PANDEMONIUM-ADAPTIVE", "PANDEMONIUM"):
            if sched not in m:
                continue
            v = m[sched]
            # Suppress the RATIO, never the measurement: both arms' numbers stay
            # visible, only the comparison between them is withheld.
            skew = None
            bc, vc = comps.get("EEVDF"), comps.get(sched)
            if bc and vc and min(bc, vc) > 0:
                r = max(bc, vc) / min(bc, vc)
                if r > _COMPLETENESS_RATIO_MAX:
                    skew = r
            if skew is not None:
                parts.append(f"{_sched_short(sched)} {v}us (ratio withheld -- "
                             f"arm completeness differs {skew:.1f}x)")
            elif base and v > 0:
                ratio = base / v
                verdict = (f"{ratio:.1f}x better" if ratio >= 1
                           else f"{1 / ratio:.1f}x worse")
                parts.append(f"{_sched_short(sched)} {v}us ({verdict})")
            else:
                parts.append(f"{_sched_short(sched)} {v}us")
        base_str = f"EEVDF {base}us" if base is not None else "EEVDF n/a"
        out.append(f"  {fam:18} {base_str} -> " + ", ".join(parts))
    return "\n".join(out) if len(out) > 1 else ""


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
    def p50(v: list) -> float:
        return median(v) if v else 0.0
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
    """Surface montauk's storm verdict into the report. trace_storm_cycle writes
    storm-*.storm (the `montauk --analyze --report storm` output) to LOG_DIR; the newest
    is this run's. The storm is the one failure the warm/controlled benches never
    trigger, so the shareable report carries montauk's verdict -- storm% and the
    REAL-IPI-vs-IDLE-churn classification, straight from the trace."""
    reps = sorted(LOG_DIR.glob("storm-*.storm"), key=lambda p: p.stat().st_mtime)
    if not reps:
        return ""
    try:
        text = reps[-1].read_text()
    except OSError:
        return ""
    # Absence is reported, never swallowed (montauk v7.17.0 captured-bit): an
    # empty artifact (pre-7.17.0 fossil) or a captured: 0 header means the storm
    # surface was NOT measured -- say so instead of dropping the section, or a
    # reader concludes storm-clean from what was never observed.
    if not text.strip() or text.startswith("captured: 0"):
        return ("STORM  (cpu_release flood, powersave)\n"
                "  not measured: storm probes off (MONTAUK_SCX_STORM gated) -- "
                "absence, not a zero")
    verdict = next((ln.strip() for ln in text.splitlines()
                    if ln.strip().startswith("VERDICT")), "")
    if not verdict:
        return ""
    body = verdict[len("VERDICT:"):].strip() if verdict.startswith("VERDICT:") else verdict
    return ("STORM  (cpu_release flood, powersave -- lower storm% is better)\n"
            f"  {body}")


def build_report(recs: list[Path], tag: str, stamp: str,
                 dmesg: DmesgMonitor, cleanroom: dict) -> Path:
    """Assemble the shareable report from montauk's digest of each recording."""
    ver = get_version()
    git = get_git_info()
    dirty = "-dirty" if git.get("dirty") else ""
    # Header matches the suite convention: <name> v<ver> [<commit><dirty>] [<mode>]
    # then a meta line; no decorative separators anywhere.
    lines = [f"PRISM v{ver} [{git['commit']}{dirty}] [{tag}]",
             f"captured: {stamp}",
             ""]
    analyze = _analyze_argv()
    # First pass: digest each recording, hoist the machine-wide blocks once, and
    # collect each workload's consolidated body + (family, scheduler, p99) so the
    # comparison can lead the report. The COMPARISON section is the headline -- it
    # turns 19 isolated blocks into the EEVDF-vs-PANDEMONIUM answer.
    stab_text = ""
    sys_text = ""
    workloads = []   # (label, body)
    meta = []        # (family, scheduler, p99) for COMPARISON
    pop = []         # (workload cell, scheduler, p99) for POPULATION
    for d in recs:
        write_stability_markers(d, dmesg, cleanroom)
        env, text = digest_envelope(analyze, d)
        label = _clean_label(d.name)
        fam, sched = _parse_label_meta(label)
        if env is None:
            # No envelope: carry montauk's own text digest verbatim rather than
            # scrape it, and contribute no p99 to the comparison (an unparsed
            # number is not a number).
            meta.append((fam, sched, None, None))
            pop.append((_workload_cell(label, sched), sched, None))
            workloads.append((label, text.strip()))
            continue
        if not stab_text:
            stab_text = _render_stability(env)
        if not sys_text:
            sys_text = _render_system(env)
        p99 = _envelope_p99(env)
        # Completeness travels with the p99 so COMPARISON can refuse a ratio
        # across unlike captures. Absent means UNKNOWN, never proven-clean, so a
        # missing value never satisfies the like-completeness test by default.
        meta.append((fam, sched, p99,
                     (env.get("digest") or {}).get("capture_completeness")))
        pop.append((_workload_cell(label, sched), sched, p99))
        cbody = _render_body(env)
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
    # The ratio above is two point estimates; this is montauk's verdict on
    # whether the difference survives an effect size, a permutation test and a
    # power check across every run recorded on this box.
    population = _build_population(analyze, write_population_prom(pop, stamp))
    if population:
        lines += [population, ""]
    storm = _storm_section()
    if storm:
        lines += [storm, ""]
    for label, body in workloads:
        lines.append("")
        lines.append(f"WORKLOAD {label}")
        lines.append(body if body.strip() else "  (no analyzable data)")
    # Fallback only if the front-and-center stability block did not render (e.g.
    # an older montauk without scx_stability_block): never lose a crash.
    if dmesg.crashed and not stab_text:
        lines += ["", f"SCHEDULER CRASH: {dmesg.crash_msg}"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"prism-report-{ver}-{stamp}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def dev_widths(cores: str | None, max_cpus: int) -> list[int]:
    """--cores as the widths prism itself makes for a workload that does not
    sweep: Ascending, each once, from 2 CPUs to the box's own count, a width
    outside that left out. Empty for no --cores, which leaves the box as it
    stands. ValueError names what is not a number."""
    if not cores:
        return []
    widths = set()
    for part in cores.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"--cores {part}: a width is a CPU count")
        if 2 <= int(part) <= max_cpus:
            widths.add(int(part))
        else:
            # AS THE SWEEPING BENCHES DO: A WIDTH THE BOX CANNOT BE IS LEFT OUT,
            # SO ONE LIST SERVES BOXES OF EVERY SIZE.
            log_warn(f"--cores {part}: outside 2 to {max_cpus} on this box, left out")
    return sorted(widths)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PRISM: shine a system under load through it and read the "
                    "diagnostic spectrum. Default (no flag) = the end-user pass "
                    "(a short profile, one shareable report). --dev <name> runs "
                    "one sustained validation; "
                    "--dev all the full sweep; --list shows the workloads.")
    ap.add_argument("--workload", metavar="CMD",
                    help="trace YOUR isolated workload: run CMD under the live "
                         "scheduler and report on it (instead of the fixed profile)")
    ap.add_argument("--attach", metavar="COMM",
                    help="trace an already-running program by comm name for "
                         "--duration seconds (instead of launching one)")
    ap.add_argument("--duration", type=float, default=0.0, metavar="S",
                    help="cap a --workload run / set --attach window (seconds); "
                         "default: run until the workload exits")
    ap.add_argument("--iterations", type=int, default=None, metavar="N",
                    help="run every arm N times, with one report over all N: the "
                         "profile, every --dev workload and a --workload/--attach "
                         "capture alike (default: 1, and under --dev each "
                         "workload's own)")
    ap.add_argument("--cores", metavar="LIST", default=None,
                    help="comma-separated core widths for any --dev workload, "
                         "e.g. --cores 4,8. scale, ipc, contention, pcpu and scx "
                         "sweep the list themselves, one report over all of "
                         "it (default: their own auto sweep, 2,4,8,...,max). "
                         "Every other workload runs whole once at each width, "
                         "prism taking the CPUs offline around it (default: the "
                         "box as it stands). No effect on the profile pass")
    ap.add_argument("--ultra", action="store_true",
                    help="trace the width-specific faults (burst-starvation, "
                         "sojourn-pressure) at EVERY core width (2,4,8,...,max) "
                         "instead of native width only. Surfaces width-cadence "
                         "bugs but writes one trace dir per width per scheduler -- "
                         "many files, sizes from KB to hundreds of MB")
    ap.add_argument("--dev", nargs="*", default=None, metavar="WORKLOAD",
                    help="run ONE OR MORE sustained dev validations by name (fork-thread, "
                         "pcpu, contention, scale, ipc, power, frame, scx, helicoprion), "
                         "or 'all' for the full sweep. Bare --dev lists them. "
                         "A CHILD'S OWN FLAGS TRAVEL AFTER `--`: "
                         "`--dev fork-thread -- --modes thread` reaches that child, "
                         "where flags before `--` are prism's.")
    ap.add_argument("--list", action="store_true",
                    help="list the PRISM workloads (default + dev tiers) and exit")
    ap.add_argument("--trace", action="store_true",
                    help="record montauk captures. Nothing else traces: a --dev "
                         "workload runs uncaptured without it. The end-user "
                         "report implies its own capture")
    add_field_flags(ap, ". Propagates to the default profile and every --dev workload")
    # CHILD PASSTHROUGH. Everything after a bare `--` goes to the --dev child
    # verbatim. The dispatcher does NOT learn any child's private flags: The
    # uniform flags pass unconditionally, and `--` lets an operator drive a
    # child that has modes of its own.
    argv = sys.argv[1:]
    passthrough = []
    if "--" in argv:
        cut = argv.index("--")
        argv, passthrough = argv[:cut], argv[cut + 1:]
    args = ap.parse_args(argv)
    args.passthrough = passthrough

    # --schedulers and --all-scx are two ways to pick the SAME thing (the
    # external field); running both is ambiguous. Warn with a usage hint rather
    # than silently favoring one.
    if args.all_scx and args.schedulers:
        log_warn("--schedulers and --all-scx cannot be used together -- pick one:")
        log_warn("  --schedulers scx_rusty,scx_lavd   compare against a chosen set")
        log_warn("  --all-scx                          compare against the full field")
        return 1
    if args.only and (args.schedulers or args.all_scx):
        log_warn("--only names the whole field by itself -- drop --schedulers "
                 "and --all-scx beside it")
        return 1

    # PRISM dev tier: the sustained validations, gated behind --dev. PRISM is the one
    # bench entry -- it dispatches each workload to its implementer script DIRECTLY
    # (pandemonium-tests.py / prism-*.py), never back through pandemonium.py.
    _pt = str(TESTS_DIR / "pandemonium-tests.py")
    PRISM_DEV = {
        "fork-thread": [str(TESTS_DIR / "prism-fork-thread.py")],
        "pcpu":        [_pt, "prism-pcpu"],
        "contention":  [_pt, "prism-contention"],
        "scale":       [_pt, "prism-scale"],
        "ipc":         [_pt, "prism-scale", "--cells", "ipc"],
        "power":       [str(TESTS_DIR / "prism-power.py")],
        "frame":       [str(TESTS_DIR / "prism-frame.py")],
        "scx":         [_pt, "prism-scx"],
        "helicoprion": [str(TESTS_DIR / "prism-helicoprion.py")],
    }
    # AN OUTSIDE SUITE prism RUNS AND DOES NOT CARRY. NAMED, IT IS OFFERED FROM
    # ITS PACKAGE WHERE IT IS MISSING; UNDER `all` IT RUNS WHERE IT IS INSTALLED.
    PRISM_DEV_OUTSIDE = {"helicoprion": "helicoprion"}
    # THE --dev CONTRACT: Every implementer takes the field flags, --iterations,
    # --cores and --trace, and the dispatcher passes them through
    # unconditionally, with no table of which child takes which. One set
    # remains because it names a difference in what the workload IS, not in
    # what flags it takes.
    # WIDTH IS PRISM'S. These own a width loop and write ONE report across the
    # widths they are given, so --cores is theirs to sweep. Every other
    # workload runs at the width the box is at, and for those prism makes the
    # width: It takes the CPUs offline, runs the workload whole, and brings
    # them back, once for each width listed.
    PRISM_DEV_SWEEPS = {"scale", "ipc", "pcpu", "contention", "scx"}
    if args.list or args.dev == []:
        log_info("PRISM -- shine the system through it, read the spectrum:")
        log_info("  (no flag)         the end-user pass: short profile, one report")
        for n in PRISM_DEV:
            log_info(f"  --dev {n:<13} sustained validation (dev-gated)")
        log_info("  --dev all         run every sustained validation in sequence")
        return 0
    if args.dev:
        names = list(PRISM_DEV) if "all" in args.dev else args.dev
        if "all" in args.dev:
            for n, prog in PRISM_DEV_OUTSIDE.items():
                if shutil.which(prog) is None:
                    log_info(f"PRISM --dev all: {n} is not installed and is left "
                             f"out; --dev {n} offers its package")
                    names.remove(n)
        unknown = [n for n in names if n not in PRISM_DEV]
        if unknown:
            log_error(f"unknown --dev workload: {', '.join(unknown)} (try --list)")
            return 2
        # CAUGHT RATHER THAN SWALLOWED. prism's own --workload and --attach trace
        # a command in place of the fixed profile and never reach a --dev child,
        # so passed with --dev they would look accepted and do nothing.
        # Child-private flags travel after `--`.
        if args.workload or args.attach:
            flag = "--workload" if args.workload else "--attach"
            log_error(f"{flag} is prism's own (trace a command you name) and "
                      f"does not reach a --dev child.")
            log_error(f"  a child's own flags travel after `--`:")
            log_error(f"    ./pandemonium.py prism --dev {names[0]} -- <its flags>")
            child = " ".join(os.path.relpath(a, REPO_ROOT) if os.sep in a else a
                             for a in PRISM_DEV[names[0]])
            log_error(f"  the child's own flags: ./{child} --help")
            return 2
        # SELF-ELEVATE when the run will capture, which is --trace and nothing
        # else. The montauk capture needs root (eBPF attach + a writable
        # /tmp/pandemonium), so re-exec under sudo rather than make the user type
        # it -- ONE standard across every --dev name, no child left to error out
        # with its own "re-run under sudo". A captureless --dev run stays
        # pre-elevation, no re-exec.
        if os.geteuid() != 0 and args.trace:
            os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
        # Canonical exit guard for the --dev path (the profile path ejects in main's
        # except/finally). On Ctrl+C: eject the benched scheduler and re-online every
        # CPU the width loop offlined -- an interrupt never leaves the box stuck on a
        # CPU subset under PANDEMONIUM. The child bench installs the same guard; both
        # are idempotent.
        install_exit_guard(eject=True)
        max_cpus = get_possible_cpus()
        try:
            widths = dev_widths(args.cores, max_cpus)
        except ValueError as e:
            log_error(str(e))
            return 2
        rc = 0
        for n in names:
            log_info(f"PRISM --dev: {n}")
            # THE WIDTHS THIS WORKLOAD IS RUN AT BY PRISM: ITS OWN SWEEP (None,
            # ONCE, --cores HANDED ON) OR ONE RUN A LISTED WIDTH.
            own_sweep = n in PRISM_DEV_SWEEPS or not widths
            for width in ([None] if own_sweep else widths):
                # Pre-flight stop, exactly as run_profile does before every bench: a
                # scheduler left loaded would have the implementer's width loop offline
                # cores under it (hotplug) and wedge on its bpffs-pin teardown (libbpf
                # statfs -ENOENT). --dev is pre-elevation; the stop sudo's as needed.
                clear_loaded_scheduler(10.0)
                # THE WIDTH, MADE AFTER THE STOP SO NO SCHEDULER IS LIVE ACROSS
                # THE HOTPLUG, AND UNDONE AS SOON AS THE WORKLOAD RETURNS.
                if width is not None:
                    restore_all_cpus(max_cpus)
                    if width < max_cpus:
                        log_info(f"PRISM --dev {n}: restricting to {width} CPUs via hotplug")
                        if not restrict_cpus(width, max_cpus):
                            log_error(f"CPU hotplug failed for {width} cores -- skipping")
                            restore_all_cpus(max_cpus)
                            rc = rc or 1
                            continue
                        time.sleep(0.5)
                dev_cmd = list(PRISM_DEV[n])
                # The standard flags pass straight through: Every implementer
                # takes them, so there is no dispatcher membership table to fall
                # out of date. --iterations passes only when given, so a
                # workload's own default count stands otherwise.
                dev_cmd += _field_flags(args.schedulers, args.all_scx, args.only,
                                        args.pandemonium, args.arm, args.disturb)
                if args.iterations:
                    dev_cmd += ["--iterations", str(max(1, args.iterations))]
                if args.cores:
                    dev_cmd += ["--cores", args.cores if width is None else str(width)]
                if args.trace:
                    dev_cmd.append("--trace")
                # Child-private flags last, so a child mode can override a uniform
                # default it also accepts.
                dev_cmd += args.passthrough
                try:
                    rc = subprocess.run([sys.executable, *dev_cmd]).returncode or rc
                finally:
                    if width is not None:
                        restore_all_cpus(max_cpus)
                        time.sleep(0.5)
        return rc

    if os.geteuid() != 0:
        # SELF-ELEVATE: the report flow needs root end-to-end (montauk eBPF
        # attach + sched_ext load + RAPL/PMU), so re-exec under sudo rather than
        # make the user type it -- matches prism-fork-thread and the rest of the
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
    elif args.only:
        tag = f"sched=only:{args.only}"
    elif args.schedulers:
        tag = f"sched=+{args.schedulers}"
    else:
        tag = "sched=eevdf+pandemonium"

    ver = get_version()
    git = get_git_info()
    dirty = "-dirty" if git.get("dirty") else ""
    log_info(f"PRISM v{ver} [{git['commit']}{dirty}] [{tag}]")
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

    iters = max(1, args.iterations or 1)
    # Clean-room state at start of the session -- leads the digest so a NOISY
    # box (high load / long uptime) is flagged before anyone trusts the tails.
    cleanroom = capture_cleanroom()
    if cleanroom["verdict"] == "NOISY":
        log_warn(f"CLEAN-ROOM: NOISY ({cleanroom['detail']}). Single-run tails "
                 "are background-contaminated; a reboot gives trustworthy numbers.")
    report_built = False
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dmesg = DmesgMonitor()
        if trace_mode:
            # --iterations N: N captures of the workload, one report over all N.
            # The index suffix keeps the recordings distinct even for sub-second
            # runs in the same wall-clock second.
            recs = []
            for it in range(iters):
                if iters > 1:
                    print()
                    log_info(f"run {it + 1}/{iters}")
                rec = run_workload_trace(args.workload, args.attach, args.duration,
                                         f"{stamp}-{it + 1}" if iters > 1 else stamp)
                if not rec or not rec.is_dir():
                    log_error("no report: the workload was not traced")
                    return 1
                recs.append(rec)
            dmesg.check()
        else:
            recs = run_profile(args.schedulers, args.all_scx, args.ultra,
                               args.pandemonium, iters, only=args.only,
                               arms=args.arm, disturb=args.disturb)
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
        log_info("Share this file -- it is small, redacted, and self-contained.")
        report_built = True
    except KeyboardInterrupt:
        # Respect Ctrl+C: the profile activates its own schedulers, so eject
        # whatever is registered and leave the box on stock EEVDF. Trace mode
        # rides the user's CURRENT scheduler -- never eject that.
        print()
        # Re-online every CPU BEFORE the eject -- disabling sched_ext while a CPU
        # is offline deadlocks cpu_hotplug_lock on 7.1.1+ (silent box freeze). The
        # finally below restores as a backstop; doing it here keeps the eject from
        # ever running on a hotplugged-down CPU set.
        try:
            restore_all_cpus(get_possible_cpus())
        except Exception:
            pass
        eject_scheduler(trace_mode, interrupted=True)
        raise
    finally:
        # THE TEARDOWN IS CONDITIONAL ON A REPORT EXISTING, AND finally WAS THE WRONG
        # CONSTRUCT FOR IT. Removing montauk on the failure path takes the one tool
        # that can assemble a report from the captures that survived -- the run's
        # expensive half is on disk in TRACE_DIR, and an interrupt or a crash in
        # assembly used to delete the analyzer on its way out, turning a retry into
        # a clone-and-rebuild. Remove it when the report is written; otherwise keep
        # it and name the command that finishes the job.
        if report_built:
            remove_montauk_if_ours(installed_by_us, uninstall_after)
        elif installed_by_us and uninstall_after:
            log_warn(f"no report was written -- KEEPING montauk; the captures in "
                     f"{TRACE_DIR} are intact and assembling them needs it.")
        # BACKSTOP: the scale/contention arms offline CPUs via hotplug; the bench subprocess
        # restores them on its own SIGINT (_cleanup_on_exit), but if it was killed before that
        # could run, restore here so the box is NEVER left stuck on a subset of cores. Idempotent.
        try:
            restore_all_cpus(get_possible_cpus())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.interrupted()
        sys.exit(130)
