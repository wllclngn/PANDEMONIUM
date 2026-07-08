#!/usr/bin/env python3
# extract-kernels.py -- pull CachyOS kernel bzImages out of the pacman cache
# into the 7_1-bug cache (~/.cache/pandemonium/7_1-bug/kernels). Nothing is
# downloaded when the packages are already cached, which they are once the
# kernel has been installed at some point (pacman keeps the .pkg.tar.zst).
#
# Usage: extract-kernels.py [version ...]   (default: the 7.0.12/7.1.1/7.1.2 set)

import subprocess
import sys
from pathlib import Path

CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
KERNELS = CACHE / "kernels"
PKGCACHE = Path("/var/cache/pacman/pkg")
DEFAULT = ["7.0.12-1", "7.1.1-2", "7.1.2-2"]


def main() -> int:
    versions = sys.argv[1:] or DEFAULT
    KERNELS.mkdir(parents=True, exist_ok=True)
    for kv in versions:
        pkg = PKGCACHE / f"linux-cachyos-{kv}-x86_64_v3.pkg.tar.zst"
        moddir = f"{kv}-cachyos"
        dst = KERNELS / f"vmlinuz-{moddir}"
        if not pkg.is_file():
            print(f"MISS {kv}: {pkg.name} not in pacman cache "
                  "(install/downgrade linux-cachyos to that version to fetch it)")
            continue
        with open(dst, "wb") as f:
            r = subprocess.run(
                ["bsdtar", "-xOf", str(pkg),
                 f"usr/lib/modules/{moddir}/vmlinuz"], stdout=f)
        if r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            print(f"OK   vmlinuz-{moddir} ({dst.stat().st_size} bytes)")
        else:
            print(f"FAIL {kv}")
            dst.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
