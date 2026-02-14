// PANDEMONIUM SCALING BENCHMARK
// A/B TEST: EEVDF VS PANDEMONIUM ACROSS CORE COUNTS VIA CPU HOTPLUG
//
// REQUIRES ROOT + SCHED_EXT KERNEL.
// RUN: sudo cargo test --test scale --release -- --ignored --test-threads=1
//
// TESTS [2, 4, 8, MAX] CORE COUNTS. AT EACH POINT:
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
// LOGGING (mirrors arch-update.py / ABRAXAS pattern)
// ---------------------------------------------------------------------------

fn _timestamp() -> String {
    unsafe {
        let mut t: libc::time_t = 0;
        libc::time(&mut t);
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        format!("[{:02}:{:02}:{:02}]", tm.tm_hour, tm.tm_min, tm.tm_sec)
    }
}

macro_rules! log_info {
    ($($arg:tt)*) => {
        println!("{} [INFO]   {}", _timestamp(), format!($($arg)*));
    };
}

macro_rules! log_warn {
    ($($arg:tt)*) => {
        println!("{} [WARN]   {}", _timestamp(), format!($($arg)*));
    };
}

macro_rules! log_error {
    ($($arg:tt)*) => {
        println!("{} [ERROR]  {}", _timestamp(), format!($($arg)*));
    };
}

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

// KILL STALE PANDEMONIUM SCHEDULER PROCESSES FROM PREVIOUS FAILED RUNS
fn kill_stale_schedulers() {
    let output = Command::new("pgrep")
        .args(["-f", "pandemonium run"])
        .output();
    if let Ok(o) = output {
        let stdout = String::from_utf8_lossy(&o.stdout);
        for line in stdout.lines() {
            if let Ok(pid) = line.trim().parse::<i32>() {
                unsafe { libc::kill(pid, libc::SIGKILL); }
            }
        }
        if !stdout.trim().is_empty() {
            std::thread::sleep(Duration::from_millis(500));
        }
    }
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
    let mut args = Vec::new();
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

// STOP SCHEDULER AND CAPTURE OUTPUT
fn stop_and_drain(mut guard: ProcGuard) -> String {
    let pgid = guard.id();
    // SIGNAL STOP VIA SIGINT
    unsafe { libc::killpg(pgid, libc::SIGINT); }
    let deadline = std::time::Instant::now() + Duration::from_millis(2000);
    if let Some(child) = guard.child.as_mut() {
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if std::time::Instant::now() >= deadline => {
                    unsafe { libc::killpg(pgid, libc::SIGKILL); }
                    let _ = child.wait();
                    break;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
    }
    let child = guard.into_child();
    match child.wait_with_output() {
        Ok(output) => String::from_utf8_lossy(&output.stdout).to_string(),
        Err(_) => String::new(),
    }
}

// ---------------------------------------------------------------------------
// EXTERNAL SCX SCHEDULERS
// ---------------------------------------------------------------------------

fn find_scx_schedulers() -> Vec<(String, String)> {
    let names: Vec<String> = if let Ok(val) = std::env::var("PANDEMONIUM_SCX_SCHEDULERS") {
        val.split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    } else {
        // Full field: ["scx_rusty", "scx_bpfland", "scx_lavd", "scx_flash", "scx_layered", "scx_p2dq"]
        vec![
            "scx_bpfland".to_string(),
        ]
    };
    let mut found = Vec::new();
    for name in names {
        if let Ok(output) = Command::new("which").arg(&name).output() {
            if output.status.success() {
                let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if !path.is_empty() {
                    found.push((name, path));
                }
            }
        }
    }
    found
}

fn start_external_scheduler(path: &str) -> Result<ProcGuard, String> {
    let child = Command::new("sudo")
        .arg(path)
        .process_group(0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("FAILED TO START {}: {}", path, e))?;
    Ok(ProcGuard::new(child))
}

fn wait_for_deactivation(timeout_secs: u64) -> bool {
    let start = Instant::now();
    while start.elapsed().as_secs() < timeout_secs {
        if !is_scx_active() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
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

// SYNCHRONOUS: SIGNAL + DRAIN + RETURN OUTPUT
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

// ASYNCHRONOUS: SIGNAL + DRAIN ON BACKGROUND THREAD
// RETURNS JOIN HANDLE -- CALLER COLLECTS OUTPUT LATER
fn kill_probe_async(guard: ProcGuard) -> std::thread::JoinHandle<Result<String, String>> {
    unsafe {
        libc::killpg(guard.id() as i32, libc::SIGTERM);
    }
    let child = guard.into_child();
    std::thread::spawn(move || {
        let output = child
            .wait_with_output()
            .map_err(|e| format!("PROBE WAIT FAILED: {}", e))?;
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    })
}

fn parse_probe_output(stdout: &str) -> (usize, f64, f64, f64) {
    let mut overshoots: Vec<f64> = stdout
        .lines()
        .filter_map(|line| line.trim().parse::<f64>().ok())
        .collect();
    overshoots.sort_by(|a, b| a.partial_cmp(b).unwrap());

    let samples = overshoots.len();
    let med = percentile(&overshoots, 50.0);
    let p99 = percentile(&overshoots, 99.0);
    let worst = overshoots.last().copied().unwrap_or(0.0);
    (samples, med, p99, worst)
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

    let (samples, med, p99, worst) = parse_probe_output(&probe_stdout);
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

struct ExternalResult {
    name: String,
    samples: usize,
    median: f64,
    p99: f64,
    worst: f64,
}

struct ScaleResult {
    cores: u32,
    eevdf_samples: usize,
    eevdf_median: f64,
    eevdf_p99: f64,
    eevdf_worst: f64,
    bpf_samples: usize,
    bpf_median: f64,
    bpf_p99: f64,
    bpf_worst: f64,
    bpf_sched_output: String,
    full_samples: usize,
    full_median: f64,
    full_p99: f64,
    full_worst: f64,
    full_sched_output: String,
    externals: Vec<ExternalResult>,
}

#[test]
#[ignore]
fn scaling_benchmark() {
    log_info!("PANDEMONIUM SCALING BENCHMARK (A/B/C)");

    assert_eq!(
        unsafe { libc::geteuid() },
        0,
        "SCALING BENCHMARK REQUIRES ROOT (CPU HOTPLUG + BPF)"
    );
    // KILL STALE PANDEMONIUM PROCESSES FROM PREVIOUS FAILED RUNS
    kill_stale_schedulers();

    if is_scx_active() {
        // GIVE IT A MOMENT TO DEACTIVATE AFTER KILLING STALE PROCESSES
        std::thread::sleep(Duration::from_secs(2));
    }
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
    log_info!("Restored {} CPUs (possible: {})", max_cpus, possible);

    // TEST POINTS: POWERS OF 2 UP TO MAX, PLUS MAX ITSELF
    let mut points: Vec<u32> = vec![2, 4, 8, 16, 32, 64]
        .into_iter()
        .filter(|&n| n <= max_cpus)
        .collect();
    if !points.contains(&max_cpus) {
        points.push(max_cpus);
    }

    let scx_schedulers = find_scx_schedulers();
    let phases_per_point = 3 + scx_schedulers.len() as u64;

    log_info!("Online CPUs: {}", max_cpus);
    log_info!("Test points: {:?}", points);
    if scx_schedulers.is_empty() {
        log_warn!("No external scx schedulers found");
    } else {
        log_info!(
            "External schedulers: {}",
            scx_schedulers.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>().join(", ")
        );
    }
    log_info!(
        "Duration: {}s per phase, {} phases per point",
        DURATION_SECS, phases_per_point
    );
    log_info!(
        "Estimated: ~{}s + overhead",
        points.len() as u64 * DURATION_SECS * phases_per_point
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
        println!();
        log_info!("TESTING {} CORE{}", n, if n == 1 { "" } else { "S" });

        // RESTRICT CPUs
        if n < max_cpus {
            log_info!("Restricting to {} CPUs via hotplug...", n);
            restrict_cpus(n, max_cpus).expect("CPU HOTPLUG FAILED");
            std::thread::sleep(Duration::from_millis(500));
        }

        let online = parse_online_cpus();
        log_info!("Online: {} CPUs", online);

        // DETERMINISTIC WORKER COUNT: STRESS n-1 CPUs, RESERVE 1 FOR PROBE.
        // ALL THREE PHASES GET IDENTICAL LOAD.
        let worker_count = (n as usize).saturating_sub(1);
        let stress_cpus: Vec<u32> = (0..worker_count as u32).collect();

        // --- PHASE 1: PANDEMONIUM BPF-ONLY (--no-adaptive) ---
        let bpf_args = vec![
            "--nr-cpus".to_string(), n.to_string(),
            "--no-adaptive".to_string(),
        ];
        let mut bpf_proc = match start_scheduler(&bpf_args) {
            Ok(c) => c,
            Err(e) => {
                log_error!("Failed to start scheduler: {} -- skipping", e);
                restore_all_cpus(max_cpus);
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
        };

        if !wait_for_activation(10) {
            log_error!("Scheduler did not activate -- skipping");
            stop_scheduler(&mut bpf_proc);
            restore_all_cpus(max_cpus);
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }
        std::thread::sleep(Duration::from_secs(5));

        if !is_scx_active() {
            log_error!("Scheduler crashed during settlement -- skipping");
            let crash_output = stop_and_drain(bpf_proc);
            for line in crash_output.lines().take(10) {
                let line = line.trim();
                if !line.is_empty() {
                    println!("    SCHED: {}", line);
                }
            }
            restore_all_cpus(max_cpus);
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }

        log_info!(
            "Phase 1: BPF-ONLY ({}s)  Stressing: {} CPUs {:?}",
            DURATION_SECS, stress_cpus.len(), stress_cpus
        );
        let (b_samples, b_med, b_p99, b_worst) =
            run_scale_phase(&probe_exe, &stress_cpus, DURATION_SECS).expect("BPF-ONLY PHASE FAILED");

        let bpf_alive = is_scx_active();
        if !bpf_alive {
            log_warn!("Scheduler crashed during measurement");
        }
        log_info!(
            "  Samples: {}  Median: {:.0}us  P99: {:.0}us  Worst: {:.0}us{}",
            b_samples, b_med, b_p99, b_worst,
            if bpf_alive { "" } else { "  [TAINTED]" }
        );

        let bpf_output = stop_and_drain(bpf_proc);
        kill_stale_schedulers();
        unsafe { libc::sync(); }
        std::thread::sleep(Duration::from_secs(2));

        // --- PHASE 2: PANDEMONIUM FULL (BPF + ADAPTIVE) ---
        let full_args = vec!["--nr-cpus".to_string(), n.to_string()];
        let mut full_proc = match start_scheduler(&full_args) {
            Ok(c) => c,
            Err(e) => {
                log_error!("Failed to start adaptive scheduler: {} -- skipping", e);
                restore_all_cpus(max_cpus);
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
        };

        if !wait_for_activation(10) {
            log_error!("Adaptive scheduler did not activate -- skipping");
            stop_scheduler(&mut full_proc);
            restore_all_cpus(max_cpus);
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }
        std::thread::sleep(Duration::from_secs(5));

        if !is_scx_active() {
            log_error!("Adaptive scheduler crashed during settlement -- skipping");
            let crash_output = stop_and_drain(full_proc);
            for line in crash_output.lines().take(10) {
                let line = line.trim();
                if !line.is_empty() {
                    println!("    SCHED: {}", line);
                }
            }
            restore_all_cpus(max_cpus);
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }

        log_info!(
            "Phase 2: BPF+ADAPTIVE ({}s)  Stressing: {} CPUs {:?}",
            DURATION_SECS, stress_cpus.len(), stress_cpus
        );
        let (f_samples, f_med, f_p99, f_worst) =
            run_scale_phase(&probe_exe, &stress_cpus, DURATION_SECS).expect("FULL PHASE FAILED");

        let full_alive = is_scx_active();
        if !full_alive {
            log_warn!("Adaptive scheduler crashed during measurement");
        }
        log_info!(
            "  Samples: {}  Median: {:.0}us  P99: {:.0}us  Worst: {:.0}us{}",
            f_samples, f_med, f_p99, f_worst,
            if full_alive { "" } else { "  [TAINTED]" }
        );

        let full_output = stop_and_drain(full_proc);
        kill_stale_schedulers();
        unsafe { libc::sync(); }
        std::thread::sleep(Duration::from_secs(2));

        // --- EXTERNAL SCX SCHEDULERS ---
        let mut ext_results: Vec<ExternalResult> = Vec::new();
        for (scx_name, scx_path) in &scx_schedulers {
            if is_scx_active() {
                log_warn!("sched_ext still active before {} -- waiting", scx_name);
                if !wait_for_deactivation(5) {
                    log_error!("sched_ext stuck active -- skipping {}", scx_name);
                    continue;
                }
            }

            let mut scx_proc = match start_external_scheduler(scx_path) {
                Ok(c) => c,
                Err(e) => {
                    log_warn!("Failed to start {}: {} -- skipping", scx_name, e);
                    continue;
                }
            };

            if !wait_for_activation(10) {
                log_warn!("{} did not activate -- skipping", scx_name);
                stop_scheduler(&mut scx_proc);
                if !wait_for_deactivation(5) {
                    log_warn!("sched_ext stuck after failed {}", scx_name);
                }
                continue;
            }
            std::thread::sleep(Duration::from_secs(5));

            if !is_scx_active() {
                log_warn!("{} crashed during settlement -- skipping", scx_name);
                drop(scx_proc);
                continue;
            }

            log_info!(
                "Phase: {} ({}s)  Stressing: {} CPUs {:?}",
                scx_name, DURATION_SECS, stress_cpus.len(), stress_cpus
            );

            let result = run_scale_phase(&probe_exe, &stress_cpus, DURATION_SECS);
            let alive = is_scx_active();
            if !alive {
                log_warn!("{} crashed during measurement", scx_name);
            }

            drop(scx_proc);
            if !wait_for_deactivation(5) {
                log_warn!("sched_ext stuck after {} -- continuing anyway", scx_name);
            }
            unsafe { libc::sync(); }
            std::thread::sleep(Duration::from_secs(2));

            match result {
                Ok((samples, med, p99, worst)) => {
                    log_info!(
                        "  Samples: {}  Median: {:.0}us  P99: {:.0}us  Worst: {:.0}us{}",
                        samples, med, p99, worst,
                        if alive { "" } else { "  [TAINTED]" }
                    );
                    ext_results.push(ExternalResult {
                        name: scx_name.clone(),
                        samples,
                        median: med,
                        p99,
                        worst,
                    });
                }
                Err(e) => {
                    log_error!("{} phase failed: {}", scx_name, e);
                }
            }
        }

        // --- EEVDF BASELINE (LAST -- CLEANUP SLOP DOESN'T MATTER) ---
        log_info!(
            "Phase 3: EEVDF ({}s)  Stressing: {} CPUs {:?}",
            DURATION_SECS, stress_cpus.len(), stress_cpus
        );

        let (e_stress_running, e_stress_handles) = spawn_stress_workers(&stress_cpus);
        let warmup_probe = spawn_probe(&probe_exe).expect("EEVDF WARMUP FAILED");
        std::thread::sleep(Duration::from_secs(WARMUP_SECS));
        let _ = kill_probe(warmup_probe);
        let e_probe = spawn_probe(&probe_exe).expect("EEVDF PROBE FAILED");
        std::thread::sleep(Duration::from_secs(DURATION_SECS));
        let eevdf_drain = kill_probe_async(e_probe);
        stop_stress_workers(e_stress_running, e_stress_handles);

        restore_all_cpus(max_cpus);

        let eevdf_stdout = eevdf_drain.join().unwrap().unwrap_or_default();
        let (e_samples, e_med, e_p99, e_worst) = parse_probe_output(&eevdf_stdout);
        log_info!(
            "  Samples: {}  Median: {:.0}us  P99: {:.0}us  Worst: {:.0}us",
            e_samples, e_med, e_p99, e_worst
        );

        std::thread::sleep(Duration::from_millis(500));

        results.push(ScaleResult {
            cores: n,
            eevdf_samples: e_samples,
            eevdf_median: e_med,
            eevdf_p99: e_p99,
            eevdf_worst: e_worst,
            bpf_samples: b_samples,
            bpf_median: b_med,
            bpf_p99: b_p99,
            bpf_worst: b_worst,
            bpf_sched_output: bpf_output,
            full_samples: f_samples,
            full_median: f_med,
            full_p99: f_p99,
            full_worst: f_worst,
            full_sched_output: full_output,
            externals: ext_results,
        });
    }

    assert!(!results.is_empty(), "NO SCALE POINTS COMPLETED");

    // COLLECT EXTERNAL SCHEDULER NAMES (USE FIRST RESULT AS CANONICAL LIST)
    let scx_names: Vec<String> = if !results.is_empty() {
        results[0].externals.iter().map(|e| e.name.clone()).collect()
    } else {
        Vec::new()
    };

    // REPORT
    let mut report = Vec::new();
    report.push("PANDEMONIUM SCALING BENCHMARK".to_string());
    report.push(format!(
        "WORKLOAD: STRESS WORKERS + INTERACTIVE PROBE ({}s PER PHASE, {} CPUs MAX)",
        DURATION_SECS, max_cpus
    ));
    report.push(String::new());

    // PER CORE COUNT: COMBINED TABLE WITH ALL SCHEDULERS
    let table_header = format!(
        "{:<24} {:>8} {:>9} {:>9} {:>9}",
        "SCHEDULER", "SAMPLES", "MEDIAN", "P99", "WORST"
    );
    for r in &results {
        report.push(format!(
            "[LATENCY: {} CORE{}]",
            r.cores, if r.cores == 1 { "" } else { "S" }
        ));
        report.push(table_header.clone());
        report.push(format!(
            "{:<24} {:>8} {:>8.0}us {:>8.0}us {:>8.0}us",
            "EEVDF", r.eevdf_samples, r.eevdf_median, r.eevdf_p99, r.eevdf_worst,
        ));
        report.push(format!(
            "{:<24} {:>8} {:>8.0}us {:>8.0}us {:>8.0}us",
            "PANDEMONIUM (BPF)", r.bpf_samples, r.bpf_median, r.bpf_p99, r.bpf_worst,
        ));
        report.push(format!(
            "{:<24} {:>8} {:>8.0}us {:>8.0}us {:>8.0}us",
            "PANDEMONIUM (FULL)", r.full_samples, r.full_median, r.full_p99, r.full_worst,
        ));
        for ext in &r.externals {
            report.push(format!(
                "{:<24} {:>8} {:>8.0}us {:>8.0}us {:>8.0}us",
                ext.name, ext.samples, ext.median, ext.p99, ext.worst,
            ));
        }
        report.push(String::new());
    }

    // SUMMARY: MEDIAN VS EEVDF ACROSS CORE COUNTS
    report.push("[SUMMARY: MEDIAN vs EEVDF (NEGATIVE = FASTER)]".to_string());
    let mut header = format!("{:<24}", "SCHEDULER");
    for r in &results {
        header.push_str(&format!(" {:>8}", format!("{}C", r.cores)));
    }
    report.push(header);

    let mut row = format!("{:<24}", "PANDEMONIUM (BPF)");
    for r in &results {
        row.push_str(&format!(" {:>+7.0}us", r.bpf_median - r.eevdf_median));
    }
    report.push(row);

    let mut row = format!("{:<24}", "PANDEMONIUM (FULL)");
    for r in &results {
        row.push_str(&format!(" {:>+7.0}us", r.full_median - r.eevdf_median));
    }
    report.push(row);

    for name in &scx_names {
        let mut row = format!("{:<24}", name);
        for r in &results {
            if let Some(ext) = r.externals.iter().find(|e| &e.name == name) {
                row.push_str(&format!(" {:>+7.0}us", ext.median - r.eevdf_median));
            } else {
                row.push_str(&format!(" {:>8}", "N/A"));
            }
        }
        report.push(row);
    }
    report.push(String::new());

    // DELTA: ADAPTIVE GAIN (FULL VS BPF-ONLY)
    report.push("[DELTA: ADAPTIVE GAIN (FULL vs BPF-ONLY, NEGATIVE = ADAPTIVE HELPS)]".to_string());
    report.push(format!(
        "{:>5} {:>10} {:>10} {:>10}",
        "CORES", "MEDIAN", "P99", "WORST"
    ));
    for r in &results {
        report.push(format!(
            "{:>5} {:>+9.0}us {:>+9.0}us {:>+9.0}us",
            r.cores,
            r.full_median - r.bpf_median,
            r.full_p99 - r.bpf_p99,
            r.full_worst - r.bpf_worst,
        ));
    }
    report.push(String::new());

    // SCHEDULER TELEMETRY
    report.push("[SCHEDULER TELEMETRY]".to_string());
    for r in &results {
        report.push(format!("{} CORE{}: BPF-ONLY",
            r.cores, if r.cores == 1 { "" } else { "S" }));
        for line in r.bpf_sched_output.lines() {
            let line = line.trim();
            if line.starts_with("d/s:") || line.starts_with("kick:") {
                report.push(format!("  {}", line));
            }
        }
        report.push(format!("{} CORE{}: BPF+ADAPTIVE",
            r.cores, if r.cores == 1 { "" } else { "S" }));
        for line in r.full_sched_output.lines() {
            let line = line.trim();
            if line.starts_with("d/s:") || line.starts_with("kick:") {
                report.push(format!("  {}", line));
            }
        }
        report.push(String::new());
    }

    let report_text = report.join("\n") + "\n";

    // PRINT TO TERMINAL
    println!();
    log_info!("BENCHMARK RESULTS");
    for line in &report {
        if line.is_empty() {
            println!();
        } else {
            log_info!("{}", line);
        }
    }

    let path = save_report(&report_text).expect("FAILED TO SAVE REPORT");
    log_info!("Report saved to {}", path);
}
