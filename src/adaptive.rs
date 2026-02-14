// PANDEMONIUM v0.9.9 ADAPTIVE CONTROL LOOP
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

use crate::procdb::ProcessDb;
use crate::scheduler::{PandemoniumStats, Scheduler, TuningKnobs};

// --- REGIME THRESHOLDS (SCHMITT TRIGGER) ---
// DIRECTIONAL HYSTERESIS PREVENTS OSCILLATION AT REGIME BOUNDARIES.
// WIDE DEAD ZONES: MUST CLEARLY ENTER A REGIME AND CLEARLY LEAVE IT.
// ENTER/EXIT SEPARATION ELIMINATES THE POSITIVE FEEDBACK LOOP THAT
// CAUSED HEAVY<->MIXED THRASHING AT 12+ CORES.

const HEAVY_ENTER_PCT: u64 = 10;   // ENTER HEAVY: IDLE < 10%
const HEAVY_EXIT_PCT: u64  = 25;   // LEAVE HEAVY: IDLE > 25%
const LIGHT_ENTER_PCT: u64 = 50;   // ENTER LIGHT: IDLE > 50%
const LIGHT_EXIT_PCT: u64  = 30;   // LEAVE LIGHT: IDLE < 30%

// --- REGIME PROFILES ---
// BPF TIMER ALWAYS ON AT 1MS. SCANS FOR BATCH TASKS BLOCKING INTERACTIVE.
// PREEMPT_THRESH CONTROLS WHEN TIMER PREEMPTS (ONLY IF TASKS WAITING IN DSQ).
// BATCH_SLICE_NS CONTROLS MAX UNINTERRUPTED BATCH RUN WHEN NO INTERACTIVE WAITING.
// RESULT: INTERACTIVE LATENCY CAPPED AT ~1MS (TIMER PERIOD) REGARDLESS OF BATCH SLICE.

const LIGHT_SLICE_NS: u64     = 2_000_000;   // 2MS: TIMER PREEMPTS BATCH AFTER 2MS IF INTERACTIVE WAITING
const LIGHT_PREEMPT_NS: u64   = 1_000_000;   // 1MS: AGGRESSIVE -- IDLE CPUs AVAILABLE, LOW OVERHEAD
const LIGHT_LAG_SCALE: u64    = 6;
const LIGHT_P99_CEIL_NS: u64  = 3_000_000;   // 3MS: TIGHT BUT REALISTIC FOR LIGHT LOAD
const LIGHT_BATCH_NS: u64     = 20_000_000;  // 20MS: NO CONTENTION, LET BATCH RIP
const LIGHT_TIMER_NS: u64     = 2_000_000;   // 2MS: IDLE CPUs HANDLE DISPATCH, TIMER IS SAFETY NET

const MIXED_SLICE_NS: u64     = 1_000_000;   // 1MS: TIGHT INTERACTIVE CONTROL UNDER CONTENTION
const MIXED_PREEMPT_NS: u64   = 1_000_000;   // 1MS: MATCH TIMER FOR CLEAN ENFORCEMENT
const MIXED_LAG_SCALE: u64    = 4;
const MIXED_P99_CEIL_NS: u64  = 5_000_000;   // 5MS: BELOW 16MS FRAME BUDGET
const MIXED_BATCH_NS: u64     = 16_000_000;  // 16MS: WIDER -- BPF KICK HANDLES INTERACTIVE LATENCY DIRECTLY
const MIXED_TIMER_NS: u64     = 1_000_000;   // 1MS: MIXED NEEDS FAST RESPONSE

const HEAVY_SLICE_NS: u64     = 4_000_000;   // 4MS: WIDER UNDER SATURATION FOR THROUGHPUT
const HEAVY_PREEMPT_NS: u64   = 2_000_000;   // 2MS: SLIGHTLY RELAXED -- EVERYTHING IS CONTENDING
const HEAVY_LAG_SCALE: u64    = 2;
const HEAVY_P99_CEIL_NS: u64  = 10_000_000;  // 10MS: HEAVY LOAD, REALISTIC CEILING
const HEAVY_BATCH_NS: u64     = 20_000_000;  // 20MS: BPF KICK + TIMER OWN LATENCY, LET BATCH RIP
const HEAVY_TIMER_NS: u64     = 2_000_000;   // 2MS: HALVES TIMER OVERHEAD UNDER SATURATION

// --- REFLEX PARAMETERS ---

const SAMPLES_PER_CHECK: u64 = 64;
const COOLDOWN_CHECKS: u32   = 2;
const MIN_SLICE_NS: u64      = 500_000;   // 500US FLOOR -- ALLOWS 5 TIGHTEN STEPS FROM 2MS BASELINE

// GRADUATED RELAX: STEP TOWARD BASELINE AFTER P99 NORMALIZES
const RELAX_STEP_NS: u64    = 500_000;   // RELAX BY 500US PER TICK
const RELAX_HOLD_TICKS: u32 = 2;         // WAIT 2S OF GOOD P99 BEFORE STEPPING

// --- LOCK-FREE LATENCY HISTOGRAM ---

// SLEEP PATTERN BUCKETS: CLASSIFY IO-WAIT VS IDLE WORKLOADS
const SLEEP_BUCKETS: usize = 4;
const SLEEP_EDGES_NS: [u64; SLEEP_BUCKETS] = [
    1_000_000,      // 1ms: IO-WAIT (FAST DISK/NETWORK/PIPE)
    10_000_000,     // 10ms: SHORT IO (TYPICAL DISK READ)
    100_000_000,    // 100ms: MODERATE (NETWORK, USER INPUT)
    u64::MAX,       // +INF: IDLE (LONG SLEEP, TIMER, POLLING)
];

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
    lat_ns:   u64,
    sleep_ns: u64,    // HOW LONG TASK SLEPT BEFORE THIS WAKEUP
    pid:      u32,
    path:     u8,     // 0=IDLE, 1=HARD_KICK, 2=SOFT_KICK
    tier:     u8,     // TASK TIER AT WAKEUP TIME
    _pad:     [u8; 2],
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
            batch_slice_ns: LIGHT_BATCH_NS,
            timer_interval_ns: LIGHT_TIMER_NS,
        },
        Regime::Mixed => TuningKnobs {
            slice_ns: MIXED_SLICE_NS,
            preempt_thresh_ns: MIXED_PREEMPT_NS,
            lag_scale: MIXED_LAG_SCALE,
            batch_slice_ns: MIXED_BATCH_NS,
            timer_interval_ns: MIXED_TIMER_NS,
        },
        Regime::Heavy => TuningKnobs {
            slice_ns: HEAVY_SLICE_NS,
            preempt_thresh_ns: HEAVY_PREEMPT_NS,
            lag_scale: HEAVY_LAG_SCALE,
            batch_slice_ns: HEAVY_BATCH_NS,
            timer_interval_ns: HEAVY_TIMER_NS,
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
    sleep_histogram: [AtomicU64; SLEEP_BUCKETS],
    sleep_count: AtomicU64,
    pub reflex_events: AtomicU64,
}

impl SharedState {
    pub fn new() -> Self {
        Self {
            p99_ns: AtomicU64::new(0),
            regime: AtomicU8::new(Regime::Mixed as u8),
            sample_count: AtomicU64::new(0),
            histogram: [ATOMIC_ZERO; HIST_BUCKETS],
            sleep_histogram: [ATOMIC_ZERO; SLEEP_BUCKETS],
            sleep_count: AtomicU64::new(0),
            reflex_events: AtomicU64::new(0),
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

    fn record_sleep(&self, sleep_ns: u64) {
        let bucket = SLEEP_EDGES_NS.iter()
            .position(|&edge| sleep_ns <= edge)
            .unwrap_or(SLEEP_BUCKETS - 1);
        self.sleep_histogram[bucket].fetch_add(1, Ordering::Relaxed);
        self.sleep_count.fetch_add(1, Ordering::Relaxed);
    }

    // DRAIN SLEEP HISTOGRAM. RETURNS (IO_WAIT_PCT, IDLE_PCT).
    // IO_WAIT = SLEEPS < 10MS, IDLE = SLEEPS > 100MS.
    fn compute_and_reset_sleep(&self) -> (u64, u64) {
        let mut counts = [0u64; SLEEP_BUCKETS];
        let mut total = 0u64;
        for i in 0..SLEEP_BUCKETS {
            counts[i] = self.sleep_histogram[i].swap(0, Ordering::Relaxed);
            total += counts[i];
        }
        self.sleep_count.store(0, Ordering::Relaxed);

        if total == 0 {
            return (0, 0);
        }

        let io_count = counts[0] + counts[1]; // <10MS
        let idle_count = counts[3]; // >100MS
        (io_count * 100 / total, idle_count * 100 / total)
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
            if sample.sleep_ns > 0 {
                shared.record_sleep(sample.sleep_ns);
            }
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

        let current_regime = shared.current_regime();
        let ceiling = current_regime.p99_ceiling();
        if p99 > ceiling {
            spike_count += 1;
            // REQUIRE 2 CONSECUTIVE ABOVE-CEILING CHECKS BEFORE TIGHTENING.
            // FILTERS TRANSIENT NOISE THAT CAUSES FALSE TRIGGERS AT LOW CORE COUNTS.
            if spike_count >= 2 {
                // ONLY TIGHTEN IN MIXED. LIGHT HAS NO CONTENTION
                // (POINTLESS). HEAVY IS FULLY SATURATED (MORE PREEMPTION
                // JUST ADDS OVERHEAD). MIXED IS THE ONLY REGIME WHERE
                // SHORTER SLICES COULD PLAUSIBLY HELP INTERACTIVE TASKS.
                if current_regime == Regime::Mixed {
                    tighten_knobs(&knobs_handle);
                    shared.reflex_events.fetch_add(1, Ordering::Relaxed);
                }
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
    // ONLY TIGHTEN SLICE + PREEMPT. BATCH_SLICE_NS STAYS WIDE FOR THROUGHPUT.
    // THE BPF TIMER HANDLES INTERACTIVE PREEMPTION VIA preempt_thresh_ns.
    let knobs = TuningKnobs {
        slice_ns: new_slice,
        preempt_thresh_ns: new_slice,
        lag_scale: current.lag_scale,
        batch_slice_ns: current.batch_slice_ns,
        timer_interval_ns: current.timer_interval_ns,
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
    let mut light_ticks: u64 = 0;
    let mut mixed_ticks: u64 = 0;
    let mut heavy_ticks: u64 = 0;

    let mut procdb = match ProcessDb::new() {
        Ok(db) => Some(db),
        Err(e) => {
            log_warn!("PROCDB INIT FAILED: {}", e);
            None
        }
    };

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
        let delta_affin = stats.nr_affinity_hits.wrapping_sub(prev.nr_affinity_hits);
        // DIAGNOSTIC: delta_zero commented out -- ~0.03% rate, not throughput issue
        // let delta_zero = stats.nr_zero_slice.wrapping_sub(prev.nr_zero_slice);

        let idle_pct = if delta_d > 0 {
            delta_idle * 100 / delta_d
        } else {
            0
        };

        // DETECT REGIME (SCHMITT TRIGGER + 2-TICK HOLD)
        // DIRECTION-AWARE: CURRENT REGIME DETERMINES WHICH THRESHOLDS APPLY.
        // DEAD ZONES PREVENT THE OSCILLATION THAT SINGLE-BOUNDARY DETECTION CAUSED.
        let detected = match regime {
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
            // GRADUATED RELAX: STEP SLICE TOWARD BASELINE (BATCH UNTOUCHED)
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
                            preempt_thresh_ns: baseline.preempt_thresh_ns
                                .min(new_slice),
                            lag_scale: baseline.lag_scale,
                            batch_slice_ns: baseline.batch_slice_ns,
                            timer_interval_ns: baseline.timer_interval_ns,
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

        // PROCESS CLASSIFICATION DATABASE: INGEST, PREDICT, EVICT
        let (db_total, db_confident) = if let Some(ref mut db) = procdb {
            db.ingest();
            db.flush_predictions();
            db.tick();
            db.summary()
        } else {
            (0, 0)
        };

        // SLEEP PATTERN ANALYSIS: IO-WAIT VS IDLE CLASSIFICATION
        let (io_pct, _idle_sleep_pct) = shared.compute_and_reset_sleep();

        let p99_us = p99_ns / 1000;
        let knobs = sched.read_tuning_knobs();

        println!(
            "d/s: {:<8} idle: {}% shared: {:<6} preempt: {:<4} keep: {:<4} kick: H={:<4} S={:<4} enq: W={:<4} R={:<4} wake: {}us p99: {}us lat_idle: {}us lat_kick: {}us affin: {} procdb: {}/{} sleep: io={}% slice: {}us guard: {} [{}]",
            delta_d, idle_pct, delta_shared, delta_preempt, delta_keep,
            delta_hard, delta_soft, delta_enq_wake, delta_enq_requeue,
            wake_avg_us, p99_us, lat_idle_us, lat_kick_us,
            delta_affin, db_total, db_confident,
            io_pct, knobs.slice_ns / 1000, delta_guard, regime.label(),
        );

        sched.log.snapshot(
            delta_d, delta_idle, delta_shared,
            delta_preempt, delta_keep, wake_avg_us,
            delta_hard, delta_soft, lat_idle_us, lat_kick_us,
        );

        match regime {
            Regime::Light => light_ticks += 1,
            Regime::Mixed => mixed_ticks += 1,
            Regime::Heavy => heavy_ticks += 1,
        }

        prev = stats;
    }

    // KNOBS SUMMARY: CAPTURED BY TEST HARNESS FOR ARCHIVE
    let final_knobs = sched.read_tuning_knobs();
    let reflex_count = shared.reflex_events.load(Ordering::Relaxed);
    println!(
        "[KNOBS] regime={} slice_ns={} batch_ns={} preempt_ns={} timer_ns={} lag={} tightened={} reflex={} ticks=L:{}/M:{}/H:{}",
        regime.label(), final_knobs.slice_ns, final_knobs.batch_slice_ns,
        final_knobs.preempt_thresh_ns, final_knobs.timer_interval_ns,
        final_knobs.lag_scale, tightened, reflex_count,
        light_ticks, mixed_ticks, heavy_ticks,
    );

    // READ UEI EXIT REASON
    let should_restart = sched.read_exit_info();
    Ok(should_restart)
}
