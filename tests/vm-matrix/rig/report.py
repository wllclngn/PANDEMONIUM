# report.py -- what a ticket's runs show, judged against what it expects.
#
# Reads only what run directories hold, so it regenerates at any time against
# the ticket as it stands now: a corrected expectation re-judges recorded runs
# without booting anything. The phases a run executed come from the spec.toml
# beside it, not from the ticket, so a narrowed --phases run is judged on what
# it actually ran.
#
# A cell's verdict comes from its latest batch of reps (the newest run dir and
# the lower-numbered reps immediately before it): MATCH only when every rep in
# the batch matches, otherwise the newest rep that did not. Only runs whose
# recorded identity still names the ticket's kernel and scheduler count: a run
# of another revision under the same label is reported as stale, not judged.

import csv
import io
import json
import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from rig.common import MONTAUK, TICKETS, log, read_json, sha256_file, slug, write_json
from rig.spec import LOAD_PHASES, PHASES, Kernel, Scheduler, Ticket

# Where an expected error substring may surface: the verifier's own log, libbpf
# on veristat's stderr (either pass), or the scheduler's loader when it attaches.
ERROR_SOURCES = ("veristat-v.log", "veristat-v.err", "veristat.err", "sched.log")
EXPECT_FIELDS = ("veristat", "fail_progs", "error", "attach", "phases", "detach")

SIGNATURES = (
    ("WARNING", re.compile(r"WARNING:")),
    ("BUG", re.compile(r"BUG:")),
    ("Oops", re.compile(r"\bOops\b")),
    ("lockdep", re.compile(r"WARNING: (possible (circular|recursive|irq)|inconsistent lock"
                           r"|suspicious RCU|bad unlock|held lock|lock held)")),
    ("RCU stall", re.compile(r"rcu_\w+ (self-)?detected stall")),
    ("soft lockup", re.compile(r"soft lockup")),
    ("hard LOCKUP", re.compile(r"hard LOCKUP")),
    ("Kernel panic", re.compile(r"Kernel panic - not syncing")),
)
PANIC = SIGNATURES[-1][1]

# veristat -v prints one of these ahead of each program's verifier log.
PROCESSING = re.compile(r"^PROCESSING (.+)/([^/,]+), DURATION US: \d+, VERDICT: (\w+), VERIFIER LOG:")
PROCESSED = re.compile(r"^processed (\d+) insns")
VSTATS = re.compile(r"^(processed \d+ insns|verification time|stack depth)")
# veristat's stats table after the last block: its header row, then "Done.".
TABLE_END = re.compile(r"^(File\s+Program\s+Verdict\b|Done\. Processed \d+ files)")
NO_LOG = "no verifier log: the program failed before the kernel verified it"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MONTAUK_ERROR = re.compile(r"\[ERROR\]\s+(.*)")
KMSG = re.compile(r"^\d+,\d+,(\d+),[^;]*;")
REP = re.compile(r"-rep(\d+)$")
HEX = re.compile(r"[0-9a-f]{40,64}")
SEV = {2: "high", 1: "med", 0: "low"}
SCHED_EXT_CAP = 60


def table(headers, rows, indent: str = "  ") -> list[str]:
    rows = [[str(c) for c in r] for r in rows]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    return [(indent + "  ".join(c.ljust(w[i]) for i, c in enumerate(r))).rstrip()
            for r in [list(headers), *rows]]


def md_table(headers, rows) -> list[str]:
    def cell(c):
        return str(c).replace("|", "\\|").replace("\n", " ") or " "
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return out


def _text(rec, name: str) -> str:
    return rec.files.get(name, b"").decode("utf-8", "replace")


def _read(p: Path) -> str:
    try:
        return p.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def _short(v) -> str:
    s = str(v)
    return HEX.sub(lambda m: m.group(0)[:12], s)


def _run_phases(d: Path, t: Ticket) -> tuple[str, ...]:
    try:
        return tuple(tomllib.loads((d / "spec.toml").read_text())["run"]["phases"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"{d}: spec.toml unreadable ({type(e).__name__}: {e}); judging it against "
            f"the ticket's phases {','.join(t.run.phases)}", "WARN")
        return t.run.phases


def _veristat_rows(data: bytes) -> list[dict]:
    text = data.decode("utf-8", "replace")
    return [r for r in csv.DictReader(io.StringIO(text)) if r.get("prog_name")]


def _error_lines(vlog: str, failing: list[str], want: str,
                 insns: dict[str, str]) -> dict[str, list[str]]:
    """Per failing program: the line that names why it failed and its
    'processed N insns' line. The error is the expected substring's line when
    the ticket names one, else the last line before the verifier's stats.

    veristat -v prints each program's log buffer without zeroing it first, so
    a program that failed before the kernel verified it shows heap leftovers,
    often an earlier program's whole log. A block counts only when its
    processed count is the one the csv pass (a zeroed buffer) recorded for
    that program; insns maps program to that csv total_insns."""
    blocks: dict[str, list[str]] = {}
    cur = None
    for line in vlog.splitlines():
        m = PROCESSING.match(line)
        if m:
            cur = m.group(2) if m.group(3) != "success" else None
            if cur:
                blocks[cur] = []
            continue
        if TABLE_END.match(line):
            cur = None
            continue
        if cur:
            blocks[cur].append(line.rstrip())
    out = {}
    for prog in failing:
        lines = blocks.get(prog)
        if lines is None:
            continue
        counts = [m.group(1) for m in map(PROCESSED.match, lines) if m]
        csv_n = insns.get(prog, "")
        if csv_n == "0":
            out[prog] = [NO_LOG]
            continue
        if csv_n.isdigit() and csv_n not in counts:
            out[prog] = [f"verbose log not this program's: processed "
                         f"{', '.join(counts) or 'nothing'}, csv pass {csv_n} insns"]
            continue
        hit = [l for l in lines if want and want in l][-1:]
        if not counts and not hit:
            out[prog] = [NO_LOG]
            continue
        end = next((i for i, l in enumerate(lines) if VSTATS.match(l)), len(lines))
        picked = hit or [l for l in lines[:end] if l.strip()][-1:]
        picked += [l for l in lines if PROCESSED.match(l)][:1]
        out[prog] = picked
    return out


def _sched_ext_lines(kmsg: str) -> list[str]:
    out = []
    for line in kmsg.splitlines():
        if "sched_ext" not in line or line.startswith(" "):
            continue
        m = KMSG.match(line)
        out.append(f"[{int(m.group(1)) / 1e6:12.6f}] {line[m.end():]}" if m else line)
    return out


def _guest_montauk(rec) -> tuple[list[str], str]:
    """How the guest tracer ended when it did not end cleanly, and the first
    error it logged."""
    kv = rec.kv
    notes = []
    if kv.get("montauk.ready") == "timeout":
        notes.append("guest montauk never reported its trace attached")
    ex = kv.get("montauk.exit", "")
    if ex and ex != "0":
        notes.append(f"guest montauk exit {ex}")
    text = ANSI.sub("", _text(rec, "montauk.log"))
    first = next((m.group(1).strip() for m in map(MONTAUK_ERROR.search, text.splitlines()) if m), "")
    return notes, first


def _montauk(run) -> dict:
    kv = run.records.kv
    notes, why = _guest_montauk(run.records)
    env = read_json(run.dir / "analyze.json")
    err = run.cell.get("analyze_error", "")
    via = ""
    if not env:
        # A guest that hung before finish() never sent events.bin; the ttyS2
        # stream, when the run had one, holds what montauk saw until then.
        stream = read_json(run.dir / "analyze-stream.json")
        if stream:
            env, via = stream, "from the ttyS2 stream, events.bin never arrived: "
        err = err or run.cell.get("analyze_stream_error", "")
    skipped = kv.get("file.events.bin.skipped", "")
    if not env:
        if err:
            head = f"analysis failed: {(err.strip().splitlines() or [err])[-1]}"
        elif skipped:
            head = f"no capture: events.bin was {skipped} bytes, over the serial cap"
        elif "montauk.pid" not in kv and "montauk.exit" not in kv:
            head = "no capture: montauk did not run (no load phase ran)"
        else:
            head = "no capture"
        head = "; ".join([head, *notes] + ([f"montauk.log: {why}"] if why else []))
        return {"headline": head, "error": err, "reports": []}
    tr = env.get("trace", {})
    raw = env.get("reports", [])
    summary = next((r.get("verdict", "") for r in raw if r.get("name") == "summary"), "")
    parts = [via + (summary or f"{tr.get('events', 0)} events")]
    if "capture_completeness" in tr:
        parts.append(f"{float(tr['capture_completeness']) * 100:.2f}% of events kept, "
                     f"{tr.get('dropped_events', 0)} dropped")
    # Severity is each montauk report's own call (0 low, 1 med, 2 high).
    offs = [(int(o.get("sev", 0)), r.get("name", ""), o) for r in raw for o in r.get("offenders", [])]
    if offs:
        tiers = {s: sum(1 for o in offs if o[0] == s) for s in (2, 1, 0)}
        top = max(s for s, _, _ in offs)
        sev, name, o = next(x for x in offs if x[0] == top)
        parts.append(f"offenders {tiers[2]} high, {tiers[1]} med, {tiers[0]} low; first "
                     f"{SEV.get(sev, sev)}: {name} {o.get('kind', '')} {o.get('id', '')} "
                     f"{o.get('metric', '')}={o.get('value', '')}")
    else:
        parts.append("no offenders")
    parts += notes + ([f"montauk.log: {why}"] if why and notes else [])
    reports = [{"name": r.get("name", ""), "class": r.get("class", ""), "verdict": r.get("verdict", "")}
               for r in raw]
    return {"headline": "; ".join(parts), "error": err, "reports": reports}


def _expected(exp) -> dict:
    out = {}
    for f in EXPECT_FIELDS:
        v = getattr(exp, f)
        if v:
            out[f] = ", ".join(v) if isinstance(v, tuple) else v
    return out


def evaluate(t: Ticket, run) -> dict:
    rec = run.records
    kv = rec.kv
    exp = t.expectation(run.kernel, run.scheduler)
    ran = _run_phases(run.dir, t)
    res = {p: kv.get(f"phase.{p}.result", "") for p in PHASES}

    rows = _veristat_rows(rec.files.get("veristat.csv", b""))
    failing = sorted({r["prog_name"] for r in rows if r.get("verdict") != "success"})
    if res["veristat"] == "skip":
        vv = "skip"
    elif not rows:
        vv = "absent"
    else:
        vv = "failure" if failing else "success"

    found = ""
    if exp.error:
        found = next((n for n in ERROR_SOURCES if exp.error in _text(rec, n)), "")

    load_ran = [p for p in ran if p in LOAD_PHASES]
    bad_load = [f"{p}={res[p] or 'absent'}" for p in load_ran if res[p] != "ok"]
    phases_got = "fail" if bad_load else "ok"

    checks = []

    # A field whose phase the run never requested is shown, not judged (ok
    # None): a narrowed run neither fails nor passes on work it did not do.
    def check(field: str, want: str, got: str, ok: bool, requested: bool) -> None:
        if not requested:
            got, ok = "not requested", None
        checks.append({"field": field, "expected": want, "got": got, "ok": ok})

    verified = "veristat" in ran
    if exp.veristat:
        check("veristat", exp.veristat, vv, vv == exp.veristat, verified)
    if exp.fail_progs:
        check("fail_progs", ", ".join(sorted(exp.fail_progs)), ", ".join(failing) or "none",
              set(exp.fail_progs) == set(failing), verified)
    if exp.error:
        check("error", exp.error, f"found in {found}" if found else "not found", bool(found),
              verified or "attach" in ran)
    if exp.attach:
        check("attach", exp.attach, res["attach"] or "absent", res["attach"] == exp.attach,
              "attach" in ran)
    if exp.phases:
        check("phases", exp.phases, phases_got + (f" ({', '.join(bad_load)})" if bad_load else ""),
              phases_got == exp.phases, bool(load_ran))
    if exp.detach:
        check("detach", exp.detach, res["detach"] or "absent", res["detach"] == exp.detach,
              "detach" in ran)

    console = _read(run.dir / "console.log")
    kmsg = _text(rec, "kmsg.txt")
    # None, not 0, when no kmsg.txt arrived: an unscanned source is not a clean one.
    have_kmsg = "kmsg.txt" in rec.files
    sigs = {name: (sum(1 for _ in rx.finditer(console)),
                   sum(1 for _ in rx.finditer(kmsg)) if have_kmsg else None)
            for name, rx in SIGNATURES}

    incomplete = []
    if not rec.complete:
        incomplete.append("no @@END" + (", stream ended mid-record" if rec.truncated else ""))
    if run.reason != "clean-exit":
        incomplete.append(f"reaped {run.reason or 'without a recorded reason'}")
    if PANIC.search(console):
        incomplete.append("kernel panic on the console")

    verdict = ("INCOMPLETE" if incomplete else
               "MISMATCH" if any(c["ok"] is False for c in checks) else "MATCH")

    kid = run.cell.get("kernel_identity") or {}
    sid = run.cell.get("scheduler_identity") or {}
    tails = [f"{n}: tail of {len(rec.files.get(n, b''))} bytes kept, original {kv.get(f'file.{n}.size', '?')}"
             for n, f in rec.file_flags.items() if f == "tail"]
    if kv.get("file.events.bin.skipped"):
        tails.append(f"events.bin: not sent, {kv['file.events.bin.skipped']} bytes")

    return {
        "kernel": run.kernel, "scheduler": run.scheduler, "rep": run.rep,
        "dir": str(run.dir), "run": run.dir.name,
        "reason": run.reason, "seconds": run.seconds,
        "complete": rec.complete, "truncated": rec.truncated,
        "release": kv.get("uname.release") or kid.get("release", ""),
        "commit": kid.get("sha", ""),
        "kernel_identity": kid, "scheduler_identity": sid,
        "boot": {k: kv[k] for k in ("uname.release", "uname.version", "nr_cpus", "btf",
                                    "sched_ext", "scratch") if k in kv},
        "modules": {k.removeprefix("module."): v for k, v in kv.items() if k.startswith("module.")},
        "phase_list": list(ran),
        "phases": [{"phase": p, "result": res[p] or "absent", "ms": kv.get(f"phase.{p}.ms", ""),
                    "detail": kv.get(f"phase.{p}.detail", "")} for p in ran],
        "attach": {k: kv.get(f"attach.{k}", "") for k in ("state", "ops", "ms")}
                  | {"pid": kv.get("sched.pid", ""), "exit": kv.get("sched.exit", "")},
        "detach": {k: kv.get(f"detach.{k}", "") for k in ("state", "ms")},
        "health": {p: {k: kv.get(f"health.{p}.{k}", "") for k in ("state", "ops", "alive")}
                   for p in ran if p in LOAD_PHASES and f"health.{p}.state" in kv},
        "veristat": {"verdict": vv, "failing": failing, "rc": kv.get("veristat.rc", ""),
                     "verbose_rc": kv.get("veristat.verbose.rc", ""),
                     "programs": [{"file": r.get("file_name", ""), "prog": r["prog_name"],
                                   "verdict": r.get("verdict", ""),
                                   "insns": r.get("total_insns", ""),
                                   "states": r.get("total_states", ""),
                                   "peak_states": r.get("peak_states", "")} for r in rows]},
        "error_lines": _error_lines(_text(rec, "veristat-v.log"), failing, exp.error,
                                    {r["prog_name"]: r.get("total_insns") or "" for r in rows}),
        "signatures": sigs,
        "sched_ext": _sched_ext_lines(kmsg),
        "montauk": _montauk(run),
        "tails": tails,
        "expected": _expected(exp),
        "checks": checks,
        "incomplete": incomplete,
        "verdict": verdict,
    }


def _unreadable(t: Ticket, kernel: str, scheduler: str, d: Path, err: Exception) -> dict:
    m = REP.search(d.name)
    return {"kernel": kernel, "scheduler": scheduler, "rep": int(m.group(1)) if m else 0,
            "dir": str(d), "run": d.name, "reason": "", "seconds": 0.0,
            "release": "", "commit": "", "kernel_identity": {}, "scheduler_identity": {},
            "boot": {}, "modules": {}, "phase_list": [], "phases": [], "attach": {}, "detach": {},
            "health": {}, "veristat": {"verdict": "absent", "failing": [], "rc": "",
                                       "verbose_rc": "", "programs": []},
            "error_lines": {}, "signatures": {}, "sched_ext": [],
            "montauk": {"headline": "no capture", "error": "", "reports": []}, "tails": [],
            "expected": _expected(t.expectation(kernel, scheduler)), "checks": [],
            "incomplete": [f"run directory unreadable: {type(err).__name__}: {err}"],
            "verdict": "INCOMPLETE"}


def _batch(dirs: list[Path]) -> list[Path]:
    """The newest run and the reps of the same batch before it, newest first:
    walking back, rep numbers fall until rep 0."""
    out, last = [], None
    for d in reversed(dirs):
        m = REP.search(d.name)
        n = int(m.group(1)) if m else 0
        if last is not None and n >= last:
            break
        out.append(d)
        last = n
        if n == 0:
            break
    return out


def _norm_path(p: str) -> str:
    q = Path(p or ".").as_posix().strip("/")
    return "" if q == "." else q


def _stale(k: Kernel, s: Scheduler, cell: dict, hashes: dict) -> list[str]:
    """What a recorded run's identity says differently from the ticket's
    kernel and scheduler as they stand: a run of another revision under the
    same labels. Only the fields the identity recorded are compared."""
    def content(p: str):
        if p not in hashes:
            hashes[p] = sha256_file(Path(p)) if Path(p).is_file() else None
        return hashes[p]

    kid = cell.get("kernel_identity") or {}
    sid = cell.get("scheduler_identity") or {}
    want: dict = {"kind": k.kind}
    if k.kind == "git":
        want.update(sha=k.sha, cc=k.cc, config_extra=list(k.config))
    else:
        want.update(image=str(Path(k.image)), modules=str(Path(k.modules)) if k.modules else None,
                    image_sha256=content(k.image))
    why = [f"kernel {f} {_short(kid[f])}" for f, v in want.items() if f in kid and kid[f] != v]
    want = {"kind": s.kind, "args": list(s.args), "ops": s.ops}
    if s.kind == "cargo":
        # "binary" is the built path or, from the build's own identity, its
        # name: the name compares either way.
        if "binary" in sid and Path(str(sid["binary"])).name != s.binary:
            why.append(f"scheduler binary {sid['binary']}")
        want.update(package=s.package)
        if s.worktree:
            want.update(source="worktree",
                        dir=str((Path(s.worktree) / _norm_path(s.path)).resolve()))
        else:
            want.update(source="commit", commit=s.commit, path=_norm_path(s.path))
    elif s.kind == "binary":
        want.update(binary=str(Path(s.binary)), binary_sha256=content(s.binary))
    why += [f"scheduler {f} {_short(sid[f])}" for f, v in want.items() if f in sid and sid[f] != v]
    return why


def _cell(k: Kernel, s: Scheduler, evals: list[dict], total: int, t: Ticket,
          stale: list[tuple[str, list[str]]]) -> dict:
    if not evals:
        return {"kernel": k.label, "scheduler": s.label, "verdict": "NOT RUN", "evals": [],
                "rep": None, "total": total, "stale": stale,
                "expected": _expected(t.expectation(k.label, s.label))}
    rep = next((e for e in evals if e["verdict"] != "MATCH"), evals[0])
    return {"kernel": k.label, "scheduler": s.label, "verdict": rep["verdict"], "evals": evals,
            "rep": rep, "total": total, "stale": stale, "expected": rep["expected"]}


def _stale_line(c: dict) -> str:
    stale = c.get("stale") or []
    if not stale:
        return ""
    name, why = stale[-1]
    return (f"{len(stale)} run{'s' if len(stale) != 1 else ''} of another revision not "
            f"counted, newest {name}: {', '.join(why)}")


def _counts(cells: list[dict]) -> str:
    order = ("MATCH", "MISMATCH", "INCOMPLETE", "NOT RUN")
    n = {v: sum(1 for c in cells if c["verdict"] == v) for v in order}
    return ", ".join(f"{n[v]} {v}" for v in order) + f" of {len(cells)} cells"


def _notes(c: dict) -> str:
    e = c["rep"]
    if e is None:
        return _stale_line(c) or "no run recorded"
    out = list(e["incomplete"])
    out += [f"{x['field']}: expected {x['expected']}, got {x['got']}" for x in e["checks"]
            if x["ok"] is False]
    hits = [f"{n} {max(c, k or 0)}" for n, (c, k) in e["signatures"].items() if c or k]
    if hits:
        out.append("splats: " + ", ".join(hits))
    return "; ".join(out)


def _reps(c: dict) -> str:
    ev = c["evals"]
    return f"{sum(1 for e in ev if e['verdict'] == 'MATCH')}/{len(ev)}" if ev else "0/0"


def _phase_cell(e: dict | None, field: str) -> str:
    if e is None:
        return "-"
    if field == "veristat":
        v = e["veristat"]
        return v["verdict"] + (f" ({', '.join(v['failing'])})" if v["failing"] else "")
    if field == "phases":
        load = [p for p in e["phases"] if p["phase"] in LOAD_PHASES]
        bad = [f"{p['phase']}={p['result']}" for p in load if p["result"] != "ok"]
        if not load:
            return "none run"
        if all(p["result"] == "skip" for p in load):
            return "skip"
        return ("fail: " + ", ".join(bad)) if bad else "ok"
    return next((p["result"] for p in e["phases"] if p["phase"] == field), "-")


def _kernel_source(k: Kernel) -> str:
    if k.kind == "git":
        return k.url + (f" ({k.ref})" if k.ref else "")
    return k.image + (f", modules {k.modules}" if k.modules else "")


def _kernel_commit(k: Kernel, cells: list[dict]) -> str:
    if k.kind == "git":
        return k.sha[:12]
    for c in cells:
        if c["kernel"] == k.label and c["rep"]:
            h = c["rep"]["kernel_identity"].get("image_sha256", "")
            if h:
                return f"image sha256 {h[:12]}"
    return ""


def _releases(label: str, cells: list[dict]) -> str:
    rel = sorted({c["rep"]["release"] for c in cells
                  if c["kernel"] == label and c["rep"] and c["rep"]["release"]})
    return ", ".join(rel) or "not booted"


def _sched_source(s: Scheduler) -> str:
    if s.kind == "cargo":
        if s.commit:
            return (f"{s.url or s.repo}@{s.commit[:12]}" + (f":{s.path}" if s.path != "." else "")
                    + (f" (local {s.repo})" if s.url and s.repo else ""))
        return f"worktree {s.worktree}"
    if s.kind == "binary":
        return s.binary
    return "kernel default"


# Identity keys the tables already show; the rest is what the build recorded.
SHOWN = {"kernel": {"label", "kind", "url", "sha", "ref", "mirrors", "image", "modules",
                    "note", "release", "config_extra"},
         "scheduler": {"label", "kind", "repo", "url", "mirrors", "ref", "commit", "worktree",
                       "path", "package", "binary", "objects", "args", "ops", "note", "sha256",
                       "inputs"}}


def _identity(label: str, cells: list[dict], group: str) -> str:
    for c in cells:
        if c["rep"] and c[group] == label:
            ident = c["rep"][f"{group}_identity"]
            return " ".join(f"{k}={_short(v)}" for k, v in sorted(ident.items())
                            if k not in SHOWN[group] and isinstance(v, (str, int, float, bool))
                            and v not in ("", None))
    return ""


def _cell_section(c: dict, t: Ticket) -> list[str]:
    L = [f"CELL {c['kernel']} x {c['scheduler']}", f"  verdict: {c['verdict']}"]
    e = c["rep"]
    stale = _stale_line(c)
    if e is None:
        L.append(f"  no run of the current revision under {TICKETS / t.slug / 'runs'}"
                 if stale else f"  no run recorded under {TICKETS / t.slug / 'runs'}")
        if stale:
            L.append(f"  stale: {stale}")
        exp = c["expected"]
        L.append("  expected: " + (", ".join(f"{k} {v}" for k, v in exp.items()) or "nothing asserted"))
        return L
    older = c["total"] - len(c["evals"])
    L.append(f"  run: {e['run']} (rep {e['rep']}; batch of {len(c['evals'])}, {_reps(c)} MATCH"
             + (f"; {older} earlier run{'s' if older != 1 else ''} not counted)" if older else ")"))
    if stale:
        L.append(f"  stale: {stale}")
    L.append(f"  dir: {e['dir']}")
    L.append(f"  reaped: {e['reason'] or 'unknown'} after {e['seconds']:.1f} s")
    for why in e["incomplete"]:
        L.append(f"  incomplete: {why}")
    b = e["boot"]
    if b:
        L.append(f"  booted: {b.get('uname.release', '?')}, {b.get('nr_cpus', '?')} cpus, "
                 f"btf {b.get('btf', '?')}, sched_ext {b.get('sched_ext', '?')}, "
                 f"scratch {b.get('scratch', '?')}")
    if e["commit"]:
        L.append(f"  kernel commit: {e['commit']}")
    if e["modules"]:
        L.append("  modules: " + ", ".join(f"{k} {v}" for k, v in sorted(e["modules"].items())))
    sid = {k: v for k, v in e["scheduler_identity"].items() if isinstance(v, (str, int, float, bool))}
    if sid:
        L.append("  scheduler: " + " ".join(f"{k}={_short(v)}" for k, v in sorted(sid.items())))

    L.append("  expected against got")
    if e["checks"]:
        L += table(("field", "expected", "got", "result"),
                   [(x["field"], x["expected"], x["got"],
                     {True: "ok", False: "MISMATCH", None: "not judged"}[x["ok"]])
                    for x in e["checks"]], "    ")
    else:
        L.append("    nothing asserted for this cell")

    if e["phases"]:
        L.append("  phases")
        L += table(("phase", "result", "ms", "detail"),
                   [(p["phase"], p["result"], p["ms"] or "-", p["detail"]) for p in e["phases"]], "    ")
    for name, facts, spec in (
            ("attach", e["attach"], (("state", "state {}"), ("ops", "ops {}"), ("ms", "{} ms"),
                                     ("pid", "pid {}"), ("exit", "scheduler exit {}"))),
            ("detach", e["detach"], (("state", "state {}"), ("ms", "{} ms")))):
        said = [fmt.format(facts[k]) for k, fmt in spec if facts.get(k)]
        if said:
            L.append(f"  {name}: " + ", ".join(said))
    if e["health"]:
        L.append("  health after each load phase")
        L += table(("phase", "state", "ops", "alive"),
                   [(p, h["state"], h["ops"], h["alive"]) for p, h in e["health"].items()], "    ")

    v = e["veristat"]
    L.append(f"  veristat: {v['verdict']}" + (f", rc {v['rc']}" if v["rc"] else "")
             + (f", verbose rc {v['verbose_rc']}" if v["verbose_rc"] else ""))
    if v["programs"]:
        many = len({p["file"] for p in v["programs"]}) > 1
        L += table(("program", "verdict", "insns", "states", "peak_states"),
                   [((f"{p['file']}/" if many else "") + p["prog"], p["verdict"], p["insns"],
                     p["states"], p["peak_states"]) for p in v["programs"]], "    ")
    if e["error_lines"]:
        L.append("  verifier errors")
        for prog, lines in e["error_lines"].items():
            L += [f"    {prog}: {l.strip()}" for l in lines]
    elif v["failing"]:
        L.append("  verifier errors: no verbose log for " + ", ".join(v["failing"]))

    sigs = e["signatures"]
    if sigs:
        hits = [(n, c, "-" if k is None else k) for n, (c, k) in sigs.items() if c or k]
        scanned = "the console" + (" or kmsg" if all(k is not None for _, k in sigs.values())
                                   else " (no kmsg.txt arrived)")
        if hits:
            L.append(f"  kernel signatures on {scanned}")
            L += table(("signature", "console", "kmsg"), hits, "    ")
        else:
            L.append(f"  kernel signatures: none on {scanned}")
    if e["sched_ext"]:
        lines = e["sched_ext"]
        L.append(f"  sched_ext in kmsg ({len(lines)} lines)")
        L += [f"    {l}" for l in lines[:SCHED_EXT_CAP]]
        if len(lines) > SCHED_EXT_CAP:
            L.append(f"    {len(lines) - SCHED_EXT_CAP} more in files/kmsg.txt")
    m = e["montauk"]
    L.append(f"  montauk: {m['headline']}")
    if m["reports"]:
        L += table(("report", "class", "verdict"),
                   [(r["name"], r["class"] or "-", r["verdict"]) for r in m["reports"]], "    ")
    for tl in e["tails"]:
        L.append(f"  truncated: {tl}")
    others = [x for x in c["evals"] if x is not e]
    if others:
        L.append("  other reps in the batch")
        L += table(("run", "verdict", "reaped", "notes"),
                   [(x["run"], x["verdict"], x["reason"] or "?",
                     _notes({"rep": x})) for x in others], "    ")
    return L


BEHAVIOUR_P = 0.05


def _behaviour(t: Ticket, cells: list[dict]) -> list[dict]:
    """montauk's population comparison, per kernel, of every cell whose load
    phases ran, over the .prom recording each guest's montauk kept. Each
    recording is copied under its cell's name first: the capture label is the
    file name, and two guests' hourly files can share one."""
    out = []
    for k in t.kernels:
        picks = []
        for c in cells:
            e = c["rep"]
            if c["kernel"] != k.label or not e:
                continue
            proms = sorted((Path(e["dir"]) / "files").glob("*.prom"))
            ran = any(p["phase"] in LOAD_PHASES and p["result"] == "ok" for p in e["phases"])
            if proms and ran:
                picks.append((c["scheduler"], proms))
        if len(picks) < 2:
            continue
        entry = {"kernel": k.label, "schedulers": [s for s, _ in picks], "pairs": [],
                 "metrics": 0, "compared": 0, "error": ""}
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for label, proms in picks:
                dst = Path(tmp) / f"{slug(label)}.prom"
                with open(dst, "wb") as f:
                    for p in proms:
                        f.write(p.read_bytes())
                files.append(str(dst))
            argv = [str(MONTAUK), "--analyze", *files, "--by", "capture", "--no-emit", "--json"]
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=900)
                env = json.loads(r.stdout) if r.stdout.strip() else {}
            except (OSError, subprocess.TimeoutExpired, ValueError) as err:
                r, env = None, {}
                entry["error"] = str(err)
        if r is not None and r.returncode != 0:
            entry["error"] = (r.stderr.strip().splitlines() or [f"rc {r.returncode}"])[-1]
        pop = env.get("population") or {}
        write_json(TICKETS / t.slug / f"behaviour-{slug(k.label)}.json", env)
        for m in pop.get("metrics", []):
            entry["metrics"] += 1
            for cell in m.get("cells", []):
                if not cell.get("compared"):
                    continue
                entry["compared"] += 1
                means = {g["group"]: g.get("mean") for g in cell.get("groups", [])}
                # The cell names the series inside the metric (a core, a
                # process); commit and version are the recording's, not it.
                series = " ".join(kv for kv in cell.get("cell", "").split()
                                  if not kv.startswith(("commit=", "version=")))
                name = f"{m['metric']}{{{series}}}" if series else m["metric"]
                for pr in cell.get("pairs", []):
                    entry["pairs"].append({
                        "metric": name, "a": pr["a"], "b": pr["b"],
                        "mean_a": means.get(pr["a"]), "mean_b": means.get(pr["b"]),
                        "cliff": pr.get("cliff"), "magnitude": pr.get("magnitude", ""),
                        "p": pr.get("perm_p")})
        out.append(entry)
    return out


def _behaviour_rows(entry: dict) -> tuple[list, int, float]:
    every = entry["pairs"]
    hits = sorted((x for x in every if x["p"] is not None and x["p"] < BEHAVIOUR_P),
                  key=lambda x: x["p"])
    rows = [(x["metric"], f"{x['a']} vs {x['b']}", _short(x["mean_a"]), _short(x["mean_b"]),
             f"{x['cliff']:+.2f}" if x["cliff"] is not None else "-", x["magnitude"],
             f"{x['p']:.4f}") for x in hits]
    return rows, len(every), len(every) * BEHAVIOUR_P


def _behaviour_text(entries: list[dict]) -> list[str]:
    if not entries:
        return []
    L = ["", "BEHAVIOUR",
         "  montauk's population comparison (--by capture) of each kernel's cells whose load",
         f"  phases ran, over the guest montauk's .prom recording; pairs at perm p < {BEHAVIOUR_P}"]
    for e in entries:
        rows, pairs, chance = _behaviour_rows(e)
        L.append(f"  {e['kernel']}: {', '.join(e['schedulers'])}; {e['compared']} series across "
                 f"{e['metrics']} metrics compared, {len(rows)} of {pairs} pairs under p {BEHAVIOUR_P} "
                 f"(about {chance:.1f} expected by chance alone)"
                 + (f"; montauk: {e['error']}" if e["error"] else ""))
        if rows:
            L += table(("metric", "pair", "mean a", "mean b", "cliff", "size", "perm p"), rows, "    ")
    return L


def _behaviour_md(entries: list[dict]) -> list[str]:
    if not entries:
        return []
    L = ["", "#### Runtime behaviour (montauk)", ""]
    for e in entries:
        rows, pairs, chance = _behaviour_rows(e)
        L.append(f"**{e['kernel']}** ({', '.join(e['schedulers'])}): {len(rows)} of {pairs} metric "
                 f"pairs differ at permutation p < {BEHAVIOUR_P}, against about {chance:.1f} "
                 f"expected by chance.")
        if rows:
            L += [""] + md_table(("metric", "pair", "mean a", "mean b", "cliff", "size", "perm p"),
                                 rows[:12])
        L.append("")
    return L


def _render_text(t: Ticket, cells: list[dict], note: str, behaviour: list[dict]) -> str:
    L = [f"vm-matrix report for {t.id}"]
    if t.title:
        L.append(f"title: {t.title}")
    if t.url:
        L.append(f"url: {t.url}")
    L.append(f"source: {t.source}")
    if note:
        L.append(f"narrowed: {note}")
    L += [f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}", f"result: {_counts(cells)}", ""]
    L.append("MATRIX")
    L += table(("kernel", "scheduler", "verdict", "reps", "reaped", "veristat", "attach",
                "phases", "detach", "notes"),
               [(c["kernel"], c["scheduler"], c["verdict"], _reps(c),
                 c["rep"]["reason"] if c["rep"] else "-", _phase_cell(c["rep"], "veristat"),
                 _phase_cell(c["rep"], "attach"), _phase_cell(c["rep"], "phases"),
                 _phase_cell(c["rep"], "detach"), _notes(c)) for c in cells])
    L += ["", "KERNELS"]
    L += table(("label", "kind", "release", "commit", "source", "note"),
               [(k.label, k.kind, _releases(k.label, cells), _kernel_commit(k, cells),
                 _kernel_source(k), k.note) for k in t.kernels])
    for k in t.kernels:
        extra = _identity(k.label, cells, "kernel")
        if extra:
            L.append(f"  {k.label}: {extra}")
    L += ["", "SCHEDULERS"]
    L += table(("label", "kind", "source", "ops", "args", "note"),
               [(s.label, s.kind, _sched_source(s), s.ops or "any", " ".join(s.args) or "-", s.note)
                for s in t.schedulers])
    for s in t.schedulers:
        extra = _identity(s.label, cells, "scheduler")
        if extra:
            L.append(f"  {s.label}: {extra}")
        for path, h in s.sha256:
            L.append(f"  {s.label}: pinned {path} sha256 {h[:16]}")
    L += _behaviour_text(behaviour)
    for c in cells:
        L += [""] + _cell_section(c, t)
    return "\n".join(L) + "\n"


def _render_md(t: Ticket, cells: list[dict], note: str, behaviour: list[dict]) -> str:
    head = f"[{t.id}]({t.url})" if t.url else t.id
    L = [f"### vm-matrix: {head}", ""]
    if t.title:
        L += [t.title, ""]
    if note:
        L += [f"Narrowed to {note}; summary.md covers the whole matrix.", ""]
    n_match = sum(1 for c in cells if c["verdict"] == "MATCH")
    rest = [f"{sum(1 for c in cells if c['verdict'] == v)} {v}" for v in ("MISMATCH", "INCOMPLETE", "NOT RUN")
            if any(c["verdict"] == v for c in cells)]
    tail = ""
    if rest:
        tail = "; " + (rest[0] if len(rest) == 1 else ", ".join(rest[:-1]) + " and " + rest[-1])
    L += [f"**Result:** {n_match} of {len(cells)} cells MATCH the ticket's expectations{tail}.", ""]
    L += ["#### Kernels", ""]
    L += md_table(("label", "release", "commit", "source", "note"),
                  [(k.label, _releases(k.label, cells), _kernel_commit(k, cells),
                    _kernel_source(k), k.note) for k in t.kernels])
    L += ["", "#### Schedulers", ""]
    L += md_table(("label", "kind", "source", "build identity", "ops", "note"),
                  [(s.label, s.kind, _sched_source(s), _identity(s.label, cells, "scheduler"),
                    s.ops or "any", s.note) for s in t.schedulers])
    L += ["", "#### Matrix", ""]
    L += md_table(("kernel", "scheduler", "verdict", "veristat", "attach", "phases", "detach", "notes"),
                  [(c["kernel"], c["scheduler"], f"**{c['verdict']}**", _phase_cell(c["rep"], "veristat"),
                    _phase_cell(c["rep"], "attach"), _phase_cell(c["rep"], "phases"),
                    _phase_cell(c["rep"], "detach"), _notes(c)) for c in cells])
    errs = [(c, prog, lines) for c in cells if c["rep"]
            for prog, lines in c["rep"]["error_lines"].items()]
    if errs:
        L += ["", "#### Failing programs", "", "```"]
        for c, prog, lines in errs:
            L.append(f"{c['kernel']} / {c['scheduler']} / {prog}")
            L += [f"  {l.strip()}" for l in lines]
        L.append("```")
    L += _behaviour_md(behaviour)
    return "\n".join(L) + "\n"


def overview(report_txt: Path) -> str:
    """The header and matrix of a written report.txt, for the terminal."""
    blocks = report_txt.read_text().split("\n\n")
    return "\n\n".join(blocks[:2]).rstrip() + "\n"


def build(t: Ticket, tag: str = "", note: str = "") -> tuple[Path, Path, bool]:
    """tag names a narrowed report (report-<tag>.txt, summary-<tag>.md), so
    the whole matrix's report.txt and summary.md are never overwritten by a
    subset; note says in words what it was narrowed to."""
    from rig import run as rrun

    dirs = rrun.cell_dirs(t)
    hashes: dict = {}
    cells = []
    for k, s in t.cells():
        fresh, stale = [], []
        for d in dirs.get((k.label, s.label), []):
            why = _stale(k, s, read_json(d / "cell.json"), hashes)
            if why:
                stale.append((d.name, why))
            else:
                fresh.append(d)
        evals = []
        for d in _batch(fresh):
            try:
                evals.append(evaluate(t, rrun.load_cell_dir(d)))
            except (OSError, ValueError, KeyError, TypeError) as err:
                evals.append(_unreadable(t, k.label, s.label, d, err))
        cells.append(_cell(k, s, evals, len(fresh), t, stale))
    out = TICKETS / t.slug
    out.mkdir(parents=True, exist_ok=True)
    suffix = f"-{tag}" if tag else ""
    txt, md = out / f"report{suffix}.txt", out / f"summary{suffix}.md"
    behaviour = _behaviour(t, cells)
    txt.write_text(_render_text(t, cells, note, behaviour))
    md.write_text(_render_md(t, cells, note, behaviour))
    return txt, md, bool(cells) and all(c["verdict"] == "MATCH" for c in cells)
