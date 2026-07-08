#!/usr/bin/env python3
# orchestrate.py -- sweep (kernel x scheduler x PANDEMONIUM version x mode),
# boot each under qemu with montauk, fold each trace with montauk_analyze
# --report kstrand, and tabulate worst HELD/DARK strand-ms.
#
# The kernel-bug signature: a strand-ms that jumps on a 7.1.x/7.2.x kernel and
# stays low on 7.0.x, for BOTH schedulers. A PANDEMONIUM-only effect (EEVDF
# stays flat across kernels, only PANDEMONIUM climbs) is a PANDEMONIUM bug,
# not a kernel one -- the eevdf arm (pand.sched=none, the guest init's
# already-built-in control: the in-kernel default scheduler, never
# PANDEMONIUM) is the nonpartisan check that tells the two apart.

import argparse
import re
import subprocess
import sys
from pathlib import Path

from pand_docker import run_in_container

HERE = Path(__file__).parent.resolve()
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
KERNELS = CACHE / "kernels"
MONTAUK_ANALYZE = "/usr/local/bin/montauk_analyze"
SUBLIMATION = "/usr/local/bin/sublimation"


def sub_stat(vals: list[float], *cmd_args: str) -> float | None:
    """Reduce a value list through sublimation -- the strand duration is a
    rare, bursty event (mostly 0.0, occasional spikes), so the aggregate
    is a real numeric reduction over the repeats, never hand-rolled."""
    if not vals:
        return None
    r = subprocess.run([SUBLIMATION, *cmd_args],
                       input="\n".join(str(v) for v in vals),
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def log(m: str) -> None:
    print(f"[orchestrate] {m}", flush=True)


def parse_kstrand(report: str) -> dict:
    d = {"clean": False, "worst_held": 0.0, "worst_dark": 0.0, "strands": 0}
    if "no per-CPU kthread strands over threshold" in report:
        d["clean"] = True
        return d
    m = re.search(r"(\d+)\s+strands across", report)
    if m:
        d["strands"] = int(m.group(1))
    m = re.search(r"worst HELD strand\s+([\d.]+)ms", report)
    if m:
        d["worst_held"] = float(m.group(1))
    # montauk emits worst_held but not worst_dark; take max max_ms among
    # all-dark offenders in the table (comm cpu strands max_ms p99 held dark).
    for line in report.splitlines():
        f = line.split()
        if len(f) == 7:
            try:
                max_ms, held, dark = float(f[3]), int(f[5]), int(f[6])
                if dark > 0 and held == 0:
                    d["worst_dark"] = max(d["worst_dark"], max_ms)
            except ValueError:
                pass
    return d


def build_initramfs(pandemonium: Path, out: Path) -> None:
    subprocess.run([sys.executable, str(HERE / "build-guest.py"),
                    "--pandemonium", str(pandemonium), "--out", str(out)],
                   check=True, capture_output=True, text=True)


# User-facing label -> pand.sched cmdline value. pand-guest-init.c loads
# PANDEMONIUM for anything other than the literal string "none", which
# leaves the kernel's own in-kernel default (EEVDF) active as the control arm.
SCHED_ENV = {"pandemonium": "pandemonium", "eevdf": "none"}


def run_one(kernel: Path, initramfs: Path, out: Path, mode: str,
            duration: int, smp: int, nohz: bool, sched: str) -> dict:
    env = {"PAND_MODE": mode, "PAND_SCHED": SCHED_ENV[sched],
           "PAND_DURATION": str(duration), "QEMU_SMP": str(smp),
           "PAND_NOHZ": "on" if nohz else "off"}
    run_in_container(kernel, initramfs, out, env)
    ev = out / "events.bin"
    if not ev.is_file() or ev.stat().st_size < 1000:
        return {"clean": None, "worst_held": 0.0, "worst_dark": 0.0,
                "strands": 0, "note": "no trace"}
    r = subprocess.run([MONTAUK_ANALYZE, str(ev), "--report", "kstrand"],
                       capture_output=True, text=True)
    return parse_kstrand(r.stdout + r.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernels", nargs="+", type=Path,
                    # NOT "vmlinuz-*-cachyos" -- an -rc package's module dir
                    # (hence its vmlinuz filename here) ends "-cachyos-rc",
                    # not "-cachyos", and that glob silently drops it.
                    default=sorted(KERNELS.glob("vmlinuz-*")))
    ap.add_argument("--versions", nargs="+",
                    default=[f"{p.name.replace('pandemonium-', '')}={p}"
                             for p in sorted((CACHE / "versions")
                                             .glob("pandemonium-*"))],
                    help="label=binpath; default: every built version in "
                         "cache/versions")
    ap.add_argument("--modes", nargs="+", default=["dark"])
    ap.add_argument("--scheds", nargs="+", default=["pandemonium", "eevdf"],
                    choices=list(SCHED_ENV),
                    help="pandemonium and/or eevdf (the in-kernel default, "
                         "pand.sched=none) -- run both to tell a kernel bug "
                         "(both climb together) from a PANDEMONIUM-only one "
                         "(only pandemonium climbs)")
    ap.add_argument("--duration", type=int, default=45)
    ap.add_argument("--smp", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=1,
                    help="boots per cell -- the strand is stochastic (a race, "
                         "not deterministic per-config), so N=1 cannot "
                         "distinguish a real trend from run-to-run noise")
    ap.add_argument("--runs-dir", type=Path, default=CACHE / "runs")
    ap.add_argument("--no-nohz", action="store_true")
    args = ap.parse_args()

    if not args.kernels:
        sys.exit(f"no kernels found in {KERNELS} -- run extract-kernels.py "
                 "or pass --kernels")
    if not args.versions:
        sys.exit("no versions found in cache/versions -- run build-versions.py "
                 "or pass --versions")

    rows = []
    for vspec in args.versions:
        label, binpath = vspec.split("=", 1)
        binp = Path(binpath)
        if not binp.is_file():
            log(f"skip {label}: {binp} missing")
            continue
        initramfs = CACHE / "initramfs" / f"initramfs-{label}.cpio.gz"
        log(f"building initramfs for {label} ({binp})")
        build_initramfs(binp, initramfs)
        for kernel in args.kernels:
            kv = kernel.name.replace("vmlinuz-", "").replace("-cachyos", "")
            for sched in args.scheds:
                for mode in args.modes:
                    reps = []
                    for i in range(args.repeats):
                        tag = f"{label}_{kv}_{sched}_{mode}_r{i}"
                        log(f"run {tag}")
                        res = run_one(kernel, initramfs, args.runs_dir / tag,
                                      mode, args.duration, args.smp,
                                      not args.no_nohz, sched)
                        reps.append(res)
                    rows.append((label, kv, sched, mode, reps))

    print()
    print(f"{'version':<10} {'kernel':<15} {'sched':<11} {'mode':<6} "
          f"{'N':>3} {'hits':>5} {'med_HELD_ms':>12} {'max_HELD_ms':>12} "
          f"{'max_strands':>12}")
    for label, kv, sched, mode, reps in rows:
        held = [r["worst_held"] for r in reps]
        hits = sum(1 for r in reps if r.get("strands", 0) > 0)
        med = sub_stat(held, "quantile", "0.5") or 0.0
        worst = sub_stat(held, "max") or 0.0
        max_strands = max((r.get("strands", 0) for r in reps), default=0)
        print(f"{label:<10} {kv:<15} {sched:<11} {mode:<6} {len(reps):>3} "
              f"{hits:>5} {med:>12.1f} {worst:>12.1f} {max_strands:>12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
