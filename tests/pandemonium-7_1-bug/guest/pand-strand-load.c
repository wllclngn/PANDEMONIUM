// pand-strand-load.c -- writeback-freeze load generator for the PANDEMONIUM
// 7.1+ kernel-bug reproducer. Static-built; runs inside the guest VM, which
// carries no python. Ported from tests/prism-strand.py's _cpu_hog / _io_writer
// / _io_writer_bursty so the SAME I/O-completion-strand condition is driven in
// the guest as on bare metal.
//
// HELD mode: peg (hogs) cores and run (writers) fsync loops. Every per-CPU
//   block-I/O-completion and writeback kthread (nr_cpus_allowed == 1) must be
//   dispatched against a running hog -- the HELD strand.
// DARK mode: no hogs; bursty writers fsync then sleep so their CPU goes idle
//   and tickless. The pinned completion/writeback kthread then becomes runnable
//   on an un-ticked CPU where no tick-driven rescue scan fires -- the NO_HZ_FULL
//   strand, the freeze condition that appears on 7.1+.
//
// Process name is set to "pand-strand" so `montauk --trace pand-strand`
// attaches to the load; montauk_analyze --report kstrand reads the strands.

#define _GNU_SOURCE
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const char *COMM = "pand-strand";

static void set_comm(void) { prctl(PR_SET_NAME, COMM, 0, 0, 0); }

static void cpu_hog(int core) {
    set_comm();
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    sched_setaffinity(0, sizeof(set), &set);
    volatile unsigned long x = 0;
    for (;;)
        x = x * 1103515245UL + 12345UL;
}

static void io_writer(const char *path, int bursty, double idle_s) {
    set_comm();
    static char buf[65536];
    memset(buf, 0, sizeof(buf));
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        _exit(1);
    off_t off = 0;
    struct timespec ts;
    ts.tv_sec = (time_t)idle_s;
    ts.tv_nsec = (long)((idle_s - (double)(time_t)idle_s) * 1e9);
    const off_t cap = bursty ? (64L << 20) : (256L << 20);
    const int burst = bursty ? 8 : 1;
    for (;;) {
        for (int i = 0; i < burst; i++) {
            if (pwrite(fd, buf, sizeof(buf), off) < 0)
                _exit(0);
            fsync(fd);
            off += (off_t)sizeof(buf);
            if (off > cap)
                off = 0;
        }
        if (bursty)
            nanosleep(&ts, NULL);
    }
}

int main(int argc, char **argv) {
    if (argc < 7) {
        fprintf(stderr,
                "usage: %s <held|dark> <hogs> <writers> <duration_s> "
                "<idle_ms> <scratch_dir>\n",
                argv[0]);
        return 2;
    }
    int dark = (strcmp(argv[1], "dark") == 0);
    int hogs = atoi(argv[2]);
    int writers = atoi(argv[3]);
    int duration = atoi(argv[4]);
    double idle_s = atof(argv[5]) / 1000.0;
    const char *scratch = argv[6];

    int nproc = 0;
    pid_t pids[4096];

    // DARK mode runs NO hogs -- the cores must be free to idle/go tickless.
    if (!dark) {
        for (int c = 0; c < hogs && nproc < 4096; c++) {
            pid_t p = fork();
            if (p == 0)
                cpu_hog(c);
            if (p > 0)
                pids[nproc++] = p;
        }
    }
    for (int w = 0; w < writers && nproc < 4096; w++) {
        char path[512];
        snprintf(path, sizeof(path), "%s/wb-%d.dat", scratch, w);
        pid_t p = fork();
        if (p == 0)
            io_writer(path, dark, idle_s);
        if (p > 0)
            pids[nproc++] = p;
    }

    printf("[pand-strand-load] mode=%s hogs=%d writers=%d duration=%ds "
           "idle=%.0fms scratch=%s\n",
           dark ? "dark" : "held", dark ? 0 : hogs, writers, duration,
           idle_s * 1000.0, scratch);
    fflush(stdout);

    sleep(duration);

    for (int i = 0; i < nproc; i++)
        kill(pids[i], SIGKILL);
    for (int i = 0; i < nproc; i++)
        waitpid(pids[i], NULL, 0);

    for (int w = 0; w < writers; w++) {
        char path[512];
        snprintf(path, sizeof(path), "%s/wb-%d.dat", scratch, w);
        unlink(path);
    }
    return 0;
}
