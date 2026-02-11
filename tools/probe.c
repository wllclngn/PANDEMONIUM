// INTERACTIVE WAKEUP PROBE -- PURE C, NO GIL, NO PYTHON OVERHEAD
// SLEEP 10MS IN A LOOP, MEASURE WAKEUP OVERSHOOT.
// OUTPUT: ONE LINE PER SAMPLE (OVERSHOOT IN MICROSECONDS).
// SIGTERM/SIGINT TO STOP.

#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
#include <time.h>

static volatile int running = 1;

static void handle_signal(int sig)
{
	(void)sig;
	running = 0;
}

int main(void)
{
	struct timespec req = { .tv_sec = 0, .tv_nsec = 10000000 }; // 10MS
	struct timespec t0, t1;
	long long elapsed_ns, overshoot_us;

	signal(SIGTERM, handle_signal);
	signal(SIGINT, handle_signal);

	setbuf(stdout, NULL); // UNBUFFERED

	while (running) {
		clock_gettime(CLOCK_MONOTONIC, &t0);
		nanosleep(&req, NULL);
		clock_gettime(CLOCK_MONOTONIC, &t1);

		elapsed_ns = (t1.tv_sec - t0.tv_sec) * 1000000000LL
			   + (t1.tv_nsec - t0.tv_nsec);
		overshoot_us = (elapsed_ns - 10000000LL) / 1000;
		if (overshoot_us < 0)
			overshoot_us = 0;

		printf("%lld\n", overshoot_us);
	}

	return 0;
}
