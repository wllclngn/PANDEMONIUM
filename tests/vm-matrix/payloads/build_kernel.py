#!/usr/bin/env python3
# build_kernel.py -- configure and build one kernel. Runs INSIDE the kbuild
# image as the invoking user. Stdlib only; never imports rig/.
#
#   /src                   kernel worktree at the pinned commit   (ro)
#   /obj                   out-of-tree build directory            (rw)
#   /out                   bzImage, config, release, config-check (rw)
#   /frag/kernel.config    the harness fragment plus spec extras  (ro)
#
# Env: KLOCALVERSION (appended to the release), KCC (gcc|clang), JOBS.

import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC, OBJ, OUT = Path("/src"), Path("/obj"), Path("/out")
FRAG = Path("/frag/kernel.config")

# A kernel missing any of these cannot boot this harness's guest or load a
# sched_ext scheduler, so the build stops rather than producing one.
REQUIRED = ("CONFIG_SCHED_CLASS_EXT", "CONFIG_DEBUG_INFO_BTF", "CONFIG_BPF_SYSCALL",
            "CONFIG_BPF_JIT", "CONFIG_BLK_DEV_INITRD", "CONFIG_RD_GZIP",
            "CONFIG_DEVTMPFS", "CONFIG_SERIAL_8250", "CONFIG_SERIAL_8250_CONSOLE",
            "CONFIG_VIRTIO_PCI", "CONFIG_VIRTIO_BLK", "CONFIG_EXT4_FS")


def make(*args: str) -> None:
    cc = ["LLVM=1"] if os.environ.get("KCC") == "clang" else []
    jobs = os.environ.get("JOBS") or str(os.cpu_count() or 1)
    subprocess.run(["make", "-C", str(SRC), f"O={OBJ}", f"-j{jobs}", *cc, *args],
                   check=True)


def parse(text: str) -> dict:
    """CONFIG_X -> value; '# CONFIG_X is not set' -> 'n'."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            out[line[2:-len(" is not set")]] = "n"
    return out


def main() -> int:
    make("x86_64_defconfig")
    make("kvm_guest.config")
    subprocess.run([str(SRC / "scripts" / "kconfig" / "merge_config.sh"), "-m",
                    "-O", str(OBJ), str(OBJ / ".config"), str(FRAG)],
                   check=True, cwd=OBJ)
    subprocess.run([str(SRC / "scripts" / "config"), "--file", str(OBJ / ".config"),
                    "--set-str", "LOCALVERSION", os.environ.get("KLOCALVERSION", ""),
                    "--disable", "LOCALVERSION_AUTO"], check=True)
    make("olddefconfig")

    want = parse(FRAG.read_text())
    got = parse((OBJ / ".config").read_text())
    lines, fatal = [], []
    for k, v in sorted(want.items()):
        g = got.get(k, "absent")
        if g == v:
            continue
        # An option the fragment names but this kernel's Kconfig no longer has
        # (CONFIG_SCHED_DEBUG went unconditional in 6.15) resolves "absent";
        # that is only fatal for an option the guest cannot do without.
        lines.append(f"{k}: wanted {v}, resolved {g}")
        if k in REQUIRED:
            fatal.append(k)
    for k in REQUIRED:
        if got.get(k) != "y" and k not in fatal:
            fatal.append(k)
            lines.append(f"{k}: required =y, resolved {got.get(k, 'absent')}")
    (OUT / "config-check.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    for line in lines:
        print(f"[kbuild] config: {line}", flush=True)
    if fatal:
        print(f"[kbuild] required options unresolved: {', '.join(fatal)}", flush=True)
        return 2

    make("bzImage")
    shutil.copy2(OBJ / "arch" / "x86" / "boot" / "bzImage", OUT / "bzImage")
    shutil.copy2(OBJ / ".config", OUT / "config")
    shutil.copy2(OBJ / "include" / "config" / "kernel.release", OUT / "release")
    print(f"[kbuild] built {(OUT / 'release').read_text().strip()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
