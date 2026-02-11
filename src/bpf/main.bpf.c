// PANDEMONIUM -- SCHED_EXT KERNEL SCHEDULER
// BEHAVIORAL-ADAPTIVE GENERAL-PURPOSE SCHEDULING FOR LINUX
//
// ADAPTIVE THREE-TIER DISPATCH:
//   TIER 0: SELECT_CPU IDLE FAST PATH -> SCX_DSQ_LOCAL (98%+ OF TASKS)
//   TIER 1: PER-CPU DSQs -- ZERO-CONTENTION DIRECTED PLACEMENT
//   TIER 2: PER-NODE OVERFLOW DSQs -- NUMA-SCOPED WORK STEALING
//
// BEHAVIORAL CLASSIFICATION:
//   LATENCY-CRITICALITY SCORE: (WAKEUP_FREQ * CSW_RATE) / AVG_RUNTIME
//   THREE TIERS: LAT_CRITICAL, INTERACTIVE, BATCH
//   CORE-COUNT-SCALED PARAMETERS (2 TO 128+ CORES)
//   OPTIONAL COMM-NAME BOOST FOR BUILD WORKLOADS (--build-mode)

#include <scx/common.bpf.h>
#include <scx/compat.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

// CONFIGURATION (SET BY USERSPACE VIA RODATA BEFORE LOAD)
const volatile u64 slice_ns = 5000000;           // 5MS DEFAULT TIME SLICE
const volatile u64 slice_min_ns = 500000;         // 0.5MS FLOOR (INTERACTIVE)
const volatile u64 slice_max_ns = 20000000;       // 20MS CEILING (COMPILERS)
const volatile bool build_mode = false;           // OPT-IN COMPILER WEIGHT BOOST
const volatile bool lightweight_mode = false;     // SKIP FULL CLASSIFICATION (AUTO ON <=4 CORES)
const volatile u64 nr_cpu_ids = 1;               // SET BY RUST (num_possible_cpus)

// BEHAVIORAL ENGINE -- CALIBRATED AT INIT FROM HARDWARE TOPOLOGY
static u64 scaled_slice_max;                      // CORE-SCALED MAX SLICE
static u64 preempt_thresh = 10;                   // WAKEUP_FREQ GATE FOR PREEMPT KICKS

// LATENCY-CRITICALITY THRESHOLDS (INTRINSIC TO TASK, NOT HARDWARE)
#define LAT_CRI_THRESH_HIGH   32                  // ABOVE = TIER_LAT_CRITICAL
#define LAT_CRI_THRESH_LOW    8                   // ABOVE = TIER_INTERACTIVE
#define LAT_CRI_CAP           255                 // MAXIMUM SCORE

// BEHAVIORAL WEIGHT MULTIPLIERS (UNITS OF 128 -- POWER-OF-2 FOR SHIFT DIVISION)
#define WEIGHT_LAT_CRITICAL   256   // 2X
#define WEIGHT_INTERACTIVE    192   // 1.5X
#define WEIGHT_BATCH          128   // 1X

// ADAPTIVE EWMA CONSTANTS
#define EWMA_AGE_MATURE    8                      // WAKEUPS BEFORE SLOW COEFFICIENT
#define EWMA_AGE_CAP       16                     // STOP INCREMENTING

// SCHEDULING CONSTANTS
#define MAX_WAKEUP_FREQ    64                     // CAP ON WAKEUP FREQUENCY
#define MAX_CSW_RATE       512                    // CAP ON VOLUNTARY CSW RATE
#define LAG_CAP_NS         (40ULL * 1000000ULL)   // 40MS MAX VTIME BOOST
#define STICKY_THRESH_NS   (10ULL * 1000ULL)      // 10US: BELOW THIS = STICKY TASK

// TOPOLOGY -- DETECTED AT INIT
static u32 nr_nodes;

// GLOBAL VTIME WATERMARK
static u64 vtime_now;

// LIGHTWEIGHT TICK-BASED PREEMPTION SIGNAL
// SET BY enqueue() WHEN NON-BATCH TASK HITS OVERFLOW DSQ.
// CLEARED BY tick() AFTER YIELDING A BATCH TASK.
// POLITE KICKS ARE NOOPS (balance_one() KEEPS TASK IF SLICE > 0).
// tick() ZEROES THE SLICE INSTEAD, FORCING NATURAL YIELD ON NEXT TICK.
static bool interactive_waiting;

// USER EXIT INFO FOR CLEAN SHUTDOWN
UEI_DEFINE(uei);

// PER-CPU STATISTICS -- NO ATOMICS IN HOT PATHS
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct pandemonium_stats);
} stats_map SEC(".maps");

// LAT_CRI SCORE HISTOGRAM -- FOR CALIBRATION AND OBSERVABILITY
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct lat_cri_histogram);
} hist_map SEC(".maps");

static __always_inline struct pandemonium_stats *get_stats(void)
{
	u32 zero = 0;
	return bpf_map_lookup_elem(&stats_map, &zero);
}

// PER-TASK CONTEXT
struct task_ctx {
	u64 awake_vtime;      // VRUNTIME ACCUMULATED SINCE LAST SLEEP
	u64 last_run_at;      // TIMESTAMP WHEN TASK STARTED RUNNING
	u64 wakeup_freq;      // EWMA OF WAKEUP FREQUENCY (HIGHER = MORE INTERACTIVE)
	u64 last_woke_at;     // TIMESTAMP OF LAST WAKEUP
	u64 avg_runtime;      // EWMA OF RUNTIME PER SCHEDULING CYCLE
	u64 cached_weight;    // CACHED EFFECTIVE WEIGHT (UPDATED IN STOPPING)
	u64 prev_nvcsw;       // SNAPSHOT OF task_struct->nvcsw
	u64 csw_rate;         // EWMA OF VOLUNTARY CONTEXT SWITCHES PER 100MS
	u64 lat_cri;          // LATENCY-CRITICALITY SCORE (0-255)
	u32 tier;             // enum task_tier -- CACHED BEHAVIORAL CLASSIFICATION
	u32 ewma_age;         // WAKEUP CYCLES SINCE TASK ENTERED (CAPS AT EWMA_AGE_CAP)
};

// TASK STORAGE MAP -- KERNEL MANAGES LIFECYCLE, NO MANUAL CLEANUP
struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

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

// ADAPTIVE EWMA: FAST FOR NEW TASKS, SLOW FOR ESTABLISHED
// AGE < 8:  50% OLD + 50% NEW  (FAST CONVERGENCE -- 2 CYCLES TO 75% TRUE VALUE)
// AGE >= 8: 87.5% OLD + 12.5% NEW  (STABILITY -- RESISTS TRANSIENT SPIKES)
static __always_inline u64 calc_avg(u64 old_val, u64 new_val, u32 age)
{
	if (age < EWMA_AGE_MATURE)
		return (old_val >> 1) + (new_val >> 1);
	return (old_val - (old_val >> 3)) + (new_val >> 3);
}

// CONVERT WAKEUP INTERVAL TO FREQUENCY (WAKEUPS PER 100MS)
static __always_inline u64 update_freq(u64 freq, u64 interval_ns, u32 age)
{
	u64 new_freq;

	if (interval_ns == 0)
		interval_ns = 1;
	new_freq = (100ULL * 1000000ULL) / interval_ns;
	return calc_avg(freq, new_freq, age);
}

// COMPUTE LATENCY-CRITICALITY SCORE FROM BEHAVIORAL SIGNALS
// HIGH WAKEUP FREQ + HIGH CSW RATE + SHORT RUNTIME = LATENCY-CRITICAL
// COMPILER: LOW FREQ, LOW CSW, LONG RUNTIME -> SCORE ~0
// COMPOSITOR: HIGH FREQ, HIGH CSW, SHORT RUNTIME -> SCORE 100+
static __always_inline u64 compute_lat_cri(u64 wakeup_freq, u64 csw_rate,
					    u64 avg_runtime_ns)
{
	u64 avg_runtime_ms = avg_runtime_ns >> 20; // >>20 ≈ /1048576 ≈ /1000000
	u64 score;

	if (avg_runtime_ms == 0)
		avg_runtime_ms = 1;
	score = (wakeup_freq * csw_rate) / avg_runtime_ms;
	if (score > LAT_CRI_CAP)
		score = LAT_CRI_CAP;
	return score;
}

// MAP LATENCY-CRITICALITY SCORE TO BEHAVIORAL TIER
static __always_inline u32 classify_tier(u64 lat_cri)
{
	if (lat_cri >= LAT_CRI_THRESH_HIGH)
		return TIER_LAT_CRITICAL;
	if (lat_cri >= LAT_CRI_THRESH_LOW)
		return TIER_INTERACTIVE;
	return TIER_BATCH;
}

// CLASSIFY A TASK BY COMM NAME -- RETURNS WEIGHT BOOST (UNITS OF 100)
// COMPILERS: 200 (2X), LINKERS/ASSEMBLERS: 150 (1.5X), EVERYTHING ELSE: 100
// SECONDARY SIGNAL -- ONLY CONSULTED WHEN build_mode IS TRUE
static __always_inline u64 classify_weight(const struct task_struct *p)
{
	char c0 = p->comm[0];

	switch (c0) {
	case 'c':
		if (p->comm[1] == 'c' && p->comm[2] == '1' &&
		    p->comm[3] == '\0')
			return 200;
		if (p->comm[1] == 'c' && p->comm[2] == '1' &&
		    p->comm[3] == 'p' && p->comm[4] == 'l' &&
		    p->comm[5] == 'u' && p->comm[6] == 's' &&
		    p->comm[7] == '\0')
			return 200;
		if (p->comm[1] == 'l' && p->comm[2] == 'a' &&
		    p->comm[3] == 'n' && p->comm[4] == 'g' &&
		    p->comm[5] == '\0')
			return 200;
		if (p->comm[1] == '+' && p->comm[2] == '+' &&
		    p->comm[3] == '\0')
			return 200;
		break;
	case 'r':
		if (p->comm[1] == 'u' && p->comm[2] == 's' &&
		    p->comm[3] == 't' && p->comm[4] == 'c' &&
		    p->comm[5] == '\0')
			return 200;
		break;
	case 'g':
		if (p->comm[1] == 'c' && p->comm[2] == 'c' &&
		    p->comm[3] == '\0')
			return 200;
		if (p->comm[1] == '+' && p->comm[2] == '+' &&
		    p->comm[3] == '\0')
			return 200;
		if (p->comm[1] == 'o' && p->comm[2] == '\0')
			return 200;
		break;
	case 'l':
		if (p->comm[1] == 'd' && p->comm[2] == '\0')
			return 150;
		if (p->comm[1] == 'l' && p->comm[2] == 'd' &&
		    p->comm[3] == '\0')
			return 150;
		if (p->comm[1] == 'd' && p->comm[2] == '.' &&
		    p->comm[3] == 'l' && p->comm[4] == 'l' &&
		    p->comm[5] == 'd' && p->comm[6] == '\0')
			return 150;
		break;
	case 'm':
		if (p->comm[1] == 'o' && p->comm[2] == 'l' &&
		    p->comm[3] == 'd' && p->comm[4] == '\0')
			return 150;
		break;
	case 'a':
		if (p->comm[1] == 's' && p->comm[2] == '\0')
			return 150;
		if (p->comm[1] == 'r' && p->comm[2] == '\0')
			return 150;
		break;
	case 'j':
		if (p->comm[1] == 'a' && p->comm[2] == 'v' &&
		    p->comm[3] == 'a' && p->comm[4] == 'c' &&
		    p->comm[5] == '\0')
			return 200;
		break;
	}

	return 100;
}

// DETECT COMPOSITOR PROCESSES BY COMM NAME
// COMPOSITORS ALWAYS GET LAT_CRITICAL -- THEY MUST PAINT FRAMES ON TIME
// PIPEWIRE/WIREPLUMBER RUN AT RT PRIORITY (RTKIT) -- THEY BYPASS SCHED_EXT
static __always_inline bool is_compositor(const struct task_struct *p)
{
	char c0 = p->comm[0];

	switch (c0) {
	case 'k':
		// kwin_wayland, kwin_x11
		if (p->comm[1] == 'w' && p->comm[2] == 'i' &&
		    p->comm[3] == 'n')
			return true;
		break;
	case 'g':
		// gnome-shell
		if (p->comm[1] == 'n' && p->comm[2] == 'o' &&
		    p->comm[3] == 'm' && p->comm[4] == 'e' &&
		    p->comm[5] == '-' && p->comm[6] == 's')
			return true;
		break;
	case 's':
		// sway
		if (p->comm[1] == 'w' && p->comm[2] == 'a' &&
		    p->comm[3] == 'y' && p->comm[4] == '\0')
			return true;
		break;
	case 'H':
		// Hyprland
		if (p->comm[1] == 'y' && p->comm[2] == 'p' &&
		    p->comm[3] == 'r' && p->comm[4] == 'l' &&
		    p->comm[5] == 'a' && p->comm[6] == 'n' &&
		    p->comm[7] == 'd' && p->comm[8] == '\0')
			return true;
		break;
	case 'p':
		// picom
		if (p->comm[1] == 'i' && p->comm[2] == 'c' &&
		    p->comm[3] == 'o' && p->comm[4] == 'm' &&
		    p->comm[5] == '\0')
			return true;
		break;
	case 'w':
		// weston
		if (p->comm[1] == 'e' && p->comm[2] == 's' &&
		    p->comm[3] == 't' && p->comm[4] == 'o' &&
		    p->comm[5] == 'n' && p->comm[6] == '\0')
			return true;
		break;
	}

	return false;
}

// EFFECTIVE WEIGHT: BEHAVIORAL TIER IS PRIMARY SIGNAL
// COMM-NAME BOOST IS SECONDARY (build_mode ONLY, HALF-STRENGTH ADDITIVE)
static __always_inline u64 effective_weight(const struct task_struct *p,
					     const struct task_ctx *tctx)
{
	u64 weight = p->scx.weight;
	u64 behavioral;
	u64 boost;
	struct pandemonium_stats *s;

	if (tctx->tier == TIER_LAT_CRITICAL)
		behavioral = WEIGHT_LAT_CRITICAL;
	else if (tctx->tier == TIER_INTERACTIVE)
		behavioral = WEIGHT_INTERACTIVE;
	else
		behavioral = WEIGHT_BATCH;

	weight = weight * behavioral >> 7;

	if (build_mode) {
		boost = classify_weight(p);
		if (boost > 100) {
			weight += weight * (boost - 100) >> 8;
			s = get_stats();
			if (s)
				s->nr_boosted += 1;
		}
	}

	return weight;
}

// COMPUTE TASK DEADLINE FOR DSQ ORDERING
// DEADLINE = VTIME + AWAKE_VTIME (LOWER = HIGHER PRIORITY)
// GREEDY DECAY: TIER-DEPENDENT AWAKE CAP PREVENTS BOOST EXPLOITATION
//   LAT_CRITICAL: 20MS CAP (MUST SLEEP FREQUENTLY TO KEEP TIER)
//   INTERACTIVE:  30MS CAP
//   BATCH:        40MS CAP
static __always_inline u64 task_deadline(struct task_struct *p,
					  struct task_ctx *tctx)
{
	u64 lag_scale = tctx->wakeup_freq;
	u64 vtime_floor;
	u64 awake_cap;

	if (lag_scale < 1)
		lag_scale = 1;
	if (lag_scale > MAX_WAKEUP_FREQ)
		lag_scale = MAX_WAKEUP_FREQ;

	vtime_floor = vtime_now - LAG_CAP_NS * lag_scale;
	if (time_before(p->scx.dsq_vtime, vtime_floor))
		p->scx.dsq_vtime = vtime_floor;

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

// DYNAMIC TIME SLICE -- TIER-BASED
// LAT_CRITICAL: 1.5X AVG_RUNTIME (TIGHT, FAST PREEMPTION OPPORTUNITY)
// INTERACTIVE:  2X AVG_RUNTIME (RESPONSIVE)
// BATCH:        CORE-SCALED CEILING * WEIGHT / 100 (THROUGHPUT-ORIENTED)
static __always_inline u64 task_slice(struct task_ctx *tctx)
{
	u64 base;
	u64 ceiling;

	if (tctx->tier == TIER_LAT_CRITICAL) {
		base = tctx->avg_runtime + (tctx->avg_runtime >> 1);
		if (base > slice_ns)
			base = slice_ns;
		if (base < slice_min_ns)
			base = slice_min_ns;
	} else if (tctx->tier == TIER_INTERACTIVE) {
		base = tctx->avg_runtime * 2;
		if (base > slice_ns)
			base = slice_ns;
		if (base < slice_min_ns)
			base = slice_min_ns;
	} else {
		ceiling = scaled_slice_max > 0 ? scaled_slice_max : slice_max_ns;
		base = ceiling * tctx->cached_weight >> 7;
		if (base > ceiling)
			base = ceiling;
		if (base < slice_min_ns)
			base = slice_min_ns;
	}

	return base;
}

// SELECT CPU: FAST-PATH IDLE CPU DISPATCH
s32 BPF_STRUCT_OPS(pandemonium_select_cpu, struct task_struct *p,
		   s32 prev_cpu, u64 wake_flags)
{
	struct pandemonium_stats *s;
	bool is_idle = false;
	s32 cpu;
	struct task_ctx *tctx;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	if (is_idle) {
		s = get_stats();
		if (s)
			s->nr_idle_hits += 1;

		tctx = lookup_task_ctx(p);
		if (tctx) {
			if (tctx->avg_runtime < STICKY_THRESH_NS && s)
				s->nr_sticky += 1;
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, task_slice(tctx), 0);
		} else {
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, slice_ns, 0);
		}
	}

	return cpu;
}

// ENQUEUE: NO IDLE CPU FOUND IN SELECT_CPU
// ADAPTIVE TWO-TIER PLACEMENT WITH BEHAVIORAL PREEMPTION:
//   TIER 1: NODE-LOCAL IDLE CPU -> DIRECT PLACEMENT ON PER-CPU DSQ
//   TIER 2: NODE-SCOPED OVERFLOW DSQ + TIER-BASED KICK
void BPF_STRUCT_OPS(pandemonium_enqueue, struct task_struct *p,
		    u64 enq_flags)
{
	struct pandemonium_stats *s;
	struct task_ctx *tctx;
	u64 dl, dyn_slice, overflow_id;
	s32 cpu, node;

	tctx = lookup_task_ctx(p);
	if (tctx) {
		dl = task_deadline(p, tctx);
		dyn_slice = task_slice(tctx);
	} else {
		dl = p->scx.dsq_vtime;
		if (time_before(dl, vtime_now - slice_ns))
			dl = vtime_now - slice_ns;
		dyn_slice = slice_ns;
	}

	node = __COMPAT_scx_bpf_cpu_node(scx_bpf_task_cpu(p));

	// TIER 1: NODE-LOCAL IDLE CPU -> DIRECT PLACEMENT ON PER-CPU DSQ
	cpu = __COMPAT_scx_bpf_pick_idle_cpu_node(p->cpus_ptr, node, 0);
	if (cpu >= 0) {
		scx_bpf_dsq_insert_vtime(p, (u64)cpu, dyn_slice, dl, enq_flags);
		scx_bpf_kick_cpu(cpu, SCX_KICK_IDLE);
		s = get_stats();
		if (s) {
			s->nr_direct += 1;
			s->nr_kicks += 1;
		}
		return;
	}

	// TIER 2: NODE-SCOPED OVERFLOW DSQ
	overflow_id = nr_cpu_ids + (u64)node;
	scx_bpf_dsq_insert_vtime(p, overflow_id, dyn_slice, dl, enq_flags);
	s = get_stats();
	if (s)
		s->nr_overflow += 1;

	// BEHAVIORAL PREEMPTION:
	// LIGHTWEIGHT: SHORT-RUNTIME TASKS GET SAME-CPU PREEMPTION.
	//   DISPATCH TO CURRENT CPU'S PER-CPU DSQ + SELF-KICK.
	//   SELF-KICK: NO IPI (JUST SETS NEED_RESCHED ON CURRENT CPU).
	//   AFTER WAKEUP PATH RETURNS, CPU RESCHEDULES AND PICKS PROBE
	//   FROM ITS OWN PER-CPU DSQ (CHECKED FIRST IN dispatch()).
	// FULL MODE: TIER-BASED KICK STRENGTH.
	if (lightweight_mode) {
		if (tctx && tctx->avg_runtime < 1000000) {
			s32 this_cpu = bpf_get_smp_processor_id();
			scx_bpf_dsq_insert_vtime(p, (u64)this_cpu,
						  dyn_slice, dl, enq_flags);
			scx_bpf_kick_cpu(this_cpu, SCX_KICK_PREEMPT);
			if (s) {
				s->nr_direct += 1;
				s->nr_preempt += 1;
				s->nr_kicks += 1;
			}
			return;
		}
		return;
	}

	cpu = __COMPAT_scx_bpf_pick_any_cpu_node(p->cpus_ptr, node, 0);
	if (cpu >= 0) {
		if (tctx &&
		    (tctx->tier == TIER_LAT_CRITICAL ||
		     (tctx->tier == TIER_INTERACTIVE &&
		      tctx->wakeup_freq > preempt_thresh))) {
			scx_bpf_kick_cpu(cpu, SCX_KICK_PREEMPT);
			if (s)
				s->nr_preempt += 1;
		} else {
			scx_bpf_kick_cpu(cpu, 0);
		}
		if (s)
			s->nr_kicks += 1;
	}
}

// DISPATCH: CPU IS IDLE AND NEEDS WORK
// THREE-TIER CONSUMPTION:
//   1. OWN PER-CPU DSQ (ZERO CONTENTION)
//   2. OWN NODE'S OVERFLOW (NUMA-LOCAL WORK STEALING)
//   3. CROSS-NODE STEAL (LAST RESORT -- BETTER THAN IDLE)
void BPF_STRUCT_OPS(pandemonium_dispatch, s32 cpu, struct task_struct *prev)
{
	s32 node = __COMPAT_scx_bpf_cpu_node(cpu);
	u32 n;

	if (scx_bpf_dsq_move_to_local((u64)cpu))
		return;

	if (scx_bpf_dsq_move_to_local(nr_cpu_ids + (u64)node))
		return;

	for (n = 0; n < nr_nodes && n < MAX_NODES; n++) {
		if (n != (u32)node &&
		    scx_bpf_dsq_move_to_local(nr_cpu_ids + (u64)n))
			return;
	}
}

// RUNNABLE: TASK WAKES UP -- BEHAVIORAL CLASSIFICATION ENGINE
// 1. UPDATE WAKEUP FREQUENCY (EWMA)
// 2. COMPUTE VOLUNTARY CSW RATE FROM task_struct->nvcsw DELTA
// 3. COMPUTE LATENCY-CRITICALITY SCORE
// 4. CLASSIFY INTO BEHAVIORAL TIER
// 5. INCREMENT TIER-BASED STATS
void BPF_STRUCT_OPS(pandemonium_runnable, struct task_struct *p,
		    u64 enq_flags)
{
	struct pandemonium_stats *s;
	struct task_ctx *tctx;
	u64 now, delta_t;
	u64 nvcsw, csw_delta, csw_freq;
	u32 new_tier;

	tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	now = bpf_ktime_get_ns();
	tctx->awake_vtime = 0;

	// FAST PATH: BRAND-NEW TASKS (< 2 WAKEUPS)
	// NOT ENOUGH DATA FOR MEANINGFUL CLASSIFICATION.
	// SKIP 4 DIVISIONS PER WAKEUP FOR SHORT-LIVED PROCESSES.
	if (tctx->ewma_age < 2) {
		tctx->last_woke_at = now;
		tctx->prev_nvcsw = p->nvcsw;
		tctx->ewma_age += 1;
		s = get_stats();
		if (s)
			s->nr_interactive += 1;
		return;
	}

	// LIGHTWEIGHT MODE: SKIP FULL CLASSIFICATION ENGINE
	// SIMPLE HEURISTIC: VOLUNTARY CSW SINCE LAST WAKEUP = INTERACTIVE
	// ELIMINATES ALL DIVISIONS + EWMA UPDATES. STILL COUNTS TIER STATS.
	if (lightweight_mode) {
		nvcsw = p->nvcsw;
		new_tier = (nvcsw > tctx->prev_nvcsw) ? TIER_INTERACTIVE : TIER_BATCH;
		tctx->prev_nvcsw = nvcsw;
		tctx->last_woke_at = now;
		s = get_stats();
		if (s) {
			if (new_tier != tctx->tier)
				s->nr_tier_changes += 1;
			if (new_tier == TIER_INTERACTIVE)
				s->nr_interactive += 1;
			else
				s->nr_batch += 1;
		}
		tctx->tier = new_tier;
		return;
	}

	// WAKEUP FREQUENCY
	delta_t = now > tctx->last_woke_at ? now - tctx->last_woke_at : 1;
	tctx->wakeup_freq = update_freq(tctx->wakeup_freq, delta_t, tctx->ewma_age);
	if (tctx->wakeup_freq > MAX_WAKEUP_FREQ)
		tctx->wakeup_freq = MAX_WAKEUP_FREQ;
	tctx->last_woke_at = now;

	// AGE TRACKING (DRIVES EWMA COEFFICIENT SELECTION)
	if (tctx->ewma_age < EWMA_AGE_CAP)
		tctx->ewma_age += 1;

	// VOLUNTARY CONTEXT SWITCH RATE
	nvcsw = p->nvcsw;
	csw_delta = nvcsw > tctx->prev_nvcsw ? nvcsw - tctx->prev_nvcsw : 0;
	tctx->prev_nvcsw = nvcsw;

	if (csw_delta > 0 && delta_t > 0) {
		csw_freq = csw_delta * (100ULL * 1000000ULL) / delta_t;
		tctx->csw_rate = calc_avg(tctx->csw_rate, csw_freq, tctx->ewma_age);
	} else {
		tctx->csw_rate = calc_avg(tctx->csw_rate, 0, tctx->ewma_age);
	}
	if (tctx->csw_rate > MAX_CSW_RATE)
		tctx->csw_rate = MAX_CSW_RATE;

	// BEHAVIORAL CLASSIFICATION
	tctx->lat_cri = compute_lat_cri(tctx->wakeup_freq, tctx->csw_rate,
					 tctx->avg_runtime);
	new_tier = classify_tier(tctx->lat_cri);

	// HISTOGRAM: RECORD LAT_CRI DISTRIBUTION (FOR CALIBRATION)
	{
		u32 hkey = 0;
		u32 bucket = tctx->lat_cri >> 3;
		struct lat_cri_histogram *hist;

		if (bucket > 31)
			bucket = 31;
		hist = bpf_map_lookup_elem(&hist_map, &hkey);
		if (hist)
			hist->buckets[bucket] += 1;
	}

	// COMPOSITOR BOOST: ALWAYS LAT_CRITICAL REGARDLESS OF SCORE
	if (new_tier != TIER_LAT_CRITICAL && is_compositor(p))
		new_tier = TIER_LAT_CRITICAL;

	// TIER STATS + THRESHOLD VALIDATION
	s = get_stats();
	if (s) {
		s->lat_cri_sum += tctx->lat_cri;
		if (new_tier != tctx->tier)
			s->nr_tier_changes += 1;
		if (is_compositor(p))
			s->nr_compositor += 1;
		if (new_tier == TIER_LAT_CRITICAL)
			s->nr_lat_critical += 1;
		else if (new_tier == TIER_INTERACTIVE)
			s->nr_interactive += 1;
		else
			s->nr_batch += 1;
	}
	tctx->tier = new_tier;
}

// RUNNING: TASK STARTS EXECUTING
void BPF_STRUCT_OPS(pandemonium_running, struct task_struct *p)
{
	struct task_ctx *tctx;

	if (time_before(vtime_now, p->scx.dsq_vtime))
		vtime_now = p->scx.dsq_vtime;

	tctx = lookup_task_ctx(p);
	if (tctx) {
		tctx->last_run_at = bpf_ktime_get_ns();
		p->scx.slice = task_slice(tctx);
	}
}

// STOPPING: TASK YIELDS CPU -- UPDATE BEHAVIORAL WEIGHT AND VTIME
void BPF_STRUCT_OPS(pandemonium_stopping, struct task_struct *p,
		    bool runnable)
{
	struct pandemonium_stats *s;
	struct task_ctx *tctx;
	u64 weight, now, slice, delta_vtime;

	tctx = lookup_task_ctx(p);
	if (!tctx)
		goto out;

	tctx->cached_weight = effective_weight(p, tctx);
	weight = tctx->cached_weight;

	now = bpf_ktime_get_ns();
	slice = now > tctx->last_run_at ? now - tctx->last_run_at : 0;
	tctx->avg_runtime = calc_avg(tctx->avg_runtime, slice, tctx->ewma_age);

	if (weight > 0)
		delta_vtime = (slice << 7) / weight;
	else
		delta_vtime = slice;

	p->scx.dsq_vtime += delta_vtime;
	tctx->awake_vtime += delta_vtime;

out:
	s = get_stats();
	if (s)
		s->nr_dispatches += 1;
}

// TICK: SAFETY NET FOR LIGHTWEIGHT MODE.
// IF enqueue() PREEMPT-KICK MISSED (RACE, NO CPU FOUND), tick() CATCHES IT.
// ALSO HANDLES EDGE CASES WHERE interactive_waiting LINGERS.
void BPF_STRUCT_OPS(pandemonium_tick, struct task_struct *p)
{
	struct task_ctx *tctx;

	if (!lightweight_mode || !interactive_waiting)
		return;

	tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	// ONLY PREEMPT LONG-RUNNING TASKS (COMPILERS). SHORT TASKS YIELD SOON.
	if (tctx->avg_runtime >= 1000000) {
		scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
		interactive_waiting = false;
	}
}

// ENABLE: NEW TASK ENTERS SCHED_EXT
// DEFAULT TIER_INTERACTIVE -- GENEROUS INITIAL CLASSIFICATION
// DECAYS TO ACTUAL BEHAVIOR ON FIRST runnable() CYCLE
void BPF_STRUCT_OPS(pandemonium_enable, struct task_struct *p)
{
	struct task_ctx *tctx;

	p->scx.dsq_vtime = vtime_now;

	tctx = ensure_task_ctx(p);
	if (tctx) {
		tctx->awake_vtime = 0;
		tctx->last_run_at = 0;
		tctx->wakeup_freq = 20;
		tctx->last_woke_at = bpf_ktime_get_ns();
		tctx->avg_runtime = 100000;
		tctx->cached_weight = WEIGHT_BATCH;
		tctx->prev_nvcsw = p->nvcsw;
		tctx->csw_rate = 0;
		tctx->lat_cri = 0;
		tctx->tier = TIER_INTERACTIVE;
		tctx->ewma_age = 0;
	}
}

// INIT: DETECT TOPOLOGY, CREATE DSQs, CALIBRATE BEHAVIORAL ENGINE
// DSQ IDs: 0..nr_cpu_ids-1 = PER-CPU, nr_cpu_ids+node_id = PER-NODE OVERFLOW
s32 BPF_STRUCT_OPS_SLEEPABLE(pandemonium_init)
{
	u64 sm;
	u32 i;

	// DETECT NUMA TOPOLOGY
	nr_nodes = __COMPAT_scx_bpf_nr_node_ids();
	if (nr_nodes < 1)
		nr_nodes = 1;

	// CREATE PER-CPU DSQs
	for (i = 0; i < nr_cpu_ids && i < MAX_CPUS; i++)
		scx_bpf_create_dsq(i, -1);

	// CREATE PER-NODE OVERFLOW DSQs
	for (i = 0; i < nr_nodes && i < MAX_NODES; i++)
		scx_bpf_create_dsq(nr_cpu_ids + i, (s32)i);

	// CORE-COUNT SCALING: BATCH SLICES GROW WITH CORE COUNT
	// 2 CORES: ~5MS CEILING (RESPONSIVE)
	// 8 CORES: ~20MS CEILING (BASELINE)
	// 32 CORES: ~80MS CEILING (THROUGHPUT)
	sm = slice_max_ns * nr_cpu_ids >> 3;
	if (sm < slice_ns)
		sm = slice_ns;
	if (sm > slice_max_ns * 4)
		sm = slice_max_ns * 4;
	scaled_slice_max = sm;

	// CONTINUOUS PREEMPT THRESHOLD: 60 / (nr_cpu_ids + 2), CLAMPED [3, 20]
	// 2 CORES: 15  |  4 CORES: 10  |  8 CORES: 6  |  16 CORES: 3  |  32+: 3
	preempt_thresh = 60 / (nr_cpu_ids + 2);
	if (preempt_thresh < 3)
		preempt_thresh = 3;
	if (preempt_thresh > 20)
		preempt_thresh = 20;

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
