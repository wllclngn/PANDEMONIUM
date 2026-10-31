# bisect.py -- which kernel commit turns a scheduler program's verdict, found by
# booting each candidate.
#
# git bisect drives the walk with --no-checkout, so the search itself checks
# out nothing; each commit it names is built by kernels.ensure and verified in a
# guest by the same veristat cell a ticket runs. A commit whose kernel will not
# build or boot, or whose program fails for a reason other than the one under
# bisection, is skipped rather than called good or bad.

import csv
import io
import re
import subprocess
from dataclasses import replace

from rig import guest, kernels, schedulers
from rig import run as rrun
from rig.common import KREPO, TICKETS, log, write_json
from rig.spec import Kernel, Ticket

# git words this "is the first bad commit" or, since it learned custom terms,
# "is the first 'bad' commit".
FIRST_BAD = re.compile(r"^([0-9a-f]{40}) is the first '?bad'? commit", re.M)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(KREPO), *args], capture_output=True, text=True,
                          check=check)


def verdict(rec, prog: str, error: str) -> tuple[str, str]:
    """("good" | "bad" | "skip", why) for one guest's veristat records."""
    if not rec.complete:
        return "skip", "the guest's records are incomplete"
    rows = list(csv.DictReader(io.StringIO(rec.files.get("veristat.csv", b"").decode(errors="replace"))))
    row = next((r for r in rows if r.get("prog_name") == prog), None)
    if row is None:
        return "skip", f"{prog} not in veristat's output"
    if row.get("verdict") == "success":
        return "good", f"{prog} verified, {row.get('total_insns')} insns"
    text = "".join(rec.files.get(n, b"").decode(errors="replace")
                   for n in ("veristat-v.log", "veristat-v.err", "veristat.err"))
    if error and error not in text:
        return "skip", f"{prog} failed without '{error}'"
    return "bad", f"{prog} rejected, {row.get('total_insns')} insns"


def _history(good: Kernel, bad: Kernel) -> None:
    """Both ends, and enough history between them for a merge base."""
    kernels.ensure_commit(good)
    kernels.ensure_commit(bad)
    if _git("merge-base", good.sha, bad.sha, check=False).returncode == 0:
        return
    since = _git("log", "-1", "--format=%cs", good.sha).stdout.strip()
    for url in (bad.url, *bad.mirrors):
        for sha in (good.sha, bad.sha):
            _git("fetch", "-q", f"--shallow-since={since}", url, sha, check=False)
        if _git("merge-base", good.sha, bad.sha, check=False).returncode == 0:
            return
    raise RuntimeError(f"no merge base of {good.sha[:12]} and {bad.sha[:12]} is reachable in {KREPO}")


def bisect(t: Ticket, sched: str, good: str, bad: str, url: str, mirrors: tuple[str, ...],
           paths: tuple[str, ...], prog: str, error: str) -> dict:
    s = next((x for x in t.schedulers if x.label == sched), None)
    if s is None:
        raise RuntimeError(f"no scheduler {sched!r} in {t.id}")
    bs = schedulers.ensure(s)

    def kernel(sha: str) -> Kernel:
        return Kernel(label=f"bisect-{sha[:12]}", kind="git", url=url, sha=sha, mirrors=mirrors)

    _history(kernel(good), kernel(bad))
    bt = replace(t, id=f"{t.id} bisect", kernels=(), schedulers=(s,),
                 run=replace(t.run, phases=("veristat",)))
    _git("bisect", "reset", check=False)
    start = _git("bisect", "start", "--no-checkout", bad, good, "--", *paths, check=False)
    if start.returncode != 0:
        raise RuntimeError(f"git bisect start: {start.stderr.strip() or start.stdout.strip()}")

    steps, first, stop = [], "", ""
    text = start.stdout
    try:
        while True:
            cur = _git("rev-parse", "BISECT_HEAD").stdout.strip()
            subject = _git("log", "-1", "--format=%h %ci %an | %s", cur).stdout.strip()
            k = kernel(cur)
            try:
                bk = kernels.ensure(k)
                initrd, ident = guest.initramfs(bk, bs)
                r = rrun.run_cell(bt, bk, bs, initrd, ident, len(steps))
                v, why = verdict(r.records, prog, error)
                where = str(r.dir)
            except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
                v, why, where = "skip", f"{type(e).__name__}: {e}", ""
            log(f"bisect: {subject}: {v.upper()} ({why})", "INFO" if v != "skip" else "WARN")
            steps.append({"commit": cur, "subject": subject, "verdict": v, "why": why, "run": where})
            out = _git("bisect", v, check=False)
            text = out.stdout + out.stderr
            m = FIRST_BAD.search(text)
            if m:
                first = m.group(1)
                break
            if out.returncode != 0 or "only 'skip'ped commits left" in text:
                stop = text.strip()
                break
            # A walk that names the same commit twice has stopped moving;
            # testing it again would only loop.
            nxt = _git("rev-parse", "BISECT_HEAD", check=False).stdout.strip()
            if nxt == cur:
                stop = f"git bisect did not move past {cur[:12]}: {text.strip()}"
                break
    finally:
        blog = _git("bisect", "log", check=False).stdout
        _git("bisect", "reset", check=False)

    result = {"ticket": t.id, "scheduler": sched, "prog": prog, "error": error,
              "good": good, "bad": bad, "paths": list(paths), "first_bad": first,
              "stopped": stop, "steps": steps, "git_bisect_log": blog,
              "first_bad_show": _git("show", "--stat", "--format=fuller", first).stdout if first else ""}
    write_json(TICKETS / t.slug / "bisect.json", result)
    return result
