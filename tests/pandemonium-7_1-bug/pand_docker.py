#!/usr/bin/env python3
# pand_docker.py -- shared docker-run assembly for every 7.1-bug orchestrator.
# One place builds the `docker run` invocation that replaces a bare-host
# qemu launch, so orchestrate.py / orchestrate-io.py / orchestrate-defconfig.py
# never construct it independently. qemu executes ONLY inside this
# container -- a guest freeze is contained to the container, never the host.

import os
import subprocess
from pathlib import Path

HERE = Path(__file__).parent.resolve()
IMAGE = "pandemonium-runner:latest"


def run_in_container(kernel: Path, initramfs: Path, out: Path, env: dict,
                     timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run run-in-container.py inside the pandemonium-runner image. `env`
    supplies the PAND_*/QEMU_*/BOOT_TIMEOUT/NO_KVM keys run-in-container.py
    reads; kernel/initramfs/out are bind-mounted at the fixed paths it
    expects there."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm"]
    if env.get("NO_KVM") != "1" and Path("/dev/kvm").exists():
        cmd += ["--device", "/dev/kvm"]
    cmd += ["-v", f"{kernel}:/run/kernel:ro",
            "-v", f"{initramfs}:/run/initramfs.cpio.gz:ro",
            "-v", f"{out}:/run/out",
            "-v", f"{HERE / 'run-in-container.py'}:/run/run-in-container.py:ro",
            # The container runs as root (qemu -accel kvm and mkfs on a
            # fresh file both want it); this lets it chown its own output
            # back to the invoking user before exiting instead of leaving
            # root-owned files the host user can no longer write next to.
            "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "python3", "/run/run-in-container.py"]
    return subprocess.run(cmd, timeout=timeout, check=False)


def run_defconfig_in_container(kernel: Path, rootfs: Path, pandemonium_bin: Path,
                               out: Path, env: dict,
                               timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run run-defconfig-in-container.py inside the pandemonium-runner image.
    rootfs is bind-mounted read-write: the container's own debugfs -w swaps
    the requested pandemonium binary in before boot (root=/dev/vda has
    snapshot=on, so the guest's own writes during the build never persist
    back to it -- only this pre-boot swap touches the file)."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm"]
    if env.get("NO_KVM") != "1" and Path("/dev/kvm").exists():
        cmd += ["--device", "/dev/kvm"]
    cmd += ["-v", f"{kernel}:/run/kernel:ro",
            "-v", f"{rootfs}:/run/rootfs.img",
            "-v", f"{out}:/run/out",
            "-v", f"{HERE / 'run-defconfig-in-container.py'}:"
                  "/run/run-defconfig-in-container.py:ro",
            "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}"]
    if env.get("PAND_SCHED") != "none":
        cmd += ["-v", f"{pandemonium_bin}:/run/pandemonium-bin:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "python3", "/run/run-defconfig-in-container.py"]
    return subprocess.run(cmd, timeout=timeout, check=False)
