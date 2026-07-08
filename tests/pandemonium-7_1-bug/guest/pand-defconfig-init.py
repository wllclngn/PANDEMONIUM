#!/usr/bin/python3
# pand-defconfig-init.py -- PID 1 for the defconfig reproducer guest.
#
# The rootfs is a base-devel toolchain (built via docker) carrying python, so
# the init is python, not C. Fixed sequence: mount pseudo-fs, mount the ext4
# results disk, load PANDEMONIUM, trace with montauk, run a TIMED `make vmlinux`
# on the pre-configured tree at /build, write the wall-clock seconds + the trace
# to the results disk, poweroff. The host reads build_seconds + events.bin off
# the results image with debugfs. Config comes from the kernel cmdline (pand.*).
#
# cmdline keys:
#   pand.sched=pandemonium|none   load PANDEMONIUM, or the in-kernel default
#   pand.jobs=N                   make -j N (default: online CPUs)
#   pand.target=vmlinux           make target (default vmlinux)

import ctypes
import os
import signal
import subprocess
import time


def say(m):
    print(f"[pand-init] {m}", flush=True)


def mount(src, tgt, fs, opts=None):
    os.makedirs(tgt, exist_ok=True)
    cmd = ["mount", "-t", fs, src, tgt]
    if opts:
        cmd += ["-o", opts]
    subprocess.run(cmd, stderr=subprocess.DEVNULL)


def cmdline():
    d = {}
    try:
        for tok in open("/proc/cmdline").read().split():
            if tok.startswith("pand."):
                k, _, v = tok[5:].partition("=")
                d[k] = v
    except OSError:
        pass
    return d


def poweroff():
    os.sync()
    try:  # sysrq poweroff, belt-and-suspenders
        with open("/proc/sysrq-trigger", "w") as f:
            f.write("o")
    except OSError:
        pass
    try:
        # glibc reboot() takes a single howto arg and adds the magics itself.
        ctypes.CDLL("libc.so.6", use_errno=True).reboot(0x4321FEDC)  # POWER_OFF
    except OSError:
        pass
    while True:
        time.sleep(1)


def main():
    os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    mount("proc", "/proc", "proc")
    mount("sysfs", "/sys", "sysfs")
    mount("devtmpfs", "/dev", "devtmpfs")
    mount("tmpfs", "/tmp", "tmpfs")
    mount("bpf", "/sys/fs/bpf", "bpf")
    mount("tracefs", "/sys/kernel/tracing", "tracefs")
    mount("debugfs", "/sys/kernel/debug", "debugfs")
    try:
        open("/proc/sys/kernel/perf_event_paranoid", "w").write("-1")
    except OSError:
        pass

    cfg = cmdline()
    sched = cfg.get("sched", "pandemonium")
    jobs = cfg.get("jobs", str(os.cpu_count() or 4))
    target = cfg.get("target", "vmlinux")

    mount("/dev/vdb", "/out", "ext4")

    sched_p = None
    if sched != "none":
        say("loading PANDEMONIUM")
        sched_p = subprocess.Popen(["/usr/local/bin/pandemonium"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        time.sleep(4)
    else:
        say("sched=none -- in-kernel default (control arm)")

    mk = None
    try:
        mk = subprocess.Popen(
            ["/usr/local/bin/montauk", "--headless", "--trace", "make",
             "--trace-out", "/out/events.bin"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        say("montauk started")
    except OSError as e:
        say(f"montauk not started (build-time still measured): {e}")

    window = int(cfg.get("window", "0") or "0")
    say(f"make -j{jobs} {target}" + (f" window={window}s" if window else ""))
    t0 = time.monotonic()
    if window:
        # Throughput mode: build for a fixed window, then count objects. Faster
        # per cell than build-to-completion and always terminates; the kernel
        # regression shows as fewer objects compiled on the slow kernel.
        # start_new_session so the whole make tree (gcc/cc1 children) can be
        # killed as a group -- otherwise the children keep compiling and the
        # guest never quiesces for a clean poweroff.
        p = subprocess.Popen(
            ["make", "-C", "/build", "-s", f"-j{jobs}", "KCFLAGS=-Wno-error",
             target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        time.sleep(window)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    else:
        subprocess.run(
            ["make", "-C", "/build", "-s", f"-j{jobs}", "KCFLAGS=-Wno-error",
             target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    secs = time.monotonic() - t0
    nobj = 0
    for _, _, files in os.walk("/build"):
        nobj += sum(1 for f in files if f.endswith(".o"))
    say(f"build {secs:.1f}s objects={nobj}")
    try:
        open("/out/build_seconds", "w").write(f"{secs:.3f}\n")
        open("/out/object_count", "w").write(f"{nobj}\n")
    except OSError:
        pass

    if mk:
        mk.send_signal(signal.SIGTERM)
        try:
            mk.wait(5)
        except subprocess.TimeoutExpired:
            mk.kill()
    if sched_p:
        sched_p.send_signal(signal.SIGTERM)
        try:
            sched_p.wait(5)
        except subprocess.TimeoutExpired:
            sched_p.kill()

    try:
        open("/out/done", "w").write("1\n")
    except OSError:
        pass
    os.sync()
    subprocess.run(["umount", "/out"], stderr=subprocess.DEVNULL)
    say("poweroff")
    poweroff()


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        try:
            print(f"[pand-init] FATAL {e!r}", flush=True)
        except Exception:
            pass
        poweroff()
