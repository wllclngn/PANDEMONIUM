// PANDEMONIUM v1.0 -- SCHED_EXT KERNEL SCHEDULER
// ADAPTIVE DESKTOP SCHEDULING FOR LINUX
//
// BPF: BEHAVIORAL CLASSIFICATION + MULTI-TIER DISPATCH
// RUST: ADAPTIVE CONTROL LOOP + REAL-TIME TELEMETRY
//
// ARCHITECTURE:
//   SELECT_CPU IDLE FAST PATH -> SCX_DSQ_LOCAL (ZERO CONTENTION)
//   ENQUEUE IDLE FOUND -> PER-CPU DSQ DIRECT PLACEMENT (ZERO CONTENTION)
//   ENQUEUE INTERACTIVE PREEMPT -> PER-CPU DSQ + HARD KICK
//   ENQUEUE FALLBACK -> PER-NODE OVERFLOW DSQ (VTIME-ORDERED)
//   DISPATCH -> PER-CPU DSQ, NODE OVERFLOW, CROSS-NODE STEAL, KEEP_RUNNING
//   BPF TIMER -> PREEMPTION ENFORCEMENT (NO_HZ_FULL PROOF)
//   TICK -> BATCH PREEMPTION WHEN INTERACTIVE WORK WAITING
//
// BEHAVIORAL CLASSIFICATION (FROM v0.9.4):
//   LAT_CRI SCORE = (WAKEUP_FREQ * CSW_RATE) / AVG_RUNTIME
//   THREE TIERS: LAT_CRITICAL, INTERACTIVE, BATCH
//   PER-TIER SLICING: 1.5X AVG_RUNTIME, 2X AVG_RUNTIME, KNOB BASE
//   COMPOSITOR AUTO-BOOST TO LAT_CRITICAL

#include <scx/common.bpf.h>
#include <scx/compat.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

// --- CONFIGURATION (SET BY RUST VIA RODATA BEFORE LOAD) ---

const volatile u64 nr_cpu_ids = 1;
const volatile bool ringbuf_active = false;

// --- BEHAVIORAL CONSTANTS ---

#define TIER_BATCH        0
#define TIER_INTERACTIVE  1
#define TIER_LAT_CRITICAL 2

#define LAT_CRI_THRESH_HIGH  32
#define LAT_CRI_THRESH_LOW   8
#define LAT_CRI_CAP          255

#define WEIGHT_LAT_CRITICAL  256   // 2X
#define WEIGHT_INTERACTIVE   192   // 1.5X
#define WEIGHT_BATCH         128   // 1X

#define EWMA_AGE_MATURE      8
#define EWMA_AGE_CAP         16
#define MAX_WAKEUP_FREQ      64
#define MAX_CSW_RATE         512
#define LAG_CAP_NS           (40ULL * 1000000ULL)

#define SLICE_MIN_NS 100000     // 100US FLOOR

// --- GLOBALS ---

static u32 nr_nodes;
static u64 vtime_now;
static u64 preempt_thresh = 10;

// TICK-BASED INTERACTIVE PREEMPTION SIGNAL
// SET BY enqueue() WHEN NON-BATCH TASK HITS OVERFLOW DSQ.
// CLEARED BY tick() AFTER PREEMPTING A BATCH TASK.
static bool interactive_waiting;

// INTERACTIVE GUARDRAIL: TIME-BASED BATCH SLICE CLAMP
// SET IN enqueue() WHEN NON-BATCH TASK HITS OVERFLOW DSQ.
// CHECKED IN task_slice() TO CLAMP BATCH SLICES DURING GUARD WINDOW.
static u64 guard_until_ns;

// --- USER EXIT ---

UEI_DEFINE(uei);

// --- MAPS ---

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct tuning_knobs);
} tuning_knobs_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct pandemonium_stats);
} stats_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, u64);
} idle_bitmap SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024);
} wake_lat_rb SEC(".maps");

struct timer_ctx {
	struct bpf_timer timer;
};

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct timer_ctx);
} timer_map SEC(".maps");

// --- PER-TASK CONTEXT ---

struct task_ctx {
	u64 awake_vtime;
	u64 last_run_at;
	u64 wakeup_freq;
	u64 last_woke_at;
	u64 avg_runtime;
	u64 cached_weight;
	u64 prev_nvcsw;
	u64 csw_rate;
	u64 lat_cri;
	u32 tier;
	u32 ewma_age;
	u8  dispatch_path;   // 0=IDLE, 1=HARD_KICK, 2=SOFT_KICK
	u8  _pad[3];
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

// --- HELPERS ---

static __always_inline struct pandemonium_stats *get_stats(void)
{
	u32 zero = 0;
	return bpf_map_lookup_elem(&stats_map, &zero);
}

static __always_inline struct tuning_knobs *get_knobs(void)
{
	u32 zero = 0;
	return bpf_map_lookup_elem(&tuning_knobs_map, &zero);
}

static __always_inline struct task_ctx *lookup_task_ctx(const struct task_struct *p)
{
	return bpf_task_storage_get(&task_ctx_stor,
				    (struct task_struct *)p, 0, 0);
}

static __always_inline struct task_ctx *ensure_task_ctx(struct task_struct *p)
{
	struct task_ctx zero = {};
	return bpf_task_storage_get(&task_ctx_stor, p, &zero,
				    BPF_LOCAL_STORAGE_GET_F_CREATE);
}

// --- EWMA ---

static __always_inline u64 calc_avg(u64 old_val, u64 new_val, u32 age)
{
	if (age < EWMA_AGE_MATURE)
		return (old_val >> 1) + (new_val >> 1);
	return old_val - (old_val >> 3) + (new_val >> 3);
}

static __always_inline u64 update_freq(u64 freq, u64 interval_ns, u32 age)
{
	if (interval_ns == 0)
		interval_ns = 1;
	u64 new_freq = (100ULL * 1000000ULL) / interval_ns;
	return calc_avg(freq, new_freq, age);
}

// --- BEHAVIORAL CLASSIFICATION ---

// LAT_CRI SCORE: HIGH WAKEUP FREQ + HIGH CSW RATE + SHORT RUNTIME = CRITICAL
static __always_inline u64 compute_lat_cri(u64 wakeup_freq, u64 csw_rate,
					    u64 avg_runtime_ns)
{
	u64 avg_runtime_ms = avg_runtime_ns >> 20;
	if (avg_runtime_ms == 0)
		avg_runtime_ms = 1;
	u64 score = (wakeup_freq * csw_rate) / avg_runtime_ms;
	if (score > LAT_CRI_CAP)
		score = LAT_CRI_CAP;
	return score;
}

static __always_inline u32 classify_tier(u64 lat_cri)
{
	if (lat_cri >= LAT_CRI_THRESH_HIGH)
		return TIER_LAT_CRITICAL;
	if (lat_cri >= LAT_CRI_THRESH_LOW)
		return TIER_INTERACTIVE;
	return TIER_BATCH;
}

// COMPOSITOR DETECTION: ALWAYS LAT_CRITICAL
static __always_inline bool is_compositor(const struct task_struct *p)
{
	char c0 = p->comm[0];

	switch (c0) {
	case 'k':
		if (p->comm[1] == 'w' && p->comm[2] == 'i' &&
		    p->comm[3] == 'n')
			return true;
		break;
	case 'g':
		if (p->comm[1] == 'n' && p->comm[2] == 'o' &&
		    p->comm[3] == 'm' && p->comm[4] == 'e' &&
		    p->comm[5] == '-' && p->comm[6] == 's')
			return true;
		break;
	case 's':
		if (p->comm[1] == 'w' && p->comm[2] == 'a' &&
		    p->comm[3] == 'y' && p->comm[4] == '\0')
			return true;
		break;
	case 'H':
		if (p->comm[1] == 'y' && p->comm[2] == 'p' &&
		    p->comm[3] == 'r' && p->comm[4] == 'l' &&
		    p->comm[5] == 'a' && p->comm[6] == 'n' &&
		    p->comm[7] == 'd' && p->comm[8] == '\0')
			return true;
		break;
	case 'p':
		if (p->comm[1] == 'i' && p->comm[2] == 'c' &&
		    p->comm[3] == 'o' && p->comm[4] == 'm' &&
		    p->comm[5] == '\0')
			return true;
		break;
	case 'w':
		if (p->comm[1] == 'e' && p->comm[2] == 's' &&
		    p->comm[3] == 't' && p->comm[4] == 'o' &&
		    p->comm[5] == 'n' && p->comm[6] == '\0')
			return true;
		break;
	}

	return false;
}

// EFFECTIVE WEIGHT: TIER-BASED MULTIPLIER ON NICE WEIGHT
static __always_inline u64 effective_weight(const struct task_struct *p,
					     const struct task_ctx *tctx)
{
	u64 weight = p->scx.weight;
	u64 behavioral;

	if (tctx->tier == TIER_LAT_CRITICAL)
		behavioral = WEIGHT_LAT_CRITICAL;
	else if (tctx->tier == TIER_INTERACTIVE)
		behavioral = WEIGHT_INTERACTIVE;
	else
		behavioral = WEIGHT_BATCH;

	return weight * behavioral >> 7;
}

// --- SCHEDULING HELPERS ---

// DEADLINE = DSQ_VTIME + AWAKE_VTIME
// PER-TASK LAG SCALING: INTERACTIVE TASKS GET MORE VTIME CREDIT
// QUEUE-PRESSURE SCALING: CREDIT SHRINKS WHEN DSQ IS DEEP
// TIER-BASED AWAKE CAP: PREVENTS BOOST EXPLOITATION
static __always_inline u64 task_deadline(struct task_struct *p,
					 struct task_ctx *tctx,
					 u64 dsq_id,
					 const struct tuning_knobs *knobs)
{
	u64 knob_scale = knobs ? knobs->lag_scale : 4;
	u64 lag_scale = (tctx->wakeup_freq * knob_scale) >> 2;
	if (lag_scale < 1)
		lag_scale = 1;
	if (lag_scale > MAX_WAKEUP_FREQ)
		lag_scale = MAX_WAKEUP_FREQ;

	// QUEUE-PRESSURE SCALING
	u64 nr_queued = scx_bpf_dsq_nr_queued(dsq_id);
	if (nr_queued > 8)
		lag_scale = 1;
	else if (nr_queued > 4 && lag_scale > 2)
		lag_scale >>= 1;

	// CLAMP VTIME TO PREVENT UNBOUNDED BOOST AFTER LONG SLEEP
	u64 vtime_floor = vtime_now - LAG_CAP_NS * lag_scale;
	if (time_before(p->scx.dsq_vtime, vtime_floor))
		p->scx.dsq_vtime = vtime_floor;

	// TIER-BASED AWAKE CAP
	u64 awake_cap;
	if (tctx->tier == TIER_LAT_CRITICAL)
		awake_cap = 20ULL * 1000000ULL;
	else if (tctx->tier == TIER_INTERACTIVE)
		awake_cap = 30ULL * 1000000ULL;
	else
		awake_cap = LAG_CAP_NS;

	if (tctx->awake_vtime > awake_cap)
		tctx->awake_vtime = awake_cap;

	return p->scx.dsq_vtime + tctx->awake_vtime;
}

// PER-TIER DYNAMIC SLICING
// LAT_CRITICAL: 1.5X AVG_RUNTIME (TIGHT -- FAST PREEMPTION)
// INTERACTIVE:  2X AVG_RUNTIME (RESPONSIVE)
// BATCH:        KNOB BASE SLICE (CONTROLLED BY ADAPTIVE LAYER)
static __always_inline u64 task_slice(const struct task_ctx *tctx,
				      const struct tuning_knobs *knobs)
{
	u64 base_slice = knobs ? knobs->slice_ns : 1000000;
	u64 base;

	if (tctx->tier == TIER_LAT_CRITICAL) {
		base = tctx->avg_runtime + (tctx->avg_runtime >> 1);
		if (base > base_slice)
			base = base_slice;
		if (base < SLICE_MIN_NS)
			base = SLICE_MIN_NS;
		return base;
	}

	if (tctx->tier == TIER_INTERACTIVE) {
		base = tctx->avg_runtime << 1;
		if (base > base_slice)
			base = base_slice;
		if (base < SLICE_MIN_NS)
			base = SLICE_MIN_NS;
		return base;
	}

	// BATCH: FULL KNOB SLICE, CLAMPED DURING INTERACTIVE GUARD WINDOW
	if (base_slice < SLICE_MIN_NS)
		base_slice = SLICE_MIN_NS;

	if (bpf_ktime_get_ns() < guard_until_ns) {
		u64 guard_slice = SLICE_MIN_NS << 1; // 200US
		if (base_slice > guard_slice) {
			base_slice = guard_slice;
			struct pandemonium_stats *s = get_stats();
			if (s)
				__sync_fetch_and_add(&s->nr_guard_clamps, 1);
		}
	}

	return base_slice;
}

// --- BPF TIMER: PREEMPTION ENFORCEMENT ---
// FIRES EVERY ~1MS. INDEPENDENT OF KERNEL TICK.
// RELIABLE UNDER NO_HZ_FULL.

static int preempt_timerfn(void *map, int *key, struct bpf_timer *timer)
{
	struct tuning_knobs *knobs = get_knobs();
	u64 thresh = knobs ? knobs->preempt_thresh_ns : 1000000;
	u64 now = bpf_ktime_get_ns();

	bpf_rcu_read_lock();

	int cpu = 0;
	bpf_for(cpu, 0, nr_cpu_ids) {
		if ((u64)cpu >= MAX_CPUS)
			break;

		struct task_struct *p = __COMPAT_scx_bpf_cpu_curr(cpu);
		if (!p || p->flags & PF_IDLE)
			continue;

		// ONLY PREEMPT IF QUEUED WORK EXISTS
		s32 node = __COMPAT_scx_bpf_cpu_node(cpu);
		if (!scx_bpf_dsq_nr_queued(nr_cpu_ids + (u64)node) &&
		    !scx_bpf_dsq_nr_queued((u64)cpu))
			continue;

		struct task_ctx *tctx = lookup_task_ctx(p);
		if (!tctx || !tctx->last_run_at)
			continue;

		u64 running = now > tctx->last_run_at ? now - tctx->last_run_at : 0;
		if (running > thresh) {
			p->scx.slice = 0;
			scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
			struct pandemonium_stats *s = get_stats();
			if (s)
				__sync_fetch_and_add(&s->nr_preempt, 1);
		}
	}

	bpf_rcu_read_unlock();

	// IDLE BITMAP SNAPSHOT
	u64 idle_mask = 0;
	u32 idle_key = 0;
	for (u32 n = 0; n < nr_nodes && n < MAX_NODES; n++) {
		const struct cpumask *idle = __COMPAT_scx_bpf_get_idle_cpumask_node(n);
		int i = 0;
		bpf_for(i, 0, IDLE_BITMAP_CPUS) {
			if (bpf_cpumask_test_cpu(i, idle))
				idle_mask |= 1ULL << i;
		}
		scx_bpf_put_idle_cpumask(idle);
	}
	bpf_map_update_elem(&idle_bitmap, &idle_key, &idle_mask, 0);

	// RESTART TIMER
	u64 interval = knobs ? knobs->slice_ns : 1000000;
	if (interval < 500000)
		interval = 500000;
	bpf_timer_start(timer, interval, 0);

	return 0;
}

// --- SCHEDULING CALLBACKS ---

// SELECT_CPU: FAST-PATH IDLE CPU DISPATCH
s32 BPF_STRUCT_OPS(pandemonium_select_cpu, struct task_struct *p,
		   s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;
	s32 cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);

	if (is_idle) {
		struct task_ctx *tctx = lookup_task_ctx(p);
		struct tuning_knobs *knobs = get_knobs();
		u64 sl = tctx ? task_slice(tctx, knobs) : 1000000;
		scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, sl, 0);

		if (tctx)
			tctx->dispatch_path = 0;

		struct pandemonium_stats *s = get_stats();
		if (s) {
			s->nr_idle_hits += 1;
			s->nr_dispatches += 1;
		}
	}

	return cpu;
}

// ENQUEUE: THREE-TIER PLACEMENT WITH BEHAVIORAL PREEMPTION
// TIER 1: IDLE CPU ON NODE -> DIRECT PER-CPU DSQ (ZERO CONTENTION)
// TIER 2: INTERACTIVE/LAT_CRITICAL -> DIRECT PER-CPU DSQ + HARD PREEMPT
// TIER 3: FALLBACK -> NODE OVERFLOW DSQ + SELECTIVE KICK
void BPF_STRUCT_OPS(pandemonium_enqueue, struct task_struct *p,
		    u64 enq_flags)
{
	s32 node = __COMPAT_scx_bpf_cpu_node(scx_bpf_task_cpu(p));
	u64 node_dsq = nr_cpu_ids + (u64)node;

	struct task_ctx *tctx = lookup_task_ctx(p);
	struct tuning_knobs *knobs = get_knobs();
	u64 sl = tctx ? task_slice(tctx, knobs) : 1000000;
	u64 dl;

	// CLASSIFY: WAKEUP VS RE-ENQUEUE
	bool is_wakeup = tctx && tctx->awake_vtime == 0;

	// TIER 1: IDLE CPU -> DIRECT PER-CPU DSQ
	s32 cpu = __COMPAT_scx_bpf_pick_idle_cpu_node(p->cpus_ptr, node, 0);
	if (cpu >= 0) {
		dl = tctx ? task_deadline(p, tctx, node_dsq, knobs)
			  : vtime_now;
		scx_bpf_dsq_insert_vtime(p, (u64)cpu, sl, dl, enq_flags);
		scx_bpf_kick_cpu(cpu, SCX_KICK_IDLE);

		if (tctx)
			tctx->dispatch_path = 0;

		struct pandemonium_stats *s = get_stats();
		if (s) {
			s->nr_shared += 1;
			s->nr_dispatches += 1;
			if (is_wakeup)
				s->nr_enq_wakeup += 1;
			else
				s->nr_enq_requeue += 1;
		}
		return;
	}

	// TIER 2: INTERACTIVE PREEMPTION -- DIRECT PER-CPU DSQ + HARD KICK
	// LAT_CRITICAL ALWAYS GETS PREEMPTION.
	// INTERACTIVE GETS IT IF FREQ > THRESHOLD OR SHORT RUNTIME.
	if (tctx &&
	    (tctx->tier == TIER_LAT_CRITICAL ||
	     (tctx->tier == TIER_INTERACTIVE &&
	      (tctx->wakeup_freq > preempt_thresh ||
	       tctx->avg_runtime < (knobs ? knobs->slice_ns : 1000000))))) {
		cpu = __COMPAT_scx_bpf_pick_any_cpu_node(
			p->cpus_ptr, node, 0);
		if (cpu >= 0) {
			dl = task_deadline(p, tctx, node_dsq, knobs);
			scx_bpf_dsq_insert_vtime(p, (u64)cpu, sl, dl,
						  enq_flags);
			scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
			tctx->dispatch_path = 1;

			struct pandemonium_stats *s = get_stats();
			if (s) {
				s->nr_shared += 1;
				s->nr_dispatches += 1;
				s->nr_hard_kicks += 1;
				if (is_wakeup)
					s->nr_enq_wakeup += 1;
				else
					s->nr_enq_requeue += 1;
			}
			return;
		}
	}

	// TIER 3: NODE OVERFLOW DSQ + SELECTIVE KICK
	dl = tctx ? task_deadline(p, tctx, node_dsq, knobs) : vtime_now;
	scx_bpf_dsq_insert_vtime(p, node_dsq, sl, dl, enq_flags);

	// ARM TICK SAFETY NET + INTERACTIVE GUARDRAIL
	if (tctx && tctx->tier != TIER_BATCH) {
		interactive_waiting = true;
		guard_until_ns = bpf_ktime_get_ns() + 1000000; // 1MS GUARD WINDOW
	}

	u64 kick_flags = is_wakeup ? SCX_KICK_PREEMPT : 0;
	scx_bpf_kick_cpu(scx_bpf_task_cpu(p), kick_flags);

	if (tctx)
		tctx->dispatch_path = is_wakeup ? 1 : 2;

	struct pandemonium_stats *s = get_stats();
	if (s) {
		s->nr_shared += 1;
		if (is_wakeup) {
			s->nr_enq_wakeup += 1;
			s->nr_hard_kicks += 1;
		} else {
			s->nr_enq_requeue += 1;
			s->nr_soft_kicks += 1;
		}
	}
}

// DISPATCH: CPU IS IDLE AND NEEDS WORK
// 1. OWN PER-CPU DSQ (DIRECT PLACEMENT FROM ENQUEUE -- ZERO CONTENTION)
// 2. OWN NODE'S OVERFLOW DSQ (NUMA-LOCAL)
// 3. CROSS-NODE STEAL (LAST RESORT)
// 4. KEEP_RUNNING IF PREV STILL WANTS CPU AND NOTHING QUEUED
void BPF_STRUCT_OPS(pandemonium_dispatch, s32 cpu, struct task_struct *prev)
{
	s32 node = __COMPAT_scx_bpf_cpu_node(cpu);
	struct pandemonium_stats *s;

	// PER-CPU DSQ: DIRECT PLACEMENT FROM ENQUEUE
	if (scx_bpf_dsq_move_to_local((u64)cpu)) {
		s = get_stats();
		if (s)
			s->nr_dispatches += 1;
		return;
	}

	// NODE OVERFLOW DSQ
	if (scx_bpf_dsq_move_to_local(nr_cpu_ids + (u64)node)) {
		s = get_stats();
		if (s)
			s->nr_dispatches += 1;
		return;
	}

	// CROSS-NODE STEAL
	for (u32 n = 0; n < nr_nodes && n < MAX_NODES; n++) {
		if (n != (u32)node &&
		    scx_bpf_dsq_move_to_local(nr_cpu_ids + (u64)n)) {
			s = get_stats();
			if (s)
				s->nr_dispatches += 1;
			return;
		}
	}

	// NOTHING IN ANY DSQ -- KEEP PREV RUNNING IF POSSIBLE
	if (prev && !(prev->flags & PF_EXITING) &&
	    (prev->scx.flags & SCX_TASK_QUEUED)) {
		struct task_ctx *tctx = lookup_task_ctx(prev);
		struct tuning_knobs *knobs = get_knobs();
		prev->scx.slice = tctx ? task_slice(tctx, knobs) :
				  (knobs ? knobs->slice_ns : 1000000);
		s = get_stats();
		if (s) {
			s->nr_keep_running += 1;
			s->nr_dispatches += 1;
		}
	}
}

// RUNNABLE: TASK WAKES UP -- BEHAVIORAL CLASSIFICATION ENGINE
void BPF_STRUCT_OPS(pandemonium_runnable, struct task_struct *p,
		    u64 enq_flags)
{
	struct task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	u64 now = bpf_ktime_get_ns();
	tctx->awake_vtime = 0;

	// FAST PATH: BRAND-NEW TASKS (< 2 WAKEUPS)
	if (tctx->ewma_age < 2) {
		tctx->last_woke_at = now;
		tctx->prev_nvcsw = p->nvcsw;
		tctx->ewma_age += 1;
		return;
	}

	// WAKEUP FREQUENCY
	u64 delta_t = now > tctx->last_woke_at ? now - tctx->last_woke_at : 1;
	tctx->wakeup_freq = update_freq(tctx->wakeup_freq, delta_t,
					 tctx->ewma_age);
	if (tctx->wakeup_freq > MAX_WAKEUP_FREQ)
		tctx->wakeup_freq = MAX_WAKEUP_FREQ;
	tctx->last_woke_at = now;

	if (tctx->ewma_age < EWMA_AGE_CAP)
		tctx->ewma_age += 1;

	// VOLUNTARY CONTEXT SWITCH RATE
	u64 nvcsw = p->nvcsw;
	u64 csw_delta = nvcsw > tctx->prev_nvcsw ? nvcsw - tctx->prev_nvcsw : 0;
	tctx->prev_nvcsw = nvcsw;

	if (csw_delta > 0 && delta_t > 0) {
		u64 csw_freq = csw_delta * (100ULL * 1000000ULL) / delta_t;
		tctx->csw_rate = calc_avg(tctx->csw_rate, csw_freq,
					   tctx->ewma_age);
	} else {
		tctx->csw_rate = calc_avg(tctx->csw_rate, 0, tctx->ewma_age);
	}
	if (tctx->csw_rate > MAX_CSW_RATE)
		tctx->csw_rate = MAX_CSW_RATE;

	// BEHAVIORAL CLASSIFICATION
	tctx->lat_cri = compute_lat_cri(tctx->wakeup_freq, tctx->csw_rate,
					 tctx->avg_runtime);
	u32 new_tier = classify_tier(tctx->lat_cri);

	// COMPOSITOR BOOST: ALWAYS LAT_CRITICAL
	if (new_tier != TIER_LAT_CRITICAL && is_compositor(p))
		new_tier = TIER_LAT_CRITICAL;

	tctx->tier = new_tier;
}

// RUNNING: TASK STARTS EXECUTING -- ADVANCE VTIME, RECORD WAKE LATENCY
void BPF_STRUCT_OPS(pandemonium_running, struct task_struct *p)
{
	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;

	struct task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx) {
		struct tuning_knobs *knobs = get_knobs();
		p->scx.slice = knobs ? knobs->slice_ns : 1000000;
		return;
	}

	u64 now = bpf_ktime_get_ns();
	tctx->last_run_at = now;

	// WAKEUP-TO-RUN LATENCY
	// ONLY RECORD ONCE PER WAKEUP: CLEAR last_woke_at AFTER RECORDING.
	if (tctx->last_woke_at && now > tctx->last_woke_at) {
		u64 wake_lat = now - tctx->last_woke_at;
		u8 path = tctx->dispatch_path;
		tctx->last_woke_at = 0;

		struct pandemonium_stats *s = get_stats();
		if (s) {
			s->wake_lat_samples += 1;
			s->wake_lat_sum += wake_lat;
			if (wake_lat > s->wake_lat_max)
				s->wake_lat_max = wake_lat;

			if (path == 0) {
				s->wake_lat_idle_sum += wake_lat;
				s->wake_lat_idle_cnt += 1;
			} else if (path == 1) {
				s->wake_lat_kick_sum += wake_lat;
				s->wake_lat_kick_cnt += 1;
			}
		}

		// RING BUFFER: ONLY WHEN RUST ADAPTIVE LOOP IS CONSUMING
		if (ringbuf_active) {
			struct wake_lat_sample *sample =
				bpf_ringbuf_reserve(&wake_lat_rb,
						    sizeof(*sample), 0);
			if (sample) {
				sample->lat_ns = wake_lat;
				sample->pid = p->pid;
				sample->path = path;
				__builtin_memset(sample->_pad, 0,
						 sizeof(sample->_pad));
				bpf_ringbuf_submit(sample, 0);
			}
		}
	}

	struct tuning_knobs *knobs = get_knobs();
	p->scx.slice = task_slice(tctx, knobs);
}

// STOPPING: TASK YIELDS CPU -- CHARGE VTIME WITH TIER-BASED WEIGHT
void BPF_STRUCT_OPS(pandemonium_stopping, struct task_struct *p,
		    bool runnable)
{
	struct task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	tctx->cached_weight = effective_weight(p, tctx);
	u64 weight = tctx->cached_weight;

	u64 now = bpf_ktime_get_ns();
	u64 slice = now > tctx->last_run_at ? now - tctx->last_run_at : 0;
	tctx->avg_runtime = calc_avg(tctx->avg_runtime, slice, tctx->ewma_age);

	u64 delta_vtime;
	if (weight > 0)
		delta_vtime = (slice << 7) / weight;
	else
		delta_vtime = slice;

	p->scx.dsq_vtime += delta_vtime;
	tctx->awake_vtime += delta_vtime;
}

// TICK: PREEMPT BATCH TASKS WHEN INTERACTIVE WORK IS WAITING
// COMPLEMENTS THE BPF TIMER. FIRES ON KERNEL TICK FOR THE RUNNING TASK.
void BPF_STRUCT_OPS(pandemonium_tick, struct task_struct *p)
{
	if (!interactive_waiting)
		return;

	struct task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	if (tctx->tier == TIER_BATCH && tctx->avg_runtime >= 1000000) {
		scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
		interactive_waiting = false;
		struct pandemonium_stats *s = get_stats();
		if (s)
			__sync_fetch_and_add(&s->nr_preempt, 1);
	}
}

// ENABLE: NEW TASK ENTERS SCHED_EXT
void BPF_STRUCT_OPS(pandemonium_enable, struct task_struct *p)
{
	p->scx.dsq_vtime = vtime_now;

	struct task_ctx *tctx = ensure_task_ctx(p);
	if (tctx) {
		tctx->awake_vtime = 0;
		tctx->last_run_at = 0;
		tctx->wakeup_freq = 20;
		tctx->last_woke_at = bpf_ktime_get_ns();
		tctx->avg_runtime = 100000;
		tctx->cached_weight = WEIGHT_INTERACTIVE;
		tctx->prev_nvcsw = p->nvcsw;
		tctx->csw_rate = 0;
		tctx->lat_cri = 0;
		tctx->tier = TIER_INTERACTIVE;
		tctx->ewma_age = 0;
		tctx->dispatch_path = 0;
	}
}

// INIT: DETECT TOPOLOGY, CREATE DSQs, CALIBRATE, START TIMER
s32 BPF_STRUCT_OPS_SLEEPABLE(pandemonium_init)
{
	u32 zero = 0;

	nr_nodes = __COMPAT_scx_bpf_nr_node_ids();
	if (nr_nodes < 1)
		nr_nodes = 1;
	if (nr_nodes > nr_cpu_ids)
		nr_nodes = nr_cpu_ids;

	// CREATE PER-CPU DSQs (DSQ ID = CPU ID, 0..nr_cpu_ids-1)
	for (u32 i = 0; i < nr_cpu_ids && i < MAX_CPUS; i++)
		scx_bpf_create_dsq(i, -1);

	// CREATE PER-NODE OVERFLOW DSQs (DSQ ID = nr_cpu_ids + NODE ID)
	for (u32 i = 0; i < nr_nodes && i < MAX_NODES; i++)
		scx_bpf_create_dsq(nr_cpu_ids + i, (s32)i);

	// CORE-COUNT-SCALED PREEMPTION THRESHOLD
	preempt_thresh = 60 / (nr_cpu_ids + 2);
	if (preempt_thresh < 3)
		preempt_thresh = 3;
	if (preempt_thresh > 20)
		preempt_thresh = 20;

	// INITIALIZE DEFAULT TUNING KNOBS
	struct tuning_knobs *knobs = bpf_map_lookup_elem(&tuning_knobs_map, &zero);
	if (knobs) {
		knobs->slice_ns = 1000000;
		knobs->preempt_thresh_ns = 1000000;
		knobs->lag_scale = 4;
	}

	// START BPF TIMER FOR PREEMPTION ENFORCEMENT
	struct timer_ctx *tc = bpf_map_lookup_elem(&timer_map, &zero);
	if (tc) {
		bpf_timer_init(&tc->timer, &timer_map, CLOCK_MONOTONIC);
		bpf_timer_set_callback(&tc->timer, preempt_timerfn);
		bpf_timer_start(&tc->timer, 1000000, 0);
	}

	return 0;
}

// EXIT: RECORD EXIT INFO FOR USERSPACE
void BPF_STRUCT_OPS(pandemonium_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(pandemonium_ops,
	       .select_cpu   = (void *)pandemonium_select_cpu,
	       .enqueue      = (void *)pandemonium_enqueue,
	       .dispatch     = (void *)pandemonium_dispatch,
	       .runnable     = (void *)pandemonium_runnable,
	       .running      = (void *)pandemonium_running,
	       .stopping     = (void *)pandemonium_stopping,
	       .tick         = (void *)pandemonium_tick,
	       .enable       = (void *)pandemonium_enable,
	       .init         = (void *)pandemonium_init,
	       .exit         = (void *)pandemonium_exit,
	       .flags        = SCX_OPS_BUILTIN_IDLE_PER_NODE,
	       .name         = "pandemonium");
