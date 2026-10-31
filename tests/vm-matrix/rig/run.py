# run.py -- one cell: boot a kernel and its initramfs in the runner container,
# then turn what came back over the serial ports into a run directory.
#
#   TICKETS/<ticket>/runs/<kernel>__<scheduler>/<stamp>-rep<N>/
#     spec.toml      the effective ticket
#     cell.json      identities, run parameters, cmdline, qemu argv, reason
#     console.log    ttyS0
#     results.bin    ttyS1, the record stream (rig.records)
#     stream.bin     ttyS2, the guest montauk's --stream-out (run.stream only)
#     reap-reason    "<reason> <seconds>"
#     vmrun.log      the container's own output, qemu's stderr included
#     files/         every @@FILE record
#     analyze.json   montauk --analyze over files/events.bin, when it arrived
#     analyze-stream.json
#                    montauk --analyze over stream.bin, when events.bin never
#                    arrived (a guest that hung before it could send it)

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from rig import docker
from rig.common import (HARNESS, MONTAUK, TICKETS, log, read_json, sha256_file, slug,
                        stamp, write_json)
from rig.kernels import BuiltKernel
from rig.records import Records, extract, parse
from rig.schedulers import BuiltScheduler
from rig.spec import Ticket, to_toml

VMRUN = HARNESS / "payloads" / "vmrun.py"
TRACE = "pand-"            # every guest workload process is named pand-*
SCRATCH_GB = 2
HOST_GRACE = 120           # past t.run.timeout before the host gives up on vmrun
ANALYZE_TIMEOUT = 900


@dataclass
class CellRun:
    ticket: str
    kernel: str
    scheduler: str
    rep: int
    dir: Path
    reason: str
    seconds: float
    records: Records
    cell: dict


def cell_id(kernel: str, scheduler: str, rep: int) -> str:
    return f"{slug(kernel)}__{slug(scheduler)}__rep{rep}"


def cmdline(t: Ticket, cell_id: str) -> str:
    """The vmm.* keys the guest init reads. The console keys ahead of them
    belong to vmrun."""
    return (f"vmm.cell={slug(cell_id)} vmm.duration={t.run.duration} "
            f"vmm.phases={','.join(t.run.phases)} vmm.trace={TRACE} "
            f"vmm.stream={int(t.run.stream)}")


def _root(t: Ticket, kernel: str, scheduler: str) -> Path:
    return TICKETS / t.slug / "runs" / f"{slug(kernel)}__{slug(scheduler)}"


def _new_dir(root: Path, rep: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    while True:
        d = root / f"{stamp()}-rep{rep}"
        try:
            d.mkdir()
            return d
        except FileExistsError:
            time.sleep(1)


def _reap_reason(d: Path) -> tuple[str, float]:
    try:
        reason, secs = (d / "reap-reason").read_text().split()
        return reason, float(secs)
    except (OSError, ValueError):
        return "", 0.0


def _kill_container(d: Path) -> None:
    """subprocess's timeout kills the docker client, not the container. This
    cell's fresh output directory is mounted by exactly one container."""
    q = subprocess.run(["docker", "ps", "-q", "--filter", f"volume={d}"],
                       capture_output=True, text=True)
    for c in q.stdout.split():
        log(f"killing runner container {c} (host timeout)", "WARN")
        subprocess.run(["docker", "kill", c], capture_output=True)


def _analyze(ev: Path, out: Path) -> tuple[dict, str]:
    """montauk's own JSON envelope, written verbatim; its text report is never
    read. Returns (what ran, failure text or "")."""
    argv = [str(MONTAUK), "--analyze", str(ev), "--json"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, capture_output=True, timeout=ANALYZE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"argv": argv}, str(e)
    ran = {"argv": argv, "rc": r.returncode, "seconds": round(time.monotonic() - t0, 2)}
    err = r.stderr.decode(errors="replace").strip()[-4000:]
    try:
        json.loads(r.stdout)
    except ValueError:
        return ran, err or f"rc {r.returncode}, no JSON on stdout"
    out.write_bytes(r.stdout)
    return ran, (err or f"rc {r.returncode}") if r.returncode else ""


def run_cell(t: Ticket, k: BuiltKernel, b: BuiltScheduler, initrd: Path,
             initrd_identity: dict, rep: int) -> CellRun:
    cid = cell_id(k.label, b.label, rep)
    d = _new_dir(_root(t, k.label, b.label), rep)
    (d / "spec.toml").write_text(to_toml(t))
    vmm = cmdline(t, cid)
    cell = {"ticket": t.id, "cell": cid, "kernel": k.label, "scheduler": b.label,
            "rep": rep, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cmdline": vmm, "run": asdict(t.run),
            "kernel_identity": {"image": str(k.image), "release": k.release,
                                **k.identity},
            "scheduler_identity": {"kind": b.kind, "binary": str(b.binary) if b.binary else None,
                                   "objects": [str(o) for o in b.objects],
                                   "args": list(b.args), "ops": b.ops, **b.identity},
            "initrd": str(initrd), "initrd_identity": initrd_identity,
            "analyzer": {"path": str(MONTAUK),
                         "sha256": sha256_file(MONTAUK) if MONTAUK.is_file() else None},
            "reason": "running", "seconds": 0.0}
    # Written first, so a run the host never finished still names its inputs.
    write_json(d / "cell.json", cell)

    env = {"SMP": t.run.smp, "THREADS": t.run.threads, "NUMA": t.run.numa,
           "MEM": t.run.mem, "APPEND": vmm, "TIMEOUT": t.run.timeout,
           "STALL_S": t.run.stall_s, "KVM": int(t.run.kvm), "PIN": int(t.run.pin_vcpus),
           "SCRATCH_GB": SCRATCH_GB}
    mounts = [(k.image, "/run/kernel", True), (initrd, "/run/initrd", True),
              (d, "/run/out", False), (VMRUN, "/run/vmrun.py", True)]
    log(f"{cid}: booting {k.release or k.label} "
        f"({'kvm' if t.run.kvm else 'tcg'}, smp {t.run.smp}), {d}")
    t0 = time.monotonic()
    try:
        rc = docker.run("runner", ["python3", "/run/vmrun.py"], mounts, env=env,
                        kvm=t.run.kvm, timeout=t.run.timeout + HOST_GRACE,
                        log_path=d / "vmrun.log").returncode
    except subprocess.TimeoutExpired:
        rc = None
        _kill_container(d)
    wall = time.monotonic() - t0

    reason, seconds = _reap_reason(d)
    if not reason:
        reason, seconds = ("host-timeout" if rc is None else f"vmrun-rc{rc}"), wall
        (d / "reap-reason").write_text(f"{reason} {seconds:.1f}\n")
    # vmrun removes it; a killed vmrun cannot.
    (d / "scratch.img").unlink(missing_ok=True)

    qemu = d / "qemu.json"
    cell["qemu"] = read_json(qemu)
    qemu.unlink(missing_ok=True)

    results = d / "results.bin"
    rec = parse(results.read_bytes() if results.is_file() else b"")
    extract(rec, d)
    ev = d / "files" / "events.bin"
    guest_env = d / "files" / "analyze.json"
    stream = d / "stream.bin"
    if not ev.is_file() and guest_env.is_file() and guest_env.stat().st_size:
        # The capture outgrew the trip, so the guest's montauk analyzed it in
        # place; its envelope is the cell's analysis.
        (d / "analyze.json").write_bytes(guest_env.read_bytes())
        cell["analyze"] = {"in_guest": True, "rc": rec.kv.get("montauk.analyze.rc", ""),
                           "capture_bytes": rec.kv.get("file.events.bin.skipped", "")}
        if cell["analyze"]["rc"] != "0":
            cell["analyze_error"] = f"in-guest montauk --analyze exited {cell['analyze']['rc']}"
    elif ev.is_file():
        cell["analyze"], err = _analyze(ev, d / "analyze.json")
        if err:
            cell["analyze_error"] = err
            log(f"{cid}: montauk --analyze failed: {err.splitlines()[-1]}", "WARN")
    elif stream.is_file() and stream.stat().st_size:
        cell["analyze_stream"], err = _analyze(stream, d / "analyze-stream.json")
        if err:
            cell["analyze_stream_error"] = err
            log(f"{cid}: montauk --analyze of stream.bin failed: {err.splitlines()[-1]}", "WARN")

    cell.update(reason=reason, seconds=round(seconds, 1), wall_seconds=round(wall, 1),
                vmrun_rc=rc,
                records={"complete": rec.complete, "truncated": rec.truncated,
                         "kv": len(rec.kv_order),
                         "files": {n: {"bytes": len(v), "flag": rec.file_flags[n]}
                                   for n, v in rec.files.items()}})
    write_json(d / "cell.json", cell)
    ok = reason == "clean-exit" and rec.complete
    log(f"{cid}: {reason} after {seconds:.0f}s, "
        f"{'complete' if rec.complete else 'INCOMPLETE'} ({len(rec.kv_order)} kv, "
        f"{len(rec.files)} files{', truncated' if rec.truncated else ''})",
        "INFO" if ok else "WARN")
    return CellRun(t.id, k.label, b.label, rep, d, reason, seconds, rec, cell)


def load_cell_dir(d: Path) -> CellRun:
    """A finished (or abandoned) run, re-read from disk. The records are
    re-parsed from results.bin, which is the ground truth."""
    cell = read_json(d / "cell.json")
    results = d / "results.bin"
    rec = parse(results.read_bytes() if results.is_file() else b"")
    reason, seconds = cell.get("reason", ""), float(cell.get("seconds", 0.0))
    if reason in ("", "running"):
        reason, seconds = _reap_reason(d)
        reason = reason or "interrupted"
    kernel, _, scheduler = d.parent.name.partition("__")
    _, _, rep = d.name.rpartition("-rep")
    return CellRun(cell.get("ticket", d.parent.parent.parent.name),
                   cell.get("kernel", kernel), cell.get("scheduler", scheduler),
                   int(cell.get("rep", rep if rep.isdigit() else 0)), d, reason, seconds,
                   rec, cell)


def cell_dirs(t: Ticket) -> dict[tuple[str, str], list[Path]]:
    """(kernel, scheduler) -> run directories, oldest first: the stamp leads
    each name, so name order is time order."""
    out = {}
    for k, s in t.cells():
        root = _root(t, k.label, s.label)
        out[(k.label, s.label)] = (sorted(p for p in root.iterdir() if p.is_dir())
                                   if root.is_dir() else [])
    return out
