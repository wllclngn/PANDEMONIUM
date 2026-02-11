# PANDEMONIUMv0.9.2

A behavioral-adaptive Linux scheduler built on sched_ext. Classifies tasks by runtime behavior -- not process names -- and adapts scheduling parameters to match. Scheduling decisions happen in BPF with zero kernel-userspace round trips. Everything else is Rust: configuration, monitoring, benchmarking, testing.

## Requirements

- Linux kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y`
- Rust toolchain (rustup.rs)
- clang (BPF compilation)
- Root privileges (sched_ext requires CAP_SYS_ADMIN)

Arch Linux:
```
pacman -S clang libbpf rust
```

Check all dependencies:
```
pandemonium check
```

## Build

```bash
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo build --release
```

Or let the `start` subcommand handle it:
```bash
pandemonium start
```

## Usage

PANDEMONIUM is a single binary with subcommands:

```
pandemonium run     Run the scheduler (needs root)
pandemonium start   Build + run with sudo + capture dmesg + save logs
pandemonium check   Verify dependencies and kernel config
pandemonium bench   A/B benchmark (EEVDF baseline vs PANDEMONIUM)
pandemonium test    Run test gate (unit + integration)
pandemonium dmesg   Show filtered kernel log
pandemonium probe   Interactive wakeup probe (standalone)
```

Running with no subcommand defaults to `run`.

### Examples

```bash
# Build, run, capture output + dmesg, save logs
pandemonium start

# Run with verbose monitoring
pandemonium start --observe

# Run scheduler directly (needs root)
sudo pandemonium run --build-mode --verbose

# Calibrate tier thresholds
pandemonium start --calibrate
```

## Configuration

Flags for the `run` subcommand (pass after `--` when using `start` or `bench`):

| Flag | Default | Description |
|------|---------|-------------|
| `--build-mode` | off | Boost compiler/linker process weights |
| `--slice-ns` | 5000000 | Base time slice (5ms) |
| `--slice-min` | 500000 | Minimum slice floor (0.5ms) |
| `--slice-max` | 20000000 | Maximum slice ceiling (20ms) |
| `--verbose` | off | Print per-second stats |
| `--dump-log` | off | Dump full time series on exit |
| `--lightweight` | auto | Force lightweight mode (skip full classification) |
| `--no-lightweight` | auto | Force full engine even on few cores |
| `--calibrate` | off | Collect histogram and suggest thresholds |

Lightweight mode auto-enables on 4 cores or fewer, replacing the full EWMA classification engine with a simple voluntary-CSW heuristic.

## Architecture

### Three-Tier Dispatch

```
TIER 0  select_cpu()    Idle CPU found -> SCX_DSQ_LOCAL          ~98% of wakeups
TIER 1  enqueue()       Node-local idle CPU -> per-CPU DSQ       Zero contention
TIER 2  enqueue()       Per-node overflow DSQ + behavioral kick  NUMA-scoped steal
```

Dispatch consumption mirrors this: own per-CPU DSQ first, then node overflow, then cross-node steal.

### Behavioral Classification

Every task gets a latency-criticality score from three EWMA-smoothed signals:

```
lat_cri = (wakeup_freq * csw_rate) / avg_runtime_ms
```

| Tier | Score | Examples | Behavior |
|------|-------|----------|----------|
| LAT_CRITICAL | >= 32 | Compositors, audio, input | Shortest slices, preemptive kicks |
| INTERACTIVE | 8-31 | Editors, terminals, browsers | Medium slices |
| BATCH | < 8 | Compilers, encoders | Full slices, relaxed deadline |

Compositors are always boosted to LAT_CRITICAL regardless of score. PipeWire runs at RT priority via RTKIT and bypasses sched_ext entirely.

### EWMA Convergence

```
Age < 8 wakeups:   50/50 split    Fast convergence for new tasks
Age >= 8 wakeups:  87.5/12.5      Stability for established tasks
```

All bit shifts. No floats. BPF-verifier-safe.

### Core-Count Scaling

Batch slice ceiling and preempt threshold scale continuously with CPU count:

| Cores | Preempt Threshold | Batch Ceiling |
|-------|-------------------|---------------|
| 2 | 15 | ~5ms |
| 8 | 6 | ~20ms |
| 32 | 3 | ~80ms |

## Benchmarking

Built-in A/B comparison against the default EEVDF scheduler:

```bash
# Self-build benchmark (compile PANDEMONIUM itself)
pandemonium bench --mode self

# Contention: compile + interactive wakeup probe (measures P99 latency)
pandemonium bench --mode contention

# Mixed: compile + audio playback (measures xruns)
pandemonium bench --mode mixed

# Custom command
pandemonium bench --mode cmd --cmd 'make -j$(nproc)' --clean-cmd 'make clean' --iterations 5
```

Each benchmark runs N iterations under EEVDF, starts PANDEMONIUM, runs N more, and reports the delta.

Reports are saved to `/tmp/pandemonium/`.

## Monitoring

With `--verbose`, PANDEMONIUM prints per-second deltas:

```
dispatches/s: 12847  idle: 11923  direct: 412  overflow: 82  preempt: 31
lat_cri: 45  int: 892  batch: 11910  sticky: 200  boosted: 0
kicks: 31  avg_score: 12  tier_chg: 8
```

| Counter | Meaning |
|---------|---------|
| dispatches/s | Tasks that completed a CPU slice |
| idle | Placed via select_cpu idle fast path |
| direct | Placed on idle per-CPU DSQ in enqueue |
| overflow | Placed on node overflow DSQ |
| preempt | Preemptive kicks issued |
| lat_cri / int / batch | Tasks classified per tier |
| sticky | Tasks with avg runtime < 10us |
| boosted | Tasks that got build-mode weight boost |
| avg_score | Average lat_cri score this second |
| tier_chg | Tasks that changed tier |

On exit, a summary shows totals, peak dispatch rate, idle hit rate, and tier distribution.

## Test Gate

Five-layer test suite:

| Layer | What | Requires |
|-------|------|----------|
| 1 | Rust unit tests | Nothing |
| 2 | BPF load/classify/unload | Root, sched_ext |
| 3 | Latency gate (cyclictest) | Root, cyclictest |
| 4 | Interactive responsiveness | Root |
| 5 | Contention latency | Root |

```bash
# Full gate
pandemonium test

# Unit tests only
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo test --release
```

## Project Layout

```
src/
  main.rs              CLI entry point, subcommand dispatch, scheduler loop
  scheduler.rs         BPF skeleton lifecycle (open/configure/load/attach/monitor)
  event.rs             Pre-allocated ring buffer for stats time series
  bpf/
    main.bpf.c         BPF scheduler (select_cpu, enqueue, dispatch, tick, etc.)
    intf.h             Shared constants and structs (BPF <-> Rust)
  cli/
    mod.rs             Shared constants, helpers (is_scx_active, wait_for_activation)
    check.rs           Dependency and kernel config verification
    run.rs             Build, sudo execution, dmesg capture, log management
    bench.rs           A/B benchmarking (self, contention, mixed, cmd)
    probe.rs           Interactive wakeup probe (clock_gettime + nanosleep)
    report.rs          Statistics, formatting, file output
    test_gate.rs       Test gate orchestration
tests/
  gate.rs              Integration test gate (layers 2-5)
include/               Vendored sched_ext + vmlinux headers
```

## License

GPL-2.0
