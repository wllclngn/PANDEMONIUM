#!/usr/bin/env python3
"""
PANDEMONIUM build/run/install manager.

Usage:
    ./pandemonium.py prism-sys         Live system telemetry capture (Ctrl+C to stop)
    ./pandemonium.py prism       PRISM: one-command shareable report -- specs + ranked misbehaving items + metrics (redacted). --dev <name> sustained validations; --list / --help
    ./pandemonium.py prism --workload "<cmd>"   Same report, captured on YOUR isolated workload (or --attach <comm>)
    ./pandemonium.py install      Build + install + activate systemd service
    ./pandemonium.py clean        Wipe build artifacts
    ./pandemonium.py status       Show build/install status
    ./pandemonium.py rebuild      Force clean rebuild
"""

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from pandemonium_common import (
    SCRIPT_DIR, TARGET_DIR, LOG_DIR, BINARY,
    get_version, log_info, log_warn, log_error,
    run_cmd, run_cmd_capture,
    has_root_owned_files, check_sources_changed, build, scx_profiles,
)


# CONFIGURATION (install-specific)

INSTALL_PATH = Path("/usr/local/bin/pandemonium")

def _service_unit(verbose: bool = False) -> str:
    exec_line = "/usr/local/bin/pandemonium"
    if verbose:
        exec_line += " --verbose"
    return f"""\
[Unit]
Description=PANDEMONIUM adaptive Linux scheduler (sched_ext)
After=multi-user.target
ConditionPathExists=/sys/kernel/sched_ext

[Service]
Type=simple
ExecStart={exec_line}
Restart=on-failure
RestartSec=5
StateDirectory=pandemonium
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""

SERVICE_PATH = Path("/etc/systemd/system/pandemonium.service")


# COMMANDS

def cmd_install(verbose: bool = False) -> int:
    """Build, install binary, create systemd service, and start (once)."""
    if not build(force=True):
        return 1

    print()
    log_info(f"Installing binary: {INSTALL_PATH}")
    subprocess.run(["sudo", "rm", "-f", str(INSTALL_PATH)],
                   capture_output=True)
    ret = subprocess.run(
        ["sudo", "cp", str(BINARY), str(INSTALL_PATH)]
    ).returncode
    if ret != 0:
        log_error("Failed to copy binary (sudo required)")
        return ret

    size = BINARY.stat().st_size // 1024
    log_info(f"Installed {INSTALL_PATH} ({size} KB)")

    print()
    log_info("Installing systemd service...")
    unit = _service_unit(verbose=verbose)
    proc = subprocess.Popen(
        ["sudo", "tee", str(SERVICE_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    proc.communicate(input=unit.encode())
    if proc.returncode != 0:
        log_error("Failed to write service file")
        return proc.returncode
    log_info(f"Created {SERVICE_PATH}")

    ret = subprocess.run(["sudo", "systemctl", "daemon-reload"]).returncode
    if ret != 0:
        log_error("systemctl daemon-reload failed")
        return ret

    offer_scx_profile()

    print()
    log_info("PANDEMONIUM is installed")
    log_info("To start:")
    log_info("  sudo systemctl start pandemonium")
    log_info("To start on boot (after confirming it works on your hardware):")
    log_info("  sudo systemctl enable pandemonium")
    log_info("To check status:")
    log_info("  systemctl status pandemonium")
    return 0


def _scx_pandemonium_installed() -> bool:
    """scx's own scx_pandemonium on systemd's default search path, which is
    where scx's service finds a catalog's scheduler."""
    rc, out, _ = run_cmd_capture(["systemd-path", "search-binaries-default"])
    if rc != 0:
        return False
    return any((Path(d) / "scx_pandemonium").exists()
               for d in out.strip().split(":") if d)


def offer_scx_profile() -> None:
    """scxcli and scxtui start schedulers from scx's catalog (version 2), and
    scx_pandemonium is listed there and starts the adaptive arm. A profile
    with options comes only from an administrator's /etc/scx/catalog.json, so
    where scxcli and scx's scx_pandemonium are installed, offer the profiles in
    catalog/profiles.json there."""
    if not shutil.which("scxcli"):
        return
    if not _scx_pandemonium_installed():
        log_info("scxcli is installed but scx's scx_pandemonium is not; "
                 "no catalog profile")
        return
    try:
        profiles = scx_profiles()
    except (OSError, ValueError) as e:
        log_warn(f"no catalog profile: {e}")
        return
    rc, out, err = run_cmd_capture(["scxcli", "catalog", "show"])
    if rc != 0:
        log_warn(f"scxcli catalog show failed: {err.strip()}")
        return
    try:
        catalog = json.loads(out)
    except ValueError:
        log_warn("scxcli catalog show did not print JSON; no catalog profile")
        return
    if catalog.get("version") != 2:
        log_info(f"scx's catalog is version {catalog.get('version')}, before "
                 "profiles; no catalog profile")
        return
    have = {p.get("id") for p in catalog.get("recommended", [])}
    missing = [p for p in profiles if p["id"] not in have]
    if not missing:
        log_info("scx's catalog already carries "
                 + ", ".join(p["id"] for p in profiles))
        return
    if not sys.stdin.isatty():
        return
    print()
    for p in missing:
        log_info(f"scx's catalog can carry profile '{p['id']}' ({p['label']}: "
                 f"{p['sched']} {' '.join(p.get('args', []))}), so "
                 f"`scxcli start {p['id']}` and scxtui start it by name.")
    log_info("The catalog then lives in /etc/scx/catalog.json and stops following "
             "scx's updates to the shipped one until `sudo scxcli catalog reset`.")
    if input("Add the profile? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    catalog["recommended"].extend(missing)
    r = subprocess.run(["sudo", "scxcli", "catalog", "save"],
                       input=json.dumps(catalog, indent=2) + "\n",
                       text=True, capture_output=True)
    if r.returncode != 0:
        log_error(f"scxcli catalog save failed: {r.stderr.strip()}")
        return
    for p in missing:
        log_info(f"Added profile {p['id']}: scxcli start {p['id']}")


def cmd_clean() -> int:
    """Wipe build artifacts."""
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


# MAIN

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 0

    cmd = sys.argv[1]
    log_info(f"PANDEMONIUMv{get_version()}: {cmd} SELECTED")

    if cmd == "install":
        return cmd_install(verbose="--verbose" in sys.argv[2:])
    elif cmd == "clean":
        return cmd_clean()
    elif cmd == "status":
        return cmd_status()
    elif cmd == "rebuild":
        return 0 if build(force=True) else 1
    elif cmd == "prism-sys":
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "tests" / "pandemonium-tests.py"),
             "prism-sys"] + sys.argv[2:],
            cwd=SCRIPT_DIR,
        ).returncode
    elif cmd == "prism":
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "tests" / "prism.py")]
            + sys.argv[2:],
            cwd=SCRIPT_DIR,
        ).returncode
    else:
        log_error(f"Unknown command: {cmd}")
        print()
        print(__doc__.strip())
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
