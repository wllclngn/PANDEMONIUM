#!/usr/bin/env python3
"""
PANDEMONIUM build/run/install manager.

Usage:
    ./pandemonium.py start                  Build + run scheduler
    ./pandemonium.py test-scale             Build + A/B scaling benchmark
    ./pandemonium.py bench --mode contention  Build + benchmark
    ./pandemonium.py install                Build + symlink to /usr/local/bin
    ./pandemonium.py clean                  Wipe build artifacts
    ./pandemonium.py <any subcommand>       Build + forward to binary
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


# =============================================================================
# CONFIGURATION
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
TARGET_DIR = Path("/tmp/pandemonium-build")
BINARY = TARGET_DIR / "release" / "pandemonium"
INSTALL_PATH = Path("/usr/local/bin/pandemonium")


# =============================================================================
# BUILD
# =============================================================================

def source_newer_than_binary() -> bool:
    """Check if any source file is newer than the compiled binary."""
    if not BINARY.exists():
        return True
    bin_mtime = BINARY.stat().st_mtime
    for pattern in ["src/**/*.rs", "src/**/*.c", "src/**/*.h",
                    "Cargo.toml", "build.rs"]:
        for src in SCRIPT_DIR.glob(pattern):
            if src.stat().st_mtime > bin_mtime:
                return True
    return False


def build(force: bool = False) -> bool:
    """Build PANDEMONIUM. Returns True on success."""
    if not force and not source_newer_than_binary():
        return True

    if force:
        print("CLEAN BUILD (PANDEMONIUM)...")
        subprocess.run(
            ["cargo", "clean", "-p", "pandemonium"],
            env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
            cwd=str(SCRIPT_DIR),
            capture_output=True,
        )
    else:
        print("BUILDING PANDEMONIUM (RELEASE)...")

    result = subprocess.run(
        ["cargo", "build", "--release"],
        env={**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)},
        cwd=str(SCRIPT_DIR),
    )

    if result.returncode != 0:
        print("BUILD FAILED.")
        return False

    if BINARY.exists():
        size = BINARY.stat().st_size // 1024
        print(f"BUILD COMPLETE. BINARY: {BINARY} ({size} KB)")
    return True


# =============================================================================
# COMMANDS
# =============================================================================

def cmd_install() -> int:
    """Build and symlink binary into PATH."""
    if not build(force=True):
        return 1

    print(f"\nINSTALLING: {INSTALL_PATH} -> {BINARY}")
    result = subprocess.run(
        ["sudo", "ln", "-sf", str(BINARY), str(INSTALL_PATH)]
    )
    if result.returncode == 0:
        print(f"INSTALLED. Run 'pandemonium <command>' from anywhere.")
    else:
        print("INSTALL FAILED (sudo required).")
    return result.returncode


def cmd_clean() -> int:
    """Wipe build artifacts."""
    if TARGET_DIR.exists():
        print(f"REMOVING {TARGET_DIR}...")
        subprocess.run(["rm", "-rf", str(TARGET_DIR)])
        print("CLEAN.")
    else:
        print("ALREADY CLEAN.")
    return 0


def cmd_status() -> int:
    """Show build/install status."""
    print("PANDEMONIUM STATUS")
    print()

    if BINARY.exists():
        size = BINARY.stat().st_size // 1024
        print(f"  Binary:  {BINARY} ({size} KB)")
    else:
        print(f"  Binary:  NOT BUILT")

    if INSTALL_PATH.is_symlink():
        target = INSTALL_PATH.resolve()
        print(f"  Install: {INSTALL_PATH} -> {target}")
    elif INSTALL_PATH.exists():
        print(f"  Install: {INSTALL_PATH} (not a symlink)")
    else:
        print(f"  Install: NOT INSTALLED")

    if source_newer_than_binary():
        print(f"  State:   SOURCE NEWER THAN BINARY (rebuild needed)")
    elif BINARY.exists():
        print(f"  State:   UP TO DATE")

    print()
    return 0


def cmd_forward(args: list) -> int:
    """Build if needed, then forward all args to the binary."""
    if not build():
        return 1

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

    if cmd == "install":
        return cmd_install()
    elif cmd == "clean":
        return cmd_clean()
    elif cmd == "status":
        return cmd_status()
    elif cmd == "rebuild":
        if not build(force=True):
            return 1
        return 0
    else:
        # Everything else: build + forward to binary
        return cmd_forward(sys.argv[1:])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nINTERRUPTED.")
        sys.exit(130)
