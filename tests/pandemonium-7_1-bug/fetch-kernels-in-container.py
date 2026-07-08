#!/usr/bin/env python3
# fetch-kernels-in-container.py -- runs inside the pandemonium-runner
# container. Syncs the CachyOS repo baked into the image (never the host's
# pacman.conf/keyring), downloads the requested package specs, extracts
# vmlinuz into /out keyed vmlinuz-<real-module-dir-name> (the module
# directory name read out of the package itself -- an -rc package's pkgver
# does not string-transform into its real uname -r, so this is discovered,
# never guessed), and keeps the raw package under /out/pkgs so a later
# xfs.ko extraction has a source that does not depend on the host's pacman
# cache (which is never populated by this harness).
#
# Config via env:
#   PAND_FETCH_PKGS   comma-separated pacman targets, each "pkg" or
#                      "repo/pkg" (default: linux-cachyos,
#                      cachyos-v3/linux-cachyos, cachyos-v3/linux-cachyos-rc)

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path("/out")
PKGCACHE = Path("/var/cache/pacman/pkg")
DEFAULT_PKGS = "linux-cachyos,cachyos-v3/linux-cachyos,cachyos-v3/linux-cachyos-rc"


def log(msg: str, level: str = "INFO") -> None:
    print(f"[fetch-kernels] [{level}] {msg}", flush=True)


def resolve_version(spec: str) -> str | None:
    r = subprocess.run(["pacman", "-Si", spec], capture_output=True, text=True)
    m = re.search(r"^Version\s*:\s*(\S+)", r.stdout, re.MULTILINE)
    return m.group(1) if m else None


def download(spec: str) -> tuple[bool, str]:
    r = subprocess.run(["pacman", "-Sw", "--noconfirm", spec],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1:]


def find_package_file(pkgname: str, version: str, search: Path) -> Path | None:
    want = f"{pkgname}-{version}-"
    for f in search.glob(f"{pkgname}-*.pkg.tar.zst"):
        if f.name.startswith(want):
            return f
    return None


def find_moddir(pkgfile: Path) -> str | None:
    # The real module directory name (the kernel's own uname -r) does not
    # reliably derive from the pacman version string: an -rc package's
    # pkgver "7.2.rc1-4" unpacks to modules/7.2.0-rc1-4-cachyos-rc, not
    # "7.2.rc1-4-cachyos". List the archive and read the real name instead
    # of guessing a transform per package flavor.
    r = subprocess.run(["bsdtar", "-tf", str(pkgfile)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        m = re.match(r"usr/lib/modules/([^/]+)/vmlinuz$", line.strip())
        if m:
            return m.group(1)
    return None


def extract_vmlinuz(pkgfile: Path, moddir: str, dst: Path) -> bool:
    r = subprocess.run(["bsdtar", "-xOf", str(pkgfile),
                        f"usr/lib/modules/{moddir}/vmlinuz"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return False
    dst.write_bytes(r.stdout)
    return True


def main() -> int:
    if not OUT.is_dir():
        log(f"{OUT} not mounted", "ERROR")
        return 1
    (OUT / "pkgs").mkdir(exist_ok=True)

    specs = [s.strip() for s in
             os.environ.get("PAND_FETCH_PKGS", DEFAULT_PKGS).split(",") if s.strip()]

    subprocess.run(["pacman", "-Sy", "--noconfirm"], check=True)

    rc = 0
    for spec in specs:
        pkgname = spec.split("/", 1)[-1]
        version = resolve_version(spec)
        if not version:
            log(f"MISS {spec}: not found in synced repo", "ERROR")
            rc = 1
            continue

        # Resumable: reuse an already-persisted package under OUT/pkgs before
        # re-downloading (OUT survives across runs; the container's own
        # pacman cache does not, since each run is --rm).
        pkgfile = find_package_file(pkgname, version, OUT / "pkgs")
        if not pkgfile:
            log(f"downloading {spec} ({version})")
            ok, err_tail = download(spec)
            if not ok:
                log(f"FAIL {spec}: pacman -Sw failed: {' '.join(err_tail)}",
                    "ERROR")
                rc = 1
                continue
            cached = find_package_file(pkgname, version, PKGCACHE)
            if not cached:
                log(f"FAIL {spec}: downloaded but package file not found "
                    f"under {PKGCACHE}", "ERROR")
                rc = 1
                continue
            pkgfile = OUT / "pkgs" / cached.name
            shutil.copy2(cached, pkgfile)

        moddir = find_moddir(pkgfile)
        if not moddir:
            log(f"FAIL {spec}: no usr/lib/modules/*/vmlinuz found in "
                f"{pkgfile.name} -- package kept under pkgs/ for inspection",
                "ERROR")
            rc = 1
            continue
        vmlinuz_dst = OUT / f"vmlinuz-{moddir}"
        if vmlinuz_dst.is_file():
            log(f"HAVE {spec} ({version}) -- vmlinuz-{moddir} already extracted")
            continue
        if not extract_vmlinuz(pkgfile, moddir, vmlinuz_dst):
            log(f"FAIL {spec}: vmlinuz not found in {pkgfile.name} "
                f"at usr/lib/modules/{moddir}/vmlinuz", "ERROR")
            rc = 1
            continue
        log(f"OK   {spec} -> vmlinuz-{moddir} "
            f"({vmlinuz_dst.stat().st_size} bytes), package kept for xfs.ko")

    # This runs as root inside the container (pacman/bsdtar need it), so
    # everything written into the bind-mounted OUT lands root-owned on the
    # host. Hand ownership back if the caller told us who invoked it.
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if uid and gid:
        uid, gid = int(uid), int(gid)
        os.chown(OUT, uid, gid)
        for p in OUT.rglob("*"):
            os.chown(p, uid, gid)
    return rc


if __name__ == "__main__":
    sys.exit(main())
