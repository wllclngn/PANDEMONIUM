#!/usr/bin/env python3
# run-defconfig-in-container.py -- runs inside the pandemonium-runner
# container. Boots the prebuilt defconfig rootfs (built once, host-side, by
# build-defconfig-guest.py) with a real `make vmlinux` under qemu; qemu never
# runs bare on the host, only in here. Swaps the requested PANDEMONIUM binary
# into the (writable, bind-mounted) rootfs with debugfs -w before boot -- the
# same in-container-does-its-own-file-prep shape as run-in-container.py's
# scratch/results image creation, so nothing mutates the rootfs from the host
# side. root=/dev/vda with snapshot=on discards make's writes each boot, so
# the mounted rootfs.img is never altered by the guest itself -- only this
# script's pre-boot debugfs swap touches it.
#
# Config via env (passed by the caller's docker run -e):
#   PAND_SCHED       pandemonium|none   (default: pandemonium)
#   PAND_JOBS        make -j N          (default: 8)
#   PAND_TARGET      make target        (default: vmlinux)
#   PAND_WINDOW      throughput-mode build window seconds (0 = to completion)
#   QEMU_SMP         int                (default: 8)
#   QEMU_MEM         string             (default: 8G)
#   PAND_NOHZ        on|off             (default: off)
#   PAND_IOPS        virtio-blk iops throttle on the rootfs (0 = unlimited)
#   BOOT_TIMEOUT     seconds            (default: 900)
#   NO_KVM           1 = force tcg      (default: 0)

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

KERNEL  = Path("/run/kernel")
ROOTFS  = Path("/run/rootfs.img")
PANDBIN = Path("/run/pandemonium-bin")
OUT     = Path("/run/out")


def ts() -> str:
    return time.strftime("[%H:%M:%S]")


def log(msg: str, level: str = "INFO") -> None:
    print(f"{ts()} [{level}]   {msg}", flush=True)


def envi(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def envb(name: str, default_on: bool) -> bool:
    return os.environ.get(name, "on" if default_on else "off") in ("on", "1")


def qemu_topo(smp: int):
    if smp % 4 == 0:
        topo = f"cpus={smp},sockets=2,cores={smp // 4},threads=2"
    elif smp % 2 == 0:
        topo = f"cpus={smp},sockets=2,cores={smp // 2},threads=1"
    else:
        topo = f"cpus={smp},sockets=1,cores={smp},threads=1"
    split = smp >= 2 and smp % 2 == 0
    args = ["-smp", topo]
    if split:
        half = smp // 2
        args += ["-numa", f"node,cpus=0-{half - 1},nodeid=0,memdev=ram0",
                 "-numa", f"node,cpus={half}-{smp - 1},nodeid=1,memdev=ram1",
                 "-numa", "dist,src=0,dst=1,val=20"]
    return args, split


def swap_pandemonium() -> bool:
    subprocess.run(["debugfs", "-w", "-R", "rm /usr/local/bin/pandemonium",
                    str(ROOTFS)], capture_output=True)
    r = subprocess.run(["debugfs", "-w", "-R",
                        f"write {PANDBIN} /usr/local/bin/pandemonium",
                        str(ROOTFS)], capture_output=True, text=True)
    return r.returncode == 0


def debugfs_dump(img: Path, guest_path: str, host_path: Path) -> bool:
    host_path.unlink(missing_ok=True)
    subprocess.run(["debugfs", "-R", f"dump {guest_path} {host_path.name}",
                    str(img.resolve())], cwd=host_path.parent,
                   capture_output=True)
    return host_path.is_file() and host_path.stat().st_size > 0


def chown_out_back() -> None:
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if not uid or not gid:
        return
    uid, gid = int(uid), int(gid)
    os.chown(OUT, uid, gid)
    for p in OUT.rglob("*"):
        os.chown(p, uid, gid)


def main() -> int:
    if not OUT.is_dir():
        log(f"{OUT} not mounted", "ERROR")
        return 1
    if not KERNEL.exists():
        log(f"{KERNEL} not mounted", "ERROR")
        return 1
    if not ROOTFS.exists():
        log(f"{ROOTFS} not mounted -- run build-defconfig-guest.py first", "ERROR")
        return 1

    sched        = os.environ.get("PAND_SCHED", "pandemonium")
    jobs         = envi("PAND_JOBS", 8)
    target       = os.environ.get("PAND_TARGET", "vmlinux")
    window       = envi("PAND_WINDOW", 0)
    smp          = envi("QEMU_SMP", 8)
    mem          = os.environ.get("QEMU_MEM", "8G")
    nohz         = envb("PAND_NOHZ", False)
    iops         = envi("PAND_IOPS", 0)
    boot_timeout = envi("BOOT_TIMEOUT", 900)
    no_kvm       = os.environ.get("NO_KVM", "0") == "1"

    log(f"sched={sched} jobs={jobs} target={target} window={window or 'full'} "
        f"smp={smp} mem={mem}")

    if sched != "none":
        if not PANDBIN.exists():
            log(f"{PANDBIN} not mounted", "ERROR")
            return 1
        log("swapping pandemonium into rootfs")
        if not swap_pandemonium():
            log("pandemonium swap failed", "ERROR")
            return 1

    results = OUT / "results.img"
    subprocess.run(["truncate", "-s", "1G", str(results)], check=True)
    subprocess.run(["mkfs.ext4", "-F", "-q", str(results)], check=True)

    kvm = (not no_kvm) and Path("/dev/kvm").exists()
    accel = ["-accel", "kvm", "-cpu", "host"] if kvm \
        else ["-accel", "tcg", "-cpu", "max"]
    topo, split = qemu_topo(smp)
    if split:
        half = max(int(mem.rstrip("G")) // 2, 1)
        mem_args = ["-object", f"memory-backend-ram,id=ram0,size={half}G",
                    "-object", f"memory-backend-ram,id=ram1,size={half}G",
                    "-m", mem]
    else:
        mem_args = ["-m", mem]

    nohz_cmd = f"nohz_full=1-{smp - 1} " if nohz else ""
    cmdline = (f"root=/dev/vda rw init=/init console=ttyS0 panic=1 {nohz_cmd}"
               f"pand.sched={sched} pand.jobs={jobs} "
               f"pand.target={target} pand.window={window}")
    rootdrive = f"file={ROOTFS},if=virtio,format=raw,snapshot=on"
    if iops:
        rootdrive += f",throttling.iops-total={iops}"

    qcmd = ["qemu-system-x86_64", "-M", "q35", *accel, *topo, *mem_args,
            "-kernel", str(KERNEL), "-drive", rootdrive,
            "-drive", f"file={results},if=virtio,format=raw",
            "-append", cmdline, "-nographic", "-no-reboot",
            "-serial", "mon:stdio"]

    log(f"launching qemu (kvm={kvm})")
    qemu_console = open(OUT / "console.log", "wb")
    proc = subprocess.Popen(qcmd, stdout=qemu_console, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)

    def reap(*_):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()

    signal.signal(signal.SIGTERM, lambda *a: (reap(), sys.exit(143)))
    signal.signal(signal.SIGINT, lambda *a: (reap(), sys.exit(130)))

    start = time.monotonic()
    try:
        proc.wait(timeout=boot_timeout)
        log(f"qemu exited rc={proc.returncode} "
            f"after {int(time.monotonic() - start)}s")
    except subprocess.TimeoutExpired:
        log(f"timeout {boot_timeout}s -- guest may have wedged; reaping", "WARN")
        reap()
    qemu_console.close()

    bs = OUT / "build_seconds"
    oc = OUT / "object_count"
    ev = OUT / "events.bin"
    debugfs_dump(results, "/build_seconds", bs)
    debugfs_dump(results, "/object_count", oc)
    got_ev = debugfs_dump(results, "/events.bin", ev)
    secs = bs.read_text().strip() if bs.is_file() and bs.stat().st_size else "MISSING"
    objs = oc.read_text().strip() if oc.is_file() and oc.stat().st_size else "MISSING"
    log(f"build_seconds={secs}  objects={objs}  events.bin="
        f"{ev.stat().st_size if got_ev else 'MISSING'}")

    # results.img is qemu's disposable results-disk backing store, not a
    # diagnostic artifact -- build_seconds/object_count/events.bin are
    # already dumped out of it above.
    results.unlink(missing_ok=True)

    chown_out_back()
    return 0


if __name__ == "__main__":
    sys.exit(main())
