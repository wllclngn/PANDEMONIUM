use std::process::Command;

use anyhow::{bail, Result};

use super::TARGET_DIR;

pub fn run_test_gate() -> Result<()> {
    let project_root = env!("CARGO_MANIFEST_DIR");

    log_info!("PANDEMONIUM test gate");

    // LAYER 1: UNIT TESTS (NO ROOT)
    log_info!("Layer 1: Rust unit tests");
    let l1 = Command::new("cargo")
        .args(["test", "--release"])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !l1.success() {
        bail!("LAYER 1 FAILED -- SKIPPING REMAINING LAYERS");
    }

    // LAYERS 2-5: INTEGRATION TESTS (REQUIRES ROOT)
    log_info!("Layers 2-5: integration (requires root)");
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

    log_info!("PANDEMONIUM scaling benchmark (A/B vs EEVDF)");
    log_info!("Requires root (CPU hotplug + BPF)");

    // NUKE STALE FINGERPRINTS (sudo cargo test CREATES ROOT-OWNED FILES
    // THAT PREVENT NON-ROOT cargo build FROM RECOMPILING)
    log_info!("Cleaning stale fingerprints...");
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

    log_info!("Building (release)...");
    let build = Command::new("cargo")
        .args(["build", "--release"])
        .env("CARGO_TARGET_DIR", TARGET_DIR)
        .current_dir(project_root)
        .status()?;

    if !build.success() {
        bail!("BUILD FAILED");
    }

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
