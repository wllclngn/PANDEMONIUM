// PANDEMONIUM v1.0 -- SCHED_EXT KERNEL SCHEDULER
// ADAPTIVE DESKTOP SCHEDULING FOR LINUX
//
// SCHEDULING DECISIONS HAPPEN IN BPF (ZERO KERNEL-USERSPACE ROUND TRIPS)
// RUST USERSPACE HANDLES: ADAPTIVE CONTROL LOOP, MONITORING, BENCHMARKING

#[allow(non_upper_case_globals)]
#[allow(non_camel_case_types)]
#[allow(non_snake_case)]
#[allow(dead_code)]
mod bpf_skel;

mod adaptive;
mod cli;
mod scheduler;

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use clap::{Parser, Subcommand};

use scheduler::Scheduler;

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[derive(Parser)]
#[command(name = "pandemonium")]
#[command(about = "PANDEMONIUM -- ADAPTIVE LINUX SCHEDULER")]
struct Cli {
    #[command(subcommand)]
    command: Option<SubCmd>,
}

#[derive(Subcommand)]
enum SubCmd {
    /// Run the scheduler (needs root + sched_ext kernel)
    Run(RunArgs),

    /// Check dependencies and kernel config
    Check,

    /// Run interactive wakeup probe (stdout: overshoot_us per line)
    Probe(ProbeArgs),

    /// Build, run with sudo, capture output + dmesg, save logs
    Start(StartArgs),

    /// Show filtered kernel dmesg for sched_ext/pandemonium
    Dmesg,

    /// A/B benchmark (EEVDF baseline vs PANDEMONIUM)
    Bench(BenchArgs),

    /// Build release then run bench (logs to /tmp/pandemonium)
    BenchRun(BenchRunArgs),

    /// Run test gate (unit + integration)
    Test,

    /// A/B scaling benchmark (EEVDF vs PANDEMONIUM across core counts)
    TestScale,

    /// Print idle CPU bitmask (requires running PANDEMONIUM scheduler)
    IdleCpus,
}

#[derive(Parser)]
struct RunArgs {
    #[arg(long)]
    verbose: bool,

    #[arg(long)]
    dump_log: bool,

    /// Override CPU count for scaling formulas (default: auto-detect)
    #[arg(long)]
    nr_cpus: Option<u64>,

    /// Run BPF scheduler only, disable Rust adaptive control loop
    #[arg(long)]
    no_adaptive: bool,
}

#[derive(Parser)]
struct ProbeArgs {
    /// Death pipe FD for orphan detection (internal use)
    #[arg(long)]
    death_pipe_fd: Option<i32>,
}

#[derive(Parser)]
struct StartArgs {
    /// Run with --verbose --dump-log
    #[arg(long)]
    observe: bool,

    /// Extra args forwarded to `pandemonium run`
    #[arg(last = true)]
    sched_args: Vec<String>,
}

#[derive(Parser)]
struct BenchArgs {
    /// Benchmark mode
    #[arg(long, value_enum)]
    mode: cli::bench::BenchMode,

    /// Command to benchmark (for --mode cmd)
    #[arg(long)]
    cmd: Option<String>,

    /// Number of iterations per phase
    #[arg(long, default_value_t = 3)]
    iterations: usize,

    /// Clean command between iterations (for --mode cmd)
    #[arg(long)]
    clean_cmd: Option<String>,

    /// Extra args forwarded to `pandemonium run`
    #[arg(last = true)]
    sched_args: Vec<String>,
}

#[derive(Parser)]
struct BenchRunArgs {
    /// Benchmark mode
    #[arg(long, value_enum)]
    mode: cli::bench::BenchMode,

    /// Command to benchmark (for --mode cmd)
    #[arg(long)]
    cmd: Option<String>,

    /// Number of iterations per phase
    #[arg(long, default_value_t = 3)]
    iterations: usize,

    /// Clean command between iterations (for --mode cmd)
    #[arg(long)]
    clean_cmd: Option<String>,

    /// Extra args forwarded to `pandemonium run`
    #[arg(last = true)]
    sched_args: Vec<String>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        None | Some(SubCmd::Run(_)) => {
            let args = match cli.command {
                Some(SubCmd::Run(a)) => a,
                _ => RunArgs {
                    verbose: false,
                    dump_log: false,
                    nr_cpus: None,
                    no_adaptive: false,
                },
            };
            run_scheduler(args)
        }
        Some(SubCmd::Check) => cli::check::run_check(),
        Some(SubCmd::Probe(args)) => {
            cli::probe::run_probe(args.death_pipe_fd);
            Ok(())
        }
        Some(SubCmd::Start(args)) => {
            cli::run::run_start(args.observe, &args.sched_args)
        }
        Some(SubCmd::Dmesg) => cli::run::run_dmesg(),
        Some(SubCmd::Bench(args)) => cli::bench::run_bench(
            args.mode,
            args.cmd.as_deref(),
            args.iterations,
            args.clean_cmd.as_deref(),
            &args.sched_args,
        ),
        Some(SubCmd::BenchRun(args)) => cli::bench::run_bench_run(
            args.mode,
            args.cmd.as_deref(),
            args.iterations,
            args.clean_cmd.as_deref(),
            &args.sched_args,
        ),
        Some(SubCmd::Test) => cli::test_gate::run_test_gate(),
        Some(SubCmd::TestScale) => cli::test_gate::run_test_scale(),
        Some(SubCmd::IdleCpus) => cli::idle_cpus::run_idle_cpus(),
    }
}

fn run_scheduler(args: RunArgs) -> Result<()> {
    ctrlc::set_handler(move || {
        SHUTDOWN.store(true, Ordering::Relaxed);
    })?;

    let nr_cpus = args.nr_cpus.unwrap_or_else(|| {
        libbpf_rs::num_possible_cpus().unwrap_or(1) as u64
    });
    let governor = std::fs::read_to_string(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    )
    .unwrap_or_default()
    .trim()
    .to_string();

    println!("PANDEMONIUM v1.0");
    println!(
        "CPUS:            {} (governor: {})",
        nr_cpus,
        if governor.is_empty() { "unknown" } else { &governor }
    );
    println!("VERBOSE:         {}", args.verbose);
    println!();

    let mut open_object = MaybeUninit::uninit();

    let adaptive = !args.no_adaptive;

    loop {
        let mut sched = Scheduler::init(
            &mut open_object,
            args.nr_cpus,
            adaptive,
        )?;

        let should_restart = if !adaptive {
            // BPF-ONLY MODE: SCHEDULER RUNS WITH DEFAULT KNOBS, NO RUST TUNING
            // STILL PRINTS STATS SO BENCHMARKS GET TELEMETRY FOR BOTH PHASES
            println!("PANDEMONIUM IS ACTIVE (BPF ONLY, CTRL+C TO EXIT)");
            let mut prev = scheduler::PandemoniumStats::default();
            while !SHUTDOWN.load(Ordering::Relaxed) && !sched.exited() {
                std::thread::sleep(Duration::from_secs(1));

                let stats = sched.read_stats();

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

                let d_idle_sum = stats.wake_lat_idle_sum.wrapping_sub(prev.wake_lat_idle_sum);
                let d_idle_cnt = stats.wake_lat_idle_cnt.wrapping_sub(prev.wake_lat_idle_cnt);
                let d_kick_sum = stats.wake_lat_kick_sum.wrapping_sub(prev.wake_lat_kick_sum);
                let d_kick_cnt = stats.wake_lat_kick_cnt.wrapping_sub(prev.wake_lat_kick_cnt);
                let lat_idle_us = if d_idle_cnt > 0 { d_idle_sum / d_idle_cnt / 1000 } else { 0 };
                let lat_kick_us = if d_kick_cnt > 0 { d_kick_sum / d_kick_cnt / 1000 } else { 0 };
                let delta_guard = stats.nr_guard_clamps.wrapping_sub(prev.nr_guard_clamps);

                let idle_pct = if delta_d > 0 { delta_idle * 100 / delta_d } else { 0 };

                println!(
                    "d/s: {:<8} idle: {}% shared: {:<6} preempt: {:<4} keep: {:<4} kick: H={:<4} S={:<4} enq: W={:<4} R={:<4} wake: {}us lat_idle: {}us lat_kick: {}us guard: {} [BPF]",
                    delta_d, idle_pct, delta_shared, delta_preempt, delta_keep,
                    delta_hard, delta_soft, delta_enq_wake, delta_enq_requeue,
                    wake_avg_us, lat_idle_us, lat_kick_us, delta_guard,
                );

                sched.log.snapshot(
                    delta_d, delta_idle, delta_shared,
                    delta_preempt, delta_keep, wake_avg_us,
                    delta_hard, delta_soft, lat_idle_us, lat_kick_us,
                );

                prev = stats;
            }
            sched.read_exit_info()
        } else {
            // FULL MODE: ADAPTIVE CONTROL LOOP WITH REFLEX + MONITOR
            let shared = Arc::new(adaptive::SharedState::new());
            let ring_buf = adaptive::build_ring_buffer(&sched, Arc::clone(&shared))?;
            let knobs_handle = Scheduler::knobs_map_handle()?;

            let shared_reflex = Arc::clone(&shared);
            let reflex_handle = std::thread::Builder::new()
                .name("reflex".into())
                .spawn(move || {
                    adaptive::reflex_thread(ring_buf, shared_reflex, knobs_handle, &SHUTDOWN);
                })?;

            println!("PANDEMONIUM IS ACTIVE (CTRL+C TO EXIT)");
            let restart = adaptive::monitor_loop(&mut sched, &shared, &SHUTDOWN)?;
            // SIGNAL REFLEX THREAD TO EXIT (MONITOR MAY RETURN FROM BPF EXIT,
            // NOT JUST CTRL+C -- WITHOUT THIS, reflex_handle.join() DEADLOCKS)
            SHUTDOWN.store(true, Ordering::Relaxed);
            let _ = reflex_handle.join();
            restart
        };

        println!("PANDEMONIUM IS SHUTTING DOWN");

        if args.dump_log {
            sched.log.dump();
        }
        sched.log.summary();

        if !should_restart || SHUTDOWN.load(Ordering::Relaxed) {
            break;
        }

        // RESET SHUTDOWN FOR RESTART
        SHUTDOWN.store(false, Ordering::Relaxed);
        println!("RESTARTING PANDEMONIUM...\n");
    }

    println!("PANDEMONIUM OUT.");
    Ok(())
}
