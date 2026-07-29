// pand-io-flood.c -- maximal I/O-completion flood for the 7.1+ kernel-bug hunt.
//
// The target is the submit -> block -> complete -> wake -> dispatch chain: many
// oversubscribed issuers each hammering the block layer so the scheduler is
// forced into constant dispatch decisions under the kernel's I/O-completion
// machinery. Runs on ext4-on-virtio-blk; under a qemu iops throttle the
// completions are latent, so waiters park and montauk's wake-to-run / kstrand /
// dispatch-stall reports light up. Comm is set to "pand-ioflood" for --trace.
//
// usage: pand-io-flood MODE ISSUERS DURATION_S BLOCK_KB SCRATCH_DIR
//   MODE = direct | fsync | fork | all
//     direct : O_DIRECT writes+reads -- every op hits the device + completes
//     fsync  : buffered write + fsync -- the writeback/completion path
//     fork   : short-lived sub-issuers (fork, K fsync ops, exit) -- compile churn
//     all    : issuers split evenly across the three (mixed maximal flood)

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int g_block;
static long g_deadline_ms;
static long g_start_ms;
static long g_burst_ms;     // 0 = continuous (the historical behavior)
static long g_quiesce_ms;
static const char *g_scratch;

static long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

// Burst mode: issue for burst_s, then every issuer idles quiesce_s, repeat
// until the deadline. The quiesce EDGE is the point: load draining off a CPU
// is the transition where a stopped tick can strand whatever is still queued,
// and a bursting run crosses that edge once per cycle instead of once per
// boot. All issuers share the same wall-clock phase, so the drains are
// system-wide, like a desktop going quiet.
static void wait_if_quiesce(void)
{
    if (g_burst_ms <= 0)
        return;
    for (;;) {
        long now = now_ms();
        if (now >= g_deadline_ms)
            return;
        long phase = (now - g_start_ms) % (g_burst_ms + g_quiesce_ms);
        if (phase < g_burst_ms)
            return;
        struct timespec ts = {0, 100 * 1000 * 1000};
        nanosleep(&ts, NULL);
    }
}

// O_DIRECT: aligned buffer, aligned 4K-multiple ops -- forces real block I/O.
static void issuer_direct(int id)
{
    char path[256];
    snprintf(path, sizeof path, "%s/iof-d-%d", g_scratch, id);
    int fd = open(path, O_RDWR | O_CREAT | O_DIRECT | O_TRUNC, 0644);
    if (fd < 0)
        fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);  // fs without O_DIRECT
    if (fd < 0)
        return;
    void *buf = NULL;
    if (posix_memalign(&buf, 4096, g_block)) {
        close(fd);
        return;
    }
    memset(buf, id & 0xff, g_block);
    off_t off = 0;
    while (now_ms() < g_deadline_ms) {
        wait_if_quiesce();
        pwrite(fd, buf, g_block, off);
        pread(fd, buf, g_block, off);
        off += g_block;
        if (off > (off_t)g_block * 256)
            off = 0;
    }
    close(fd);
    free(buf);
    unlink(path);
}

// buffered write + fsync -- writeback + fsync-completion path.
static void issuer_fsync(int id)
{
    char path[256];
    snprintf(path, sizeof path, "%s/iof-f-%d", g_scratch, id);
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return;
    char *buf = malloc(g_block);
    memset(buf, id & 0xff, g_block);
    off_t off = 0;
    while (now_ms() < g_deadline_ms) {
        wait_if_quiesce();
        pwrite(fd, buf, g_block, off);
        fsync(fd);
        off += g_block;
        if (off > (off_t)g_block * 256)
            off = 0;
    }
    close(fd);
    free(buf);
    unlink(path);
}

// short-lived sub-issuers: fork, do K fsync ops, exit -- fork+I/O+wake churn.
static void issuer_fork(int id)
{
    while (now_ms() < g_deadline_ms) {
        wait_if_quiesce();
        pid_t c = fork();
        if (c == 0) {
            char path[256];
            snprintf(path, sizeof path, "%s/iof-k-%d-%d", g_scratch, id,
                     (int)getpid());
            int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
            if (fd >= 0) {
                char *buf = malloc(g_block);
                memset(buf, id & 0xff, g_block);
                for (int k = 0; k < 20; k++) {
                    pwrite(fd, buf, g_block, (off_t)k * g_block);
                    fsync(fd);
                }
                free(buf);
                close(fd);
                unlink(path);
            }
            _exit(0);
        }
        if (c > 0)
            waitpid(c, NULL, 0);
    }
}

int main(int argc, char **argv)
{
    prctl(PR_SET_NAME, "pand-ioflood", 0, 0, 0);
    if (argc < 6) {
        fprintf(stderr,
                "usage: %s MODE ISSUERS DURATION_S BLOCK_KB SCRATCH [BURST_S QUIESCE_S]\n", argv[0]);
        return 1;
    }
    const char *mode = argv[1];
    int issuers = atoi(argv[2]);
    int duration = atoi(argv[3]);
    g_block = atoi(argv[4]) * 1024;
    g_scratch = argv[5];
    if (g_block < 4096)
        g_block = 4096;
    if (argc >= 8) {
        g_burst_ms = atol(argv[6]) * 1000L;
        g_quiesce_ms = atol(argv[7]) * 1000L;
    }
    g_start_ms = now_ms();
    g_deadline_ms = g_start_ms + (long)duration * 1000L;

    void (*base)(int) = issuer_fsync;
    if (!strcmp(mode, "direct"))
        base = issuer_direct;
    else if (!strcmp(mode, "fork"))
        base = issuer_fork;

    for (int i = 0; i < issuers; i++) {
        void (*fn)(int) = base;
        if (!strcmp(mode, "all")) {
            int m = i % 3;
            fn = (m == 0) ? issuer_direct
               : (m == 1) ? issuer_fsync
                          : issuer_fork;
        }
        pid_t p = fork();
        if (p == 0) {
            prctl(PR_SET_NAME, "pand-ioflood", 0, 0, 0);
            fn(i);
            _exit(0);
        }
    }
    for (int i = 0; i < issuers; i++)
        wait(NULL);
    return 0;
}
