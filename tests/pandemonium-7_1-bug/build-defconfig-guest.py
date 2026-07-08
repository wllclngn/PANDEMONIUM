#!/usr/bin/env python3
# build-defconfig-guest.py -- build the defconfig reproducer rootfs (ext4).
#
# docker exports the base-devel toolchain image into a stage; we add the kernel
# source (clean + defconfig'd) at /build, montauk (bundled with its non-glibc
# deps -- the rootfs's own glibc/loader are kept), a default PANDEMONIUM binary,
# and the python init at /init. mke2fs -d packs it all into an ext4 image,
# rootless. qemu boots it root=/dev/vda with a -snapshot overlay so each build
# starts from the clean defconfig tree; per-version PANDEMONIUM is swapped in at
# run time with debugfs -w. Docker is only the build environment -- the kernel
# under test is the qemu -kernel, never docker's.

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
GUEST_SRC = HERE / "guest"
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
SRC = (Path.home() / ".cache" / "pandemonium" / "prism-cachyos-assets"
       / "linux-6.14.7")
IMAGE = "pand-defconfig-builder"


def log(m):
    print(f"[defconfig-guest] {m}", flush=True)


def bundle_binary(src: Path, stage: Path):
    """Copy src to /usr/local/bin + its deps. glibc/loader already exist in the
    rootfs (skip), so only montauk's special libs (libbpf, liburing, ...) land."""
    dst = stage / "usr/local/bin" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    dst.chmod(0o755)
    out = subprocess.run(["ldd", str(src)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        line = line.strip()
        path = None
        if "=>" in line:
            rhs = line.split("=>", 1)[1].strip()
            if rhs.startswith("/"):
                path = rhs.split()[0]
        elif line.startswith("/"):
            path = line.split()[0]
        if not path:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        sub = "lib64" if ("ld-linux" in p.name or p.parent.name == "lib64") \
            else "usr/lib"
        d = stage / sub / p.name
        d.parent.mkdir(parents=True, exist_ok=True)
        if not d.exists():
            shutil.copy2(p, d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--montauk", type=Path, default=Path("/usr/local/bin/montauk"))
    ap.add_argument("--pandemonium", type=Path,
                    default=CACHE / "versions" / "pandemonium-5.16.0")
    ap.add_argument("--out", type=Path, default=CACHE / "defconfig-rootfs.img")
    ap.add_argument("--size-gb", type=int, default=8)
    args = ap.parse_args()

    if not SRC.is_dir():
        sys.exit(f"kernel source not found: {SRC}")
    if not args.pandemonium.is_file():
        sys.exit(f"pandemonium not found: {args.pandemonium}")

    stage = Path(tempfile.mkdtemp(prefix="pand-defconfig-"))
    try:
        log("exporting toolchain container -> stage")
        cid = subprocess.run(["docker", "create", IMAGE],
                             capture_output=True, text=True).stdout.strip()
        exp = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-C", str(stage), "-xf", "-"],
                       stdin=exp.stdout, check=True)
        exp.wait()
        subprocess.run(["docker", "rm", cid], capture_output=True)

        log("staging kernel source (the slow 2.3G copy)")
        build = stage / "build"
        shutil.copytree(SRC, build, symlinks=True, ignore_dangling_symlinks=True)
        log("distclean + defconfig")
        subprocess.run(["make", "-C", str(build), "distclean"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["make", "-C", str(build), "defconfig"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        log("bundling montauk + pandemonium")
        bundle_binary(args.montauk, stage)
        pand = stage / "usr/local/bin" / "pandemonium"
        shutil.copy2(args.pandemonium, pand)
        pand.chmod(0o755)

        shutil.copy2(GUEST_SRC / "pand-defconfig-init.py", stage / "init")
        (stage / "init").chmod(0o755)

        # Empty runtime/pseudo dirs -- the guest recreates them, and some (e.g.
        # /run/systemd/dissect-root) are mode-000 and block mke2fs -d's walk.
        for d in ("run", "proc", "sys", "dev", "tmp"):
            shutil.rmtree(stage / d, ignore_errors=True)
            (stage / d).mkdir(exist_ok=True)
        # Fix any other unreadable dirs left by the container export.
        subprocess.run(["find", str(stage), "-type", "d", "!", "-perm",
                        "-u+rwx", "-exec", "chmod", "u+rwx", "{}", "+"],
                       capture_output=True)

        log(f"mke2fs -d -> {args.out} ({args.size_gb}G)")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["truncate", "-s", f"{args.size_gb}G", str(args.out)],
                       check=True)
        r = subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-d",
                            str(stage), str(args.out)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"mke2fs failed: {r.stderr.strip()}")
        log(f"built {args.out} ({args.out.stat().st_size} bytes)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
