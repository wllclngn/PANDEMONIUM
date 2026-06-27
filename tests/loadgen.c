// loadgen: one generic load generator for the scheduler-precision workloads the
// bench suite needs. A thin driver dispatches to a registered pattern; each
// pattern runs its own control structure (sustained pool / cold-cache table sweep
// / phased starvation) over the shared primitives in loadgen_common. Replaces the
// per-problem one-off C generators (stormwork.c, coldwork.c) -- a new problem adds
// a run() and one registry line, never re-rolls pin/timing/spawn/futex.
//
//   loadgen storm  [dur_s=30] [busy_per_cpu=4] [rt_sleep_us=100] [rt_spin_us=10]
//   loadgen cache  [core=1] [dwell_ms=2000] [cpu_csv] [mem_csv] [dur_s=60]
//   loadgen starve [core=1] [dur_s=60]
//
// Output lines (STORM / SIZE / MEM / STARVE) are unchanged from the originals so
// the existing montauk-side parsers and storm_score read them verbatim.
#define _GNU_SOURCE
#include "loadgen_common.h"
#include <fcntl.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int arg_i(struct loadctx *c, int i, int def) { return c->argc > i ? atoi(c->argv[i]) : def; }
static long arg_l(struct loadctx *c, int i, long def) { return c->argc > i ? atol(c->argv[i]) : def; }
static const char *arg_s(struct loadctx *c, int i, const char *def) { return c->argc > i ? c->argv[i] : def; }

// PATTERN: storm -- the cpu_release reenqueue flood (from stormwork.c).
// A busy SCHED_OTHER population keeps per-CPU DSQs deep; one SCHED_FIFO thread per
// CPU yanks its CPU from sched_ext on every wake -> cpu_release -> reenqueue_local.
static volatile int      storm_running = 1;
static volatile uint64_t storm_rt_wakes = 0;
struct storm_rt_arg { int cpu; uint64_t sleep_us, spin_us; };

static void *storm_rt_thread(void *p) {
    struct storm_rt_arg *a = p;
    pin_to(a->cpu);
    struct sched_param sp = { .sched_priority = 50 };
    if (sched_setscheduler(0, SCHED_FIFO, &sp) != 0)
        a->cpu = -1;  // no CAP_SYS_NICE -- falls back to SCHED_OTHER, weaker storm
    struct timespec slp = { .tv_sec = 0, .tv_nsec = (long)(a->sleep_us * 1000) };
    while (storm_running) {
        clock_nanosleep(CLOCK_MONOTONIC, 0, &slp, NULL);
        spin_ns(a->spin_us * 1000);
        __sync_fetch_and_add(&storm_rt_wakes, 1);
    }
    return NULL;
}

static void *storm_busy_thread(void *p) {
    (void)p;
    struct timespec slp = { .tv_sec = 0, .tv_nsec = 200 * 1000 };
    while (storm_running) {
        spin_ns(150 * 1000);
        clock_nanosleep(CLOCK_MONOTONIC, 0, &slp, NULL);
    }
    return NULL;
}

static int storm_run(struct loadctx *c) {
    int      dur          = arg_i(c, 0, 30);
    int      busy_per_cpu = arg_i(c, 1, 4);
    uint64_t sleep_us     = (uint64_t)arg_l(c, 2, 100);
    uint64_t spin_us      = (uint64_t)arg_l(c, 3, 10);

    int ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (ncpu < 1) ncpu = 1;
    int n_busy = ncpu * busy_per_cpu;

    pthread_t          *rt = calloc((size_t)ncpu, sizeof(*rt));
    pthread_t          *bz = calloc((size_t)n_busy, sizeof(*bz));
    struct storm_rt_arg *ra = calloc((size_t)ncpu, sizeof(*ra));
    if (!rt || !bz || !ra) { perror("calloc"); return 1; }

    for (int i = 0; i < n_busy; i++)
        pthread_create(&bz[i], NULL, storm_busy_thread, NULL);
    for (int cc = 0; cc < ncpu; cc++) {
        ra[cc].cpu = cc; ra[cc].sleep_us = sleep_us; ra[cc].spin_us = spin_us;
        pthread_create(&rt[cc], NULL, storm_rt_thread, &ra[cc]);
    }

    uint64_t t0 = now_ns();
    sleep((unsigned)dur);
    storm_running = 0;
    double secs = (double)(now_ns() - t0) / 1e9;

    int rt_ok = 1;
    for (int cc = 0; cc < ncpu; cc++) { pthread_join(rt[cc], NULL); if (ra[cc].cpu < 0) rt_ok = 0; }
    for (int i = 0; i < n_busy; i++) pthread_join(bz[i], NULL);

    printf("STORM cpus %d busy %d rt %s releases/s %.0f\n",
           ncpu, n_busy, rt_ok ? "FIFO" : "OTHER(no-CAP_SYS_NICE)",
           secs > 0 ? (double)storm_rt_wakes / secs : 0.0);
    free(rt); free(bz); free(ra);
    return 0;
}

// PATTERN: cache -- cold-wake cost sweep, frequency AND memory (from coldwork.c).
// Per cycle: idle to cool the core, then a register burst (freq cost) and a
// non-prefetchable pointer-chase (memory cost) cold vs warm, across a size sweep.
#define MSR_APERF 0xE8
#define MSR_MPERF 0xE7
#define WARMUP_ITERS 50000000UL
#define MEM_STEP_CAP 2000000UL

static volatile unsigned long cache_sink;
static volatile size_t        cache_msink;
typedef struct { long ns; unsigned long long ratio_milli; } burst_t;

static size_t mem_chase(const size_t *buf, size_t start, unsigned long steps) {
    size_t idx = start;
    for (unsigned long i = 0; i < steps; i++) idx = buf[idx];
    return idx;
}

// single random Hamiltonian cycle (Sattolo) -- a dependent, non-prefetchable chase
static void build_cycle(size_t *buf, size_t n) {
    for (size_t i = 0; i < n; i++) buf[i] = i;
    unsigned long r = 0x243f6a8885a308d3UL;
    for (size_t i = n - 1; i > 0; i--) {
        r = r * 6364136223846793005UL + 1442695040888963407UL;
        size_t j = (size_t)((r >> 11) % i);
        size_t t = buf[i]; buf[i] = buf[j]; buf[j] = t;
    }
}

static burst_t freq_window(struct timespec t0, struct timespec t1,
                           uint64_t a0, uint64_t m0, uint64_t a1, uint64_t m1) {
    burst_t b;
    b.ns = elapsed_ns(t0, t1);
    b.ratio_milli = (m1 > m0) ? (unsigned long long)(a1 - a0) * 1000ULL / (m1 - m0) : 0ULL;
    return b;
}

static burst_t cpu_burst(int msr_fd, unsigned long iters) {
    uint64_t a0 = rdmsr(msr_fd, MSR_APERF), m0 = rdmsr(msr_fd, MSR_MPERF);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    cache_sink = cpu_quantum(iters);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    uint64_t a1 = rdmsr(msr_fd, MSR_APERF), m1 = rdmsr(msr_fd, MSR_MPERF);
    return freq_window(t0, t1, a0, m0, a1, m1);
}

static burst_t mem_burst(int msr_fd, const size_t *buf, unsigned long steps) {
    uint64_t a0 = rdmsr(msr_fd, MSR_APERF), m0 = rdmsr(msr_fd, MSR_MPERF);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    cache_msink = mem_chase(buf, 0, steps);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    uint64_t a1 = rdmsr(msr_fd, MSR_APERF), m1 = rdmsr(msr_fd, MSR_MPERF);
    return freq_window(t0, t1, a0, m0, a1, m1);
}

static int cache_run(struct loadctx *c) {
    int          core       = arg_i(c, 0, 1);
    long         dwell_ms   = arg_l(c, 1, 2000);
    const char  *cpu_csv    = arg_s(c, 2, "100000,500000,1000000,4000000,16000000,50000000");
    const char  *mem_csv    = arg_s(c, 3, "32768,262144,2097152,8388608,33554432,134217728");
    long         duration_s = arg_l(c, 4, 60);

    unsigned long cpu_sizes[64]; int nsz = parse_csv(cpu_csv, cpu_sizes, 64);
    unsigned long mem_bytes[64]; int nmem = parse_csv(mem_csv, mem_bytes, 64);
    if (nsz == 0) cpu_sizes[nsz++] = 50000000UL;

    pin_to(core);
    char msrpath[64];
    snprintf(msrpath, sizeof(msrpath), "/dev/cpu/%d/msr", core);
    int msr_fd = open(msrpath, O_RDONLY);

    size_t *mbuf[64]; size_t mn[64];
    for (int i = 0; i < nmem; i++) {
        mn[i] = mem_bytes[i] / sizeof(size_t);
        if (mn[i] < 2) mn[i] = 2;
        mbuf[i] = (size_t *)malloc(mn[i] * sizeof(size_t));
        if (mbuf[i]) build_cycle(mbuf[i], mn[i]);
    }

    struct timespec dwell = { dwell_ms / 1000, (dwell_ms % 1000) * 1000000L };
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);

    int stop = 0;
    while (!stop) {
        for (int s = 0; s < nsz && !stop; s++) {
            nanosleep(&dwell, NULL);
            burst_t cold = cpu_burst(msr_fd, cpu_sizes[s]);
            cpu_quantum(WARMUP_ITERS);
            burst_t warm = cpu_burst(msr_fd, cpu_sizes[s]);
            printf("SIZE %lu COLD %ld %llu WARM %ld %llu\n",
                   cpu_sizes[s], cold.ns, cold.ratio_milli, warm.ns, warm.ratio_milli);
            fflush(stdout);
            clock_gettime(CLOCK_MONOTONIC, &now);
            if (now.tv_sec - start.tv_sec >= duration_s) stop = 1;
        }
        for (int s = 0; s < nmem && !stop; s++) {
            if (!mbuf[s]) continue;
            unsigned long steps = mn[s] < MEM_STEP_CAP ? mn[s] : MEM_STEP_CAP;
            nanosleep(&dwell, NULL);
            burst_t cold = mem_burst(msr_fd, mbuf[s], steps);
            burst_t warm = mem_burst(msr_fd, mbuf[s], steps);
            printf("MEM %lu COLD %ld %llu WARM %ld %llu\n",
                   mem_bytes[s], cold.ns, cold.ratio_milli, warm.ns, warm.ratio_milli);
            fflush(stdout);
            clock_gettime(CLOCK_MONOTONIC, &now);
            if (now.tv_sec - start.tv_sec >= duration_s) stop = 1;
        }
    }
    if (msr_fd >= 0) close(msr_fd);
    return 0;
}

// PATTERN: starve -- dispatch-stall repro (from coldwork.c). A PINNED waker sleeps
// to an absolute deadline; the overshoot is the dispatch-starvation latency. LOCAL
// (own-hrtimer, always HELD) and REMOTE (cross-CPU futex wake onto an idle/shallow
// core, the only topology that can go DARK), each IDLE and HOG.
static void starve_phase(int core, const char *label, int with_hog,
                         long interval_us, long phase_s) {
    pid_t hog = -1;
    if (with_hog) {
        hog = fork();
        if (hog == 0) {
            pin_to(core);
            volatile unsigned long x = 1;
            for (;;) x = x * 6364136223846793005UL + 1442695040888963407UL;
            _exit(0);
        }
    }
    pin_to(core);
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    long end_ns = ts_ns(t) + phase_s * 1000000000L;
    long worst = 0, count = 0, over1ms = 0, over10ms = 0, sum = 0;
    while (1) {
        struct timespec woke; clock_gettime(CLOCK_MONOTONIC, &woke);
        long deadline = ts_ns(woke) + interval_us * 1000L;
        struct timespec dl = { deadline / 1000000000L, deadline % 1000000000L };
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &dl, NULL);
        clock_gettime(CLOCK_MONOTONIC, &woke);
        long overshoot = ts_ns(woke) - deadline;
        if (overshoot < 0) overshoot = 0;
        if (overshoot > worst) worst = overshoot;
        if (overshoot > 1000000L)  over1ms++;
        if (overshoot > 10000000L) over10ms++;
        sum += overshoot; count++;
        if (ts_ns(woke) >= end_ns) break;
    }
    if (with_hog && hog > 0) { kill(hog, SIGKILL); waitpid(hog, NULL, 0); }
    long mean = count ? sum / count : 0;
    printf("STARVE %s interval_us %ld samples %ld mean_ns %ld worst_ns %ld over1ms %ld over10ms %ld\n",
           label, interval_us, count, mean, worst, over1ms, over10ms);
    fflush(stdout);
}

struct remote_cell {
    int core, waker_core; long interval_us, end_ns;
    volatile int flag; volatile long wake_ts;
    long worst, count, over1ms, over10ms, sum;
    volatile int stop;
};

static void *remote_waker(void *arg) {
    struct remote_cell *c = (struct remote_cell *)arg;
    pin_to(c->waker_core);
    struct timespec iv = { c->interval_us / 1000000L, (c->interval_us % 1000000L) * 1000L };
    while (!c->stop) {
        nanosleep(&iv, NULL);
        struct timespec n; clock_gettime(CLOCK_MONOTONIC, &n);
        c->wake_ts = ts_ns(n);
        __atomic_store_n(&c->flag, 1, __ATOMIC_SEQ_CST);
        futex_op((int *)&c->flag, FUTEX_WAKE, 1);
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        if (ts_ns(now) >= c->end_ns) c->stop = 1;
    }
    return NULL;
}

static void remote_phase(int core, const char *label, int with_hog,
                         long interval_us, long phase_s) {
    pid_t hog = -1;
    if (with_hog) {
        hog = fork();
        if (hog == 0) {
            pin_to(core);
            for (;;) {
                volatile unsigned long x = 1;
                for (int i = 0; i < 50000; i++) x = x * 6364136223846793005UL + 1;
                struct timespec s = { 0, 200000L }; nanosleep(&s, NULL);
            }
            _exit(0);
        }
    }
    struct remote_cell c;
    memset(&c, 0, sizeof(c));
    c.core = core;
    c.waker_core = (core == 0) ? 1 : 0;
    c.interval_us = interval_us;
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    c.end_ns = ts_ns(t) + phase_s * 1000000000L;

    pthread_t w;
    pthread_create(&w, NULL, remote_waker, &c);

    pin_to(core);
    while (!c.stop) {
        while (__atomic_load_n(&c.flag, __ATOMIC_SEQ_CST) == 0 && !c.stop)
            futex_op((int *)&c.flag, FUTEX_WAIT, 0);
        if (c.stop) break;
        struct timespec woke; clock_gettime(CLOCK_MONOTONIC, &woke);
        long lat = ts_ns(woke) - c.wake_ts;
        if (lat < 0) lat = 0;
        if (lat > c.worst) c.worst = lat;
        if (lat > 1000000L)  c.over1ms++;
        if (lat > 10000000L) c.over10ms++;
        c.sum += lat; c.count++;
        __atomic_store_n(&c.flag, 0, __ATOMIC_SEQ_CST);
    }
    pthread_join(w, NULL);
    if (with_hog && hog > 0) { kill(hog, SIGKILL); waitpid(hog, NULL, 0); }
    long mean = c.count ? c.sum / c.count : 0;
    printf("STARVE %s interval_us %ld samples %ld mean_ns %ld worst_ns %ld over1ms %ld over10ms %ld\n",
           label, interval_us, c.count, mean, c.worst, c.over1ms, c.over10ms);
    fflush(stdout);
}

static int starve_run(struct loadctx *c) {
    int  core       = arg_i(c, 0, 1);
    long duration_s = arg_l(c, 1, 60);
    struct { const char *label; int hog; long interval_us; int remote; } phases[] = {
        { "IDLE",        0, 1000,  0 }, { "IDLE",        0, 20000, 0 },
        { "HOG",         1, 1000,  0 }, { "HOG",         1, 20000, 0 },
        { "REMOTE-IDLE", 0, 1000,  1 }, { "REMOTE-IDLE", 0, 20000, 1 },
        { "REMOTE-HOG",  1, 1000,  1 }, { "REMOTE-HOG",  1, 20000, 1 },
    };
    int n = (int)(sizeof(phases) / sizeof(phases[0]));
    long each = duration_s / n; if (each < 1) each = 1;
    for (int i = 0; i < n; i++) {
        if (phases[i].remote)
            remote_phase(core, phases[i].label, phases[i].hog, phases[i].interval_us, each);
        else
            starve_phase(core, phases[i].label, phases[i].hog, phases[i].interval_us, each);
    }
    return 0;
}

// the registry: a new problem-resolution load is a run() plus one line here.
static const struct load_pattern PATTERNS[] = {
    { "storm",  storm_run },
    { "cache",  cache_run },
    { "starve", starve_run },
};

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: loadgen <pattern> [args]\n  patterns: storm cache starve\n");
        return 2;
    }
    struct loadctx ctx = { argc - 2, argv + 2 };
    for (size_t i = 0; i < sizeof(PATTERNS) / sizeof(PATTERNS[0]); i++)
        if (strcmp(argv[1], PATTERNS[i].name) == 0)
            return PATTERNS[i].run(&ctx);
    fprintf(stderr, "loadgen: unknown pattern '%s'\n", argv[1]);
    return 2;
}
