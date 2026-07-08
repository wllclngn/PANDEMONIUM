#!/usr/bin/env python3
# orchestrate-defconfig.py -- sweep (kernel x PANDEMONIUM version) running a real
# `make vmlinux` defconfig build under qemu; collect build_seconds per cell and
# quantify with sublimation.
#
# The kernel bug: for a FIXED version, build_seconds jumps on a 7.1.x kernel and
# stays low on 7.0.x (the charted defconfig blowup). A version-only effect that
# is equal across kernels is not the kernel bug. Runs backgrounded -- each
# vmlinux build is ~2min, so a full 3x5 pass is ~30min.

import argparse
import subprocess
import sys
from pathlib import Path

from pand_docker import run_defconfig_in_container

HERE = Path(__file__).parent.resolve()
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
KERNELS = CACHE / "kernels"
VERSIONS = CACHE / "versions"
ROOTFS = CACHE / "defconfig-rootfs.img"
SUBLIMATION = "/usr/local/bin/sublimation"

# label -> path, built from the actual files present, never reconstructed by
# string-templating a label back into a filename: an -rc package's flavor
# suffix lands AFTER "-cachyos" (vmlinuz-7.2.0-rc1-4-cachyos-rc), so
# "{label}-cachyos" does not invert the label derivation below for it.
KERNEL_BY_LABEL = {p.name.replace("vmlinuz-", "").replace("-cachyos", ""): p
                   for p in sorted(KERNELS.glob("vmlinuz-*"))}


def log(m):
    print(f"[defconfig-matrix] {m}", flush=True)


def resolve_kernel(kv: str) -> Path:
    p = KERNEL_BY_LABEL.get(kv)
    if not p:
        sys.exit(f"kernel not found: {kv} (available: "
                 f"{', '.join(sorted(KERNEL_BY_LABEL)) or 'none cached'})")
    return p


SCHED_ENV = {"pandemonium": "pandemonium", "eevdf": "none"}


def run_one(kv, version, sched, out, args):
    env = {"PAND_SCHED": SCHED_ENV[sched],
           "PAND_JOBS": str(args.jobs), "PAND_TARGET": args.target,
           "PAND_WINDOW": str(args.window), "QEMU_SMP": str(args.smp),
           "BOOT_TIMEOUT": str(args.timeout),
           "PAND_NOHZ": "on" if args.nohz else "off"}
    if args.iops:
        env["PAND_IOPS"] = str(args.iops)
    pandbin = VERSIONS / f"pandemonium-{version}"
    run_defconfig_in_container(resolve_kernel(kv), ROOTFS, pandbin, out, env)
    metric = out / ("object_count" if args.window else "build_seconds")
    try:
        return float(metric.read_text().strip())
    except (OSError, ValueError):
        return None


def sub_reduce(samples, *cmd_args):
    if not samples:
        return None
    p = subprocess.run([SUBLIMATION, *cmd_args], input="\n".join(
        f"{s}" for s in samples), capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def quantile(samples, q):
    return sub_reduce(samples, "quantile", str(q))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernels", nargs="+", default=sorted(KERNEL_BY_LABEL))
    ap.add_argument("--versions", nargs="+",
                    default=sorted(p.name.replace("pandemonium-", "")
                                   for p in VERSIONS.glob("pandemonium-*")))
    ap.add_argument("--target", default="vmlinux")
    ap.add_argument("--window", type=int, default=0,
                    help="throughput mode: build N seconds + count objects "
                         "(higher=faster; the kernel bug shows as fewer on 7.1.x)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--smp", type=int, default=8)
    ap.add_argument("--iops", type=int, default=0)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--nohz", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--scheds", nargs="+", choices=list(SCHED_ENV),
                    default=list(SCHED_ENV),
                    help="nonpartisan by default: both PANDEMONIUM and the "
                         "kernel's own EEVDF (pand.sched=none)")
    ap.add_argument("--runs-dir", type=Path, default=CACHE / "runs" / "defconfig")
    args = ap.parse_args()

    if not args.kernels or not args.versions:
        sys.exit("no kernels or versions in cache -- extract/build them first")

    results = {}
    for version in args.versions:
        for kv in args.kernels:
            for sched in args.scheds:
                samples = []
                for i in range(args.repeats):
                    out = args.runs_dir / f"{version}_{kv}_{sched}_{i}"
                    log(f"build {version}/{sched} on {kv} "
                        f"(rep {i + 1}/{args.repeats})")
                    s = run_one(kv, version, sched, out, args)
                    if s is not None:
                        samples.append(s)
                    log(f"  -> {s if s is not None else 'FAILED'}s")
                results[(version, kv, sched)] = samples

    print()
    metric_name = f"objects/{args.window}s" if args.window else "build_seconds"
    print(f"metric: {metric_name}"
          + ("  (higher=faster; bug=fewer on 7.1.x)" if args.window
             else "  (lower=faster; bug=higher on 7.1.x)"))
    for sched in args.scheds:
        print(f"\nscheduler: {sched}")
        hdr = f"{'version':<10} " + " ".join(f"{kv:>12}" for kv in args.kernels)
        if len(args.kernels) >= 2:
            hdr += f"  {'ratio(hi/lo)':>13}"
        print(hdr)
        for version in args.versions:
            cells = []
            for kv in args.kernels:
                s = results.get((version, kv, sched), [])
                cells.append(sub_reduce(s, "mean") or 0.0)
            row = f"{version:<10} " + " ".join(f"{c:>12.1f}" for c in cells)
            nz = [c for c in cells if c > 0]
            if len(nz) >= 2:
                row += f"  {max(nz) / min(nz):>13.2f}"
            print(row)

    if args.repeats > 1:
        print()
        print("per-cell median (sublimation quantile 0.5):")
        for version in args.versions:
            for kv in args.kernels:
                for sched in args.scheds:
                    s = results.get((version, kv, sched), [])
                    med = quantile(s, 0.5)
                    if med is not None:
                        print(f"  {version:<8} {kv:<12} {sched:<11} "
                              f"median={med:.1f}s  n={len(s)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
