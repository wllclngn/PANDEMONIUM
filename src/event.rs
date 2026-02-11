// PANDEMONIUM EVENT LOG
// RECORDS STATS SNAPSHOTS DURING SCHEDULER EXECUTION
// PRE-ALLOCATED RING BUFFER. NO HEAP ALLOCATION DURING MONITORING.
// WRAPS AROUND AT CAPACITY -- OLDEST ENTRIES OVERWRITTEN.

const MAX_SNAPSHOTS: usize = 8192;

#[derive(Clone, Copy)]
pub struct Snapshot {
    pub ts_ns:       u64,
    pub dispatches:  u64,
    pub idle_hits:   u64,
    pub any_hits:    u64,
    pub lat_cri:     u64,
    pub interactive: u64,
    pub batch:       u64,
}

pub struct EventLog {
    snapshots: Vec<Snapshot>,
    head:      usize,
    len:       usize,
}

impl EventLog {
    pub fn new() -> Self {
        Self {
            snapshots: vec![
                Snapshot { ts_ns: 0, dispatches: 0, idle_hits: 0, any_hits: 0,
                           lat_cri: 0, interactive: 0, batch: 0 };
                MAX_SNAPSHOTS
            ],
            head: 0,
            len: 0,
        }
    }

    // RECORD ONE STATS SNAPSHOT. CALLED ONCE PER SECOND FROM THE MONITOR LOOP.
    // OVERWRITES OLDEST ENTRY WHEN FULL.
    pub fn snapshot(&mut self, dispatches: u64, idle_hits: u64, any_hits: u64,
                    lat_cri: u64, interactive: u64, batch: u64) {
        self.snapshots[self.head] = Snapshot {
            ts_ns: now_ns(),
            dispatches,
            idle_hits,
            any_hits,
            lat_cri,
            interactive,
            batch,
        };
        self.head = (self.head + 1) % MAX_SNAPSHOTS;
        if self.len < MAX_SNAPSHOTS {
            self.len += 1;
        }
    }

    // ITERATE SNAPSHOTS IN CHRONOLOGICAL ORDER
    fn iter_chronological(&self) -> impl Iterator<Item = &Snapshot> {
        let start = if self.len < MAX_SNAPSHOTS { 0 } else { self.head };
        (0..self.len).map(move |i| {
            &self.snapshots[(start + i) % MAX_SNAPSHOTS]
        })
    }

    // DUMP THE TIME SERIES AFTER EXECUTION
    pub fn dump(&self) {
        if self.len == 0 {
            return;
        }

        let mut iter = self.iter_chronological();
        let first = iter.next().unwrap();
        let base_ts = first.ts_ns;

        println!("\n{:<10} {:<12} {:<10} {:<10} {:<10} {:<10} {:<10}",
            "TIME_S", "DISPATCH/S", "IDLE/S", "ANY/S", "LAT_CRI", "INT", "BATCH");
        println!("{}", "-".repeat(72));

        // PRINT FIRST ENTRY
        println!("{:<10.1} {:<12} {:<10} {:<10} {:<10} {:<10} {:<10}",
            0.0, first.dispatches, first.idle_hits, first.any_hits,
            first.lat_cri, first.interactive, first.batch);

        for s in iter {
            let elapsed_s = (s.ts_ns - base_ts) as f64 / 1_000_000_000.0;
            println!("{:<10.1} {:<12} {:<10} {:<10} {:<10} {:<10} {:<10}",
                elapsed_s, s.dispatches, s.idle_hits, s.any_hits,
                s.lat_cri, s.interactive, s.batch);
        }

        if self.len == MAX_SNAPSHOTS {
            println!("\n(RING BUFFER WRAPPED -- SHOWING MOST RECENT {} SNAPSHOTS)", MAX_SNAPSHOTS);
        }
        println!("TOTAL SNAPSHOTS: {}", self.len);
    }

    // SUMMARY STATISTICS
    pub fn summary(&self) {
        if self.len < 2 {
            return;
        }

        let snapshots: Vec<&Snapshot> = self.iter_chronological().collect();

        let total_d: u64 = snapshots.iter().map(|s| s.dispatches).sum();
        let total_idle: u64 = snapshots.iter().map(|s| s.idle_hits).sum();
        let total_any: u64 = snapshots.iter().map(|s| s.any_hits).sum();
        let total_lat_cri: u64 = snapshots.iter().map(|s| s.lat_cri).sum();
        let total_int: u64 = snapshots.iter().map(|s| s.interactive).sum();
        let total_batch: u64 = snapshots.iter().map(|s| s.batch).sum();

        let peak_d = snapshots.iter().map(|s| s.dispatches).max().unwrap_or(0);

        let elapsed_ns = snapshots.last().unwrap().ts_ns - snapshots.first().unwrap().ts_ns;
        let elapsed_s = elapsed_ns as f64 / 1_000_000_000.0;

        println!("\n{}", "=".repeat(50));
        println!("PANDEMONIUM SUMMARY");
        println!("{}", "=".repeat(50));
        println!("  TOTAL DISPATCHES:  {}", total_d);
        println!("  TOTAL IDLE HITS:   {}", total_idle);
        println!("  TOTAL ANY HITS:    {}", total_any);
        println!("  PEAK DISPATCH/S:   {}", peak_d);
        if elapsed_s > 0.0 {
            println!("  AVG DISPATCH/S:    {:.0}", total_d as f64 / elapsed_s);
            let idle_pct = total_idle as f64 / (total_idle + total_any).max(1) as f64 * 100.0;
            println!("  IDLE HIT RATE:     {:.1}%", idle_pct);
        }
        let total_tier = total_lat_cri + total_int + total_batch;
        if total_tier > 0 {
            let lc_pct = total_lat_cri as f64 / total_tier as f64 * 100.0;
            let int_pct = total_int as f64 / total_tier as f64 * 100.0;
            let bat_pct = total_batch as f64 / total_tier as f64 * 100.0;
            println!("  TIER DISTRIBUTION: LAT_CRI {:.1}% / INT {:.1}% / BATCH {:.1}%",
                lc_pct, int_pct, bat_pct);
        }
        println!("  ELAPSED:           {:.1}s", elapsed_s);
        println!("  SAMPLES:           {}", self.len);
    }
}

fn now_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts);
    }
    (ts.tv_sec as u64) * 1_000_000_000 + (ts.tv_nsec as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_records() {
        let mut log = EventLog::new();
        assert_eq!(log.len, 0);

        log.snapshot(100, 90, 10, 5, 30, 65);
        assert_eq!(log.len, 1);
        assert_eq!(log.snapshots[0].dispatches, 100);
        assert_eq!(log.snapshots[0].idle_hits, 90);
        assert_eq!(log.snapshots[0].any_hits, 10);
        assert_eq!(log.snapshots[0].lat_cri, 5);
        assert_eq!(log.snapshots[0].interactive, 30);
        assert_eq!(log.snapshots[0].batch, 65);
        assert!(log.snapshots[0].ts_ns > 0);
    }

    #[test]
    fn ring_buffer_wraps() {
        let mut log = EventLog::new();

        // FILL TO CAPACITY
        for i in 0..MAX_SNAPSHOTS {
            log.snapshot(i as u64, 0, 0, 0, 0, 0);
        }
        assert_eq!(log.len, MAX_SNAPSHOTS);
        assert_eq!(log.head, 0); // WRAPPED BACK TO START

        // WRITE ONE MORE -- OVERWRITES OLDEST
        log.snapshot(9999, 0, 0, 0, 0, 0);
        assert_eq!(log.len, MAX_SNAPSHOTS);
        assert_eq!(log.head, 1);
        assert_eq!(log.snapshots[0].dispatches, 9999);

        // CHRONOLOGICAL ITERATION STARTS FROM OLDEST (INDEX 1)
        let ordered: Vec<u64> = log.iter_chronological()
            .map(|s| s.dispatches)
            .collect();
        assert_eq!(ordered[0], 1); // OLDEST SURVIVING ENTRY
        assert_eq!(*ordered.last().unwrap(), 9999); // NEWEST
        assert_eq!(ordered.len(), MAX_SNAPSHOTS);
    }

    #[test]
    fn summary_no_panic_empty() {
        let log = EventLog::new();
        log.summary(); // SHOULD NOT PANIC WITH 0 SNAPSHOTS
    }

    #[test]
    fn summary_no_panic_one() {
        let mut log = EventLog::new();
        log.snapshot(100, 50, 50, 10, 20, 70);
        log.summary(); // SHOULD NOT PANIC WITH 1 SNAPSHOT
    }

    #[test]
    fn dump_no_panic() {
        let mut log = EventLog::new();
        log.snapshot(100, 50, 50, 5, 25, 70);
        log.snapshot(200, 150, 50, 10, 40, 150);
        log.dump(); // SHOULD NOT PANIC
    }
}
