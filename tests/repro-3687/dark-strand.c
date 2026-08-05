// dark-strand.c -- minimal reproducer for the DARK stranding in
// sched-ext/scx#3687. Reduced from the I/O-flood workload that produced the
// captures reported in that issue by removing everything that turned out not to
// be load-bearing: the O_DIRECT and fork-churn issuer modes, burst/quiesce
// phasing, and the block-size knob. What is left is the smallest form still
// observed to strand.
//
// THE CONDITION: a per-CPU block-I/O completion kworker becomes runnable on a
// nohz_full CPU that has just gone tickless-idle, and nothing dispatches it --
// no tick, no rescue scan, no kick. The writer waiting on that completion stays
// in D-state until something unrelated wakes the CPU.
//
// THREE THINGS MATTER AND ARE NOT KNOBS:
//   * OVERSUBSCRIPTION. Issuers >> CPUs. The default 128 on 12 CPUs is what the
//     reported captures ran.
//   * NO SLEEP. Issuers hammer continuously. An earlier attempt at this file
//     had each issuer sleep between operations on the theory that the idle gap
//     created the strand; that theory was wrong, and a sleeping workload only
//     reproduces device backpressure.
//   * NO PINNING. Issuers are never bound to a CPU. Where the scheduler places
//     them is the thing under test; pinning removes it from the experiment.
//
// THIS PROGRAM DOES NOT DECIDE WHETHER THE BUG OCCURRED. It prints the fsync
// latency distribution it observed and nothing more. fsync latency conflates
// scheduler stranding with block-device queueing, so a single arm's numbers are
// not evidence on their own -- RUN BOTH ARMS on the same kernel and cmdline and
// compare. The finding is the difference between them, not any one threshold.
// Reading the strand directly requires watching dispatch (which CPU was idle,
// which kthread was runnable, for how long); that is what montauk's kstrand
// report does in the full harness.
//
//   cc -O2 -o dark-strand dark-strand.c
//   ./dark-strand <seconds> <issuers> <scratch_dir>
//
// Requires nohz_full= on the cmdline and a scratch dir on a real block device.

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/vfs.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define BLOCK    65536
#define MAXSAMP  20000

static long g_deadline_ms;

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

static int cmp_d(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

// One issuer: buffered write + fsync, continuously, wherever the scheduler puts
// it. This is the flood workload's fsync issuer with the timing kept.
static int issuer(int id, const char *dir, double *out, int max)
{
    char path[256];
    snprintf(path, sizeof path, "%s/ds-%d", dir, id);
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return 0;
    char *buf = malloc(BLOCK);
    if (!buf) {
        close(fd);
        return 0;
    }
    memset(buf, id & 0xff, BLOCK);

    off_t off = 0;
    int n = 0;
    while (now_ms() < g_deadline_ms) {
        pwrite(fd, buf, BLOCK, off);
        double t0 = now_ms();
        fsync(fd);
        if (n < max)
            out[n++] = now_ms() - t0;
        off += BLOCK;
        if (off > (off_t)BLOCK * 256)
            off = 0;
    }
    free(buf);
    close(fd);
    unlink(path);
    return n;
}

int main(int argc, char **argv)
{
    prctl(PR_SET_NAME, "dark-strand", 0, 0, 0);
    if (argc < 4) {
        fprintf(stderr, "usage: %s <seconds> <issuers> <scratch_dir>\n"
                        "  needs nohz_full= on the cmdline and a scratch dir on\n"
                        "  a real block device (not tmpfs). 128 issuers is what\n"
                        "  the reported captures ran on 12 CPUs.\n", argv[0]);
        return 2;
    }
    int seconds = atoi(argv[1]);
    int issuers = atoi(argv[2]);
    const char *dir = argv[3];
    if (seconds <= 0 || issuers <= 0)
        return 2;

    // Refuse a memory-backed scratch dir: fsync is a no-op there, so there is no
    // block-completion kworker to strand and the run measures nothing.
    struct statfs sfs;
    if (statfs(dir, &sfs) != 0) {
        fprintf(stderr, "cannot stat %s\n", dir);
        return 2;
    }
    if (sfs.f_type == 0x01021994 /* TMPFS_MAGIC */ ||
        sfs.f_type == 0x858458f6 /* RAMFS_MAGIC */) {
        fprintf(stderr, "%s is memory-backed -- fsync is a no-op there and this\n"
                        "run would measure nothing. Use a real block device.\n", dir);
        return 2;
    }

    size_t slab = (size_t)MAXSAMP * sizeof(double);
    double *shared = mmap(NULL, slab * issuers + issuers * sizeof(int),
                          PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS,
                          -1, 0);
    if (shared == MAP_FAILED) {
        perror("mmap");
        return 2;
    }
    int *counts = (int *)(shared + (size_t)MAXSAMP * issuers);

    g_deadline_ms = (long)now_ms() + seconds * 1000L;
    printf("dark-strand: %ds, %d issuers, scratch %s\n", seconds, issuers, dir);
    printf("  unpinned and continuous by design -- see the header\n");
    fflush(stdout);

    for (int i = 0; i < issuers; i++) {
        pid_t p = fork();
        if (p == 0) {
            prctl(PR_SET_NAME, "dark-strand", 0, 0, 0);
            counts[i] = issuer(i, dir, shared + (size_t)MAXSAMP * i, MAXSAMP);
            _exit(0);
        }
    }
    for (int i = 0; i < issuers; i++)
        wait(NULL);

    int total = 0;
    for (int i = 0; i < issuers; i++)
        total += counts[i];
    if (total == 0) {
        fprintf(stderr, "no fsync completed -- is the scratch dir writable and\n"
                        "on a real block device?\n");
        return 2;
    }
    double *all = malloc((size_t)total * sizeof(double));
    if (!all)
        return 2;
    int k = 0;
    for (int i = 0; i < issuers; i++)
        for (int j = 0; j < counts[i]; j++)
            all[k++] = shared[(size_t)MAXSAMP * i + j];
    qsort(all, total, sizeof(double), cmp_d);

    printf("\nfsync latency over %d completions\n", total);
    printf("  p50   %9.1f ms\n", all[total / 2]);
    printf("  p99   %9.1f ms\n", all[(int)(total * 0.99)]);
    printf("  worst %9.1f ms\n", all[total - 1]);
    printf("\nRun the other scheduler on this same kernel and cmdline and\n"
           "compare. One arm's numbers alone are not evidence.\n");
    return 0;
}
