// PANDEMONIUM -- SCHED_EXT KERNEL SCHEDULER
// ADAPTIVE DESKTOP SCHEDULING FOR LINUX
//
// BPF: BEHAVIORAL CLASSIFICATION + MULTI-TIER DISPATCH
// RUST: ADAPTIVE CONTROL LOOP + REAL-TIME TELEMETRY
//
// ARCHITECTURE:
//   SELECT_CPU IDLE FAST PATH -> PER-CPU DSQ (DEPTH-GATED, VISIBLE, STEALABLE)
//   ENQUEUE IDLE FOUND -> NODE DSQ (SHARED, ANY CPU DRAINS)
//   ENQUEUE INTERACTIVE PREEMPT -> NODE DSQ (SHARED, KICKED CPU DRAINS)
//   ENQUEUE FALLBACK -> PER-NODE OVERFLOW DSQ (VTIME-ORDERED)
//   DISPATCH -> OWN PER-CPU, L2 WORK STEAL, NODE OVERFLOW, CROSS-NODE, KEEP
//   TICK -> PER-CPU SOJOURN (LOCAL + ROTATING SCAN) + BATCH PREEMPTION
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

// CONFIGURATION (SET BY RUST VIA RODATA BEFORE LOAD)

const volatile u64 nr_cpu_ids = 1;

// BEHAVIORAL CONSTANTS

#define TRACE_SCHED 0

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
// starvation_rescue_ns AND overflow_sojourn_rescue_ns ARE DERIVED FROM
// knobs->topology_tau_ns VIA scale_tau() AT THE FIRST CPU-0 TICK. SEE
// apply_tau_scaling() AND pandemonium_init().

// FIEDLER-SCALED TIMING CONSTANTS (Q16 FIXED-POINT DIMENSIONLESS RATIOS).
// EACH k_i ENCODES (target_ns / tau_ns) AT THE 12C REFERENCE TOPOLOGY WHERE
// tau = 40MS. scale_tau(tau, k_i) REPRODUCES THE TARGET VALUE.
#define K_Q16_SHIFT             16
#define K_SOJOURN_INTERVAL       19661u   // 0.30   (12MS @ tau=40MS)
#define K_OVERFLOW_RESCUE        16384u   // 0.25   (10MS @ tau=40MS)
#define K_CODEL_FLOOR             1147u   // 0.0175 (700US @ tau=40MS)
#define K_STARVATION_RESCUE     273285u   // 4.17   (166MS @ tau=40MS)
#define K_LONGRUN              3276800u   // 50.0   (2000MS @ tau=40MS)
#define K_CODEL_MAX               3277u   // 0.05   (2MS @ tau=40MS)

// OSCILLATOR DYNAMICS DERIVED FROM tau SO THE CONTROLLER RUNS ON THE SAME
// TIME CONSTANT AS THE CoDel TARGET RANGE IT MODULATES. pull_scale AND
// damping_shift ARE SMALL INTEGERS (1-4 AND 1-5 RESPECTIVELY) SO THEY USE
// DIRECT-DIVIDE RATHER THAN Q16 (Q16 LOSES PRECISION FOR SMALL-INTEGER
// OUTPUTS). velocity_cap COUPLES TO pull_scale: vcap = 50000 * pull.
#define K_OSC_PULL_THRESH_NS    10000000u  // 10MS PER pull-scale STEP
#define K_OSC_DAMP_THRESH_NS     8000000u  //  8MS PER damping-shift STEP
#define OSC_VELOCITY_CAP_PER_PULL  50000u  // vcap = OSC_VELOCITY_CAP_PER_PULL * pull


// GLOBALS

static u32 nr_nodes;
static u64 vtime_now;

// TICK-BASED INTERACTIVE PREEMPTION SIGNAL
// SET BY enqueue() WHEN NON-BATCH TASK HITS OVERFLOW DSQ.
// CLEARED BY tick() AFTER PREEMPTING A BATCH TASK.
// latcrit_waiting IS A SHARPER VARIANT: SET WHEN A TIER_LAT_CRITICAL TASK
// SPECIFICALLY IS WAITING. tick() USES IT TO TIGHTEN THE PREEMPT THRESHOLD
// SO AUDIO / COMPOSITOR / OTHER TIGHT-DEADLINE WAKERS DON'T SIT BEHIND A
// FULL BATCH SLICE WORTH OF PREEMPT-WAIT. TIER INFO ALREADY AVAILABLE AT
// THE enqueue() SITE -- WE'RE JUST PROPAGATING IT INTO THE SAFETY NET.
static bool interactive_waiting;
static bool latcrit_waiting;

// SOJOURN TRACKERS: RECORD WHEN OVERFLOW DSQs TRANSITION FROM EMPTY.
// DISPATCH STEP 0 CHECKS THESE TO RESCUE OVERFLOW TASKS AGING PAST
// overflow_sojourn_rescue_ns. WITHOUT THIS, PER-CPU DSQ DOMINANCE
// UNDER SUSTAINED LOAD MAKES ALL DOWNSTREAM ANTI-STARVATION LOGIC
// (DEFICIT, SOJOURN, STARVATION_RESCUE) UNREACHABLE.
static u64 batch_enqueue_ns;
static u64 interactive_enqueue_ns;

// PER-CPU DSQ SOJOURN: TRACKS WHEN EACH PER-CPU DSQ TRANSITIONS
// FROM EMPTY. DISPATCH AND TICK CHECK THESE TO DETECT STALE TASKS.
// WORK STEALING + DEPTH GATE HANDLE MOST CASES; THIS IS THE SAFETY NET.
static u64 pcpu_enqueue_ns[MAX_CPUS];

// DEFICIT COUNTER: ANTI-STARVATION INTERLEAVE (DRR)
// COUNTS DISPATCHES SINCE LAST BATCH SERVICE. WHEN interactive_run
// EXCEEDS interactive_budget AND BATCH IS STARVING, FORCE ONE BATCH
// DISPATCH. PROPORTIONAL: BUDGET = nr_cpu_ids * ratio (RATIO SCALES 2-4).
static u64 interactive_run;
static u64 interactive_budget;
static u64 starvation_rescue_ns;
static u64 overflow_sojourn_rescue_ns;
static u32 pcpu_depth_base;

// CODEL STALL DETECTION WITH OSCILLATOR-ADAPTED TARGET
// BINARY FLOWING/STALLED DECISION (CoDel): IF MIN SOJOURN STAYS ABOVE THE
// TARGET FOR AN INTERVAL, THE DSQ IS DECLARED STALLED AND RESCUE FIRES.
// THE TARGET ITSELF IS ADAPTED BY A DAMPED HARMONIC OSCILLATOR:
//   RESCUE EVENTS APPLY A NEGATIVE IMPULSE (TIGHTEN: DETECT STALLS SOONER)
//   QUIET TICKS APPLY A POSITIVE IMPULSE (RELAX: TOLERATE MORE SOJOURN)
//   VELOCITY DECAYS VIA BIT-SHIFT DAMPING FOR STABILITY
// ALL OSCILLATOR PARAMETERS (MARGIN, DAMPING, PULL SCALE, VELOCITY CAP,
// TARGET FLOOR) ARE CORE-SCALED AT init() AND HELD CONSTANT AT RUNTIME.
// REFERENCE: VAN JACOBSON CoDel (RFC 8289) + DAMPED HARMONIC OSCILLATOR.
#define OSCILLATOR_PULL_NS  8000     // BASE TIGHTEN IMPULSE
#define OSCILLATOR_RELAX_NS 1000     // RELAX IMPULSE PER QUIET TICK
// CORE-SCALED CONSTANTS (SET ONCE IN init())
static u32 oscillator_damping_shift;      // VELOCITY DECAY SHIFT
static u32 oscillator_pull_scale;         // RESCUE IMPULSE MULTIPLIER
static s64 oscillator_velocity_cap;       // VELOCITY CLAMP
static u64 codel_target_floor_ns;         // CORE-SCALED FLOOR FOR TARGET
// ADAPTIVE STATE
static u64 sojourn_interval_ns;        // CORE-SCALED, UNCERTAIN ZONE TIMER
static u64 codel_target_ns;          // ADAPTIVE CENTER
static s64 oscillator_velocity_ns;        // DAMPED OSCILLATION VELOCITY
static u64 prev_rescue_snapshot;       // LAST-SEEN RESCUE COUNT
static u64 global_rescue_count;        // ATOMIC CROSS-CPU RESCUE ACCUMULATOR
static u64 pcpu_min_sojourn_ns[MAX_CPUS];
static u64 pcpu_stall_start_ns[MAX_CPUS];

// LONGRUN DETECTION
// TRACKS SUSTAINED BATCH DSQ PRESSURE. WHEN BATCH DSQ IS NON-EMPTY
// FOR > longrun_thresh_ns, TIGHTEN DEFICIT RATIO TO INCREASE BATCH SHARE.
// CLEARS WHEN BATCH DSQ EMPTIES.
// longrun_thresh_ns AND codel_target_max_ns ARE PROMOTED FROM #define TO
// RUNTIME STATICS SO THEY CAN BE REDERIVED FROM knobs->topology_tau_ns
// WHEN TAU-SCALING IS ACTIVE. LEGACY DEFAULTS ARE 2s AND 2ms.
static u64 longrun_thresh_ns = 2000000000ULL;     // 2s -- legacy default
static u64 codel_target_max_ns = 2000000ULL;      // 2ms -- legacy default
static bool longrun_mode;

// TAU-SCALING: SNAPSHOT OF LAST knobs->topology_tau_ns APPLIED.
// TICK() ON CPU 0 COMPARES AGAINST CURRENT KNOB VALUE; IF CHANGED, ALL
// TAU-DERIVED STATICS ARE REDERIVED. ZERO = LEGACY FORMULAS IN EFFECT.
static u64 last_tau_snapshot;

// USER EXIT

UEI_DEFINE(uei);

// MAPS

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

// CACHE DOMAIN MAP: l2_domain[cpu] = group_id
// POPULATED BY RUST AT STARTUP FROM SYSFS TOPOLOGY
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, MAX_CPUS);
	__type(key, u32);
	__type(value, u32);
} cache_domain SEC(".maps");

// PROCESS CLASSIFICATION DATABASE: BPF OBSERVES, RUST LEARNS, BPF APPLIES
// OBSERVE: BPF WRITES MATURE TASK CLASSIFICATION, RUST DRAINS EVERY SECOND
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 512);
	__type(key, char[16]);
	__type(value, struct task_class_entry);
} task_class_observe SEC(".maps");

// INIT: RUST WRITES PREDICTIONS, BPF READS IN enable() FOR NEW TASKS
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 512);
	__type(key, char[16]);
	__type(value, struct task_class_entry);
} task_class_init SEC(".maps");

// COMPOSITOR MAP: RUST POPULATES AT STARTUP, BPF LOOKS UP IN runnable()
// KEY: COMM NAME (16 BYTES), VALUE: UNUSED (EXISTENCE = COMPOSITOR)
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 32);
	__type(key, char[16]);
	__type(value, u8);
} compositor_map SEC(".maps");

// L2 SIBLINGS MAP: FLAT ARRAY FOR L2-AWARE CPU PLACEMENT
// l2_siblings[group_id * MAX_L2_SIBLINGS + slot] = cpu_id
// SENTINEL: (u32)-1 MARKS END OF GROUP
// POPULATED BY RUST AT STARTUP FROM CpuTopology
#define MAX_L2_SIBLINGS 8

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 512);
	__type(key, u32);
	__type(value, u32);
} l2_siblings SEC(".maps");

// RESISTANCE AFFINITY MAP: PER-CPU RANKED PLACEMENT TARGETS
// affinity_rank[cpu * MAX_AFFINITY_CANDIDATES + slot] = target_cpu
// SORTED BY ASCENDING EFFECTIVE RESISTANCE (LAPLACIAN PSEUDOINVERSE).
// SLOT 0 = CHEAPEST MIGRATION TARGET (TYPICALLY L2 SIBLING).
// POPULATED BY RUST AT STARTUP FROM EXACT R_EFF COMPUTATION.
// SENTINEL: (u32)-1 MARKS END OF VALID ENTRIES.
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, MAX_CPUS * MAX_AFFINITY_CANDIDATES);
	__type(key, u32);
	__type(value, u32);
} affinity_rank SEC(".maps");

// WAKEUP LATENCY HISTOGRAM: 3 TIERS x 12 BUCKETS = 36 ENTRIES PER CPU
// BPF INCREMENTS IN running(); RUST READS ONCE PER SECOND IN MONITOR LOOP
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 36);
	__type(key, u32);
	__type(value, u64);
} wake_lat_hist SEC(".maps");

// SLEEP DURATION HISTOGRAM: 4 BUCKETS PER CPU
// BPF INCREMENTS IN running(); RUST READS ONCE PER SECOND
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, 4);
	__type(key, u32);
	__type(value, u64);
} sleep_hist SEC(".maps");

// PER-TASK CONTEXT

struct task_ctx {
	u64 awake_vtime;
	u64 last_run_at;
	u64 wakeup_freq;
	u64 last_woke_at;
	u64 avg_runtime;
	u64 runtime_dev;     // EWMA OF |RUNTIME - AVG_RUNTIME| (VARIANCE SIGNAL)
	u64 cached_weight;
	u64 prev_nvcsw;
	u64 csw_rate;
	u64 lat_cri;
	u64 sleep_start_ns;  // SET IN quiescent(), USED IN running()
	u32 tier;
	u32 ewma_age;
	s32 last_cpu;        // LAST CPU THIS TASK RAN ON (FOR CACHE AFFINITY)
	u8  dispatch_path;   // 0=IDLE, 1=HARD_KICK, 2=SOFT_KICK
	u8  _pad[3];
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

// HELPERS

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

// L2 CACHE AFFINITY INSTRUMENTATION
// COMPARE SELECTED CPU'S L2 DOMAIN WITH TASK'S LAST_CPU DOMAIN.
// INCREMENT PER-TIER HIT/MISS COUNTERS. CALLED FROM select_cpu() AND enqueue().

static __always_inline void count_l2_affinity(struct pandemonium_stats *s,
					       const struct task_ctx *tctx,
					       s32 cpu)
{
	u32 lcpu = (u32)tctx->last_cpu;
	u32 ncpu = (u32)cpu;
	u32 *ld = bpf_map_lookup_elem(&cache_domain, &lcpu);
	u32 *nd = bpf_map_lookup_elem(&cache_domain, &ncpu);
	bool hit = ld && nd && *ld == *nd;

	if (tctx->tier == TIER_BATCH) {
		if (hit) s->nr_l2_hit_batch += 1;
		else     s->nr_l2_miss_batch += 1;
	} else if (tctx->tier == TIER_INTERACTIVE) {
		if (hit) s->nr_l2_hit_interactive += 1;
		else     s->nr_l2_miss_interactive += 1;
	} else {
		if (hit) s->nr_l2_hit_lat_crit += 1;
		else     s->nr_l2_miss_lat_crit += 1;
	}
}

// L2 CACHE PLACEMENT: FIND IDLE SIBLING IN SAME L2 DOMAIN
// BOUNDED LOOP (MAX 8 ITERATIONS), VERIFIER-SAFE.
// RETURNS IDLE CPU IN SAME L2 GROUP, OR -1 IF NONE FOUND.

static __always_inline s32 find_idle_l2_sibling(const struct task_ctx *tctx)
{
	if (tctx->last_cpu < 0)
		return -1;

	u32 lcpu = (u32)tctx->last_cpu;
	u32 *group = bpf_map_lookup_elem(&cache_domain, &lcpu);
	if (!group)
		return -1;

	u32 base = *group * MAX_L2_SIBLINGS;
	for (int i = 0; i < MAX_L2_SIBLINGS; i++) {
		u32 key = base + i;
		u32 *val = bpf_map_lookup_elem(&l2_siblings, &key);
		if (!val || *val == (u32)-1)
			break;
		s32 cpu = (s32)*val;
		if (scx_bpf_test_and_clear_cpu_idle(cpu))
			return cpu;
	}
	return -1;
}

// RESISTANCE AFFINITY: IDLE CPU SEARCH BY EFFECTIVE RESISTANCE
// WALKS THE R_EFF-RANKED AFFINITY LIST (LAPLACIAN PSEUDOINVERSE) FOR A
// GIVEN SOURCE CPU. RETURNS FIRST IDLE CPU FOUND, OR -1.
// SEARCH IS BOUNDED TO limit ENTRIES TO CONTROL HOT-PATH COST.
// SLOT 0 = L2 SIBLING (LOWEST R_EFF), SLOT 1+ = NEXT CHEAPEST.
// NO DEPTH GATE. NO DSQ DISPATCH. PURE IDLE SEARCH.
// REFERENCE: KYNG ET AL. EFFECTIVE RESISTANCE (STOC 2011, FOCS 2022)

// BUDGET IS ONLINE CANDIDATES CHECKED, NOT TOTAL SLOTS WALKED.
// affinity_rank IS BUILT AT INIT AND KEEPS OFFLINE ENTRIES AFTER HOTPLUG.
// WALKING FROM RANK 0 MAY HIT SEVERAL OFFLINE SLOTS BEFORE AN ONLINE ONE;
// THOSE SLOTS ARE CHEAP TO SKIP (ONE MAP LOOKUP EACH, NO IDLE CHECK).
// THE EXPENSIVE OP IS scx_bpf_test_and_clear_cpu_idle; CAP THAT AT 3.
// THIS IS ROBUST TO ANY HOTPLUG TOPOLOGY (12C->8C, 32C->4C, ETC.) --
// OFFLINE ENTRIES DON'T ROB THE BUDGET, SO THE COST IS THE SAME ON A
// FULLY-ONLINE SYSTEM AS THE ORIGINAL LIMIT=3 WAS.
#define AFFINITY_SEARCH_ONLINE 3

static __always_inline s32 find_idle_by_affinity(s32 src_cpu)
{
	if (src_cpu < 0 || (u32)src_cpu >= nr_cpu_ids)
		return -1;

	u32 base = (u32)src_cpu * MAX_AFFINITY_CANDIDATES;
	u32 checked = 0;
	for (int i = 0; i < MAX_AFFINITY_CANDIDATES; i++) {
		u32 key = base + (u32)i;
		u32 *val = bpf_map_lookup_elem(&affinity_rank, &key);
		// SENTINEL OR MISSING -> END OF LIST, STOP.
		if (!val || *val == (u32)-1)
			break;
		// OFFLINE CPU POST-HOTPLUG -> SKIP WITHOUT COSTING BUDGET.
		// affinity_rank IS BUILT AT INIT FROM THE FULL TOPOLOGY;
		// HOTPLUG DOESN'T REBUILD IT.
		if (*val >= nr_cpu_ids)
			continue;
		if (scx_bpf_test_and_clear_cpu_idle((s32)*val))
			return (s32)*val;
		// BUDGET IS ONLINE CANDIDATES, NOT SLOTS WALKED.
		if (++checked >= AFFINITY_SEARCH_ONLINE)
			break;
	}

	return -1;
}

// CODEL DRAIN RATE: UPDATE MIN SOJOURN WHEN TASK DEQUEUED FROM PER-CPU DSQ
static __always_inline void update_pcpu_sojourn(u32 cpu, u64 now)
{
	if (cpu >= MAX_CPUS) return;
	u64 enq = pcpu_enqueue_ns[cpu];
	if (enq == 0) return;
	u64 sojourn = now - enq;
	if (sojourn < pcpu_min_sojourn_ns[cpu])
		pcpu_min_sojourn_ns[cpu] = sojourn;
}

// CODEL STALL DETECTION: MIN SOJOURN ABOVE DYNAMIC TARGET FOR INTERVAL = STALLED.
// THE TARGET (codel_target_ns) IS MODULATED BY DAMPED OSCILLATION IN tick().
// RESCUES PULL THE TARGET DOWN (TIGHTEN). QUIET PUSHES IT UP (RELAX).
// THE TARGET ADAPTS TO WHAT "NORMAL SOJOURN" IS ON THIS SYSTEM RIGHT NOW.
static __always_inline bool pcpu_dsq_is_stalled(u32 cpu, u64 now)
{
	if (cpu >= MAX_CPUS) return false;
	u64 min_s = pcpu_min_sojourn_ns[cpu];

	if (min_s < codel_target_ns) {
		pcpu_stall_start_ns[cpu] = 0;
		pcpu_min_sojourn_ns[cpu] = ~0ULL;
		return false;
	}

	if (pcpu_stall_start_ns[cpu] == 0) {
		pcpu_stall_start_ns[cpu] = now + sojourn_interval_ns;
		return false;
	}

	if (now >= pcpu_stall_start_ns[cpu]) {
		pcpu_min_sojourn_ns[cpu] = ~0ULL;
		pcpu_stall_start_ns[cpu] = 0;
		return true;
	}

	return false;
}

// SOJOURN GATE: RETURNS TRUE IF BOTH OVERFLOW DSQs ARE WITHIN THE RESCUE
// WINDOW (i.e. IT IS SAFE TO RETURN FROM dispatch() AFTER A STEP 0/1/1b HIT
// WITHOUT STARVING A SHARED OVERFLOW DSQ). CALLERS SHORT-CIRCUIT AS
// `if (sojourn_gate_pass(now)) return;` TO KEEP THE HOT PATH FLAT.
static __always_inline bool sojourn_gate_pass(u64 now)
{
	u64 ie = interactive_enqueue_ns;
	u64 be = batch_enqueue_ns;
	return (ie == 0 || (now - ie) <= overflow_sojourn_rescue_ns) &&
	       (be == 0 || (now - be) <= overflow_sojourn_rescue_ns);
}

// TAU-SCALED TIMING CONSTANT DERIVATION.
//   tau_ns * k_q16 / 65536. When tau_ns is 0 (feature-flag off), callers are
//   expected to skip this and use their legacy formula. No div, no float --
//   verifier-clean. The multiply cannot overflow u64 for any sane (tau, k_i)
//   pair (tau <= 40e6 ns, k_i <= ~3.3e6 Q16 -> product ~1.3e14, fits in u64).
static __always_inline u64 scale_tau(u64 tau_ns, u64 k_q16)
{
	return (tau_ns * k_q16) >> K_Q16_SHIFT;
}

// TAU-SCALING RE-DERIVATION.
//   Init runs before Rust writes topology_tau_ns, so pandemonium_init() uses
//   legacy core-scaled formulas. First tick on CPU 0 calls this after reading
//   knobs; if tau differs from last_tau_snapshot, every tau-scaled static is
//   re-derived via scale_tau() and clamped to its legacy safety rail.
//   Hotplug flows through the same path (Rust re-writes tau, next tick picks
//   it up). tau == 0 reverts to legacy formulas unchanged.
static __always_inline void apply_tau_scaling(u64 tau_ns)
{
	// SHORT-CIRCUIT ON UNCHANGED OR ZERO tau. THE ZERO CASE COVERS THE
	// ~1MS WINDOW BEFORE RUST WRITES THE KNOB AFTER struct_ops ATTACH;
	// INIT-TIME MIDPOINT CONSTANTS STAND UNTIL tau ARRIVES. AFTER THAT,
	// EVERY CHANGE TO tau (HOTPLUG) RE-DERIVES THE FULL SET.
	if (tau_ns == 0 || tau_ns == last_tau_snapshot)
		return;
	last_tau_snapshot = tau_ns;

	// DERIVE EACH TIMING CONSTANT VIA k_i * tau, THEN APPLY THE CLAMP AS
	// A SAFETY RAIL DURING ROLLOUT (KILL SWITCH IF A k_i IS MISCALIBRATED).
	u64 v;

	v = scale_tau(tau_ns, K_SOJOURN_INTERVAL);
	if (v < 2000000ULL) v = 2000000ULL;
	if (v > 12000000ULL) v = 12000000ULL;
	sojourn_interval_ns = v;

	v = scale_tau(tau_ns, K_OVERFLOW_RESCUE);
	if (v < 4000000ULL) v = 4000000ULL;
	if (v > 10000000ULL) v = 10000000ULL;
	overflow_sojourn_rescue_ns = v;

	v = scale_tau(tau_ns, K_STARVATION_RESCUE);
	if (v < 20000000ULL) v = 20000000ULL;
	if (v > 500000000ULL) v = 500000000ULL;
	starvation_rescue_ns = v;

	v = scale_tau(tau_ns, K_CODEL_FLOOR);
	if (v < 200000ULL) v = 200000ULL;
	if (v > 800000ULL) v = 800000ULL;
	codel_target_floor_ns = v;

	v = scale_tau(tau_ns, K_LONGRUN);
	if (v < 500000000ULL) v = 500000000ULL;       // FLOOR 500MS (HALF LEGACY)
	if (v > 8000000000ULL) v = 8000000000ULL;     // CEILING 8S (4X LEGACY)
	longrun_thresh_ns = v;

	v = scale_tau(tau_ns, K_CODEL_MAX);
	if (v < 1000000ULL) v = 1000000ULL;           // FLOOR 1MS
	if (v > 8000000ULL) v = 8000000ULL;           // CEILING 8MS
	codel_target_max_ns = v;

	// OSCILLATOR DYNAMICS: DERIVED FROM tau SO THE CONTROLLER RUNS ON THE
	// SAME TIME CONSTANT AS ITS TARGET RANGE. DIRECT-DIVIDE (NOT Q16)
	// BECAUSE pull_scale (1-4) AND damping_shift (1-5) ARE SMALL INTEGERS.
	// REFERENCE: tau=40MS (12C HETEROGENEOUS) -> pull=4, damp=5 MATCHING
	// PRIOR 12C VALUES.
	u32 pull = (u32)(tau_ns / K_OSC_PULL_THRESH_NS);
	if (pull < 1) pull = 1;
	if (pull > 4) pull = 4;
	oscillator_pull_scale = pull;

	u32 damp = (u32)(tau_ns / K_OSC_DAMP_THRESH_NS);
	if (damp < 1) damp = 1;
	if (damp > 5) damp = 5;
	oscillator_damping_shift = damp;

	// velocity_cap PRESERVES COUPLING TO pull_scale.
	oscillator_velocity_cap = (s64)((u64)OSC_VELOCITY_CAP_PER_PULL * (u64)pull);
}

// PCPU DSQ DRAIN-AND-CLEAR: shared by STEP -1, STEP 0, STEP 1, STEP 1b.
// CALLED AFTER A SUCCESSFUL scx_bpf_dsq_move_to_local((u64)cpu). CLEARS THE
// PER-CPU ENQUEUE TIMESTAMP IF THE DSQ DRAINED EMPTY. CALLERS STILL OWN
// THEIR COUNTER UPDATES (interactive_run/nr_overflow_rescue/global_rescue_count)
// BECAUSE THOSE DIVERGE ACROSS SITES.
static __always_inline void pcpu_drain_clear(u32 cpu)
{
	if (cpu >= MAX_CPUS)
		return;
	if (scx_bpf_dsq_nr_queued((u64)cpu) != 0)
		return;
	u64 old = pcpu_enqueue_ns[cpu];
	if (old > 0)
		__sync_val_compare_and_swap(&pcpu_enqueue_ns[cpu], old, 0);
}

// HISTOGRAM BUCKETING: MATCHES HIST_EDGES_NS AND SLEEP_EDGES_NS IN RUST

static __always_inline u32 lat_bucket(u64 lat_ns)
{
	if (lat_ns <= 10000) return 0;
	if (lat_ns <= 25000) return 1;
	if (lat_ns <= 50000) return 2;
	if (lat_ns <= 100000) return 3;
	if (lat_ns <= 250000) return 4;
	if (lat_ns <= 500000) return 5;
	if (lat_ns <= 1000000) return 6;
	if (lat_ns <= 2000000) return 7;
	if (lat_ns <= 5000000) return 8;
	if (lat_ns <= 10000000) return 9;
	if (lat_ns <= 20000000) return 10;
	return 11;
}

static __always_inline u32 sleep_bucket(u64 sleep_ns)
{
	if (sleep_ns <= 1000000) return 0;
	if (sleep_ns <= 10000000) return 1;
	if (sleep_ns <= 100000000) return 2;
	return 3;
}

// EWMA

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

// BEHAVIORAL CLASSIFICATION

// LAT_CRI SCORE: HIGH WAKEUP FREQ + HIGH CSW RATE + SHORT RUNTIME = CRITICAL
static __always_inline u64 compute_lat_cri(u64 wakeup_freq, u64 csw_rate,
					    u64 avg_runtime_ns,
					    u64 runtime_dev_ns)
{
	u64 effective_runtime_ns = avg_runtime_ns + (runtime_dev_ns >> 1);
	u64 avg_runtime_ms = effective_runtime_ns >> 20;
	if (avg_runtime_ms == 0)
		avg_runtime_ms = 1;
	u64 score = (wakeup_freq * csw_rate) / avg_runtime_ms;
	if (score > LAT_CRI_CAP)
		score = LAT_CRI_CAP;
	return score;
}

static __always_inline u32 classify_tier(u64 lat_cri,
					  const struct tuning_knobs *knobs)
{
	u64 thresh_high = knobs ? knobs->lat_cri_thresh_high : LAT_CRI_THRESH_HIGH;
	u64 thresh_low  = knobs ? knobs->lat_cri_thresh_low  : LAT_CRI_THRESH_LOW;
	if (lat_cri >= thresh_high)
		return TIER_LAT_CRITICAL;
	if (lat_cri >= thresh_low)
		return TIER_INTERACTIVE;
	return TIER_BATCH;
}

// COMPOSITOR DETECTION: MAP LOOKUP (POPULATED BY RUST AT STARTUP)
// STACK-LOCAL KEY COPY: BPF VERIFIER REJECTS DIRECT p->comm POINTER
static __always_inline bool is_compositor(const struct task_struct *p)
{
	char key[16] = {};
	unsigned int i;
	for (i = 0; i < 15 && p->comm[i]; i++)
		key[i] = p->comm[i];
	return bpf_map_lookup_elem(&compositor_map, key) != NULL;
}

// TRACE: FAST 4-BYTE COMM CHECK FOR SCHEDULER PROCESS TRACING
// CATCHES "pandemonium" WITH ZERO MAP OVERHEAD. GATED BY TRACE_SCHED BECAUSE
// ALL CALL SITES ARE #if TRACE_SCHED -- WITHOUT THE GUARD ON THE DEFINITION,
// CLANG WARNS -Wunused-function WHEN TRACE_SCHED=0 (SCX MONOREPO DEFAULT).
#if TRACE_SCHED
static __always_inline bool is_sched_task(const struct task_struct *p)
{
	return p->comm[0] == 'p' && p->comm[1] == 'a' &&
	       p->comm[2] == 'n' && p->comm[3] == 'd';
}
#endif

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

// SCHEDULING HELPERS

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
	// SLICE COMPRESSION: longrun_mode IS THE ONLY CONSUMER. SUSTAINED BATCH
	// PRESSURE SWAPS IN burst_slice_ns; EVERYTHING ELSE USES slice_ns.
	u64 base_slice = knobs ? (longrun_mode
		? knobs->burst_slice_ns : knobs->slice_ns) : 1000000;
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

	// BATCH: DEDICATED CEILING FROM RUST ADAPTIVE LAYER.
	// WEIGHT-SCALED: HIGHER BEHAVIORAL WEIGHT = LONGER SLICE.
	u64 batch_ceil = knobs ? knobs->batch_slice_ns : 20000000;
	if (batch_ceil < SLICE_MIN_NS)
		batch_ceil = SLICE_MIN_NS;

	base = batch_ceil * tctx->cached_weight >> 7;
	if (base > batch_ceil)
		base = batch_ceil;
	if (base < SLICE_MIN_NS)
		base = SLICE_MIN_NS;

	return base;
}

// SCHEDULING CALLBACKS

// SELECT_CPU: FAST-PATH IDLE CPU DISPATCH TO PER-CPU DSQ
// DISPATCHES TO NAMED PER-CPU DSQ (u64)cpu -- VISIBLE TO WORK STEALING
// AND SOJOURN RESCUE. DEPTH-GATED: IF PER-CPU DSQ ALREADY HAS TASKS,
// SPILL TO SHARED NODE DSQ SO ANY CPU CAN GRAB IT.
// THE CPU IS IDLE SO IT ENTERS dispatch() IMMEDIATELY AND DRAINS.
s32 BPF_STRUCT_OPS(pandemonium_select_cpu, struct task_struct *p,
		   s32 prev_cpu, u64 wake_flags)
{
	bool is_idle = false;

	// RESISTANCE AFFINITY: WAKEE_FLIPS-GATED WAKE_SYNC
	// GATE: wakee_flips (per-task wakeup partner diversity) separates
	//   1:1 pipe pairs (low flips, affinity beneficial) from
	//   1:N server patterns (high flips, affinity harmful).
	// PLACEMENT: R_eff ranked search from waker's CPU finds cheapest
	//   idle CPU in waker's L2 group. Falls back to waker's DSQ if
	//   no idle found and DSQ depth allows.
	// REFERENCE: kernel wake_wide() uses same wakee_flips signal.
	//   Kyng et al. effective resistance for migration cost.
	if (wake_flags & SCX_WAKE_SYNC) {
		struct task_struct *waker =
			(struct task_struct *)bpf_get_current_task_btf();
		if (waker) {
			u32 wflips = BPF_CORE_READ(waker, wakee_flips);
			u32 pflips = p->wakee_flips;
			u32 thresh = nr_cpu_ids;

			// WAKE_WIDE: SKIP IF EITHER SIDE WAKES DIVERSE TASKS
			if (wflips <= thresh && pflips <= thresh) {
				s32 waker_cpu = bpf_get_smp_processor_id();
				if ((u64)waker_cpu >= nr_cpu_ids)
					goto normal_path;

				// R_EFF RANKED IDLE SEARCH FROM WAKER
				s32 target = find_idle_by_affinity(waker_cpu);
				if (target >= 0) {
					struct task_ctx *tctx = lookup_task_ctx(p);
					struct tuning_knobs *knobs = get_knobs();
					u64 sl = tctx ? task_slice(tctx, knobs)
						      : 1000000;
					u64 dl = tctx ? task_deadline(p, tctx,
						(u64)target, knobs) : vtime_now;
					scx_bpf_dsq_insert_vtime(p,
						(u64)target, sl, dl, 0);
					if ((u32)target < MAX_CPUS)
						__sync_val_compare_and_swap(
							&pcpu_enqueue_ns[target & (MAX_CPUS - 1)],
							0, bpf_ktime_get_ns());
					if (tctx)
						tctx->dispatch_path = 0;
					struct pandemonium_stats *s = get_stats();
					if (s) {
						s->nr_idle_hits += 1;
						s->nr_dispatches += 1;
					}
					return target;
				}

				// NO IDLE NEAR WAKER: DSQ DISPATCH IF DSQ IS FLOWING
				// CODEL: IF MIN SOJOURN < 500us OVER LAST 8ms, TASKS
				// ARE CYCLING THROUGH FAST. DSQ DISPATCH IS SAFE.
				// IF STALLED (PINNED WORKERS), FALL THROUGH TO
				// NORMAL PATH WHERE scx_bpf_select_cpu_dfl HANDLES
				// PREEMPTION AND LOAD BALANCING.
				if (!pcpu_dsq_is_stalled(
					(u32)waker_cpu, bpf_ktime_get_ns())) {
					struct task_ctx *tctx = lookup_task_ctx(p);
					struct tuning_knobs *knobs = get_knobs();
					u64 sl = tctx ? task_slice(tctx, knobs)
						      : 1000000;
					u64 dl = tctx ? task_deadline(p, tctx,
						(u64)waker_cpu, knobs)
						      : vtime_now;
					scx_bpf_dsq_insert_vtime(p,
						(u64)waker_cpu, sl, dl, 0);
					if ((u32)waker_cpu < MAX_CPUS)
						__sync_val_compare_and_swap(
							&pcpu_enqueue_ns[waker_cpu & (MAX_CPUS - 1)],
							0, bpf_ktime_get_ns());
					if (tctx)
						tctx->dispatch_path = 0;
					struct pandemonium_stats *s = get_stats();
					if (s) {
						s->nr_idle_hits += 1;
						s->nr_dispatches += 1;
					}
					return waker_cpu;
				}
			}
		}
	}
normal_path:;

	s32 cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);

	if (is_idle) {
		struct task_ctx *tctx = lookup_task_ctx(p);
		struct tuning_knobs *knobs = get_knobs();
		u64 sl = tctx ? task_slice(tctx, knobs) : 1000000;

		// PER-CPU DSQ DEPTH GATE. STEP -1 RESCUE AND HARD STARVATION RESCUE
		// (BOTH DRIVEN BY global_rescue_count) ARE THE STALL-RELIEF PATH;
		// THE DEPTH=1 SPILL THAT USED TO TRIGGER HERE IS NO LONGER NEEDED.
		u32 depth_thresh = pcpu_depth_base;
		if ((u64)cpu < nr_cpu_ids &&
		    scx_bpf_dsq_nr_queued((u64)cpu) < depth_thresh) {
			// PER-CPU DSQ: CACHE-HOT, VISIBLE, STEALABLE
			u64 dl = tctx ? task_deadline(p, tctx, (u64)cpu, knobs)
				      : vtime_now;
			scx_bpf_dsq_insert_vtime(p, (u64)cpu, sl, dl, 0);
			if ((u32)cpu < MAX_CPUS)
				__sync_val_compare_and_swap(
					&pcpu_enqueue_ns[cpu], 0,
					bpf_ktime_get_ns());
		} else {
			// DEPTH EXCEEDED: SPILL TO SHARED NODE DSQ
			s32 node = __COMPAT_scx_bpf_cpu_node(cpu);
			if (node < 0 || (u32)node >= nr_nodes) node = 0;
			u64 node_dsq = nr_cpu_ids + (u64)node;
			u64 dl = tctx ? task_deadline(p, tctx, node_dsq, knobs)
				      : vtime_now;
			scx_bpf_dsq_insert_vtime(p, node_dsq, sl, dl, 0);
			__sync_val_compare_and_swap(
				&interactive_enqueue_ns, 0,
				bpf_ktime_get_ns());
		}

		scx_bpf_kick_cpu(cpu, SCX_KICK_IDLE);
		__sync_fetch_and_add(&interactive_run, 1);

		if (tctx)
			tctx->dispatch_path = 0;

		struct pandemonium_stats *s = get_stats();
		if (s) {
			s->nr_idle_hits += 1;
			s->nr_dispatches += 1;
			if (tctx)
				count_l2_affinity(s, tctx, cpu);
		}

#if TRACE_SCHED
		if (is_sched_task(p))
			bpf_printk("PAND: select_cpu pid=%d cpu=%d", p->pid, cpu);
#endif
	}

	return cpu;
}

// ENQUEUE: THREE-TIER PLACEMENT WITH BEHAVIORAL PREEMPTION
// TIER 1: IDLE CPU ON NODE -> PER-CPU DSQ (DEPTH-GATED) + KICK
// TIER 2: INTERACTIVE/LAT_CRITICAL -> PER-CPU DSQ (DEPTH-GATED) + HARD PREEMPT
// TIER 3: FALLBACK -> PER-NODE OVERFLOW DSQ + SELECTIVE KICK
void BPF_STRUCT_OPS(pandemonium_enqueue, struct task_struct *p,
		    u64 enq_flags)
{
	s32 node = __COMPAT_scx_bpf_cpu_node(scx_bpf_task_cpu(p));
	if (node < 0 || (u32)node >= nr_nodes) node = 0;
	u64 node_dsq = nr_cpu_ids + (u64)node;

	struct task_ctx *tctx = lookup_task_ctx(p);

	struct tuning_knobs *knobs = get_knobs();
	u64 sl = tctx ? task_slice(tctx, knobs) : 1000000;
	u64 dl;

	// CLASSIFY: WAKEUP VS RE-ENQUEUE
	bool is_wakeup = tctx && tctx->awake_vtime == 0;

	// TIER 1: IDLE CPU -> NODE DSQ + KICK
	// L2 PLACEMENT: TRY IDLE SIBLING IN SAME L2 DOMAIN FIRST.
	// LAT_CRITICAL AND KERNEL THREADS SKIP AFFINITY -- FASTEST CPU WINS.
	// TASK GOES TO SHARED NODE DSQ SO ANY CPU ON THE NODE CAN DRAIN IT.
	s32 cpu = -1;
	if (knobs && knobs->affinity_mode > 0 && tctx &&
	    tctx->tier != TIER_LAT_CRITICAL &&
	    !(p->flags & PF_KTHREAD)) {
		// cpu = find_idle_by_affinity(tctx->last_cpu);
		cpu = find_idle_l2_sibling(tctx);
	}
	if (cpu < 0)
		cpu = __COMPAT_scx_bpf_pick_idle_cpu_node(p->cpus_ptr, node, 0);
	if (cpu >= 0 && (u64)cpu < nr_cpu_ids) {
		dl = tctx ? task_deadline(p, tctx, node_dsq, knobs)
			  : vtime_now;
		scx_bpf_dsq_insert_vtime(p, node_dsq, sl, dl, enq_flags);

		u64 kick_flag = (tctx && tctx->tier != TIER_BATCH)
			      ? SCX_KICK_PREEMPT : SCX_KICK_IDLE;
		scx_bpf_kick_cpu(cpu, kick_flag);

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
			if (tctx)
				count_l2_affinity(s, tctx, cpu);
		}
#if TRACE_SCHED
		if (is_sched_task(p))
			bpf_printk("PAND: enq tier1 pid=%d cpu=%d", p->pid, cpu);
#endif
		return;
	}

	// TIER 2: WAKEUP PREEMPTION -- NODE DSQ + SELECTIVE KICK
	// ALL WAKEUPS GET NODE DSQ DISPATCH: A TASK WAKING FROM SLEEP
	// HAS EXTERNAL INPUT TO DELIVER (TIMER, IO, USER) REGARDLESS OF
	// BEHAVIORAL TIER. THE CLASSIFIER OPERATES ON HISTORICAL BEHAVIOR;
	// THE WAKEUP IS THE REAL-TIME LATENCY SIGNAL.
	// LAT_CRITICAL ALSO GETS PREEMPTION ON REQUEUE (COMPOSITOR GUARANTEE).
	// ONLINE GUARD: pick_any_cpu_node() CAN RETURN OFFLINE CPUs DURING
	// HOTPLUG. OFFLINE CPUs HAVE NO CURRENT TASK (cpu_curr == NULL).
	if (tctx &&
	    (tctx->tier == TIER_LAT_CRITICAL || is_wakeup)) {
		cpu = __COMPAT_scx_bpf_pick_any_cpu_node(
			p->cpus_ptr, node, 0);
		if (cpu >= 0 && (u64)cpu < nr_cpu_ids &&
		    __COMPAT_scx_bpf_cpu_curr(cpu)) {
			// DEPTH GATE: USE PER-CPU DSQ IF ROOM, ELSE NODE DSQ.
			// PREVENTS DSQ BUILDUP DURING FORK STORMS.
			u64 tier2_dsq;
			if ((u64)cpu < nr_cpu_ids &&
			    scx_bpf_dsq_nr_queued((u64)cpu) < pcpu_depth_base) {
				tier2_dsq = (u64)cpu;
				if ((u32)cpu < MAX_CPUS)
					__sync_val_compare_and_swap(
						&pcpu_enqueue_ns[cpu], 0,
						bpf_ktime_get_ns());
			} else {
				tier2_dsq = node_dsq;
				__sync_val_compare_and_swap(
					&interactive_enqueue_ns, 0,
					bpf_ktime_get_ns());
			}

			dl = task_deadline(p, tctx, tier2_dsq, knobs);
			scx_bpf_dsq_insert_vtime(p, tier2_dsq, sl, dl,
						  enq_flags);

			u64 kick_flag = (is_wakeup ||
				 tctx->tier == TIER_LAT_CRITICAL)
				? SCX_KICK_PREEMPT : SCX_KICK_IDLE;
			scx_bpf_kick_cpu(cpu, kick_flag);
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
#if TRACE_SCHED
			if (is_sched_task(p))
				bpf_printk("PAND: enq tier2 pid=%d cpu=%d dsq=%llu", p->pid, cpu, tier2_dsq);
#endif
			return;
		}
	}

	// TIER 3: NODE OVERFLOW DSQ + SELECTIVE KICK
	// ONLY BATCH-CLASSIFIED TASKS GO TO BATCH DSQ.
	// IMMATURE TASKS (ewma_age < 2) STAY IN INTERACTIVE DSQ TO PREVENT
	// STARVATION DURING BURST SPAWNS -- NEW THREADS STARTING WITH
	// ewma_age=0 WOULD FLOOD THE BATCH DSQ AND STARVE FOR 30-40S
	// WAITING FOR SOJOURN RESCUE THAT NEVER REACHES THE TAIL.
	// LAT_CRITICAL (COMPOSITORS) ARE NEVER REDIRECTED.
	u64 target_dsq = (tctx && tctx->tier == TIER_BATCH)
		? (nr_cpu_ids + nr_nodes + (u64)node)
		: node_dsq;

	// SOJOURN TRACKING: RECORD WHEN OVERFLOW DSQs TRANSITION FROM EMPTY.
	// DISPATCH STEP 0 CHECKS THESE TO RESCUE TASKS AGING PAST THRESHOLD.
	if (target_dsq != node_dsq)
		__sync_val_compare_and_swap(&batch_enqueue_ns, 0, bpf_ktime_get_ns());
	if (target_dsq == node_dsq)
		__sync_val_compare_and_swap(&interactive_enqueue_ns, 0, bpf_ktime_get_ns());

	dl = tctx ? task_deadline(p, tctx, target_dsq, knobs) : vtime_now;

	// VTIME CEILING: PREVENT UNBOUNDED STARVATION DURING BURST.
	// HIGH-VTIME DAEMONS SORT TO THE TAIL OF THE VTIME-ORDERED BATCH DSQ
	// WHILE FRESH BURST TASKS WITH LOW VTIME TAKE THE HEAD. SOJOURN RESCUE
	// DISPATCHES FROM THE HEAD, SO DAEMONS AT THE TAIL STARVE.
	// THE CEILING CAPS DEADLINE AT vtime_now + 30MS, KEEPING EVERY BATCH
	// TASK WITHIN 6 SOJOURN CYCLES OF THE HEAD.
	// CORE-SCALED CEILING: WIDER AT LOW CORES (PRESERVE DIFFERENTIATION),
	// TIGHTER AT HIGH CORES (PREVENT TAIL STARVATION).
	// 2C: 40MS, 4C: 40MS, 8C: 80MS, 16C: 160MS (CAPPED AT LAG_CAP_NS*4)
	if (target_dsq != node_dsq) {
		u64 ceil_scale = nr_cpu_ids >> 2;
		if (ceil_scale < 1) ceil_scale = 1;
		if (ceil_scale > 4) ceil_scale = 4;
		u64 vtime_ceiling = vtime_now + LAG_CAP_NS * ceil_scale;
		if (time_after(dl, vtime_ceiling))
			dl = vtime_ceiling;
	}

	scx_bpf_dsq_insert_vtime(p, target_dsq, sl, dl, enq_flags);

#if TRACE_SCHED
	if (is_sched_task(p))
		bpf_printk("PAND: enq tier3 pid=%d dsq=%llu tier=%d", p->pid, target_dsq, tctx ? tctx->tier : -1);
#endif

	// ARM TICK SAFETY NET: SIGNAL THAT INTERACTIVE TASKS ARE WAITING IN OVERFLOW.
	// tick() CHECKS THIS FLAG TO PREEMPT BATCH TASKS VIA preempt_thresh_ns.
	// TIER_LAT_CRITICAL ALSO ARMS latcrit_waiting SO THE TICK PATH CAN USE A
	// TIGHTER THRESHOLD -- AUDIO/COMPOSITOR WAKERS SHOULDN'T SIT BEHIND A FULL
	// BATCH SLICE.
	if (tctx && tctx->tier != TIER_BATCH) {
		interactive_waiting = true;
		if (tctx->tier == TIER_LAT_CRITICAL)
			latcrit_waiting = true;
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
// HYBRID PER-CPU + NODE DSQ DESIGN:
//   SELECT_CPU -> PER-CPU DSQ (DEPTH-GATED, VISIBLE, STEALABLE)
//   ENQUEUE TIER 1/2 -> NODE DSQ (SHARED, ANY CPU DRAINS)
//   ENQUEUE TIER 3 -> PER-NODE BATCH/INTERACTIVE DSQ
//
// -1. GLOBAL PER-CPU DSQ STALL SCAN (v5.7.0: CLOSES KWORKER/JOURNAL CLASS)
//  0. OWN PER-CPU DSQ (CACHE-HOT, ZERO CONTENTION)
//  1. L2 WORK STEALING (SIBLING PER-CPU DSQs, SAME CACHE DOMAIN)
//  1b. R_EFF CROSS-L2 FALLBACK (v5.7.0: TOPOLOGY-ASYMMETRIC CPUs)
//  2. HARD STARVATION RESCUE (PRE-DEFICIT SAFETY NET, BOTH TIERS)
//  3. DEFICIT GATE + OVERFLOW SOJOURN AMPLIFICATION
//  4. BATCH OVERFLOW RESCUE (BUDGET-EXHAUSTED BATCH FORCE)
//  5. DEFICIT COUNTER (DRR: INTERLEAVE BATCH INTO INTERACTIVE SERVICE)
//  6. NODE INTERACTIVE OVERFLOW (LAT_CRIT + INTERACTIVE, VTIME-ORDERED)
//  7. BATCH SOJOURN RESCUE (CODEL-BINARY STARVATION SAFETY NET)
//  8. NODE BATCH OVERFLOW (NORMAL BATCH FALLBACK)
//  9. CROSS-NODE STEAL (INTERACTIVE + BATCH PER REMOTE NODE)
// 10. KEEP_RUNNING IF PREV STILL WANTS CPU AND NOTHING QUEUED
void BPF_STRUCT_OPS(pandemonium_dispatch, s32 cpu, struct task_struct *prev)
{
	s32 node = __COMPAT_scx_bpf_cpu_node(cpu);
	if (node < 0 || (u32)node >= nr_nodes) node = 0;
	u64 node_dsq = nr_cpu_ids + (u64)node;
	u64 batch_dsq = nr_cpu_ids + nr_nodes + (u64)node;
	struct pandemonium_stats *s;
	u64 now = bpf_ktime_get_ns();

	// STEP -1: GLOBAL PER-CPU DSQ STALL SCAN.
	// THE WATERFALL STEPS 0-1 ONLY TOUCH OWN + L2 SIBLING PER-CPU DSQs,
	// SO A TASK ENQUEUED ON CPU X'S PER-CPU DSQ CAN STRAND IF CPU X AND
	// ITS L2 SIBLINGS ALL RUN LONG-SLICE TASKS. THIS CAUSED THE
	// 2026-04-09 (kworker/0:2, 35.2s) AND 2026-04-12
	// (systemd-journal[430], 38.9s) WATCHDOG KILLS ON v5.6.0.
	//
	// FIX: BEFORE THE NORMAL WATERFALL, SCAN ALL PER-CPU DSQs. IF ANY
	// HEAD HAS AGED PAST codel_target_ns + sojourn_interval_ns (THE
	// OSCILLATOR-ADAPTED CoDel STALL WINDOW), DRAIN IT REGARDLESS OF
	// WHICH CPU OWNS IT. SINGLE BOUNDED LOOP, MINIMAL STATE ->
	// VERIFIER-FRIENDLY.
	{
		u64 threshold = codel_target_ns + sojourn_interval_ns;
		for (u32 i = 0; i < MAX_CPUS; i++) {
			if (i >= nr_cpu_ids) break;
			u64 enq = pcpu_enqueue_ns[i];
			if (enq == 0) continue;
			if ((now - enq) < threshold) continue;
			if (!scx_bpf_dsq_move_to_local((u64)i)) continue;
			pcpu_drain_clear(i);
			__sync_fetch_and_add(&global_rescue_count, 1);
			s = get_stats();
			if (s) {
				s->nr_dispatches += 1;
				s->nr_overflow_rescue += 1;
			}
			return;
		}
	}

	// STEP 0: OWN PER-CPU DSQ -- HIGHEST PRIORITY, CACHE-HOT
	if ((u64)cpu < nr_cpu_ids &&
	    scx_bpf_dsq_move_to_local((u64)cpu)) {
		// CODEL: UPDATE DRAIN RATE BEFORE CLEARING TIMESTAMP
		update_pcpu_sojourn((u32)cpu, now);
		pcpu_drain_clear((u32)cpu);
		__sync_fetch_and_add(&interactive_run, 1);
		s = get_stats();
		if (s)
			s->nr_dispatches += 1;
		// SOJOURN GATE: ONLY RETURN IF SHARED DSQs ARE NOT STARVING.
		// IF EITHER OVERFLOW DSQ HAS TASKS AGING PAST THRESHOLD,
		// FALL THROUGH SO DOWNSTREAM RESCUE LOGIC CAN FIRE.
		if (sojourn_gate_pass(now))
			return;
	}

	// STEP 1: L2 WORK STEALING -- PULL FROM SIBLING PER-CPU DSQs
	// SAME L2 CACHE DOMAIN = MINIMAL CACHE PENALTY ON STEAL.
	// BOUNDED LOOP (MAX_L2_SIBLINGS), SAME PATTERN AS find_idle_l2_sibling.
	u32 my_cpu = (u32)cpu;
	bool stolen = false;
	u32 *group = bpf_map_lookup_elem(&cache_domain, &my_cpu);
	if (group) {
		u32 base = *group * MAX_L2_SIBLINGS;
		for (int i = 0; i < MAX_L2_SIBLINGS; i++) {
			u32 key = base + i;
			u32 *val = bpf_map_lookup_elem(&l2_siblings, &key);
			if (!val || *val == (u32)-1)
				break;
			u32 sibling = *val;
			if (sibling == my_cpu || sibling >= nr_cpu_ids)
				continue;
			if (scx_bpf_dsq_move_to_local((u64)sibling)) {
				// CODEL: UPDATE DRAIN RATE BEFORE CLEARING
				update_pcpu_sojourn(sibling, now);
				pcpu_drain_clear(sibling);
				__sync_fetch_and_add(&interactive_run, 1);
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				stolen = true;
				// SOJOURN GATE: SAME CHECK AS STEP 0.
				if (sojourn_gate_pass(now))
					return;
				break;
			}
		}
	}

	// STEP 1b: R_EFF CROSS-L2 FALLBACK -- STEAL FROM NEAREST ONLINE PEERS.
	// FIRES ONLY IF L2 STEAL FOUND NOTHING. NECESSARY FOR TOPOLOGY-
	// ASYMMETRIC CPUs (e.g. SOLO AFTER HOTPLUG): A CPU WHOSE L2 PARTNER
	// IS OFFLINE HAS NO L2 STEAL PATH AND ITS PEERS CAN'T REACH ITS WORK.
	// WALKS affinity_rank (R_EFF-ORDERED) WITH THE SAME ONLINE-BUDGET
	// PATTERN AS find_idle_by_affinity -- OFFLINE ENTRIES SKIPPED FREE,
	// UP TO AFFINITY_SEARCH_ONLINE ONLINE CANDIDATES TESTED.
	if (!stolen) {
		u32 rbase = my_cpu * MAX_AFFINITY_CANDIDATES;
		u32 rchecked = 0;
		for (int i = 0; i < MAX_AFFINITY_CANDIDATES; i++) {
			u32 key = rbase + (u32)i;
			u32 *val = bpf_map_lookup_elem(&affinity_rank, &key);
			if (!val || *val == (u32)-1)
				break;
			u32 peer = *val;
			if (peer >= nr_cpu_ids)
				continue;
			if (peer == my_cpu) {
				if (++rchecked >= AFFINITY_SEARCH_ONLINE)
					break;
				continue;
			}
			if (scx_bpf_dsq_move_to_local((u64)peer)) {
				update_pcpu_sojourn(peer, now);
				pcpu_drain_clear(peer);
				__sync_fetch_and_add(&interactive_run, 1);
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				if (sojourn_gate_pass(now))
					return;
				break;
			}
			if (++rchecked >= AFFINITY_SEARCH_ONLINE)
				break;
		}
	}

	struct tuning_knobs *knobs = get_knobs();
	u64 sojourn_thresh = knobs ? knobs->sojourn_thresh_ns : 5000000;
	u64 oldest = batch_enqueue_ns;
	bool batch_starving = oldest > 0 && (now - oldest) > sojourn_thresh;
	u64 effective_budget = longrun_mode ? nr_cpu_ids : interactive_budget;

	// HARD STARVATION RESCUE: ABSOLUTE SAFETY NET FOR BOTH TIERS
	// FIRES BEFORE THE DEFICIT GATE SO IT CAN NEVER BE SUPPRESSED.
	// GUARANTEES NO TASK IN ANY OVERFLOW DSQ SITS LONGER THAN 500MS.
	{
		u64 int_age = interactive_enqueue_ns;
		if (int_age > 0 && (now - int_age) > starvation_rescue_ns) {
			if (scx_bpf_dsq_move_to_local(node_dsq)) {
				if (scx_bpf_dsq_nr_queued(node_dsq) == 0) {
					u64 old_iens = interactive_enqueue_ns;
					if (old_iens > 0)
						__sync_val_compare_and_swap(
							&interactive_enqueue_ns,
							old_iens, 0);
				} else {
					interactive_enqueue_ns =
						bpf_ktime_get_ns();
				}
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				return;
			}
		}
		if (oldest > 0 && (now - oldest) > starvation_rescue_ns) {
			if (scx_bpf_dsq_move_to_local(batch_dsq)) {
				if (scx_bpf_dsq_nr_queued(batch_dsq) == 0) {
					u64 old_bens = batch_enqueue_ns;
					if (old_bens > 0)
						__sync_val_compare_and_swap(
							&batch_enqueue_ns,
							old_bens, 0);
				} else {
					batch_enqueue_ns =
						bpf_ktime_get_ns();
				}
				__sync_lock_test_and_set(&interactive_run, 0);
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				return;
			}
		}
	}

	// DEFICIT GATE: WHEN INTERACTIVE HAS EXCEEDED ITS BUDGET AND BATCH
	// IS STARVING, SKIP INTERACTIVE OVERFLOW RESCUE SO BATCH
	// GETS SERVED VIA DEFICIT CHECK OR STARVATION RESCUE INSTEAD.
	// EXCEPTION: IF INTERACTIVE TASKS HAVE BEEN WAITING PAST THE
	// OVERFLOW SOJOURN THRESHOLD, RESCUE IS MORE URGENT THAN DEFICIT.
	if (interactive_run >= effective_budget && batch_starving) {
		u64 ie_gate = interactive_enqueue_ns;
		if (ie_gate == 0 || (now - ie_gate) <= overflow_sojourn_rescue_ns)
			goto skip_interactive_rescue;
	}

	// STEP 2: OVERFLOW SOJOURN AMPLIFICATION
	// WHEN OVERFLOW DSQs HAVE TASKS AGING PAST 10MS, SERVE THEM.
	u64 int_oldest = interactive_enqueue_ns;
	if (int_oldest > 0 &&
	    (now - int_oldest) > overflow_sojourn_rescue_ns) {
		if (scx_bpf_dsq_move_to_local(node_dsq)) {
			if (scx_bpf_dsq_nr_queued(node_dsq) == 0) {
				u64 old_iens = interactive_enqueue_ns;
				if (old_iens > 0)
					__sync_val_compare_and_swap(&interactive_enqueue_ns, old_iens, 0);
			} else {
				interactive_enqueue_ns = bpf_ktime_get_ns();
			}
			__sync_fetch_and_add(&interactive_run, 1);
			s = get_stats();
			if (s) {
				s->nr_dispatches += 1;
				s->nr_overflow_rescue += 1;
			}
			__sync_fetch_and_add(&global_rescue_count, 1);
			return;
		}
		u64 old_iens = interactive_enqueue_ns;
		if (old_iens > 0)
			__sync_val_compare_and_swap(&interactive_enqueue_ns, old_iens, 0);
	}

skip_interactive_rescue:;

	// BATCH OVERFLOW RESCUE
	u64 bat_oldest = batch_enqueue_ns;
	if (bat_oldest > 0 &&
	    (now - bat_oldest) > overflow_sojourn_rescue_ns) {
		if (scx_bpf_dsq_move_to_local(batch_dsq)) {
			if (scx_bpf_dsq_nr_queued(batch_dsq) == 0) {
				u64 old_bens = batch_enqueue_ns;
				if (old_bens > 0)
					__sync_val_compare_and_swap(&batch_enqueue_ns, old_bens, 0);
			} else {
				batch_enqueue_ns = bpf_ktime_get_ns();
			}
			__sync_lock_test_and_set(&interactive_run, 0);
			s = get_stats();
			if (s) {
				s->nr_dispatches += 1;
				s->nr_overflow_rescue += 1;
			}
			__sync_fetch_and_add(&global_rescue_count, 1);
			return;
		}
		u64 old_bens = batch_enqueue_ns;
		if (old_bens > 0)
			__sync_val_compare_and_swap(&batch_enqueue_ns, old_bens, 0);
	}

	// DEFICIT COUNTER: ANTI-STARVATION INTERLEAVE (DRR)
	// AFTER interactive_budget DISPATCHES WITHOUT BATCH SERVICE,
	// FORCE ONE BATCH DISPATCH WHEN BATCH IS STARVING.
	// PROPORTIONAL: BUDGET = nr_cpu_ids * 4 (SET IN init()).
	// LONGRUN OVERRIDE: WHEN SUSTAINED BATCH PRESSURE (>2S), TIGHTEN
	// FROM nr_cpu_ids*4 TO nr_cpu_ids*1, QUADRUPLING BATCH SHARE.
	if (interactive_run >= effective_budget && batch_starving) {
		if (scx_bpf_dsq_move_to_local(batch_dsq)) {
			if (scx_bpf_dsq_nr_queued(batch_dsq) == 0) {
				u64 old_bens = batch_enqueue_ns;
				if (old_bens > 0)
					__sync_val_compare_and_swap(&batch_enqueue_ns, old_bens, 0);
			} else {
				batch_enqueue_ns = bpf_ktime_get_ns();
			}
			__sync_lock_test_and_set(&interactive_run, 0);
			s = get_stats();
			if (s)
				s->nr_dispatches += 1;
			return;
		}
		__sync_lock_test_and_set(&interactive_run, 0);
	}

	// NODE INTERACTIVE OVERFLOW: LATCRIT + INTERACTIVE TASKS
	// INTERACTIVE FIRST WITHIN EACH BUDGET CYCLE. NO PRIORITY INVERSION.
	if (scx_bpf_dsq_move_to_local(node_dsq)) {
		if (scx_bpf_dsq_nr_queued(node_dsq) == 0) {
			u64 old_iens = interactive_enqueue_ns;
			if (old_iens > 0)
				__sync_val_compare_and_swap(&interactive_enqueue_ns, old_iens, 0);
		} else {
			interactive_enqueue_ns = bpf_ktime_get_ns();
		}
		__sync_fetch_and_add(&interactive_run, 1);
		s = get_stats();
		if (s)
			s->nr_dispatches += 1;
		return;
	}

	// BATCH SOJOURN RESCUE: CODEL-INSPIRED STARVATION SAFETY NET.
	// FIRES WHEN INTERACTIVE OVERFLOW IS EMPTY AND BATCH IS STARVING.
	// THRESHOLD SET BY RUST ADAPTIVE LAYER FROM OBSERVED DISPATCH RATE.
	if (batch_starving) {
		if (scx_bpf_dsq_move_to_local(batch_dsq)) {
			if (scx_bpf_dsq_nr_queued(batch_dsq) == 0) {
				u64 old_bens = batch_enqueue_ns;
				if (old_bens > 0)
					__sync_val_compare_and_swap(&batch_enqueue_ns, old_bens, 0);
			} else {
				batch_enqueue_ns = bpf_ktime_get_ns();
			}
			s = get_stats();
			if (s)
				s->nr_dispatches += 1;
			return;
		}
	}

	// NODE BATCH OVERFLOW: NORMAL FALLBACK FOR BATCH TASKS
	if (scx_bpf_dsq_move_to_local(batch_dsq)) {
		if (scx_bpf_dsq_nr_queued(batch_dsq) == 0) {
			u64 old_bens = batch_enqueue_ns;
			if (old_bens > 0)
				__sync_val_compare_and_swap(&batch_enqueue_ns, old_bens, 0);
		} else {
			batch_enqueue_ns = bpf_ktime_get_ns();
		}
		s = get_stats();
		if (s)
			s->nr_dispatches += 1;
		return;
	}

	// CROSS-NODE STEAL
	for (u32 n = 0; n < nr_nodes && n < MAX_NODES; n++) {
		if (n != (u32)node) {
			if (scx_bpf_dsq_move_to_local(nr_cpu_ids + (u64)n)) {
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				return;
			}
			if (scx_bpf_dsq_move_to_local(nr_cpu_ids + nr_nodes + (u64)n)) {
				s = get_stats();
				if (s)
					s->nr_dispatches += 1;
				return;
			}
		}
	}

	// NOTHING IN ANY DSQ -- KEEP PREV RUNNING IF POSSIBLE
	if (prev && !(prev->flags & PF_EXITING) &&
	    (prev->scx.flags & SCX_TASK_QUEUED)) {
		struct task_ctx *tctx = lookup_task_ctx(prev);
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
					 tctx->avg_runtime, tctx->runtime_dev);
	struct tuning_knobs *knobs = get_knobs();
	u32 new_tier = classify_tier(tctx->lat_cri, knobs);

	// COMPOSITOR BOOST: ALWAYS LAT_CRITICAL
	if (new_tier != TIER_LAT_CRITICAL && is_compositor(p))
		new_tier = TIER_LAT_CRITICAL;

	// KWORKER FLOOR: WORKQUEUE WORKERS HANDLE I/O COMPLETIONS, TIMER
	// CALLBACKS, AND DEFERRED INTERRUPT WORK. USERSPACE BLOCKS ON THESE.
	// THEIR LOW EWMA SCORES (INFREQUENT WAKEUPS, LONG RUNTIMES) PUSH
	// THEM TO BATCH, BUT THEY ARE LATENCY-CRITICAL KERNEL INFRASTRUCTURE.
	if (new_tier == TIER_BATCH && (p->flags & PF_WQ_WORKER))
		new_tier = TIER_INTERACTIVE;

	tctx->tier = new_tier;
}

// RUNNING: TASK STARTS EXECUTING -- ADVANCE VTIME, RECORD WAKE LATENCY
void BPF_STRUCT_OPS(pandemonium_running, struct task_struct *p)
{
#if TRACE_SCHED
	if (is_sched_task(p))
		bpf_printk("PAND: running pid=%d cpu=%d", p->pid, bpf_get_smp_processor_id());
#endif
	u64 cur = vtime_now;
	for (int i = 0; i < 4; i++) {
		if (!time_before(cur, p->scx.dsq_vtime))
			break;
		if (__sync_bool_compare_and_swap(&vtime_now, cur, p->scx.dsq_vtime))
			break;
		cur = vtime_now;
	}

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

		// SLEEP DURATION: TIME BETWEEN quiescent() AND runnable()
		u64 sleep_dur = 0;
		if (tctx->sleep_start_ns > 0 &&
		    tctx->last_woke_at > tctx->sleep_start_ns) {
			sleep_dur = tctx->last_woke_at - tctx->sleep_start_ns;
			tctx->sleep_start_ns = 0;
		}

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

		// HISTOGRAM: BPF-SIDE LATENCY BUCKETING (NO RING BUFFER)
		u32 tier_idx = (u32)tctx->tier;
		if (tier_idx > 2) tier_idx = 2;
		u32 bucket = lat_bucket(wake_lat);
		u32 hist_key = tier_idx * 12 + bucket;
		u64 *hist_val = bpf_map_lookup_elem(&wake_lat_hist, &hist_key);
		if (hist_val)
			*hist_val += 1;

		if (sleep_dur > 0) {
			u32 sbucket = sleep_bucket(sleep_dur);
			u64 *sval = bpf_map_lookup_elem(&sleep_hist, &sbucket);
			if (sval)
				*sval += 1;
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
	tctx->last_cpu = bpf_get_smp_processor_id();
	u64 weight = tctx->cached_weight;

	u64 now = bpf_ktime_get_ns();
	u64 slice = now > tctx->last_run_at ? now - tctx->last_run_at : 0;
	{
		u64 avg = tctx->avg_runtime;
		u64 diff = slice > avg ? slice - avg : avg - slice;
		tctx->avg_runtime = calc_avg(avg, slice, tctx->ewma_age);
		tctx->runtime_dev = calc_avg(tctx->runtime_dev, diff,
					      tctx->ewma_age);
	}

	// PROCDB: PUBLISH TASK CLASSIFICATION FOR USERSPACE
	// INITIAL AT EWMA MATURITY, THEN EVERY 64 SCHEDULING EVENTS
	// RE-PUBLISHING KEEPS PROCDB FRESH FOR LONG-LIVED TASKS
	if (tctx->ewma_age == EWMA_AGE_MATURE ||
	    (tctx->ewma_age > EWMA_AGE_MATURE && tctx->ewma_age % 64 == 0)) {
		struct task_class_entry obs = {};
		obs.tier = (u8)tctx->tier;
		obs.avg_runtime = tctx->avg_runtime;
		obs.runtime_dev = tctx->runtime_dev;
		obs.wakeup_freq = tctx->wakeup_freq;
		obs.csw_rate = tctx->csw_rate;
		char key[16];
		__builtin_memcpy(key, p->comm, 16);
		bpf_map_update_elem(&task_class_observe, key, &obs, BPF_ANY);
	}

	u64 delta_vtime;
	if (weight > 0)
		delta_vtime = (slice << 7) / weight;
	else
		delta_vtime = slice;

	p->scx.dsq_vtime += delta_vtime;
	tctx->awake_vtime += delta_vtime;
}

// TICK: SOJOURN ENFORCEMENT + EVENT-DRIVEN BATCH PREEMPTION
// FIRES ON EVERY KERNEL SCHEDULER TICK (HZ-DEPENDENT, 1-4MS) REGARDLESS
// OF SLICE LENGTH. TWO RESPONSIBILITIES:
// 1. SOJOURN: WRITE BATCH WAIT AGE TO STATS FOR RUST ADAPTIVE LAYER.
//    IF BATCH STARVING PAST THRESHOLD AND CURRENT TASK IS BATCH, KICK
//    CPU TO FORCE DISPATCH. THRESHOLD SET BY RUST FROM DISPATCH RATE.
// 2. PREEMPTION: WHEN INTERACTIVE IS WAITING AND BATCH HAS RUN PAST
//    THRESHOLD, PREEMPT TO MAINTAIN INTERACTIVE RESPONSIVENESS.
void BPF_STRUCT_OPS(pandemonium_tick, struct task_struct *p)
{
	// SOJOURN: COMPUTE BATCH WAIT AGE AND WRITE TO STATS FOR RUST
	struct pandemonium_stats *s = get_stats();
	struct tuning_knobs *knobs = get_knobs();

	// THE OSCILLATOR IS THE ONE DETECTOR. RESCUE DELTAS ARE THE ONLY SIGNAL
	// IT CONSUMES; IT ADAPTS codel_target_ns WHICH DRIVES STEP -1 RESCUE +
	// HARD STARVATION RESCUE. NO SEPARATE BURST DETECTOR. NO THRESHOLD FLAGS.
	if (bpf_get_smp_processor_id() == 0) {
		// TAU-SCALING: re-derive the timing statics if Rust wrote a new
		// topology_tau_ns (initial detect or hotplug). Idempotent when
		// tau is unchanged; cheap when it is (single compare + early out).
		apply_tau_scaling(knobs ? knobs->topology_tau_ns : 0);

		// DAMPED HARMONIC OSCILLATION: CORE-SCALED PULL + DAMPING
		// 2C: HEAVY DAMPING, WEAK PULL -> CENTER BARELY MOVES
		// 12C: LIGHT DAMPING, STRONG PULL -> CENTER TRACKS STALL POINT
		{
			u64 cur = __sync_fetch_and_add(&global_rescue_count, 0);
			u64 delta = cur - prev_rescue_snapshot;
			prev_rescue_snapshot = cur;

			s64 impulse;
			if (delta > 0) {
				u64 capped = delta > 8 ? 8 : delta;
				impulse = -((s64)(capped * OSCILLATOR_PULL_NS *
					oscillator_pull_scale));
			} else {
				impulse = (s64)OSCILLATOR_RELAX_NS;
			}

			oscillator_velocity_ns += impulse;
			oscillator_velocity_ns -= oscillator_velocity_ns >>
				oscillator_damping_shift;

			if (oscillator_velocity_ns > oscillator_velocity_cap)
				oscillator_velocity_ns = oscillator_velocity_cap;
			if (oscillator_velocity_ns < -oscillator_velocity_cap)
				oscillator_velocity_ns = -oscillator_velocity_cap;

			// MODULATE THE CODEL TARGET (WHAT COUNTS AS "ABOVE NORMAL")
			// RESCUES -> PULL TARGET DOWN (TIGHTEN: DETECT STALLS SOONER)
			// QUIET -> PUSH TARGET UP (RELAX: TOLERATE HIGHER SOJOURN)
			// THE TARGET ADAPTS TO WHAT "NORMAL" IS ON THIS SYSTEM.
			s64 nc = (s64)codel_target_ns + oscillator_velocity_ns;
			if (nc < (s64)codel_target_floor_ns)
				nc = (s64)codel_target_floor_ns;
			if (nc > (s64)codel_target_max_ns)
				nc = (s64)codel_target_max_ns;
			codel_target_ns = (u64)nc;
		}
	}

	if (s) {
		s->longrun_mode_active = longrun_mode ? 1 : 0;
	}

	u64 bens = batch_enqueue_ns;
	if (bens > 0) {
		u64 now = bpf_ktime_get_ns();
		u64 sojourn = now - bens;
		if (s)
			s->batch_sojourn_ns = sojourn;

		// LONGRUN DETECTION: SUSTAINED BATCH PRESSURE
		// BATCH DSQ NON-EMPTY FOR > 2S SETS longrun_mode, WHICH
		// TIGHTENS THE DEFICIT RATIO IN dispatch() FROM nr_cpu_ids*4
		// TO nr_cpu_ids*1 (QUADRUPLING BATCH'S DISPATCH SHARE).
		longrun_mode = sojourn > longrun_thresh_ns;

		// SOJOURN ENFORCEMENT: THRESHOLD SET BY RUST ADAPTIVE LAYER
		// FROM OBSERVED DISPATCH RATE. IF BATCH STARVING PAST THRESHOLD
		// AND CURRENT TASK IS BATCH, KICK THIS CPU TO FORCE DISPATCH.
		// ONLY PREEMPT BATCH: INTERACTIVE/LATCRIT SLICES ARE ALREADY
		// SHORT (CAPPED AT slice_ns) AND WILL YIELD QUICKLY ON THEIR OWN.
		u64 sojourn_thresh = knobs ? knobs->sojourn_thresh_ns : 5000000;
		if (sojourn > sojourn_thresh) {
			struct task_ctx *tctx = lookup_task_ctx(p);
			if (tctx && tctx->tier == TIER_BATCH) {
				scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
				return;
			}
		}
	} else {
		longrun_mode = false;
		if (s)
			s->batch_sojourn_ns = 0;
	}

	// PER-CPU DSQ SOJOURN: CHECK OWN DSQ + ROTATING GLOBAL SCAN.
	// LOCAL CHECK: CATCHES STALE TASKS ON THIS CPU.
	// GLOBAL SCAN: CATCHES STALE TASKS ON IDLE CPUS WHERE tick() NEVER
	// FIRES. ROTATES 4 CPUS PER TICK SO ALL CPUS GET COVERED OVER TIME.
	{
		u32 this_cpu = bpf_get_smp_processor_id();
		u64 now2 = bpf_ktime_get_ns();
		u64 pcpu_sojourn_thresh = knobs
			? knobs->sojourn_thresh_ns : 5000000;

		// LOCAL: OWN PER-CPU DSQ
		if (this_cpu < MAX_CPUS) {
			u64 pcpu_oldest = pcpu_enqueue_ns[this_cpu];
			if (pcpu_oldest > 0 &&
			    (now2 - pcpu_oldest) > pcpu_sojourn_thresh) {
				scx_bpf_kick_cpu(this_cpu,
						 SCX_KICK_PREEMPT);
				return;
			}
		}

		// GLOBAL: ROTATING SCAN OF REMOTE PER-CPU DSQs
		// SHIFT BY 20 (~1ms ROTATION) TO AVOID u64 DIVISION.
		// CONSTANT MODULO (MAX_CPUS) + MASK FOR VERIFIER SAFETY.
		u32 scan_base = (u32)(now2 >> 20);
		for (int i = 0; i < 4; i++) {
			u32 scan_cpu = (scan_base + (u32)i) &
				       (MAX_CPUS - 1);
			if (scan_cpu == this_cpu)
				continue;
			if (scan_cpu >= nr_cpu_ids)
				continue;
			u64 remote_stamp =
				pcpu_enqueue_ns[scan_cpu & (MAX_CPUS - 1)];
			if (remote_stamp > 0 &&
			    (now2 - remote_stamp) > pcpu_sojourn_thresh)
				scx_bpf_kick_cpu(scan_cpu,
						 SCX_KICK_PREEMPT);
		}
	}

	if (!interactive_waiting)
		return;

	struct task_ctx *tctx = lookup_task_ctx(p);
	if (!tctx)
		return;

	// CORE-SCALED LONGRUN PROTECTION: 2C IS THIN ENOUGH THAT BATCH LONG-RUNNERS
	// NEED EXTRA SLICE HEADROOM AGAINST LAT_CRIT/BATCH CONTENTION ON TWO CPUs.
	// 4C+ HAS ENOUGH CAPACITY TO HANDLE BOTH TIERS AT BASELINE PREEMPT.
	u64 base_thresh = knobs ? knobs->preempt_thresh_ns : 1000000;
	u32 longrun_mult_shift = nr_cpu_ids <= 2 ? 2 : 0;
	u64 thresh = longrun_mode ? (base_thresh << longrun_mult_shift)
	           : base_thresh;

	// LAT_CRITICAL WAITING -> TIGHTEN THRESHOLD BY 4X. AUDIO AND COMPOSITOR
	// WAKERS ARE THE HOT CASES; THE STANDARD 1MS WAIT IS ENOUGH TO SKIP A
	// 10MS AUDIO BUFFER. INTERACTIVE WAITERS KEEP THE CURRENT THRESHOLD SO
	// BATCH THROUGHPUT IS NOT PENALIZED BY ORDINARY WAKEUP PATTERNS.
	if (latcrit_waiting)
		thresh >>= 2;

	u64 on_cpu = tctx->last_run_at > 0
		? bpf_ktime_get_ns() - tctx->last_run_at : 0;
	if (tctx->tier == TIER_BATCH && on_cpu >= thresh) {
		scx_bpf_kick_cpu(scx_bpf_task_cpu(p), SCX_KICK_PREEMPT);
		interactive_waiting = false;
		latcrit_waiting = false;
		if (!s)
			s = get_stats();
		if (s)
			s->nr_preempt += 1;
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

		// PROCDB: APPLY LEARNED CLASSIFICATION FROM PRIOR RUNS
		char key[16];
		__builtin_memcpy(key, p->comm, 16);
		struct task_class_entry *init_entry =
		    bpf_map_lookup_elem(&task_class_init, key);
		if (init_entry) {
			tctx->tier = (u32)init_entry->tier;
			tctx->avg_runtime = init_entry->avg_runtime;
			tctx->runtime_dev = init_entry->runtime_dev;
			tctx->wakeup_freq = init_entry->wakeup_freq;
			tctx->csw_rate = init_entry->csw_rate;
			tctx->cached_weight = effective_weight(p, tctx);
			struct pandemonium_stats *s = get_stats();
			if (s)
				s->nr_procdb_hits += 1;
		}
	}
}

// INIT: DETECT TOPOLOGY, CREATE DSQs, CALIBRATE
s32 BPF_STRUCT_OPS_SLEEPABLE(pandemonium_init)
{
	u32 zero = 0;

	nr_nodes = __COMPAT_scx_bpf_nr_node_ids();
	if (nr_nodes < 1)
		nr_nodes = 1;
	if (nr_nodes > nr_cpu_ids)
		nr_nodes = nr_cpu_ids;

	// PER-CPU DSQs: USED BY SELECT_CPU ONLY (v5.4.8)
	// SELECT_CPU DISPATCHES TO PER-CPU DSQ (CACHE-HOT, VISIBLE, STEALABLE).
	// ENQUEUE ALWAYS USES SHARED NODE DSQ (EVEN DISTRIBUTION).
	// VISIBILITY LAYERS:
	//   1. L2 WORK STEALING IN DISPATCH -- IDLE CPUs PULL FROM SIBLINGS
	//   2. ROTATING TICK SCAN -- CATCHES STALE TASKS ON IDLE CPUs
	//   3. PER-CPU SOJOURN RESCUE -- THRESHOLD CEILING ON INVISIBILITY
	for (u32 i = 0; i < nr_cpu_ids && i < MAX_CPUS; i++)
		scx_bpf_create_dsq(i, -1);

	// CREATE PER-NODE INTERACTIVE OVERFLOW DSQs (DSQ ID = nr_cpu_ids + NODE)
	for (u32 i = 0; i < nr_nodes && i < MAX_NODES; i++)
		scx_bpf_create_dsq(nr_cpu_ids + i, (s32)i);

	// CREATE PER-NODE BATCH OVERFLOW DSQs (DSQ ID = nr_cpu_ids + nr_nodes + NODE)
	for (u32 i = 0; i < nr_nodes && i < MAX_NODES; i++)
		scx_bpf_create_dsq(nr_cpu_ids + nr_nodes + i, (s32)i);

	// ANTI-STARVATION BUDGET: SCALE RATIO WITH CORE COUNT
	// 2C: RATIO=3 (BUDGET=6), 4C+: RATIO=4 (SAME AS BEFORE)
	{
		u64 ratio = 2 + (nr_cpu_ids >> 1);
		if (ratio > 4) ratio = 4;
		interactive_budget = nr_cpu_ids * ratio;
		if (interactive_budget < 2) interactive_budget = 2;
	}

	// PER-CPU DSQ DEPTH GATE: 1 BELOW 4 CPUS, 2 AT 4+
	pcpu_depth_base = (nr_cpu_ids < 4) ? 1 : 2;

	// ALL TIMING-CONSTANT AND OSCILLATOR-DYNAMICS STATICS BELOW ARE DERIVED
	// FROM tau (Fiedler-based time constant) VIA apply_tau_scaling() AT THE
	// FIRST CPU-0 TICK. MIDPOINT CONSTANTS HERE PROVIDE SANE BEHAVIOR DURING
	// THE ~1MS WINDOW BETWEEN struct_ops ATTACH AND THAT FIRST TICK. THEY
	// ARE OVERWRITTEN IMMEDIATELY -- DON'T READ SIGNIFICANCE INTO THEM.
	starvation_rescue_ns       = 100000000ULL;  // 100ms midpoint of [20, 500]
	overflow_sojourn_rescue_ns =   6000000ULL;  //   6ms midpoint of [4, 10]
	sojourn_interval_ns        =   4000000ULL;  //   4ms midpoint of [2, 12]
	codel_target_floor_ns      =    500000ULL;  // 500us midpoint of [200, 800]
	oscillator_damping_shift   = 3;
	oscillator_pull_scale      = 3;
	oscillator_velocity_cap    = (s64)((u64)OSC_VELOCITY_CAP_PER_PULL * 3);
	// START PERMISSIVE. LET THE DAMPED OSCILLATION FIND THE RIGHT CENTER.
	// RESCUES PULL IT DOWN. NO STATIC FORMULA. THE WAVE FUNCTION DOES THE WORK.
	codel_target_ns = codel_target_max_ns;
	oscillator_velocity_ns = 0;
	prev_rescue_snapshot = 0;
	global_rescue_count = 0;
	for (u32 i = 0; i < nr_cpu_ids && i < MAX_CPUS; i++) {
		pcpu_min_sojourn_ns[i] = ~0ULL;
		pcpu_stall_start_ns[i] = 0;
	}

	longrun_mode = false;

	// INITIALIZE DEFAULT TUNING KNOBS
	struct tuning_knobs *knobs = bpf_map_lookup_elem(&tuning_knobs_map, &zero);
	if (knobs) {
		knobs->slice_ns = 1000000;
		knobs->preempt_thresh_ns = 1000000;
		knobs->lag_scale = 4;
		knobs->batch_slice_ns = 20000000;        // 20MS FLAT DEFAULT
		knobs->cpu_bound_thresh_ns = 2500000;    // 2.5MS (RESERVED FOR FUTURE USE)
		knobs->lat_cri_thresh_high = LAT_CRI_THRESH_HIGH; // 32
		knobs->lat_cri_thresh_low  = LAT_CRI_THRESH_LOW;  // 8
		knobs->affinity_mode = 0;                // OFF BY DEFAULT (RUST SETS PER REGIME)
		knobs->sojourn_thresh_ns = 5000000;      // 5MS DEFAULT (RUST OVERRIDES)
		knobs->burst_slice_ns = 1000000;         // 1MS DEFAULT (BURST/LONGRUN CEILING)
	}

	return 0;
}

// EXIT: RECORD EXIT INFO FOR USERSPACE
void BPF_STRUCT_OPS(pandemonium_exit, struct scx_exit_info *ei)
{
	UEI_RECORD(uei, ei);
}

// QUIESCENT: TASK GOES TO SLEEP -- RECORD TIMESTAMP FOR SLEEP ANALYSIS
void BPF_STRUCT_OPS(pandemonium_quiescent, struct task_struct *p,
		    u64 deq_flags)
{
	struct task_ctx *tctx = lookup_task_ctx(p);
	if (tctx)
		tctx->sleep_start_ns = bpf_ktime_get_ns();
}

// CPU RELEASE: RESCUE STRANDED TASKS WHEN RT/DL PREEMPTS OUR CPU
// CALLED WHEN THE KERNEL TAKES A CPU AWAY FROM SCHED_EXT (DL SERVER,
// RT TASKS, PIPEWIRE). WITHOUT THIS, TASKS THAT dispatch() MOVED TO THE
// LOCAL DSQ VIA scx_bpf_dsq_move_to_local() GET STUCK, TRIGGERING THE
// WATCHDOG. EVERY REFERENCE SCHEDULER IMPLEMENTS THIS.
void BPF_STRUCT_OPS(pandemonium_cpu_release, s32 cpu,
		    struct scx_cpu_release_args *args)
{
	scx_bpf_reenqueue_local();
}

// CPU HOTPLUG CALLBACKS
// SUSPEND/RESUME: KERNEL PM CALLS scx_bypass(true) BEFORE SUSPEND,
// DEQUEUES ALL TASKS FROM BPF DSQs. CPUs GO OFFLINE ONE BY ONE.
// ON RESUME, CPUs COME BACK, scx_bypass(false), BPF TAKES OVER.
// STALE TIMESTAMPS AND COUNTERS FROM PRE-SUSPEND CAUSE THE DISPATCH
// WATERFALL TO MALFUNCTION FOR 30-40s POST-RESUME, STARVING
// LATENCY-CRITICAL TASKS UNTIL THE WATCHDOG KILLS THE SCHEDULER.
// FIX: CLEAR PER-CPU AND GLOBAL STATE ON HOTPLUG TRANSITIONS.

void BPF_STRUCT_OPS(pandemonium_cpu_online, s32 cpu)
{
	if ((u32)cpu < MAX_CPUS) {
		__sync_lock_test_and_set(&pcpu_enqueue_ns[cpu], 0);
		pcpu_min_sojourn_ns[cpu] = ~0ULL;
		pcpu_stall_start_ns[cpu] = 0;
	}
	// Force the next CPU-0 tick to re-derive tau-scaled statics. Rust will
	// have recomputed lambda_2 against the new topology and written a fresh
	// topology_tau_ns; clearing the snapshot makes apply_tau_scaling() pick
	// it up instead of short-circuiting on the stale value.
	last_tau_snapshot = 0;
}

void BPF_STRUCT_OPS(pandemonium_cpu_offline, s32 cpu)
{
	if ((u32)cpu < MAX_CPUS) {
		__sync_lock_test_and_set(&pcpu_enqueue_ns[cpu], 0);
		pcpu_min_sojourn_ns[cpu] = ~0ULL;
		pcpu_stall_start_ns[cpu] = 0;
	}
	last_tau_snapshot = 0;

	__sync_lock_test_and_set(&interactive_enqueue_ns, 0);
	__sync_lock_test_and_set(&batch_enqueue_ns, 0);
	__sync_lock_test_and_set(&interactive_run, 0);

	// RESET OSCILLATOR FEEDBACK TO AVOID STALE DELTA POST-SUSPEND
	__sync_lock_test_and_set(&global_rescue_count, 0);
	prev_rescue_snapshot = 0;
	oscillator_velocity_ns = 0;
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
	       .quiescent    = (void *)pandemonium_quiescent,
	       .cpu_release  = (void *)pandemonium_cpu_release,
	       .cpu_online   = (void *)pandemonium_cpu_online,
	       .cpu_offline  = (void *)pandemonium_cpu_offline,
	       .init         = (void *)pandemonium_init,
	       .exit         = (void *)pandemonium_exit,
	       .flags        = SCX_OPS_BUILTIN_IDLE_PER_NODE,
	       .name         = "pandemonium");