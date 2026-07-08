#!/usr/bin/env python3
# run-in-container.py -- runs inside the pandemonium-runner container.
#
# Config via env, kernel/initramfs/out bind-mounted by the caller's
# `docker run`, this script owns the qemu process end to end -- qemu never
# runs bare on the host, only inside this container, so a guest freeze is
# contained (kill the container, the host and the disk images survive).
# This replaces run-vm.py's bare-host qemu launch.
#
# montauk runs INSIDE the guest (pand-guest-init.c), not in this container
# tracing the qemu process: kstrand needs the guest's own per-CPU kthread
# view, which a host-side qemu-process trace cannot see. This script never
# launches montauk itself.
#
# scratch.img/results.img are real virtio-blk-backed disk images, not a
# passthrough share: the strand/freeze condition under test is
# block-I/O-completion and writeback kthread behavior, which requires an
# actual block device.
#
# Config via env (passed by the caller's docker run -e):
#   PAND_MODE        dark|held                 (default: dark)
#   PAND_SCHED       pandemonium|none          (default: pandemonium)
#   QEMU_SMP         int                       (default: 8)
#   QEMU_MEM         string                    (default: 4G)
#   PAND_DURATION    seconds under capture     (default: 45)
#   PAND_WRITERS     fsync writer count        (default: 4)
#   PAND_NOHZ        on|off                    (default: on)
#   PAND_SCRATCH_GB  scratch disk size (GB)    (default: 2)
#   BOOT_TIMEOUT     seconds                   (default: 180)
#   NO_KVM           1 = force tcg             (default: 0)
#   PAND_WORKLOAD    strand|ioflood            (default: strand)
#   PAND_IOMODE      direct|fsync|fork|all     (default: all)
#   PAND_ISSUERS     io-flood issuers          (default: 8x smp)
#   PAND_BLOCK_KB    io-flood block size (KB)  (default: 4)
#   PAND_FS          ext4|xfs                  (default: ext4)
#   PAND_WAYLAND     1 = kwin compositor proxy (default: 0)
#   PAND_FRAME_US    compositor frame period   (default: 16667)
#   PAND_IOPS        virtio-blk iops throttle  (default: 0 = unlimited)
#   PAND_BPS         virtio-blk bps throttle   (default: 0 = unlimited)
#   PAND_IDLEPING    1 = Finding-2 targeted induction (default: 0)
#   PAND_PING_PAIRS  idle-ping thread pairs    (default: 4)
#   PAND_LOCKBLOCK   1 = Finding-4 targeted induction (default: 0)
#   PAND_LOCK_THREADS lock-block thread count  (default: 8)
#   PAND_SCXSTORM    1 = MONTAUK_SCX_STORM opt-in (default: 0, see Finding 5)

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

KERNEL    = Path("/run/kernel")
INITRAMFS = Path("/run/initramfs.cpio.gz")
OUT       = Path("/run/out")


def ts() -> str:
    return time.strftime("[%H:%M:%S]")


def log(msg: str, level: str = "INFO") -> None:
    print(f"{ts()} [{level}]   {msg}", flush=True)


def envi(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def envb(name: str, default_on: bool) -> bool:
    v = os.environ.get(name, "on" if default_on else "off")
    return v in ("on", "1")


def qemu_topo(smp: int):
    # Single socket, matching the real host (AMD Ryzen 5 3600: 1 socket, 6
    # cores, SMT2) -- a fabricated 2-socket/2-NUMA split perturbs sched-domain
    # construction, which the idle-kick race under investigation (ext.c's
    # can_skip_idle_kick/balance_one) directly depends on. No -numa clauses:
    # one flat domain, same as bare metal.
    if smp % 2 == 0:
        topo = f"cpus={smp},sockets=1,cores={smp // 2},threads=2"
    else:
        topo = f"cpus={smp},sockets=1,cores={smp},threads=1"
    args = ["-smp", topo]
    return args, False


def _qmp_command(sock, cmd, arguments=None):
    payload = {"execute": cmd}
    if arguments is not None:
        payload["arguments"] = arguments
    sock.sendall((json.dumps(payload) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.splitlines()[0])


def pin_vcpus(qmp_path: Path, smp: int) -> None:
    # 1:1 vCPU-thread -> host-logical-CPU pinning via QMP, so each guest CPU
    # runs on a fixed host thread instead of floating across whatever the
    # host scheduler picks -- on bare metal every logical CPU is a fixed
    # execution unit; letting KVM vCPU threads drift adds host-scheduler
    # jitter on top of the guest scheduling behavior under test, which can
    # smear the exact idle-kick race window wide enough to mask it.
    sock = None
    for _ in range(50):  # QMP socket appears shortly after qemu starts
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(qmp_path))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            sock = None
            time.sleep(0.1)
    if sock is None:
        log("QMP socket never appeared -- vCPU pinning skipped", "WARN")
        return
    with sock:
        sock.settimeout(5)
        greeting = sock.recv(4096)  # QMP's own capabilities banner
        if not greeting:
            log("QMP greeting empty -- vCPU pinning skipped", "WARN")
            return
        _qmp_command(sock, "qmp_capabilities")
        r = _qmp_command(sock, "query-cpus-fast")
        cpus = r.get("return", [])
        if len(cpus) != smp:
            log(f"query-cpus-fast returned {len(cpus)} cpus, expected {smp} "
                "-- vCPU pinning skipped", "WARN")
            return
        pinned = 0
        for cpu in cpus:
            idx, tid = cpu.get("cpu-index"), cpu.get("thread-id")
            if idx is None or tid is None:
                continue
            t = subprocess.run(["taskset", "-pc", str(idx), str(tid)],
                               capture_output=True, text=True)
            if t.returncode == 0:
                pinned += 1
            else:
                log(f"taskset failed for vcpu {idx} (tid {tid}): "
                    f"{t.stderr.strip()}", "WARN")
        log(f"pinned {pinned}/{len(cpus)} vCPU threads 1:1 to host logical CPUs")


def mkfs_scratch(img: Path, size_gb: int, fs: str = "ext4") -> None:
    subprocess.run(["truncate", "-s", f"{size_gb}G", str(img)], check=True)
    tool = (["mkfs.xfs", "-f", str(img)] if fs == "xfs"
            else ["mkfs.ext4", "-F", "-q", str(img)])
    r = subprocess.run(tool, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"mkfs.{fs} failed: {r.stderr.strip()}")


def chown_out_back() -> None:
    # This script runs as root inside the container (qemu -accel kvm and
    # mkfs on a fresh file both want it), so everything it writes into the
    # bind-mounted OUT lands root-owned on the host. HOST_UID/HOST_GID (set
    # by the caller's docker run -e) let it hand ownership back before
    # exiting, instead of leaving the host's own cache directory locked out
    # from under the invoking user.
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if not uid or not gid:
        return
    uid, gid = int(uid), int(gid)
    os.chown(OUT, uid, gid)
    for p in OUT.rglob("*"):
        os.chown(p, uid, gid)


def debugfs_dump(img: Path, guest_path: str, host_path: Path) -> bool:
    host_path.unlink(missing_ok=True)
    # debugfs -R splits its command string on spaces, so the destination must
    # be space-free -- dump to a bare relative name in the image's own dir.
    subprocess.run(["debugfs", "-R", f"dump {guest_path} {host_path.name}",
                    str(img.resolve())], cwd=host_path.parent,
                   capture_output=True, text=True)
    return host_path.is_file() and host_path.stat().st_size > 0


def main() -> int:
    if not OUT.is_dir():
        log(f"{OUT} not mounted", "ERROR")
        return 1
    if not KERNEL.exists():
        log(f"{KERNEL} not mounted", "ERROR")
        return 1
    if not INITRAMFS.exists():
        log(f"{INITRAMFS} not mounted", "ERROR")
        return 1

    mode         = os.environ.get("PAND_MODE", "dark")
    sched        = os.environ.get("PAND_SCHED", "pandemonium")
    smp          = envi("QEMU_SMP", 8)
    mem          = os.environ.get("QEMU_MEM", "4G")
    duration     = envi("PAND_DURATION", 45)
    writers      = envi("PAND_WRITERS", 4)
    nohz         = envb("PAND_NOHZ", True)
    scratch_gb   = envi("PAND_SCRATCH_GB", 2)
    boot_timeout = envi("BOOT_TIMEOUT", 180)
    no_kvm       = os.environ.get("NO_KVM", "0") == "1"
    workload     = os.environ.get("PAND_WORKLOAD", "strand")
    iomode       = os.environ.get("PAND_IOMODE", "all")
    issuers      = envi("PAND_ISSUERS", smp * 8)
    block_kb     = envi("PAND_BLOCK_KB", 4)
    fs           = os.environ.get("PAND_FS", "ext4")
    wayland      = envi("PAND_WAYLAND", 0)
    frame_us     = envi("PAND_FRAME_US", 16667)
    iops         = envi("PAND_IOPS", 0)
    bps          = envi("PAND_BPS", 0)
    hogs         = max(smp - 2, 1)
    idleping     = envi("PAND_IDLEPING", 0)
    ping_pairs   = envi("PAND_PING_PAIRS", 4)
    lockblock    = envi("PAND_LOCKBLOCK", 0)
    lock_threads = envi("PAND_LOCK_THREADS", 8)
    scxstorm     = envi("PAND_SCXSTORM", 0)

    log(f"mode={mode} sched={sched} smp={smp} mem={mem} nohz={nohz} "
        f"workload={workload} fs={fs} duration={duration}s")

    scratch = OUT / "scratch.img"
    results = OUT / "results.img"
    log(f"fresh {fs} scratch + ext4 results ({scratch_gb}G)")
    mkfs_scratch(scratch, scratch_gb, fs)
    mkfs_scratch(results, 1, "ext4")

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
    cmdline = (f"console=ttyS0 panic=1 {nohz_cmd}"
               f"pand.sched={sched} pand.mode={mode} "
               f"pand.duration={duration} pand.writers={writers} "
               f"pand.hogs={hogs} "
               f"pand.workload={workload} pand.iomode={iomode} "
               f"pand.issuers={issuers} pand.block_kb={block_kb} "
               f"pand.fs={fs} pand.wayland={wayland} "
               f"pand.frame_us={frame_us} "
               f"pand.idleping={idleping} pand.ping_pairs={ping_pairs} "
               f"pand.lockblock={lockblock} pand.lock_threads={lock_threads} "
               f"pand.scxstorm={scxstorm}")

    # if=none + a separate -device virtio-blk-pci (rather than the if=virtio
    # shorthand) so num-queues/iothread can be set: a real NVMe controller
    # fans I/O completions out over per-CPU blk-mq queues with independent
    # MSI-X vectors, but the shorthand's default (aio=threads, one queue, no
    # iothread) serializes every completion for every vCPU through one
    # thread-pool-backed queue on QEMU's main loop -- changing which CPU
    # takes the completion and when, which is exactly the input the idle-kick
    # race (can_skip_idle_kick) is sensitive to.
    scratch_drive = f"id=scratch0,file={scratch},format=raw,cache=none,aio=native,if=none"
    if iops:
        scratch_drive += f",throttling.iops-total={iops}"
    if bps:
        scratch_drive += f",throttling.bps-total={bps}"
    results_drive = f"id=results0,file={results},format=raw,cache=none,aio=native,if=none"

    # Second serial port (ttyS1 in the guest): montauk's --stream-out target.
    # qemu writes this straight to a host file as bytes arrive at the virtual
    # UART -- independent of the guest's own filesystem/block-device state,
    # so it survives a guest hang that leaves events.bin (on the results
    # disk) empty. Truncated fresh each run, same as console.log.
    stream_out = OUT / "montauk-stream.bin"
    stream_out.unlink(missing_ok=True)

    qmp_path = Path(f"/tmp/pand-qmp-{os.getpid()}.sock")
    qmp_path.unlink(missing_ok=True)

    qcmd = ["qemu-system-x86_64", "-M", "q35", *accel, *topo, *mem_args,
            "-object", "iothread,id=iothread0",
            "-kernel", str(KERNEL), "-initrd", str(INITRAMFS),
            "-drive", scratch_drive,
            "-device", "virtio-blk-pci,drive=scratch0,iothread=iothread0,"
                       f"num-queues={smp}",
            "-drive", results_drive,
            "-device", "virtio-blk-pci,drive=results0,iothread=iothread0,"
                       f"num-queues={smp}",
            "-qmp", f"unix:{qmp_path},server,nowait",
            "-append", cmdline, "-nographic", "-no-reboot",
            "-serial", "mon:stdio",
            "-serial", f"file:{stream_out}"]

    log(f"launching qemu (kvm={kvm})")
    qemu_console = open(OUT / "console.log", "wb")
    proc = subprocess.Popen(qcmd, stdout=qemu_console, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    pin_vcpus(qmp_path, smp)
    qmp_path.unlink(missing_ok=True)

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
        log(f"timeout {boot_timeout}s -- guest may have frozen; reaping qemu",
            "WARN")
        reap()
    qemu_console.close()

    # Results live on the ext4 vdb (debugfs is ext4-only; scratch may be xfs).
    ev = OUT / "events.bin"
    if debugfs_dump(results, "/events.bin", ev):
        log(f"events.bin extracted ({ev.stat().st_size} bytes)")
    else:
        log("events.bin MISSING or empty -- inspect console.log", "WARN")
    if debugfs_dump(results, "/kwin_overruns.txt", OUT / "kwin_overruns.txt"):
        log("kwin_overruns.txt extracted (compositor frame overruns)")
    got_done = debugfs_dump(results, "/done", OUT / "done")
    log(f"done marker: {'yes (clean run)' if got_done else 'no (froze or errored)'}")
    if stream_out.is_file() and stream_out.stat().st_size > 0:
        log(f"montauk-stream.bin captured ({stream_out.stat().st_size} bytes) -- "
            f"survives a hang independent of events.bin")
    else:
        log("montauk-stream.bin MISSING or empty", "WARN")

    # scratch/results are qemu's disposable virtio-blk backing store, not a
    # diagnostic artifact -- everything worth keeping has already been
    # debugfs-dumped out of them above. At ~350MB/cell (2G scratch, 1G
    # results, both sparse but ext4 still touches a real chunk at mkfs) they
    # dwarf the actual events.bin (single-digit MB), so leaving them behind
    # is pure waste, not caution.
    scratch.unlink(missing_ok=True)
    results.unlink(missing_ok=True)

    chown_out_back()
    return 0


if __name__ == "__main__":
    sys.exit(main())
