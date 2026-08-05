# Minimal reproducer for the DARK stranding

As requested at the Weekly meeting, here is a minimal reproducer for the DARK pattern. Apologies for not supplying one sooner as I just didn't think to do so. It is `dark-strand.c` in this directory, one file, no BPF and no tracer, reduced from the harness workload that produced the captures reported earlier in this issue.

## What it does

128 issuers, oversubscribed on 12 CPUs, each doing buffered write plus fsync continuously against xfs on virtio-blk. Three properties are load-bearing and are deliberately not knobs:

- **Oversubscription.** Issuers far exceed CPUs. 128 on 12 is what the reported captures ran.
- **No sleeping.** Issuers hammer continuously. An earlier version of this file had each issuer sleep between operations, on the theory that the idle gap was what created the strand. That theory was wrong: A sleeping workload only reproduces block-device backpressure, which every scheduler exhibits alike.
- **No pinning.** Issuers are never bound to a CPU. Where the scheduler places them is the thing under test.

The condition it induces: A per-CPU block-I/O completion kworker becomes runnable on a nohz_full CPU that has just gone tickless-idle, and nothing dispatches it, so the writer waiting on that completion stays in D-state.

## What it does not do

It does not decide whether the bug occurred, and this is the important caveat. The program prints the fsync latency distribution and nothing else. **fsync latency does not discriminate between the two schedulers**: at every issuer count I tried, p99 sat near 3000ms on both arms, because a throttled device queues under either one. Reading the strand requires watching dispatch itself, which CPU was idle, which kthread was runnable and for how long.

**Any tracer that sees `sched_waking`/`sched_wakeup` and `sched_switch` is sufficient**, and no particular tool is required. The pattern is defined entirely in those two events:

- A wakeup targets CPU N.
- Between that wakeup and the woken task's `sched_switch` onto CPU N, CPU N ran only the idle task. That interval is the strand, and it is DARK because the CPU was idle throughout rather than busy with something else.
- If no `sched_switch` for that task arrives before the trace ends, the strand is CENSORED: Still open at trace end, with no upper bound observed.

The censored strands are what to look at: A strand with no observed upper bound.

I read these with montauk, whose kstrand and dispatch-stall reports compute the above directly, but the definition is the thing that matters and it is tool-agnostic. Anyone can derive the same from a `sched:*` trace with whatever tooling they already use.

## What this reproduces, and what it does not

It reproduces the CONDITION. Over 41 reps on the configuration below, 20 per arm with the arms interleaved, every rep produced DARK strands under both schedulers.

**It does not establish a scheduler-specific split, and should not be read as doing so.** At this load stock EEVDF strands too, and its worst censored strand across those reps was 131.4s against 117.0s for scx_pandemonium. Median strand duration did separate, 0.7s under EEVDF against 5.9s under sched_ext, but Cliff's delta over the pooled runs is +0.285, a small effect, and the tails overlap completely. I am reporting that rather than the stronger separation the first half of those reps showed, because it did not survive doubling the sample.

This is consistent with what I reported on 2026-07-24 in this issue: The entry condition occurs under stock EEVDF as well, in every control rep measured then. What that earlier comment established, and what this reproducer does not attempt to re-establish, is the bounded-against-unbounded difference. That rests on the 215-run census and the four preserved freeze captures, not on this file.

So: This is an inducer, offered because it makes the condition cheap to observe on your own hardware. Treat any single run's numbers as one draw from a wide distribution.

## Build and run

```
cc -O2 -o dark-strand dark-strand.c
./dark-strand <seconds> <issuers> <scratch_dir>
```

Requires `nohz_full=` on the cmdline and a scratch dir on a real block device. It refuses to run on tmpfs or ramfs, where fsync is a no-op and there is no completion kworker to strand.

The configuration below: qemu guest, 12 vCPU single socket, `nohz_full=1-11`, xfs on virtio-blk throttled to 50 IOPS, kernel 7.1.3-2, 30s per rep, 128 issuers. Arms interleaved within one campaign rather than run as blocks.

## What a run looks like

Censored strand duration, worst per rep, 20 reps per arm:

| | EEVDF | scx_pandemonium |
|---|---|---|
| median | 679ms | 5888ms |
| p75 | 5180ms | 14351ms |
| max | 131383ms | 116951ms |

Both arms reach the two-minute range. The medians differ by roughly 9x, but Cliff's delta is +0.285 and the distributions overlap across their whole span, so a handful of reps per arm can easily produce either ordering. Run enough reps that the tail is visible rather than trusting a single pair.

## Limits, stated up front

- **This does not separate the schedulers, and is not offered as doing so.** See above. The scheduler-specific claim in this issue rests on the census and the freeze captures, not on this file.
- **The tails overlap completely.** EEVDF's worst rep exceeded sched_ext's worst. Three or four reps per arm is not enough to see the shape.
- Reps that failed for harness reasons are excluded rather than counted, and the exclusions are not random: One rep hit a boot timeout with no I/O completions recorded, and one never started its tracer. Both are dropped, and the boot-timeout rep's apparent 117-second strand is a boot artifact that appears in no figure here.
- The fsync numbers the program prints are not the finding and should not be read as one. They are included so the run is self-describing.
