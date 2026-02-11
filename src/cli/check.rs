use std::io::Read;
use std::path::Path;
use std::process::Command;

use anyhow::Result;

fn check_tool(name: &str) -> bool {
    Command::new("which")
        .arg(name)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn check_kernel_config() -> bool {
    let file = match std::fs::File::open("/proc/config.gz") {
        Ok(f) => f,
        Err(_) => {
            println!("  /proc/config.gz       NOT FOUND (SKIPPED)");
            return true;
        }
    };
    let mut decoder = flate2::read::GzDecoder::new(file);
    let mut config = String::new();
    if decoder.read_to_string(&mut config).is_err() {
        println!("  /proc/config.gz       UNREADABLE (SKIPPED)");
        return true;
    }
    let found = config.contains("CONFIG_SCHED_CLASS_EXT=y");
    if found {
        println!("  CONFIG_SCHED_CLASS_EXT OK");
    } else {
        println!("  CONFIG_SCHED_CLASS_EXT NOT FOUND -- sched_ext may not be available");
    }
    found
}

pub fn run_check() -> Result<()> {
    println!("PANDEMONIUM DEPENDENCY CHECK");
    println!();

    let mut ok = true;
    let tools = ["cargo", "rustc", "clang", "bpftool", "sudo"];
    for tool in &tools {
        if check_tool(tool) {
            println!("  {:<24}OK", tool);
        } else {
            println!("  {:<24}MISSING", tool);
            ok = false;
        }
    }
    println!();

    println!("KERNEL CONFIG:");
    if !check_kernel_config() {
        ok = false;
    }
    println!();

    let scx_path = Path::new("/sys/kernel/sched_ext/root/ops");
    if scx_path.exists() {
        let active = std::fs::read_to_string(scx_path).unwrap_or_default();
        let active = active.trim();
        if active.is_empty() {
            println!("  sched_ext             AVAILABLE (no scheduler active)");
        } else {
            println!("  sched_ext             ACTIVE ({})", active);
        }
    } else {
        println!("  sched_ext             NOT AVAILABLE (sysfs path missing)");
        ok = false;
    }
    println!();

    if ok {
        println!("ALL CHECKS PASSED");
    } else {
        println!("SOME CHECKS FAILED");
        if !check_tool("cargo") || !check_tool("rustc") {
            println!("  Install Rust: https://rustup.rs");
        }
        if !check_tool("clang") {
            println!("  Install clang: pacman -S clang");
        }
        std::process::exit(1);
    }

    Ok(())
}
