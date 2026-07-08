// pand-idle-ping.c -- targeted induction for Finding 2 (the can_skip_idle_kick
// race, kernel/sched/ext/ext.c), not the diffuse CPU-0/throughput levers used
// for Findings 1 and 4.
//
// The race needs a target CPU to actually take balance_one()'s empty/idle
// fast-return (clearing SCX_RQ_IN_BALANCE with nothing found) at the exact
// instant a remote CPU's wakeup lands and evaluates can_skip_idle_kick() for
// that same target. A CPU kept permanently busy (a spin-loop hog) never takes
// that path at all. This instead cycles a target CPU between idle and running
// as fast as userspace can manage, cross-fired against a pinger on a
// different CPU whose wakeup is what would need to race the target's idle
// transition -- maximizing the NUMBER of chances per second the kernel's own
// narrow race window gets exercised, since the exact nanosecond-level timing
// isn't controllable from userspace.
//
// usage: pand-idle-ping DURATION_S NUM_PAIRS FIRST_CPU
//   Spawns NUM_PAIRS (target, pinger) thread pairs. Pair i: target pinned to
//   FIRST_CPU + 2*i, pinger pinned to FIRST_CPU + 2*i + 1. Each target
//   futex_waits on its own word; each pinger futex_wakes it in as tight a
//   loop as it can sustain, with no throttling -- more attempts per second is
//   the only lever userspace has on hitting the race.

#define _GNU_SOURCE
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define MAX_PAIRS 16

static long g_deadline_ms;
static atomic_int g_word[MAX_PAIRS];

static long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static long futex(atomic_int *uaddr, int op, int val)
{
    return syscall(SYS_futex, uaddr, op, val, NULL, NULL, 0);
}

static void pin_self(int cpu)
{
    cpu_set_t cpus;
    CPU_ZERO(&cpus);
    CPU_SET(cpu, &cpus);
    pthread_setaffinity_np(pthread_self(), sizeof(cpus), &cpus);
}

struct pair_arg {
    int cpu;
    int idx;
};

static void *target_fn(void *p)
{
    struct pair_arg *a = p;
    pin_self(a->cpu);
    prctl(PR_SET_NAME, "pand-ping-tgt", 0, 0, 0);
    while (now_ms() < g_deadline_ms) {
        futex(&g_word[a->idx], FUTEX_WAIT, 0);
        atomic_store(&g_word[a->idx], 0);
    }
    return NULL;
}

static void *pinger_fn(void *p)
{
    struct pair_arg *a = p;
    pin_self(a->cpu);
    prctl(PR_SET_NAME, "pand-ping-src", 0, 0, 0);
    while (now_ms() < g_deadline_ms) {
        atomic_store(&g_word[a->idx], 1);
        futex(&g_word[a->idx], FUTEX_WAKE, 1);
    }
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: %s DURATION_S NUM_PAIRS FIRST_CPU\n", argv[0]);
        return 1;
    }
    int duration_s = atoi(argv[1]);
    int num_pairs = atoi(argv[2]);
    int first_cpu = atoi(argv[3]);
    if (num_pairs > MAX_PAIRS)
        num_pairs = MAX_PAIRS;

    prctl(PR_SET_NAME, "pand-idleping", 0, 0, 0);
    g_deadline_ms = now_ms() + duration_s * 1000L;

    pthread_t targets[MAX_PAIRS], pingers[MAX_PAIRS];
    struct pair_arg args[MAX_PAIRS];
    for (int i = 0; i < num_pairs; i++) {
        atomic_store(&g_word[i], 0);
        args[i].idx = i;
        args[i].cpu = first_cpu + 2 * i;
        pthread_create(&targets[i], NULL, target_fn, &args[i]);
    }
    // Pingers start after every target is already blocked in FUTEX_WAIT, so
    // the very first wake is a real cross-thread race, not a freebie against
    // a target that hasn't reached the syscall yet.
    usleep(50000);
    for (int i = 0; i < num_pairs; i++) {
        struct pair_arg *pa = malloc(sizeof(*pa));
        pa->idx = i;
        pa->cpu = first_cpu + 2 * i + 1;
        pthread_create(&pingers[i], NULL, pinger_fn, pa);
    }

    for (int i = 0; i < num_pairs; i++) {
        pthread_join(pingers[i], NULL);
        atomic_store(&g_word[i], 1);
        futex(&g_word[i], FUTEX_WAKE, 1);  // unblock target if still waiting
        pthread_join(targets[i], NULL);
    }
    return 0;
}
