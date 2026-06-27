// loadgen_common: the shared scaffolding stormwork.c and coldwork.c duplicated.
#define _GNU_SOURCE
#include "loadgen_common.h"
#include <linux/futex.h>
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000UL + ts.tv_nsec;
}

long ts_ns(struct timespec t) {
    return t.tv_sec * 1000000000L + t.tv_nsec;
}

long elapsed_ns(struct timespec a, struct timespec b) {
    return (b.tv_sec - a.tv_sec) * 1000000000L + (b.tv_nsec - a.tv_nsec);
}

void spin_ns(uint64_t ns) {
    uint64_t end = now_ns() + ns;
    volatile uint64_t x = 0x9e3779b97f4a7c15UL;
    while (now_ns() < end)
        x = x * 6364136223846793005UL + 1442695040888963407UL;
    (void)x;
}

unsigned long cpu_quantum(unsigned long iters) {
    unsigned long x = 0x9e3779b97f4a7c15UL;
    for (unsigned long i = 0; i < iters; i++)
        x = x * 6364136223846793005UL + 1442695040888963407UL;
    return x;
}

void pin_to(int cpu) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    sched_setaffinity(0, sizeof(set), &set);
}

int futex_op(int *uaddr, int op, int val) {
    return (int)syscall(SYS_futex, uaddr, op, val, NULL, NULL, 0);
}

uint64_t rdmsr(int fd, off_t reg) {
    uint64_t v = 0;
    if (fd >= 0 && pread(fd, &v, sizeof(v), reg) == (ssize_t)sizeof(v))
        return v;
    return 0;
}

int parse_csv(const char *csv, unsigned long *out, int cap) {
    char b[1024];
    strncpy(b, csv, sizeof(b) - 1);
    b[sizeof(b) - 1] = '\0';
    int n = 0;
    for (char *t = strtok(b, ","); t && n < cap; t = strtok(NULL, ","))
        out[n++] = strtoul(t, NULL, 10);
    return n;
}
