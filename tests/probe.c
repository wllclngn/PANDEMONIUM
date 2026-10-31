// THE TEST SUITE'S WAKE PROBE AND STRESS WORKER. BUILT BY pandemonium_common's
// probe_tool() THE WAY loadgen.c AND framebench.c ARE, SO THE SCHEDULER BINARY
// CARRIES NO TEST CODE.
//
//   probe [--threads N] [--interval-us I] [--hist]
//       N PERIODIC SLEEPERS, UNPINNED, AT SCHED_NORMAL, EACH ON AN ABSOLUTE
//       CLOCK_MONOTONIC DEADLINE EVERY I MICROSECONDS. EACH WAKE'S LATENESS IS
//       RECORDED IN MEMORY AND PRINTED AFTER SIGINT. DEFAULT: ONE THREAD AT
//       10MS, ONE LINE PER SAMPLE (EVERY LATENCY PHASE READS THIS). --hist
//       PRINTS `lateness_us count` LINES, `over count` FOR WAKES PAST 100MS
//       WHEN THERE ARE ANY, AND A FINAL `max lateness_us`: cyclictest's
//       MEASUREMENT WITH NOTHING TO INSTALL.
//   stress-worker --cpu C
//       A PURE COMPUTE SPIN PINNED TO CPU C UNTIL SIGINT.
//
// THREADS NAME THEMSELVES pand-probe AND pand-stress, SO A CAPTURE TELLS THEM
// APART AND montauk MATCHES THE NAMES IN-KERNEL.

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>

#define NSEC_PER_SEC 1000000000LL
// ONE THREAD'S SAMPLE BUFFER, ALLOCATED AND TOUCHED BEFORE THE LOOP.
#define MAX_SAMPLES 16384
// ONE BUCKET PER MICROSECOND BELOW 100MS; A LATER WAKE IS COUNTED AS OVERFLOW,
// NEVER AS A 100MS READING, AND THE EXACT MAX IS KEPT BESIDE IT.
#define HIST_BUCKETS 100000
#define MAX_THREADS 1024

static atomic_bool running = true;

static void on_signal(int)
{
	atomic_store_explicit(&running, false, memory_order_relaxed);
}

static void install_signals(void)
{
	// NO SA_RESTART: A SIGNAL ENDS THE SLEEP SO THE LOOP SEES running AT ONCE.
	struct sigaction sa = {0};
	sa.sa_handler = on_signal;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, nullptr);
	sigaction(SIGTERM, &sa, nullptr);
}

static int64_t ts_ns(const struct timespec *t)
{
	return (int64_t)t->tv_sec * NSEC_PER_SEC + t->tv_nsec;
}

typedef bool (*record_fn)(void *ctx, int64_t us);

// COORDINATED-OMISSION-CORRECT: SLEEP TO AN ABSOLUTE RUNNING DEADLINE, NOT A
// RELATIVE NANOSLEEP. WHEN A STALL MAKES THE THREAD OVERSLEEP PAST LATER
// DEADLINES, ONE SAMPLE IS RECORDED PER SWALLOWED DEADLINE (HdrHistogram's
// recordValueWithExpectedInterval). A RELATIVE SLEEP RECORDS ONE LONG OVERSHOOT
// AND DROPS THE DEADLINES THE STALL ATE, UNDERSTATING THE TAIL. A SIGNAL ENDS
// THE SLEEP EARLY AND RECORDS NOTHING: THE LOOP RECHECKS running AND SLEEPS
// AGAIN TO THE SAME DEADLINE.
static void periodic(int64_t period_ns, record_fn record, void *ctx)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	int64_t deadline = ts_ns(&now) + period_ns;

	while (atomic_load_explicit(&running, memory_order_relaxed)) {
		struct timespec dl = {
			.tv_sec = deadline / NSEC_PER_SEC,
			.tv_nsec = deadline % NSEC_PER_SEC,
		};
		if (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &dl, nullptr) == EINTR)
			continue;
		clock_gettime(CLOCK_MONOTONIC, &now);
		int64_t now_ns = ts_ns(&now);
		for (;;) {
			int64_t late = now_ns - deadline;
			if (!record(ctx, (late > 0 ? late : 0) / 1000))
				return;
			deadline += period_ns;
			if (now_ns < deadline)
				break;
		}
	}
}

struct samples {
	int64_t *v;
	size_t n;
};

static bool record_sample(void *ctx, int64_t us)
{
	struct samples *s = ctx;
	s->v[s->n++] = us;
	return s->n < MAX_SAMPLES;
}

struct hist {
	uint64_t *b;
	uint64_t over;
	int64_t max;
};

static bool record_hist(void *ctx, int64_t us)
{
	struct hist *h = ctx;
	if (us < HIST_BUCKETS)
		h->b[us] += 1;
	else
		h->over += 1;
	if (us > h->max)
		h->max = us;
	return true;
}

struct worker {
	pthread_t tid;
	int64_t period_ns;
	bool hist;
	struct samples s;
	struct hist h;
};

static void name_self(const char *name)
{
	prctl(PR_SET_NAME, name, 0, 0, 0);
}

static void *probe_thread(void *arg)
{
	struct worker *w = arg;
	name_self("pand-probe");
	if (w->hist)
		periodic(w->period_ns, record_hist, &w->h);
	else
		periodic(w->period_ns, record_sample, &w->s);
	return nullptr;
}

// THE BUFFERS ARE WRITTEN ONCE HERE SO NO PAGE IS FIRST TOUCHED INSIDE THE LOOP.
static bool worker_alloc(struct worker *w)
{
	if (w->hist) {
		w->h.b = malloc(sizeof(uint64_t) * HIST_BUCKETS);
		if (!w->h.b)
			return false;
		memset(w->h.b, 0, sizeof(uint64_t) * HIST_BUCKETS);
	} else {
		w->s.v = malloc(sizeof(int64_t) * MAX_SAMPLES);
		if (!w->s.v)
			return false;
		memset(w->s.v, 0, sizeof(int64_t) * MAX_SAMPLES);
	}
	return true;
}

static int run_probe(int threads, int64_t interval_us, bool hist)
{
	install_signals();
	int64_t period_ns = interval_us * 1000;
	struct worker *ws = calloc((size_t)threads, sizeof(*ws));
	if (!ws)
		return 1;
	for (int i = 0; i < threads; i++) {
		ws[i].period_ns = period_ns;
		ws[i].hist = hist;
		if (!worker_alloc(&ws[i]))
			return 1;
	}

	// ONE THREAD WITH ONE LINE PER SAMPLE RUNS ON THE MAIN THREAD, AS IT ALWAYS
	// HAS: THE PROCESS'S OWN TASK IS THE ONE MEASURED.
	if (threads == 1 && !hist) {
		probe_thread(&ws[0]);
	} else {
		for (int i = 0; i < threads; i++)
			if (pthread_create(&ws[i].tid, nullptr, probe_thread, &ws[i]) != 0)
				return 1;
		for (int i = 0; i < threads; i++)
			pthread_join(ws[i].tid, nullptr);
	}

	if (!hist) {
		for (int i = 0; i < threads; i++)
			for (size_t k = 0; k < ws[i].s.n; k++)
				printf("%lld\n", (long long)ws[i].s.v[k]);
		return 0;
	}
	uint64_t over = 0;
	int64_t max = 0;
	for (int us = 0; us < HIST_BUCKETS; us++) {
		uint64_t c = 0;
		for (int i = 0; i < threads; i++)
			c += ws[i].h.b[us];
		if (c)
			printf("%d %llu\n", us, (unsigned long long)c);
	}
	for (int i = 0; i < threads; i++) {
		over += ws[i].h.over;
		if (ws[i].h.max > max)
			max = ws[i].h.max;
	}
	if (over)
		printf("over %llu\n", (unsigned long long)over);
	printf("max %lld\n", (long long)max);
	return 0;
}

static int run_stress(int cpu)
{
	install_signals();
	name_self("pand-stress");
	cpu_set_t set;
	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	sched_setaffinity(0, sizeof(set), &set);
	volatile uint64_t x = 1;
	while (atomic_load_explicit(&running, memory_order_relaxed))
		x = x * 6364136223846793005ULL + 1;
	return 0;
}

static int usage(void)
{
	fprintf(stderr,
		"usage: probe probe [--threads N] [--interval-us I] [--hist]\n"
		"       probe stress-worker --cpu C\n");
	return 2;
}

static bool parse_long(const char *s, long lo, long hi, long *out)
{
	char *end = nullptr;
	errno = 0;
	long v = strtol(s, &end, 10);
	if (errno || !end || *end || v < lo || v > hi)
		return false;
	*out = v;
	return true;
}

int main(int argc, char **argv)
{
	if (argc < 2)
		return usage();
	if (strcmp(argv[1], "probe") == 0) {
		long threads = 1, interval_us = 10000;
		bool hist = false;
		for (int i = 2; i < argc; i++) {
			if (strcmp(argv[i], "--hist") == 0) {
				hist = true;
			} else if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
				if (!parse_long(argv[++i], 1, MAX_THREADS, &threads))
					return usage();
			} else if (strcmp(argv[i], "--interval-us") == 0 && i + 1 < argc) {
				if (!parse_long(argv[++i], 1, 10000000, &interval_us))
					return usage();
			} else {
				return usage();
			}
		}
		return run_probe((int)threads, interval_us, hist);
	}
	if (strcmp(argv[1], "stress-worker") == 0) {
		long cpu = -1;
		if (argc == 4 && strcmp(argv[2], "--cpu") == 0 &&
		    parse_long(argv[3], 0, CPU_SETSIZE - 1, &cpu))
			return run_stress((int)cpu);
		return usage();
	}
	return usage();
}
