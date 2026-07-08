#!/usr/bin/env python3
# fetch-kernels.py -- host-side driver. Runs fetch-kernels-in-container.py
# inside the pandemonium-runner image so the CachyOS repo sync, package
# download and vmlinuz extraction all happen against the image's own
# pacman.conf/keyring -- the host's pacman is never touched.
#
# Usage: fetch-kernels.py [spec ...]   (default: base + v3 stable + v3 rc)
#   spec is a pacman target: "pkgname" or "repo/pkgname", e.g.
#   cachyos-v3/linux-cachyos or cachyos-v3/linux-cachyos-rc

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent.resolve()
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
KERNELS = CACHE / "kernels"
IMAGE = "pandemonium-runner:latest"
DEFAULT = "linux-cachyos,cachyos-v3/linux-cachyos,cachyos-v3/linux-cachyos-rc"


def main() -> int:
    specs = ",".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT
    KERNELS.mkdir(parents=True, exist_ok=True)

    cmd = ["docker", "run", "--rm",
           "-v", f"{HERE / 'fetch-kernels-in-container.py'}:/run/fetch-kernels.py:ro",
           "-v", f"{KERNELS}:/out",
           "-e", f"PAND_FETCH_PKGS={specs}",
           # pacman/bsdtar need root inside the container; hand ownership
           # of whatever it writes back to the invoking user on exit.
           "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}",
           IMAGE, "python3", "/run/fetch-kernels.py"]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"fetch-kernels-in-container.py failed (rc={r.returncode})")
    print()
    print("kernels now cached:")
    for f in sorted(KERNELS.glob("vmlinuz-*")):
        print(f"  {f.name} ({f.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
