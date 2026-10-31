# docker.py -- every container this harness starts goes through here. qemu
# runs only inside the runner image, so a guest that hangs or panics is
# contained to a container the host can kill.

import hashlib
import subprocess
import tempfile
from pathlib import Path

from rig.common import HARNESS, SIBLING, ids

# name -> Dockerfile. The builder is the 7.1-bug suite's own image, under its
# own name and label scheme, so the guest tracer it builds is shared.
IMAGES = {
    "kbuild": ("vm-matrix-kbuild:latest", HARNESS / "Dockerfile.kbuild"),
    "runner": ("vm-matrix-runner:latest", HARNESS / "Dockerfile.runner"),
    "builder": ("pandemonium-builder:latest", SIBLING / "Dockerfile.builder"),
}


def _label(dockerfile: Path) -> str:
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]


def ensure_image(key: str) -> str:
    """Build the image when absent or when its Dockerfile changed. The label is
    the Dockerfile's content hash, the same scheme the 7.1-bug suite uses, so
    the two never rebuild each other's images. The build context is empty:
    no Dockerfile here COPYs anything."""
    name, dockerfile = IMAGES[key]
    want = _label(dockerfile)
    got = subprocess.run(["docker", "image", "inspect", "-f",
                          '{{ index .Config.Labels "dockerfile_sha" }}', name],
                         capture_output=True, text=True)
    if got.returncode == 0 and got.stdout.strip() == want:
        return name
    with tempfile.TemporaryDirectory() as ctx:
        subprocess.run(["docker", "build", "-f", str(dockerfile), "-t", name,
                        "--label", f"dockerfile_sha={want}", ctx], check=True)
    return name


def run(key: str, argv: list[str], mounts: list[tuple[Path, str, bool]],
        env: dict | None = None, as_user: bool = False, kvm: bool = False,
        timeout: int | None = None, log_path: Path | None = None
        ) -> subprocess.CompletedProcess:
    """mounts: (host path, container path, read_only). as_user runs as the
    invoking uid:gid so outputs are never root-owned; otherwise the payload
    receives HOST_UID/HOST_GID and hands ownership back itself."""
    image = ensure_image(key)
    for src, _, _ in mounts:
        # docker silently creates a directory for a missing source path, which
        # then fails deep inside the container as "Is a directory".
        if not Path(src).exists():
            raise FileNotFoundError(f"mount source missing: {src}")
    uid, gid = ids()
    cmd = ["docker", "run", "--rm"]
    if as_user:
        cmd += ["--user", f"{uid}:{gid}"]
    if kvm and Path("/dev/kvm").exists():
        cmd += ["--device", "/dev/kvm"]
    for src, dst, ro in mounts:
        cmd += ["-v", f"{src}:{dst}{':ro' if ro else ''}"]
    for k, v in {"HOST_UID": uid, "HOST_GID": gid, **(env or {})}.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, *argv]
    if log_path is None:
        return subprocess.run(cmd, timeout=timeout, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as lf:
        return subprocess.run(cmd, timeout=timeout, check=False,
                              stdout=lf, stderr=subprocess.STDOUT)
