# kernels.py -- every kernel a cell boots, by content.
#
# Two kinds. A git kernel is fetched at its pinned commit and built in the
# kbuild container with kernel.config merged over x86_64_defconfig and
# kvm_guest.config. An image kernel is a bzImage/vmlinuz someone already has,
# optionally with its modules directory so the guest can load the scratch-disk
# drivers.
#
# kernel.org refuses fetch-by-SHA for anything that is not a branch tip, so a
# pinned commit that has scrolled off is fetched from a mirror that serves
# reachable SHAs (GitHub does), then from the branch as a last resort.

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rig import docker
from rig.common import (BUILD, HARNESS, KERNELS, KREPO, LOGS, digest, log, read_json,
                        sha256_file, slug, write_json)
from rig.spec import Kernel

FRAGMENT = HARNESS / "kernel.config"
PAYLOAD = HARNESS / "payloads" / "build_kernel.py"


@dataclass(frozen=True)
class BuiltKernel:
    label: str
    image: Path
    release: str
    config: Path | None          # the resolved .config, when known
    modules: Path | None         # image kind: its modules directory
    identity: dict


def bzimage_release(p: Path) -> str:
    """The version string a bzImage carries in its setup header: the 16-bit
    kernel_version pointer at 0x20E is an offset from 0x200."""
    with open(p, "rb") as f:
        head = f.read(0x8000)
    if head[0x202:0x206] != b"HdrS":
        return ""
    off = struct.unpack_from("<H", head, 0x20E)[0] + 0x200
    raw = head[off:off + 256].split(b"\0", 1)[0].decode(errors="replace")
    return raw.split(" ", 1)[0]


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(KREPO), *args], capture_output=True,
                          text=True, check=check)


def _have(sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def ensure_commit(k: Kernel) -> None:
    if not KREPO.is_dir():
        KREPO.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(KREPO)], check=True)
    if _have(k.sha):
        return
    tried = []
    for url in (k.url, *k.mirrors):
        log(f"{k.label}: fetching {k.sha[:12]} from {url}")
        r = _git("fetch", "-q", "--depth=1", url, k.sha, check=False)
        if r.returncode == 0 and _have(k.sha):
            return
        tried.append(f"{url}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    if k.ref:
        for depth in (200, 2000):
            log(f"{k.label}: fetching {k.ref} at depth {depth} from {k.url}")
            r = _git("fetch", "-q", f"--depth={depth}", k.url, k.ref, check=False)
            if r.returncode == 0 and _have(k.sha):
                return
            tried.append(f"{k.url} {k.ref}@{depth}: not reachable")
    raise RuntimeError(f"{k.label}: commit {k.sha} not fetchable:\n  " + "\n  ".join(tried))


def _worktree(k: Kernel) -> Path:
    wt = BUILD / f"src-{k.sha[:12]}"
    if wt.is_dir():
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        if head.stdout.strip() == k.sha:
            return wt
        _git("worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    _git("worktree", "prune")
    _git("worktree", "add", "--detach", "--force", str(wt), k.sha)
    return wt


def _key(k: Kernel) -> str:
    return digest({"sha": k.sha, "cc": k.cc, "extra": "\n".join(k.config),
                   "fragment": sha256_file(FRAGMENT), "payload": sha256_file(PAYLOAD),
                   "dockerfile": sha256_file(docker.IMAGES["kbuild"][1])})


def _dir(k: Kernel) -> Path:
    return KERNELS / f"{slug(k.label)}-{k.sha[:12]}-{_key(k)}"


def cached(k: Kernel) -> BuiltKernel | None:
    if k.kind == "image":
        return ensure(k)
    d = _dir(k)
    ident = read_json(d / "identity.json")
    if (d / "bzImage").is_file() and ident.get("key") == _key(k):
        return BuiltKernel(k.label, d / "bzImage", ident["release"], d / "config",
                           None, ident)
    return None


def ensure(k: Kernel, keep_build: bool = False) -> BuiltKernel:
    if k.kind == "image":
        img = Path(k.image)
        mods = Path(k.modules) if k.modules else None
        ident = {"label": k.label, "kind": "image", "image": str(img),
                 "image_sha256": sha256_file(img), "release": bzimage_release(img),
                 "modules": str(mods) if mods else None}
        cfg = mods / "build" / ".config" if mods else None
        return BuiltKernel(k.label, img, ident["release"],
                           cfg if cfg and cfg.is_file() else None, mods, ident)

    hit = cached(k)
    if hit:
        return hit
    ensure_commit(k)
    src = _worktree(k)
    obj = BUILD / f"obj-{k.sha[:12]}"
    obj.mkdir(parents=True, exist_ok=True)
    d = _dir(k)
    stage = d.with_name(f".{d.name}.build")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    frag = stage / "kernel.config"
    frag.write_text(FRAGMENT.read_text() + "".join(f"{c}\n" for c in k.config))
    blog = LOGS / f"kbuild-{slug(k.label)}-{k.sha[:12]}.log"
    log(f"{k.label}: building {k.sha[:12]} ({k.cc}), log {blog}")
    r = docker.run("kbuild", ["python3", "/run/build_kernel.py"],
                   [(src, "/src", True), (obj, "/obj", False), (stage, "/out", False),
                    (frag, "/frag/kernel.config", True), (PAYLOAD, "/run/build_kernel.py", True)],
                   env={"KLOCALVERSION": f"-vmm-{k.sha[:12]}", "KCC": k.cc},
                   as_user=True, log_path=blog)
    if r.returncode != 0 or not (stage / "bzImage").is_file():
        raise RuntimeError(f"{k.label}: kernel build failed (rc={r.returncode}), see {blog}")
    release = (stage / "release").read_text().strip()
    ident = {"label": k.label, "kind": "git", "url": k.url, "sha": k.sha, "cc": k.cc,
             "config_extra": list(k.config), "key": _key(k), "release": release,
             "bzimage_sha256": sha256_file(stage / "bzImage")}
    write_json(stage / "identity.json", ident)
    shutil.rmtree(d, ignore_errors=True)
    stage.replace(d)
    if not keep_build:
        # Peak tmpfs is one kernel's objects at a time, not the whole matrix's.
        shutil.rmtree(obj, ignore_errors=True)
        _git("worktree", "remove", "--force", str(src), check=False)
    log(f"{k.label}: {release} -> {d / 'bzImage'}")
    return BuiltKernel(k.label, d / "bzImage", release, d / "config", None, ident)
