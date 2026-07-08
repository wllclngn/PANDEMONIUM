#!/usr/bin/env python3
# build-guest.py -- assemble the 7.1-bug reproducer initramfs.
#
# busybox-static + montauk (bundled with its .so deps) + the chosen PANDEMONIUM
# binary (bundled) + the static strand load + the C init -> initramfs.cpio.gz.
# montauk runs IN the guest (kstrand needs the guest's own per-CPU kthreads);
# the host runs montauk_analyze on /out/events.bin afterward.
#
# Usage:
#   build-guest.py --pandemonium <bin> --out <initramfs.cpio.gz>
#                  [--montauk /usr/local/bin/montauk] [--busybox PATH]

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
GUEST_SRC = HERE / "guest"
# All ephemera live here, never in the working tree.
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
GUEST_BIN = CACHE / "guest-bin"

# busybox applets init / the load actually call.
APPLETS = ("sh mount umount dmesg grep ls cat echo sleep poweroff uptime date "
           "mkdir printf uname stat cut head tail tee sort wc sync chmod cp ln "
           "rm find dd").split()


def log(m: str) -> None:
    print(f"[build-guest] {m}", flush=True)


def find_busybox(explicit: Path | None) -> Path | None:
    # OPTIONAL: the C init is self-contained (mount() syscall + execv, no shell).
    # busybox is only baked in when present, as a debugging shell.
    for c in (explicit, Path("/usr/bin/busybox"), Path("/bin/busybox")):
        if c and Path(c).is_file():
            return Path(c)
    w = shutil.which("busybox")
    return Path(w) if w else None


def bundle_binary(src: Path, stage: Path, dest_rel: str) -> None:
    """Copy src into the stage + every shared lib it needs + the loader."""
    dest = stage / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    dest.chmod(0o755)
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
        if "ld-linux" in p.name or p.parent.name == "lib64":
            d = stage / "lib64" / p.name
        else:
            d = stage / "usr/lib" / p.name
        d.parent.mkdir(parents=True, exist_ok=True)
        if not d.exists():
            shutil.copy2(p, d)


def extract_xfs(kver, stage):
    """Bundle the kernel's xfs.ko (decompressed) as /xfs.ko so the init can
    finit_module it. kver like 7.1.2-2-cachyos; the package strips -cachyos."""
    pkgver = kver.replace("-cachyos", "")
    pkg = CACHE / "kernels" / "pkgs" / \
        f"linux-cachyos-{pkgver}-x86_64_v3.pkg.tar.zst"
    if not pkg.is_file():
        sys.exit(f"linux-cachyos package not found for xfs: {pkg}")
    modpath = f"usr/lib/modules/{kver}/kernel/fs/xfs/xfs.ko.zst"
    r = subprocess.run(["bsdtar", "-xOf", str(pkg), modpath],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        sys.exit(f"xfs.ko not found in {pkg.name} at {modpath}")
    z = subprocess.run(["zstd", "-dq"], input=r.stdout, capture_output=True)
    (stage / "xfs.ko").write_bytes(z.stdout)
    log(f"xfs.ko for {kver} bundled ({len(z.stdout)} bytes)")


def compile_guest():
    """Compile the guest C sources into the cache, rebuilding when the source
    is newer. Returns (init_bin, load_bin)."""
    GUEST_BIN.mkdir(parents=True, exist_ok=True)
    out = {}
    pthread_bins = ("pand-idle-ping", "pand-lock-block")
    for name in ("pand-guest-init", "pand-strand-load", "pand-io-flood",
                 "pand-kwin-proxy", "pand-idle-ping", "pand-lock-block"):
        src = GUEST_SRC / f"{name}.c"
        dst = GUEST_BIN / name
        extra = ["-lpthread"] if name in pthread_bins else []
        if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
            subprocess.run(["gcc", "-O2", "-static", "-Wall", "-o", str(dst),
                            str(src), *extra], check=True)
            log(f"compiled {name}")
        out[name] = dst
    return (out["pand-guest-init"], out["pand-strand-load"],
            out["pand-io-flood"], out["pand-kwin-proxy"],
            out["pand-idle-ping"], out["pand-lock-block"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pandemonium", required=True, type=Path)
    ap.add_argument("--montauk", type=Path, default=Path("/usr/local/bin/montauk"))
    ap.add_argument("--busybox", type=Path, default=None)
    ap.add_argument("--out", type=Path,
                    default=CACHE / "initramfs" / "pand.cpio.gz")
    ap.add_argument("--xfs-kver", default=None,
                    help="bundle this kernel's xfs.ko (e.g. 7.1.2-2-cachyos)")
    args = ap.parse_args()

    (init_bin, load_bin, flood_bin, kwin_bin,
     ping_bin, lockblock_bin) = compile_guest()
    if not args.pandemonium.is_file():
        sys.exit(f"pandemonium binary not found: {args.pandemonium}")
    if not args.montauk.is_file():
        sys.exit(f"montauk not found: {args.montauk}")

    stage = Path(tempfile.mkdtemp(prefix="pand7bug-"))
    try:
        for d in ("bin", "sbin", "proc", "sys", "sys/fs/bpf", "dev", "tmp",
                  "scratch", "out", "usr/lib", "usr/local/bin", "lib64"):
            (stage / d).mkdir(parents=True, exist_ok=True)

        bb = find_busybox(args.busybox)
        if bb:
            shutil.copy2(bb, stage / "bin/busybox")
            (stage / "bin/busybox").chmod(0o755)
            for a in APPLETS:
                (stage / "bin" / a).symlink_to("busybox")
            log(f"busybox ({bb}) + {len(APPLETS)} applets (debug shell)")
        else:
            log("no busybox -- guest is init + binaries only (init is self-contained C)")

        bundle_binary(args.montauk, stage, "usr/local/bin/montauk")
        (stage / "bin/montauk").symlink_to("/usr/local/bin/montauk")
        log("montauk bundled")

        bundle_binary(args.pandemonium, stage, "bin/pandemonium")
        log(f"pandemonium bundled ({args.pandemonium})")

        shutil.copy2(load_bin, stage / "bin/pand-strand-load")
        (stage / "bin/pand-strand-load").chmod(0o755)
        shutil.copy2(flood_bin, stage / "bin/pand-io-flood")
        (stage / "bin/pand-io-flood").chmod(0o755)
        shutil.copy2(kwin_bin, stage / "bin/pand-kwin-proxy")
        (stage / "bin/pand-kwin-proxy").chmod(0o755)
        shutil.copy2(ping_bin, stage / "bin/pand-idle-ping")
        (stage / "bin/pand-idle-ping").chmod(0o755)
        shutil.copy2(lockblock_bin, stage / "bin/pand-lock-block")
        (stage / "bin/pand-lock-block").chmod(0o755)
        shutil.copy2(init_bin, stage / "init")
        (stage / "init").chmod(0o755)
        log("strand load + io-flood + init")

        if args.xfs_kver:
            extract_xfs(args.xfs_kver, stage)

        args.out.parent.mkdir(parents=True, exist_ok=True)
        find = subprocess.Popen(["find", ".", "-print0"], cwd=stage,
                                stdout=subprocess.PIPE)
        cpio = subprocess.Popen(["cpio", "--null", "-o", "-H", "newc"], cwd=stage,
                                stdin=find.stdout, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        find.stdout.close()
        with open(args.out, "wb") as f:
            gz = subprocess.Popen(["gzip", "-9"], stdin=cpio.stdout, stdout=f)
            cpio.stdout.close()
            gz.communicate()
        if gz.returncode != 0 or not args.out.is_file():
            sys.exit("cpio/gzip failed")
        log(f"built {args.out} ({args.out.stat().st_size} bytes)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
