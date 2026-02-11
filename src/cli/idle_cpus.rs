// IDLE CPU READER -- READS PINNED BPF MAP FROM RUNNING PANDEMONIUM SCHEDULER
// PRINTS SPACE-SEPARATED LIST OF IDLE CPU IDs TO STDOUT

use anyhow::Result;
use libbpf_rs::{MapCore, MapHandle, MapFlags};

const PIN_PATH: &str = "/sys/fs/bpf/pandemonium/idle_cpus";

pub fn run_idle_cpus() -> Result<()> {
    let map = match MapHandle::from_pinned_path(PIN_PATH) {
        Ok(m) => m,
        Err(_) => {
            eprintln!("PANDEMONIUM scheduler not running (no pinned idle_cpus map)");
            std::process::exit(1);
        }
    };

    let key = 0u32.to_ne_bytes();
    let val = match map.lookup(&key, MapFlags::ANY)? {
        Some(v) => v,
        None => {
            eprintln!("idle_cpus map has no entry");
            std::process::exit(1);
        }
    };

    if val.len() < 8 {
        eprintln!("idle_cpus map value too small ({} bytes)", val.len());
        std::process::exit(1);
    }

    let mask = u64::from_ne_bytes(val[..8].try_into().unwrap());

    for cpu in 0..64u32 {
        if mask & (1u64 << cpu) != 0 {
            print!("{} ", cpu);
        }
    }
    println!();

    Ok(())
}
