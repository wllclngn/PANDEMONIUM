#!/usr/bin/env python3
"""
system-forensics.py -- find a past hang and explain it.

This is a backward-looking hang-finder. It scrapes the system to DISCOVER every
log source (no paths supplied), searches them to FIND the hang event, SCRAPES the
window around it, ANALYZES that window, and REPORTS what the hang was. Run with no
arguments and it locates the most recent hang on the box and tells you the cause.

It classifies into the failure taxonomy this night produced:

  GPU-DISPLAY   amdgpu DMUB/FAMS2/DRR firmware hang, NVIDIA Xid, or a compositor
                blocked/crashed in its DRM present path
  IO-WEDGE      a per-CPU kworker that completes writeback or mm-drains is not
                running -- D-state piles up; khugepaged stuck in lru_add_drain is
                the mm-workqueue-livelock variant
  SCHED-WEDGE   sched_ext ejected/faulted, a lockup/RCU stall, or a runnable task
                left undispatched
  USER-CRASH    a userspace process segfaulted; the coredump names the frame

montauk is the only tracer; sublimation does the stream and numeric analysis. The
silence-gap heuristic catches the wedge that kills journald before it can flush.

Modes:
  (no args)         find + analyze the most recent hang
  --list            discover sources + list every candidate event, no deep dive
  --boot N          restrict to journald boot N (0=current, -1=previous, ...)
  --at "HH:MM"      analyze the event nearest a time
  --live            snapshot the CURRENT system state instead of hunting the past
  --wedge           live: trigger sysrq dumps first (run over SSH while frozen)
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

REPORT_LINES: list[str] = []


def log(level: str, msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] [{level:<5}]   {msg}"
    print(line, flush=True)
    REPORT_LINES.append(line)


def info(m): log("INFO", m)
def warn(m): log("WARN", m)
def error(m): log("ERROR", m)


def section(title: str) -> None:
    REPORT_LINES.append("")
    info(title)


def run(cmd: list[str], stdin: str | None = None, timeout: float = 30.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fp:
            return fp.read()
    except OSError:
        return ""


class Tools:
    def __init__(self):
        self.montauk = shutil.which("montauk")
        self.montauk_analyze = shutil.which("montauk_analyze")
        self.sublimation = shutil.which("sublimation")
        self.coredumpctl = shutil.which("coredumpctl")
        self.journalctl = shutil.which("journalctl")
        self.is_root = (os.geteuid() == 0)


# sublimation does stream filtering and numeric shape, per the standing rule.
def sub_grep(t: Tools, text: str, pattern: str, flags: list[str] | None = None) -> list[str]:
    if not text:
        return []
    if t.sublimation:
        rc, out = run([t.sublimation, "grep", *(flags or []), pattern], stdin=text)
        if rc in (0, 1):
            return [ln for ln in out.splitlines() if ln]
    rx = re.compile(pattern, re.IGNORECASE if (flags and "-i" in flags) else 0)
    return [ln for ln in text.splitlines() if rx.search(ln)]


def sub_classify(t: Tools, numbers: list[float]) -> str:
    if not numbers:
        return "(empty)"
    if t.sublimation:
        rc, out = run([t.sublimation, "classify"], stdin="\n".join(map(str, numbers)) + "\n")
        if rc == 0 and out.strip():
            return out.strip()
    return f"n={len(numbers)} min={min(numbers):.1f} max={max(numbers):.1f}"


# DISCOVER ------------------------------------------------------------------
class Source:
    def __init__(self, name: str, kind: str, recency: int):
        self.name = name        # human label
        self.kind = kind        # journal-boot | dmesg | varlog | captured | pstore | montauk
        self.recency = recency  # higher = more recent; current boot beats prior
        self._text: str | None = None
        self._fetch = None      # callable -> str

    def text(self) -> str:
        if self._text is None:
            self._text = self._fetch() if self._fetch else ""
        return self._text


def discover_sources(t: Tools) -> list[Source]:
    section("DISCOVER  (scraping the system for log sources)")
    sources: list[Source] = []

    # journald boots -- enumerate so prior boots (where a wedge lives) are reachable
    if t.journalctl:
        rc, out = run([t.journalctl, "--list-boots", "--no-pager"], timeout=15)
        if rc == 0:
            boots = []
            for ln in out.splitlines():
                m = re.match(r"\s*(-?\d+)\s", ln)
                if m:
                    boots.append(int(m.group(1)))
            for b in sorted(boots, reverse=True)[:5]:  # current + 4 prior
                s = Source(f"journal boot {b}", "journal-boot", 1000 + b)
                s._fetch = (lambda bb=b: run([t.journalctl, "-k", "-b", str(bb), "--no-pager"], timeout=25)[1])
                sources.append(s)

    # current kernel ring buffer
    s = Source("dmesg (live ring buffer)", "dmesg", 999)
    s._fetch = lambda: run(["dmesg", "-T"])[1]
    sources.append(s)

    # /var/log text logs
    for pat in ("kern.log", "messages", "syslog", "Xorg.0.log", "Xorg.0.log.old"):
        for p in glob.glob(f"/var/log/{pat}"):
            s = Source(p, "varlog", 500)
            s._fetch = (lambda pp=p: read(pp))
            sources.append(s)

    # pstore -- a crash dmesg saved across a reboot, if ramoops/ERST is set up
    for p in glob.glob("/sys/fs/pstore/*"):
        s = Source(p, "pstore", 800)
        s._fetch = (lambda pp=p: read(pp))
        sources.append(s)

    # artifacts the operator already captured
    for pat in (os.path.expanduser("~/hang-*.log"),
                "/tmp/pandemonium/*.stdout", "/tmp/pandemonium/*.txt",
                "/tmp/*hang*.log", "/tmp/*stack*.txt"):
        for p in glob.glob(pat):
            if os.path.basename(p).startswith("forensics-"):
                continue  # our own report output -- not a log, would self-reference
            s = Source(p, "captured", 700)
            s._fetch = (lambda pp=p: read(pp))
            sources.append(s)

    for s in sources:
        info(f"  found: {s.name}")
    if not sources:
        warn("no log sources discovered")
    return sources


# FIND ----------------------------------------------------------------------
# pattern -> (kind, severity). Higher severity wins when ranking events.
SIGNATURES = [
    (r"blocked for more than \d+ seconds|hung_task",              "hung_task",   9),
    (r"watchdog: BUG: soft lockup|hard LOCKUP",                   "lockup",      9),
    (r"rcu_sched.*stall|rcu_preempt.*stall|RCU.*CPU.*stall",      "rcu_stall",   8),
    (r"kernel BUG|general protection fault|Oops:|Kernel panic",   "oops",        9),
    (r"Out of memory|invoked oom-killer|oom-kill",               "oom",         7),
    (r"sched_ext.*(ops_error|error exit|EXIT)|scx.*ops_error",    "scx_eject",   8),
    (r"segfault at|SIGSEGV|traps:.*SIGSEGV",                      "segfault",    6),
    (r"NVRM:?.*Xid|Xid \(PCI",                                    "nvidia_xid",  7),
    (r"ring .* timeout|amdgpu.*reset|\[drm\].*reset",            "gpu_reset",   8),
]


class Event:
    def __init__(self, kind: str, severity: int, source: Source, line: str):
        self.kind = kind
        self.severity = severity
        self.source = source
        self.line = line.strip()
        self.ts = leading_timestamp(line)
        self.order = 0  # line index within the source, for windowing


def leading_timestamp(line: str) -> str:
    m = re.match(r"\[(.*?)\]", line)                          # dmesg -T or [secs]
    if m:
        return m.group(1)
    m = re.match(r"([A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d)", line)  # journald
    if m:
        return m.group(1)
    return "?"


def find_events(t: Tools, sources: list[Source]) -> list[Event]:
    section("FIND  (searching every source for hang signatures)")
    events: list[Event] = []
    for s in sources:
        text = s.text()
        if not text:
            continue
        # strip firewall noise so the silence-gap and signatures are not buried
        clean_lines = sub_grep(t, text, "UFW BLOCK", ["-v"]) or text.splitlines()
        clean = "\n".join(clean_lines)
        for pat, kind, sev in SIGNATURES:
            for ln in sub_grep(t, clean, pat, ["-i"]):
                events.append(Event(kind, sev, s, ln))
        # silence gap: the wedge that kills journald before it can flush. If the
        # log ends shortly after "sched_ext enabled" with no further real kernel
        # line, the box went dark and could not write the hung_task it suffered.
        enabled = [i for i, ln in enumerate(clean_lines)
                   if re.search(r"BPF scheduler .* enabled|sched_ext.* enabled", ln, re.I)]
        if enabled:
            tail = clean_lines[enabled[-1] + 1:]
            real = [ln for ln in tail if ln.strip() and "kernel:" in ln]
            if len(real) <= 2 and s.kind in ("journal-boot", "captured", "dmesg"):
                events.append(Event("silence_gap", 6, s, clean_lines[enabled[-1]]))
    info(f"candidate events: {len(events)}")
    # coredumps are their own source of truth for USER-CRASH
    if t.coredumpctl:
        rc, out = run([t.coredumpctl, "list", "--no-pager", "--reverse"], timeout=15)
        if rc == 0:
            for ln in out.splitlines()[1:8]:
                if re.search(r"SIGSEGV|SIGABRT|SIGBUS|SIGILL", ln):
                    parts = ln.split()
                    pid = parts[4] if len(parts) > 4 and parts[4].isdigit() else None
                    exe = os.path.basename(parts[-2]) if len(parts) >= 2 else "?"
                    ts = " ".join(parts[0:3])
                    src = Source(f"coredump {exe} @ {ts}", "coredump", 850)
                    if pid:
                        src._fetch = (lambda pp=pid: run([t.coredumpctl, "info", pp, "--no-pager"], timeout=15)[1])
                    ev = Event("coredump", 7, src, ln)
                    ev.ts = ts
                    events.append(ev)
    return events


def pick_event(events: list[Event], args) -> Event | None:
    if not events:
        return None
    pool = events
    if args.boot is not None:
        pool = [e for e in events if e.source.name == f"journal boot {args.boot}"] or events
    if args.at:
        pool = sorted(pool, key=lambda e: (abs_time_dist(e.ts, args.at), -e.severity))
        return pool[0]
    # default: most severe, then most recent source
    return sorted(pool, key=lambda e: (e.severity, e.source.recency), reverse=True)[0]


def abs_time_dist(ts: str, target: str) -> int:
    m = re.search(r"(\d\d):(\d\d)(?::(\d\d))?", ts)
    n = re.search(r"(\d\d):(\d\d)(?::(\d\d))?", target)
    if not (m and n):
        return 10 ** 9
    a = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)
    b = int(n[1]) * 3600 + int(n[2]) * 60 + int(n[3] or 0)
    return abs(a - b)


# SCRAPE --------------------------------------------------------------------
def scrape_window(t: Tools, event: Event) -> str:
    text = event.source.text()
    if not text:
        return event.line
    lines = text.splitlines()
    idx = next((i for i, ln in enumerate(lines) if event.line[:80] in ln), None)
    if idx is None:
        # coredump info / pstore: the line is not literally inside the fetched
        # text, but the text IS the evidence (the backtrace) -- return its head.
        head = [ln for ln in lines if ln.strip()][:32]
        return "\n".join(head) if head else event.line
    # a hung_task / oops has a Call Trace block below it; grab generously, then
    # stop at </TASK> or the next unrelated timestamped line cluster.
    start = max(0, idx - 2)
    end = idx + 1
    block = []
    for j in range(idx, min(len(lines), idx + 60)):
        block.append(lines[j])
        if "</TASK>" in lines[j] or re.search(r"RSP:|R13:|R15:", lines[j]):
            end = j + 1
            break
        end = j + 1
    return "\n".join(lines[start:end])


# ANALYZE -------------------------------------------------------------------
def classify(window: str) -> tuple[str, str]:
    w = window.lower()
    if re.search(r"dmub|fams2|dc_dmub|\bdrr\b", w):
        return ("GPU-DISPLAY", "amdgpu DMUB / Freesync DRR firmware hang -- a kworker is stuck in the display "
                               "microcontroller mailbox holding the DRM lock. Disable VRR or amdgpu.dcdebugmask, "
                               "update kernel+firmware.")
    if re.search(r"testpresentation|drmoutput|setuplayers|compositor.*composite", w):
        return ("USER-CRASH", "compositor blocked/crashed in its DRM frame-presentation path -- display stack, "
                              "not the run-queue. Update the compositor/driver, file the coredump.")
    if re.search(r"lru_add_drain|kcompactd|khugepaged.*flush|page.?reclaim", w):
        return ("IO-WEDGE", "per-CPU mm-workqueue drain not completing (lru_add_drain_all -> flush_work) -- the "
                            "khugepaged-hang livelock. DISCRIMINATOR: rerun the same load under EEVDF; survives = "
                            "scheduler, also hangs = workload thrashing the kernel mm path.")
    if re.search(r"fdatasync|folio_wait|btrfs.*wait|writeback|ordered_range", w):
        return ("IO-WEDGE", "fsync/writeback waiter blocked behind a stranded per-CPU I/O-completion kworker. The "
                           "nr_cpus_allowed==1 direct-dispatch fix is the remedy; confirm with montauk kstrand.")
    if re.search(r"ops_error|sched_ext.*(error|exit)|scx.*exit", w):
        return ("SCHED-WEDGE", "sched_ext scheduler reported an error/exit -- it ejected or faulted. Analyze the "
                              "montauk trace and pull a live backtrace with --wedge.")
    if re.search(r"nvrm.*xid|xid \(pci", w):
        return ("GPU-DISPLAY", "NVIDIA Xid GPU fault -- update the driver, check explicit-sync.")
    if re.search(r"soft lockup|hard lockup|rcu.*stall", w):
        return ("SCHED-WEDGE", "lockup/stall detector fired -- a CPU spun without forward progress.")
    if re.search(r"segfault|sigsegv", w):
        return ("USER-CRASH", "a userspace process segfaulted -- the coredump backtrace names where.")
    return ("UNKNOWN", "signature found but no specific class matched -- read the scraped window above.")


def analyze_event(t: Tools, event: Event, window: str) -> None:
    section("ANALYZE")
    info(f"event: {event.kind}  severity {event.severity}  at {event.ts}  in [{event.source.name}]")
    info("scraped window:")
    for ln in window.splitlines()[:40]:
        info(f"  {ln.strip()[:150]}")

    # lock-ownership edges the kernel prints, plus the scx-frame liveness check
    edges = sub_grep(t, window, r"is blocked on a (mutex|rwsem|lock) likely owned by")
    for e in edges:
        info(f"  EDGE: {e.strip()[:150]}")
    scx = sub_grep(t, window, r"find_user_dsq|scx_|consume_dispatch")
    if scx:
        stale = all(s.strip().startswith("?") or "? " in s for s in scx)
        info(f"  scheduler frames present ({len(scx)}); "
             + ("all '?' stale-unwind -- scheduler not on the live path" if stale
                else "on a LIVE path -- scheduler implicated"))

    # if a montauk trace covers this window, let the instrument speak
    traces = glob.glob("/tmp/pandemonium/*.events") + glob.glob("/tmp/pandemonium/**/*.prom", recursive=True)
    if traces and t.montauk_analyze:
        newest = max(traces, key=os.path.getmtime)
        info(f"montauk artifact found: {newest} -- analyzing")
        rc, out = run([t.montauk_analyze, newest, "--report", "kstrand,dispatch-stall,endstate"], timeout=45)
        if rc == 0:
            for ln in out.splitlines():
                if ln.startswith("REPORT") or "VERDICT" in ln or "strand" in ln.lower():
                    info(f"  {ln.strip()[:150]}")

    klass, guidance = classify(window)
    section("VERDICT")
    info(f"CAUSE: {klass}")
    info(f"WHAT:  {event.kind} at {event.ts} ({event.source.name})")
    info(f"NEXT:  {guidance}")


# LIVE SNAPSHOT (the old behaviour, demoted to a mode) ----------------------
def live_snapshot(t: Tools) -> None:
    section("LIVE SNAPSHOT  (current system state)")
    base = "/sys/kernel/sched_ext"
    if os.path.isdir(base):
        info(f"scx state: {read(os.path.join(base, 'state')).strip() or '?'}   "
             f"ops: {read(os.path.join(base, 'root', 'ops')).strip() or 'none'}")
    la = read("/proc/loadavg").split()
    ncpu = os.cpu_count() or 1
    if len(la) >= 4:
        info(f"load average: {la[0]} {la[1]} {la[2]}   runnable/total: {la[3]}")
        try:
            if float(la[0]) > 4 * ncpu:
                warn(f"load {float(la[0]):.0f} = {float(la[0]) / ncpu:.0f}x cores -- run-queue pileup")
        except ValueError:
            pass
    for res in ("io", "cpu", "memory"):
        txt = read(f"/proc/pressure/{res}")
        m = re.search(r"some avg10=([\d.]+)", txt)
        if m:
            info(f"PSI {res:<6} some avg10={m.group(1)}%")
    # top CPU consumers (kworker-mm churn shows here)
    def snap():
        out = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            st = read(f"/proc/{pid}/stat")
            if not st:
                continue
            try:
                comm = st[st.index("(") + 1:st.rindex(")")]
                fld = st[st.rindex(")") + 2:].split()
                out[pid] = (comm, int(fld[11]) + int(fld[12]))
            except (ValueError, IndexError):
                continue
        return out
    a = snap(); time.sleep(0.7); b = snap()
    hz = os.sysconf("SC_CLK_TCK") or 100
    rows = sorted(((100.0 * (b[p][1] - a[p][1]) / (0.7 * hz), p, b[p][0])
                   for p in b if p in a and b[p][1] > a[p][1]), reverse=True)
    info("top CPU consumers:")
    mm = 0
    for pct, p, comm in rows[:12]:
        info(f"  {pct:6.1f}%  pid {p:>7}  {comm}")
        if re.search(r"kworker.*-mm|kworker.*mm_p", comm):
            mm += 1
    if mm >= 4:
        warn(f"{mm} mm-workqueue kworkers churning without completing -- the lru_add_drain livelock; "
             f"rerun under EEVDF to split scheduler vs workload")
    d = sum(1 for p in os.listdir("/proc") if p.isdigit()
            and (read(f"/proc/{p}/stat").split(") ")[-1:] or [""])[0][:1] == "D")
    info(f"D-state tasks: {d}")


def wedge_capture(t: Tools) -> None:
    section("LIVE WEDGE CAPTURE  (sysrq -> ring buffer, bypassing the wedged disk)")
    if not t.is_root:
        error("--wedge needs root (writes /proc/sysrq-trigger). Rerun over SSH as root.")
        return
    try:
        with open("/proc/sys/kernel/sysrq", "w") as fp:
            fp.write("1")
    except OSError:
        pass
    for key, what in (("w", "blocked/D-state tasks"), ("l", "backtrace every CPU"), ("t", "all task states")):
        info(f"sysrq {key}: {what}")
        try:
            with open("/proc/sysrq-trigger", "w") as fp:
                fp.write(key)
        except OSError as e:
            warn(f"sysrq {key} failed: {e}")
        time.sleep(0.5)


# MAIN ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Find a past hang and explain it (montauk + sublimation).")
    ap.add_argument("--list", action="store_true", help="discover sources + list candidate events, no deep dive")
    ap.add_argument("--boot", type=int, default=None, help="restrict to journald boot N (0 current, -1 previous)")
    ap.add_argument("--at", default=None, help="analyze the event nearest a time, e.g. 12:43")
    ap.add_argument("--live", action="store_true", help="snapshot CURRENT state instead of hunting the past")
    ap.add_argument("--wedge", action="store_true", help="live: sysrq dumps first, then hunt (run over SSH while frozen)")
    ap.add_argument("--out", default=None, help="report path (default /tmp/pandemonium/)")
    args = ap.parse_args()

    t = Tools()
    info(f"system-forensics on {os.uname().nodename}  kernel {os.uname().release}  "
         f"root={'yes' if t.is_root else 'NO -- reduced coverage'}")
    info(f"tools: montauk_analyze={'y' if t.montauk_analyze else 'n'} "
         f"sublimation={'y' if t.sublimation else 'n'} journalctl={'y' if t.journalctl else 'n'} "
         f"coredumpctl={'y' if t.coredumpctl else 'n'}")
    if not t.sublimation:
        warn("sublimation not on PATH -- stream/numeric analysis falls back to plain Python (install it)")

    if args.wedge:
        wedge_capture(t)

    if args.live:
        live_snapshot(t)
    else:
        sources = discover_sources(t)
        events = find_events(t, sources)
        if args.list or not events:
            section("CANDIDATE EVENTS")
            if not events:
                info("no hang signature found in any source. If a freeze was real and left nothing,")
                info("it was a non-crashing GUI stall that recovered -- rerun --wedge WHILE it is frozen.")
            for e in sorted(events, key=lambda e: (e.severity, e.source.recency), reverse=True)[:25]:
                info(f"  sev{e.severity}  {e.kind:<12} {e.ts:<22} [{e.source.name}]  {e.line[:80]}")
        else:
            event = pick_event(events, args)
            window = scrape_window(t, event)
            analyze_event(t, event, window)
            others = [e for e in sorted(events, key=lambda e: (e.severity, e.source.recency), reverse=True)
                      if e is not event][:8]
            if others:
                section("OTHER CANDIDATE EVENTS")
                for e in others:
                    info(f"  sev{e.severity}  {e.kind:<12} {e.ts:<22} [{e.source.name}]")

    out = args.out or f"/tmp/pandemonium/forensics-{datetime.now():%Y%m%d-%H%M%S}.txt"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fp:
        fp.write("\n".join(REPORT_LINES) + "\n")
    info(f"report written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
