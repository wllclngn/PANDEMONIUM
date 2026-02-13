#!/usr/bin/env python3
"""
PANDEMONIUM build/run/install manager.

Usage:
    ./pandemonium.py start                    Build + run scheduler
    ./pandemonium.py test-scale               Build + A/B scaling benchmark
    ./pandemonium.py bench --mode contention  Build + benchmark
    ./pandemonium.py install                  Build + symlink to /usr/local/bin
    ./pandemonium.py clean                    Wipe build artifacts
    ./pandemonium.py status                   Show build/install status
    ./pandemonium.py <any subcommand>         Build + forward to binary
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# =============================================================================
# CONFIGURATION
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
TARGET_DIR = Path("/tmp/pandemonium-build")
LOG_DIR = Path("/tmp/pandemonium")
BINARY = TARGET_DIR / "release" / "pandemonium"
INSTALL_PATH = Path("/usr/local/bin/pandemonium")

SOURCE_PATTERNS = [
    "src/**/*.rs", "src/**/*.c", "src/**/*.h",
    "Cargo.toml", "build.rs",
]


# =============================================================================
# LOGGING (mirrors arch-update.py / ABRAXAS)
# =============================================================================

def _timestamp() -> str:
    """Get current timestamp in [HH:MM:SS] format."""
    return datetime.now().strftime("[%H:%M:%S]")


def log_info(msg: str) -> None:
    print(f"{_timestamp()} [INFO]   {msg}")


def log_warn(msg: str) -> None:
    print(f"{_timestamp()} [WARN]   {msg}")


def log_error(msg: str) -> None:
    print(f"{_timestamp()} [ERROR]  {msg}")


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


# =============================================================================
# BUILD
# =============================================================================

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


def fix_ownership():
    """After sudo cargo test, chown build dir back to current user."""
    uid = os.getuid()
    gid = os.getgid()
    log_info(f"Fixing build dir ownership to {uid}:{gid}...")
    subprocess.run(
        ["sudo", "chown", "-R", f"{uid}:{gid}", str(TARGET_DIR)],
        capture_output=True,
    )


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


def build(force: bool = False) -> bool:
    """Build PANDEMONIUM release binary. Returns True on success."""
    log_info("PANDEMONIUM BUILD")

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
            log_info(f"Source changes detected ({len(changed)} file(s)):")
            for f in changed[:5]:
                print(f"  {f}")
            if len(changed) > 5:
                log_info(f"... and {len(changed) - 5} more")
        else:
            log_info("No existing binary, full build required")

    print()
    log_info("Build configuration:")
    print(f"  Source:  {SCRIPT_DIR}")
    print(f"  Target:  {TARGET_DIR}")
    print(f"  Binary:  {BINARY}")
    print()

    if force:
        log_info("Forced rebuild, cleaning package cache...")
        subprocess.run(
            ["cargo", "clean", "-p", "pandemonium"],
            env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
            cwd=str(SCRIPT_DIR),
            capture_output=True,
        )

    log_info("Building (release)...")
    ret = run_cmd(
        ["cargo", "build", "--release"],
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


def build_test(test_name: str) -> bool:
    """Build a test binary without running it. Returns True on success."""
    log_info(f"Building test binary: {test_name}...")
    ret = run_cmd(
        ["cargo", "test", "--release", "--test", test_name, "--no-run"],
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
        cwd=SCRIPT_DIR,
    )
    if ret != 0:
        log_error(f"Test build failed: {test_name}")
        return False
    log_info(f"Test binary ready: {test_name}")
    return True


# =============================================================================
# COMMANDS
# =============================================================================

def cmd_test_scale() -> int:
    """Build + run scaling benchmark directly via sudo cargo test."""
    if not build():
        return 1

    log_info("Ensuring test binary is built before sudo...")
    if not build_test("scale"):
        return 1

    print()
    log_info("PANDEMONIUM SCALING BENCHMARK")

    ncpus = os.cpu_count()
    if ncpus:
        log_info(f"System CPUs: {ncpus}")
    log_info(f"Log directory: {LOG_DIR}/")
    log_info("Running benchmark (requires root for sched_ext + CPU hotplug)...")

    print()
    ret = run_cmd(
        ["sudo", "-E",
         f"CARGO_TARGET_DIR={TARGET_DIR}",
         "cargo", "test",
         "--test", "scale", "--release",
         "--", "--ignored", "--test-threads=1", "--nocapture"],
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
        cwd=SCRIPT_DIR,
    )
    print()

    # sudo cargo test creates root-owned files -- fix ownership now
    # so the NEXT build doesn't need to nuke the entire dir
    fix_ownership()

    # Report log files
    if LOG_DIR.exists():
        logs = sorted(LOG_DIR.glob("scale-*.log"),
                      key=lambda p: p.stat().st_mtime)
        if logs:
            latest = logs[-1]
            size = latest.stat().st_size
            log_info(f"Latest log: {latest} ({size} bytes)")
        else:
            log_warn("No scale logs found in {LOG_DIR}")
    else:
        log_warn(f"Log directory does not exist: {LOG_DIR}")

    if ret == 0:
        log_info("Benchmark completed successfully")
    else:
        log_error(f"Benchmark exited with code {ret}")

    return ret


def cmd_install() -> int:
    """Build and symlink binary into PATH."""
    log_info("PANDEMONIUM INSTALL")

    if not build(force=True):
        return 1

    print()
    log_info(f"Creating symlink: {INSTALL_PATH} -> {BINARY}")
    result = subprocess.run(
        ["sudo", "ln", "-sf", str(BINARY), str(INSTALL_PATH)]
    )

    if result.returncode == 0:
        log_info("Install complete")
        log_info("Run 'pandemonium <command>' from anywhere")
    else:
        log_error("Install failed (sudo required)")
    return result.returncode


def cmd_clean() -> int:
    """Wipe build artifacts."""
    log_info("PANDEMONIUM CLEAN")

    if not TARGET_DIR.exists():
        log_info("Already clean, nothing to remove")
        return 0

    ret, out, _ = run_cmd_capture(["du", "-sh", str(TARGET_DIR)])
    if ret == 0:
        size = out.strip().split()[0]
        log_info(f"Build directory: {TARGET_DIR} ({size})")

    resp = input(f"REMOVE {TARGET_DIR}? [Y/N] ").strip().lower()
    if resp == "y":
        log_info("Removing build directory...")
        run_cmd(["sudo", "rm", "-rf", str(TARGET_DIR)])
        log_info("Clean complete")
    else:
        log_info("Aborted")

    return 0


def cmd_status() -> int:
    """Show build/install status."""
    log_info("PANDEMONIUM STATUS")
    print()

    if BINARY.exists():
        size = BINARY.stat().st_size // 1024
        mtime = datetime.fromtimestamp(BINARY.stat().st_mtime)
        print(f"  Binary:    {BINARY}")
        print(f"             {size} KB, built {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print(f"  Binary:    NOT BUILT")
    print()

    if INSTALL_PATH.is_symlink():
        target = INSTALL_PATH.resolve()
        print(f"  Install:   {INSTALL_PATH} -> {target}")
    elif INSTALL_PATH.exists():
        print(f"  Install:   {INSTALL_PATH} (not a symlink)")
    else:
        print(f"  Install:   NOT INSTALLED")
    print()

    root = has_root_owned_files()
    if root:
        print(f"  State:     ROOT-OWNED FILES PRESENT (run clean)")
    elif not BINARY.exists():
        print(f"  State:     NOT BUILT")
    else:
        print(f"  State:     OK")
    print()

    if BINARY.exists():
        changed = check_sources_changed()
        if changed and changed[0] != "(binary not found)":
            print(f"  Sources:   {len(changed)} file(s) changed since last build")
        else:
            print(f"  Sources:   Up to date")
        print()

    if LOG_DIR.exists():
        logs = sorted(LOG_DIR.glob("*.log"))
        print(f"  Logs:      {LOG_DIR}/ ({len(logs)} file(s))")
        if logs:
            latest = max(logs, key=lambda p: p.stat().st_mtime)
            print(f"             Latest: {latest.name}")
    else:
        print(f"  Logs:      {LOG_DIR}/ (not created yet)")
    print()

    return 0


def cmd_forward(args: list) -> int:
    """Build if needed, then forward all args to the binary."""
    if not build():
        return 1

    log_info(f"Forwarding: pandemonium {' '.join(args)}")
    print()
    os.execv(str(BINARY), [str(BINARY)] + args)


# =============================================================================
# MAIN
# =============================================================================

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 0

    cmd = sys.argv[1]
    log_info(f"PANDEMONIUM ({cmd})")

    if cmd == "install":
        return cmd_install()
    elif cmd == "clean":
        return cmd_clean()
    elif cmd == "status":
        return cmd_status()
    elif cmd == "rebuild":
        return 0 if build(force=True) else 1
    elif cmd == "test-scale":
        return cmd_test_scale()
    else:
        return cmd_forward(sys.argv[1:])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
