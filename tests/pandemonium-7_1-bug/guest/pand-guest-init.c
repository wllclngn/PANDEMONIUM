// pand-guest-init.c -- PID 1 for the 7.1-bug reproducer guest.
//
// Static-built (the guest carries no libc loader, no shell, no python). A fixed
// boot sequence, parameterised only by kernel cmdline `pand.*` keys the host
// sets via qemu -append. montauk runs IN the guest (kstrand needs the guest's
// own per-CPU kthreads); its raw --trace-out lands on the 9p /out share, and the
// host runs montauk_analyze --report kstrand on it afterward. Ported in spirit
// from tests/prism-strand.py's probe flow.
//
// cmdline keys (all optional, defaults below):
//   pand.sched=pandemonium|none   load PANDEMONIUM, or leave the in-kernel default
//   pand.mode=held|dark           strand-load mode
//   pand.hogs=N                   HELD-mode CPU hogs (default online-1)
//   pand.writers=N                fsync writers (default 4)
//   pand.duration=N               seconds under capture (default 60)
//   pand.idle_ms=N                DARK-mode idle gap (default 150)

#define _GNU_SOURCE
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

// PATHS BAKED INTO THE INITRAMFS BY build-guest.py
#define PANDEMONIUM "/bin/pandemonium"
#define MONTAUK     "/bin/montauk"
#define STRAND      "/bin/pand-strand-load"
#define IOFLOOD     "/bin/pand-io-flood"
#define KWINPROXY   "/bin/pand-kwin-proxy"
#define IDLEPING    "/bin/pand-idle-ping"
#define LOCKBLOCK   "/bin/pand-lock-block"
#define SCRATCH     "/scratch"
#define OUT         "/out"

static char g_cmdline[4096];

// Read the value for `pand.<key>=` out of /proc/cmdline into dst.
static void cmdline_get(const char *key, char *dst, size_t n, const char *dflt) {
    char needle[64];
    snprintf(needle, sizeof(needle), "pand.%s=", key);
    char *p = strstr(g_cmdline, needle);
    if (!p) {
        snprintf(dst, n, "%s", dflt);
        return;
    }
    p += strlen(needle);
    size_t i = 0;
    while (*p && *p != ' ' && *p != '\n' && i + 1 < n)
        dst[i++] = *p++;
    dst[i] = '\0';
}

static void say(const char *msg) {
    write(1, "[pand-init] ", 12);
    write(1, msg, strlen(msg));
    write(1, "\n", 1);
}

// fork+exec, return child pid (0 on failure). argv NULL-terminated.
static pid_t spawn(char *const argv[]) {
    pid_t p = fork();
    if (p == 0) {
        execv(argv[0], argv);
        _exit(127);
    }
    return p > 0 ? p : 0;
}

static int spawn_wait(char *const argv[]) {
    pid_t p = spawn(argv);
    if (!p)
        return -1;
    int st = 0;
    waitpid(p, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

static void stop(pid_t p) {
    if (!p)
        return;
    kill(p, SIGTERM);
    for (int i = 0; i < 50; i++) {  // up to 5s for a clean detach
        if (waitpid(p, NULL, WNOHANG) == p)
            return;
        usleep(100000);
    }
    kill(p, SIGKILL);
    waitpid(p, NULL, 0);
}

static int online_cpus(void) {
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 1;
}

int main(void) {
    // 1. PSEUDO-FILESYSTEMS
    mkdir("/proc", 0755);
    mkdir("/sys", 0755);
    mkdir("/dev", 0755);
    mkdir("/tmp", 0755);
    mkdir(SCRATCH, 0755);
    mkdir(OUT, 0755);
    mount("proc", "/proc", "proc", 0, NULL);
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
    mount("bpf", "/sys/fs/bpf", "bpf", 0, NULL);
    // tracefs + debugfs so libbpf can resolve tracepoint perf-event IDs.
    // montauk attaches sched/* tracepoints; without these the ID lookup is
    // -ENOENT and the BPF attach fails (misreported as a caps error).
    mount("tracefs", "/sys/kernel/tracing", "tracefs", 0, NULL);
    mount("debugfs", "/sys/kernel/debug", "debugfs", 0, NULL);
    // fully permissive perf -- this is a throwaway repro guest, and montauk's
    // perf_event_open must never trip paranoia.
    int pf = open("/proc/sys/kernel/perf_event_paranoid", O_WRONLY);
    if (pf >= 0) {
        write(pf, "-1\n", 3);
        close(pf);
    }

    // 2. CMDLINE
    int fd = open("/proc/cmdline", O_RDONLY);
    if (fd >= 0) {
        ssize_t r = read(fd, g_cmdline, sizeof(g_cmdline) - 1);
        if (r > 0)
            g_cmdline[r] = '\0';
        close(fd);
    }
    char sched[64], mode[16], hogs[16], writers[16], duration[16], idle[16];
    cmdline_get("sched", sched, sizeof(sched), "pandemonium");
    cmdline_get("mode", mode, sizeof(mode), "dark");
    cmdline_get("writers", writers, sizeof(writers), "4");
    cmdline_get("duration", duration, sizeof(duration), "60");
    cmdline_get("idle_ms", idle, sizeof(idle), "150");
    char hogs_default[16];
    snprintf(hogs_default, sizeof(hogs_default), "%d", online_cpus() - 1);
    cmdline_get("hogs", hogs, sizeof(hogs), hogs_default);
    // io-flood workload params (pand.workload=ioflood)
    char workload[16], iomode[16], issuers[16], block_kb[16];
    cmdline_get("workload", workload, sizeof(workload), "strand");
    cmdline_get("iomode", iomode, sizeof(iomode), "all");
    char issuers_default[16];
    snprintf(issuers_default, sizeof(issuers_default), "%d", online_cpus() * 8);
    cmdline_get("issuers", issuers, sizeof(issuers), issuers_default);
    cmdline_get("block_kb", block_kb, sizeof(block_kb), "4");
    // filesystem + Wayland compositor-proxy params
    char fs[16], wayland[8], frame_us[16];
    cmdline_get("fs", fs, sizeof(fs), "ext4");
    cmdline_get("wayland", wayland, sizeof(wayland), "0");
    cmdline_get("frame_us", frame_us, sizeof(frame_us), "16667");
    // Finding-2-targeted induction (pand-idle-ping): off by default.
    char idleping[8], ping_pairs[16];
    cmdline_get("idleping", idleping, sizeof(idleping), "0");
    cmdline_get("ping_pairs", ping_pairs, sizeof(ping_pairs), "4");
    // Finding-4-targeted induction (pand-lock-block): off by default.
    char lockblock[8], lock_threads[16];
    cmdline_get("lockblock", lockblock, sizeof(lockblock), "0");
    cmdline_get("lock_threads", lock_threads, sizeof(lock_threads), "8");
    // MONTAUK_SCX_STORM opt-in (Finding 5 re-test): off by default.
    char scxstorm[8];
    cmdline_get("scxstorm", scxstorm, sizeof(scxstorm), "0");

    // 3. EXT4 SCRATCH on virtio-blk (/dev/vda). ext4 + virtio-blk are built
    // into the CachyOS kernel (=y); XFS + 9p are modules (=m) the bare initramfs
    // cannot load. The block-I/O-completion + writeback kthreads strand the same
    // on ext4. montauk's events.bin and the done marker land here; the host pulls
    // them with debugfs (rootless) after poweroff.
    // XFS is a module (Piotr's disks are xfs); ext4 is built in. Load xfs.ko
    // via finit_module before mounting when pand.fs=xfs.
    if (strcmp(fs, "xfs") == 0) {
        int mfd = open("/xfs.ko", O_RDONLY);
        if (mfd >= 0) {
            if (syscall(SYS_finit_module, mfd, "", 0) != 0)
                say("WARN: xfs module load failed");
            else
                say("xfs module loaded");
            close(mfd);
        } else {
            say("WARN: /xfs.ko missing -- rebuild initramfs with --xfs-kver");
        }
    }
    if (mount("/dev/vda", SCRATCH, fs, 0, NULL) != 0)
        say("WARN: scratch mount failed -- no writeback path / no results");
    // Results disk: ext4 on /dev/vdb, ALWAYS ext4. debugfs (host, rootless)
    // reads ext4 but not xfs, so montauk's events.bin + the kwin overruns land
    // here while the flood's I/O hits the fs-under-test on vda.
    if (mount("/dev/vdb", OUT, "ext4", 0, NULL) != 0)
        say("WARN: ext4 /dev/vdb (results) mount failed");

    // 4. LOAD PANDEMONIUM (stays resident holding the BPF attached)
    pid_t sched_pid = 0;
    if (strcmp(sched, "none") != 0) {
        say("loading PANDEMONIUM");
        char *argv[] = {(char *)PANDEMONIUM, NULL};
        sched_pid = spawn(argv);
        sleep(4);  // let struct_ops attach before load ramps
    } else {
        say("sched=none -- in-kernel default (control arm)");
    }

    // 5. MONTAUK TRACE (raw event log to the host-shared /out)
    // --sched-detail: without it, SCHED_OP_SWITCH_IN (the sched_switch-derived
    // pick CpuHolderLedger needs to name who held a CPU through a kstrand
    // wait) and CPU_IDLE (dispatch-stall's DARK-vs-HELD split) are both
    // suppressed by default -- confirmed by reading montauk_trace.bpf.c, not
    // assumed. Without this flag every kstrand/dispatch-stall verdict from
    // this guest is holder-blind (held_by always resolves to the tid=0
    // sentinel). The volume tradeoff the gate exists for is fine at this
    // harness's short, bounded durations.
    // "pand-" (not "pand-ioflood") so the kwin-proxy (renamed "pand-kwin" via
    // prctl, see pand-kwin-proxy.c) is tracked too, not just the io-flood
    // workers -- a multi-second real-world kwin_overruns.txt spike traced
    // back to this gap: dispatch-stall showed nothing near that magnitude
    // because montauk had never been tracking the compositor process at all.
    int ioflood = (strcmp(workload, "ioflood") == 0);
    char *pattern = ioflood ? (char *)"pand-" : (char *)"pand-strand";
    say(ioflood ? "starting montauk --trace pand- --sched-detail --stream-out /dev/ttyS1"
                : "starting montauk --trace pand-strand --sched-detail --stream-out /dev/ttyS1");
    // --stream-out /dev/ttyS1: a second, independent copy of the trace,
    // written to the qemu-backed second serial port instead of the ext4
    // results disk. --trace-out's durability depends on the guest's own
    // filesystem eventually committing it -- exactly what a kernel hang can
    // take down with it. The character device write lands directly in
    // qemu's host-side backing file, surviving a hang that leaves
    // /out/events.bin empty.
    // MONTAUK_SCX_STORM stays OFF by default (pand.scxstorm=0): the first
    // induction attempt with it enabled produced a real, reproducible kernel
    // oops in bpf_prog_get_assoc_struct_ops (called from scx_bpf_kick_cpu),
    // distinct from the RCU-stall/softlockup chain this harness otherwise
    // targets -- see UPSTREAM_REPORT.md's Finding 5. Gated behind its own
    // cmdline toggle (rather than a hardcoded setenv) so a single, isolated,
    // deliberate re-test (does that oops still reproduce on the corrected,
    // non-oversubscribed topology?) doesn't require editing this file.
    if (strcmp(scxstorm, "0") != 0)
        setenv("MONTAUK_SCX_STORM", "1", 1);
    char *mk_argv[] = {(char *)MONTAUK, "--headless", "--trace", pattern,
                       "--sched-detail",
                       "--trace-out", (char *)(OUT "/events.bin"),
                       "--stream-out", (char *)"/dev/ttyS1", NULL};
    pid_t mk_pid = spawn(mk_argv);
    // Pin montauk to CPU 0 -- the one CPU never in the nohz_full=1-{smp-1}
    // range this harness boots with. The --stream-out sink only survives a
    // hang if the thread emitting to it keeps running; a montauk left to
    // float onto a NOHZ_FULL CPU could itself be the one that goes dark.
    if (mk_pid) {
        cpu_set_t cpus;
        CPU_ZERO(&cpus);
        CPU_SET(0, &cpus);
        sched_setaffinity(mk_pid, sizeof(cpus), &cpus);
    }
    sleep(1);

    // TEST-ONLY: deliberate induction of Finding 1 ONLY (chronic rcu_preempt
    // starvation on CPU 0, the sole housekeeping CPU under nohz_full=1-N).
    // NOT a lever for Finding 2 (the idle-kick race needs a CPU to actually
    // take balance_one()'s empty/idle path, which a permanently-busy CPU 0
    // never does) or Finding 4 (the RCU stall needs a task blocked while
    // holding a lock, which a bare spin loop never produces) -- see the
    // panel-review notes in UPSTREAM_REPORT.md's induction section. Several tight
    // busy-spins pinned to CPU 0 compete directly with rcu_preempt for that
    // one scarce CPU, on demand rather than waiting on however often the
    // natural workload happens to produce enough contention. These never
    // exit on their own (infinite loop) -- hog_pids[] below MUST be reaped
    // before teardown or they run forever and can stall the whole guest's
    // shutdown by starving whatever CPU-0-housekeeping the rest of teardown
    // needs (found the hard way: a real, reproducible harness hang, not a
    // kernel finding). Remove once the induction capture is complete.
    pid_t hog_pids[4] = {0};
    if (ioflood) {
        for (int hog_n = 0; hog_n < 4; hog_n++) {
            pid_t hog_pid = fork();
            if (hog_pid < 0) {
                hog_pid = 0;  // match spawn()'s failure convention: 0, never -1
            } else if (hog_pid == 0) {
                cpu_set_t cpus;
                CPU_ZERO(&cpus);
                CPU_SET(0, &cpus);
                sched_setaffinity(0, sizeof(cpus), &cpus);
                for (;;) {
                    /* spin */
                }
            }
            hog_pids[hog_n] = hog_pid;
        }
    }

    // TEST-ONLY: targeted induction of Finding 2 specifically (the
    // can_skip_idle_kick race), separate from the Finding-1 hog above.
    // Starts at CPU 1 (CPU 0 is the hog's), so with the default 4 pairs it
    // occupies CPUs 1-8 -- leaves the io-flood/kwin-proxy/PANDEMONIUM/montauk
    // processes the remaining CPUs on a 12-vCPU guest. Off by default
    // (pand.idleping=0); enable with pand.idleping=1. Remove once the
    // induction capture is complete.
    pid_t ping_pid = 0;
    if (ioflood && strcmp(idleping, "0") != 0) {
        say("+ idle-ping (Finding 2 targeted induction)");
        char *ping_argv[] = {(char *)IDLEPING, duration, ping_pairs,
                             (char *)"1", NULL};
        ping_pid = spawn(ping_argv);
    }

    // TEST-ONLY: targeted induction of Finding 4 specifically (the real
    // captured RCU stall: a task blocked while holding a lock). Off by
    // default (pand.lockblock=0); enable with pand.lockblock=1. Remove once
    // the induction capture is complete.
    pid_t lockblock_pid = 0;
    if (ioflood && strcmp(lockblock, "0") != 0) {
        say("+ lock-block (Finding 4 targeted induction)");
        char *lb_argv[] = {(char *)LOCKBLOCK, duration, lock_threads,
                           block_kb, (char *)SCRATCH, NULL};
        lockblock_pid = spawn(lb_argv);
    }

    // 6. WORKLOAD -- the fsync/writeback strand storm, or the I/O flood
    if (ioflood) {
        // Wayland compositor proxy: a 60fps latency victim that records its own
        // wake overruns -- the kwin_wayland stall, measured directly.
        pid_t kwin_pid = 0;
        if (strcmp(wayland, "0") != 0) {
            say("+ kwin compositor proxy (60fps latency victim)");
            char *kw_argv[] = {(char *)KWINPROXY, duration, frame_us,
                               (char *)OUT, NULL};
            kwin_pid = spawn(kw_argv);
        }
        say("running io-flood");
        char *fl_argv[] = {(char *)IOFLOOD, iomode, issuers, duration, block_kb,
                           (char *)SCRATCH, NULL};
        spawn_wait(fl_argv);
        if (kwin_pid)
            stop(kwin_pid);
        if (ping_pid)
            stop(ping_pid);
        if (lockblock_pid)
            stop(lockblock_pid);
    } else {
        say("running strand load");
        char *ld_argv[] = {(char *)STRAND, mode, hogs, writers, duration, idle,
                           (char *)SCRATCH, NULL};
        spawn_wait(ld_argv);
    }

    // 7. TEARDOWN -- reap the induction hogs first (they never exit on
    // their own; see the comment at their spawn site), then montauk (flush
    // + detach BPF), then PANDEMONIUM.
    for (int hog_n = 0; hog_n < 4; hog_n++)
        if (hog_pids[hog_n])
            stop(hog_pids[hog_n]);
    say("stopping montauk (flush + detach)");
    stop(mk_pid);
    if (sched_pid) {
        say("detaching PANDEMONIUM");
        stop(sched_pid);
    }

    // 8. PERSIST + POWEROFF
    int df = open(OUT "/done", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (df >= 0) {
        write(df, "1\n", 2);
        close(df);
    }
    sync();
    umount(SCRATCH);
    umount(OUT);
    say("done -- poweroff");
    reboot(RB_POWER_OFF);
    for (;;)
        pause();
    return 0;
}
