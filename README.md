# PANDEMONIUM

Built in Rust and C23, PANDEMONIUM is a Linux kernel scheduler built for sched_ext. Utilizing BPF patterns, PANDEMONIUM classifies every task by its behavior--wakeup frequency, context switch rate, runtime, sleep patterns--and adapts scheduling decisions in real time. Two-thread adaptive control loop (zero mutexes), three-tier behavioral dispatch and a process database that learns task classifications across lifetimes.

## Performance

Benchmarked on 12 AMD Zen CPUs, kernel 6.18.9-arch1-2, clang 21.1.6. Numbers below are representative ranges across multiple single-iteration bench-scale runs under real desktop load.

### Throughput (kernel build, vs EEVDF baseline)

| Cores | PANDEMONIUM | scx_bpfland |
|-------|-------------|-------------|
| 2     | +3.3-5.7%   | +0.3-2.4%   |
| 4     | +2.8-6.0%   | +0.5-1.4%   |
| 8     | +3.1-4.8%   | +3.1-6.6%   |
| 12    | +1.8-3.5%   | +0.7-3.9%   |

3-4% overhead at low core counts is inherent per-dispatch cost from 5 BPF callbacks per scheduling cycle -- amortized at higher core counts. At 12 cores, overhead is 2-3.5% in exchange for 8-19x better tail latency.

### P99 Wakeup Latency (interactive probe under CPU saturation)

| Cores | EEVDF     | PANDEMONIUM | scx_bpfland | vs EEVDF    |
|-------|-----------|-------------|-------------|-------------|
| 2     | 830-995us | 85-119us    | 1034-1932us | **8-10x**   |
| 4     | 827-884us | 78-101us    | 1009-1756us | **8-10x**   |
| 8     | 822-1596us| 67-83us     | 1003-1194us | **12-19x**  |
| 12    | 941-1632us| 68-95us     | 1001-1007us | **10-17x**  |

8-19x better tail latency than the kernel default scheduler. Sub-120us P99 across all core counts under full CPU saturation.

## Key Features

### Three-Tier Dispatch
- **Idle CPU Fast Path**: `select_cpu()` places wakeups directly to per-CPU DSQ with zero contention, kicks with `SCX_KICK_IDLE`
- **Node-Local Placement**: `enqueue()` finds idle CPUs within the NUMA node, dispatches to per-CPU DSQ with `SCX_KICK_PREEMPT` for non-batch tasks
- **Direct Preemptive Placement**: LAT_CRITICAL tasks (any path) and INTERACTIVE wakeups placed directly onto busy CPU's per-CPU DSQ with `SCX_KICK_PREEMPT`. Requeued INTERACTIVE tasks fall to overflow DSQ to avoid unnecessary BPF helper calls
- **NUMA-Scoped Overflow**: Per-node overflow DSQ with cross-node work stealing as final fallback
- **Tick Safety Net**: `tick()` preempts batch tasks when interactive work is waiting in the overflow DSQ
- **BPF Timer**: Independent 1ms preemption scan, reliable under NO_HZ_FULL

### Behavioral Classification
- **Latency-Criticality Score**: `lat_cri = (wakeup_freq * csw_rate) / effective_runtime` where `effective_runtime = avg_runtime + (runtime_dev >> 1)`
- **Three Tiers**: LAT_CRITICAL (1.5x avg_runtime slices, preemptive kicks), INTERACTIVE (2x avg_runtime), BATCH (configurable ceiling via adaptive layer)
- **CPU-Bound Demotion**: Tasks with avg_runtime >= 2.5ms are demoted from INTERACTIVE to BATCH in `stopping()`. Reversed automatically when the task sleeps and `runnable()` reclassifies from fresh behavioral signals
- **Compositor Boosting**: Compositors (kwin, sway, Hyprland, gnome-shell, picom, weston) are always LAT_CRITICAL
- **Runtime Variance Tracking**: EWMA of |runtime - avg_runtime| penalizes jittery tasks in the lat_cri formula

### Process Classification Database (procdb)
- **Cross-Lifecycle Learning**: BPF publishes mature task profiles (tier + avg_runtime) keyed by `comm[16]` to an observation map
- **Confidence Scoring**: Rust ingests observations, tracks EWMA convergence stability, and promotes profiles to "confident" when avg_runtime stabilizes
- **Prediction on Spawn**: `enable()` applies learned classification from prior runs -- `make -j12` forks start as BATCH from the first fork instead of 100 fresh INTERACTIVE classifications
- **Telemetry**: `procdb: total/confident` per tick shows learning progress

### Sleep-Aware Scheduling
- **quiescent() Callback**: Records sleep timestamp when tasks go to sleep
- **Sleep Duration Tracking**: `running()` computes sleep duration, pushes to ring buffer with tier and path metadata
- **I/O-Wait Classification**: Histogram-based analysis classifies short sleepers (I/O-bound interactive) vs long sleepers. Reported as `io=N%` in telemetry

### Adaptive Control Loop
- **Two Threads, Zero Mutexes**: Reflex thread (ring buffer consumer, sub-millisecond response) and monitor thread (1-second control loop, regime detection). Lock-free shared state via atomics
- **Workload Regime Detection**: LIGHT (idle >50%), MIXED (10-50%), HEAVY (<10%) with Schmitt trigger hysteresis and 2-tick hold to prevent regime bouncing
- **Regime Profiles**:
  - LIGHT: slice 4ms, preempt 4ms, batch 20ms, timer off (no contention)
  - MIXED: slice 4ms, preempt 2ms, batch 8ms, timer 10ms (balance)
  - HEAVY: slice 8ms, preempt 4ms, batch 4ms, timer 5ms (throughput)
- **Reflex Tightening**: BPF emits per-wakeup latency via ring buffer. Reflex thread computes P99 from a lock-free histogram and tightens both slice_ns and batch_slice_ns by 25% when P99 exceeds the regime ceiling. Only fires in MIXED regime
- **Graduated Relax**: After P99 normalizes, knobs step back toward baseline by 1ms per tick with a 2-second hold. Recovery from 1ms floor to 4ms baseline in 6 seconds
- **P99 Ceilings**: LIGHT 5ms, MIXED 10ms, HEAVY 20ms
- **BPF Guard Window**: When interactive wakeup hits overflow DSQ, batch slices are clamped to 200us for 1ms. Self-expiring

### Core-Count Scaling
- **Preempt Threshold**: `60 / (nr_cpu_ids + 2)`, clamped 3-20. Scales interactive kick aggressiveness with CPU count
- **CPU Hotplug**: `cpu_online`/`cpu_offline` callbacks prevent sched_ext auto-exit during benchmark CPU restriction
- **Topology Detection**: Parses sysfs for physical packages, L2/L3 cache domains, NUMA nodes. Populates cache_domain BPF map at init
- **BPF-Verifier Safe**: All EWMA uses bit shifts, no floats. Loop bounds via `bpf_for` and `MAX_CPUS`/`MAX_NODES` defines

## Architecture

```
pandemonium.py           Build/install manager (Python)
pandemonium_common.py    Shared infrastructure (logging, build, constants)
src/
  main.rs              Entry point, CLI, scheduler loop, telemetry
  scheduler.rs         BPF skeleton lifecycle, tuning knobs I/O
  adaptive.rs          Adaptive control loop (reflex + monitor threads)
  procdb.rs            Process classification database (observe -> learn -> predict)
  topology.rs          CPU topology detection (sysfs -> cache_domain BPF map)
  event.rs             Pre-allocated ring buffer for stats time series
  log.rs               Logging macros
  lib.rs               Library root
  bpf/
    main.bpf.c         BPF scheduler (~1000 lines, GNU C23)
    intf.h             Shared structs: tuning_knobs, pandemonium_stats, wake_lat_sample, task_class_entry
  cli/
    mod.rs             Shared constants, helpers
    check.rs           Dependency + kernel config verification
    run.rs             Build, sudo execution, dmesg, log management
    bench.rs           A/B benchmarking
    probe.rs           Interactive wakeup probe
    report.rs          Statistics, formatting
    test_gate.rs       Test gate orchestration
    child_guard.rs     RAII child process guard
    death_pipe.rs      Orphan detection via pipe POLLHUP
build.rs               vmlinux.h generation + C23 patching + BPF compilation
tests/
  pandemonium-tests.py Test orchestrator (bench-scale, CPU hotplug, dmesg capture)
  event.rs             Unit tests (ring buffer)
  scale.rs             Latency scaling benchmark (stress + interactive probe)
include/
  scx/                 Vendored sched_ext headers
```

### BPF Scheduler (main.bpf.c)

```
select_cpu()  ->  Idle CPU found?  ->  Per-CPU DSQ (fast path, KICK_IDLE)
                      |
                      v (no)
enqueue()     ->  Node-local idle?  ->  Per-CPU DSQ + PREEMPT kick
                      |
                      v (no)
              ->  LAT_CRITICAL or   ->  Direct per-CPU + KICK_PREEMPT
                  INTERACTIVE wakeup?
                      |
                      v (no)
              ->  Per-node overflow DSQ  ->  dispatch() work stealing
```

### Adaptive Layer (adaptive.rs)

```
BPF ring buffer              Reflex Thread              Monitor Thread
(per-wakeup latency)  --->   P99 histogram       |      1s control loop
                              |                   |      idle% -> regime
                              v                   |      regime -> baseline knobs
                        P99 > ceiling? --------+  |      P99 ok? -> graduated relax
                        (MIXED only)           |  |      procdb ingest + flush
                              |                v  v
                              v          BPF reads knobs on next dispatch
                        tighten slice
                        + batch knobs
```

Two threads, zero mutexes. BPF produces events, Rust reacts. Rust writes knobs, BPF reads them on the very next scheduling decision.

### Process Database (procdb.rs)

```
BPF stopping()                    Rust monitor                    BPF enable()
  |                                |                                |
  v                                v                                v
task_class_observe  -------->  ingest()  -------->  task_class_init
(comm -> tier, avg_runtime)    confidence scoring   (comm -> tier, avg_runtime)
                               EWMA convergence
                               detection
```

### Tuning Knobs (BPF map)

| Knob | Default | Purpose |
|------|---------|---------|
| `slice_ns` | 4ms | Interactive/lat_cri slice ceiling, Tier 2 threshold |
| `preempt_thresh_ns` | 2ms | BPF timer preemption threshold |
| `lag_scale` | 4 | Deadline lag multiplier (higher = more vtime credit) |
| `batch_slice_ns` | 20ms | Batch task slice ceiling |
| `timer_interval_ns` | 0/10ms | BPF timer interval (0 = scan disabled) |

## Requirements

- Linux kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y`
- Rust toolchain
- clang (BPF compilation)
- bpftool (vmlinux.h generation from running kernel BTF)
- system libbpf
- Root privileges (`CAP_SYS_ADMIN`)

```bash
# Arch Linux
pacman -S clang libbpf bpf rust
```

## Build & Install

```bash
# Build manager (recommended)
./pandemonium.py rebuild        # Force clean rebuild
./pandemonium.py install        # Build + install to /usr/local/bin + systemd service
./pandemonium.py status         # Show build/install status
./pandemonium.py clean          # Wipe build artifacts

# Manual
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo build --release
```

vmlinux.h is generated at build time from `/sys/kernel/btf/vmlinux` via bpftool and cached at `/tmp/pandemonium-vmlinux.h`. A generic vmlinux.h will not work -- sched_ext types only exist in kernels with `CONFIG_SCHED_CLASS_EXT=y`.

Note: the source directory path contains spaces, so `CARGO_TARGET_DIR=/tmp/pandemonium-build` is required for the vendored libbpf Makefile.

## Usage

```bash
# Run the scheduler (default: adaptive mode)
sudo pandemonium

# BPF-only mode (no Rust adaptive control loop)
sudo pandemonium --no-adaptive

# Override CPU count for scaling formulas
sudo pandemonium --nr-cpus 4

# Subcommands
pandemonium check        # Verify dependencies and kernel config
pandemonium start        # Build + sudo run + dmesg capture + log management
pandemonium bench        # A/B benchmark (EEVDF vs PANDEMONIUM)
pandemonium test         # Full test gate (unit + integration)
pandemonium test-scale   # A/B scaling benchmark with CPU hotplug
pandemonium probe        # Standalone interactive wakeup probe
pandemonium dmesg        # Filtered kernel log for sched_ext/pandemonium
```

### Monitoring

Per-second telemetry (printed to stdout while running):

```
d/s: 35402  idle: 3% shared: 32898  preempt: 5  keep: 0  kick: H=7061 S=25670 enq: W=7163 R=25735 wake: 8us p99: 100us lat_idle: 4us lat_kick: 9us affin: 0 procdb: 42/5 sleep: io=87% slice: 4000us guard: 0 [HEAVY]
```

| Counter | Meaning |
|---------|---------|
| d/s | Total dispatches per second |
| idle | Placed via select_cpu idle fast path (%) |
| shared | Enqueue -> per-node DSQ |
| preempt | BPF timer preemptions |
| kick H/S | Hard (PREEMPT) / Soft (nudge) kicks |
| enq W/R | Wakeup / Re-enqueue counts |
| wake | Average wakeup-to-run latency |
| p99 | P99 wakeup latency (from histogram) |
| lat_idle/kick | Per-path average latency |
| affin | Cache-affinity dispatch hits |
| procdb | Total profiles / confident predictions |
| sleep: io | I/O-wait sleep pattern percentage |
| slice | Current slice_ns knob value |
| guard | Batch slices clamped by interactive guard |
| [REGIME] | Current workload regime (MIXED/HEAVY) |

## Benchmarking

```bash
# Full benchmark (N-way scaling + latency at 2, 4, 8, 12 cores)
./pandemonium.py bench-scale

# Custom options
./pandemonium.py bench-scale --iterations 3 --core-counts 4,8,12
./pandemonium.py bench-scale --skip-latency
./pandemonium.py bench-scale --schedulers scx_bpfland,scx_rusty
```

Benchmarks compare EEVDF (kernel default), PANDEMONIUM (BPF-only and FULL adaptive), and external sched_ext schedulers across core counts via CPU hotplug.

Results are archived to `~/.cache/pandemonium/{version}-{timestamp}.json` for cross-build regression tracking.

## Testing

```bash
# Unit tests (no root required)
cargo test --release --test event

# Full test gate (requires root + sched_ext kernel)
pandemonium test

# Scaling benchmark (EEVDF vs PANDEMONIUM, CPU hotplug, requires root)
./pandemonium.py bench-scale
```

### Test Gate

| Layer | Name | What it tests |
|-------|------|---------------|
| 1 | Unit tests | Ring buffer, snapshot recording, summary/dump |
| 2 | Load/Classify/Unload | BPF lifecycle, dispatch stats, classification |
| 3 | Latency gate | cyclictest under scheduler (avg latency threshold) |
| 4 | Interactive | Wakeup overshoot (median < 500us) |
| 5 | Contention | Interactive latency under full CPU saturation |

### Scaling Benchmark

`tests/scale.rs`: A/B comparison at each core count [2, 4, 8, max] via CPU hotplug. Pins stress workers to CPUs via `sched_setaffinity`, reserves 1 core for interactive probe. 15-second phases per scheduler per core count. Reports median, P99, and worst wakeup latency with deltas and adaptive gain.

## Attribution

- `include/scx/*` headers from the [sched_ext](https://github.com/sched-ext/scx) project (GPL-2.0)
- vmlinux.h generated at build time from the running kernel's BTF via bpftool

## License

GPL-2.0
