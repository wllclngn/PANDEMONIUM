// pand-lock-block.c -- targeted induction for Finding 4 (the real kernel RCU
// CPU stall report captured this session: "Tasks blocked on level-0 rcu_node
// ...: P205/1:b..l", i.e. a task blocked while holding a lock). io-flood's
// own issuers each use a private file and never share a lock, so nothing in
// the existing workload structurally approaches "blocked while holding a
// lock, others piled up behind it" -- this does, directly.
//
// All threads share ONE file and ONE pthread_mutex. Each thread: acquire the
// mutex, then perform a real O_DIRECT write (a genuine, potentially slow
// block-I/O completion wait, D-state in the kernel) while STILL HOLDING the
// mutex, then release. Every other thread piles up on the mutex's own futex
// wait for the duration of that write -- a real lock convoy gated on I/O
// completion, not a userspace-only spin. This is the closest userspace-
// inducible approximation of the captured signature; it cannot directly
// replicate an RCU read-side critical section, which is kernel-internal.
//
// usage: pand-lock-block DURATION_S NUM_THREADS BLOCK_KB SCRATCH_DIR [IOPS]
//   IOPS (optional, default 0 = unthrottled): caps this shared file's write
//   rate to simulate a slower or contended device, the same role
//   PAND_IOPS/virtio-blk throttling.iops-total plays in the VM harness. Real,
//   untethered host storage completes each O_DIRECT write fast enough that
//   the mutex hold time per iteration may be too short to matter; this
//   restores the same per-write minimum latency without needing root/cgroup
//   configuration on the host.

#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

static long g_deadline_ms;
static int g_fd;
static int g_block;
static long g_min_interval_us;  // 0 = unthrottled
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

static long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static long now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000000L + ts.tv_nsec / 1000L;
}

struct thread_arg {
    int id;
};

static void *worker_fn(void *p)
{
    struct thread_arg *a = p;
    prctl(PR_SET_NAME, "pand-lockblk", 0, 0, 0);
    void *buf = NULL;
    if (posix_memalign(&buf, 4096, g_block))
        return NULL;
    memset(buf, a->id & 0xff, g_block);
    off_t off = (off_t)a->id * g_block;
    while (now_ms() < g_deadline_ms) {
        pthread_mutex_lock(&g_lock);
        long t0 = now_us();
        pwrite(g_fd, buf, g_block, off);  // blocks (D-state) with the lock held
        if (g_min_interval_us > 0) {
            long elapsed = now_us() - t0;
            if (elapsed < g_min_interval_us)
                usleep(g_min_interval_us - elapsed);  // still holding g_lock
        }
        pthread_mutex_unlock(&g_lock);
    }
    free(buf);
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc < 5) {
        fprintf(stderr,
                "usage: %s DURATION_S NUM_THREADS BLOCK_KB SCRATCH_DIR [IOPS]\n",
                argv[0]);
        return 1;
    }
    int duration_s = atoi(argv[1]);
    int num_threads = atoi(argv[2]);
    g_block = atoi(argv[3]) * 1024;
    const char *scratch = argv[4];
    int iops = argc > 5 ? atoi(argv[5]) : 0;
    g_min_interval_us = iops > 0 ? 1000000L / iops : 0;

    prctl(PR_SET_NAME, "pand-lockblk", 0, 0, 0);

    char path[256];
    snprintf(path, sizeof path, "%s/lock-block-shared", scratch);
    g_fd = open(path, O_RDWR | O_CREAT | O_DIRECT | O_TRUNC, 0644);
    if (g_fd < 0)
        g_fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);  // fs without O_DIRECT
    if (g_fd < 0)
        return 1;

    g_deadline_ms = now_ms() + duration_s * 1000L;

    pthread_t *tids = calloc(num_threads, sizeof(pthread_t));
    struct thread_arg *args = calloc(num_threads, sizeof(struct thread_arg));
    for (int i = 0; i < num_threads; i++) {
        args[i].id = i;
        pthread_create(&tids[i], NULL, worker_fn, &args[i]);
    }
    for (int i = 0; i < num_threads; i++)
        pthread_join(tids[i], NULL);

    close(g_fd);
    unlink(path);
    return 0;
}
