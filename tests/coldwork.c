// coldwork: cold-wake probe for bench-coldwake. CPU-frequency AND memory.
//
// A cold wake costs two things, and a register-only CPU burst sees only one:
//   1. FREQUENCY: the core wakes at base and ramps to boost over a few ms (small,
//      ~7% / 0.07ms on amd-pstate-epp -- real but not felt).
//   2. MEMORY: deep C-states flush L1/L2 and long idle drops DRAM into self-
//      refresh, so the first work that TOUCHES memory off a cold core pays a
//      storm of cache misses to L3/DRAM plus a self-refresh exit -- hundreds of
//      microseconds to milliseconds, the cost a user actually feels.
//
// So this measures both. Per cycle it sweeps CPU burst sizes (register LCG,
// emits SIZE) and memory working sets (pointer-chase, emits MEM), each off a
// genuinely cold core against a warmed reference. A pointer-chase over a random
// cyclic permutation defeats the prefetcher, so the chase pays the true miss
// latency of wherever the working set lives. Distinct comm so montauk targets
// exactly these wakes.
//
//   coldwork <core> <dwell_ms> <cpu_sizes_csv> <mem_bytes_csv> <duration_s>
//   stdout:  SIZE <iters>  COLD <ns> <ratio_milli> WARM <ns> <ratio_milli>
//            MEM  <bytes>  COLD <ns> <ratio_milli> WARM <ns> <ratio_milli>

#define _GNU_SOURCE
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

static volatile unsigned long sink;
static volatile size_t        msink;

static unsigned long cpu_quantum(unsigned long iters) {
    unsigned long x = 0x9e3779b97f4a7c15UL;
    for (unsigned long i = 0; i < iters; i++)
        x = x * 6364136223846793005UL + 1442695040888963407UL;
    return x;
}

// Pointer-chase over a single random cycle: idx = buf[idx], `steps` times. The
// random cycle (Sattolo) gives a dependent, non-prefetchable access stream, so
// each step pays the real miss latency of where buf currently lives.
static size_t mem_chase(const size_t *buf, size_t start, unsigned long steps) {
    size_t idx = start;
    for (unsigned long i = 0; i < steps; i++) idx = buf[idx];
    return idx;
}

static long elapsed_ns(struct timespec a, struct timespec b) {
    return (b.tv_sec - a.tv_sec) * 1000000000L + (b.tv_nsec - a.tv_nsec);
}

#define MSR_APERF 0xE8
#define MSR_MPERF 0xE7
static uint64_t rdmsr(int fd, off_t reg) {
    uint64_t v = 0;
    if (fd >= 0 && pread(fd, &v, sizeof(v), reg) == (ssize_t)sizeof(v)) return v;
    return 0;
}

#define WARMUP_ITERS 50000000UL          // ramps the core to steady frequency
#define MEM_STEP_CAP 2000000UL           // bound the chase length for big working sets

typedef struct { long ns; unsigned long long ratio_milli; } burst_t;

static burst_t freq_window(int msr_fd, struct timespec t0, struct timespec t1,
                           uint64_t a0, uint64_t m0, uint64_t a1, uint64_t m1) {
    (void)msr_fd;
    burst_t b;
    b.ns = elapsed_ns(t0, t1);
    b.ratio_milli = (m1 > m0) ? (unsigned long long)(a1 - a0) * 1000ULL / (m1 - m0) : 0ULL;
    return b;
}

static burst_t cpu_burst(int msr_fd, unsigned long iters) {
    uint64_t a0 = rdmsr(msr_fd, MSR_APERF), m0 = rdmsr(msr_fd, MSR_MPERF);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    sink = cpu_quantum(iters);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    uint64_t a1 = rdmsr(msr_fd, MSR_APERF), m1 = rdmsr(msr_fd, MSR_MPERF);
    return freq_window(msr_fd, t0, t1, a0, m0, a1, m1);
}

static burst_t mem_burst(int msr_fd, const size_t *buf, unsigned long steps) {
    uint64_t a0 = rdmsr(msr_fd, MSR_APERF), m0 = rdmsr(msr_fd, MSR_MPERF);
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    msink = mem_chase(buf, 0, steps);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    uint64_t a1 = rdmsr(msr_fd, MSR_APERF), m1 = rdmsr(msr_fd, MSR_MPERF);
    return freq_window(msr_fd, t0, t1, a0, m0, a1, m1);
}

// Build a single random Hamiltonian cycle over n elements (Sattolo). buf[i] is
// the next index to chase. Deterministic PRNG -- the access pattern only needs
// to be unpredictable to the prefetcher, not random.
static void build_cycle(size_t *buf, size_t n) {
    for (size_t i = 0; i < n; i++) buf[i] = i;
    unsigned long r = 0x243f6a8885a308d3UL;
    for (size_t i = n - 1; i > 0; i--) {
        r = r * 6364136223846793005UL + 1442695040888963407UL;
        size_t j = (size_t)((r >> 11) % i);   // j in [0, i-1] -> single cycle
        size_t t = buf[i]; buf[i] = buf[j]; buf[j] = t;
    }
}

static int parse_csv(const char *csv, unsigned long *out, int cap) {
    char b[1024]; strncpy(b, csv, sizeof(b) - 1); b[sizeof(b) - 1] = '\0';
    int n = 0;
    for (char *t = strtok(b, ","); t && n < cap; t = strtok(NULL, ","))
        out[n++] = strtoul(t, NULL, 10);
    return n;
}

// ---- starvation cell (dispatch-stall repro) --------------------------------
// The cold-cache sweep above measures the COST of a wake (cache/frequency). It
// can never reproduce the dispatch STALL -- a runnable pinned task that the
// scheduler does not dispatch for seconds (the kworker/1:1 watchdog ejection),
// because its task sleeps voluntarily and wakes to run immediately. This mode
// builds the actual starvation condition instead: a PINNED waker (single-CPU
// cpumask -- cannot be stolen or migrated, exactly like a per-CPU kworker)
// sleeps to an ABSOLUTE deadline; the overshoot past that deadline is the
// dispatch-starvation latency -- how long the runnable task waited to be put on
// the CPU. Healthy: overshoot ~ one tick. Stalled: it climbs toward the 30s
// sched_ext watchdog. Two sub-phases cover both stranding shapes:
//   IDLE -- waker alone on an otherwise-idle core: the stranded-on-a-deep-idle-
//           CPU case (no tick fires there, so the tick-gated rescue scan never
//           runs). Keep the rest of the box quiet for this to bite.
//   HOG  -- waker contends the SAME core with a non-yielding hog: the kworker-
//           behind-a-running-task case (stranded on a busy per-CPU DSQ, the
//           steal path declines it because the cpumask is a single CPU).
// Distinct comm `coldwork`, so montauk's wake2run on these wakes is the real
// signal; the STARVE line is the userspace-visible backup.

static long ts_ns(struct timespec t) {
    return t.tv_sec * 1000000000L + t.tv_nsec;
}

static void pin_to(int core) {
    cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
    sched_setaffinity(0, sizeof(set), &set);
}

// Run one starvation sub-phase for phase_s seconds at the given wake interval.
// with_hog forks a non-yielding hog pinned to the SAME core so the waker must
// be preempted onto the CPU to run; without it the core sits idle between wakes.
static void starve_phase(int core, const char *label, int with_hog,
                         long interval_us, long phase_s) {
    pid_t hog = -1;
    if (with_hog) {
        hog = fork();
        if (hog == 0) {                       // child: non-yielding hog on `core`
            pin_to(core);
            volatile unsigned long x = 1;
            for (;;) x = x * 6364136223846793005UL + 1442695040888963407UL;
            _exit(0);                         // unreachable
        }
    }
    pin_to(core);
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    long end_ns = ts_ns(t) + phase_s * 1000000000L;
    long worst = 0, count = 0, over1ms = 0, over10ms = 0, sum = 0;
    while (1) {
        struct timespec woke; clock_gettime(CLOCK_MONOTONIC, &woke);
        long deadline = ts_ns(woke) + interval_us * 1000L;   // re-base on wake (no catch-up runaway)
        struct timespec dl = { deadline / 1000000000L, deadline % 1000000000L };
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &dl, NULL);
        clock_gettime(CLOCK_MONOTONIC, &woke);
        long overshoot = ts_ns(woke) - deadline;             // dispatch-starvation latency
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

// ---- REMOTE cross-CPU waker (the wake topology the local timer cannot make) --
// montauk's DARK/HELD split proved the local-timer starve only ever produces
// HELD wakes (busy CPU, tick-bounded): a hrtimer fires ON the victim's own CPU,
// so the CPU is never dark when the task becomes runnable. A per-CPU kworker
// stalls differently -- it is made runnable from ANOTHER CPU's context (work
// queued remotely) and lands on its owning CPU while that CPU sits IDLE. If the
// scheduler's enqueue kicks SCX_KICK_IDLE and it no-ops / races on a deep-idle
// core, the wakee strands with no tick to rescue it. This builds exactly that:
// a waker thread on a DIFFERENT core futex-wakes a victim pinned to `core`, and
// `core` is otherwise idle (REMOTE-IDLE) or shallowly hogged (REMOTE-HOG). The
// victim measures wake-to-run from the remote wake stamp; montauk's WAKE2RUN on
// these is the true signal and now classifies them DARK vs HELD.

static int futex_op(int *uaddr, int op, int val) {
    return (int)syscall(SYS_futex, uaddr, op, val, NULL, NULL, 0);
}

struct remote_cell {
    int       core;          // victim core (pinned)
    int       waker_core;    // remote waker core (different)
    long      interval_us;   // remote wake cadence
    long      end_ns;        // phase deadline
    volatile int flag;       // futex: 0 = victim waiting, 1 = waker signalled
    volatile long wake_ts;   // stamp set by the waker just before the wake
    long      worst, count, over1ms, over10ms, sum;
    volatile int stop;
};

static void *remote_waker(void *arg) {
    struct remote_cell *c = (struct remote_cell *)arg;
    pin_to(c->waker_core);
    struct timespec iv = { c->interval_us / 1000000L,
                           (c->interval_us % 1000000L) * 1000L };
    while (!c->stop) {
        nanosleep(&iv, NULL);
        struct timespec n; clock_gettime(CLOCK_MONOTONIC, &n);
        c->wake_ts = ts_ns(n);
        __atomic_store_n(&c->flag, 1, __ATOMIC_SEQ_CST);
        futex_op((int *)&c->flag, FUTEX_WAKE, 1);   // wake victim from THIS (remote) CPU
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
            // SHALLOW hog: spins but yields briefly, so `core` cycles idle<->busy
            // -- the realistic state a kworker's owning CPU is in, not pinned-busy.
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
    c.waker_core = (core == 0) ? 1 : 0;   // a DIFFERENT core issues the wake
    c.interval_us = interval_us;
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    c.end_ns = ts_ns(t) + phase_s * 1000000000L;

    pthread_t w;
    pthread_create(&w, NULL, remote_waker, &c);

    pin_to(core);                          // victim: pinned, single-CPU cpumask
    while (!c.stop) {
        while (__atomic_load_n(&c.flag, __ATOMIC_SEQ_CST) == 0 && !c.stop)
            futex_op((int *)&c.flag, FUTEX_WAIT, 0);   // block off-CPU until remote wake
        if (c.stop) break;
        struct timespec woke; clock_gettime(CLOCK_MONOTONIC, &woke);
        long lat = ts_ns(woke) - c.wake_ts;            // remote-wake -> running
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

// Sweep every stranding shape. LOCAL (IDLE/HOG): the self-timer baseline -- a
// CPU woken by its own hrtimer, always HELD. REMOTE (REMOTE-IDLE/REMOTE-HOG):
// a remote CPU wakes a pinned victim onto an idle/shallow core -- the kworker
// wake topology, the only one that can go DARK. Splits the budget evenly.
static void run_starve(int core, long duration_s) {
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
            remote_phase(core, phases[i].label, phases[i].hog,
                         phases[i].interval_us, each);
        else
            starve_phase(core, phases[i].label, phases[i].hog,
                         phases[i].interval_us, each);
    }
}

int main(int argc, char **argv) {
    int          core       = argc > 1 ? atoi(argv[1]) : 1;
    long         dwell_ms   = argc > 2 ? atol(argv[2]) : 2000;
    const char  *cpu_csv    = argc > 3 ? argv[3] : "100000,500000,1000000,4000000,16000000,50000000";
    const char  *mem_csv    = argc > 4 ? argv[4] : "32768,262144,2097152,8388608,33554432,134217728";
    long         duration_s = argc > 5 ? atol(argv[5]) : 60;
    const char  *mode       = argc > 6 ? argv[6] : "cache";   // "cache" (default, unchanged) | "starve"

    // STARVE: build the dispatch-stall condition instead of the cold-cache sweep.
    // Default 5-arg invocations are unaffected -- mode falls back to "cache".
    if (strcmp(mode, "starve") == 0) {
        run_starve(core, duration_s);
        return 0;
    }

    unsigned long cpu_sizes[64]; int ncpu = parse_csv(cpu_csv, cpu_sizes, 64);
    unsigned long mem_bytes[64]; int nmem = parse_csv(mem_csv, mem_bytes, 64);
    if (ncpu == 0) cpu_sizes[ncpu++] = 50000000UL;

    cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
    sched_setaffinity(0, sizeof(set), &set);

    char msrpath[64];
    snprintf(msrpath, sizeof(msrpath), "/dev/cpu/%d/msr", core);
    int msr_fd = open(msrpath, O_RDONLY);

    // Allocate + permute one chase buffer per memory working set, once. Built
    // warm; the per-cycle idle is what cools them.
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
        for (int s = 0; s < ncpu && !stop; s++) {
            nanosleep(&dwell, NULL);                    // core idles -> cold
            burst_t cold = cpu_burst(msr_fd, cpu_sizes[s]);
            cpu_quantum(WARMUP_ITERS);                  // ramp the core
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
            nanosleep(&dwell, NULL);                    // core idles -> caches cool, DRAM self-refresh
            burst_t cold = mem_burst(msr_fd, mbuf[s], steps);   // pays the cold-miss storm
            burst_t warm = mem_burst(msr_fd, mbuf[s], steps);   // now hot
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
