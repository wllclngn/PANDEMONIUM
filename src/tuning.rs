// PANDEMONIUM TUNING TYPES
// PURE-RUST MODULE: ZERO BPF DEPENDENCIES
// SHARED BETWEEN BINARY CRATE (scheduler.rs, adaptive.rs) AND LIB CRATE (tests)

// --- REGIME THRESHOLDS (SCHMITT TRIGGER) ---
// DIRECTIONAL HYSTERESIS PREVENTS OSCILLATION AT REGIME BOUNDARIES.
// WIDE DEAD ZONES: MUST CLEARLY ENTER A REGIME AND CLEARLY LEAVE IT.

pub const HEAVY_ENTER_PCT: u64 = 10;   // ENTER HEAVY: IDLE < 10%
pub const HEAVY_EXIT_PCT: u64  = 25;   // LEAVE HEAVY: IDLE > 25%
pub const LIGHT_ENTER_PCT: u64 = 50;   // ENTER LIGHT: IDLE > 50%
pub const LIGHT_EXIT_PCT: u64  = 30;   // LEAVE LIGHT: IDLE < 30%

// --- REGIME PROFILES ---
// PREEMPT_THRESH CONTROLS WHEN TICK PREEMPTS BATCH TASKS (IF INTERACTIVE WAITING).
// BATCH_SLICE_NS CONTROLS MAX UNINTERRUPTED BATCH RUN WHEN NO INTERACTIVE WAITING.
// CPU_BOUND_THRESH_NS CONTROLS DEMOTION THRESHOLD PER REGIME (FEATURE 5).

const LIGHT_SLICE_NS: u64     = 2_000_000;   // 2MS
const LIGHT_PREEMPT_NS: u64   = 1_000_000;   // 1MS: AGGRESSIVE
const LIGHT_LAG_SCALE: u64    = 6;
const LIGHT_BATCH_NS: u64     = 20_000_000;  // 20MS: NO CONTENTION, LET BATCH RIP

const MIXED_SLICE_NS: u64     = 1_000_000;   // 1MS: TIGHT INTERACTIVE CONTROL
const MIXED_PREEMPT_NS: u64   = 1_000_000;   // 1MS: MATCH FOR CLEAN ENFORCEMENT
const MIXED_LAG_SCALE: u64    = 4;
const MIXED_BATCH_NS: u64     = 20_000_000;  // 20MS: MATCHES LIGHT/HEAVY/BPF DEFAULT

const HEAVY_SLICE_NS: u64     = 4_000_000;   // 4MS: WIDER FOR THROUGHPUT
const HEAVY_PREEMPT_NS: u64   = 2_000_000;   // 2MS: SLIGHTLY RELAXED
const HEAVY_LAG_SCALE: u64    = 2;
const HEAVY_BATCH_NS: u64     = 20_000_000;  // 20MS: LET BATCH RIP

// --- P99 CEILINGS ---

const LIGHT_P99_CEIL_NS: u64  = 3_000_000;   // 3MS
const MIXED_P99_CEIL_NS: u64  = 5_000_000;   // 5MS: BELOW 16MS FRAME BUDGET
const HEAVY_P99_CEIL_NS: u64  = 10_000_000;  // 10MS: HEAVY LOAD, REALISTIC

// --- CPU-BOUND DEMOTION THRESHOLDS (FEATURE 5) ---
// PER-REGIME: LENIENT IN LIGHT, AGGRESSIVE IN HEAVY

pub const LIGHT_DEMOTION_NS: u64 = 3_500_000;  // 3.5MS: LENIENT, FEW CONTEND
pub const MIXED_DEMOTION_NS: u64 = 2_500_000;  // 2.5MS: CURRENT CPU_BOUND_THRESH_NS
pub const HEAVY_DEMOTION_NS: u64 = 2_000_000;  // 2.0MS: AGGRESSIVE

// --- ADAPTIVE SAMPLES_PER_CHECK (FEATURE 4) ---

pub const LIGHT_SAMPLES_PER_CHECK: u32 = 16;
pub const MIXED_SAMPLES_PER_CHECK: u32 = 32;
pub const HEAVY_SAMPLES_PER_CHECK: u32 = 64;

// --- CLASSIFIER THRESHOLDS (PHASE 4: POLISH) ---
// LAT_CRI SCORE BOUNDARIES FOR TIER CLASSIFICATION
// EXPOSED AS TUNING KNOBS FOR RUNTIME ADJUSTMENT

pub const DEFAULT_LAT_CRI_THRESH_HIGH: u64 = 32;  // >= THIS: LAT_CRITICAL
pub const DEFAULT_LAT_CRI_THRESH_LOW: u64  = 8;   // >= THIS: INTERACTIVE, BELOW: BATCH

// --- TUNING KNOBS ---
// MATCHES struct tuning_knobs IN BPF (intf.h)

#[repr(C)]
#[derive(Clone, Copy)]
pub struct TuningKnobs {
    pub slice_ns: u64,
    pub preempt_thresh_ns: u64,
    pub lag_scale: u64,
    pub batch_slice_ns: u64,
    pub cpu_bound_thresh_ns: u64,
    pub lat_cri_thresh_high: u64,
    pub lat_cri_thresh_low: u64,
}

impl Default for TuningKnobs {
    fn default() -> Self {
        Self {
            slice_ns: 1_000_000,
            preempt_thresh_ns: 1_000_000,
            lag_scale: 4,
            batch_slice_ns: 20_000_000,
            cpu_bound_thresh_ns: MIXED_DEMOTION_NS,
            lat_cri_thresh_high: DEFAULT_LAT_CRI_THRESH_HIGH,
            lat_cri_thresh_low: DEFAULT_LAT_CRI_THRESH_LOW,
        }
    }
}

// --- REGIME ---

#[repr(u8)]
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Regime {
    Light = 0,
    Mixed = 1,
    Heavy = 2,
}

impl Regime {
    pub fn from_u8(v: u8) -> Self {
        match v {
            0 => Self::Light,
            1 => Self::Mixed,
            _ => Self::Heavy,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Light => "LIGHT",
            Self::Mixed => "MIXED",
            Self::Heavy => "HEAVY",
        }
    }

    pub fn p99_ceiling(self) -> u64 {
        match self {
            Self::Light => LIGHT_P99_CEIL_NS,
            Self::Mixed => MIXED_P99_CEIL_NS,
            Self::Heavy => HEAVY_P99_CEIL_NS,
        }
    }
}

// --- REGIME KNOBS ---

pub fn regime_knobs(r: Regime) -> TuningKnobs {
    match r {
        Regime::Light => TuningKnobs {
            slice_ns: LIGHT_SLICE_NS,
            preempt_thresh_ns: LIGHT_PREEMPT_NS,
            lag_scale: LIGHT_LAG_SCALE,
            batch_slice_ns: LIGHT_BATCH_NS,
            cpu_bound_thresh_ns: LIGHT_DEMOTION_NS,
            lat_cri_thresh_high: DEFAULT_LAT_CRI_THRESH_HIGH,
            lat_cri_thresh_low: DEFAULT_LAT_CRI_THRESH_LOW,
        },
        Regime::Mixed => TuningKnobs {
            slice_ns: MIXED_SLICE_NS,
            preempt_thresh_ns: MIXED_PREEMPT_NS,
            lag_scale: MIXED_LAG_SCALE,
            batch_slice_ns: MIXED_BATCH_NS,
            cpu_bound_thresh_ns: MIXED_DEMOTION_NS,
            lat_cri_thresh_high: DEFAULT_LAT_CRI_THRESH_HIGH,
            lat_cri_thresh_low: DEFAULT_LAT_CRI_THRESH_LOW,
        },
        Regime::Heavy => TuningKnobs {
            slice_ns: HEAVY_SLICE_NS,
            preempt_thresh_ns: HEAVY_PREEMPT_NS,
            lag_scale: HEAVY_LAG_SCALE,
            batch_slice_ns: HEAVY_BATCH_NS,
            cpu_bound_thresh_ns: HEAVY_DEMOTION_NS,
            lat_cri_thresh_high: DEFAULT_LAT_CRI_THRESH_HIGH,
            lat_cri_thresh_low: DEFAULT_LAT_CRI_THRESH_LOW,
        },
    }
}

// --- REGIME DETECTION (SCHMITT TRIGGER) ---
// DIRECTION-AWARE: CURRENT REGIME DETERMINES WHICH THRESHOLDS APPLY.
// DEAD ZONES PREVENT OSCILLATION THAT SINGLE-BOUNDARY DETECTION CAUSED.

pub fn detect_regime(current: Regime, idle_pct: u64) -> Regime {
    match current {
        Regime::Light => {
            if idle_pct < LIGHT_EXIT_PCT {
                Regime::Mixed
            } else {
                Regime::Light
            }
        }
        Regime::Mixed => {
            if idle_pct > LIGHT_ENTER_PCT {
                Regime::Light
            } else if idle_pct < HEAVY_ENTER_PCT {
                Regime::Heavy
            } else {
                Regime::Mixed
            }
        }
        Regime::Heavy => {
            if idle_pct > HEAVY_EXIT_PCT {
                Regime::Mixed
            } else {
                Regime::Heavy
            }
        }
    }
}

// --- ADAPTIVE SAMPLES PER CHECK ---

pub fn samples_per_check_for_regime(r: Regime) -> u32 {
    match r {
        Regime::Light => LIGHT_SAMPLES_PER_CHECK,
        Regime::Mixed => MIXED_SAMPLES_PER_CHECK,
        Regime::Heavy => HEAVY_SAMPLES_PER_CHECK,
    }
}

// --- STABILITY MODE (PHASE 2.5) ---
// REFLEX THREAD HIBERNATION WHEN SYSTEM IS STABLE.
// REDUCES P99 COMPUTATION FROM ~1250/SEC TO ~312/SEC DURING STABLE GAMING.

pub const STABILITY_THRESHOLD: u32 = 10;    // CONSECUTIVE STABLE TICKS BEFORE HIBERNATE
pub const HIBERNATE_MULTIPLIER: u32 = 4;    // 4X SAMPLES_PER_CHECK WHEN STABLE

pub fn compute_stability_score(
    prev_score: u32,
    regime_changed: bool,
    guard_clamps: u64,
    reflex_events_delta: u64,
    p99_ns: u64,
    p99_ceiling_ns: u64,
) -> u32 {
    if regime_changed
        || guard_clamps > 0
        || reflex_events_delta > 0
        || p99_ns > p99_ceiling_ns / 2
    {
        return 0;
    }
    (prev_score + 1).min(STABILITY_THRESHOLD)
}

pub fn hibernate_samples_per_check(regime: Regime, stability_score: u32) -> u32 {
    let base = samples_per_check_for_regime(regime);
    if stability_score >= STABILITY_THRESHOLD {
        base * HIBERNATE_MULTIPLIER
    } else {
        base
    }
}

// --- L2 BATCH SLICE FEEDBACK (PHASE 2.5) ---
// CLOSED-LOOP CONTROL: L2 HIT RATE DRIVES BATCH_SLICE_NS ADJUSTMENTS.
// LOW L2 -> LONGER BATCH SLICES (FEWER MIGRATIONS).
// HIGH L2 -> SHORTER BATCH SLICES (MORE RESPONSIVE).

pub const L2_LOW_THRESH: u64 = 55;             // BELOW: L2 DEGRADED
pub const L2_HIGH_THRESH: u64 = 70;            // ABOVE: L2 EXCELLENT
pub const BATCH_STEP_UP_NS: u64 = 2_000_000;   // +2MS PER STEP (AGGRESSIVE RECOVERY)
pub const BATCH_STEP_DOWN_NS: u64 = 1_000_000; // -1MS PER STEP (CONSERVATIVE TIGHTENING)
pub const BATCH_MAX_NS: u64 = 24_000_000;      // 24MS ABSOLUTE CEILING
pub const L2_HOLD_TICKS: u32 = 3;              // 3 CONSECUTIVE TICKS TO TRIGGER

pub fn adjust_batch_slice(
    current_batch_ns: u64,
    baseline_batch_ns: u64,
    l2_pct: u64,
    l2_low_ticks: u32,
    l2_high_ticks: u32,
) -> (u64, u32, u32) {
    if l2_pct < L2_LOW_THRESH {
        let new_low = l2_low_ticks + 1;
        if new_low >= L2_HOLD_TICKS {
            let new_batch = (current_batch_ns + BATCH_STEP_UP_NS).min(BATCH_MAX_NS);
            return (new_batch, 0, 0);
        }
        return (current_batch_ns, new_low, 0);
    }
    if l2_pct > L2_HIGH_THRESH {
        let new_high = l2_high_ticks + 1;
        if new_high >= L2_HOLD_TICKS {
            let new_batch = current_batch_ns
                .saturating_sub(BATCH_STEP_DOWN_NS)
                .max(baseline_batch_ns);
            return (new_batch, 0, 0);
        }
        return (current_batch_ns, 0, new_high);
    }
    (current_batch_ns, 0, 0)
}

// --- TELEMETRY GATING (PHASE 2.5) ---

pub fn should_print_telemetry(tick_counter: u64, stability_score: u32) -> bool {
    if stability_score >= STABILITY_THRESHOLD {
        tick_counter % 2 == 0
    } else {
        true
    }
}
