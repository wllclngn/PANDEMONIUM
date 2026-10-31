# guest.py -- the guest initramfs, in three cached parts.
#
# base carries what every cell boots: the static PID 1, the workloads, the
# guest tracer and veristat. sched carries one scheduler: its binary, its BPF
# objects and sched.conf. kmod carries the scratch-disk drivers an image kernel
# ships as modules. Each part is keyed by a digest over every input whose
# content lands in it (sources, binaries, payload, builder Dockerfile and
# image), never by name, so a changed input rebuilds exactly the parts it
# reaches. A cell's initramfs is the concatenation; the kernel unpacks each
# gzip cpio member in turn, later entries overwriting earlier ones.

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rig import docker
from rig.common import (BUILD, CACHE, HARNESS, IMAGES, LOGS, SIBLING, digest, log,
                        read_json, sha256_bytes, sha256_file, slug, stamp, write_json)
from rig.kernels import BuiltKernel

if TYPE_CHECKING:
    from rig.schedulers import BuiltScheduler

PAYLOAD = HARNESS / "payloads" / "build_guest.py"
INIT_SRC = HARNESS / "guest" / "vmm-init.c"
WORKLOADS = (SIBLING / "guest" / "pand-strand-load.c",
             SIBLING / "guest" / "pand-io-flood.c",
             SIBLING / "guest" / "pand-idle-ping.c",
             SIBLING / "guest" / "pand-lock-block.c",
             HARNESS / "guest" / "pand-affinity-churn.c")

TOOLS = CACHE / "tools"
VERISTAT_URL = "https://github.com/libbpf/veristat"
VERISTAT_TAG = "v0.5.1"
# What the tag resolved to when pinned; a moved tag is refused, not followed.
VERISTAT_COMMIT = "ca3717b165522860a01e01ea67dd0e99c56e0faf"
# glibc 2.43 made strchr and friends const-preserving and libbpf builds with
# -Werror, so the discarded-qualifiers warnings it now raises must stay warnings.
VERISTAT_CFLAGS = "-Wno-error=discarded-qualifiers"

# What vmm-init needs before it can mount /dev/vda as ext4.
SCRATCH_MODULES = ("virtio_pci", "virtio_blk", "ext4")


def _builder() -> dict:
    """The builder image's recipe and the exact image built from it: libraries
    are resolved inside it, so both are inputs of every part."""
    name = docker.ensure_image("builder")
    r = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", name],
                       capture_output=True, text=True, check=True)
    return {"dockerfile": sha256_file(docker.IMAGES["builder"][1]),
            "builder_image": r.stdout.strip()}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _veristat_src() -> Path:
    src = BUILD / f"veristat-{VERISTAT_TAG}"
    if (src / ".git").exists() and (src / "libbpf" / "src" / "Makefile").is_file():
        if _git(src, "rev-parse", "HEAD") == VERISTAT_COMMIT:
            return src
    shutil.rmtree(src, ignore_errors=True)
    tmp = src.with_name(f".{src.name}.clone")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    log(f"veristat: cloning {VERISTAT_URL} {VERISTAT_TAG}")
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", VERISTAT_TAG,
                    "--recurse-submodules", "--shallow-submodules", VERISTAT_URL, str(tmp)],
                   check=True)
    head = _git(tmp, "rev-parse", "HEAD")
    if head != VERISTAT_COMMIT:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"veristat {VERISTAT_TAG} now resolves to {head}, "
                           f"pinned {VERISTAT_COMMIT}")
    tmp.replace(src)
    return src


def ensure_veristat() -> Path:
    """libbpf/veristat at the pinned tag, built in the builder image against the
    libbpf submodule it pins. Cached under CACHE/tools with an identity sidecar;
    reuse needs the recorded key and the binary's own hash to still match."""
    out = TOOLS / "veristat"
    side = TOOLS / "veristat.json"
    want = {"url": VERISTAT_URL, "tag": VERISTAT_TAG, "commit": VERISTAT_COMMIT,
            "extra_cflags": VERISTAT_CFLAGS, **_builder()}
    key = digest(want)
    ident = read_json(side)
    if out.is_file() and ident.get("key") == key and ident.get("sha256") == sha256_file(out):
        return out
    src = _veristat_src()
    shutil.rmtree(src / "src" / ".output", ignore_errors=True)
    (src / "src" / "veristat").unlink(missing_ok=True)
    blog = LOGS / f"veristat-{VERISTAT_TAG}.log"
    log(f"veristat: building {VERISTAT_TAG} ({VERISTAT_COMMIT[:12]}), log {blog}")
    r = docker.run("builder", ["make", "-C", "/src/src", f"-j{os.cpu_count() or 1}",
                               f"EXTRA_CFLAGS={VERISTAT_CFLAGS}"],
                   [(src, "/src", False)], as_user=True, log_path=blog)
    built = src / "src" / "veristat"
    if r.returncode != 0 or not built.is_file():
        raise RuntimeError(f"veristat build failed (rc={r.returncode}), see {blog}")
    TOOLS.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    shutil.copy2(built, tmp)
    tmp.replace(out)
    write_json(side, {**want, "key": key, "libbpf": _git(src / "libbpf", "rev-parse", "HEAD"),
                      "sha256": sha256_file(out), "built": stamp()})
    log(f"veristat: {out}")
    return out


def _guest_montauk() -> Path:
    """The guest tracer from the 7.1-bug suite's own builder: compiled in the
    builder image with GPU support out, cached by its source digest."""
    if str(SIBLING) not in sys.path:
        sys.path.insert(0, str(SIBLING))
    from lib.guest import ensure_guest_montauk
    return ensure_guest_montauk()


def _sidecar(img: Path) -> Path:
    return img.with_name(img.name.removesuffix(".cpio.gz") + ".json")


def _build_part(part: str, out: Path, files: dict[str, Path], texts: dict[str, str],
                inputs: dict, extra: dict | None = None) -> Path:
    """Stage the inputs, run the payload over them and install the archive. The
    staged copies are re-hashed, so an input that changed after it was digested
    fails the build instead of landing under the wrong key."""
    key = digest(inputs)
    side = _sidecar(out)
    if out.is_file() and read_json(side).get("digest") == key:
        return out
    stage = BUILD / "guest" / f"{part}-{key}"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "in").mkdir(parents=True)
    (stage / "out").mkdir()
    for rel, src in files.items():
        dst = stage / "in" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if sha256_file(dst) != inputs[rel]:
            raise RuntimeError(f"{part}: {src} changed while the image was being built")
    for rel, text in texts.items():
        (stage / "in" / rel).write_text(text)
    blog = LOGS / f"guest-{part}-{key}.log"
    log(f"guest {part}: building {out.name}, log {blog}")
    r = docker.run("builder", ["python3", "/run/build_guest.py", part],
                   [(stage / "in", "/in", True), (stage / "out", "/out", False),
                    (PAYLOAD, "/run/build_guest.py", True)],
                   as_user=True, log_path=blog)
    built = stage / "out" / f"{part}.cpio.gz"
    if r.returncode != 0 or not built.is_file():
        raise RuntimeError(f"guest {part} build failed (rc={r.returncode}), see {blog}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    shutil.copyfile(built, tmp)
    tmp.replace(out)
    write_json(side, {"part": part, "digest": key, "inputs": inputs, **(extra or {}),
                      "sha256": sha256_file(out), "bytes": out.stat().st_size,
                      "built": stamp()})
    shutil.rmtree(stage, ignore_errors=True)
    log(f"guest {part}: {out} ({out.stat().st_size} bytes)")
    return out


def base_image() -> Path:
    montauk = _guest_montauk()
    veristat = ensure_veristat()
    files = {"vmm-init.c": INIT_SRC, "montauk": montauk, "veristat": veristat}
    files.update({f"workloads/{p.name}": p for p in WORKLOADS})
    inputs = {rel: sha256_file(p) for rel, p in files.items()}
    inputs.update(part="base", payload=sha256_file(PAYLOAD), **_builder())
    prov = {"provenance": {
        "montauk": str(montauk),
        "montauk_src": read_json(montauk.with_suffix(".src.json")).get("src_digest"),
        "veristat": str(veristat), "veristat_commit": VERISTAT_COMMIT}}
    return _build_part("base", IMAGES / f"base-{digest(inputs)}.cpio.gz", files, {},
                       inputs, prov)


def sched_conf(b: "BuiltScheduler", binary: str | None, objects: list[str]) -> str:
    """/etc/vmm/sched.conf, one key=value per line, order preserved."""
    lines = [f"kind={b.kind}", f"label={b.label}"]
    if binary:
        lines.append(f"binary=/bin/{binary}")
    lines += [f"arg={a}" for a in b.args]
    lines.append(f"ops={b.ops}")
    lines += [f"object=/obj/{o}" for o in objects]
    for ln in lines:
        if "\n" in ln or "\r" in ln:
            raise ValueError(f"scheduler {b.label}: sched.conf value carries a newline: {ln!r}")
    return "\n".join(lines) + "\n"


def scheduler_image(b: "BuiltScheduler") -> Path:
    files: dict[str, Path] = {}
    binary, objects = None, []
    if b.kind != "none":
        if b.binary is None:
            raise ValueError(f"scheduler {b.label}: kind {b.kind} without a binary")
        binary = slug(Path(b.binary).name)
        files[f"bin/{binary}"] = Path(b.binary)
        for i, o in enumerate(b.objects):
            name = slug(Path(o).name)
            if name in objects:
                name = f"{i}-{name}"
            objects.append(name)
            files[f"obj/{name}"] = Path(o)
    conf = sched_conf(b, binary, objects)
    inputs = {rel: sha256_file(p) for rel, p in files.items()}
    inputs.update({"part": "sched", "sched.conf": sha256_bytes(conf.encode()),
                   "payload": sha256_file(PAYLOAD), **_builder()})
    out = IMAGES / f"sched-{slug(b.label)}-{digest(inputs)}.cpio.gz"
    return _build_part("sched", out, files, {"sched.conf": conf}, inputs,
                       {"label": b.label, "sched.conf": conf})


def _modname(path: str) -> str:
    name = Path(path.strip()).name
    return name[:name.index(".ko")].replace("-", "_") if ".ko" in name else name


def _lines(p: Path) -> list[str]:
    return p.read_text(errors="replace").splitlines() if p.is_file() else []


def module_plan(moddir: Path) -> tuple[dict[str, str], list[Path]]:
    """Each scratch module's state in this modules dir (builtin, module or
    missing) and the files to load for them, every dependency first. Built-ins
    come from modules.builtin, loadable ones and their dependencies from
    modules.dep; the order is a depth-first walk, so it holds whatever order
    depmod listed the dependencies in.

    A module's softdep "pre:" entries load ahead of it as well: the guest has
    no modprobe, so nothing else would. ext4 and jbd2 on older kernels name
    crc32c that way, which a metadata_csum filesystem cannot mount without.
    An entry is a module name or an exact alias; one that resolves to a
    built-in or to nothing in this modules dir is skipped, as modprobe would."""
    builtin = {_modname(ln) for ln in _lines(moddir / "modules.builtin") if ln.strip()}
    deps: dict[str, tuple[str, list[str]]] = {}
    for ln in _lines(moddir / "modules.dep"):
        mod, sep, rest = ln.partition(":")
        if sep:
            deps[_modname(mod)] = (mod.strip(), rest.split())
    soft: dict[str, list[str]] = {}
    for ln in _lines(moddir / "modules.softdep"):
        words = ln.split()
        if len(words) < 3 or words[0] != "softdep":
            continue
        pre, cur = soft.setdefault(_modname(words[1]), []), None
        for w in words[2:]:
            if w in ("pre:", "post:"):
                cur = w
            elif cur == "pre:":
                pre.append(w)
    aliases: dict[str, list[str]] = {}
    for ln in _lines(moddir / "modules.alias"):
        words = ln.split()
        if len(words) == 3 and words[0] == "alias":
            aliases.setdefault(words[1], []).append(_modname(words[2]))

    def resolve(entry: str) -> str | None:
        for name in (_modname(entry).replace("-", "_"), *aliases.get(entry, ())):
            if name in builtin:
                return None
            if name in deps:
                return name
        return None

    order: list[str] = []
    placed: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in placed:
            return
        if name in chain:
            raise RuntimeError(f"{moddir}: dependency cycle {' -> '.join(chain + (name,))}")
        if name not in deps:
            raise RuntimeError(f"{moddir}: {chain[-1]} needs {name}, absent from modules.dep")
        rel, ds = deps[name]
        for entry in soft.get(name, ()):
            pre = resolve(entry)
            if pre and pre not in chain and pre != name:
                visit(pre, chain + (name,))
        for d in ds:
            visit(_modname(d), chain + (name,))
        placed.add(name)
        order.append(rel)

    states = {}
    for m in SCRATCH_MODULES:
        if m in builtin:
            states[m] = "builtin"
        elif m in deps:
            visit(m, ())
            states[m] = "module"
        else:
            states[m] = "missing"
    return states, [moddir / rel for rel in order]


def kernel_image(k: BuiltKernel) -> Path | None:
    if k.modules is None:
        return None
    moddir = Path(k.modules)
    if k.release and k.release != moddir.name:
        log(f"{k.label}: image release {k.release} but modules dir {moddir.name}; "
            f"its modules will not load", "WARN")
    states, files = module_plan(moddir)
    summary = " ".join(f"{m}={s}" for m, s in states.items())
    for m, s in states.items():
        if s == "missing":
            log(f"{k.label}: {m} is neither built in nor a module under {moddir}", "WARN")
    if not files:
        log(f"{k.label}: {summary}; no modules to load")
        return None
    log(f"{k.label}: {summary}; loading {' '.join(p.name for p in files)}")
    staged = {f"modules/{p.name}": p for p in files}
    order = "".join(f"{p.name}\n" for p in files)
    inputs = {rel: sha256_file(p) for rel, p in staged.items()}
    inputs.update({"part": "kmod", "modules.order": sha256_bytes(order.encode()),
                   "payload": sha256_file(PAYLOAD), **_builder()})
    out = IMAGES / f"kmod-{slug(k.label)}-{digest(inputs)}.cpio.gz"
    return _build_part("kmod", out, staged, {"modules.order": order}, inputs,
                       {"label": k.label, "modules_dir": str(moddir), "plan": states,
                        "order": [p.name for p in files]})


def initramfs(k: BuiltKernel, b: "BuiltScheduler") -> tuple[Path, dict]:
    parts = [("base", base_image()), ("sched", scheduler_image(b))]
    km = kernel_image(k)
    if km is not None:
        parts.append(("kmod", km))
    ident_parts: dict = {"kmod": None}
    for name, p in parts:
        side = read_json(_sidecar(p))
        ident_parts[name] = {"file": p.name, "digest": side.get("digest"),
                             "sha256": side.get("sha256"), "inputs": side.get("inputs", {})}
    key = digest({name: ident_parts[name]["digest"] for name, _ in parts})
    out = IMAGES / f"initrd-{key}.cpio.gz"
    side = _sidecar(out)
    if not (out.is_file() and read_json(side).get("digest") == key):
        tmp = out.with_name(f".{out.name}.tmp")
        with open(tmp, "wb") as f:
            for _, p in parts:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, f)
        tmp.replace(out)
        write_json(side, {"digest": key, "parts": [p.name for _, p in parts],
                          "sha256": sha256_file(out), "built": stamp()})
    ident = {"digest": key, "file": out.name, "sha256": sha256_file(out),
             "bytes": out.stat().st_size, "parts": ident_parts}
    return out, ident
