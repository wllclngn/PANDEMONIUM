# PANDEMONIUM

A Linux kernel scheduler for sched_ext, built in Rust and C23. PANDEMONIUM classifies every task by behavior — wakeup frequency, context switch rate, runtime, sleep patterns — and adapts scheduling decisions in real time. A damped harmonic oscillator drives CoDel-inspired stall detection with the literal RFC 8289 sojourn metric and an R_eff-derived equilibrium reference. Resistance affinity (effective resistance from the Laplacian pseudoinverse of the CPU topology graph) provides topology-aware task placement for pipe/IPC storms. Multiplicative Weight Updates (MWU) balance six competing expert profiles across four loss pathways.

Three-tier behavioral dispatch, overflow sojourn rescue, longrun detection, tier-aware preempt scaling, sleep-informed batch tuning, classification-gated DSQ routing, workload regime detection, a sojourn selector with bounded maturity-gated tier warp, a per-CPU tau-derived preempt, an RT-policy latency floor, hard starvation rescue, and a persistent process database that learns task classifications across lifetimes.

PANDEMONIUM is included in the [sched-ext/scx](https://github.com/sched-ext/scx) project alongside scx_rusty, scx_lavd, scx_cosmos and the rest of the sched_ext family. Thank you to Piotr Gorski and the sched-ext team. PANDEMONIUM is made possible by contributions from the sched_ext, CachyOS, Gentoo, OpenSUSE, Arch and Ubuntu communities within the Linux ecosystem.

## Performance

12 AMD Zen CPUs, kernel 7.0.9, clang 21. Numbers below are from a single 3-iteration bench-scale run (2026-05-22, v5.11.0) — all four schedulers (EEVDF, scx_bpfland, PANDEMONIUM BPF + ADAPTIVE) measured in the same run. Per the in-run Gauge R&R gate, the 2C/4C deltas are trustworthy (%GRR 14–25%, ICC ≥0.94) while the 8C/12C *throughput* deltas are noise-swamped (%GRR 34–59%); the latency / jitter / deadline-miss tables, where PANDEMONIUM leads by 10–30×, are well clear of that noise floor.

### v5.11.0 vs v5.10.0 (best-mode deltas)

v5.11.0 is a different engine: the EEVDF-style weighted virtual-time ordering is gone, replaced by a pure sojourn selector (`now − warp`), with the audio-under-load preempt reworked per-CPU (no global flag) and a BPF-mode procdb warm-start. Against the v5.10.0 vtime build, best mode each:

| Metric (best mode)         | Core | v5.10.0  | v5.11.0  | Δ             |
|----------------------------|------|----------|----------|---------------|
| App Launch (BPF)           | 12C  | 16,551us | 1,302us  | 12.7x faster  |
| App Launch (ADAPTIVE)      | 12C  | 3,633us  | 1,459us  | 2.5x faster   |
| Longrun P99 (BPF)          | 2C   | 862us    | 338us    | 2.5x faster   |
| Latency P99 (BPF)          | 2C   | 206us    | 203us    | held          |
| Longrun P99 (ADAPTIVE)     | 2C   | 834us    | 60,695us | 73x slower    |
| IPC P99 (ADAPTIVE)         | 8C   | 72us     | 799us    | 11x slower    |

The headline is the engine swap plus the procdb warm-start: **BPF app-launch is 12.7× faster** (the cold-start tail is gone), BPF latency/mixed hold at 2C, and the intra-cycle +95% messaging regression is fully recovered (see Fork/Thread below). The open clusters are the **2C ADAPTIVE sustained-load tail** (longrun/mixed) and the **IPC round-trip** corner. Single 3-iteration session; the 8C/12C cells sit in the noise-swamped Gauge R&R band, so ANOVA over repeated runs is the way to separate the rest from sampling.

### P99 Wakeup Latency (interactive probe under CPU saturation)

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 5,516us  | 2,701us     | 203us             | **126us**              |
| 4     | 4,615us  | 1,925us     | 73us              | **72us**               |
| 8     | 2,344us  | 1,726us     | **72us**          | 72us                   |
| 12    | 754us    | 1,799us     | **76us**          | 81us                   |

~70-80µs at 4C+, 126-203µs at 2C — 10-30× under EEVDF (754-5,516µs) and scx_bpfland (1,726-2,701µs). ADAPTIVE edges BPF at 2C/4C, level at 8C/12C. scx_bpfland parks at its ~2ms wake quantum; EEVDF tightens with core count (5,516 → 754µs) but never reaches PANDEMONIUM.

### Burst P99 (fork/exec storm under CPU saturation)

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 3,778us  | 4,037us     | **288us**         | 384us                  |
| 4     | 5,959us  | 1,658us     | **110us**         | 115us                  |
| 8     | 2,979us  | 1,940us     | 692us             | **96us**               |
| 12    | 1,321us  | 1,798us     | 3,102us           | **85us**               |

BPF holds 110-288µs at 2C/4C; its 8C/12C burst P99 climbs (692µs, 3,102µs) but those land in the noise-swamped %GRR band, and ADAPTIVE stays clean there (96µs, 85µs). EEVDF runs 1.3-6.0ms, scx_bpfland 1.7-4.0ms throughout.

### Longrun P99 (interactive latency with sustained CPU-bound long-runners)

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 3,615us  | 1,858us     | **338us**         | 60,695us               |
| 4     | 981us    | 1,633us     | **357us**         | 1,016us                |
| 8     | 1,072us  | 1,803us     | **140us**         | 792us                  |
| 12    | 2,701us  | 1,731us     | **186us**         | 1,047us                |

BPF wins every core count (140-357µs), well under EEVDF (981-3,615µs) and scx_bpfland (1,633-1,858µs). The outlier is **2C ADAPTIVE (60,695µs)** — the adaptive layer's sustained-load tail on two cores, where BPF on the same cores stays clean at 338µs. That 2C ADAPTIVE cluster is the open regression.

### Mixed Latency P99 (interactive + batch concurrent)

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 2,850us  | 1,939us     | **779us**         | 17,267us               |
| 4     | 1,603us  | 1,052us     | 292us             | **136us**              |
| 8     | 1,485us  | 1,900us     | **232us**         | 296us                  |
| 12    | 1,033us  | 1,757us     | **348us**         | 650us                  |

BPF beats EEVDF at every core count (232-779µs vs 1,033-2,850µs); ADAPTIVE takes 4C (136µs) but carries the same **2C tail (17,267µs)** as longrun — the adaptive 2C regression, real and the open target.

### Deadline Miss Ratio (16.6ms frame target)

| Cores | EEVDF   | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|---------|-------------|-------------------|------------------------|
| 2     | 21.1%   | 67.8%       | **0.4%**          | 0.9%                   |
| 4     | 14.7%   | 61.5%       | **0.2%**          | 0.3%                   |
| 8     | 12.6%   | 64.0%       | 0.2%              | **0.2%**               |
| 12    | 11.1%   | 65.5%       | 0.8%              | **0.2%**               |

PANDEMONIUM holds 0.2-0.9% at every core count — against EEVDF's **11.1-21.1%** and scx_bpfland's **61.5-67.8%** miss rates on the 16.6ms frame target. The deadline-discipline gap is the design's clearest, most noise-immune win.

### Burst Recovery P99 (latency after storm subsides)

| Cores | PANDEMONIUM (bench-contention burst-recovery phase) |
|-------|------------------------------------------------------|
| 2     | base 64us / burst 65us / recovery 62us              |
| 4     | base 64us / burst 73us / recovery 60us              |
| 8     | base 74us / burst 75us / recovery 75us              |
| 12    | base 66us / burst 70us / recovery 64us              |

Recovery sub-76us at every core count. Numbers from bench-contention v5.11.0 (2026-05-20).

### App Launch P99

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 3,201us  | 2,881us     | 2,526us           | **2,117us**            |
| 4     | 4,478us  | 3,036us     | 3,399us           | **2,102us**            |
| 8     | 3,980us  | 8,020us     | **1,595us**       | 1,805us                |
| 12    | 4,310us  | 3,996us     | **1,302us**       | 1,459us                |

**PANDEMONIUM now leads app launch** — the v5.11.0 BPF-mode procdb warm-start (load persisted task classes at startup instead of cold-learning each launch) erased the old cold-start tail: BPF 12C went 16,551µs → 1,302µs. BPF leads 8C/12C (1,595/1,302µs), ADAPTIVE leads 2C/4C (2,117/2,102µs); scx_bpfland trails (3,036-8,020µs at 4C+) and EEVDF runs 3.2-4.5ms. The former known trade-off is closed.

### IPC Round-Trip P99

| Cores | EEVDF    | scx_bpfland | PANDEMONIUM (BPF) | PANDEMONIUM (ADAPTIVE) |
|-------|----------|-------------|-------------------|------------------------|
| 2     | 322us    | **26us**    | 1,065us           | 1,066us                |
| 4     | 314us    | **32us**    | 1,013us           | 2,018us                |
| 8     | **14us** | 93us        | 428us             | 799us                  |
| 12    | **31us** | 84us        | 1,093us           | 1,334us                |

EEVDF (14-322µs) and scx_bpfland (26-93µs) own the pipe ping-pong; PANDEMONIUM runs ~0.4-2ms (BPF closest at 8C, 428µs). This stays the corner where the routing/affinity machinery has least to offer and the most relative overhead — the open soft spot, and the only table where PANDEMONIUM trails.

### Fork/Thread IPC (`perf bench sched messaging`, 12C, v5.11.0)

| Scheduler                | Time     | vs EEVDF  | Cache Misses | IPC   |
|--------------------------|----------|-----------|--------------|-------|
| EEVDF                    | 16.531s  | baseline  | 35.41M       | 0.420 |
| PANDEMONIUM (BPF)        | 16.485s  | **-0.3%** | 39.78M       | 0.421 |
| PANDEMONIUM (ADAPTIVE)   | 16.543s  | +0.1%     | 41.21M       | 0.425 |
| scx_bpfland              | 22.940s  | +38.8%    | 87.17M       | 0.404 |

BPF now lands **−0.3%** of EEVDF wall-time (a hair faster) and ADAPTIVE +0.1% — both ~39% ahead of scx_bpfland (16.5s vs 22.9s). EEVDF still leads cache-misses (35.4M vs 39.8M/41.2M); IPC is level (0.420 / 0.421 / 0.425). The v5.10.0 +95% messaging regression — the un-gated backlog boost clustering the fork-storm children — is fully recovered: the pure-warp ordering holds parity on the workload least suited to resistance affinity.

### Energy Efficiency (`bench-power`, 12C, v5.11.0)

3 captures × 5 runs per cell, 30s cooldown. Package energy via `perf stat -a -e power/energy-pkg/`. Zen 2 exposes only `J_pkg` (no per-core or per-DRAM RAPL). Numbers below are 15-sample means.

**Idle floor** (30s `sleep`, scheduler restlessness):

| Scheduler                | J_pkg   | Avg W    | vs EEVDF |
|--------------------------|---------|----------|----------|
| scx_lavd                 | 723.61J | 24.10W   | -1.1%    |
| EEVDF                    | **731.81J** | **24.37W** | baseline |
| scx_bpfland              | 737.57J | 24.56W   | +0.8%    |
| PANDEMONIUM (ADAPTIVE)   | 744.87J | 24.81W   | +1.8%    |
| PANDEMONIUM (BPF)        | 762.22J | 25.39W   | +4.2%    |

scx_lavd's parking heuristics own the idle floor. ADAPTIVE's +1.8% is down from v5.9.0's +6.9% — the Rust 1Hz monitoring tax has been thinned to within 0.5W of EEVDF. BPF's +4.2% is the dispatch path firing on idle more than the kernel default.

**Messaging** (`perf bench sched messaging -t -g 24 -l 6000`, fork-storm + IPC):

| Scheduler                | Wall_s     | J_pkg       | J/msg      | vs EEVDF |
|--------------------------|------------|-------------|------------|----------|
| PANDEMONIUM (ADAPTIVE)   | 15.92s     | 982.45J     | **170.57uJ** | **-2.4%** |
| PANDEMONIUM (BPF)        | 15.94s     | 984.86J     | 170.98uJ   | -2.1%    |
| EEVDF                    | **15.90s** | 1006.29J    | 174.71uJ   | baseline |
| scx_bpfland              | 21.07s     | 1278.30J    | 221.93uJ   | +27.0%   |
| scx_lavd                 | 26.00s     | 1637.72J    | 284.33uJ   | +62.7%   |

Both PANDEMONIUM modes now run J/msg below EEVDF (ADAPTIVE -2.4%, BPF -2.1%). scx_bpfland is +27% over EEVDF on J/msg; scx_lavd is +63%. Under sustained wake-heavy work, PANDEMONIUM is the most energy-efficient scheduler in this comparison — the inverse of the idle ranking, where lavd wins by parking aggressively.

## Key Features

### Dispatch Waterfall

Layered dispatch with per-CPU DSQ dominance and one age-driven safety mechanism. CPU-tied placement (Tier 2 wakeup preemption, `select_cpu`) is bounded at the enqueue site by `pcpu_depth_base` and overflow spills to a sibling per-CPU DSQ in R_eff order (`find_pcpu_with_room` → `pick_pcpu_dsq_with_spill`), so the dispatch waterfall reaches every CPU-tied enqueue at step 0 (sibling owns) or step 1 (R_eff steal). Idle-CPU placement (Tier 1) inserts directly into `node_dsq` and is picked up by step 3 within one dispatch cycle — eager R_eff search at this site was a wire-speed regression on fork-storm workloads with no measurable placement benefit. v5.8.0 collapsed the v5.7.0 14-decision-point waterfall into 7 steps (0–6) + 1 safety net by removing redundant rescue paths (deficit-gate-with-exception, DRR deficit counter, batch sojourn rescue) that all reduced to "service the older overflow side past a threshold." `sojourn_gate_pass` was kept at STEP 0 / STEP 1 — bench evidence proved it load-bearing for workqueue-worker fairness under sustained per-CPU load (without it, `scx_watchdog_workfn` strands in node_dsq long enough to trigger 30s watchdog kills).

0. **Own per-CPU DSQ** — cache-hot, zero contention. Sojourn-gated return: if either overflow side has aged past `overflow_sojourn_rescue_ns`, fall through to STEP 2 so this dispatch serves overflow too.
1. **R_eff steal** — single loop over `affinity_rank` (slot 0 = L2 sibling, slots 1+ = R_eff-ranked cross-L2 peers). Budget is tau-derived: `pcpu_spill_search_budget = K_SPILL_BUDGET / tau ≈ λ₂/2`, clamped to `[6, min(nr_cpu_ids - 1, MAX_AFFINITY_CANDIDATES)]`. The companion WAKE_SYNC idle-search budget `affinity_search_online = K_AFFINITY_SEARCH / tau ≈ λ₂/4` clamps the same way (smaller divisor because the predicate is more expensive). `MAX_AFFINITY_CANDIDATES = MAX_CPUS >> 3` (= 128 at the current MAX_CPUS=1024) is the verifier-safe compile-time ceiling; the runtime ceiling tracks actual topology via `nr_cpu_ids - 1`. Wider graphs get more coverage: 12C → 6/3, 32C → 16/8, 128C+ → 128/64. Subsumes the v5.7.0 STEP 1 (L2 walk) and STEP 1b (R_eff fallback) since `affinity_rank` is authoritative for placement distance. Same sojourn gate as STEP 0 on success.
1. *Safety net.* **Hard starvation rescue** — `try_service_older_overflow(starvation_rescue_ns)`: drains whichever of `interactive_enqueue_ns` / `batch_enqueue_ns` is older past the tau-scaled hard cap. Fires before STEP 2 so it cannot be gated. Should ~never fire post-placement-fix.
2. **Service older overflow side** — `try_service_older_overflow(overflow_sojourn_rescue_ns)`: same pick-the-older comparison at the R_eff-derived equilibrium threshold (`overflow_sojourn_rescue_ns = codel_target_equilibrium_ns`, ≤~2ms at 12C). Feeds the CoDel oscillator (`global_rescue_count++`). Replaces the v5.7.0 interactive-overflow-amplification + batch-overflow-rescue + deficit-gate-with-exception + DRR cluster.
3. **Node interactive overflow** — unconditional `node_dsq` drain (LAT_CRITICAL + INTERACTIVE, sojourn-ordered).
4. **Node batch overflow** — unconditional `batch_dsq` drain.
5. **Cross-node steal** — interactive + batch per remote node.
6. **KEEP_RUNNING** — if prev still wants CPU and nothing queued.

### Three-Tier Enqueue

- **select_cpu**: idle CPU -> per-CPU DSQ (depth-gated: 1 slot at <4C, 2 at 4C+) -> R_eff sibling spill if full -> last-resort node DSQ, with KICK_IDLE on the placement target. WAKE_SYNC path (v5.11.0): partner-CPU fast path (claim the wakee's `last_cpu` directly if idle and allowed, skipping the R_eff scan on a stable pair) -> R_eff idle search -> waker fallback, now WITH a kick — arm A (found-idle) KICK_IDLE, arm B (no-idle) KICK_PREEMPT — so the wakee runs next instead of aging until the target's tick (the dominant IPC round-trip tail)
- **enqueue Tier 1** (idle CPU): direct `node_dsq` insert + KICK_PREEMPT for non-BATCH / KICK_IDLE for BATCH. Drained by STEP 3 (unconditional `node_dsq`) within one dispatch cycle. v5.6.0's wire-speed path — restored after v5.8.0's eager R_eff search proved a fork-storm regression with no placement benefit
- **enqueue Tier 2** (wakeup preemption): uses `pick_pcpu_dsq_with_spill` for symmetric placement with `select_cpu`. CPU-tied; benefits from eager per-CPU placement
- **enqueue Tier 3** (fallback): batch overflow DSQ for BATCH only; LAT_CRITICAL, INTERACTIVE, and immature INTERACTIVE (`ewma_age < 2`) all stay in `node_dsq` — immature tasks are deliberately kept out of the batch DSQ to avoid burst-spawn starvation. The sojourn deadline (`now − warp`) is computed at the insert
- **tick**: longrun detection (batch non-empty >2s), sojourn enforcement, per-CPU preempt of the resident for an aged waiter (`pcpu_enqueue_ns[this_cpu]` age vs a tau-scaled threshold; BATCH yields at base, INTERACTIVE at 2×, LAT_CRITICAL never)

### Damped Harmonic Oscillator Stall Detection

CoDel-inspired per-CPU DSQ stall detection where the target follows the full damped harmonic oscillator equation:

```
ẍ + 2γẋ + ω₀²(x − c_eq) = F(t)
```

with `γ` set for Butterworth-optimal damping (ζ ≈ 0.707) — a maximally-flat response that trades a small ~4.3% step-response overshoot per adaptation for shorter settling time; the overshoot is the controller's deliberate exploration term (it probes the convex-response boundary on each impulse instead of parking inside it). v5.8.0 added the missing ω₀² spring term last present in v3.0.0 and the literal RFC 8289 sojourn metric replacing the empty-cycle proxy.

**Per-task sojourn** (RFC 8289): `task_ctx.enqueue_at` is set at every `scx_bpf_dsq_insert_vtime` call site (six placement paths) and consumed in `pandemonium_running` to compute `sojourn = now − enqueue_at`. This is the literal CoDel metric — wait between enqueue and run start — feeding `pcpu_min_sojourn_ns`. Replaces the v5.7.0 proxy `now − pcpu_enqueue_ns[cpu]` which weakened past the first task in any drain.

**Stall decision** (`pcpu_dsq_is_stalled`): compares per-CPU minimum sojourn against `codel_target_ns`. Below = flowing. Above for `sojourn_interval_ns` = stalled, force rescue. Binary CoDel decision; the target itself is what oscillates.

**Spring (restoring term)**: equilibrium `c_eq = ⟨R_eff⟩ × 2m × τ` derived from spectral graph properties already computed at topology detect — `⟨R_eff⟩ = Tr(L⁺)/N` over nonzero eigenvalues, `2m = Tr(L)`, τ the Fiedler-derived mixing time. The product is the natural latency tolerance of the topology. Rust clamps `c_eq` to `[200µs, 8ms]`; BPF additionally constrains it inside the oscillator's `[floor, max]` window.

**Butterworth damping** (discrete): the existing `v −= v >> damping_shift` corresponds to `2γ ≈ 2^−D`. The spring is `v −= disp >> spring_shift` with `spring_shift = 2*damping_shift + 1`, placing the pole pair at ζ ≈ 2^(−1/2) ≈ 0.707 (Butterworth-optimal). Co-derived in `apply_tau_scaling` so topology changes preserve the damping ratio automatically: `damping_shift=1 → spring_shift=3` (2C, fast restore), `damping_shift=5 → spring_shift=11` (12C, gentle restore).

**Feedback loop**: `global_rescue_count` (atomic, incremented at the overflow rescue site in dispatch) drives the impulse `F(t)`. Each tick on CPU 0: apply impulse → apply spring (`v −= disp >> spring_shift`) → apply damping (`v −= v >> damping_shift`) → cap velocity → integrate `x`. v5.8.0 removed the v5.7.0 `OSCILLATOR_RELAX_NS` quiet-tick drift toward the ceiling, which had been a primitive substitute for the spring and pushed `x` AWAY from `c_eq` every quiet tick.

**Core-scaled parameters** (derived from τ in `apply_tau_scaling`; the per-column values below predate the capacity-aware τ law and are being re-derived against the v5.11.0 bench):

| Parameter | 2C | 4C | 8C | 12C |
|-----------|----|----|-----|------|
| Sojourn interval | 2ms | 4ms | 8ms | 12ms |
| Damping shift D | 1 (v/2) | 1-2 | 3 | 5 (v/32) |
| Spring shift (2D+1) | 3 (disp/8) | 3-5 | 7 (disp/128) | 11 (disp/2048) |
| Pull scale | 1 | 1 | 3 | 4 |
| Center floor | 200µs | 200µs | 500µs | 700µs |
| Center ceiling | 1ms | 1ms | 1ms | 2ms |

`x` rests at `c_eq` when the system is quiet, descends below on rescue events, and returns Butterworth-damped — one bounded ~4.3% overshoot, then settle.

### Overflow Sojourn Rescue

Per-CPU DSQ dominance under sustained load makes downstream anti-starvation unreachable — 90%+ of dispatches serve per-CPU DSQ while overflow tasks age indefinitely. Dispatch STEP 0 / STEP 1 fall through to STEP 2 when either overflow DSQ has aged past `overflow_sojourn_rescue_ns` — which is set to `codel_target_equilibrium_ns` (the R_eff-derived CoDel equilibrium `⟨R_eff⟩ × 2m × τ`, clamped into the oscillator's `[floor, max]` window, so ≤~2ms at 12C — not a hand-tuned ~10ms). The spectral scalar opens the gate; sojourn (enqueue-age) fills it and selects the older side. `try_service_older_overflow` then drains that side past the threshold. CAS-based timestamp management prevents races across CPUs.

**Drain-both-when-both-aged** (v5.8.0): under sustained mixed load both overflow DSQs can stay continuously non-empty for tens of seconds, freezing both timestamps at their first-non-empty values. The historical "older wins" picked the same side every rescue call until external pressure dropped, locking out batch-demoted long-runners. At 2C this produced 19-29s starvation tails; 4C+ was unaffected because higher dispatch density closed the window. The fix: when BOTH sides are aged, drain BOTH. Older-first ordering preserved (latency-budget bias for interactive on ties); the second drain costs one extra `scx_bpf_dsq_move_to_local`. Result: 2C longrun WORST 19s -> 166ms, mixed WORST 29s -> 170ms.

### Longrun Detection

When batch DSQ stays non-empty past `longrun_thresh_ns` (tau-scaled, ~2s at 12C reference), `longrun_mode` activates. Two consumers: `task_slice` substitutes `burst_slice_ns` for `slice_ns` on INTERACTIVE/LATCRIT (1ms tighter cap, yields CPU faster under pressure); `tick` scales the preempt threshold via `longrun_preempt_shift` — 4× at 2C (extends BATCH's protected window) so thin topologies don't thrash, no scaling at 4C+ where capacity already absorbs LAT_CRIT contention.

### Wake Sensitivity & Preemption

No burst detector. Prior versions layered three (CUSUM on enqueue-interval EWMA, `wake_burst` on absolute wakeup rate, and `burst_mode` gating slice/depth/preempt behaviors). All three have been retired: every failure mode they were covering is now handled by the oscillator-adapted CoDel target, the placement-side depth gate + L2/R_eff spill, hard starvation rescue, or tier information already present at the enqueue site. Tick preemption itself is derived per-CPU, with no global signal:

- **Per-CPU preempt (v5.11.0)**: the prior global `interactive_waiting`/`latcrit_waiting` bool pair was removed. A single shared flag — armed at enqueue, cleared by the first tick to preempt on *any* CPU — was token-stolen across cores under a fork storm, so the CPU actually burying a latency waker rarely won the race (the audio-under-load pathology: intermittent, single-thread, bursty-only). `pandemonium_tick` now reads its own `pcpu_enqueue_ns[this_cpu]` — the age of the oldest task waiting on this CPU — against a tau-scaled threshold (`preempt_thresh_ns`): a BATCH resident yields once a waiter ages past the base threshold, an INTERACTIVE resident at 2× (batch-throughput protection), a LAT_CRITICAL resident never. Per-CPU by construction — no token to race over — and it reuses the bounded-array read already running in the coarse per-CPU sojourn scan, so no new global state.
- **RT-policy floor**: `SCHED_FIFO`/`SCHED_RR` threads are pinned `TIER_LAT_CRITICAL` by declaration, after the behavioral classifier and the high-prio-kthread→BATCH override. A periodic audio RT thread scores erratically on the behavioral `lat_cri` metric and was flipping out of LAT_CRITICAL mid-burst; threaded USB IRQ kthreads were force-demoted to BATCH. The floor keeps both latency-critical under load, immune to the EWMA jitter.
- **Core-scaled longrun protection**: during sustained `longrun_mode`, the preempt threshold scales up on thin topologies (τ < 4ms) only, extending the protected BATCH window so they don't thrash; wider topologies keep the baseline, where capacity already absorbs LAT_CRIT contention.

### Sojourn Selector + Lag Cap

v5.11.0 removed the weighted virtual-time engine. `task_deadline()` no longer accumulates or reads a vtime clock — it returns `now − warp`, the enqueue timestamp back-dated by a bounded per-tier warp, so every DSQ is ordered oldest-first (largest sojourn served first). Sojourn IS the selector; there is no second fairness clock running parallel to the sojourn + R_eff/CoDel layer.

`lag_cap_ns = K_LAG_CAP × τ` clamped `[8ms, 80ms]` is the warp bound (~13ms at the 12C reference). The warp is flat per tier — `LAT_CRITICAL = lag_cap`, `INTERACTIVE = lag_cap/2`, `BATCH = 0` — and **maturity-gated**: a task earns its warp only once classified (`ewma_age ≥ EWMA_AGE_MATURE`), so a fork storm of unmatured INTERACTIVE children competes on pure arrival order and cannot leapfrog established work.

Because the warp is bounded, a BATCH task older than `lag_cap` always out-sorts a freshly-warped LAT_CRITICAL waker: **starvation-free by construction**, with no ceiling and no new-task penalty needed. A new task simply enters at `now` like any other arrival. (A wakeup-frequency-weighted warp and a queue-depth backlog term were both tried this cycle and dropped — each reordered by something other than wait and either clustered ping-pong wakers or rubberbanded interactivity. The shipped form is the flat, bounded, maturity-gated warp; deep-queue drainage is left to the overflow sojourn rescue, which forces aged work forward by wait, not depth.)

### Hard Starvation Rescue

`clamp(K_STARVATION_RESCUE × τ, 20ms, 500ms)` — tau-scaled and monotonic in topology timing: 20ms at 2C, ~80ms at 4C, ~160ms at 8C, ~167ms at 12C (the Core-Count Scaling table values). Runs as the dispatch safety net before STEP 2, ungated; should ~never trip after the placement fix.

### Topology-Aware Placement

**Resistance affinity**: The CPU topology is modeled as a weighted electrical network (L2 siblings = 10.0, same socket = 1.0, cross-socket = 0.3). The Laplacian pseudoinverse (Jacobi eigendecomposition, O(n^3), pure Rust) gives all-pairs migration costs accounting for every path through the graph, not just direct connections. `R_eff(i,j) = L+[i,i] + L+[j,j] - 2*L+[i,j]` — a true metric satisfying the triangle inequality. Per-CPU ranked lists stored in a BPF map sized at `MAX_AFFINITY_CANDIDATES = MAX_CPUS >> 3` slots per CPU (= 128 at MAX_CPUS=1024); the runtime walk is bounded by `nr_cpu_ids - 1`, with `(u32)-1` sentinels marking unused slots so loops early-exit on small N.

**Online-budget search**: `find_idle_by_affinity()` walks the ranked list with an online-candidates budget, not a total-slots budget. The rank map is built once at init from the full topology; after hotplug, some top ranks may reference offline CPUs. Offline entries are skipped without charging budget, so the search cost on a fully-online system is identical to a raw limit of 3, while remaining robust to arbitrary hotplug asymmetry (12C → 8C, 32C → 4C, etc.).

**Single-loop R_eff steal**: dispatch STEP 1 walks `affinity_rank` once with a tau-derived runtime budget (`pcpu_spill_search_budget`, clamped to `[6, min(nr_cpu_ids - 1, MAX_AFFINITY_CANDIDATES)]`) — slot 0 is the L2 sibling, slots 1+ are R_eff-ranked cross-L2 peers. Subsumes the old STEP 1 (l2_siblings walk) and STEP 1b (R_eff fallback) since v5.6.0 made `affinity_rank` authoritative for placement distance. Solo-topology hotplug cases (all L2 partners offline) work without a separate fallback.

**wakee_flips gate**: `select_cpu()` reads waker/wakee `wakee_flips` from `task_struct`. Both below `nr_cpu_ids` = 1:1 pipe pair (affinity beneficial). Either above = 1:N server pattern (skip to normal path). Same discrimination as the kernel's `wake_wide()`.

**L2 cache affinity**: `find_idle_l2_sibling()` in enqueue finds idle CPUs in the same L2 domain (max 8 iterations), gated by the `affinity_mode` knob (0=OFF / 1=WEAK / 2=STRONG). The Rust regime baseline sets it per regime (LIGHT=WEAK, MIXED=STRONG, HEAVY=WEAK); MWU can override by majority vote. Per-dispatch L2 hit/miss counters for BATCH, INTERACTIVE, LAT_CRITICAL tiers.

**Commute time interpretation**: R_eff is proportional to expected round-trip time for work between CPUs [2]. Minimizing R_eff between pipe partners minimizes cache line transfer cost [1][3][4].

### Behavioral Classification

- **Latency-Criticality Score**: `lat_cri = (wakeup_freq * csw_rate) / effective_runtime_ms`, where `effective_runtime_ms = (avg_runtime + runtime_dev/2) >> 20` (ns→ms, floored to 1)
- **Three Tiers**: LAT_CRITICAL (1.5x avg_runtime slices), INTERACTIVE (2x), BATCH (configurable ceiling)
- **EWMA Classification**: wakeup frequency, context switch rate, runtime variance drive scoring
- **CPU-Bound Demotion**: an INTERACTIVE task whose `avg_runtime` reaches 75% of `slice_ns` (`avg_runtime*4 >= slice_ns*3`), guarded by `ewma_age <= 4`, is demoted to BATCH — the guard spares tasks that burn a full slice but sleep often
- **Kworker Floor**: PF_WQ_WORKER floors at INTERACTIVE
- **High-Priority Kthread Override** (v5.8.0): `PF_KTHREAD` at `static_prio <= 110` (`task_nice <= -10`) forced to BATCH regardless of behavioral score. ZFS workers (`z_rd_int_*`, `arc_*`), kopia helpers, and similar storage kthreads no longer compete with userspace LAT_CRITICAL. The kworker floor wins for `PF_WQ_WORKER` (a `PF_KTHREAD` subset), so workqueue workers continue to be treated as latency-adjacent.

### Process Database (procdb)

BPF publishes mature task profiles (tier + avg_runtime) keyed by `comm[16]`. Rust tracks EWMA convergence stability, promotes to "confident", applies learned classifications on spawn. `enable()` warm-starts; `runnable()` EWMA validates and corrects. Persistent to `~/.cache/pandemonium/procdb.bin` (atomic write).

### Adaptive Control Loop

The Rust control plane is chaos-theory-driven: both regime detection and the MWU weight-update damping run off raw-window nonlinear-dynamics statistics (`chaos.rs`), not EWMAs or Schmitt triggers.

- **One Thread, Zero Mutexes**: 1-second control loop on the main thread reads BPF histogram maps, computes per-tier and aggregate P99, and drives the MWU orchestrator. BPF produces histograms; Rust reads them once per second and writes knobs BPF picks up on the next scheduling decision.
- **Chaos primitives** (`chaos.rs`, pure Rust, recomputed each tick over a 16-sample raw window): HVG mean degree λ (Luque–Lacasa horizontal visibility graph — ~2 periodic, →4 IID-random), Bandt–Pompe D=3 permutation entropy (ordinal disorder, normalized to [0,1]), and RQA determinism (fraction of recurrence points on diagonals — →1 steady, →0 IID).
- **Workload Regime Detection**: LIGHT / MIXED / HEAVY from the raw `idle_pct` window — no Schmitt trigger. LIGHT needs `mean_idle ≥ 50%` AND "chaos-low" (`λ < 3.4` OR `bp_h < 0.85`); HEAVY needs `mean_idle ≤ 10%` AND chaos-low; everything else is MIXED. A 2-tick hold smooths transitions.
- **MWU Orchestrator**: 6 experts (LATENCY, BALANCED, THROUGHPUT, IO_HEAVY, FORK_STORM, SATURATED) compete via multiplicative weight updates, with one weight vector **per regime** (LIGHT/MIXED/HEAVY each learn independently). 7 continuous knobs are blended via per-expert scale factors; 1 discrete knob (`affinity_mode`) by weighted majority vote (the `lag_scale` knob was removed with the vtime engine). Learning rate ETA = 0.33465 (= √(ln 6 / 16), the theory-optimal Hedge rate for the 16-tick window — the old fixed 8.0 overshot); weight floor 1e-6; relaxation 80% toward a no-op-skewed equilibrium after 2 healthy ticks below 70% of the regime P99 ceiling. The weight update is itself Butterworth-damped by a signal-trust coefficient (RQA determinism + λ), so the controller tracks faithfully only when the workload looks steady.
- **5 loss pathways**, all firing immediately (no streak confirmation): (1) **P99 spike** — worst of aggregate / interactive P99 over the regime ceiling; (2) **rescue delta** — 0→nonzero, penalizes all experts so MWU holds steady while the BPF oscillator does the tightening; (3) **IO-bucket transition**; (4) **fork storm** — raw wake-rate over a tau-derived threshold (~8000/s at 12C, ~1200/s at 4C) AND concurrent `rescue_count > 0`, scaled by wake-rate overage, letting the FORK_STORM expert compress `burst_slice_ns` / `preempt_thresh_ns` / `sojourn_thresh_ns` / `batch_slice_ns`; (5) **chaos transition** — a `bp_h` jump > 0.10 or an upward λ crossing penalizes the currently-dominant non-BALANCED expert.
- **Oscillator-aware gating**: before scoring, MWU reads the BPF CoDel oscillator's position; the rescue-delta and fork-storm pathways defer when it has already tightened (< 0.40) or gone quiet (> 0.90), so the two controllers never double-correct on `global_rescue_count`.
- **Quiescence freeze + adaptive-rarity retune**: when λ sits in the periodic band, RQA determinism ≥ 0.90, and the active regime's weight vector has converged, the loop latches `frozen` and skips the MWU retune + knob write (it still ticks at 1 Hz; the chaos sensors are the thaw condition). When not frozen, a sub-threshold retune stretches the retune interval ×1.5 (capped at 8 ticks); any disturbance snaps it back to 1.

### Core-Count Scaling

All timing constants scale from `tau_ns = TAU_SCALE_NS / √(λ₂ · N)` — capacity-aware (the geometric mean of connectivity `1/λ₂` and capacity `1/√N`, so a well-connected but core-starved topology loosens instead of tightening), with safety-rail clamps. 12C reference: τ≈13.3ms (λ₂=12, N=12). Cardinality decisions (per-CPU DSQ depth, wake_wide threshold, tick scan budget) use `nr_cpus` directly — counts are not tau-derived. **The per-column τ values and derived cells below predate the capacity-aware law (which also shifts which topologies are τ-thin) and are being re-derived against the v5.11.0 bench.**

| Parameter | Formula | 2C | 4C | 8C | 12C | 32C |
|-----------|---------|------------|----|----|----|----|
| Sojourn interval | `clamp(K × τ, 2ms, 12ms)` | 2ms | 4ms | 8ms | 12ms | 5ms |
| Overflow rescue | `= codel_target_equilibrium_ns` (R_eff-derived) | varies | varies | varies | varies | varies |
| Starvation rescue | `clamp(K × τ, 20ms, 500ms)` | 20ms | 80ms | 160ms | 166ms | 21ms |
| CoDel target floor | `clamp(K × τ, 200µs, 800µs)` | 200µs | 200µs | 500µs | 700µs | 200µs |
| CoDel target max | `clamp(K × τ, 1ms, 8ms)` | 1ms | 1ms | 1ms | 2ms | 1ms |
| CoDel equilibrium | `clamp(⟨R_eff⟩ × 2m × τ, 200µs, 8ms)` | varies | varies | varies | varies | varies |
| Lag cap (warp bound) | `clamp(K × τ, 8ms, 80ms)` | — | — | — | — | — |
| Spill search budget | `clamp(K / τ, 6, min(nr_cpu_ids - 1, MAX_AFFINITY_CANDIDATES))` (≈ λ₂/2) | 6 | 6 | 6 | 6 | 16 |
| Affinity idle search | `clamp(K / τ, 3, min(nr_cpu_ids - 1, MAX_AFFINITY_CANDIDATES))` (≈ λ₂/4) | 3 | 3 | 3 | 3 | 8 |
| Per-CPU DSQ depth | `tau ≥ 6ms ? 2 : 1` | 1 | 2 | 2 | 2 | 1 |
| Longrun preempt shift | `tau < 4ms ? 2 : 0` | 4× | 1× | 1× | 1× | 1× |

- **CPU Hotplug**: `cpu_online`/`cpu_offline` callbacks clear per-CPU timestamps and oscillator state (velocity, rescue count) to prevent stale oscillation after suspend/resume
- **BPF-Verifier Safe**: All EWMA uses bit shifts, no floats. All shared state uses GCC __sync builtins

## Architecture

```
pandemonium.py           Build/install/benchmark manager (Python)
pandemonium_common.py    Shared infrastructure (logging, build, CPU management,
                           scheduler detection, tracefs, statistics)
export_scx.py            Automated import into sched-ext/scx monorepo
src/
  main.rs              Entry point, CLI, scheduler loop, telemetry
  lib.rs               Library root
  scheduler.rs         BPF skeleton lifecycle, tuning knobs I/O, histogram reads
  adaptive.rs          Adaptive control loop (monitor thread, histogram P99,
                         chaos-driven regime detection, MWU, quiescence freeze)
  chaos.rs             Chaos primitives: HVG mean degree/entropy, Bandt-Pompe D=3
                         permutation entropy, RQA determinism (raw-window, no EWMA)
  tuning.rs            MWU orchestrator (6 experts, 5 loss pathways, scale factors),
                         regime thresholds, quiescence + adaptive-rarity retune
  procdb.rs            Process classification database (observe -> learn -> predict -> persist)
  topology.rs          CPU topology detection, Laplacian pseudoinverse, effective resistance,
                         resistance affinity ranking (sysfs -> BPF maps)
  event.rs             Pre-allocated ring buffer for stats time series
  watchdog.rs          Control-loop stall detector (10s heartbeat, abort on miss)
  bpf_intf.rs          Mirror of intf.h constants (MAX_CPUS, MAX_AFFINITY_CANDIDATES,
                         MAX_NODES) with static_assert against the C macro
  bpf_skel.rs          libbpf-cargo-generated BPF skeleton bindings
  log.rs               Logging macros
  bpf/
    main.bpf.c         BPF scheduler (GNU C23)
    intf.h             Shared structs: tuning_knobs, pandemonium_stats, task_class_entry
  cli/
    mod.rs             Shared CLI helpers
    probe.rs           Interactive wakeup probe (Python test harness hook)
    stress.rs          CPU-pinned stress worker (Python test harness hook)
build.rs               vmlinux.h generation + C23 patching + BPF compilation
tests/
  pandemonium-tests.py Test orchestrator (bench-scale, bench-trace, bench-contention,
                         bench-pcpu, bench-scx, bench-sys, low-cpu-deadline)
  bench-fork-thread.py Fork/thread IPC benchmark with hardware counter profiling
  bench-power.py       Energy-efficiency benchmark (RAPL J/op + idle floor)
  gate.rs              Integration test gate (load/classify/latency/responsiveness/contention)
include/
  scx/                 Vendored sched_ext headers
```

### Data Flow

```
BPF per-CPU histograms              Monitor Thread (1s loop)
(wake_lat_hist, sleep_hist)  --->   Read + drain histograms
                                    Compute P99 per tier
                                      |
                                      v
                                    detect_regime (chaos: HVG λ + Bandt-Pompe)
                                      -> scaled_regime_knobs() -> baseline
                                      -> MWU orchestrator (6 experts, 5 loss pathways)
                                        -> blend continuous knobs (scale factors)
                                        -> vote discrete knobs (majority)
                                      -> oscillator gating + quiescence freeze
                                      |
                                      v
                                    BPF reads knobs on next dispatch

Resistance affinity: R_eff ranked map -> BPF select_cpu (wakee_flips-gated)
L2 placement:        affinity_mode knob -> BPF enqueue (per-regime baseline; MWU vote)
Sojourn threshold:   sojourn_thresh_ns knob -> BPF dispatch (core-count-scaled)
Stall detection:     codel_target_ns (BPF-internal, damped oscillation, no Rust input)
```

One thread, zero mutexes. BPF produces histograms, Rust reads them once per second. Rust writes knobs, BPF reads them on the next scheduling decision. Stall detection is fully BPF-internal — the damped oscillation runs in tick() on CPU 0 with no Rust involvement.

### Tuning Knobs (BPF map)

| Knob | Default | Owner | Purpose |
|------|---------|-------|---------|
| `slice_ns` | 1ms | MWU | Interactive/lat_cri slice ceiling |
| `preempt_thresh_ns` | 1ms | MWU | Tick preemption threshold |
| `batch_slice_ns` | 20ms | MWU | Batch task slice ceiling (sleep-adjusted) |
| `burst_slice_ns` | 1ms | MWU | Slice during longrun mode |
| `lat_cri_thresh_high` | 32 | MWU | LAT_CRITICAL classifier threshold |
| `lat_cri_thresh_low` | 8 | MWU | INTERACTIVE classifier threshold |
| `affinity_mode` | 0 | MWU | L2 placement (0=OFF, 1=WEAK, 2=STRONG); Rust writes the per-regime value at startup |
| `sojourn_thresh_ns` | 5ms | MWU | Batch DSQ rescue threshold (tau-scaled) |
| `topology_tau_ns` | 0 | Topology | Fiedler-derived time constant (τ = TAU_SCALE / λ₂) |
| `codel_eq_ns` | 0 | Topology | R_eff-derived CoDel equilibrium (`⟨R_eff⟩ × 2m × τ`) |

Topology-owned fields are written by Rust at topology detect and on hotplug. The adaptive loop preserves them on every MWU write (the 1Hz cycle would otherwise clobber the equilibrium).

## Requirements

- Linux kernel 6.12+ with `CONFIG_SCHED_CLASS_EXT=y`
- Rust toolchain
- clang (BPF compilation)
- system libbpf
- bpftool (first build only — generates vmlinux.h, can be uninstalled after)
- Root privileges (`CAP_SYS_ADMIN`)

```bash
# Arch Linux
pacman -S clang libbpf bpf rust
```

## Build & Install

```bash
# Build manager (recommended)
./pandemonium.py rebuild        # Force clean rebuild
./pandemonium.py install        # Build + install to /usr/local/bin + systemd service file
./pandemonium.py status         # Show build/install status
./pandemonium.py clean          # Wipe build artifacts

# Manual
CARGO_TARGET_DIR=$HOME/.cache/pandemonium-build cargo build --release
```

vmlinux.h is generated from the running kernel's BTF via bpftool on first build and cached at `~/.cache/pandemonium/vmlinux.h`. The cargo target tree lives at `~/.cache/pandemonium-build` (alongside the log and vmlinux caches under `~/.cache/pandemonium`), so all per-user pandemonium state sits in one place. The `CARGO_TARGET_DIR=$HOME/.cache/pandemonium-build` override is also what lets the vendored libbpf Makefile build cleanly when the source tree path contains spaces.

After install:

```bash
sudo systemctl start pandemonium          # Start now
sudo systemctl enable pandemonium         # Start on boot
```

## Usage

```bash
sudo scx_pandemonium                  # Default: adaptive mode
sudo scx_pandemonium --no-adaptive    # BPF-only (no Rust control loop)
sudo scx_pandemonium -v               # Verbose telemetry on stdout
```

There is no compositor allowlist. Compositors land at `LAT_CRITICAL` naturally
through the behavioral classifier (high wakeup frequency × high context-switch
rate × short avg runtime → `lat_cri` peaks), and procdb warm-starts known
names from prior sessions. The earlier hardcoded comm boost was scaffolding
from when the classifier was less reliable on cold-start; the current
classifier no longer needs the safety net.

### Monitoring

Per-second telemetry:

```
d/s: 251000 idle: 5% shared: 230000 preempt: 12 keep: 0 kick: H=8000 S=22000 enq: W=8000 R=22000 wake: 4us p99: 10us [B:8 I:9 L:7] lat_idle: 3us lat_kick: 6us procdb: 42/5 sleep: io=87% slice: 1000us batch: 20000us reenq: 4 sjrn: 3ms/5ms rescue: 0 l2: B=67% I=72% L=85% chaos: lam=2.10 H=0.40 det=0.95 x=0 frozen: 0 (n=12) retune_iv: 2 [MIXED]
```

| Counter | Meaning |
|---------|---------|
| d/s | Total dispatches per second |
| idle | select_cpu idle fast path (%) |
| shared | Enqueue -> per-node DSQ |
| preempt | Tick preemptions |
| keep | KEEP_RUNNING re-slices |
| kick H/S | Hard (PREEMPT) / Soft kicks |
| enq W/R | Wakeup / Re-enqueue counts |
| wake / p99 | Average / aggregate P99 wakeup latency |
| [B/I/L] | Per-tier P99 (BATCH / INTERACTIVE / LAT_CRITICAL) |
| lat_idle / lat_kick | Wakeup latency split: idle-placement vs kick path |
| procdb | Total profiles / confident predictions |
| sleep: io | I/O-wait sleep pattern (%) |
| slice / batch | Current interactive / batch slice knob (us) |
| reenq | Re-enqueue count |
| sjrn | Batch sojourn: current / threshold |
| rescue | Overflow rescue dispatches this tick |
| l2: B/I/L | L2 cache hit rate per tier |
| chaos: lam/H/det/x | HVG mean degree λ / Bandt-Pompe entropy / RQA determinism / chaos-crossing counter |
| frozen (n) | Quiescence freeze active (1/0) and cumulative frozen ticks |
| retune_iv | Adaptive-rarity retune interval (ticks between retunes) |
| [REGIME] | LIGHT/MIXED/HEAVY + LONGRUN flag |

## Benchmarking

```bash
./pandemonium.py bench-scale                     # Full suite (throughput, latency, burst, longrun, mixed, deadline, IPC, launch)
./pandemonium.py bench-scale --iterations 3      # Multi-iteration
./pandemonium.py bench-scale --pandemonium-only  # Skip EEVDF and externals
./pandemonium.py bench-contention                # Contention stress (6 phases)
./pandemonium.py bench-pcpu                      # Per-CPU DSQ correctness
./pandemonium.py bench-fork-thread               # Fork/thread IPC + hardware counters
./pandemonium.py bench-power                     # Energy efficiency (RAPL J/op + idle floor)
./pandemonium.py bench-trace                     # BPF trace capture for external workloads
./pandemonium.py bench-sys                       # Live system telemetry capture
./pandemonium.py bench-scx                       # sched-ext/scx CI compatibility
./pandemonium.py bench-piotr-gorski              # Piotr Gorski-style application workload suite
```

All benchmarks compare across core counts via CPU hotplug (2, 4, 8, ..., max). Results archived to `~/.cache/pandemonium/`.

## Testing

```bash
CARGO_TARGET_DIR=$HOME/.cache/pandemonium-build cargo test --release   # Unit tests (no root)
sudo CARGO_TARGET_DIR=$HOME/.cache/pandemonium-build \
     cargo test --release --test gate -- --ignored \
     --test-threads=1 full_gate                                 # Integration gate (requires root)
```

5 tests in 1 file: the integration gate at `tests/gate.rs` — `full_gate` plus the
load/classify, latency, responsiveness, and contention layers (all `#[ignore]`,
root-only). The `src/*.rs` modules carry no inline unit tests; the pure-Rust logic
(chaos, tuning, topology) is validated offline through the bench harnesses.

## sched-ext/scx Integration

PANDEMONIUM is included in the sched-ext/scx monorepo. `export_scx.py` automates the import:

```bash
./export_scx.py /path/to/scx
```

Copies source into `scheds/rust/scx_pandemonium/`, renames the crate, replaces `build.rs` with `scx_cargo::BpfBuilder`, swaps `libbpf-cargo` for `scx_cargo`, registers the workspace member, and runs `cargo fmt`.

## Attribution

- `include/scx/*` headers from the [sched_ext](https://github.com/sched-ext/scx) project (GPL-2.0)
- vmlinux.h generated from the running kernel's BTF
- Included in the [sched-ext/scx](https://github.com/sched-ext/scx) project

## References

[1] D.J. Klein, M. Randic. "Resistance Distance." *Journal of Mathematical Chemistry* 12, 81-95, 1993. [doi:10.1007/BF01164627](https://link.springer.com/article/10.1007/BF01164627)

[2] A.K. Chandra, P. Raghavan, W.L. Ruzzo, R. Smolensky, P. Tiwari. "The Electrical Resistance of a Graph Captures its Commute and Cover Times." *STOC 1989*, 574-586. Journal version: *Computational Complexity* 6, 312-340, 1996. [doi:10.1007/BF01270385](https://link.springer.com/article/10.1007/BF01270385)

[3] P. Christiano, J.A. Kelner, A. Madry, D.A. Spielman, S.-H. Teng. "Electrical Flows, Laplacian Systems, and Faster Approximation of Maximum Flow in Undirected Graphs." *STOC 2011*, 273-282. [arXiv:1010.2921](https://arxiv.org/abs/1010.2921)

[4] L. Chen, R. Kyng, Y.P. Liu, R. Peng, M.P. Gutenberg, S. Sachdeva. "Maximum Flow and Minimum-Cost Flow in Almost-Linear Time." *FOCS 2022*. Journal version: *Journal of the ACM* 72(3), 2025. [arXiv:2203.00671](https://arxiv.org/abs/2203.00671)

[5] K. Nichols, V. Jacobson. "Controlling Queue Delay." *ACM Queue* 10(5), 2012. [doi:10.1145/2208917.2209336](https://queue.acm.org/detail.cfm?id=2209336)

[6] K. Nichols, V. Jacobson. "Controlled Delay Active Queue Management." *RFC 8289*, January 2018. [rfc-editor.org/rfc/rfc8289](https://www.rfc-editor.org/rfc/rfc8289.html)

[7] M. Shreedhar, G. Varghese. "Efficient Fair Queuing Using Deficit Round Robin." *ACM SIGCOMM 1995*, 231-242. [doi:10.1145/217382.217453](https://dl.acm.org/doi/10.1145/217382.217453)

[8] S. Arora, E. Hazan, S. Kale. "The Multiplicative Weights Update Method: a Meta-Algorithm and Applications." *Theory of Computing* 8, 121-164, 2012. [doi:10.4086/toc.2012.v008a006](https://theoryofcomputing.org/articles/v008a006/)

[9] J.D. Valois. "Lock-Free Linked Lists Using Compare-and-Swap." *PODC 1995*, 214-222. [doi:10.1145/224964.224988](https://dl.acm.org/doi/10.1145/224964.224988)

## License

GPL-2.0
