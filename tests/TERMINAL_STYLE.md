# PANDEMONIUM test-suite terminal style

The single reference for everything the suite prints to a terminal. One logger,
one shape per output kind. Every bench (`prism`, `prism-scale`/
`pandemonium-tests`, `prism-cachyos`, `prism-fork-thread`, `prism-ipc`,
`prism-power`, `prism-plot`, `prism-strand`, `prism-locality`) renders through it,
so a user sees the same grammar everywhere.

Binding rules (from the project doctrine, restated so this file is self-contained):

- Every line is `[HH:MM:SS] [LEVEL]   message`. No exceptions for "headers" or
  "banners" — a header is just a message.
- No decorative separators anywhere: no `===`, `---`, `***`, `###`, no `== text ==`,
  no box-drawing, no underlines. A blank line is the only separator.
- Numeric/structural data is a table (header row + aligned columns) or routes to
  sublimation / montauk_analyze. Never an ad-hoc dump.
- Run benches BARE: `./pandemonium.py ...`, NEVER `sudo ./pandemonium.py ...` -- not in the
  docs, not in program output, not in any instruction or example shown to the user. This is
  non-negotiable. Each bench acquires root ITSELF: the canonical pattern is a self-elevating
  `os.execvp("sudo", ...)` re-exec in `main()` (and on the --dev path, before a trace-capable
  workload), so sudo prompts AFTER the bare command -- once, when the bench actually needs it.
  A bench that only shells out per command warms the sudo cache (`sudo -v`) and prefixes those
  commands instead. Either way the user types `./pandemonium.py`, never sudo.

## The line format

```
[HH:MM:SS] [LEVEL]   message
```

- Timestamp: `[%H:%M:%S]` then one space.
- Level tag padded so the message column is fixed at the same offset for every
  level: `[INFO]` + 3 spaces, `[WARN]` + 3 spaces, `[ERROR]` + 2 spaces,
  `[DEBUG]` + 2 spaces. Message always begins at column 19.
- Levels: `DEBUG` `INFO` `WARN` `ERROR`. INFO and up print to the terminal; DEBUG
  goes to the `/tmp/<project>` log only unless `-v`. `-q` raises the floor to WARN.

```
[08:51:58] [INFO]   an informational line
[08:51:58] [WARN]   a warning
[08:51:58] [ERROR]  an error
[08:51:58] [DEBUG]  a debug line (file / -v only)
```

## No indentation — every line is flat

Every message begins at the same column (19), regardless of what it belongs to.
There is no nesting depth and no leading spaces in the message field. Grouping is
conveyed by the SECTION line and a single blank line between sections, not by
indent. A line never carries spaces to "sit under" another.

```
[09:01:22] [INFO]   16 cores
[09:01:22] [INFO]   SCHEDULER: EEVDF
[09:01:29] [INFO]   pipe   p50=10us p99=14us
```

## INTRO line

First line of every command. Identity, version, build, mode — one line.

```
[08:51:58] [INFO]   PRISM v5.15.0 [8bbd90349] [sched=eevdf+pandemonium]
```

Form: `<command> <version> [<commit><-dirty?>] <mode-tag?>`. The `[COMMAND]
SELECTED` line and the separate `prism-cachyos v...` banner are removed — one
intro per invocation, not two. A sub-bench spawned under another (child mode,
below) prints no intro.

## WELCOME block

Optional. A short description under the intro for user-facing commands
(`prism`). Continuation lines are flush like every other line; a single
blank line closes it.

```
[08:51:58] [INFO]   Welcome to PRISM: Turn a scheduler problem into one shareable file.
[08:51:58] [INFO]   runs a short profile under your scheduler and EEVDF, traces
[08:51:58] [INFO]   each with montauk, and ranks what misbehaves by name.
[08:51:58] [INFO]   names are hashed, nothing is uploaded, traces stay local.

[08:51:58] [INFO]   this may take five-plus minutes depending on your hardware.
```

## SECTION header

A phase. One grammar for all of them — replaces today's five shapes (`PHASE:`,
`Scheduler:`, `[16 CORES]`, `[cachyos] running`, `WORKLOAD`). Preceded by one
blank line.

Form: `<Name>: <VERB>` — the verb UPPERCASE, a colon between. Names are fixed
labels, cased per name (proper nouns CachyOS and IPC keep their casing; the
descriptive phase names stay lowercase), not the raw key.

```

[09:01:21] [INFO]   CachyOS: TRACE
[09:01:21] [INFO]   fork-thread: TRACE
[09:01:21] [INFO]   IPC: TRACE
[09:01:21] [INFO]   burst-starvation: TRACE
[09:01:21] [INFO]   sojourn-pressure: TRACE
```

A new section starts with a blank line, never a separator rule. Lines that belong
to it follow flush below — grouping is the blank line plus the SECTION text, not
indent.

## Status / summary lines

A standalone status or completion line leads with an UPPERCASE keyword and a
colon — the same grammar as artifacts. Used for phase completions and clean-room
state, not per-step progress.

```
[11:05:17] [INFO]   TRACE COMPLETE: 9 recording(s) under /tmp/pandemonium
[11:03:36] [WARN]   CLEAN-ROOM: NOISY (load 0.2/16cpu, uptime 2.1h). Single-run tails are background-contaminated; a reboot gives trustworthy numbers.
```

## STEP and result lines

Progress inside a section. The action and its result are both flush lines; the
result reads as a continuation by wording, not indent.

```
[08:58:55] [INFO]   tracing stress-ng cpu-cache-mem (comm=stress-ng)
[08:59:11] [INFO]   13.637s: montauk-cachyos-EEVDF-stress-ng-cpu-cache-mem-...
```

## Tables

Numeric results. Header row of plain column names, then aligned data rows: label
column left-justified to width 28, numeric columns right-justified to width 10,
units inline. No separator row. Built via `table_header()` / `table_row()`.

```
SCHEDULER                          MEAN      STDEV   VS EEVDF
EEVDF                            6.626s      0.01s (baseline)
PANDEMONIUM (BPF)                6.245s      0.02s      -5.8%
```

A short table is printed verbatim (see "Reports are absorbed"), not line-by-line
through the logger, so its columns are not prefixed with timestamps.

## Reports are absorbed, not dumped

A full report (a montauk_analyze `--digest`, a prism-scale table block) is the
FILE artifact, not terminal output. The terminal stays the progress log; the
report is written to disk and its path announced as an artifact line. The user
opens and shares the file. A run does NOT echo the whole report to the terminal —
that was the "huge mass at the end" this rule removes.

prism additionally CONSOLIDATES the digest before writing it (B, in the
suite, not montauk):
- It LEADS with a COMPARISON block -- per traced workload, EEVDF vs PANDEMONIUM
  p99 with the ratio (`fork-thread  EEVDF 32301us -> BPF 4276us (7.6x better)`).
  This is the answer the report exists to give; the per-workload blocks are the
  backing detail.
- It drops all-zero dispatch-stall blocks and the `not analyzed (no per-event
  trace)` non-result.
- THERMAL/POWER is trimmed to temp/power/idle; for cachyos (no latency trace) the
  THERMAL/POWER block is dropped entirely, leaving the hot-cpu offender.

```
[09:02:07] [INFO]   REPORT: ~/.cache/pandemonium/prism-report-5.14.0-...txt
[09:02:07] [INFO]   Share this file -- it is small, redacted, and self-contained.
```

A short pre-formatted TABLE (the prism-scale comparison) may still print to the
terminal for an interactive single-bench run, framed by one blank line and with
no per-line timestamp. Large per-workload report bodies do not.

## WARN and ERROR

Same format, different level. Used for conditions, never for normal progress. No
manual `[WARN]` inside the text (the level tag already carries it).

```
[08:58:54] [WARN]   sched_ext active (pandemonium) — stopping it for the profile
[08:52:17] [ERROR]  montauk install failed: could not insert module (busy)
```

## Artifacts (end of run)

Where output landed. Depth 0, grouped at the end, one line each.

```
[09:02:07] [INFO]   REPORT: ~/.cache/pandemonium/prism-scale-20260618-090121.log
[09:02:07] [INFO]   METRICS: ~/.cache/pandemonium/5.14.0-20260618-090121.prom
```

## Interrupt (Ctrl+C)

One line, one wording, one level everywhere. Replaces the four current variants.

```
[09:03:00] [WARN]   interrupted — cleaning up
```

## Child / quiet mode

When one bench spawns another (`prism` → sub-benches), the child runs with
`PANDEMONIUM_CHILD=1`. A child:

- prints no INTRO, no WELCOME, no kernel/vmlinux/binary preamble, no pre-flight
  chatter, no final verbatim report, no dmesg listing;
- emits only its SECTION / STEP / WARN / ERROR lines, flush like everything else;
- still writes its full `.log` / `.prom` to disk.

So a profile run reads as one uniform progress log, and the per-bench detail is on
disk and in the final report, never inline.

```
[08:58:54] [INFO]   cachyos — tracing 3 workloads × 3 schedulers

[08:58:55] [INFO]   TRACING stress-ng cpu-cache-mem (comm=stress-ng)
[08:59:11] [INFO]   13.637s: montauk-cachyos-EEVDF-stress-ng-cpu-cache-mem-...
[09:00:36] [INFO]   fork-thread — tracing the messaging storm × 3 schedulers
[09:00:41] [INFO]   EEVDF storm (montauk, 180s window)
[09:00:43] [INFO]   completed in 2.574s
```

## dmesg

Kernel scheduler events are evidence, not progress. They are written to the
`dmesg-*.log` artifact and, when relevant, summarized in ONE line — never streamed
one-`[INFO]`-per-event into the terminal.

```
[09:02:07] [INFO]   dmesg: 9 messages (~/.cache/pandemonium/dmesg-20260618-090121.log)
```

## montauk as the utility

The logger handles presentation; measurement and analysis route through montauk
and sublimation, not hand-rolled code:

- percentiles / mean / stdev → `sublimation quantile --nearest` / `mean` / `stdev`.
- trace analysis, offenders, cross-CCX, waits/spins → `montauk_analyze`.
- the report is a `montauk_analyze --digest`, consolidated by the suite and
  written to a file, then announced as an artifact line (not echoed).

A bench presents; montauk measures; the logger renders. No bench hand-formats a
number that a table helper, sublimation, or montauk_analyze should produce.

## What this replaces

- Hand-typed indentation (82× 2-space, 8× 4-space, 2× 6-space) → flat, no indent.
- Five phase-header shapes → one SECTION grammar (`Proper-Name: VERB`).
- Mixed-case status/artifact lines → UPPERCASE keyword + colon (`REPORT:`,
  `METRICS:`, `TRACE COMPLETE:`, `CLEAN-ROOM: NOISY`).
- The whole digest dumped to the terminal → absorbed into the file, consolidated
  (all-zero dispatch-stall and "not analyzed" non-results dropped).
- ~40 bare `print()` calls → logger-owned spacing; single blank as the only break.
- Four interrupt variants → one `log.interrupted()`.
- `prism-plot.py`'s raw `print(..., file=sys.stderr)` → the shared logger.
- Per-event dmesg streaming → one summarized artifact line.
