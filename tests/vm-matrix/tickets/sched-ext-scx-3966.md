## scx_pandemonium on the 7.4 verifier: Root cause and fix

**Summary.** This is a verifier change, not a change in scx_pandemonium: The
same scx commit (`5d5ebf506de7`) passed `veristat-dev` on bpf-next
`7101b10cf281` and failed on bpf-next `b73855c31c7b` six hours later. The
verifier that rejects it is already in mainline: `kernel/bpf/verifier.c`,
`scev.c` and `states.c` in v7.4-rc1 are the same blobs as in `b73855c31c7b`
and in sched_ext for-next `f7008fb94ee0`. Both `pandemonium_select_cpu` and
`pandemonium_enqueue` fail for the same reason, in the `find_spill_seat()`
search loop they both inline. The fix moves that loop and its twin in
`find_idle_by_affinity()` onto `bpf_for` with their bodies unchanged, and it
ships in v5.24.

### What fails

| kernel | scx `b0efa3f614c0`, as CI built it | v5.24, before the fix | v5.24, with `bpf_for` |
|---|---|---|---|
| bpf-next `d346f5906967` | rejected | rejected | verifies, loads, runs, unloads |
| sched_ext for-next `f7008fb94ee0` | rejected | rejected | verifies, loads, runs, unloads |
| mainline v7.4-rc1 `6628e7d4c6b2` | rejected | rejected | verifies, loads, runs, unloads |
| bpf-next `7101b10cf281`, CI's passing run | verifies, loads, runs, unloads | verifies, loads, runs, unloads | verifies, loads, runs, unloads |

Every rejection is `The sequence of 8193 jumps is too complex.`, in both
`pandemonium_select_cpu` and `pandemonium_enqueue`; every other program in the
object verifies on every kernel. scx's own build lands within six instructions
of CI's: `select_cpu` is rejected after 79,507 to 79,510 processed instructions
(CI's `(baseline)` veristat runs: 79,504 and 79,505) and `enqueue` after 75,711
(CI: 75,710 and 75,714), with `max_states_per_insn` at 129. CI's second
configuration, `9950x`, is rejected the same way. Size is nowhere near the 1M
limit; the pending branch stack (`BPF_COMPLEXITY_LIMIT_JMP_SEQ`, 8192) is what
runs out.

### Why

Both programs inline `find_spill_seat()` through `pick_pcpu_dsq_with_spill()`
at five call sites (four in `select_cpu`, one in `enqueue`). It walks a per-CPU
rank list:

```c
u32 checked = 0;
for (int i = 0; i < PCPU_SPILL_SCAN_MAX; i++) {
        ...                                         /* 20 conditional jumps per pass, about 13 left pending */
        if (++checked >= pcpu_spill_search_budget)  /* .data, set at run time */
                break;
}
```

Both `7101b10cf281` and the failing kernels widen `i`: Scalar evolution
(`kernel/bpf/scev.c`) computes the loop's trip count, sets `i` to `[0, 31]` on
entry and checks later passes against a checkpoint at the loop header with
`RANGE_WITHIN`. What changed is the clamp at each back edge,
`bpf_clamp_scev_regs()`. On `7101b10cf281` it intersected `[0, 31]` with `i`'s
current range, so `i`'s lower bound rose by one per pass and
`i < PCPU_SPILL_SCAN_MAX` still ended every path within 32 passes. On the
failing kernels it resets `i` to `[0, 31]` on every pass, so the bound never
ends a path and termination falls to state pruning.

Pruning never fires here. `checked` is a 32-bit stack spill, which scalar
evolution reads as an unknown value and leaves alone, while the verifier keeps
it as an exact constant that differs on every pass; `RANGE_WITHIN` compares
imprecise scalars too, so no pass falls within an earlier one. Its compare
against `pcpu_spill_search_budget`, a writable global, is undecidable, so both
outcomes stay open. Each pass leaves about 13 branches pending, and the stack
passes 8192 after 628 passes of the spill loop in `select_cpu`.

`find_idle_by_affinity()`'s loop has the same shape (its own `checked` against
the `.data` global `affinity_search_online`) and ends on its bound in these
logs. It moves to `bpf_for` as well, so it cannot be the next one.

### Fix

```diff
 	u32 checked = 0;
-	for (int i = 0; i < MAX_AFFINITY_CANDIDATES; i++) {
+	int i;
+	bpf_for(i, 0, MAX_AFFINITY_CANDIDATES) {
```

```diff
 	u32 checked = 0;
 
-	for (int i = 0; i < PCPU_SPILL_SCAN_MAX; i++) {
+	int i;
+	bpf_for(i, 0, PCPU_SPILL_SCAN_MAX) {
```

`bpf_for` is libbpf's open-coded numeric iterator (`bpf_iter_num`). At each
`next()` the verifier widens every imprecise scalar that changed since the
previous pass, such as `checked`, to unknown before queuing the next pass. It
stops once a pass reaches `next()` within an earlier one (`RANGE_WITHIN`), so
the walk converges in a few passes whatever `checked` holds. Bounds, budgets
and bodies are unchanged. On these kernels the verifier inlines
`bpf_iter_num_next()` and drops `bpf_iter_num_destroy()`, so a pass costs a few
instructions, not a call.

### Verification

Each kernel was built at the commit above with scx's `kernel.config` fragment
and booted under QEMU/KVM. In each guest: `veristat` over the exact BPF object
the scheduler binary embeds; the scheduler attached (`/sys/kernel/sched_ext/state`
enabled, with the expected ops); six load phases (CPU hogs with fsync writers,
an I/O flood, idle wake ping-pong, lock contention, threads churning partial
CPU affinity, then all of them at once) with a health check after each; then a
detach to `disabled` and exit 0.

Verifier work, processed instructions:

| program | kernel | before | with `bpf_for` |
|---|---|---|---|
| `select_cpu` | `7101b10cf281` | 234,811 (peak states 5,730) | 19,847 (peak states 1,525) |
| `enqueue` | `7101b10cf281` | 117,699 | 10,207 |
| `select_cpu` | the three failing kernels | rejected | 19,437 to 19,847 |
| `enqueue` | the three failing kernels | rejected | 9,938 to 10,207 |

Runtime behaviour on `7101b10cf281`, where both builds load, compared over the
first ~53 s of each guest's per-second recording (the CPU-hog and I/O-flood
phases and part of idle ping-pong; the recordings stop there) with montauk's
permutation-tested population comparison: Of 104 scheduling series (CPU use per
core and in total, context switches, migrations, IPC, runnable tasks, wakeups
and per-command CPU), 58 read zero in both runs. Of the 46 that carry data, 1
differs at p < 0.05 (core 7's CPU use, 60.0% and 48.3%, p = 0.025), against
about 2 expected by chance alone. Context switches read 22.6k/s before and
24.3k/s after (p = 0.68), migrations 8,260/s and 8,698/s (p = 0.83) and CPU use
75.8% and 74.0% (p = 0.68). That is one run per build, so it rules out a large
change, not a small one.

### Kernel commit

A bisect over the 24 commits in `7101b10cf281..b73855c31c7b` that touch
`kernel/bpf/` and the verifier headers, each one built and booted as above with
scx's build at `b0efa3f614c0`, names
`0c33d612bdd5` ("bpf: Do not intersect with current register range on clamp").
Its parent, `c463b378eae7`, verifies `pandemonium_select_cpu`; `0c33d612bdd5`
rejects it with the same error, on all 22 boots of it. The commit stops
`bpf_clamp_scev_regs()` from intersecting an induction variable's loop range
with its current range at each back edge, which is the per-pass narrowing that
used to end this loop on its bound. The change is deliberate (it avoids
assigning an empty range), so the fix belongs on the scheduler side.

### Timing

`veristat-dev` is `continue-on-error`, so today this shows red without
blocking. The required `veristat` job (whose kernels include
`linux-rolling-stable` beside 6.13.y, 6.16.y and 6.18.y) and `test-vm`
(`linux-rolling-stable` only) both run on `linux-rolling-stable`; once that
moves to 7.4, both fail on the current scx_pandemonium, so the fix needs to be
in before then. It ships with v5.24.
