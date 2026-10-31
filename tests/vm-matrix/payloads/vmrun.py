#!/usr/bin/env python3
# vmrun.py -- boot one cell under qemu. Runs INSIDE the runner image as root
# (kvm, mkfs and the chown back all want it). Stdlib only; never imports rig/.
#
#   /run/kernel    bzImage / vmlinuz            (ro)
#   /run/initrd    the cell's initramfs         (ro)
#   /run/out       the run directory            (rw)
#
# Env: SMP THREADS NUMA MEM APPEND TIMEOUT STALL_S KVM(1|0) PIN(1|0)
#      SCRATCH_GB, HOST_UID / HOST_GID (ownership handed back at the end).
#
# Each port is a plain file qemu writes as bytes reach it, so it survives
# anything the guest's own storage does not:
#   ttyS0 console.log   ttyS1 results-tty.bin   ttyS2 stream.bin
#   virtio-serial port "vmm.results" -> results-vio.bin
# The init sends its records over the virtio port when the kernel carries
# virtio_console (memory speed) and over ttyS1 only without it (an emulated
# 16550 moves about 50 KB/s). Whichever carried them becomes results.bin.
#
# reap-reason is "<reason> <seconds>" from qemu's launch: clean-exit (qemu
# exited 0: the guest powered off, or panicked and rebooted under -no-reboot,
# which only the console tells apart), qemu-rc<N>, timeout (TIMEOUT wall
# seconds), stall (no port grew for STALL_S once the console had started).

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

KERNEL, INITRD, OUT = Path("/run/kernel"), Path("/run/initrd"), Path("/run/out")
# log_buf_len: the init drains /dev/kmsg after every phase, and a lockdep or
# verifier splat inside one phase can outrun a defconfig's 256 KiB ring.
CONSOLE = "console=ttyS0 loglevel=6 panic=5 printk.devkmsg=on log_buf_len=16M"
SERIALS = ("console.log", "results-tty.bin", "stream.bin")
VIRTIO_RESULTS = "results-vio.bin"
PORTS = (*SERIALS, VIRTIO_RESULTS)
QMP = Path("/tmp/vmrun-qmp.sock")
SCRATCH = OUT / "scratch.img"


class Interrupted(Exception):
    pass


def log(msg: str, level: str = "INFO") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level}]   {msg}", flush=True)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def mem_mib(s: str) -> int:
    """qemu's -m syntax: a bare number is MiB."""
    m = re.fullmatch(r"([0-9]+)([KMGT]?)(i?B)?", s.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"MEM={s!r} is not a size")
    n, unit = int(m[1]), m[2].upper()
    return {"": n, "K": n // 1024, "M": n, "G": n << 10, "T": n << 20}[unit]


def topology(smp: int, threads: int, numa: int, mib: int) -> list[str]:
    """One socket per NUMA node; with NUMA>1 each socket's CPUs and an equal
    share of memory form one node."""
    if min(smp, threads, numa) < 1 or smp % (numa * threads):
        raise ValueError(f"SMP={smp} is not a multiple of NUMA*THREADS={numa * threads}")
    if mib < numa:
        raise ValueError(f"MEM={mib}M cannot be split over {numa} nodes")
    cores = smp // (numa * threads)
    args = ["-smp", f"cpus={smp},sockets={numa},cores={cores},threads={threads}",
            "-m", f"{mib}M"]
    if numa > 1:
        share = mib // numa
        for n in range(numa):
            size = share + (mib - share * numa if n == numa - 1 else 0)
            args += ["-object", f"memory-backend-ram,id=mem{n},size={size}M",
                     "-numa", f"node,nodeid={n},memdev=mem{n}",
                     "-numa", f"cpu,node-id={n},socket-id={n}"]
    return args


def make_scratch(gb: int) -> str:
    """A fresh ext4 image for the guest's /dev/vda. fallocate, not a sparse
    file: lazy host allocation would land inside the guest's I/O latency.
    Returns the -drive cache options the backing filesystem supports."""
    SCRATCH.unlink(missing_ok=True)
    subprocess.run(["fallocate", "-l", f"{gb}G", str(SCRATCH)], check=True)
    subprocess.run(["mkfs.ext4", "-F", "-q", str(SCRATCH)], check=True)
    # cache=none needs O_DIRECT, which a tmpfs-backed run directory may refuse.
    try:
        os.close(os.open(SCRATCH, os.O_RDWR | os.O_DIRECT))
        return "cache=none,aio=native"
    except OSError:
        return "cache=writeback,aio=threads"


def _qmp_reply(f) -> dict:
    """The next greeting or command reply; asynchronous events are skipped."""
    while True:
        line = f.readline()
        if not line:
            raise ConnectionError("QMP closed")
        msg = json.loads(line)
        if "event" not in msg:
            return msg


def _qmp(f, cmd: str) -> dict:
    f.write((json.dumps({"execute": cmd}) + "\n").encode())
    f.flush()
    return _qmp_reply(f)


def pin_vcpus(smp: int) -> None:
    """1:1 vCPU thread -> host CPU, so each guest CPU is a fixed execution
    unit, not a thread the host scheduler moves under the guest's own."""
    host = sorted(os.sched_getaffinity(0))
    if len(host) < smp:
        log(f"PIN=1 but {len(host)} host CPUs for {smp} vCPUs: not pinned", "WARN")
        return
    sock = None
    for _ in range(100):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(QMP))
            sock = s
            break
        except OSError:
            s.close()
            time.sleep(0.1)
    if sock is None:
        log("QMP socket never appeared: not pinned", "WARN")
        return
    with sock:
        sock.settimeout(5)
        f = sock.makefile("rwb")
        try:
            _qmp_reply(f)
            _qmp(f, "qmp_capabilities")
            cpus = _qmp(f, "query-cpus-fast").get("return", [])
        except (OSError, ValueError) as e:
            log(f"QMP failed ({e}): not pinned", "WARN")
            return
    if len(cpus) != smp:
        log(f"query-cpus-fast listed {len(cpus)} of {smp} vCPUs: not pinned", "WARN")
        return
    pinned = []
    for c in cpus:
        idx, tid = c["cpu-index"], c["thread-id"]
        try:
            os.sched_setaffinity(tid, {host[idx]})
            pinned.append(f"{idx}->{host[idx]}")
        except OSError as e:
            log(f"vCPU {idx} (tid {tid}) not pinned: {e}", "WARN")
    log(f"pinned {len(pinned)}/{smp} vCPUs: {' '.join(pinned)}")


def size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def watch(proc: subprocess.Popen, start: float, timeout: int, stall_s: int
          ) -> tuple[str, float]:
    last, grew = None, start
    while True:
        rc = proc.poll()
        now = time.monotonic()
        if rc is not None:
            return ("clean-exit" if rc == 0 else f"qemu-rc{rc}"), now - start
        sizes = tuple(size(OUT / p) for p in PORTS)
        if sizes != last:
            last, grew = sizes, now
        elif stall_s > 0 and sizes[0] > 0 and now - grew >= stall_s:
            return "stall", now - start
        if now - start >= timeout:
            return "timeout", now - start
        time.sleep(0.5)


def settle_results() -> str:
    """results.bin is the channel that carried the records; the other one is
    dropped when empty and kept beside it when not, since the init writes one."""
    vio, tty = OUT / VIRTIO_RESULTS, OUT / "results-tty.bin"
    pick = vio if size(vio) > 0 or size(tty) == 0 else tty
    pick.replace(OUT / "results.bin")
    other = tty if pick is vio else vio
    if other.exists() and size(other) == 0:
        other.unlink()
    return "virtio" if pick is vio else "ttyS1"


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def chown_back() -> None:
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if not (uid and gid):
        return
    for p in (OUT, *OUT.rglob("*")):
        os.chown(p, int(uid), int(gid), follow_symlinks=False)


def _interrupt(signum, _frame):
    raise Interrupted(signum)


def main() -> int:
    for p, want in ((KERNEL, "file"), (INITRD, "file"), (OUT, "dir")):
        if not (p.is_dir() if want == "dir" else p.is_file()):
            log(f"{p} not mounted", "ERROR")
            return 2
    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)

    smp, threads, numa = env_int("SMP", 8), env_int("THREADS", 2), env_int("NUMA", 1)
    timeout, stall_s = env_int("TIMEOUT", 1800), env_int("STALL_S", 180)
    kvm, pin = os.environ.get("KVM", "1") == "1", os.environ.get("PIN", "0") == "1"
    try:
        topo = topology(smp, threads, numa, mem_mib(os.environ.get("MEM", "4G")))
    except ValueError as e:
        log(str(e), "ERROR")
        return 2
    if kvm:
        try:
            os.close(os.open("/dev/kvm", os.O_RDWR))
        except OSError as e:
            # Never a silent tcg fallback: its timing is a different experiment.
            log(f"KVM=1 but /dev/kvm is unusable ({e}); KVM=0 runs under tcg", "ERROR")
            return 2
        accel = ["-accel", "kvm", "-cpu", "host"]
    else:
        accel = ["-accel", "tcg", "-cpu", "max"]
    append = f"{CONSOLE} {os.environ.get('APPEND', '').strip()}".strip()

    proc = None
    try:
        cache = make_scratch(env_int("SCRATCH_GB", 2))
        for p in (*PORTS, "results.bin"):
            (OUT / p).unlink(missing_ok=True)
        (OUT / "reap-reason").unlink(missing_ok=True)
        QMP.unlink(missing_ok=True)
        argv = ["qemu-system-x86_64", "-M", "q35", *accel, *topo,
                "-nodefaults", "-display", "none", "-no-reboot",
                "-object", "iothread,id=io0",
                "-drive", f"id=scratch,file={SCRATCH},format=raw,if=none,{cache}",
                # One blk-mq queue per vCPU, completions off qemu's main loop:
                # the shape a real NVMe controller gives the guest.
                "-device", f"virtio-blk-pci,drive=scratch,iothread=io0,num-queues={smp}"]
        for p in SERIALS:
            argv += ["-serial", f"file:{OUT / p}"]
        argv += ["-device", "virtio-serial-pci,id=vser0",
                 "-chardev", f"file,id=vmmres,path={OUT / VIRTIO_RESULTS}",
                 "-device", "virtserialport,bus=vser0.0,chardev=vmmres,name=vmm.results"]
        if pin:
            argv += ["-qmp", f"unix:{QMP},server=on,wait=off"]
        argv += ["-kernel", str(KERNEL), "-initrd", str(INITRD), "-append", append]
        (OUT / "qemu.json").write_text(json.dumps(
            {"argv": argv, "accel": "kvm" if kvm else "tcg", "scratch_cache": cache,
             "append": append}, indent=2) + "\n")
        log(shlex.join(argv))

        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        start = time.monotonic()
        if pin:
            pin_vcpus(smp)
        reason, secs = watch(proc, start, timeout, stall_s)
        stop(proc)
        sizes = ", ".join(f"{p} {size(OUT / p)}" for p in PORTS)
        channel = settle_results()
        (OUT / "reap-reason").write_text(f"{reason} {secs:.1f}\n")
        meta = json.loads((OUT / "qemu.json").read_text())
        meta["results_channel"] = channel
        (OUT / "qemu.json").write_text(json.dumps(meta, indent=2) + "\n")
        sizes += f"; results via {channel}"
        log(f"{reason} after {secs:.1f}s ({sizes})",
            "INFO" if reason == "clean-exit" else "WARN")
        return 0
    except Interrupted as e:
        log(f"signal {e.args[0]}: qemu stopped, no reap reason", "WARN")
        return 128 + e.args[0]
    finally:
        if proc is not None:
            stop(proc)
        if not (OUT / "results.bin").exists():
            settle_results()
        SCRATCH.unlink(missing_ok=True)
        QMP.unlink(missing_ok=True)
        chown_back()


if __name__ == "__main__":
    sys.exit(main())
