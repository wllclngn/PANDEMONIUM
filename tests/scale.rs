// PANDEMONIUM SCALING BENCHMARK
// A/B TEST: EEVDF VS PANDEMONIUM ACROSS CORE COUNTS VIA CPU HOTPLUG
//
// REQUIRES ROOT + SCHED_EXT KERNEL.
// RUN: sudo cargo test --test scale --release -- --ignored --test-threads=1
//
// TESTS [1, 2, 4, 8, MAX] CORE COUNTS. AT EACH POINT:
//   PHASE 1: EEVDF BASELINE (STRESS WORKERS + INTERACTIVE PROBE)
//   PHASE 2: PANDEMONIUM (SAME WORKLOAD, SCHEDULER LOADED WITH --nr-cpus N)
//
// REPORTS MEDIAN, P99, AND WORST WAKEUP LATENCY WITH DELTAS.

use std::fs;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const LOG_DIR: &str = "/tmp/pandemonium";
const DURATION_SECS: u64 = 15;

// ---------------------------------------------------------------------------
// CPU HOTPLUG
// ---------------------------------------------------------------------------

fn set_cpu_online(cpu: u32, online: bool) -> Result<(), String> {
    if cpu == 0 {
        return Ok(()); // CPU 0 CANNOT BE OFFLINED
    }
    let path = format!("/sys/devices/system/cpu/cpu{}/online", cpu);
    fs::write(&path, if online { "1" } else { "0" })
        .map_err(|e| format!("FAILED TO SET CPU {} {}: {}", cpu, if online { "ONLINE" } else { "OFFLINE" }, e))
}

fn restrict_cpus(count: u32, max: u32) -> Result<(), String> {
    for cpu in count..max {
        set_cpu_online(cpu, false)?;
    }
    Ok(())
}

fn restore_all_cpus(max: u32) {
    for cpu in 1..max {
        let _ = set_cpu_online(cpu, true);
    }
}

fn parse_cpu_range(path: &str) -> u32 {
    let raw = fs::read_to_string(path).unwrap_or_default();
    let mut count = 0u32;
    for range in raw.trim().split(',') {
        let parts: Vec<&str> = range.split('-').collect();
        match parts.len() {
            1 => {
                if parts[0].parse::<u32>().is_ok() {
                    count += 1;
                }
            }
            2 => {
                if let (Ok(lo), Ok(hi)) = (parts[0].parse::<u32>(), parts[1].parse::<u32>()) {
                    count += hi - lo + 1;
                }
            }
            _ => {}
        }
    }
    count
}

fn parse_online_cpus() -> u32 {
    parse_cpu_range("/sys/devices/system/cpu/online")
}

fn parse_possible_cpus() -> u32 {
    parse_cpu_range("/sys/devices/system/cpu/possible")
}

struct CpuGuard {
    max: u32,
}

impl Drop for CpuGuard {
    fn drop(&mut self) {
        restore_all_cpus(self.max);
    }
}

struct ProcGuard {
    child: Option<Child>,
    pgid: i32,
}

impl ProcGuard {
    fn new(child: Child) -> Self {
        let pgid = child.id() as i32;
        Self {
            child: Some(child),
            pgid,
        }
    }

    fn id(&self) -> i32 {
        self.pgid
    }

    fn stop(&mut self) {
        let child = match self.child.as_mut() {
            Some(c) => c,
            None => return,
        };
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        unsafe { libc::killpg(self.pgid, libc::SIGINT); }
        let deadline = Instant::now() + Duration::from_millis(500);
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) if Instant::now() >= deadline => break,
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        unsafe { libc::killpg(self.pgid, libc::SIGKILL); }
        let _ = child.wait();
    }

    fn into_child(mut self) -> Child {
        self.child.take().expect("ProcGuard: child consumed")
    }
}

impl Drop for ProcGuard {
    fn drop(&mut self) {
        if self.child.is_some() {
            self.stop();
        }
    }
}

// ---------------------------------------------------------------------------
// CPU IDLE DETECTION
// ---------------------------------------------------------------------------

struct CpuTimes {
    idle: u64,
    total: u64,
}

fn read_proc_stat() -> Vec<CpuTimes> {
    let raw = fs::read_to_string("/proc/stat").unwrap_or_default();
    let mut cpus = Vec::new();
    for line in raw.lines() {
        if !line.starts_with("cpu") || line.starts_with("cpu ") {
            continue;
        }
        let fields: Vec<u64> = line.split_whitespace()
            .skip(1)
            .filter_map(|s| s.parse().ok())
            .collect();
        if fields.len() < 4 {
            continue;
        }
        // idle + iowait
        let idle = fields[3] + fields.get(4).copied().unwrap_or(0);
        let total: u64 = fields.iter().sum();
        cpus.push(CpuTimes { idle, total });
    }
    cpus
}

fn detect_idle_cpus_procstat(threshold_pct: f64) -> Vec<u32> {
    let t1 = read_proc_stat();
    std::thread::sleep(Duration::from_millis(200));
    let t2 = read_proc_stat();

    let mut idle_cpus = Vec::new();
    for (i, (a, b)) in t1.iter().zip(t2.iter()).enumerate() {
        let total_delta = b.total.saturating_sub(a.total);
        if total_delta == 0 {
            continue;
        }
        let idle_delta = b.idle.saturating_sub(a.idle);
        let idle_pct = idle_delta as f64 / total_delta as f64 * 100.0;
        if idle_pct >= threshold_pct {
            idle_cpus.push(i as u32);
        }
    }
    idle_cpus
}

fn detect_idle_cpus_bpf(probe_exe: &str) -> Option<Vec<u32>> {
    let output = Command::new(probe_exe)
        .arg("idle-cpus")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    Some(
        stdout.split_whitespace()
            .filter_map(|s| s.parse::<u32>().ok())
            .collect(),
    )
}

fn detect_idle_cpus(probe_exe: &str, use_bpf: bool) -> Vec<u32> {
    if use_bpf {
        if let Some(cpus) = detect_idle_cpus_bpf(probe_exe) {
            if !cpus.is_empty() {
                return cpus;
            }
        }
    }
    detect_idle_cpus_procstat(80.0)
}

// ---------------------------------------------------------------------------
// STRESS WORKERS (PINNED TO SPECIFIC CPUs)
// ---------------------------------------------------------------------------

fn spawn_stress_workers(cpus: &[u32]) -> (Arc<AtomicBool>, Vec<std::thread::JoinHandle<()>>) {
    let running = Arc::new(AtomicBool::new(true));
    let mut handles = Vec::with_capacity(cpus.len());
    for &cpu in cpus {
        let r = running.clone();
        handles.push(std::thread::spawn(move || {
            unsafe {
                let mut set: libc::cpu_set_t = std::mem::zeroed();
                libc::CPU_SET(cpu as usize, &mut set);
                libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &set);
            }
            let mut x: u64 = 1;
            while r.load(Ordering::Relaxed) {
                x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
            }
            std::hint::black_box(x);
        }));
    }
    (running, handles)
}

fn stop_stress_workers(running: Arc<AtomicBool>, handles: Vec<std::thread::JoinHandle<()>>) {
    running.store(false, Ordering::Relaxed);
    for h in handles {
        let _ = h.join();
    }
}

// ---------------------------------------------------------------------------
// SCHEDULER HELPERS
// ---------------------------------------------------------------------------

fn binary_path() -> String {
    let target_dir = std::env::var("CARGO_TARGET_DIR")
        .unwrap_or_else(|_| "/tmp/pandemonium-build".to_string());
    format!("{}/release/pandemonium", target_dir)
}

fn is_scx_active() -> bool {
    fs::read_to_string("/sys/kernel/sched_ext/root/ops")
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false)
}

fn wait_for_activation(timeout_secs: u64) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed().as_secs() < timeout_secs {
        if is_scx_active() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

fn start_scheduler(extra_args: &[String]) -> Result<ProcGuard, String> {
    let bin = binary_path();
    let mut args = vec!["run".to_string()];
    args.extend(extra_args.iter().cloned());

    let child = Command::new("sudo")
        .arg(&bin)
        .args(&args)
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("FAILED TO START SCHEDULER: {}", e))?;
    Ok(ProcGuard::new(child))
}

fn stop_scheduler(guard: &mut ProcGuard) {
    guard.stop();
}

// ---------------------------------------------------------------------------
// LATENCY COLLECTION
// ---------------------------------------------------------------------------

fn percentile(sorted_vals: &[f64], p: f64) -> f64 {
    if sorted_vals.is_empty() {
        return 0.0;
    }
    let idx = (sorted_vals.len() as f64 * p / 100.0) as usize;
    let idx = idx.min(sorted_vals.len() - 1);
    sorted_vals[idx]
}

const WARMUP_SECS: u64 = 3;

fn spawn_probe(probe_exe: &str) -> Result<ProcGuard, String> {
    let proc = unsafe {
        Command::new(probe_exe)
            .arg("probe")
            .process_group(0)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong);
                Ok(())
            })
            .spawn()
            .map_err(|e| format!("FAILED TO START PROBE: {}", e))?
    };
    Ok(ProcGuard::new(proc))
}

fn kill_probe(guard: ProcGuard) -> Result<String, String> {
    unsafe {
        libc::killpg(guard.id() as i32, libc::SIGTERM);
    }
    let child = guard.into_child();
    let output = child
        .wait_with_output()
        .map_err(|e| format!("PROBE WAIT FAILED: {}", e))?;
    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

fn run_scale_phase(
    probe_exe: &str,
    stress_cpus: &[u32],
    duration_secs: u64,
) -> Result<(usize, f64, f64, f64), String> {
    let (stress_running, stress_handles) = spawn_stress_workers(stress_cpus);

    // WARMUP: RUN THROWAWAY PROBE FOR 3s TO LET EWMA/BPF STABILIZE
    let warmup_probe = spawn_probe(probe_exe)?;
    std::thread::sleep(Duration::from_secs(WARMUP_SECS));
    let _ = kill_probe(warmup_probe);

    // MEASUREMENT: FRESH PROBE, CLEAN DATA
    let probe_guard = spawn_probe(probe_exe)?;
    std::thread::sleep(Duration::from_secs(duration_secs));
    let probe_stdout = kill_probe(probe_guard)?;

    stop_stress_workers(stress_running, stress_handles);

    let mut overshoots: Vec<f64> = probe_stdout
        .lines()
        .filter_map(|line| line.trim().parse::<f64>().ok())
        .collect();
    overshoots.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let samples = overshoots.len();
    let med = percentile(&overshoots, 50.0);
    let p99 = percentile(&overshoots, 99.0);
    let worst = overshoots.last().copied().unwrap_or(0.0);

    Ok((samples, med, p99, worst))
}

// ---------------------------------------------------------------------------
// REPORT
// ---------------------------------------------------------------------------

fn save_report(content: &str) -> Result<String, String> {
    fs::create_dir_all(LOG_DIR).map_err(|e| format!("MKDIR FAILED: {}", e))?;
    let stamp = Command::new("date")
        .arg("+%Y%m%d-%H%M%S")
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                Some(String::from_utf8_lossy(&o.stdout).trim().to_string())
            } else {
                None
            }
        })
        .unwrap_or_else(|| "unknown".to_string());
    let path = format!("{}/scale-{}.log", LOG_DIR, stamp);
    fs::write(&path, content).map_err(|e| format!("WRITE FAILED: {}", e))?;
    Ok(path)
}

// ---------------------------------------------------------------------------
// SCALING TEST
// ---------------------------------------------------------------------------

struct ScaleResult {
    cores: u32,
    lightweight: bool,
    slice_ceil_ms: u64,
    preempt_thresh: u64,
    eevdf_samples: usize,
    eevdf_median: f64,
    eevdf_p99: f64,
    eevdf_worst: f64,
    pand_samples: usize,
    pand_median: f64,
    pand_p99: f64,
    pand_worst: f64,
}

#[test]
#[ignore]
fn scaling_benchmark() {
    let sep = "=".repeat(60);
    println!("{}", sep);
    println!("PANDEMONIUM SCALING BENCHMARK (A/B)");
    println!("{}", sep);
    println!();

    assert_eq!(
        unsafe { libc::geteuid() },
        0,
        "SCALING BENCHMARK REQUIRES ROOT (CPU HOTPLUG + BPF)"
    );
    assert!(
        !is_scx_active(),
        "SCHED_EXT IS ALREADY ACTIVE. STOP IT BEFORE BENCHMARKING."
    );

    // RESTORE ALL CPUs FIRST (PREVIOUS KILLED RUN MAY HAVE LEFT THEM OFFLINE)
    let possible = parse_possible_cpus();
    restore_all_cpus(possible);
    std::thread::sleep(Duration::from_millis(500));

    let max_cpus = parse_online_cpus();
    assert!(max_cpus >= 2, "SCALING BENCHMARK REQUIRES AT LEAST 2 CPUs");
    println!("RESTORED {} CPUs (possible: {})", max_cpus, possible);

    // TEST POINTS: POWERS OF 2 UP TO MAX, PLUS MAX ITSELF
    let mut points: Vec<u32> = vec![1, 2, 4, 8, 16, 32, 64]
        .into_iter()
        .filter(|&n| n <= max_cpus)
        .collect();
    if !points.contains(&max_cpus) {
        points.push(max_cpus);
    }

    println!("ONLINE CPUs: {}", max_cpus);
    println!("TEST POINTS: {:?}", points);
    println!(
        "DURATION:    {}s PER PHASE, 2 PHASES PER POINT",
        DURATION_SECS
    );
    println!(
        "TOTAL:       ~{}s + OVERHEAD",
        points.len() as u64 * DURATION_SECS * 2
    );
    println!();

    // COPY BINARY TO SAFE LOCATION
    let probe_exe = format!("{}/probe", LOG_DIR);
    fs::create_dir_all(LOG_DIR).expect("FAILED TO CREATE LOG DIR");
    let self_exe =
        std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from(binary_path()));
    // USE THE BUILT BINARY, NOT THE TEST HARNESS
    let bin = binary_path();
    fs::copy(&bin, &probe_exe).unwrap_or_else(|_| {
        fs::copy(&self_exe, &probe_exe).expect("FAILED TO COPY PROBE BINARY")
    });

    let _guard = CpuGuard { max: max_cpus };
    let mut results: Vec<ScaleResult> = Vec::new();

    for &n in &points {
        println!("{}", "-".repeat(40));
        println!("TESTING {} CORE{}", n, if n == 1 { "" } else { "S" });
        println!("{}", "-".repeat(40));

        // RESTRICT CPUs
        if n < max_cpus {
            restrict_cpus(n, max_cpus).expect("CPU HOTPLUG FAILED");
            std::thread::sleep(Duration::from_millis(500));
        }

        let online = parse_online_cpus();
        println!("  ONLINE: {} CPUs", online);

        // COMPUTE EXPECTED SCALING PARAMS (MIRROR BPF FORMULAS)
        let lightweight = n <= 4;
        let sm = (20_000_000u64 * n as u64) >> 3;
        let slice_ceil = sm.max(5_000_000).min(80_000_000);
        let pt = 60u64 / (n as u64 + 2);
        let preempt_thresh = pt.max(3).min(20);

        println!(
            "  MODE: {}  SLICE_CEIL: {}ms  PREEMPT_THRESH: {}",
            if lightweight { "LIGHTWEIGHT" } else { "FULL" },
            slice_ceil / 1_000_000,
            preempt_thresh
        );

        // PHASE 1: EEVDF BASELINE
        let idle = detect_idle_cpus(&probe_exe, false);
        let e_worker_count = idle.len().saturating_sub(1);
        let e_stress_cpus: Vec<u32> = idle[..e_worker_count].to_vec();
        println!(
            "  PHASE 1: EEVDF ({}s)  IDLE: {}  STRESSING: {} CPUs {:?}",
            DURATION_SECS, idle.len(), e_stress_cpus.len(), e_stress_cpus
        );
        let (e_samples, e_med, e_p99, e_worst) =
            run_scale_phase(&probe_exe, &e_stress_cpus, DURATION_SECS).expect("EEVDF PHASE FAILED");
        println!(
            "    SAMPLES: {}  MEDIAN: {:.0}us  P99: {:.0}us  WORST: {:.0}us",
            e_samples, e_med, e_p99, e_worst
        );

        // PHASE 2: PANDEMONIUM
        let sched_args = vec!["--nr-cpus".to_string(), n.to_string()];
        let mut pand_proc = match start_scheduler(&sched_args) {
            Ok(c) => c,
            Err(e) => {
                println!("  FAILED TO START SCHEDULER: {} -- SKIPPING", e);
                restore_all_cpus(max_cpus);
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
        };

        if !wait_for_activation(10) {
            println!("  SCHEDULER DID NOT ACTIVATE -- SKIPPING");
            stop_scheduler(&mut pand_proc);
            restore_all_cpus(max_cpus);
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }
        std::thread::sleep(Duration::from_secs(5));

        let idle = detect_idle_cpus(&probe_exe, true);
        let p_worker_count = idle.len().saturating_sub(1);
        let p_stress_cpus: Vec<u32> = idle[..p_worker_count].to_vec();
        println!(
            "  PHASE 2: PANDEMONIUM ({}s)  IDLE: {}  STRESSING: {} CPUs {:?}",
            DURATION_SECS, idle.len(), p_stress_cpus.len(), p_stress_cpus
        );
        let (p_samples, p_med, p_p99, p_worst) =
            run_scale_phase(&probe_exe, &p_stress_cpus, DURATION_SECS).expect("PANDEMONIUM PHASE FAILED");
        println!(
            "    SAMPLES: {}  MEDIAN: {:.0}us  P99: {:.0}us  WORST: {:.0}us",
            p_samples, p_med, p_p99, p_worst
        );

        stop_scheduler(&mut pand_proc);
        restore_all_cpus(max_cpus);
        std::thread::sleep(Duration::from_millis(500));
        println!();

        results.push(ScaleResult {
            cores: n,
            lightweight,
            slice_ceil_ms: slice_ceil / 1_000_000,
            preempt_thresh,
            eevdf_samples: e_samples,
            eevdf_median: e_med,
            eevdf_p99: e_p99,
            eevdf_worst: e_worst,
            pand_samples: p_samples,
            pand_median: p_med,
            pand_p99: p_p99,
            pand_worst: p_worst,
        });
    }

    assert!(!results.is_empty(), "NO SCALE POINTS COMPLETED");

    // REPORT
    let mut report = Vec::new();
    report.push(sep.clone());
    report.push("PANDEMONIUM SCALING BENCHMARK (A/B VS EEVDF)".to_string());
    report.push(sep.clone());
    report.push(format!(
        "WORKLOAD: N STRESS WORKERS + INTERACTIVE PROBE ({}s PER PHASE, {} CPUs MAX)",
        DURATION_SECS, max_cpus
    ));
    report.push(String::new());

    // EEVDF TABLE
    report.push("EEVDF (DEFAULT SCHEDULER)".to_string());
    report.push(format!(
        "{:>5} {:>8} {:>8} {:>8} {:>8}",
        "CORES", "SAMPLES", "MEDIAN", "P99", "WORST"
    ));
    report.push(format!(
        "{} {} {} {} {}",
        "-".repeat(5),
        "-".repeat(8),
        "-".repeat(8),
        "-".repeat(8),
        "-".repeat(8),
    ));
    for r in &results {
        report.push(format!(
            "{:>5} {:>8} {:>7.0}us {:>7.0}us {:>7.0}us",
            r.cores, r.eevdf_samples, r.eevdf_median, r.eevdf_p99, r.eevdf_worst,
        ));
    }
    report.push(String::new());

    // PANDEMONIUM TABLE
    report.push("PANDEMONIUM".to_string());
    report.push(format!(
        "{:>5} {:>14} {:>10} {:>8} {:>8} {:>8} {:>8} {:>8}",
        "CORES", "MODE", "SLICE_CEIL", "PREEMPT", "SAMPLES", "MEDIAN", "P99", "WORST"
    ));
    report.push(format!(
        "{} {} {} {} {} {} {} {}",
        "-".repeat(5),
        "-".repeat(14),
        "-".repeat(10),
        "-".repeat(8),
        "-".repeat(8),
        "-".repeat(8),
        "-".repeat(8),
        "-".repeat(8),
    ));
    for r in &results {
        report.push(format!(
            "{:>5} {:>14} {:>9}ms {:>8} {:>8} {:>7.0}us {:>7.0}us {:>7.0}us",
            r.cores,
            if r.lightweight { "LIGHTWEIGHT" } else { "FULL" },
            r.slice_ceil_ms,
            r.preempt_thresh,
            r.pand_samples,
            r.pand_median,
            r.pand_p99,
            r.pand_worst,
        ));
    }
    report.push(String::new());

    // DELTA TABLE
    report.push("DELTA (NEGATIVE = PANDEMONIUM IS BETTER)".to_string());
    report.push(format!(
        "{:>5} {:>10} {:>10} {:>10}",
        "CORES", "MEDIAN", "P99", "WORST"
    ));
    report.push(format!(
        "{} {} {} {}",
        "-".repeat(5),
        "-".repeat(10),
        "-".repeat(10),
        "-".repeat(10),
    ));
    for r in &results {
        let med_d = r.pand_median - r.eevdf_median;
        let p99_d = r.pand_p99 - r.eevdf_p99;
        let worst_d = r.pand_worst - r.eevdf_worst;
        report.push(format!(
            "{:>5} {:>+9.0}us {:>+9.0}us {:>+9.0}us",
            r.cores, med_d, p99_d, worst_d,
        ));
    }
    report.push(sep.clone());

    let report_text = report.join("\n") + "\n";
    for line in &report {
        println!("{}", line);
    }

    let path = save_report(&report_text).expect("FAILED TO SAVE REPORT");
    println!("\nSAVED TO {}", path);
}
