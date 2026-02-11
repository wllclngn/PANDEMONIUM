// PANDEMONIUM v1.0.0 -- SCHED_EXT KERNEL SCHEDULER
// BEHAVIORAL-ADAPTIVE GENERAL-PURPOSE SCHEDULING FOR LINUX
//
// SCHEDULING DECISIONS HAPPEN IN BPF (ZERO KERNEL-USERSPACE ROUND TRIPS)
// RUST USERSPACE HANDLES: CONFIGURATION, MONITORING, REPORTING

#[allow(non_upper_case_globals)]
#[allow(non_camel_case_types)]
#[allow(non_snake_case)]
#[allow(dead_code)]
mod bpf_skel;

mod event;
mod scheduler;

use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::Result;
use clap::Parser;

use scheduler::Scheduler;

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[derive(Parser)]
#[command(name = "pandemonium")]
#[command(about = "PANDEMONIUM -- BEHAVIORAL-ADAPTIVE LINUX SCHEDULER")]
struct Cli {
    // BUILD MODE: BOOST COMPILER PROCESS WEIGHTS (OPT-IN)
    #[arg(long)]
    build_mode: bool,

    // BASE TIME SLICE IN NANOSECONDS (5MS DEFAULT)
    #[arg(long, default_value_t = 5_000_000)]
    slice_ns: u64,

    // MINIMUM SLICE IN NANOSECONDS (0.5MS -- INTERACTIVE FLOOR)
    #[arg(long, default_value_t = 500_000)]
    slice_min: u64,

    // MAXIMUM SLICE IN NANOSECONDS (20MS -- COMPILER CEILING)
    #[arg(long, default_value_t = 20_000_000)]
    slice_max: u64,

    // PRINT VERBOSE OUTPUT
    #[arg(long)]
    verbose: bool,

    // DUMP FULL EVENT LOG ON EXIT
    #[arg(long)]
    dump_log: bool,

    // CALIBRATE: COLLECT LAT_CRI HISTOGRAM FOR 30S AND SUGGEST THRESHOLDS
    #[arg(long)]
    calibrate: bool,

    // FORCE LIGHTWEIGHT MODE (SKIP FULL CLASSIFICATION ENGINE)
    #[arg(long)]
    lightweight: bool,

    // FORCE FULL ENGINE EVEN ON FEW CORES (OVERRIDE AUTO-LIGHTWEIGHT)
    #[arg(long)]
    no_lightweight: bool,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    ctrlc::set_handler(move || {
        SHUTDOWN.store(true, Ordering::Relaxed);
    })?;

    // DETECT TOPOLOGY
    let nr_cpus = libbpf_rs::num_possible_cpus().unwrap_or(1) as u64;
    let governor = std::fs::read_to_string(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
    ).unwrap_or_default().trim().to_string();

    // RESOLVE LIGHTWEIGHT MODE
    let lightweight = if cli.no_lightweight {
        false
    } else if cli.lightweight {
        true
    } else {
        nr_cpus <= 4
    };

    println!("PANDEMONIUM v1.0.0");
    println!("CPUS:            {} (governor: {})", nr_cpus,
             if governor.is_empty() { "unknown" } else { &governor });
    println!("BUILD MODE:      {}", cli.build_mode);
    println!("LIGHTWEIGHT:     {}{}", lightweight,
             if !cli.lightweight && !cli.no_lightweight && lightweight {
                 " (auto: <=4 cores)"
             } else { "" });
    println!("SLICE:           {} ns (min={}, max={})", cli.slice_ns, cli.slice_min, cli.slice_max);
    println!("VERBOSE:         {}", cli.verbose);
    if cli.calibrate {
        println!("MODE:            CALIBRATE");
    }
    println!();

    let mut open_object = MaybeUninit::uninit();

    // CALIBRATION MODE: SPECIAL PATH
    if cli.calibrate {
        let mut sched = Scheduler::init(
            &mut open_object,
            cli.build_mode,
            cli.slice_ns,
            cli.slice_min,
            cli.slice_max,
            cli.verbose,
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
            cli.build_mode,
            cli.slice_ns,
            cli.slice_min,
            cli.slice_max,
            cli.verbose,
            lightweight,
        )?;

        println!("PANDEMONIUM IS ACTIVE (CTRL+C TO EXIT)");

        let should_restart = sched.run(&SHUTDOWN)?;

        println!("PANDEMONIUM IS SHUTTING DOWN");

        if cli.dump_log {
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
