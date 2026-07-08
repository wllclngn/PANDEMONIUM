#!/usr/bin/env python3
# orchestrate-io.py -- sweep (kernel x version) running the I/O flood on XFS with
# a Wayland compositor proxy, and quantify the kernel separation on TWO metrics:
#   kwin   -- the compositor's frame-overrun p99 (the kwin_wayland stall)
#   wake2run -- the flood's completion-wake -> dispatch p99 (montauk sched)
# with montauk_analyze's cross-run permutation test on each.
#
# Matches Piotr's environment: xfs, KDE-Plasma-6 Wayland, 16-thread Ryzen. The
# kernel bug: for a fixed version, a metric's tail climbs on 7.1.2 vs 7.0.12.
# Per-kernel initramfs because xfs.ko must match the running kernel's vermagic.

import argparse
import re
import subprocess
import sys
from pathlib import Path

from pand_docker import run_in_container

HERE = Path(__file__).parent.resolve()
CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
KERNELS = CACHE / "kernels"
VERSIONS = CACHE / "versions"
# Persistent, compact: one montauk_analyze-generated report per rep, plus any
# watchdog/lockup/RCU-stall console lines verbatim. Survives reboots.
REPORTS = CACHE / "reports"
# Ephemeral, bulky: the raw per-rep capture (events.bin, montauk-stream.bin,
# console.log, scratch/results disk images). Cleared by the OS at reboot --
# once run_cell() has distilled a rep into REPORTS, the raw capture is bloat,
# not an artifact worth manually curating or accidentally deleting.
RUNS_TMP = Path("/tmp") / "pandemonium-7_1-bug" / "runs"
MONTAUK_ANALYZE = "/usr/local/bin/montauk_analyze"
SUBLIMATION = "/usr/local/bin/sublimation"

# label -> path, built from the actual files present, never reconstructed by
# string-templating a label back into a filename: an -rc package's flavor
# suffix lands AFTER "-cachyos" (vmlinuz-7.2.0-rc1-4-cachyos-rc), so
# "{label}-cachyos" does not invert the label derivation below for it.
KERNEL_BY_LABEL = {p.name.replace("vmlinuz-", "").replace("-cachyos", ""): p
                   for p in sorted(KERNELS.glob("vmlinuz-*"))}


def resolve_kernel(kv: str) -> Path:
    """kv is the bare label (e.g. 7.1.2-3); resolve to the cached vmlinuz."""
    p = KERNEL_BY_LABEL.get(kv)
    if not p:
        sys.exit(f"kernel not found: {kv} (available: "
                 f"{', '.join(sorted(KERNEL_BY_LABEL)) or 'none cached'})")
    return p


def log(m):
    print(f"[io-matrix] {m}", flush=True)


def sub_quantile(path, q):
    r = subprocess.run([SUBLIMATION, "quantile", str(q)],
                       stdin=open(path), capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def quantile_vals(vals, q):
    if not vals:
        return None
    r = subprocess.run([SUBLIMATION, "quantile", str(q)],
                       input="\n".join(str(v) for v in vals),
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def build_initramfs(version, kv):
    # kv is the short label (7.0.12-1); xfs.ko lives under <kv>-cachyos.
    binp = VERSIONS / f"pandemonium-{version}"
    out = CACHE / "initramfs" / f"io-{version}-{kv}.cpio.gz"
    if out.is_file():  # reuse -- concurrent ablation runs must not rebuild-race
        return out
    subprocess.run([sys.executable, str(HERE / "build-guest.py"),
                    "--pandemonium", str(binp),
                    "--xfs-kver", f"{kv}-cachyos",
                    "--out", str(out)], check=True, capture_output=True,
                   text=True)
    return out


def distill_report(out, report_path, tag, issuers):
    # Persistent, compact distillation of one rep's raw /tmp capture: dispatch-
    # stall text (DARK%/HELD-by/worst), any watchdog/lockup/RCU-stall lines
    # from the console log verbatim, and the two headline metrics. This is
    # the artifact meant to survive; the raw capture in RUNS_TMP is not.
    lines = [f"tag={tag} issuers={issuers}"]
    ev = out / "events.bin"
    if ev.is_file() and ev.stat().st_size > 1000:
        r = subprocess.run([MONTAUK_ANALYZE, str(ev), "--report", "dispatch-stall"],
                           capture_output=True, text=True)
        lines.append(r.stdout.strip() or r.stderr.strip())
    console = out / "console.log"
    if console.is_file():
        # "rcu:" is a broad but safe catch-all: real kernel RCU stall/warning
        # lines are always tagged this way, but the exact wording varies
        # (e.g. "RCU CPU stall detected!" from sched_ext's own exit path vs
        # the kernel's own "rcu: INFO: rcu_preempt detected stalls..." --
        # a narrower literal match silently missed the second form once
        # already; don't repeat that.
        # sched_ext's own forced-disable path ("BPF scheduler ... disabled
        # (runnable task stall)" plus the companion "<task> failed to run for
        # N.NNNs") was ALSO silently missed by an earlier, narrower version of
        # this pattern -- a real, distinct, more severe finding than the rcu:
        # stall alone when both appear in the same log. Catch it explicitly.
        sig = subprocess.run(
            ["grep", "-iE",
             "watchdog failed|soft lockup|hard lockup|^\\[.*\\] rcu:|BUG:|"
             "sched_ext:.*disabled|failed to run for",
             str(console)], capture_output=True, text=True)
        if sig.stdout.strip():
            lines.append("kernel signature lines:")
            lines.append(sig.stdout.strip())
    report_path.write_text("\n".join(lines) + "\n")


def run_cell(kv, initramfs, out, args, issuers, report_path, tag):
    ev = out / "events.bin"
    kw = out / "kwin_overruns.txt"
    have = (ev.is_file() and ev.stat().st_size > 1000
            and kw.is_file() and kw.stat().st_size > 0)
    if not have:  # reuse an already-captured run (resumable + incremental n)
        env = {"PAND_WORKLOAD": "ioflood", "PAND_IOMODE": args.iomode,
               "PAND_FS": args.fs, "PAND_WAYLAND": str(args.wayland),
               "PAND_SCHED": args.sched, "PAND_IOPS": str(args.iops),
               "QEMU_SMP": str(args.smp), "QEMU_MEM": args.mem,
               "PAND_DURATION": str(args.duration),
               "BOOT_TIMEOUT": str(args.timeout),
               # Held at least at the original smp=16 intensity (16*8) rather
               # than left to scale with --smp, so lowering --smp to match the
               # host (avoid oversubscribing KVM) changes the topology under
               # test without also dropping workload pressure. Escalates
               # linearly per repeat (see --issuers-base/-step/-max), bounded
               # so the search finds a reproducing intensity without running
               # away exponentially.
               "PAND_ISSUERS": str(issuers),
               "PAND_NOHZ": "on" if args.nohz else "off",
               # Finding-2/Finding-4 targeted induction levers (off unless
               # explicitly requested): see guest/pand-idle-ping.c and
               # guest/pand-lock-block.c.
               "PAND_IDLEPING": str(int(args.idleping)),
               "PAND_PING_PAIRS": str(args.ping_pairs),
               "PAND_LOCKBLOCK": str(int(args.lockblock)),
               "PAND_LOCK_THREADS": str(args.lock_threads)}
        run_in_container(resolve_kernel(kv), initramfs, out, env)
    distill_report(out, report_path, tag, issuers)
    d = {}
    if ev.is_file() and ev.stat().st_size > 1000:
        r = subprocess.run([MONTAUK_ANALYZE, str(ev), "--report", "sched"],
                           capture_output=True, text=True)
        m = re.search(r"p99 (\d+)us p999 (\d+)us worst (\d+)us",
                      r.stdout + r.stderr)
        if m:
            d["wake2run"] = int(m.group(1))
    kw = out / "kwin_overruns.txt"
    if kw.is_file() and kw.stat().st_size:
        d["kwin"] = sub_quantile(kw, 0.99)
    return d


def cross_run(samples_by_kernel, metric, tag):
    d = CACHE / "runs" / "io-proms" / tag
    subprocess.run(["rm", "-rf", str(d)], capture_output=True)
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for kv, vals in samples_by_kernel.items():
        for v in vals:
            (d / f"{kv}-{n}.prom").write_text(
                f"# TYPE pand_{metric} gauge\n"
                f'pand_{metric}{{kernel="{kv}"}} {v}\n')
            n += 1
    r = subprocess.run([MONTAUK_ANALYZE, str(d), "--by", "kernel"],
                       capture_output=True, text=True)
    pop = Path.home() / ".cache" / "montauk" / "analysis-pop-kernel-all.prom"
    if pop.is_file():
        # Preserve the permutation result as its own artifact (the shared
        # filename is overwritten each metric otherwise).
        (d / "pop-analysis.prom").write_bytes(pop.read_bytes())
        for ln in pop.read_text().splitlines():
            if ("pop_perm_p" in ln or "pop_cliff" in ln) and metric in ln:
                mm = re.search(r'pair="([^"]+)"\}\s+([\d.]+)', ln)
                kind = "perm_p" if "perm_p" in ln else "cliff"
                if mm:
                    print(f"    {metric} {kind:7} {mm.group(1)}: {mm.group(2)}")
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernels", nargs="+", default=sorted(KERNEL_BY_LABEL))
    ap.add_argument("--versions", nargs="+", default=["5.16.0"])
    ap.add_argument("--iomode", default="all")
    ap.add_argument("--fs", default="xfs")
    ap.add_argument("--wayland", type=int, default=1)
    ap.add_argument("--sched", default="pandemonium")
    ap.add_argument("--iops", type=int, default=50)
    ap.add_argument("--smp", type=int, default=12)  # host is a 12-thread Ryzen 5 3600; don't oversubscribe KVM
    ap.add_argument("--mem", default="16G")
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--nohz", action=argparse.BooleanOptionalAction,
                    default=True)
    ap.add_argument("--timeout", type=int, default=200)
    ap.add_argument("--issuers-base", type=int, default=128)
    ap.add_argument("--issuers-step", type=int, default=32)
    ap.add_argument("--issuers-max", type=int, default=320)
    ap.add_argument("--idleping", action="store_true",
                    help="Finding-2 targeted induction (pand-idle-ping)")
    ap.add_argument("--ping-pairs", type=int, default=4)
    ap.add_argument("--lockblock", action="store_true",
                    help="Finding-4 targeted induction (pand-lock-block)")
    ap.add_argument("--lock-threads", type=int, default=8)
    args = ap.parse_args()

    if not args.kernels or not args.versions:
        sys.exit("no kernels or versions in cache")

    # config tag keeps each ablation arm's runs + proms distinct (no clobber).
    cfg = (f"{args.fs}-{args.sched}-{args.iomode}-io{args.iops}"
           f"-{'nohz' if args.nohz else 'tick'}-smp{args.smp}")
    REPORTS.mkdir(parents=True, exist_ok=True)

    # (version, kernel) -> {metric -> [vals]}
    results = {}
    for version in args.versions:
        for kv in args.kernels:
            log(f"building io initramfs {version} / {kv} (xfs.ko match)")
            initramfs = build_initramfs(version, kv)
            acc = {"kwin": [], "wake2run": []}
            for i in range(args.repeats):
                tag = f"{version}_{cfg}_{kv}_{i}"
                out = RUNS_TMP / "io" / tag  # raw capture: ephemeral, /tmp
                report_path = REPORTS / f"{tag}.txt"  # distilled: persistent
                issuers = min(args.issuers_base + args.issuers_step * i,
                              args.issuers_max)
                log(f"flood {version} {args.fs} wayland on {kv} "
                    f"(rep {i + 1}/{args.repeats}, issuers={issuers})")
                d = run_cell(kv, initramfs, out, args, issuers, report_path, tag)
                for k in ("kwin", "wake2run"):
                    if d.get(k) is not None:
                        acc[k].append(d[k])
                log(f"  -> kwin_p99={d.get('kwin')}us "
                    f"wake2run_p99={d.get('wake2run')}us")
            results[(version, kv)] = acc

    for metric in ("kwin", "wake2run"):
        print()
        print(f"metric: {metric} p99 (us)  (lower=better; bug=higher on 7.1.2)")
        for version in args.versions:
            cells = {kv: results[(version, kv)][metric] for kv in args.kernels}
            row = f"  {version} " + "  ".join(
                f"{kv}:{quantile_vals(cells[kv], 0.5) or 0:.0f}"
                for kv in args.kernels)
            print(row)
            if sum(len(v) >= 2 for v in cells.values()) >= 2:
                cross_run(cells, metric, f"{version}_{cfg}_{metric}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
