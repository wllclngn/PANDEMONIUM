use std::process::Command;

use anyhow::{bail, Result};

use super::TARGET_DIR;

pub fn run_test_gate() -> Result<()> {
    let project_root = env!("CARGO_MANIFEST_DIR");

    println!();
    println!("PANDEMONIUM TEST GATE (RUST)");
    println!("{}", "=".repeat(60));

    // LAYER 1: UNIT TESTS (NO ROOT)
    println!("LAYER 1: RUST UNIT TESTS");
    let l1 = Command::new("cargo")
        .args(["test", "--release"])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !l1.success() {
        bail!("LAYER 1 FAILED -- SKIPPING REMAINING LAYERS");
    }
    println!();

    // LAYERS 2-5: INTEGRATION TESTS (REQUIRES ROOT)
    println!("LAYERS 2-5: INTEGRATION (REQUIRES ROOT)");
    let l2 = Command::new("sudo")
        .args([
            "-E",
            &format!("CARGO_TARGET_DIR={}", TARGET_DIR),
            "cargo",
            "test",
            "--test",
            "gate",
            "--release",
            "--",
            "--ignored",
            "--test-threads=1",
            "full_gate",
        ])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !l2.success() {
        std::process::exit(l2.code().unwrap_or(1));
    }

    Ok(())
}

pub fn run_test_scale() -> Result<()> {
    let project_root = env!("CARGO_MANIFEST_DIR");

    println!();
    println!("PANDEMONIUM SCALING BENCHMARK (A/B VS EEVDF)");
    println!("REQUIRES ROOT (CPU HOTPLUG + BPF)");
    println!("{}", "=".repeat(60));
    println!();

    // NUKE STALE FINGERPRINTS (sudo cargo test CREATES ROOT-OWNED FILES
    // THAT PREVENT NON-ROOT cargo build FROM RECOMPILING)
    println!("CLEANING STALE FINGERPRINTS...");
    let fp_dir = format!("{}/release/.fingerprint", TARGET_DIR);
    if let Ok(entries) = std::fs::read_dir(&fp_dir) {
        for entry in entries.flatten() {
            if entry.file_name().to_string_lossy().starts_with("pandemonium-") {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
    }
    // ALSO CLEAN THE PACKAGE TO FORCE FULL RECOMPILATION
    let _ = Command::new("cargo")
        .args(["clean", "-p", "pandemonium", "--release"])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status();

    println!("BUILDING (RELEASE)...");
    let build = Command::new("cargo")
        .args(["build", "--release"])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !build.success() {
        bail!("BUILD FAILED");
    }
    println!();

    let status = Command::new("sudo")
        .args([
            "-E",
            &format!("CARGO_TARGET_DIR={}", TARGET_DIR),
            "cargo",
            "test",
            "--test",
            "scale",
            "--release",
            "--",
            "--ignored",
            "--test-threads=1",
            "--nocapture",
        ])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !status.success() {
        std::process::exit(status.code().unwrap_or(1));
    }

    Ok(())
}
