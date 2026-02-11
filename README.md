# PANDEMONIUMv0.9.1

A behavioral-adaptive Linux sched_ext scheduler. Classifies tasks by runtime behavior -- not process names -- and adapts scheduling parameters to match. All scheduling decisions happen in BPF with zero kernel-userspace round trips. Rust userspace handles configuration, monitoring, and reporting.

## Architecture

### Three-Tier Dispatch with NUMA Awareness

```
TIER 0  select_cpu()    Idle fast path -> SCX_DSQ_LOCAL     (~98% of tasks)
TIER 1  enqueue()       Node-local idle CPU -> per-CPU DSQ  (zero contention)
TIER 2  enqueue()       Per-node overflow DSQ + kick        (NUMA-scoped steal)
```

Dispatch consumption mirrors this in reverse: own per-CPU DSQ, then own node's overflow, then cross-node steal as a last resort.

### Behavioral Classification Engine

Every task is scored by a latency-criticality metric computed from three EWMA-smoothed signals:

```
lat_cri = (wakeup_freq * csw_rate) / max(avg_runtime_ms, 1)
```

| Signal | Source | Meaning |
|--------|--------|---------|
| `wakeup_freq` | `runnable()` delta timing | Wakeups per 100ms window |
| `csw_rate` | `task_struct->nvcsw` delta | Voluntary context switches per 100ms |
| `avg_runtime` | `stopping()` slice duration | EWMA of actual CPU time per run |

The score maps to three tiers:

| Tier | Score Range | Examples | Scheduling Behavior |
|------|-------------|----------|---------------------|
| `TIER_LAT_CRITICAL` | >= 32 | Compositors, audio, input handlers | Shortest slices, preemptive kicks, tight deadline |
| `TIER_INTERACTIVE` | 8-31 | Editors, terminals, web browsers | Medium slices, moderate deadline |
| `TIER_BATCH` | < 8 | Compilers, encoders, batch jobs | Full slices, relaxed deadline |

### Adaptive Two-Phase EWMA

EWMA smoothing uses age-dependent coefficients for fast convergence on new tasks and stability on established ones:

```
Age < 8 wakeups:   50% old + 50% new   (fast -- 2 cycles to 75% true value)
Age >= 8 wakeups:  87.5% old + 12.5% new  (stable -- resists transient spikes)
```

All power-of-2 bit shifts. No floats, no division. BPF-verifier-safe.

### Continuous Core-Count Scaling

Preempt threshold scales smoothly with CPU count:

```
preempt_thresh = 60 / (nr_cpu_ids + 2), clamped [3, 20]
```

| Cores | Threshold | Behavior |
|-------|-----------|----------|
| 2 | 15 | Very selective preemption |
| 4 | 10 | Moderate |
| 8 | 6 | Aggressive |
| 16+ | 3 | Maximum preemption |

### Components

```
src/bpf/main.bpf.c    BPF scheduler (select_cpu, enqueue, dispatch, etc.)
src/bpf/intf.h         Shared constants and stats struct (BPF <-> Rust)
src/main.rs            CLI entry point (clap), shutdown handling
src/scheduler.rs       BPF skeleton lifecycle (open, configure, load, attach, monitor)
src/event.rs           Pre-allocated ring buffer for stats time series
src/bpf_skel.rs        Generated BPF skeleton (build-time)
pandemonium.py         Python driver for build, run, benchmark, test gate
tests/gate.rs          Integration test gate (layers 2-4)
include/               Vendored sched_ext + vmlinux headers
```

## Requirements

- Linux kernel with `CONFIG_SCHED_CLASS_EXT=y` (6.12+)
- Rust toolchain (rustup.rs)
- clang (BPF compilation)
- libbpf development headers
- Root privileges (sched_ext requires CAP_SYS_ADMIN)

Arch Linux:
```
pacman -S clang libbpf rust
```

## Build and Run

The Python driver handles everything:

```bash
# Build and run
python3 pandemonium.py --build

# Run (uses cached build if available)
python3 pandemonium.py

# Check dependencies only
python3 pandemonium.py --check
```

Or manually with cargo:
```bash
# Spaces in path require CARGO_TARGET_DIR redirect
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo build --release
sudo /tmp/pandemonium-build/release/pandemonium
```

## Configuration

Pass after `--` when using the Python driver, or directly when running the binary:

| Flag | Default | Description |
|------|---------|-------------|
| `--build-mode` | off | Boost compiler/linker process weights (flag, no value) |
| `--slice-ns` | `5000000` | Base time slice (5ms) |
| `--slice-min` | `500000` | Minimum slice floor (0.5ms, interactive) |
| `--slice-max` | `20000000` | Maximum slice ceiling (20ms, compilers) |
| `--verbose` | off | Print cumulative stats each second |
| `--dump-log` | off | Dump full time series on exit |

```bash
# Verbose mode with custom slices
python3 pandemonium.py -- --verbose --slice-ns 3000000

# Enable build-mode classification
python3 pandemonium.py -- --build-mode
```

## Monitoring Output

While running, PANDEMONIUM prints per-second deltas:

```
dispatches/s: 12847    idle: 11923    direct: 412    overflow: 82    preempt: 31    lat_cri: 45    int: 892    batch: 11910    sticky: 200    boosted: 0    kicks: 31    avg_score: 12    tier_chg: 8
```

| Counter | Meaning |
|---------|---------|
| dispatches/s | Total tasks that stopped (completed a CPU slice) |
| idle | Tasks placed via select_cpu idle fast path |
| direct | Tasks placed on idle per-CPU DSQ in enqueue |
| overflow | Tasks placed on node overflow DSQ |
| preempt | Interactive preemptive kicks issued |
| lat_cri | Tasks classified as latency-critical this second |
| int | Tasks classified as interactive |
| batch | Tasks classified as batch |
| sticky | Tasks with very short avg runtime (< 10us) |
| boosted | Tasks that received build-mode weight boost |
| kicks | Total CPU kicks issued |
| avg_score | Average lat_cri score across all wakeups this second |
| tier_chg | Number of tasks that changed tier classification |

On exit, a summary is printed with totals, peak dispatch rate, average dispatch rate, idle hit rate, and tier distribution percentages.

## Benchmarking

Built-in A/B comparison against the default EEVDF scheduler:

```bash
# Benchmark a kernel build
python3 pandemonium.py --build --benchmark \
    --benchmark-cmd 'make -C /path/to/linux -j$(nproc)' \
    --clean-cmd 'make -C /path/to/linux clean' \
    --benchmark-iter 5

# Benchmark a Rust project
python3 pandemonium.py --benchmark \
    --benchmark-cmd 'cargo build --release --manifest-path /path/to/Cargo.toml' \
    --clean-cmd 'cargo clean --manifest-path /path/to/Cargo.toml'
```

The benchmark runs N iterations under EEVDF first, then starts PANDEMONIUM and runs N more, then reports the delta with mean and standard deviation.

## Test Gate

Four-layer test suite:

| Layer | What | Requires |
|-------|------|----------|
| 1 | Rust unit tests | Nothing |
| 2 | BPF load/classify/unload | Root, sched_ext kernel |
| 3 | Latency gate (cyclictest) | Root, cyclictest |
| 4 | Interactive responsiveness (wakeup latency) | Root |

```bash
# Full gate via Python driver
python3 pandemonium.py --build --test

# Unit tests only
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo test --release
```

Reports are saved to `/tmp/pandemonium/`.

## Tuning Guide

Use the monitoring output to validate tier thresholds:

- **avg_score**: Average lat_cri score. If this is consistently above 32 under idle conditions, the thresholds are too low.
- **tier_chg**: Tier churn rate. High values (> 100/s) indicate unstable classification -- the EWMA may need tuning or the thresholds are near a boundary.
- **TIER DISTRIBUTION** (exit summary): Shows what percentage of wakeups fell into each tier. A healthy desktop should be ~80% BATCH, ~15% INTERACTIVE, ~5% LAT_CRITICAL.

## License

GPL-2.0
