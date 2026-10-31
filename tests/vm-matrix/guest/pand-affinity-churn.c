// pand-affinity-churn.c -- oversubscribed affinity churn for the vm-matrix guest.
//
// THREADS threads, more than the guest has CPUs, each restrict themselves to a
// random subset of 2..max(2, ncpu/2) CPUs, spin 20-200 us, sleep 50-500 us and
// pick a new subset every 64 iterations. Every wakeup then has to land inside a
// partial cpumask while most CPUs are contended, which is what drives a
// scheduler's affinity search and spill paths.
//
// usage: pand-affinity-churn DURATION_S THREADS [SEED]
//
// Each spin samples the CPU it runs on. A sample outside the thread's own mask
// is counted as outside_mask: the kernel should never let one happen.
//
// Exit 0 when the churn ran clean, 3 when any sample landed outside its mask,
// 4 when no affinity change was ever accepted (nothing was exercised); 1 and 2
// are setup and usage errors.

#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#define MAX_THREADS 4096
#define REPICK_EVERY 64

static int *g_cpus;        // CPUs the process may use at start
static int g_ncpu;
static int g_setcpus;      // cpu_set_t capacity, in CPUs
static uint64_t g_deadline_ns;

struct worker {
    pthread_t tid;
    int idx;
    int started;
    uint64_t rng;
    unsigned long long iters, repicks, setaff_failed, migrations, samples, outside;
};

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

// splitmix64: one 64-bit word of state per thread, no shared RNG.
static uint64_t rng_next(uint64_t *s) {
    uint64_t z = (*s += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static int rng_range(uint64_t *s, int lo, int hi) {
    return lo + (int)(rng_next(s) % (uint64_t)(hi - lo + 1));
}

static void *worker_fn(void *arg) {
    struct worker *w = arg;
    char name[16];
    snprintf(name, sizeof(name), "pand-affin-%u", (unsigned)w->idx % 10000u);
    prctl(PR_SET_NAME, name, 0, 0, 0);

    size_t setsz = CPU_ALLOC_SIZE(g_setcpus);
    cpu_set_t *set = CPU_ALLOC(g_setcpus);
    int *pool = malloc(sizeof(int) * (size_t)g_ncpu);
    if (!set || !pool) {
        CPU_FREE(set);
        free(pool);
        return NULL;
    }
    int kmax = g_ncpu / 2 > 2 ? g_ncpu / 2 : 2;
    if (kmax > g_ncpu)
        kmax = g_ncpu;
    int kmin = g_ncpu < 2 ? g_ncpu : 2;
    int last_cpu = -1;

    while (now_ns() < g_deadline_ns) {
        if (w->iters % REPICK_EVERY == 0) {
            int k = rng_range(&w->rng, kmin, kmax);
            memcpy(pool, g_cpus, sizeof(int) * (size_t)g_ncpu);
            CPU_ZERO_S(setsz, set);
            for (int i = 0; i < k; i++) {
                int j = rng_range(&w->rng, i, g_ncpu - 1);
                int t = pool[i];
                pool[i] = pool[j];
                pool[j] = t;
                CPU_SET_S(pool[i], setsz, set);
            }
            // On failure the mask in force is whatever it was; read it back
            // so outside_mask is judged against the real one.
            if (sched_setaffinity(0, setsz, set) != 0) {
                w->setaff_failed++;
                sched_getaffinity(0, setsz, set);
            }
            w->repicks++;
        }
        uint64_t spin_end = now_ns() + (uint64_t)rng_range(&w->rng, 20, 200) * 1000ULL;
        do {
            int cpu = sched_getcpu();
            w->samples++;
            if (cpu < 0)
                continue;
            if (cpu != last_cpu) {
                if (last_cpu >= 0)
                    w->migrations++;
                last_cpu = cpu;
            }
            if (cpu >= g_setcpus || !CPU_ISSET_S(cpu, setsz, set))
                w->outside++;
        } while (now_ns() < spin_end);
        struct timespec ts = {0, (long)rng_range(&w->rng, 50, 500) * 1000L};
        nanosleep(&ts, NULL);
        w->iters++;
    }
    CPU_FREE(set);
    free(pool);
    return NULL;
}

// The CPUs this process may run on, with a mask sized for the kernel's
// nr_cpu_ids (grown until sched_getaffinity accepts it).
static int init_cpus(void) {
    long conf = sysconf(_SC_NPROCESSORS_CONF);
    for (g_setcpus = conf > 0 ? (int)conf : 64; g_setcpus <= (1 << 20); g_setcpus *= 2) {
        size_t sz = CPU_ALLOC_SIZE(g_setcpus);
        cpu_set_t *set = CPU_ALLOC(g_setcpus);
        if (!set)
            return -1;
        if (sched_getaffinity(0, sz, set) == 0) {
            g_cpus = malloc(sizeof(int) * (size_t)g_setcpus);
            if (!g_cpus) {
                CPU_FREE(set);
                return -1;
            }
            for (int c = 0; c < g_setcpus; c++)
                if (CPU_ISSET_S(c, sz, set))
                    g_cpus[g_ncpu++] = c;
            CPU_FREE(set);
            return g_ncpu > 0 ? 0 : -1;
        }
        CPU_FREE(set);
    }
    return -1;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s DURATION_S THREADS [SEED]\n", argv[0]);
        return 2;
    }
    long duration = strtol(argv[1], NULL, 10);
    long threads = strtol(argv[2], NULL, 10);
    if (duration < 1 || threads < 1) {
        fprintf(stderr, "%s: DURATION_S and THREADS must be positive\n", argv[0]);
        return 2;
    }
    if (threads > MAX_THREADS)
        threads = MAX_THREADS;
    uint64_t seed = argc > 3 ? strtoull(argv[3], NULL, 0)
                             : now_ns() ^ ((uint64_t)getpid() << 32);
    prctl(PR_SET_NAME, "pand-affinity", 0, 0, 0);
    if (init_cpus() != 0) {
        fprintf(stderr, "%s: cannot read the process affinity mask\n", argv[0]);
        return 1;
    }

    struct worker *w = calloc((size_t)threads, sizeof(*w));
    if (!w) {
        fprintf(stderr, "%s: out of memory\n", argv[0]);
        return 1;
    }
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 256 * 1024);
    g_deadline_ns = now_ns() + (uint64_t)duration * 1000000000ULL;
    int started = 0;
    for (long i = 0; i < threads; i++) {
        w[i].idx = (int)i;
        w[i].rng = seed ^ (0x9e3779b97f4a7c15ULL * (uint64_t)(i + 1));
        if (pthread_create(&w[i].tid, &attr, worker_fn, &w[i]) == 0) {
            w[i].started = 1;
            started++;
        }
    }
    pthread_attr_destroy(&attr);
    if (!started) {
        fprintf(stderr, "%s: no thread could be created\n", argv[0]);
        return 1;
    }
    if (started < threads)
        fprintf(stderr, "%s: only %d of %ld threads started\n", argv[0], started, threads);

    unsigned long long iters = 0, repicks = 0, failed = 0, migrations = 0, samples = 0,
                       outside = 0;
    for (long i = 0; i < threads; i++) {
        if (!w[i].started)
            continue;
        pthread_join(w[i].tid, NULL);
        iters += w[i].iters;
        repicks += w[i].repicks;
        failed += w[i].setaff_failed;
        migrations += w[i].migrations;
        samples += w[i].samples;
        outside += w[i].outside;
    }
    printf("pand-affinity-churn: duration=%lds threads=%d cpus=%d seed=%llu iters=%llu "
           "repicks=%llu setaffinity_failed=%llu migrations=%llu samples=%llu "
           "outside_mask=%llu\n",
           duration, started, g_ncpu, (unsigned long long)seed, iters, repicks, failed,
           migrations, samples, outside);
    free(w);
    if (outside) {
        fprintf(stderr, "%s: %llu samples ran outside their thread's cpumask\n", argv[0], outside);
        return 3;
    }
    if (repicks && failed == repicks) {
        fprintf(stderr, "%s: every one of %llu sched_setaffinity calls failed\n", argv[0], repicks);
        return 4;
    }
    return 0;
}
