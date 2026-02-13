// PANDEMONIUM v1.0 ADAPTIVE CONTROL LOOP
// EVENT-DRIVEN CLOSED-LOOP TUNING SYSTEM
//
// TWO THREADS, ZERO MUTEXES:
//   REFLEX THREAD: RING BUFFER CONSUMER. REACTS TO EVERY WAKE LATENCY SAMPLE.
//                  TIGHTENS TUNING KNOBS ON P99 SPIKES. SUB-MILLISECOND RESPONSE.
//   MONITOR THREAD: 1-SECOND CONTROL LOOP. DETECTS WORKLOAD REGIME.
//                   SETS BASELINE KNOBS. RELAXES GRADUALLY AFTER P99 NORMALIZES.
//
// BPF PRODUCES EVENTS, RUST REACTS. RUST WRITES KNOBS, BPF READS THEM
// ON THE VERY NEXT SCHEDULING DECISION. ONE SYSTEM, NOT TWO.

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use libbpf_rs::MapCore;

use crate::scheduler::{PandemoniumStats, Scheduler, TuningKnobs};

// --- REGIME THRESHOLDS ---

const LIGHT_IDLE_PCT: u64 = 40;
const HEAVY_IDLE_PCT: u64 = 15;

// --- REGIME PROFILES ---

const LIGHT_SLICE_NS: u64     = 500_000;
const LIGHT_PREEMPT_NS: u64   = 1_000_000;
const LIGHT_LAG_SCALE: u64    = 6;
const LIGHT_P99_CEIL_NS: u64  = 250_000;

const MIXED_SLICE_NS: u64     = 1_000_000;
const MIXED_PREEMPT_NS: u64   = 1_000_000;
const MIXED_LAG_SCALE: u64    = 4;
const MIXED_P99_CEIL_NS: u64  = 1_000_000;

const HEAVY_SLICE_NS: u64     = 1_000_000;
const HEAVY_PREEMPT_NS: u64   = 1_000_000;
const HEAVY_LAG_SCALE: u64    = 2;
const HEAVY_P99_CEIL_NS: u64  = 2_000_000;

// --- REFLEX PARAMETERS ---

const SAMPLES_PER_CHECK: u64 = 64;
const COOLDOWN_CHECKS: u32   = 2;
const MIN_SLICE_NS: u64      = 500_000;

// GRADUATED RELAX (ONLY CHANGE FROM WORKING VERSION)
const RELAX_STEP_NS: u64    = 100_000;  // RELAX BY 100US PER TICK
const RELAX_HOLD_TICKS: u32 = 3;       // WAIT 3S OF GOOD P99 BEFORE STEPPING

// --- LOCK-FREE LATENCY HISTOGRAM ---

const HIST_BUCKETS: usize = 12;
const HIST_EDGES_NS: [u64; HIST_BUCKETS] = [
    10_000,      // 10us
    25_000,      // 25us
    50_000,      // 50us
    100_000,     // 100us
    250_000,     // 250us
    500_000,     // 500us
    1_000_000,   // 1ms
    2_000_000,   // 2ms
    5_000_000,   // 5ms
    10_000_000,  // 10ms
    20_000_000,  // 20ms
    u64::MAX,    // +inf
];

// --- TYPES ---

#[repr(C)]
struct WakeLatSample {
    lat_ns: u64,
    pid:    u32,
    path:   u8,     // 0=IDLE, 1=HARD_KICK, 2=SOFT_KICK
    _pad:   [u8; 3],
}

#[repr(u8)]
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Regime {
    Light = 0,
    Mixed = 1,
    Heavy = 2,
}

impl Regime {
    fn from_u8(v: u8) -> Self {
        match v {
            0 => Self::Light,
            1 => Self::Mixed,
            _ => Self::Heavy,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Light => "LIGHT",
            Self::Mixed => "MIXED",
            Self::Heavy => "HEAVY",
        }
    }

    fn p99_ceiling(self) -> u64 {
        match self {
            Self::Light => LIGHT_P99_CEIL_NS,
            Self::Mixed => MIXED_P99_CEIL_NS,
            Self::Heavy => HEAVY_P99_CEIL_NS,
        }
    }
}

fn regime_knobs(r: Regime) -> TuningKnobs {
    match r {
        Regime::Light => TuningKnobs {
            slice_ns: LIGHT_SLICE_NS,
            preempt_thresh_ns: LIGHT_PREEMPT_NS,
            lag_scale: LIGHT_LAG_SCALE,
        },
        Regime::Mixed => TuningKnobs {
            slice_ns: MIXED_SLICE_NS,
            preempt_thresh_ns: MIXED_PREEMPT_NS,
            lag_scale: MIXED_LAG_SCALE,
        },
        Regime::Heavy => TuningKnobs {
            slice_ns: HEAVY_SLICE_NS,
            preempt_thresh_ns: HEAVY_PREEMPT_NS,
            lag_scale: HEAVY_LAG_SCALE,
        },
    }
}

// --- SHARED STATE (ATOMICS ONLY, NO MUTEX) ---

const ATOMIC_ZERO: AtomicU64 = AtomicU64::new(0);

pub struct SharedState {
    pub p99_ns: AtomicU64,
    regime: AtomicU8,
    sample_count: AtomicU64,
    histogram: [AtomicU64; HIST_BUCKETS],
}

impl SharedState {
    pub fn new() -> Self {
        Self {
            p99_ns: AtomicU64::new(0),
            regime: AtomicU8::new(Regime::Mixed as u8),
            sample_count: AtomicU64::new(0),
            histogram: [ATOMIC_ZERO; HIST_BUCKETS],
        }
    }

    fn record_sample(&self, lat_ns: u64) {
        let bucket = HIST_EDGES_NS.iter()
            .position(|&edge| lat_ns <= edge)
            .unwrap_or(HIST_BUCKETS - 1);
        self.histogram[bucket].fetch_add(1, Ordering::Relaxed);
        self.sample_count.fetch_add(1, Ordering::Relaxed);
    }

    // DRAIN HISTOGRAM, COMPUTE P99, STORE IN ATOMIC. RETURNS P99 IN NANOSECONDS.
    fn compute_and_reset_p99(&self) -> u64 {
        let mut counts = [0u64; HIST_BUCKETS];
        let mut total = 0u64;
        for i in 0..HIST_BUCKETS {
            counts[i] = self.histogram[i].swap(0, Ordering::Relaxed);
            total += counts[i];
        }
        self.sample_count.store(0, Ordering::Relaxed);

        if total == 0 {
            return 0;
        }

        // 99TH PERCENTILE: FIND BUCKET WHERE CUMULATIVE REACHES ceil(total * 0.99)
        let threshold = (total * 99 + 99) / 100;
        let mut cumulative = 0u64;
        for i in 0..HIST_BUCKETS {
            cumulative += counts[i];
            if cumulative >= threshold {
                // CAP AT LAST REAL BUCKET EDGE -- +INF (u64::MAX) WOULD POISON
                // EVERY COMPARISON AND TRIGGER INFINITE TIGHTENING
                let p99 = HIST_EDGES_NS[i].min(HIST_EDGES_NS[HIST_BUCKETS - 2]);
                self.p99_ns.store(p99, Ordering::Relaxed);
                return p99;
            }
        }

        let p99 = HIST_EDGES_NS[HIST_BUCKETS - 2];
        self.p99_ns.store(p99, Ordering::Relaxed);
        p99
    }

    fn current_regime(&self) -> Regime {
        Regime::from_u8(self.regime.load(Ordering::Relaxed))
    }

    fn set_regime(&self, r: Regime) {
        self.regime.store(r as u8, Ordering::Relaxed);
    }
}

// --- RING BUFFER BUILDER ---

// BUILD A RingBuffer FROM THE BPF WAKE LATENCY MAP.
// THE CALLBACK RECORDS EVERY SAMPLE INTO THE SHARED HISTOGRAM.
// THE RETURNED RingBuffer OWNS THE FD INTERNALLY -- SAFE TO MOVE TO THREAD.
pub fn build_ring_buffer(
    sched: &Scheduler,
    shared: Arc<SharedState>,
) -> Result<libbpf_rs::RingBuffer<'static>> {
    sched.build_wake_lat_ring_buffer(move |data: &[u8]| -> i32 {
        if data.len() >= std::mem::size_of::<WakeLatSample>() {
            let sample: WakeLatSample = unsafe {
                std::ptr::read_unaligned(data.as_ptr() as *const WakeLatSample)
            };
            shared.record_sample(sample.lat_ns);
        }
        0
    })
}

// --- REFLEX THREAD ---

// RING BUFFER CONSUMER. BLOCKS ON poll(), RECORDS SAMPLES, TIGHTENS KNOBS
// WHEN P99 EXCEEDS THE CURRENT REGIME'S CEILING.
// FIXED 25% CUT PER TRIGGER -- SIMPLE AND STABLE.
pub fn reflex_thread(
    ring_buf: libbpf_rs::RingBuffer<'static>,
    shared: Arc<SharedState>,
    knobs_handle: libbpf_rs::MapHandle,
    shutdown: &'static AtomicBool,
) {
    let mut cooldown: u32 = 0;
    let mut spike_count: u32 = 0;

    while !shutdown.load(Ordering::Relaxed) {
        // BLOCK FOR UP TO 100MS (SO WE CHECK SHUTDOWN PERIODICALLY)
        let _ = ring_buf.poll(Duration::from_millis(100));

        // CHECK IF ENOUGH SAMPLES ACCUMULATED FOR A P99 COMPUTATION
        let count = shared.sample_count.load(Ordering::Relaxed);
        if count < SAMPLES_PER_CHECK {
            continue;
        }

        let p99 = shared.compute_and_reset_p99();

        if cooldown > 0 {
            cooldown -= 1;
            continue;
        }

        let ceiling = shared.current_regime().p99_ceiling();
        if p99 > ceiling {
            spike_count += 1;
            // REQUIRE 2 CONSECUTIVE ABOVE-CEILING CHECKS BEFORE TIGHTENING.
            // FILTERS TRANSIENT NOISE THAT CAUSES FALSE TRIGGERS AT LOW CORE COUNTS.
            if spike_count >= 2 {
                tighten_knobs(&knobs_handle);
                cooldown = COOLDOWN_CHECKS;
                spike_count = 0;
            }
        } else {
            spike_count = 0;
        }
    }
}

fn tighten_knobs(handle: &libbpf_rs::MapHandle) {
    let key = 0u32.to_ne_bytes();
    let current = match handle.lookup(&key, libbpf_rs::MapFlags::ANY) {
        Ok(Some(v)) if v.len() >= std::mem::size_of::<TuningKnobs>() => unsafe {
            std::ptr::read_unaligned(v.as_ptr() as *const TuningKnobs)
        },
        _ => return,
    };

    let new_slice = (current.slice_ns * 3 / 4).max(MIN_SLICE_NS);
    let knobs = TuningKnobs {
        slice_ns: new_slice,
        preempt_thresh_ns: (new_slice * 2).min(1_000_000),
        lag_scale: current.lag_scale,
    };

    let value = unsafe {
        std::slice::from_raw_parts(
            &knobs as *const TuningKnobs as *const u8,
            std::mem::size_of::<TuningKnobs>(),
        )
    };
    let _ = handle.update(&key, value, libbpf_rs::MapFlags::ANY);
}

// --- MONITOR LOOP ---

// 1-SECOND CONTROL LOOP. READS STATS, DETECTS WORKLOAD REGIME,
// SETS BASELINE KNOBS, RELAXES GRADUALLY AFTER P99 NORMALIZES.
// RUNS ON THE MAIN THREAD. REPLACES THE OLD Scheduler::run().
pub fn monitor_loop(
    sched: &mut Scheduler,
    shared: &Arc<SharedState>,
    shutdown: &'static AtomicBool,
) -> Result<bool> {
    let mut prev = PandemoniumStats::default();
    let mut regime = Regime::Mixed;
    let mut relax_counter: u32 = 0;
    let mut tightened = false;
    let mut pending_regime = regime;
    let mut regime_hold: u32 = 0;

    // APPLY INITIAL REGIME
    shared.set_regime(regime);
    sched.write_tuning_knobs(&regime_knobs(regime))?;

    while !shutdown.load(Ordering::Relaxed) && !sched.exited() {
        std::thread::sleep(Duration::from_secs(1));

        let stats = sched.read_stats();

        // COMPUTE DELTAS
        let delta_d = stats.nr_dispatches.wrapping_sub(prev.nr_dispatches);
        let delta_idle = stats.nr_idle_hits.wrapping_sub(prev.nr_idle_hits);
        let delta_shared = stats.nr_shared.wrapping_sub(prev.nr_shared);
        let delta_preempt = stats.nr_preempt.wrapping_sub(prev.nr_preempt);
        let delta_keep = stats.nr_keep_running.wrapping_sub(prev.nr_keep_running);
        let delta_wake_sum = stats.wake_lat_sum.wrapping_sub(prev.wake_lat_sum);
        let delta_wake_samples = stats.wake_lat_samples.wrapping_sub(prev.wake_lat_samples);
        let delta_hard = stats.nr_hard_kicks.wrapping_sub(prev.nr_hard_kicks);
        let delta_soft = stats.nr_soft_kicks.wrapping_sub(prev.nr_soft_kicks);
        let delta_enq_wake = stats.nr_enq_wakeup.wrapping_sub(prev.nr_enq_wakeup);
        let delta_enq_requeue = stats.nr_enq_requeue.wrapping_sub(prev.nr_enq_requeue);
        let wake_avg_us = if delta_wake_samples > 0 {
            delta_wake_sum / delta_wake_samples / 1000
        } else {
            0
        };

        // PER-PATH LATENCY
        let d_idle_sum = stats.wake_lat_idle_sum.wrapping_sub(prev.wake_lat_idle_sum);
        let d_idle_cnt = stats.wake_lat_idle_cnt.wrapping_sub(prev.wake_lat_idle_cnt);
        let d_kick_sum = stats.wake_lat_kick_sum.wrapping_sub(prev.wake_lat_kick_sum);
        let d_kick_cnt = stats.wake_lat_kick_cnt.wrapping_sub(prev.wake_lat_kick_cnt);
        let lat_idle_us = if d_idle_cnt > 0 { d_idle_sum / d_idle_cnt / 1000 } else { 0 };
        let lat_kick_us = if d_kick_cnt > 0 { d_kick_sum / d_kick_cnt / 1000 } else { 0 };
        let delta_guard = stats.nr_guard_clamps.wrapping_sub(prev.nr_guard_clamps);

        let idle_pct = if delta_d > 0 {
            delta_idle * 100 / delta_d
        } else {
            0
        };

        // DETECT REGIME (WITH HYSTERESIS: 2 CONSECUTIVE TICKS TO CONFIRM SWITCH)
        let detected = if idle_pct > LIGHT_IDLE_PCT {
            Regime::Light
        } else if idle_pct < HEAVY_IDLE_PCT {
            Regime::Heavy
        } else {
            Regime::Mixed
        };

        let p99_ns = shared.p99_ns.load(Ordering::Relaxed);

        if detected != regime {
            if detected == pending_regime {
                regime_hold += 1;
            } else {
                pending_regime = detected;
                regime_hold = 1;
            }
            if regime_hold >= 2 {
                regime = detected;
                shared.set_regime(regime);
                sched.write_tuning_knobs(&regime_knobs(regime))?;
                tightened = false;
                relax_counter = 0;
            }
        } else {
            pending_regime = regime;
            regime_hold = 0;
        }

        if tightened {
            // GRADUATED RELAX: STEP TOWARD BASELINE BY RELAX_STEP_NS
            // (THIS IS THE ONLY CHANGE FROM THE WORKING VERSION --
            //  PREVIOUSLY THIS WAS A FULL SNAP-TO-BASELINE)
            let ceiling = regime.p99_ceiling();
            let baseline = regime_knobs(regime);
            if p99_ns <= ceiling {
                relax_counter += 1;
                if relax_counter >= RELAX_HOLD_TICKS {
                    let current = sched.read_tuning_knobs();
                    if current.slice_ns < baseline.slice_ns {
                        let new_slice = (current.slice_ns + RELAX_STEP_NS)
                            .min(baseline.slice_ns);
                        let knobs = TuningKnobs {
                            slice_ns: new_slice,
                            preempt_thresh_ns: (new_slice * 2).min(1_000_000),
                            lag_scale: baseline.lag_scale,
                        };
                        sched.write_tuning_knobs(&knobs)?;
                        if new_slice >= baseline.slice_ns {
                            tightened = false;
                        }
                    } else {
                        tightened = false;
                    }
                    relax_counter = 0;
                }
            } else {
                relax_counter = 0;
            }
        }

        // DETECT IF REFLEX THREAD TIGHTENED KNOBS
        if !tightened {
            let current = sched.read_tuning_knobs();
            let baseline = regime_knobs(regime);
            if current.slice_ns < baseline.slice_ns {
                tightened = true;
                relax_counter = 0;
            }
        }

        let p99_us = p99_ns / 1000;
        let knobs = sched.read_tuning_knobs();

        println!(
            "d/s: {:<8} idle: {}% shared: {:<6} preempt: {:<4} keep: {:<4} kick: H={:<4} S={:<4} enq: W={:<4} R={:<4} wake: {}us p99: {}us lat_idle: {}us lat_kick: {}us slice: {}us guard: {} [{}]",
            delta_d, idle_pct, delta_shared, delta_preempt, delta_keep,
            delta_hard, delta_soft, delta_enq_wake, delta_enq_requeue,
            wake_avg_us, p99_us, lat_idle_us, lat_kick_us,
            knobs.slice_ns / 1000, delta_guard, regime.label(),
        );

        sched.log.snapshot(
            delta_d, delta_idle, delta_shared,
            delta_preempt, delta_keep, wake_avg_us,
            delta_hard, delta_soft, lat_idle_us, lat_kick_us,
        );

        prev = stats;
    }

    // READ UEI EXIT REASON
    let should_restart = sched.read_exit_info();
    Ok(should_restart)
}
