# Understanding PANDEMONIUM

> "What?"
> — Richard M. Nixon

This is the companion to the README. The README covers *what* PANDEMONIUM does and how fast it does it; this one covers *what the ideas are*, *where they came from*, and *how PANDEMONIUM puts them to work* — with the math derived as it comes up, not assumed.

## Start here: what is a scheduler, and what is PANDEMONIUM doing?

Your computer has a handful of CPU cores and, at any moment, far more programs that want to run than there are cores to run them. Something has to decide, thousands of times a second, *which* task runs on *which* core and for *how long*. That something is the scheduler. It is one of the oldest, most-tuned pieces of any operating system, and almost every scheduler in history has treated the problem as a queue: who's been waiting longest, who has the most priority, who goes next.

PANDEMONIUM treats it as a **graph problem** instead.

The word "graph" is worth pinning down, because here it does not mean what it often does. The graph is **not** physical space — there's no X, Y, or Z, no 3-D coordinates. The graph is **your machine's own layout**: the cores, and the caches they share. Two cores that share a fast cache are "close together" on the graph. Two cores in different cache clusters are "far apart." Distance on this graph means one thing: *how expensive it is to move a task's data from one core to the other.* Close = cheap. Far = a cold cache and wasted time.

So when the README says each task is classified as a **"spacetime object on the graph,"** here is the whole idea:

- **Space** — *where* on that cache-map a task belongs. PANDEMONIUM computes this with a tool from physics (effective resistance, below) and calls it *resistance affinity*.
- **Time** — *how long* a task has been waiting in line. PANDEMONIUM measures this with a tool from networking (CoDel sojourn, below).

Every task gets a place in space and a reading in time, and PANDEMONIUM uses both to put it exactly where it should run, exactly when it should run. That's the entire thesis. The rest of this document is the seven ideas that make it work, and the people and decades they came from.

## The ideas, and where they came from

### 1. Graph theory → where a task belongs (resistance affinity)

**The history.** Graph theory — the study of dots connected by lines — began in 1736, when Euler asked whether you could walk the seven bridges of Königsberg crossing each exactly once. The clever twist PANDEMONIUM uses came a century later, when physicists realized you could treat a graph as an *electrical circuit*: make every connection a wire with some resistance, inject current at one dot, and measure the voltage at another. The result, called *effective resistance*, has a beautiful property — it doesn't just measure the single shortest wire between two points, it accounts for **every path between them at once**. Two points connected by many routes are "low resistance," i.e. truly close; two points joined by a single thin wire are "high resistance," i.e. truly far. This was formalized as *resistance distance* by Klein and Randić in 1993, and a famous 1989 result proved it equals the expected round-trip time of a random walk between the two points.

**How PANDEMONIUM uses it.** The dots are your CPU cores; the wires are the caches they share (a shared fast L2 cache is a thick low-resistance wire; a link across the chip is a thin one). PANDEMONIUM builds this circuit once when it starts, solves it, and gets the *true* cost of moving a task between any two cores — counting every path through the cache hierarchy, not a crude "same chip / different chip" guess. That cost is the task's **space** coordinate. When a task wakes up, PANDEMONIUM places it on the lowest-resistance core available, which keeps its data in warm cache. To our knowledge this is the first time effective resistance has been used to place tasks in any operating-system scheduler.

### 2. CoDel → how long is too long to wait (sojourn)

**The history.** In the late 2000s the internet had a quiet disease called *bufferbloat*: routers and devices had so much memory that they'd buffer enormous backlogs of packets rather than drop any, and latency ballooned — your video call stuttered not because the network was full but because your data was sitting in a giant queue. Two engineers, Kathleen Nichols and Van Jacobson (Jacobson is one of the people who kept the early internet from collapsing under congestion), proposed a fix in 2012 called **CoDel** — Controlled Delay. Its insight: don't measure *how many* packets are in the queue (a misleading number); measure *how long each packet actually sat there* — its **sojourn time**. If packets are spending too long waiting, the queue is in trouble, regardless of its length. CoDel is now standard in the Linux network stack (RFC 8289).

**How PANDEMONIUM uses it.** A run-queue of waiting tasks is a queue as well. PANDEMONIUM stamps each task with the moment it joins the queue and, when it finally runs, measures the sojourn — *now minus when it arrived*. That wait is the task's **time** coordinate, and it's the literal CoDel metric applied to CPU scheduling rather than network packets. When sojourn climbs past a target, PANDEMONIUM knows tasks are starving and steps in — the same way CoDel knows a network queue has gone bad.

### 3. Maximum flow → routing work across the machine (Φ)

**The history.** In 1955, two researchers at the RAND Corporation (Harris and Ross) analyzed the capacity of the Soviet rail network — how much traffic it could carry, and where its narrowest bottleneck lay. That question birthed the **maximum-flow / minimum-cut** problem, formalized the next year by Ford and Fulkerson. Decades later the story came full circle: the fastest known algorithms for maximum flow (Christiano and others, 2011; a celebrated 2022 result) solve it by — of all things — treating the network as an *electrical circuit*, the same Laplacian math as resistance distance in idea #1.

**How PANDEMONIUM uses it.** Moving backlogged work from a busy core to an idle one is a flow problem: push work along the cheapest routes through the machine. But a move isn't free — dragging a task to a distant core means a cold cache. So PANDEMONIUM prices every potential move with a quantity it calls **Φ (phi)**: the graph resistance of the move (from idea #1) weighed against the queueing relief it would buy (from idea #2). A task only crosses a cache boundary when the backlog it relieves is worth the cache cost it pays. Cheap nearby moves happen freely; an expensive far-away move only happens when the relief outweighs the cache cost. Resistance (idea 1) and flow (idea 3) are the same underlying mathematics, and PANDEMONIUM runs it in the opposite direction from those algorithms — pricing flow *by* resistance.

### 4. Control theory → staying calm, reacting fast, resting when idle (the oscillator)

**The history.** Control theory is the science of feedback — of systems that sense their own output and correct themselves. Its first great machine was James Watt's 1788 centrifugal governor: two spinning weights that eased a steam engine's throttle as it sped up and opened it as it slowed, the first automatic feedback loop. Two centuries of mathematics (Maxwell, Lyapunov, and the PID controllers in everything from thermostats to rockets) made it rigorous. A *damped harmonic oscillator* — a weight on a spring with friction — is the textbook example: the spring pulls it toward rest, the damping keeps it from bouncing forever. A more recent idea, *minimum-attention control*, adds a frugal twist: a good controller shouldn't burn effort every instant — it should act only when the situation actually calls for it.

**How PANDEMONIUM uses it.** The "too long to wait" target from idea #2 isn't a fixed number — PANDEMONIUM lets it ride on a damped harmonic oscillator that settles toward an equilibrium derived from the machine's own shape. When tasks start starving, the target tightens; when things calm down, it eases back, smoothly, without ringing. And following the minimum-attention principle, when the system goes quiet the whole controller **parks** — it stops recomputing, pins the target at its resting value, and spends almost nothing. The instant a real stall appears it snaps fully awake in the same beat. The scheduler tunes itself, and when there's nothing to tune, it rests.

### 5. Chaos theory → reading the texture of a workload

**The history.** Chaos theory grew out of Edward Lorenz's 1960s discovery that tiny changes in weather models exploded into wildly different forecasts — the "butterfly effect." Out of that field came a set of tools for reading the *character* of any stream of numbers: is it steady and periodic, is it chaotic, or is it pure random noise? Three of them — *permutation entropy* (Bandt & Pompe, 2002), *visibility graphs* (Luque and others, 2009), and *recurrence analysis* (Marwan and others, 2007) — can tell those apart from nothing but the shape of the data.

**How PANDEMONIUM uses it.** A workload is a stream of numbers too — how busy the machine is, moment to moment. PANDEMONIUM runs the three measures over a short window every second, and each one asks the same question from a different angle:

- **Permutation entropy** looks only at the *order* of consecutive samples — the up-down-up shapes — and throws the actual magnitudes away. When every little pattern is equally likely, the stream is disordered; when one pattern dominates, it's periodic. It catches structure that survives any rescaling.
- **The visibility graph** turns the stream into a skyline and asks which samples can "see" each other over the tops of the shorter ones in between. The connectivity of that skyline has a known fingerprint: pure random noise drives the average number of sightlines toward one value, a clean periodic signal toward another. It reads structure in the *geometry* of the peaks, where the order-only measure is blind.
- **Recurrence analysis** asks how often the system returns to a state close to one it has already visited, and whether those returns line up into runs. A genuinely steady signal revisits the same neighborhoods in long diagonal streaks; noise returns by accident and scatters.

The reason there are three and not one is the whole point: each is fooled by something the others catch. A stream can look random to the order measure yet betray a pattern in the visibility geometry, or pass both and still scatter under recurrence. Reading all three lets PANDEMONIUM recognize what *kind* of moment the system is in — idle, a mix, or flat-out saturated — by agreement across independent tests, and tell a genuinely steady workload apart from one that just looks calm for a tick. That reading is what lets the scheduler shift strategy to match reality instead of chasing a single noisy number.

### 6. Learning theory → a panel of experts that tunes the scheduler (multiplicative weights)

**The history.** Suppose you have to make a repeated decision, you've got several advisors each with a different bias, and no idea in advance which is right. A remarkably durable answer: trust each advisor in proportion to how well it has done lately, and after every round nudge that trust up for the ones who were right and down for the ones who were wrong — *multiplicatively*, by a fixed factor. This was crystallized as the **weighted-majority algorithm** by Littlestone and Warmuth around 1989, and later recognized (Arora, Hazan, and Kale) as one meta-algorithm that keeps reappearing under different names — in machine learning as boosting, in game theory as a route to equilibrium, in optimization as a solver. It learns with provable guarantees while assuming almost nothing about the world it's learning about.

**How PANDEMONIUM uses it.** The scheduler's settings — how long a slice is, how eager preemption is, how wide a batch task may run — have no single right value; the right value depends on the moment. So PANDEMONIUM runs a small panel of fixed strategies, each an "expert" standing for one bias: one tuned for latency, a do-nothing anchor that leaves the knobs at the regime's baseline, one for throughput, one for I/O-heavy work, one for fork storms, one for saturation. Every second the layer scores what actually went wrong — a latency spike past the budget, a starvation rescue that had to fire, an I/O surge — and shifts trust away from the experts whose bias would have caused it and toward the ones who'd have done better. The knobs it actually applies are the trust-weighted blend of the panel, not any single expert. It keeps a *separate* panel for each workload regime, so what it learned about saturation isn't erased when the machine goes idle. No model of the workload is assumed anywhere; the panel simply converges on what is working right now.

### 7. BPF/eBPF and sched_ext → where all of this runs

**The history.** In 1992, McCanne and Jacobson (the same Jacobson from idea #2) built the **Berkeley Packet Filter** — a tiny, safe mini-language that let `tcpdump` filter network packets inside the kernel without crashing it. Around 2014 it was generalized into **eBPF**, a sandboxed virtual machine that can run small verified programs safely inside the Linux kernel. And in 2024, **sched_ext** opened the kernel's most sacred ground — the CPU scheduler itself — to eBPF, so that for the first time you can write a real, production scheduler that the kernel loads and runs safely, without recompiling the kernel.

**How PANDEMONIUM uses it.** PANDEMONIUM *is* a sched_ext scheduler. Its moment-to-moment decisions — placement, preemption, detecting stalls — run as eBPF code inside the kernel, where they're fast enough to keep up with thousands of scheduling decisions a second, with no slow trips out to ordinary programs. That's the foundation the other six ideas stand on.

## Two layers that talk to each other

PANDEMONIUM runs in two layers, and the split is the key to making heavy math affordable:

- **The BPF layer** lives in the kernel and makes the microsecond-by-microsecond decisions: where each task goes, who gets preempted, when a queue has stalled. It has to be fast, so it does the placing.
- **The Adaptive layer** lives in an ordinary program and wakes once a second. It reads what the BPF layer measured, figures out the workload regime (idle/mixed/saturated, idea #5), and re-tunes the kernel layer's knobs through the panel of experts (idea #6). Fast decisions below, slow learning above.

There's one more piece that ties it together over time. As a program runs, PANDEMONIUM watches its behavior and classifies what it *is* — a snappy interactive app, a background batch job, a latency-critical thread — and writes that verdict to a small on-disk **process database**. The next time that program starts, PANDEMONIUM doesn't have to rediscover its character from a cold start: it reads the verdict and places the program correctly from its first wakeup. The machine learns a workload once and remembers it across reboots.

## Decisions: the road not taken

A system is also the sum of the tempting ideas it tried and threw out, and two of those are worth knowing, because they explain why PANDEMONIUM stays calm under load rather than twitchy.

**No change-point accumulator.** A textbook way to detect "things just got worse" is to accumulate evidence over time until it crosses a threshold — the classic CUSUM control chart, out of 1950s industrial statistics. PANDEMONIUM built one and removed it. On a scheduler, an accumulator chatters on the ordinary noise floor: fixed-point rounding, clock jitter, a single late wakeup all nudge it, so tuned tight it fires on nothing, and tuned loose it sleeps through a real burst. The replacement is the hysteresis already living in the oscillator (idea #4) — a deliberate gap between the level that says "tighten" and the level that says "relax," wider than the jitter — so the system commits to a state and holds it until the world genuinely changes, instead of flinching at every delta.

**Raw windows, not smoothed averages.** The reflexive way to tame a noisy measurement is an exponential moving average. PANDEMONIUM refuses it for the signals the chaos measures read, because smoothing blurs the very thing they exist to see — the *texture* of the last second. It recomputes those statistics over a fresh, unsmoothed window every tick. A smoothed signal looks calm whether the workload is truly steady or merely quiet for an instant; the raw window can tell the two apart, which is the entire reason the chaos layer is there.

## Putting it back together

So, the whole machine in one picture. A task wakes up. PANDEMONIUM knows two things about it: a place in **space** (which cache neighborhood it belongs to — resistance affinity, idea #1) and a reading in **time** (how long it's been waiting — sojourn, idea #2). The kernel layer (idea #7) places it on the right core. A self-tuning controller (idea #4) keeps the timing honest and rests when the system is idle. When work piles up unevenly, a priced flow (idea #3) routes the overflow along the cheapest paths. A learning layer (idea #6) reading the workload's texture (idea #5) re-tunes the whole thing once a second, and a database remembers what it learned about each program.

Seven fields of mathematics and decades of other people's work — graph theory, queue control, maximum flow, feedback control, nonlinear dynamics, online learning, and in-kernel programming — condensed into one decision, made thousands of times a second: *where does this task go, right now?*

## Older than it looks

None of the pieces are new. What's new is the braid. Laid out on a timeline, PANDEMONIUM is most of three centuries of separate work that never met until now:

| Year | The piece | The people |
|---|---|---|
| 1736 | Graph theory — the bridges of Königsberg | Euler |
| 1788 | Feedback control — the centrifugal governor | Watt |
| 1955–56 | Maximum flow / minimum cut | Harris & Ross; Ford & Fulkerson |
| 1989 | Random-walk commute time = effective resistance | Chandra and others |
| 1989 | Weighted-majority learning | Littlestone & Warmuth |
| 1992 | Berkeley Packet Filter (the ancestor of eBPF) | McCanne & Jacobson |
| 1993 | Resistance distance, named | Klein & Randić |
| 2002 | Permutation entropy | Bandt & Pompe |
| 2009 | Visibility graphs | Luque and others |
| 2012 | CoDel — controlled delay | Nichols & Jacobson |
| 2022 | Maximum flow in almost-linear time | Chen, Kyng, and others |
| 2024 | sched_ext — schedulers as safe kernel programs | the Linux community |

Each row solved its own problem and stopped there. PANDEMONIUM is what happens when one decision — *where does this task go, right now?* — turns out to need all of them at once.

That's PANDEMONIUM: a lot of good ideas in one place.

## Want to go deeper?

- The **README** has the full mechanism-by-mechanism breakdown and the research citations behind every concept above.
- The **source is open** — `src/topology.rs` builds the resistance graph, `src/bpf/main.bpf.c` is the in-kernel scheduler, `src/chaos.rs` holds the nonlinear-dynamics measures, and `src/tuning.rs` is the learning layer.
- Questions are welcome — open an issue or read the source.
