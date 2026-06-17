// stormwork: cpu_release-storm reproducer for bench-coldwake's storm mode.
//
// The reboot live-lock is NOT a cold-cache cost -- it is a cpu_release flood.
// On a powersave boot, RT/DL tasks (audio, kworkers, the freq driver) and a
// storm of forking LAT_CRITICAL services keep preempting sched_ext off its
// CPUs. Each time the kernel takes a CPU from sched_ext it calls cpu_release(),
// which runs scx_bpf_reenqueue_local() -- every queued task on that CPU is
// bounced back through enqueue(). With deep per-CPU queues that is a re-enqueue
// flood, and an ungated kick path turns it into the 1.3M-d/s live-lock.
//
// No bench reproduced that, so every report looked clean while the real machine
// hitched. stormwork synthesizes the condition on demand, no reboot:
//   1. A busy sched_ext population (SCHED_OTHER workers, work + short sleep) so
//      the per-CPU DSQs carry depth for reenqueue_local to bounce.
//   2. One SCHED_FIFO thread pinned per CPU. RT is a higher class than
//      sched_ext, so every FIFO wake yanks its CPU from scx -> cpu_release ->
//      reenqueue_local. sleep_us/spin_us set the release rate and keep RT duty
//      well under the kernel throttle.
//
// Distinct comm `stormwork` so montauk targets exactly this. Run it while a
// scheduler tick log captures d/s / shared / kick H / enq R; the storm shows as
// the d/s>=900k, idle~0, kick-vs-requeue signature the suite scores.
//
//   stormwork <duration_s> [busy_per_cpu=4] [rt_sleep_us=100] [rt_spin_us=10]
//   stdout:   STORM cpus <n> busy <n> releases/s <approx>  (one line at exit)

#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile int      running = 1;
static volatile uint64_t rt_wakes = 0;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000UL + ts.tv_nsec;
}

static void spin_ns(uint64_t ns) {
    uint64_t end = now_ns() + ns;
    volatile uint64_t x = 0x9e3779b97f4a7c15UL;
    while (now_ns() < end)
        x = x * 6364136223846793005UL + 1442695040888963407UL;
    (void)x;
}

static void pin_to(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    sched_setaffinity(0, sizeof(set), &set);
}

struct rt_arg { int cpu; uint64_t sleep_us, spin_us; };

// One SCHED_FIFO thread per CPU: sleep, then spin briefly. Each wake preempts
// sched_ext on this CPU -> cpu_release -> reenqueue_local flood.
static void *rt_thread(void *p) {
    struct rt_arg *a = p;
    pin_to(a->cpu);

    struct sched_param sp = { .sched_priority = 50 };
    if (sched_setscheduler(0, SCHED_FIFO, &sp) != 0) {
        // No CAP_SYS_NICE: fall back to SCHED_OTHER. Still churns, weaker
        // release rate -- the storm needs RT, so warn via the exit line.
        a->cpu = -1;
    }

    struct timespec slp = {
        .tv_sec = 0,
        .tv_nsec = (long)(a->sleep_us * 1000),
    };
    while (running) {
        clock_nanosleep(CLOCK_MONOTONIC, 0, &slp, NULL);
        spin_ns(a->spin_us * 1000);
        __sync_fetch_and_add(&rt_wakes, 1);
    }
    return NULL;
}

// SCHED_OTHER worker: small work then short sleep, so its per-CPU DSQ keeps
// depth for reenqueue_local to bounce on each cpu_release.
static void *busy_thread(void *p) {
    (void)p;
    struct timespec slp = { .tv_sec = 0, .tv_nsec = 200 * 1000 };
    while (running) {
        spin_ns(150 * 1000);
        clock_nanosleep(CLOCK_MONOTONIC, 0, &slp, NULL);
    }
    return NULL;
}

int main(int argc, char **argv) {
    int      dur          = argc > 1 ? atoi(argv[1]) : 30;
    int      busy_per_cpu = argc > 2 ? atoi(argv[2]) : 4;
    uint64_t sleep_us     = argc > 3 ? (uint64_t)atoll(argv[3]) : 100;
    uint64_t spin_us      = argc > 4 ? (uint64_t)atoll(argv[4]) : 10;

    int ncpu = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (ncpu < 1) ncpu = 1;

    int           n_busy = ncpu * busy_per_cpu;
    pthread_t    *rt = calloc((size_t)ncpu, sizeof(*rt));
    pthread_t    *bz = calloc((size_t)n_busy, sizeof(*bz));
    struct rt_arg *ra = calloc((size_t)ncpu, sizeof(*ra));
    if (!rt || !bz || !ra) { perror("calloc"); return 1; }

    for (int i = 0; i < n_busy; i++)
        pthread_create(&bz[i], NULL, busy_thread, NULL);

    int rt_ok = 1;
    for (int c = 0; c < ncpu; c++) {
        ra[c].cpu = c;
        ra[c].sleep_us = sleep_us;
        ra[c].spin_us = spin_us;
        pthread_create(&rt[c], NULL, rt_thread, &ra[c]);
    }

    uint64_t t0 = now_ns();
    sleep((unsigned)dur);
    running = 0;
    double secs = (double)(now_ns() - t0) / 1e9;

    for (int c = 0; c < ncpu; c++) {
        pthread_join(rt[c], NULL);
        if (ra[c].cpu < 0) rt_ok = 0;
    }
    for (int i = 0; i < n_busy; i++)
        pthread_join(bz[i], NULL);

    uint64_t rel = rt_wakes;
    printf("STORM cpus %d busy %d rt %s releases/s %.0f\n",
           ncpu, n_busy, rt_ok ? "FIFO" : "OTHER(no-CAP_SYS_NICE)",
           secs > 0 ? (double)rel / secs : 0.0);
    free(rt); free(bz); free(ra);
    return 0;
}
