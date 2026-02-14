// PANDEMONIUM v0.9.9 PROCESS CLASSIFICATION DATABASE
// BPF OBSERVES MATURE TASK BEHAVIOR, RUST LEARNS PATTERNS, BPF APPLIES
//
// PROBLEM: EVERY NEW TASK ENTERS AS TIER_INTERACTIVE IN BPF enable().
// SHORT-LIVED PROCESSES (cc1, as, ld DURING COMPILATION) NEVER SURVIVE
// LONG ENOUGH TO GET RECLASSIFIED. HUNDREDS OF MISCLASSIFIED TASKS PER
// SECOND DURING make -j12, EACH FIRING PREEMPT KICKS AND GETTING SHORT
// INTERACTIVE SLICES.
//
// SOLUTION: BPF WRITES OBSERVATIONS TO AN LRU MAP WHEN A TASK'S EWMA
// MATURES (ewma_age == 8). RUST DRAINS OBSERVATIONS EVERY SECOND,
// MERGES INTO A HASHMAP WITH EWMA DECAY, AND WRITES CONFIDENT
// PREDICTIONS BACK TO A BPF HASH MAP. NEW TASKS WITH MATCHING comm
// START WITH THE CORRECT TIER AND avg_runtime FROM enable().

use std::collections::HashMap;

use anyhow::Result;
use libbpf_rs::MapCore;

const OBSERVE_PIN: &str = "/sys/fs/bpf/pandemonium/task_class_observe";
const INIT_PIN: &str = "/sys/fs/bpf/pandemonium/task_class_init";

const MIN_OBSERVATIONS: u32 = 3;
const MIN_CONFIDENCE: f64 = 0.6;
const MAX_PROFILES: usize = 512;
const STALE_TICKS: u64 = 60;

// MATCHES struct task_class_entry IN intf.h
#[repr(C)]
#[derive(Clone, Copy)]
struct TaskClassEntry {
    tier: u8,
    _pad: [u8; 7],
    avg_runtime: u64,
}

struct TaskProfile {
    tier_votes: [u32; 3],   // COUNT PER TIER: [BATCH, INTERACTIVE, LAT_CRITICAL]
    avg_runtime_ns: u64,
    observations: u32,
    last_seen_tick: u64,
}

impl TaskProfile {
    fn confidence(&self) -> f64 {
        let total: u32 = self.tier_votes.iter().sum();
        if total == 0 {
            return 0.0;
        }
        let max_count = *self.tier_votes.iter().max().unwrap_or(&0);
        max_count as f64 / total as f64
    }

    fn dominant_tier(&self) -> u8 {
        self.tier_votes.iter()
            .enumerate()
            .max_by_key(|(_, c)| *c)
            .map(|(i, _)| i as u8)
            .unwrap_or(1) // INTERACTIVE DEFAULT
    }
}

pub struct ProcessDb {
    observe: libbpf_rs::MapHandle,
    init: libbpf_rs::MapHandle,
    profiles: HashMap<[u8; 16], TaskProfile>,
    tick: u64,
}

impl ProcessDb {
    pub fn new() -> Result<Self> {
        let observe = libbpf_rs::MapHandle::from_pinned_path(OBSERVE_PIN)?;
        let init = libbpf_rs::MapHandle::from_pinned_path(INIT_PIN)?;
        Ok(Self {
            observe,
            init,
            profiles: HashMap::new(),
            tick: 0,
        })
    }

    // DRAIN OBSERVATIONS FROM BPF LRU MAP, MERGE INTO PROFILES
    pub fn ingest(&mut self) {
        let keys: Vec<Vec<u8>> = self.observe.keys().collect();
        for key in &keys {
            if let Ok(Some(val)) = self.observe.lookup(key, libbpf_rs::MapFlags::ANY) {
                if val.len() >= std::mem::size_of::<TaskClassEntry>() {
                    let entry: TaskClassEntry = unsafe {
                        std::ptr::read_unaligned(val.as_ptr() as *const TaskClassEntry)
                    };

                    let mut comm = [0u8; 16];
                    let copy_len = key.len().min(16);
                    comm[..copy_len].copy_from_slice(&key[..copy_len]);

                    let profile = self.profiles.entry(comm).or_insert(TaskProfile {
                        tier_votes: [0; 3],
                        avg_runtime_ns: 0,
                        observations: 0,
                        last_seen_tick: 0,
                    });

                    let tier_idx = (entry.tier as usize).min(2);
                    profile.tier_votes[tier_idx] += 1;
                    profile.avg_runtime_ns = if profile.observations == 0 {
                        entry.avg_runtime
                    } else {
                        // EWMA: 7/8 OLD + 1/8 NEW
                        (profile.avg_runtime_ns * 7 + entry.avg_runtime) / 8
                    };
                    profile.observations += 1;
                    profile.last_seen_tick = self.tick;
                }
            }
            let _ = self.observe.delete(key);
        }
    }

    // WRITE CONFIDENT PREDICTIONS TO BPF INIT MAP
    pub fn flush_predictions(&self) {
        for (comm, profile) in &self.profiles {
            if profile.observations >= MIN_OBSERVATIONS
                && profile.confidence() >= MIN_CONFIDENCE
            {
                let entry = TaskClassEntry {
                    tier: profile.dominant_tier(),
                    _pad: [0; 7],
                    avg_runtime: profile.avg_runtime_ns,
                };

                let val = unsafe {
                    std::slice::from_raw_parts(
                        &entry as *const TaskClassEntry as *const u8,
                        std::mem::size_of::<TaskClassEntry>(),
                    )
                };
                let _ = self.init.update(comm.as_slice(), val, libbpf_rs::MapFlags::ANY);
            }
        }
    }

    // EVICT STALE PROFILES, CAP TOTAL ENTRIES
    pub fn tick(&mut self) {
        self.tick += 1;

        // REMOVE PROFILES NOT SEEN IN 60 SECONDS
        let tick = self.tick;
        let stale: Vec<[u8; 16]> = self.profiles.iter()
            .filter(|(_, p)| tick - p.last_seen_tick > STALE_TICKS)
            .map(|(k, _)| *k)
            .collect();
        for comm in &stale {
            self.profiles.remove(comm);
            let _ = self.init.delete(comm.as_slice());
        }

        // CAP ENTRIES: EVICT OLDEST IF OVER LIMIT
        if self.profiles.len() > MAX_PROFILES {
            let mut entries: Vec<([u8; 16], u64)> = self.profiles.iter()
                .map(|(k, v)| (*k, v.last_seen_tick))
                .collect();
            entries.sort_by_key(|(_, t)| *t);
            let to_remove = self.profiles.len() - MAX_PROFILES;
            for (k, _) in entries.into_iter().take(to_remove) {
                self.profiles.remove(&k);
                let _ = self.init.delete(k.as_slice());
            }
        }
    }

    // (TOTAL PROFILES, CONFIDENT PROFILES)
    pub fn summary(&self) -> (usize, usize) {
        let total = self.profiles.len();
        let confident = self.profiles.values()
            .filter(|p| p.observations >= MIN_OBSERVATIONS
                && p.confidence() >= MIN_CONFIDENCE)
            .count();
        (total, confident)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn profile_confidence_unanimous() {
        let p = TaskProfile {
            tier_votes: [5, 0, 0],
            avg_runtime_ns: 100000,
            observations: 5,
            last_seen_tick: 0,
        };
        assert_eq!(p.confidence(), 1.0);
        assert_eq!(p.dominant_tier(), 0); // BATCH
    }

    #[test]
    fn profile_confidence_majority() {
        let p = TaskProfile {
            tier_votes: [3, 2, 0],
            avg_runtime_ns: 100000,
            observations: 5,
            last_seen_tick: 0,
        };
        assert_eq!(p.confidence(), 0.6);
        assert_eq!(p.dominant_tier(), 0); // BATCH WINS 3:2
    }

    #[test]
    fn profile_confidence_below_threshold() {
        let p = TaskProfile {
            tier_votes: [2, 2, 1],
            avg_runtime_ns: 100000,
            observations: 5,
            last_seen_tick: 0,
        };
        // 2/5 = 0.4, BELOW MIN_CONFIDENCE OF 0.6
        assert!(p.confidence() < MIN_CONFIDENCE);
    }

    #[test]
    fn profile_dominant_tier_lat_critical() {
        let p = TaskProfile {
            tier_votes: [1, 1, 5],
            avg_runtime_ns: 50000,
            observations: 7,
            last_seen_tick: 0,
        };
        assert_eq!(p.dominant_tier(), 2); // LAT_CRITICAL
    }

    #[test]
    fn profile_confidence_zero_votes() {
        let p = TaskProfile {
            tier_votes: [0, 0, 0],
            avg_runtime_ns: 0,
            observations: 0,
            last_seen_tick: 0,
        };
        assert_eq!(p.confidence(), 0.0);
    }

    #[test]
    fn task_class_entry_layout() {
        // VERIFY RUST STRUCT MATCHES BPF: 1 + 7 + 8 = 16 BYTES
        assert_eq!(std::mem::size_of::<TaskClassEntry>(), 16);
    }
}
