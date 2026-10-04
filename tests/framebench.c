// framebench: a synthetic game for scheduler comparison.
//
// The task graph follows what Igalia measured on Proton games running under
// sched_ext (LPC 2024, LPC 2025): tasks of a few hundred microseconds, chained
// through futex_wait, pipe_read and epoll, with a wineserver-style hub at the
// centre. Every unit of work is a fixed instruction count calibrated once, never
// a timed spin, so a scheduler that delays or splits a thread makes the frame
// take longer and the frame rate is the scheduler's result.
//
//   main     simulation, job fan-out/fan-in, hub round trips, input drain
//   job-N    worker pool woken by futex, jobs claimed by atomic index
//   render   frame N while main simulates N+1, hands to submit by futex
//   submit   short command-stream work, queues the frame to the GPU thread
//   gpu      a fixed GPU time per frame, then present (frame timestamp)
//   hub      epoll server answering pipe round trips from main and workers
//   audio    periodic, SCHED_FIFO when permitted
//   input    1 kHz timer, signals main through an eventfd
//   bg-N     optional CPU-bound background load, bg-spawn forks /bin/true
//
// Output: frame times in ns to --out, job wake latencies to --wake-out,
// audio lateness to --audio-out, and a key=value summary on stdout.
#define _GNU_SOURCE
#include "loadgen_common.h"
#include <errno.h>
#include <linux/futex.h>
#include <pthread.h>
#include <sched.h>
#include <spawn.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/timerfd.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

#define MAX_WORKERS 128
#define MAX_JOBS    1024
#define MAX_FRAMES  (1u << 20)
#define MAX_WAKES   (1u << 22)
#define MAX_AUDIO   (1u << 18)
#define STUTTER_WIN 20

static struct {
	double   ipu;              // cpu_quantum iterations per microsecond
	double   duration_s, warmup_s;
	unsigned workers, jobs, job_us_min, job_us_max;
	unsigned sim_us, render_us, submit_us, gpu_us;
	unsigned hub_rpcs, hub_us, worker_rpc_every;
	unsigned audio_period_us, audio_us, input_hz, cap_hz;
	unsigned bg_hogs, bg_spawn_ms, inflight;
	bool     audio_rt;
	const char *out, *wake_out, *audio_out;
} cfg = {
	.duration_s = 20, .warmup_s = 3,
	.workers = 4, .jobs = 16, .job_us_min = 50, .job_us_max = 400,
	.sim_us = 1500, .render_us = 1800, .submit_us = 300, .gpu_us = 1200,
	.inflight = 3,
	.hub_rpcs = 4, .hub_us = 20, .worker_rpc_every = 4,
	.audio_period_us = 5333, .audio_us = 250, .input_hz = 1000,
	.audio_rt = true,
};

static atomic_bool stop;
static atomic_bool measuring;
// Per thread: one shared sink would bounce a cache line between every core and
// make the work cost depend on placement.
static thread_local volatile unsigned long sink;

static void work_us(unsigned us)
{
	sink += cpu_quantum((unsigned long)(us * cfg.ipu));
}

static void name_self(const char *n)
{
	pthread_setname_np(pthread_self(), n);
}

// splitmix64 over (frame, job): every arm runs the identical job sizes.
static uint64_t mix(uint64_t x)
{
	x += 0x9e3779b97f4a7c15ULL;
	x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
	x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
	return x ^ (x >> 31);
}

static void fwait(atomic_int *w, int val)
{
	futex_op((int *)w, FUTEX_WAIT_PRIVATE, val);
}

static void fwake(atomic_int *w, int n)
{
	futex_op((int *)w, FUTEX_WAKE_PRIVATE, n);
}

// recorded samples
static uint64_t *frame_ns;   static atomic_uint n_frames;
static uint64_t *wake_ns;    static atomic_uint n_wakes;
static uint64_t *audio_late; static atomic_uint n_audio;
static atomic_uint stutters;
static atomic_ullong stutter_ns;

static void record(uint64_t *buf, atomic_uint *n, unsigned cap, uint64_t v)
{
	unsigned i = atomic_fetch_add(n, 1);
	if (i < cap)
		buf[i] = v;
}

// job system
static atomic_int job_gen;        // bumped once per frame to release workers
static atomic_int job_next;       // next job index to claim
static atomic_int jobs_left;      // main sleeps on this until it reaches 0
static atomic_ullong job_release_ns;
static unsigned job_size_us[MAX_JOBS];

// pipeline handoffs, each a frame count
static atomic_int sim_done;       // main -> render
static atomic_int rendered;       // render -> submit
static atomic_int submitted;      // submit -> gpu
static atomic_int presented;      // gpu -> main backpressure

// hub
struct client { int req[2], rep[2]; };
static struct client clients[MAX_WORKERS + 1];
static int input_efd;

static void hub_call(struct client *c)
{
	char b = 1;
	if (write(c->req[1], &b, 1) != 1 || read(c->rep[0], &b, 1) != 1)
		atomic_store(&stop, true);
}

static void *hub_thread(void *arg)
{
	(void)arg;
	name_self("fb-hub");
	int ep = epoll_create1(0);
	for (unsigned i = 0; i <= cfg.workers; i++) {
		struct epoll_event ev = { .events = EPOLLIN, .data.u32 = i };
		epoll_ctl(ep, EPOLL_CTL_ADD, clients[i].req[0], &ev);
	}
	struct epoll_event evs[MAX_WORKERS + 1];
	while (!atomic_load(&stop)) {
		int n = epoll_wait(ep, evs, MAX_WORKERS + 1, 100);
		for (int k = 0; k < n; k++) {
			struct client *c = &clients[evs[k].data.u32];
			char b;
			if (read(c->req[0], &b, 1) != 1)
				continue;
			work_us(cfg.hub_us);
			if (write(c->rep[1], &b, 1) != 1)
				atomic_store(&stop, true);
		}
	}
	close(ep);
	return NULL;
}

static void *worker_thread(void *arg)
{
	unsigned id = (unsigned)(uintptr_t)arg;
	char nm[16];
	snprintf(nm, sizeof(nm), "fb-job%u", id);
	name_self(nm);
	int seen = 0;
	unsigned done = 0;
	while (!atomic_load(&stop)) {
		int g = atomic_load(&job_gen);
		if (g == seen) {
			fwait(&job_gen, g);
			continue;
		}
		seen = g;
		uint64_t start = now_ns();
		if (atomic_load(&measuring))
			record(wake_ns, &n_wakes, MAX_WAKES, start - atomic_load(&job_release_ns));
		int j;
		while ((j = atomic_fetch_add(&job_next, 1)) < (int)cfg.jobs) {
			work_us(job_size_us[j]);
			if (cfg.worker_rpc_every && ++done % cfg.worker_rpc_every == 0)
				hub_call(&clients[id + 1]);
			if (atomic_fetch_sub(&jobs_left, 1) == 1)
				fwake(&jobs_left, 1);
		}
	}
	return NULL;
}

// One stage of the frame pipeline: wait for the upstream count to pass ours,
// do the stage's work, publish our count downstream.
static void stage(const char *name, atomic_int *up, atomic_int *down, unsigned us)
{
	name_self(name);
	int f = 0;
	while (!atomic_load(&stop)) {
		int u = atomic_load(up);
		if (u <= f) {
			fwait(up, u);
			continue;
		}
		work_us(us);
		atomic_store(down, ++f);
		fwake(down, 1);
	}
}

static void *render_thread(void *arg)
{
	(void)arg;
	stage("fb-render", &sim_done, &rendered, cfg.render_us);
	return NULL;
}

static void *submit_thread(void *arg)
{
	(void)arg;
	stage("fb-submit", &rendered, &submitted, cfg.submit_us);
	return NULL;
}

// The GPU executes a fixed time per frame. A frame time is the gap between two
// presents. A stutter follows CapFrameX: a frame longer than 2.5x the moving
// average of the preceding STUTTER_WIN frames.
static void *gpu_thread(void *arg)
{
	(void)arg;
	name_self("fb-gpu");
	int f = 0;
	uint64_t last = 0, ring[STUTTER_WIN] = {0}, rsum = 0;
	unsigned rn = 0;
	while (!atomic_load(&stop)) {
		int s = atomic_load(&submitted);
		if (s <= f) {
			fwait(&submitted, s);
			continue;
		}
		struct timespec ts = { .tv_sec = 0, .tv_nsec = (long)cfg.gpu_us * 1000 };
		clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, NULL);
		uint64_t now = now_ns();
		atomic_store(&presented, ++f);
		fwake(&presented, 1);
		if (last && atomic_load(&measuring)) {
			uint64_t ft = now - last;
			record(frame_ns, &n_frames, MAX_FRAMES, ft);
			// ft > 2.5 * rsum / STUTTER_WIN
			if (rn >= STUTTER_WIN && ft * STUTTER_WIN * 2 > rsum * 5) {
				atomic_fetch_add(&stutters, 1);
				atomic_fetch_add(&stutter_ns, ft);
			}
			unsigned slot = rn % STUTTER_WIN;
			if (rn >= STUTTER_WIN)
				rsum -= ring[slot];
			ring[slot] = ft;
			rsum += ft;
			rn++;
		}
		last = now;
	}
	return NULL;
}

static void *audio_thread(void *arg)
{
	(void)arg;
	name_self("fb-audio");
	if (cfg.audio_rt) {
		struct sched_param sp = { .sched_priority = 10 };
		if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0)
			cfg.audio_rt = false;
	}
	struct timespec next;
	clock_gettime(CLOCK_MONOTONIC, &next);
	while (!atomic_load(&stop)) {
		long ns = next.tv_nsec + (long)cfg.audio_period_us * 1000;
		next.tv_sec += ns / 1000000000L;
		next.tv_nsec = ns % 1000000000L;
		clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
		uint64_t woke = now_ns();
		uint64_t due = (uint64_t)ts_ns(next);
		if (atomic_load(&measuring))
			record(audio_late, &n_audio, MAX_AUDIO, woke > due ? woke - due : 0);
		work_us(cfg.audio_us);
	}
	return NULL;
}

static void *input_thread(void *arg)
{
	(void)arg;
	name_self("fb-input");
	int tfd = timerfd_create(CLOCK_MONOTONIC, 0);
	long period = 1000000000L / (long)cfg.input_hz;
	struct itimerspec its = { .it_interval = { 0, period }, .it_value = { 0, period } };
	timerfd_settime(tfd, 0, &its, NULL);
	uint64_t exp, one = 1;
	while (!atomic_load(&stop)) {
		if (read(tfd, &exp, sizeof(exp)) != sizeof(exp))
			break;
		work_us(5);
		if (write(input_efd, &one, sizeof(one)) != sizeof(one))
			break;
	}
	close(tfd);
	return NULL;
}

static void *bg_hog(void *arg)
{
	char nm[16];
	snprintf(nm, sizeof(nm), "fb-bg%u", (unsigned)(uintptr_t)arg);
	name_self(nm);
	while (!atomic_load(&stop))
		work_us(1000);
	return NULL;
}

static void *bg_spawner(void *arg)
{
	(void)arg;
	name_self("fb-spawn");
	char *argv[] = { "/bin/true", NULL };
	while (!atomic_load(&stop)) {
		pid_t pid;
		if (posix_spawn(&pid, argv[0], NULL, NULL, argv, environ) == 0)
			waitpid(pid, NULL, 0);
		struct timespec ts = { .tv_sec = 0, .tv_nsec = (long)cfg.bg_spawn_ms * 1000000L };
		clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, NULL);
	}
	return NULL;
}

static void run_frame(uint64_t frame)
{
	work_us(cfg.sim_us);
	for (unsigned j = 0; j < cfg.jobs; j++) {
		uint64_t r = mix(0x5eedULL * (frame + 1) + j);
		job_size_us[j] = cfg.job_us_min +
			(unsigned)(r % (cfg.job_us_max - cfg.job_us_min + 1));
	}
	// jobs_left before job_next: a worker still draining the previous
	// generation claims from job_next, and must find the count already set.
	atomic_store(&jobs_left, (int)cfg.jobs);
	atomic_store(&job_next, 0);
	atomic_store(&job_release_ns, now_ns());
	atomic_fetch_add(&job_gen, 1);
	fwake(&job_gen, INT32_MAX);
	int left;
	while ((left = atomic_load(&jobs_left)) > 0)
		fwait(&jobs_left, left);
	for (unsigned k = 0; k < cfg.hub_rpcs; k++)
		hub_call(&clients[0]);
	uint64_t drained;
	if (read(input_efd, &drained, sizeof(drained)) < 0 && errno != EAGAIN)
		atomic_store(&stop, true);
}

// Best of seven: the unperturbed instruction rate of this machine.
static double calibrate(void)
{
	double best = 0;
	for (int t = 0; t < 7; t++) {
		unsigned long iters = 20000000UL;
		uint64_t a = now_ns();
		sink += cpu_quantum(iters);
		uint64_t b = now_ns();
		double ipu = (double)iters / ((double)(b - a) / 1000.0);
		if (ipu > best)
			best = ipu;
	}
	return best;
}

static int dump(const char *path, const uint64_t *buf, unsigned n)
{
	if (!path)
		return 0;
	FILE *f = fopen(path, "w");
	if (!f)
		return -1;
	for (unsigned i = 0; i < n; i++)
		fprintf(f, "%lu\n", (unsigned long)buf[i]);
	return fclose(f);
}

static unsigned uarg(const char *v) { return (unsigned)strtoul(v, NULL, 10); }

static void wake_all(atomic_int *w)
{
	atomic_fetch_add(w, 1);
	fwake(w, INT32_MAX);
}

int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : "0";
		if (!strcmp(a, "--calibrate")) {
			printf("ipu=%.3f\n", calibrate());
			return 0;
		}
		if (!strcmp(a, "--no-audio-rt")) { cfg.audio_rt = false; continue; }
		i++;
		if      (!strcmp(a, "--ipu"))              cfg.ipu = strtod(v, NULL);
		else if (!strcmp(a, "--duration"))         cfg.duration_s = strtod(v, NULL);
		else if (!strcmp(a, "--warmup"))           cfg.warmup_s = strtod(v, NULL);
		else if (!strcmp(a, "--workers"))          cfg.workers = uarg(v);
		else if (!strcmp(a, "--jobs"))             cfg.jobs = uarg(v);
		else if (!strcmp(a, "--job-us-min"))       cfg.job_us_min = uarg(v);
		else if (!strcmp(a, "--job-us-max"))       cfg.job_us_max = uarg(v);
		else if (!strcmp(a, "--sim-us"))           cfg.sim_us = uarg(v);
		else if (!strcmp(a, "--render-us"))        cfg.render_us = uarg(v);
		else if (!strcmp(a, "--submit-us"))        cfg.submit_us = uarg(v);
		else if (!strcmp(a, "--gpu-us"))           cfg.gpu_us = uarg(v);
		else if (!strcmp(a, "--hub-rpcs"))         cfg.hub_rpcs = uarg(v);
		else if (!strcmp(a, "--hub-us"))           cfg.hub_us = uarg(v);
		else if (!strcmp(a, "--worker-rpc-every")) cfg.worker_rpc_every = uarg(v);
		else if (!strcmp(a, "--audio-period-us"))  cfg.audio_period_us = uarg(v);
		else if (!strcmp(a, "--audio-us"))         cfg.audio_us = uarg(v);
		else if (!strcmp(a, "--input-hz"))         cfg.input_hz = uarg(v);
		else if (!strcmp(a, "--cap-hz"))           cfg.cap_hz = uarg(v);
		else if (!strcmp(a, "--bg-hogs"))          cfg.bg_hogs = uarg(v);
		else if (!strcmp(a, "--bg-spawn-ms"))      cfg.bg_spawn_ms = uarg(v);
		else if (!strcmp(a, "--inflight"))         cfg.inflight = uarg(v);
		else if (!strcmp(a, "--out"))              cfg.out = v;
		else if (!strcmp(a, "--wake-out"))         cfg.wake_out = v;
		else if (!strcmp(a, "--audio-out"))        cfg.audio_out = v;
		else {
			fprintf(stderr, "framebench: unknown option %s\n", a);
			return 2;
		}
	}
	if (cfg.ipu <= 0 || cfg.workers < 1 || cfg.workers > MAX_WORKERS ||
	    cfg.jobs < 1 || cfg.jobs > MAX_JOBS || cfg.job_us_max < cfg.job_us_min ||
	    cfg.bg_hogs > MAX_WORKERS || cfg.inflight < 1) {
		fprintf(stderr, "framebench: need --ipu > 0, 1..%d workers, 1..%d jobs, "
			"at most %d bg hogs, --inflight >= 1\n", MAX_WORKERS, MAX_JOBS, MAX_WORKERS);
		return 2;
	}

	frame_ns = calloc(MAX_FRAMES, sizeof(uint64_t));
	wake_ns = calloc(MAX_WAKES, sizeof(uint64_t));
	audio_late = calloc(MAX_AUDIO, sizeof(uint64_t));
	input_efd = eventfd(0, EFD_NONBLOCK);
	if (!frame_ns || !wake_ns || !audio_late || input_efd < 0)
		return 1;
	for (unsigned i = 0; i <= cfg.workers; i++)
		if (pipe(clients[i].req) || pipe(clients[i].rep))
			return 1;
	name_self("fb-main");

	pthread_t th[MAX_WORKERS + 8];
	unsigned nt = 0;
	pthread_create(&th[nt++], NULL, hub_thread, NULL);
	for (unsigned i = 0; i < cfg.workers; i++)
		pthread_create(&th[nt++], NULL, worker_thread, (void *)(uintptr_t)i);
	pthread_create(&th[nt++], NULL, render_thread, NULL);
	pthread_create(&th[nt++], NULL, submit_thread, NULL);
	pthread_create(&th[nt++], NULL, gpu_thread, NULL);
	if (cfg.audio_period_us)
		pthread_create(&th[nt++], NULL, audio_thread, NULL);
	if (cfg.input_hz)
		pthread_create(&th[nt++], NULL, input_thread, NULL);
	pthread_t bg[MAX_WORKERS + 1];
	unsigned nbg = 0;
	for (unsigned i = 0; i < cfg.bg_hogs; i++)
		pthread_create(&bg[nbg++], NULL, bg_hog, (void *)(uintptr_t)i);
	if (cfg.bg_spawn_ms)
		pthread_create(&bg[nbg++], NULL, bg_spawner, NULL);

	uint64_t t0 = now_ns();
	uint64_t warm_end = t0 + (uint64_t)(cfg.warmup_s * 1e9);
	uint64_t end = warm_end + (uint64_t)(cfg.duration_s * 1e9);
	uint64_t period = cfg.cap_hz ? 1000000000ULL / cfg.cap_hz : 0;
	uint64_t next = t0, measured_from = 0;
	for (uint64_t f = 0; !atomic_load(&stop); f++) {
		uint64_t now = now_ns();
		if (now >= end)
			break;
		if (!measured_from && now >= warm_end) {
			measured_from = now;
			atomic_store(&measuring, true);
		}
		if (period) {
			next += period;
			struct timespec ts = { .tv_sec = (time_t)(next / 1000000000ULL),
					       .tv_nsec = (long)(next % 1000000000ULL) };
			clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
		}
		run_frame(f);
		atomic_store(&sim_done, (int)(f + 1));
		fwake(&sim_done, 1);
		// At most `inflight` frames between simulated and presented (DXGI's
		// default maximum frame latency is 3).
		int p;
		while ((p = atomic_load(&presented)) + (int)cfg.inflight <= (int)(f + 1) &&
		       !atomic_load(&stop))
			fwait(&presented, p);
	}
	uint64_t measured_ns = now_ns() - (measured_from ? measured_from : t0);
	atomic_store(&stop, true);
	wake_all(&job_gen);
	wake_all(&sim_done);
	wake_all(&rendered);
	wake_all(&submitted);
	for (unsigned i = 0; i <= cfg.workers; i++) {
		char b = 0;
		if (write(clients[i].rep[1], &b, 1) != 1)
			break;
	}
	for (unsigned i = 0; i < nbg; i++)
		pthread_join(bg[i], NULL);
	for (unsigned i = 0; i < nt; i++)
		pthread_join(th[i], NULL);

	unsigned nf = atomic_load(&n_frames), nw = atomic_load(&n_wakes);
	unsigned na = atomic_load(&n_audio);
	nf = nf < MAX_FRAMES ? nf : MAX_FRAMES;
	nw = nw < MAX_WAKES ? nw : MAX_WAKES;
	na = na < MAX_AUDIO ? na : MAX_AUDIO;
	if (dump(cfg.out, frame_ns, nf) || dump(cfg.wake_out, wake_ns, nw) ||
	    dump(cfg.audio_out, audio_late, na)) {
		fprintf(stderr, "framebench: could not write output\n");
		return 1;
	}
	printf("frames=%u\n", nf);
	printf("measured_s=%.3f\n", (double)measured_ns / 1e9);
	printf("stutters=%u\n", atomic_load(&stutters));
	printf("stutter_ns=%llu\n", (unsigned long long)atomic_load(&stutter_ns));
	printf("job_wakes=%u\n", nw);
	printf("audio_periods=%u\n", na);
	printf("audio_rt=%d\n", cfg.audio_rt ? 1 : 0);
	return 0;
}
