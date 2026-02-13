# PANDEMONIUM

A Linux kernel scheduler that observes how every task behaves -- wakeup patterns, context switch rates, execution times -- and adapts dispatch decisions within milliseconds. Built on sched_ext in Rust and BPF (GNU C23), PANDEMONIUM replaces static heuristics with a lock-free adaptive control loop that tightens and relaxes scheduling knobs based on live P99 latency. Beats the default Linux scheduler (EEVDF) on both throughput and tail latency under contention.

**Key Features:**

### Three-Tier Dispatch
- **Idle CPU Fast Path**: `select_cpu()` places ~98% of wakeups directly to `SCX_DSQ_LOCAL` with zero contention
- **Node-Local Placement**: `enqueue()` finds idle CPUs within the NUMA node via per-node idle bitmap, dispatches to per-CPU DSQ
- **Direct Preemptive Placement**: When no idle CPU exists, latency-sensitive tasks are placed directly onto a busy CPU's per-CPU DSQ with `SCX_KICK_PREEMPT`, zeroing the running task's slice. The kicked CPU finds the interactive task first on reschedule -- no overflow queue, no searching
- **NUMA-Scoped Overflow**: Per-node overflow DSQ with work stealing as final fallback

### Behavioral Classification
- **Latency-Criticality Score**: Every task accumulates a score from four EWMA-smoothed signals: `lat_cri = (wakeup_freq * csw_rate) / effective_runtime`
- **Three Tiers**: LAT_CRITICAL (shortest slices, preemptive kicks, direct placement), INTERACTIVE (medium slices, conditional kicks), BATCH (core-scaled slices, full throughput)
- **Runtime Variance Tracking**: Tasks with jittery execution times are penalized, preventing unstable tasks from holding high-priority tiers
- **Compositor Boosting**: Compositors (kwin, sway, Hyprland, gnome-shell) are always boosted to LAT_CRITICAL. PipeWire runs at RT priority via RTKIT and bypasses sched_ext entirely
- **Auto-Calibration**: `--calibrate` mode collects histograms and suggests thresholds for your workload

### Adaptive Control Loop
- **Two Threads, Zero Mutexes**: Reflex thread (ring buffer consumer, sub-millisecond response) and monitor thread (1-second control loop, workload regime detection). Lock-free shared state via atomics
- **Workload Regime Detection**: Classifies system load as LIGHT (idle >40%), MIXED (10-40%), or HEAVY (<10%) with hysteresis to prevent regime bouncing on noisy ticks
- **Reflex Tightening**: BPF emits per-wakeup latency samples via ring buffer. Reflex thread maintains a lock-free P99 histogram and tightens tuning knobs (slice, preempt threshold) when P99 exceeds the regime ceiling. Consecutive-spike filter prevents transient noise from triggering false tightening
- **Graduated Relax**: After P99 normalizes, knobs step back toward baseline in 100us increments with a 3-second hold, avoiding oscillation
- **BPF Guard Window**: When interactive wakeup-to-run latency exceeds the base slice, batch task slices are temporarily clamped. The guard window scales proportionally to the detected delay and expires naturally

### Core-Count Scaling
- **Continuous Parameter Scaling**: Preempt threshold, batch ceiling, and EWMA convergence scale continuously with CPU count (2 cores: threshold 15, 8 cores: 6, 32 cores: 3)
- **BPF-Verifier Safe**: EWMA convergence uses bit shifts only, no floats
- **Idle CPU Bitmap**: `tick()` snapshots per-node idle cpumasks into a BPF map. Uses `__COMPAT_scx_bpf_get_idle_cpumask_node()` for kernel 6.12+ with `SCX_OPS_BUILTIN_IDLE_PER_NODE`

### Safety Nets
- **Tick Preemption** (lightweight mode): Optional simplified classifier with tick-based preemption for batch tasks when interactive work is pending. Enable with `--lightweight`
- **P99 Histogram Overflow Protection**: Last histogram bucket (+inf) is capped at the last real edge (20ms), preventing u64::MAX from poisoning ceiling comparisons
- **Minimum Slice Floor**: 500us floor prevents reflex tightening from creating a death spiral of excessive preemptions

---

## Benchmark Results

### Contention Benchmark (parallel build + interactive probe)

```
SCHEDULER                   BUILD  SAMPLES   MEDIAN      P99    WORST
------------------------ -------- -------- -------- -------- --------
EEVDF (DEFAULT)            17.02s     1780      69us    1991us   10158us
PANDEMONIUM (BUILD-MODE)   16.37s     1716      67us    1411us    4662us

BUILD DELTA: -3.8% (PANDEMONIUM IS 3.8% FASTER)
P99 LATENCY DELTA: -580us (PANDEMONIUM IS 580us BETTER)
```

Workload: parallel Rust compilation + interactive wakeup probe (10ms sleep/wake cycle). 12-thread AMD system.

### Scaling Benchmark (stress workers + interactive probe, CPU hotplug)

```
CORES   EEVDF P99  PANDEMONIUM P99    DELTA
----- --------- --------------- ---------
    1      932us           661us    -271us  (PANDEMONIUM WINS)
    8      786us          2148us   +1362us  (ROOM TO IMPROVE)
   12      829us          3905us   +3076us  (ROOM TO IMPROVE)

MEDIANS TIED ACROSS ALL CORE COUNTS (~58-78us)
```

Workload: N-1 stress workers pinned to idle CPUs via `sched_setaffinity` + interactive probe. CPU hotplug for core count control. 3s warmup discard + 5s settlement for stable measurement.

## Architecture

```
pandemonium.py           Build/run/install manager (Python)
src/
  lib.rs               Library root (exports event module for tests)
  main.rs              CLI, subcommand dispatch, scheduler loop
  scheduler.rs         BPF skeleton lifecycle + calibration mode
  adaptive.rs          Adaptive control loop (reflex + monitor threads)
  event.rs             Pre-allocated ring buffer for stats time series
  bpf/
    main.bpf.c         BPF scheduler (~890 lines, GNU C23)
    intf.h             Shared constants and structs (BPF <-> Rust)
  cli/
    mod.rs             Shared constants, helpers
    check.rs           Dependency + kernel config verification
    run.rs             Build, sudo execution, dmesg, log management
    bench.rs           A/B benchmarking (self, contention, mixed, cmd)
    probe.rs           Interactive wakeup probe
    idle_cpus.rs       Read BPF idle bitmap from running scheduler
    report.rs          Statistics, formatting, file output
    test_gate.rs       Test gate orchestration
    child_guard.rs     RAII child process guard (SIGINT -> SIGKILL + killpg)
    death_pipe.rs      Orphan detection via pipe POLLHUP
build.rs               vmlinux.h generation + C23 patching + BPF compilation
tests/
  event.rs             Unit tests (ring buffer)
  gate.rs              Integration test gate (5 layers)
  scale.rs             A/B scaling benchmark (EEVDF vs PANDEMONIUM)
include/
  scx/                 Vendored sched_ext headers
```

### BPF Scheduler (main.bpf.c)

```
select_cpu()  ->  Idle CPU found?  ->  SCX_DSQ_LOCAL (fast path, ~98%)
                      |
                      v (no)
enqueue()     ->  Node-local idle?  ->  Per-CPU DSQ (zero contention)
                      |
                      v (no)
              ->  Latency-sensitive? ->  Direct per-CPU + KICK_PREEMPT
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
                              |                |  |
                              v                v  v
                        tighten knobs    BPF reads knobs on next dispatch
```

Two threads, zero mutexes. BPF produces events, Rust reacts. Rust writes knobs, BPF reads them on the very next scheduling decision.

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

# Verify everything
pandemonium check
```

## Build & Install

```bash
# Automated
./pandemonium.py rebuild        # Clean rebuild
./pandemonium.py install        # Build + symlink to /usr/local/bin
./pandemonium.py start          # Build if needed + run

# Manual
CARGO_TARGET_DIR=/tmp/pandemonium-build cargo build --release
```

vmlinux.h is generated at build time from `/sys/kernel/btf/vmlinux` via bpftool and cached at `/tmp/pandemonium-vmlinux.h`. A generic vmlinux.h will not work -- sched_ext types only exist in kernels with `CONFIG_SCHED_CLASS_EXT=y`.

## Usage

```
pandemonium run         Run the scheduler (needs root)
pandemonium start       Build + sudo run + dmesg capture + log management
pandemonium check       Verify dependencies and kernel config
pandemonium bench       A/B benchmark against EEVDF
pandemonium bench-run   Build release + run benchmark + save logs
pandemonium test        Full test gate (unit + integration)
pandemonium test-scale  A/B scaling benchmark (EEVDF vs PANDEMONIUM, CPU hotplug)
pandemonium probe       Standalone interactive wakeup probe
pandemonium idle-cpus   Print idle CPU bitmask (requires running scheduler)
pandemonium dmesg       Filtered kernel log
```

Running with no subcommand defaults to `run`.

### Examples

```bash
# Standard operation
pandemonium start

# Verbose monitoring with full time series dump on exit
pandemonium start --observe

# Compile-heavy workload (boost compiler/linker weights)
sudo pandemonium run --build-mode --verbose

# Tune classification thresholds for your workload
pandemonium start --calibrate

# Custom thresholds
sudo pandemonium run --lat-cri-low 12 --lat-cri-high 48

# Override CPU count for scaling formula testing
sudo pandemonium run --nr-cpus 4 --verbose
```

### Benchmark Modes

```bash
# Contention benchmark (P99 tail latency under parallel build + probe)
pandemonium bench --mode contention -- --build-mode

# Full A/B build benchmark (5 iterations)
pandemonium bench --mode self --iterations 5

# Audio quality under load (xrun tracking)
pandemonium bench --mode mixed

# Custom command A/B
pandemonium bench --mode cmd --cmd "make -j" --clean-cmd "make clean"
```

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--build-mode` | off | Boost compiler/linker process weights |
| `--slice-ns` | 5000000 | Base time slice (5ms) |
| `--slice-min` | 500000 | Minimum slice floor (0.5ms) |
| `--slice-max` | 20000000 | Maximum slice ceiling (20ms) |
| `--lat-cri-low` | 8 | Score threshold for INTERACTIVE tier |
| `--lat-cri-high` | 32 | Score threshold for LAT_CRITICAL tier |
| `--nr-cpus` | auto | Override CPU count for scaling formulas |
| `--verbose` | off | Per-second stats output |
| `--dump-log` | off | Full time series on exit |
| `--lightweight` | off | Enable lightweight classification mode |
| `--calibrate` | off | Histogram collection + threshold suggestion |

### Monitoring

With `--verbose`, per-second deltas including wakeup latency tracking:

```
dispatches/s: 12847  idle: 11923  direct: 412  overflow: 82  preempt: 31
lat_cri: 45  int: 892  batch: 11910  sticky: 200  boosted: 0
kicks: 31  avg_score: 12  tier_chg: 8  wake_avg: 4200  wake_max: 189000
```

| Counter | Meaning |
|---------|---------|
| idle | Placed via select_cpu idle fast path |
| direct | Placed on per-CPU DSQ (idle or preemptive) |
| overflow | Placed on node overflow DSQ |
| preempt | Preemptive kicks issued (SCX_KICK_PREEMPT) |
| lat_cri / int / batch | Wakeups classified per tier |
| wake_avg / wake_max | Non-batch wakeup-to-run latency (ns) |

## Testing

All tests live in `tests/`:

```bash
# Unit tests (no root required)
cargo test --release --test event

# Full test gate (requires root + sched_ext kernel)
pandemonium test

# Or run integration layers directly
sudo cargo test --test gate --release -- --ignored --test-threads=1 full_gate

# Core-count scaling benchmark (A/B vs EEVDF, CPU hotplug, requires root)
pandemonium test-scale
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

`tests/scale.rs`: A/B comparison at each core count [1, 2, 4, 8, max] via CPU hotplug. Detects idle CPUs before each phase (BPF idle bitmap for PANDEMONIUM, `/proc/stat` delta for EEVDF), pins stress workers to idle cores via `sched_setaffinity`, reserves 1 core for system tasks. 3s warmup discard + 5s settlement for stable measurement. Reports median, P99, and worst wakeup latency with deltas.

## Attribution

- `include/scx/*` headers from the [sched_ext](https://github.com/sched-ext/scx) project (GPL-2.0)
- vmlinux.h generated at build time from the running kernel's BTF via bpftool

## License

GPL-2.0
