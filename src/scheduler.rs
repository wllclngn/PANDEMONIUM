// PANDEMONIUM SCHEDULER
// WRAPS THE BPF SKELETON: OPEN, CONFIGURE, LOAD, ATTACH, MONITOR, SHUTDOWN

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::Result;
use libbpf_rs::MapCore;
use libbpf_rs::skel::{OpenSkel, SkelBuilder};

use crate::bpf_skel::*;
use pandemonium::event::EventLog;

// SCX EXIT CODES (FROM KERNEL)
const SCX_EXIT_NONE: i32 = 0;
const SCX_ECODE_RST_MASK: u64 = 1 << 16;

// SCX DSQ FLAGS (STABLE KERNEL ABI -- sched_ext/sched.h)
// THESE MUST BE SET IN RODATA BEFORE LOADING THE BPF PROGRAM.
// THE VENDORED HEADERS USE const volatile u64 __weak WHICH DEFAULTS TO 0
// WITHOUT USERSPACE POPULATING THEM.
const SCX_DSQ_FLAG_BUILTIN:  u64 = 1u64 << 63;
const SCX_DSQ_FLAG_LOCAL_ON: u64 = 1u64 << 62;

// MATCHES struct pandemonium_stats IN BPF (intf.h)
#[repr(C)]
#[derive(Default, Clone, Copy)]
struct PandemoniumStats {
    nr_dispatches: u64,
    nr_idle_hits: u64,
    nr_direct: u64,
    nr_overflow: u64,
    nr_preempt: u64,
    nr_interactive: u64,
    nr_sticky: u64,
    nr_boosted: u64,
    nr_kicks: u64,
    nr_lat_critical: u64,
    nr_batch: u64,
    lat_cri_sum: u64,
    nr_tier_changes: u64,
    nr_compositor: u64,
    wake_lat_sum: u64,
    wake_lat_max: u64,
    wake_lat_samples: u64,
}

pub struct Scheduler<'a> {
    skel: MainSkel<'a>,
    _link: libbpf_rs::Link,
    verbose: bool,
    lat_cri_low: u64,
    lat_cri_high: u64,
    pub log: EventLog,
}

impl<'a> Scheduler<'a> {
    pub fn init(
        open_object: &'a mut MaybeUninit<libbpf_rs::OpenObject>,
        build_mode: bool,
        slice_ns: u64,
        slice_min_ns: u64,
        slice_max_ns: u64,
        lat_cri_low: u64,
        lat_cri_high: u64,
        verbose: bool,
        lightweight: bool,
        nr_cpus_override: Option<u64>,
    ) -> Result<Self> {
        // OPEN
        let builder = MainSkelBuilder::default();
        let mut open_skel = builder.open(open_object)?;

        // CONFIGURE RODATA (BEFORE LOAD)
        let rodata = open_skel.maps.rodata_data.as_mut().unwrap();
        rodata.slice_ns = slice_ns;
        rodata.slice_min_ns = slice_min_ns;
        rodata.slice_max_ns = slice_max_ns;
        rodata.build_mode = build_mode;
        rodata.lat_cri_thresh_low = lat_cri_low;
        rodata.lat_cri_thresh_high = lat_cri_high;

        // DSQs MUST COVER ALL POSSIBLE CPUs (KERNEL MAY REFERENCE ANY OF THEM).
        // SCALING FORMULAS USE THE OVERRIDE (--nr-cpus) FOR BEHAVIORAL TUNING.
        let possible = libbpf_rs::num_possible_cpus()? as u64;
        let scaling = nr_cpus_override.unwrap_or(possible);
        rodata.nr_cpu_ids = possible;
        rodata.nr_scaling_cpus = scaling;
        rodata.lightweight_mode = lightweight;

        // POPULATE SCX ENUM VALUES
        // THE VENDORED HEADERS DEFINE THESE AS const volatile u64 __weak
        // WHICH DEFAULT TO 0 IF USERSPACE DOESN'T SET THEM.
        // SCX_DSQ_LOCAL = 0 WOULD COLLIDE WITH CPU 0'S PER-CPU DSQ.
        rodata.__SCX_DSQ_FLAG_BUILTIN  = SCX_DSQ_FLAG_BUILTIN;
        rodata.__SCX_DSQ_FLAG_LOCAL_ON = SCX_DSQ_FLAG_LOCAL_ON;
        rodata.__SCX_DSQ_INVALID       = SCX_DSQ_FLAG_BUILTIN;
        rodata.__SCX_DSQ_GLOBAL        = SCX_DSQ_FLAG_BUILTIN | 1;
        rodata.__SCX_DSQ_LOCAL         = SCX_DSQ_FLAG_BUILTIN | SCX_DSQ_FLAG_LOCAL_ON;
        rodata.__SCX_DSQ_LOCAL_ON      = SCX_DSQ_FLAG_BUILTIN | SCX_DSQ_FLAG_LOCAL_ON | 1;
        rodata.__SCX_DSQ_LOCAL_CPU_MASK = 0xFFFFFFFF;

        // POPULATE SCX_KICK_* ENUM VALUES
        rodata.__SCX_KICK_IDLE    = 1;   // vmlinux.h:19408
        rodata.__SCX_KICK_PREEMPT = 2;   // vmlinux.h:19409
        rodata.__SCX_KICK_WAIT    = 4;   // vmlinux.h:19410

        // LOAD (VALIDATES BPF WITH KERNEL)
        let mut skel = open_skel.load()?;

        // ATTACH STRUCT_OPS (MAKES PANDEMONIUM THE ACTIVE SCHEDULER)
        // skel.attach() DOESN'T WORK FOR STRUCT_OPS -- IT ATTACHES PROGRAMS, NOT MAPS.
        // STRUCT_OPS MUST BE ATTACHED VIA bpf_map__attach_struct_ops() DIRECTLY.
        let link = skel.maps.pandemonium_ops.attach_struct_ops()?;

        // PIN IDLE BITMAP FOR `pandemonium idle-cpus` SUBCOMMAND
        let pin_dir = "/sys/fs/bpf/pandemonium";
        let pin_path = "/sys/fs/bpf/pandemonium/idle_cpus";
        std::fs::create_dir_all(pin_dir).ok();
        // REMOVE STALE PIN FROM PREVIOUS UNCLEAN SHUTDOWN
        std::fs::remove_file(pin_path).ok();
        skel.maps.idle_bitmap.pin(pin_path)?;

        Ok(Self {
            skel,
            _link: link,
            verbose,
            lat_cri_low,
            lat_cri_high,
            log: EventLog::new(),
        })
    }

    // SUM PER-CPU HISTOGRAM INTO A SINGLE TOTAL
    fn read_histogram(&self) -> [u64; 32] {
        let key = 0u32.to_ne_bytes();
        let mut total = [0u64; 32];

        let percpu_vals = match self.skel.maps.hist_map.lookup_percpu(&key, libbpf_rs::MapFlags::ANY) {
            Ok(Some(v)) => v,
            _ => return total,
        };

        for cpu_val in &percpu_vals {
            // EACH CPU'S VALUE IS 32 * u64 = 256 BYTES
            if cpu_val.len() >= 32 * 8 {
                for i in 0..32 {
                    let offset = i * 8;
                    let val = u64::from_ne_bytes(
                        cpu_val[offset..offset + 8].try_into().unwrap_or([0; 8])
                    );
                    total[i] += val;
                }
            }
        }

        total
    }

    // SUM PER-CPU STATS INTO A SINGLE TOTAL
    fn read_stats(&self) -> PandemoniumStats {
        let key = 0u32.to_ne_bytes();
        let mut total = PandemoniumStats::default();

        let percpu_vals = match self.skel.maps.stats_map.lookup_percpu(&key, libbpf_rs::MapFlags::ANY) {
            Ok(Some(v)) => v,
            _ => return total,
        };

        for cpu_val in &percpu_vals {
            if cpu_val.len() >= std::mem::size_of::<PandemoniumStats>() {
                let stats: PandemoniumStats = unsafe {
                    std::ptr::read_unaligned(cpu_val.as_ptr() as *const PandemoniumStats)
                };
                total.nr_dispatches += stats.nr_dispatches;
                total.nr_idle_hits += stats.nr_idle_hits;
                total.nr_direct += stats.nr_direct;
                total.nr_overflow += stats.nr_overflow;
                total.nr_preempt += stats.nr_preempt;
                total.nr_interactive += stats.nr_interactive;
                total.nr_sticky += stats.nr_sticky;
                total.nr_boosted += stats.nr_boosted;
                total.nr_kicks += stats.nr_kicks;
                total.nr_lat_critical += stats.nr_lat_critical;
            total.nr_batch += stats.nr_batch;
            total.lat_cri_sum += stats.lat_cri_sum;
            total.nr_tier_changes += stats.nr_tier_changes;
            total.nr_compositor += stats.nr_compositor;
            total.wake_lat_sum += stats.wake_lat_sum;
            if stats.wake_lat_max > total.wake_lat_max {
                total.wake_lat_max = stats.wake_lat_max;
            }
            total.wake_lat_samples += stats.wake_lat_samples;
        }
        }

        total
    }

    // RUN THE MONITORING LOOP. RETURNS TRUE IF SCHED_EXT REQUESTED RESTART.
    pub fn run(&mut self, shutdown: &AtomicBool) -> Result<bool> {
        let mut prev = PandemoniumStats::default();

        while !shutdown.load(Ordering::Relaxed) && !self.exited() {
            std::thread::sleep(Duration::from_secs(1));

            let stats = self.read_stats();

            let delta_d = stats.nr_dispatches - prev.nr_dispatches;
            let delta_idle = stats.nr_idle_hits - prev.nr_idle_hits;
            let delta_direct = stats.nr_direct - prev.nr_direct;
            let delta_overflow = stats.nr_overflow - prev.nr_overflow;
            let delta_preempt = stats.nr_preempt - prev.nr_preempt;
            let delta_int = stats.nr_interactive - prev.nr_interactive;
            let delta_sticky = stats.nr_sticky - prev.nr_sticky;
            let delta_boost = stats.nr_boosted - prev.nr_boosted;
            let delta_kicks = stats.nr_kicks - prev.nr_kicks;
            let delta_lat_cri = stats.nr_lat_critical - prev.nr_lat_critical;
            let delta_batch = stats.nr_batch - prev.nr_batch;
            let delta_lat_cri_sum = stats.lat_cri_sum - prev.lat_cri_sum;
            let delta_tier_chg = stats.nr_tier_changes - prev.nr_tier_changes;
            let delta_compositor = stats.nr_compositor - prev.nr_compositor;
            let delta_wake_sum = stats.wake_lat_sum - prev.wake_lat_sum;
            let delta_wake_samples = stats.wake_lat_samples - prev.wake_lat_samples;
            let wake_avg = if delta_wake_samples > 0 {
                delta_wake_sum / delta_wake_samples
            } else {
                0
            };

            let total_wakeups = delta_lat_cri + delta_int + delta_batch;
            let avg_score = if total_wakeups > 0 { delta_lat_cri_sum / total_wakeups } else { 0 };

            println!("dispatches/s: {:<8} idle: {:<8} direct: {:<6} overflow: {:<6} preempt: {:<6} lat_cri: {:<6} int: {:<6} batch: {:<6} sticky: {:<6} boosted: {:<6} kicks: {:<6} avg_score: {:<6} tier_chg: {:<6} compositor: {:<6} wake_avg: {:<6} wake_max: {:<6}",
                delta_d, delta_idle, delta_direct, delta_overflow, delta_preempt, delta_lat_cri, delta_int, delta_batch, delta_sticky, delta_boost, delta_kicks, avg_score, delta_tier_chg, delta_compositor, wake_avg, stats.wake_lat_max);

            self.log.snapshot(delta_d, delta_idle, delta_direct + delta_overflow,
                              delta_lat_cri, delta_int, delta_batch);

            if self.verbose {
                println!("  TOTAL dispatches={} idle={} direct={} overflow={} preempt={} lat_cri={} int={} batch={} sticky={} boosted={} kicks={} tier_changes={} compositor={} wake_avg={} wake_max={}",
                    stats.nr_dispatches, stats.nr_idle_hits, stats.nr_direct, stats.nr_overflow, stats.nr_preempt,
                    stats.nr_lat_critical, stats.nr_interactive, stats.nr_batch, stats.nr_sticky, stats.nr_boosted, stats.nr_kicks,
                    stats.nr_tier_changes, stats.nr_compositor,
                    if stats.wake_lat_samples > 0 { stats.wake_lat_sum / stats.wake_lat_samples } else { 0 },
                    stats.wake_lat_max);
            }

            prev = stats;
        }

        // READ UEI FOR EXIT REASON
        let data = self.skel.maps.data_data.as_ref().unwrap();
        let kind = data.uei.kind;
        let exit_code = data.uei.exit_code;

        if kind != SCX_EXIT_NONE {
            // REASON AND MSG ARE i8 ARRAYS. CAST TO u8 FOR FROM_UTF8.
            let reason_bytes: &[u8] = unsafe {
                std::slice::from_raw_parts(data.uei.reason.as_ptr() as *const u8, 128)
            };
            let msg_bytes: &[u8] = unsafe {
                std::slice::from_raw_parts(data.uei.msg.as_ptr() as *const u8, 1024)
            };

            let reason = std::str::from_utf8(reason_bytes)
                .unwrap_or("unknown")
                .trim_end_matches('\0');
            let msg = std::str::from_utf8(msg_bytes)
                .unwrap_or("")
                .trim_end_matches('\0');

            println!("EXIT KIND: {}  CODE: {}", kind, exit_code);
            if !reason.is_empty() {
                println!("REASON: {}", reason);
            }
            if !msg.is_empty() {
                println!("MSG: {}", msg);
            }
        }

        // CHECK IF KERNEL REQUESTED RESTART
        let should_restart = (exit_code as u64 & SCX_ECODE_RST_MASK) != 0;
        Ok(should_restart)
    }

    // CALIBRATION MODE: COLLECT LAT_CRI HISTOGRAM AND SUGGEST THRESHOLDS
    pub fn calibrate(&mut self, shutdown: &AtomicBool) -> Result<()> {
        println!("CALIBRATION MODE: COLLECTING DATA FOR 30 SECONDS...");
        println!("  RUN YOUR TYPICAL WORKLOAD NOW.");
        println!();

        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        let mut tick = 0u32;

        while !shutdown.load(Ordering::Relaxed) && !self.exited()
              && std::time::Instant::now() < deadline
        {
            std::thread::sleep(Duration::from_secs(1));
            tick += 1;
            if tick % 5 == 0 {
                println!("  {}s / 30s ...", tick);
            }
        }

        println!();
        println!("CALIBRATION COMPLETE. READING HISTOGRAM...");
        println!();

        let hist = self.read_histogram();
        let total: u64 = hist.iter().sum();

        if total == 0 {
            println!("NO DATA COLLECTED. WAS A WORKLOAD RUNNING?");
            return Ok(());
        }

        // PRINT HISTOGRAM
        let max_count = *hist.iter().max().unwrap_or(&1);
        let bar_width = 40usize;

        println!("{:<12} {:>10} {:>7}  {}", "SCORE RANGE", "COUNT", "%", "DISTRIBUTION");
        println!("{}", "-".repeat(75));
        for (i, &count) in hist.iter().enumerate() {
            let lo = i * 8;
            let hi = lo + 7;
            let pct = count as f64 / total as f64 * 100.0;
            let bar_len = if max_count > 0 {
                (count as f64 / max_count as f64 * bar_width as f64) as usize
            } else {
                0
            };
            let bar: String = "#".repeat(bar_len);
            println!("{:>3}-{:<3}       {:>10} {:>6.1}%  {}", lo, hi, count, pct, bar);
        }
        println!("{}", "-".repeat(75));
        println!("TOTAL SAMPLES: {}", total);
        println!();

        // COMPUTE PERCENTILE-BASED THRESHOLD SUGGESTIONS
        let mut cumulative = 0u64;
        let mut p90_score = 0u32;
        let mut p99_score = 0u32;
        let p90_target = (total as f64 * 0.90) as u64;
        let p99_target = (total as f64 * 0.99) as u64;

        for (i, &count) in hist.iter().enumerate() {
            cumulative += count;
            if p90_score == 0 && cumulative >= p90_target {
                p90_score = (i as u32 + 1) * 8; // UPPER BOUND OF BUCKET
            }
            if p99_score == 0 && cumulative >= p99_target {
                p99_score = (i as u32 + 1) * 8;
            }
        }

        // CLAMP TO VALID RANGE
        if p90_score < 4 { p90_score = 4; }
        if p99_score < p90_score + 8 { p99_score = p90_score + 8; }
        if p99_score > 248 { p99_score = 248; }

        println!("SUGGESTED THRESHOLDS:");
        println!("  --lat-cri-low {}   (P90: 90%% OF TASKS SCORE BELOW THIS = BATCH)", p90_score);
        println!("  --lat-cri-high {}  (P99: TOP 1%% = LATENCY-CRITICAL)", p99_score);
        println!();
        println!(
            "CURRENT THRESHOLDS: LOW={}, HIGH={}",
            self.lat_cri_low, self.lat_cri_high
        );
        println!();

        // SHOW TIER DISTRIBUTION AT SUGGESTED THRESHOLDS
        let mut batch = 0u64;
        let mut interactive = 0u64;
        let mut lat_cri = 0u64;
        for (i, &count) in hist.iter().enumerate() {
            let bucket_upper = ((i as u32) + 1) * 8;
            if bucket_upper <= p90_score {
                batch += count;
            } else if bucket_upper <= p99_score {
                interactive += count;
            } else {
                lat_cri += count;
            }
        }
        println!("PROJECTED TIER DISTRIBUTION AT SUGGESTED THRESHOLDS:");
        println!("  BATCH:        {:>6.1}%", batch as f64 / total as f64 * 100.0);
        println!("  INTERACTIVE:  {:>6.1}%", interactive as f64 / total as f64 * 100.0);
        println!("  LAT_CRITICAL: {:>6.1}%", lat_cri as f64 / total as f64 * 100.0);

        Ok(())
    }

    fn exited(&self) -> bool {
        self.skel.maps.data_data.as_ref().unwrap().uei.kind != SCX_EXIT_NONE
    }
}

impl Drop for Scheduler<'_> {
    fn drop(&mut self) {
        let _ = self.skel.maps.idle_bitmap.unpin("/sys/fs/bpf/pandemonium/idle_cpus");
        let _ = std::fs::remove_dir("/sys/fs/bpf/pandemonium");
    }
}
