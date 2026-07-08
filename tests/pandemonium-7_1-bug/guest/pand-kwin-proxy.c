// pand-kwin-proxy.c -- Wayland compositor proxy: a 60fps frame-paced latency
// victim modelling kwin_wayland, the process the 6/27 montauk holder capture
// caught stranded. Each frame it sleeps to an absolute deadline, wakes, does a
// little work, and records how LATE it woke -- the scheduling overrun. Under the
// I/O flood on 7.1.x/XFS a scheduler that fails to dispatch the compositor
// promptly shows a fat overrun tail; that IS the stall. Overruns (us) go to
// SCRATCH/kwin_overruns.txt for host-side sublimation.
//
// usage: pand-kwin-proxy DURATION_S FRAME_US SCRATCH_DIR

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <time.h>

int main(int argc, char **argv)
{
    prctl(PR_SET_NAME, "pand-kwin", 0, 0, 0);
    if (argc < 4) {
        fprintf(stderr, "usage: %s DURATION_S FRAME_US SCRATCH\n", argv[0]);
        return 1;
    }
    int duration = atoi(argv[1]);
    long frame_ns = (long)atoi(argv[2]) * 1000L;
    const char *scratch = argv[3];
    if (frame_ns < 1000000L)
        frame_ns = 16667000L;  // default 60fps

    long n_frames = (long)duration * 1000000000L / frame_ns;
    long *overrun = malloc(sizeof(long) * (n_frames + 8));
    if (!overrun)
        return 1;
    long cnt = 0;

    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    long long deadline = (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
    volatile long sink = 0;
    for (long i = 0; i < n_frames; i++) {
        deadline += frame_ns;
        struct timespec ts = {(time_t)(deadline / 1000000000LL),
                              (long)(deadline % 1000000000LL)};
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        long long n = (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
        long over_us = (long)((n - deadline) / 1000);  // how late we woke, us
        if (over_us < 0)
            over_us = 0;
        overrun[cnt++] = over_us;
        // a little per-frame work so we are a real runnable, not a pure sleeper
        for (int k = 0; k < 50000; k++)
            sink += k;
    }

    char path[256];
    snprintf(path, sizeof path, "%s/kwin_overruns.txt", scratch);
    FILE *f = fopen(path, "w");
    if (f) {
        for (long i = 0; i < cnt; i++)
            fprintf(f, "%ld\n", overrun[i]);
        fclose(f);
    }
    free(overrun);
    return 0;
}
