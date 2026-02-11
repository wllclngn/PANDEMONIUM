use std::sync::atomic::{AtomicBool, Ordering};

static RUNNING: AtomicBool = AtomicBool::new(true);

pub fn run_probe() {
    ctrlc::set_handler(move || {
        RUNNING.store(false, Ordering::Relaxed);
    })
    .ok();

    // UNBUFFERED STDOUT -- ONE LINE PER SAMPLE
    // (println! is line-buffered to tty, which is correct here)

    let target_ns: i64 = 10_000_000; // 10MS SLEEP TARGET
    let req = libc::timespec {
        tv_sec: 0,
        tv_nsec: target_ns as i64,
    };

    while RUNNING.load(Ordering::Relaxed) {
        let mut t0 = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        let mut t1 = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        unsafe {
            libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t0);
            libc::nanosleep(&req, std::ptr::null_mut());
            libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t1);
        }
        let elapsed_ns =
            (t1.tv_sec - t0.tv_sec) * 1_000_000_000 + (t1.tv_nsec - t0.tv_nsec);
        let overshoot_us = (elapsed_ns - target_ns as i64).max(0) / 1000;
        println!("{}", overshoot_us);
    }
}
