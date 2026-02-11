// PANDEMONIUM v0.9.3 -- SCHED_EXT KERNEL SCHEDULER
// BEHAVIORAL-ADAPTIVE GENERAL-PURPOSE SCHEDULING FOR LINUX
//
// SCHEDULING DECISIONS HAPPEN IN BPF (ZERO KERNEL-USERSPACE ROUND TRIPS)
// RUST USERSPACE HANDLES: CONFIGURATION, MONITORING, REPORTING, BENCHMARKING

#[allow(non_upper_case_globals)]
#[allow(non_camel_case_types)]
#[allow(non_snake_case)]
#[allow(dead_code)]
mod bpf_skel;

mod cli;
mod event;
mod scheduler;

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::Result;
use clap::{Parser, Subcommand};

use scheduler::Scheduler;

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[derive(Parser)]
#[command(name = "pandemonium")]
#[command(about = "PANDEMONIUM -- BEHAVIORAL-ADAPTIVE LINUX SCHEDULER")]
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
    Probe,

    /// Build, run with sudo, capture output + dmesg, save logs
    Start(StartArgs),

    /// Show filtered kernel dmesg for sched_ext/pandemonium
    Dmesg,

    /// A/B benchmark (EEVDF baseline vs PANDEMONIUM)
    Bench(BenchArgs),

    /// Run test gate (unit + integration)
    Test,
}

#[derive(Parser)]
struct RunArgs {
    #[arg(long)]
    build_mode: bool,

    #[arg(long, default_value_t = 5_000_000)]
    slice_ns: u64,

    #[arg(long, default_value_t = 500_000)]
    slice_min: u64,

    #[arg(long, default_value_t = 20_000_000)]
    slice_max: u64,

    #[arg(long)]
    verbose: bool,

    #[arg(long)]
    dump_log: bool,

    #[arg(long)]
    calibrate: bool,

    #[arg(long)]
    lightweight: bool,

    #[arg(long)]
    no_lightweight: bool,
}

#[derive(Parser)]
struct StartArgs {
    /// Run with --verbose --dump-log
    #[arg(long)]
    observe: bool,

    /// Run calibration mode
    #[arg(long)]
    calibrate: bool,

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

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        None | Some(SubCmd::Run(_)) => {
            let args = match cli.command {
                Some(SubCmd::Run(a)) => a,
                _ => RunArgs {
                    build_mode: false,
                    slice_ns: 5_000_000,
                    slice_min: 500_000,
                    slice_max: 20_000_000,
                    verbose: false,
                    dump_log: false,
                    calibrate: false,
                    lightweight: false,
                    no_lightweight: false,
                },
            };
            run_scheduler(args)
        }
        Some(SubCmd::Check) => cli::check::run_check(),
        Some(SubCmd::Probe) => {
            cli::probe::run_probe();
            Ok(())
        }
        Some(SubCmd::Start(args)) => {
            cli::run::run_start(args.observe, args.calibrate, &args.sched_args)
        }
        Some(SubCmd::Dmesg) => cli::run::run_dmesg(),
        Some(SubCmd::Bench(args)) => cli::bench::run_bench(
            args.mode,
            args.cmd.as_deref(),
            args.iterations,
            args.clean_cmd.as_deref(),
            &args.sched_args,
        ),
        Some(SubCmd::Test) => cli::test_gate::run_test_gate(),
    }
}

fn run_scheduler(args: RunArgs) -> Result<()> {
    ctrlc::set_handler(move || {
        SHUTDOWN.store(true, Ordering::Relaxed);
    })?;

    let nr_cpus = libbpf_rs::num_possible_cpus().unwrap_or(1) as u64;
    let governor = std::fs::read_to_string(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
    )
    .unwrap_or_default()
    .trim()
    .to_string();

    let lightweight = if args.no_lightweight {
        false
    } else if args.lightweight {
        true
    } else {
        nr_cpus <= 4
    };

    println!("PANDEMONIUM v0.9.3");
    println!(
        "CPUS:            {} (governor: {})",
        nr_cpus,
        if governor.is_empty() { "unknown" } else { &governor }
    );
    println!("BUILD MODE:      {}", args.build_mode);
    println!(
        "LIGHTWEIGHT:     {}{}",
        lightweight,
        if !args.lightweight && !args.no_lightweight && lightweight {
            " (auto: <=4 cores)"
        } else {
            ""
        }
    );
    println!(
        "SLICE:           {} ns (min={}, max={})",
        args.slice_ns, args.slice_min, args.slice_max
    );
    println!("VERBOSE:         {}", args.verbose);
    if args.calibrate {
        println!("MODE:            CALIBRATE");
    }
    println!();

    let mut open_object = MaybeUninit::uninit();

    if args.calibrate {
        let mut sched = Scheduler::init(
            &mut open_object,
            args.build_mode,
            args.slice_ns,
            args.slice_min,
            args.slice_max,
            args.verbose,
            lightweight,
        )?;

        println!("PANDEMONIUM IS ACTIVE (CALIBRATING)");
        sched.calibrate(&SHUTDOWN)?;
        println!("PANDEMONIUM OUT.");
        return Ok(());
    }

    loop {
        let mut sched = Scheduler::init(
            &mut open_object,
            args.build_mode,
            args.slice_ns,
            args.slice_min,
            args.slice_max,
            args.verbose,
            lightweight,
        )?;

        println!("PANDEMONIUM IS ACTIVE (CTRL+C TO EXIT)");

        let should_restart = sched.run(&SHUTDOWN)?;

        println!("PANDEMONIUM IS SHUTTING DOWN");

        if args.dump_log {
            sched.log.dump();
        }
        sched.log.summary();

        if !should_restart || SHUTDOWN.load(Ordering::Relaxed) {
            break;
        }

        println!("RESTARTING PANDEMONIUM...\n");
    }

    println!("PANDEMONIUM OUT.");
    Ok(())
}
