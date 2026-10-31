# schedulers.py -- every scheduler a cell loads, by content.
#
# Three kinds. A cargo scheduler is built from source: either a pinned commit
# exported out of its repository with git archive, so the user's repo and
# working tree are never touched, or a directory built as it stands. A binary
# scheduler is an executable someone already has, beside the BPF objects
# veristat should check. none is the kernel's own scheduler.
#
# A cargo scheduler's BPF objects are not files on disk: libbpf-cargo compiles
# each one into a generated skeleton as a byte array. Every candidate the build
# produced is extracted and only those whose bytes appear verbatim inside the
# built binary are kept, so veristat checks exactly what the scheduler loads.

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rig.common import (BUILD, LOGS, REPOS, SCHEDULERS, digest, log, read_json, sha256_bytes,
                        sha256_file, slug, stamp, write_json)
from rig.spec import Scheduler

WORK = BUILD / "schedulers"
SELF = Path(__file__).resolve()
# What a kept cargo target was last built with; see _reuse_target.
TARGET_FACTS = ".vm-matrix.json"

# Environment that changes what cargo produces, keyed into a commit build.
KEY_ENV = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC", "CARGO_PROFILE_", "CARGO_BUILD_")
# Variables that would point git somewhere other than the repo around cwd.
GIT_REDIRECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY", "GIT_CEILING_DIRECTORIES")

EM_BPF = 247
# libbpf-cargo skeletons: `static DATA: [u8; N] = [...]` (0.23 on) or
# `const DATA: &[u8] = &[...]` (earlier).
DATA_RE = re.compile(rb"(?:static|const)\s+(\w+)\s*:\s*"
                     rb"(?:\[u8;\s*(\d+)\s*\]|&(?:'static\s+)?\[u8\])\s*=\s*&?\[")
NAME_RE = re.compile(rb'ObjectSkeletonConfigBuilder::new\(\s*&?(\w+)\s*\)[^"]*?\.name\("([^"]+)"\)')


@dataclass(frozen=True)
class BuiltScheduler:
    label: str
    kind: str                    # "cargo" | "binary" | "none"
    binary: Path | None          # host path of the executable (None for none)
    objects: tuple[Path, ...]    # BPF objects for veristat; for cargo, exactly the
                                 # bytes embedded in the binary
    args: tuple[str, ...]
    ops: str
    identity: dict


def _git_env() -> dict:
    # GIT_OPTIONAL_LOCKS=0 keeps status and diff from refreshing the index, so
    # reading the user's repo never writes to it.
    env = {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=_git_env())
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: "
                           f"{r.stderr.decode(errors='replace').strip()}")
    return r


def _is_bpf(head: bytes) -> bool:
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return False
    return int.from_bytes(head[18:20], "little" if head[5] == 1 else "big") == EM_BPF


def _check_pins(label: str, root: Path, pins) -> list[dict]:
    out, bad = [], []
    for rel, want in pins:
        p = root / rel
        got = sha256_file(p) if p.is_file() else None
        out.append({"path": rel, "sha256": want, "ok": got == want})
        if got != want:
            bad.append(f"{rel}: pinned {want}, found {got or 'no such file'}")
    if bad:
        raise RuntimeError(f"{label}: provenance pins do not match {root}:\n  " + "\n  ".join(bad))
    return out


def _toolchain(cwd: Path) -> dict:
    """rustc and cargo as resolved from the project directory (a rust-toolchain
    file there can select another), and the clang libbpf-cargo compiles BPF
    with."""
    out = {}
    for name, argv in (("rustc", ["rustc", "-V"]), ("cargo", ["cargo", "-V"]),
                       ("clang", ["clang", "--version"])):
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except FileNotFoundError:
            out[name] = "absent"
            continue
        lines = r.stdout.strip().splitlines()
        out[name] = lines[0] if r.returncode == 0 and lines else f"rc={r.returncode}"
    return out


def _target(name: str) -> Path:
    # One target directory per source tree, never shared. Cargo's metadata hash
    # for a root package does not depend on where the tree lives and its
    # freshness check compares mtimes, so two trees in one target directory
    # can each be handed the other's artifacts, build script output included.
    return WORK / f"target-{name}"


def _file_inputs(s: Scheduler) -> dict:
    """The declared host files a build script reads without telling cargo, by
    content; None for one that does not exist (yet)."""
    return {p: sha256_file(Path(p)) if Path(p).is_file() else None for p in s.inputs}


def _reuse_target(label: str, target: Path, facts: dict) -> None:
    """A kept target is reused only while what cargo cannot see still holds.
    libbpf-cargo declares no dependency on clang and a build script may read
    host files it never names, so either changing would leave the old BPF
    objects in place and embedded."""
    if target.is_dir() and read_json(target / TARGET_FACTS) != facts:
        log(f"{label}: {target} was built with another clang or other inputs, "
            f"starting it fresh", "WARN")
        shutil.rmtree(target)


def _cargo_env(target: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT}
    env.update(CARGO_TARGET_DIR=str(target), GIT_OPTIONAL_LOCKS="0", **extra)
    return env


def _cargo(s: Scheduler, cwd: Path, env: dict, locked: bool) -> tuple[list[dict], Path]:
    argv = ["cargo", "build", "--release", "--message-format=json-render-diagnostics",
            "--bin", s.binary]
    if s.package:
        argv += ["-p", s.package]
    if locked:
        argv.append("--locked")
    blog = LOGS / f"cargo-{slug(s.label)}.log"
    blog.parent.mkdir(parents=True, exist_ok=True)
    log(f"{s.label}: cargo build in {cwd}, log {blog}")
    with open(blog, "ab") as lf:
        start = lf.tell()
        lf.write(f"[{time.strftime('%H:%M:%S')}] [INFO]   {' '.join(argv)} (cwd {cwd})\n".encode())
        lf.flush()
        r = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=lf)
    if r.returncode != 0:
        with open(blog, "rb") as lf:
            lf.seek(start)
            errs = [l for l in lf.read().decode(errors="replace").splitlines()
                    if l.startswith("error")][:4]
        hint = "; --locked was in effect and Cargo.lock was not rewritten" if locked else ""
        raise RuntimeError(f"{s.label}: cargo build failed rc={r.returncode}{hint}, see {blog}"
                           + "".join(f"\n  {e}" for e in errs))
    return [json.loads(l) for l in r.stdout.splitlines() if l.startswith(b"{")], blog


def _candidates(out_dir: Path) -> list[tuple[str, bytes, Path]]:
    """Every BPF object a build script left: byte arrays in generated skeletons
    and BPF ELF files written out directly."""
    found = []
    for q in sorted(out_dir.rglob("*")):
        if q.is_symlink() or not q.is_file():
            continue
        if q.suffix == ".rs":
            text = q.read_bytes()
            if b"[u8" not in text:
                continue
            names = {m.group(1): m.group(2).decode() for m in NAME_RE.finditer(text)}
            for m in DATA_RE.finditer(text):
                end = text.find(b"]", m.end())
                try:
                    data = bytes(int(x) for x in text[m.end():end].split(b",") if x.strip())
                except ValueError:
                    continue
                if (m.group(2) and len(data) != int(m.group(2))) or not _is_bpf(data[:20]):
                    continue
                stem = names.get(m.group(1), q.name.split(".")[0].removesuffix("_skel"))
                found.append((f"{stem.removesuffix('_bpf')}.bpf.o", data, q))
        else:
            with open(q, "rb") as f:
                if not _is_bpf(f.read(20)):
                    continue
            name = q.name if q.name.endswith(".bpf.o") else f"{q.stem}.bpf.o"
            found.append((name, q.read_bytes(), q))
    return found


def _collect(s: Scheduler, msgs: list[dict], env: dict) -> tuple[Path, bytes, list, dict]:
    """From cargo's own messages: the binary, the embedded objects and the
    build-script facts. Only this build's out dirs are searched, so a stale
    skeleton from an earlier configuration can never be picked up."""
    exe = [m for m in msgs if m.get("reason") == "compiler-artifact" and m.get("executable")
           and m["target"]["name"] == s.binary]
    if not exe:
        raise RuntimeError(f"{s.label}: cargo produced no binary named {s.binary!r}")
    binary, pkg = Path(exe[-1]["executable"]), exe[-1]["package_id"]
    blob = binary.read_bytes()
    scripts = [m for m in msgs if m.get("reason") == "build-script-executed"]

    cands, seen, names = [], set(), set()
    for m in scripts:
        out = Path(m["out_dir"])
        for name, data, src in (_candidates(out) if out.is_dir() else ()):
            h = sha256_bytes(data)
            if h in seen:
                continue
            seen.add(h)
            base, n = name.removesuffix(".bpf.o"), 2
            while name in names:
                name, n = f"{base}-{n}.bpf.o", n + 1
            names.add(name)
            cands.append((name, data, src))
    if not cands:
        raise RuntimeError(f"{s.label}: no BPF object in any build script output of this build")
    kept = [c for c in cands if c[1] in blob]
    if not kept:
        raise RuntimeError(f"{s.label}: {len(cands)} BPF object(s) found in build outputs, "
                           f"none embedded verbatim in {binary}")

    own = next((m for m in scripts if m["package_id"] == pkg), None)
    benv = dict(own["env"]) if own else {}
    watched = set()
    for m in scripts:
        f = Path(m["out_dir"]).parent / "output"
        for line in (f.read_text(errors="replace").splitlines() if f.is_file() else ()):
            for pre in ("cargo::rerun-if-env-changed=", "cargo:rerun-if-env-changed="):
                if line.startswith(pre):
                    watched.add(line[len(pre):].strip())
    info = {"package_id": pkg, "build_env": benv,
            "build_id": next((v for k, v in benv.items() if "BUILD_ID" in k), None),
            # Variables the build scripts declared they read, as this build saw
            # them; a cached build is reused only while they still hold.
            "env_inputs": {n: env.get(n) for n in sorted(watched)},
            "candidates": len(cands)}
    return binary, blob, kept, info


def _store(s: Scheduler, d: Path, binary: Path, kept: list, ident: dict,
           extra: tuple[Path, ...] = ()) -> BuiltScheduler:
    stage = d.with_name(f".{d.name}.build")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(binary, stage / s.binary)
    for name, data, _ in kept:
        (stage / name).write_bytes(data)
    for p in extra:
        shutil.copy2(p, stage / p.name)
    write_json(stage / "identity.json", ident)
    shutil.rmtree(d, ignore_errors=True)
    stage.replace(d)
    names = ", ".join(f"{o['name']} ({o['size']} bytes)" for o in ident["objects"])
    log(f"{s.label}: {ident['provenance']} -> {d} [{names}]")
    return BuiltScheduler(s.label, "cargo", d / s.binary,
                          tuple(d / o["name"] for o in ident["objects"]), s.args, s.ops, ident)


def _objects(kept: list, target: Path) -> list[dict]:
    return [{"name": name, "size": len(data), "sha256": sha256_bytes(data),
             "source": str(src.relative_to(target)) if src.is_relative_to(target) else str(src)}
            for name, data, src in kept]


def _cached(s: Scheduler, d: Path, key: str, env: dict) -> BuiltScheduler | None:
    ident = read_json(d / "identity.json")
    if ident.get("key") != key:
        return None
    files = [(d / s.binary, ident.get("binary_sha256"))]
    files += [(d / o["name"], o["sha256"]) for o in ident.get("objects", [])]
    if any(not p.is_file() or sha256_file(p) != h for p, h in files):
        log(f"{s.label}: cached build in {d} does not match its identity, rebuilding", "WARN")
        return None
    changed = [n for n, v in ident.get("env_inputs", {}).items() if env.get(n) != v]
    changed += [p for p, h in _file_inputs(s).items() if ident.get("file_inputs", {}).get(p) != h]
    if changed:
        log(f"{s.label}: build inputs changed ({', '.join(changed)}), rebuilding", "WARN")
        return None
    log(f"{s.label}: cached {d}")
    return BuiltScheduler(s.label, "cargo", d / s.binary,
                          tuple(d / o["name"] for o in ident["objects"]), s.args, s.ops, ident)


def _norm(path: str) -> str:
    p = Path(path or ".").as_posix().strip("/")
    return "" if p == "." else p


def _cargo_root(repo: Path, commit: str, path: str) -> str:
    """The directory cargo treats as the workspace root at that commit: path
    itself or the nearest ancestor whose Cargo.toml has [workspace]. A
    workspace member only builds when the export carries the whole workspace."""
    parts = path.split("/") if path else []
    for n in range(len(parts), -1, -1):
        d = "/".join(parts[:n])
        r = _git(repo, "show", f"{commit}:{d + '/' if d else ''}Cargo.toml", check=False)
        if r.returncode != 0:
            continue
        try:
            if "workspace" in tomllib.loads(r.stdout.decode(errors="replace")):
                return d
        except tomllib.TOMLDecodeError:
            continue
    return path


def _has(repo: Path, commit: str) -> bool:
    return repo.is_dir() and _git(repo, "cat-file", "-e", f"{commit}^{{commit}}",
                                  check=False).returncode == 0


def _source_repo(s: Scheduler) -> Path:
    """The repository holding the commit: the ticket's repo when it has it,
    else a bare clone of url under CACHE, fetched by SHA from url and then the
    mirrors, then from ref as a last resort. The user's repo is never written."""
    if s.repo and _has(Path(s.repo), s.commit):
        return Path(s.repo)
    if not s.url:
        raise RuntimeError(f"{s.label}: commit {s.commit} not in {s.repo}; "
                           f"name a url to fetch it from")
    name = s.url.rstrip("/").rpartition("/")[2].removesuffix(".git")
    clone = REPOS / f"{slug(name)}-{digest({'url': s.url})}"
    if not clone.is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(clone.parent, "init", "-q", "--bare", clone.name)
    if _has(clone, s.commit):
        return clone
    tried = []
    fetches = [(u, s.commit, 1) for u in (s.url, *s.mirrors)]
    fetches += [(s.url, s.ref, depth) for depth in (200, 2000) if s.ref]
    for url, what, depth in fetches:
        log(f"{s.label}: fetching {what[:12] if what == s.commit else what} "
            f"at depth {depth} from {url} into {clone}")
        r = _git(clone, "fetch", "-q", f"--depth={depth}", url, what, check=False)
        if r.returncode == 0 and _has(clone, s.commit):
            # A ref keeps a later gc from pruning a commit fetched by SHA alone.
            _git(clone, "update-ref", f"refs/vm-matrix/{s.commit}", s.commit)
            return clone
        err = r.stderr.decode(errors="replace").strip().splitlines()
        tried.append(f"{url} {what[:12]}@{depth}: {err[-1] if err else 'commit not reachable'}")
    raise RuntimeError(f"{s.label}: commit {s.commit} not fetchable:\n  " + "\n  ".join(tried))


def _export(s: Scheduler, repo: Path, root: str) -> tuple[Path, str, int]:
    """git archive the workspace root at the commit into a fresh directory, then
    check every blob against the commit's tree, so no attribute (export-ignore,
    export-subst, a filter) can make the exported tree differ from it."""
    treeish = f"{s.commit}:{root}"
    tree = _git(repo, "rev-parse", treeish).stdout.decode().strip()
    dest = WORK / f"src-{slug(s.label)}-{s.commit[:12]}"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", treeish],
                             stdout=subprocess.PIPE, stderr=err, env=_git_env())
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                tf.extractall(dest, filter="data")
        finally:
            p.stdout.close()
            rc = p.wait()
        err.seek(0)
        if rc != 0:
            raise RuntimeError(f"{s.label}: git archive {treeish} failed: "
                               f"{err.read().decode(errors='replace').strip()}")

    want = {}
    for ent in _git(repo, "ls-tree", "-r", "-z", tree).stdout.split(b"\0"):
        if not ent:
            continue
        meta, _, name = ent.partition(b"\t")
        mode, kind, oid = meta.split()
        if kind == b"blob":
            want[os.fsdecode(name)] = (mode == b"120000", oid.decode())
    have = {str(q.relative_to(dest)) for q in dest.rglob("*") if q.is_symlink() or q.is_file()}
    bad = sorted(have ^ want.keys())
    for rel, (link, oid) in want.items():
        if rel not in have:
            continue
        q = dest / rel
        data = os.fsencode(os.readlink(q)) if link else q.read_bytes()
        h = hashlib.new("sha1" if len(oid) == 40 else "sha256", b"blob %d\0" % len(data))
        h.update(data)
        if h.hexdigest() != oid:
            bad.append(rel)
    if bad:
        raise RuntimeError(f"{s.label}: exported tree differs from {treeish} at: "
                           + ", ".join(bad[:8]))

    # A tree-ish archive stamps every entry with the current time. The commit
    # time instead makes a re-export identical, so cargo stays incremental.
    when = int(_git(repo, "show", "-s", "--format=%ct", s.commit).stdout)
    for q in (*dest.rglob("*"), dest):
        os.utime(q, (when, when), follow_symlinks=False)
    return dest, tree, len(want)


def _commit(s: Scheduler, keep_build: bool) -> BuiltScheduler:
    path = _norm(s.path)
    repo = _source_repo(s)
    root = _cargo_root(repo, s.commit, path)
    dest, tree, nfiles = _export(s, repo, root)
    proj = dest / path[len(root):].lstrip("/") if root else dest / path
    target = _target(dest.name.removeprefix("src-"))
    try:
        pins = _check_pins(s.label, proj, s.sha256)
        tc = _toolchain(proj)
        # No .git in the export and none reachable above it: a build script
        # that asks git for a build id gets the answer an exported tree earns.
        env = _cargo_env(target, GIT_CEILING_DIRECTORIES=str(dest.parent))
        key = digest({"commit": s.commit, "path": path, "package": s.package,
                      "binary": s.binary, "pins": json.dumps(s.sha256), "harness": sha256_file(SELF),
                      "env": json.dumps({k: v for k, v in sorted(os.environ.items())
                                         if k.startswith(KEY_ENV)}),
                      **tc})
        d = SCHEDULERS / f"{slug(s.label)}-{key}"
        hit = _cached(s, d, key, env)
        if hit:
            return hit

        _reuse_target(s.label, target, {"toolchain": tc, "inputs": _file_inputs(s)})
        lock = dest / "Cargo.lock"
        in_tree = lock.is_file()
        if not in_tree:
            log(f"{s.label}: {s.commit[:12]} carries no Cargo.lock; dependencies resolve "
                f"at build time and the lock cargo writes is recorded", "WARN")
        msgs, blog = _cargo(s, proj, env, locked=in_tree)
        binary, blob, kept, info = _collect(s, msgs, env)
        inputs = _file_inputs(s)
        write_json(target / TARGET_FACTS, {"toolchain": tc, "inputs": inputs})
        origin = str(repo) if s.repo and repo == Path(s.repo) else s.url
        ident = {"label": s.label, "kind": "cargo", "source": "commit", "key": key,
                 "provenance": (f"{origin} at {s.commit[:12]}, {path or '.'}"
                                + (f", {len(pins)} pin(s) ok" if pins else "")),
                 "repo": str(repo), "url": s.url, "commit": s.commit, "path": path,
                 "workspace_root": root, "tree": tree, "tree_files": nfiles, "pins": pins,
                 "package": s.package, "binary": s.binary, "binary_size": len(blob),
                 "binary_sha256": sha256_bytes(blob), "objects": _objects(kept, target),
                 "toolchain": tc, "file_inputs": inputs,
                 "cargo_lock": {"in_tree": in_tree, "locked": in_tree,
                                "sha256": sha256_file(lock) if lock.is_file() else None},
                 "log": str(blog), "built": stamp(), **info}
        # The lock this build resolved is kept beside it: without one in the
        # tree it is the only record of which dependency versions went in.
        return _store(s, d, binary, kept, ident, (lock,) if lock.is_file() else ())
    finally:
        if not keep_build:
            # The result is cached by key; peak tmpfs is one commit's build.
            shutil.rmtree(dest, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)


def _tree_state(root: Path, top: Path | None) -> dict:
    """What a build must leave as it found it: porcelain status and the diff
    against HEAD under root, or every file's size and mtime outside git."""
    if top is None:
        return {"files": sorted((str(q.relative_to(root)), st.st_size, st.st_mtime_ns)
                                for q in root.rglob("*") for st in (q.lstat(),))}
    status = _git(root, "status", "--porcelain", "--", ".").stdout
    diff = _git(root, "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color",
                "--", ".").stdout
    return {"status": status.decode(errors="replace").splitlines(),
            "diff_sha256": sha256_bytes(diff)}


def _worktree(s: Scheduler) -> BuiltScheduler:
    proj = (Path(s.worktree) / _norm(s.path)).resolve()
    if not (proj / "Cargo.toml").is_file():
        raise FileNotFoundError(f"{s.label}: no Cargo.toml in {proj}")
    loc = subprocess.run(["cargo", "locate-project", "--workspace", "--message-format", "plain"],
                         cwd=proj, capture_output=True, text=True)
    if loc.returncode != 0:
        raise RuntimeError(f"{s.label}: cargo locate-project in {proj}: {loc.stderr.strip()}")
    root = Path(loc.stdout.strip()).parent
    lock = root / "Cargo.lock"
    if not lock.is_file():
        raise RuntimeError(f"{s.label}: {root} has no Cargo.lock and cargo would write one into "
                           f"the tree; add a lock there or build the scheduler from a commit")
    pins = _check_pins(s.label, proj, s.sha256)
    r = _git(proj, "rev-parse", "--show-toplevel", check=False)
    top = Path(os.fsdecode(r.stdout.strip())) if r.returncode == 0 else None
    tc = _toolchain(proj)
    # Keyed by the tree's path and kept, so the next build of it is incremental.
    target = _target(f"{slug(proj.name)}-{digest({'dir': str(proj)})}")
    env = _cargo_env(target)
    lock_sha = sha256_file(lock)
    _reuse_target(s.label, target, {"toolchain": tc, "inputs": _file_inputs(s)})

    before = _tree_state(root, top)
    msgs, blog = _cargo(s, proj, env, locked=True)
    after = _tree_state(root, top)
    if after != before or sha256_file(lock) != lock_sha:
        gone = set(before.get("status", ())) - set(after.get("status", ()))
        new = set(after.get("status", ())) - set(before.get("status", ()))
        raise RuntimeError(f"{s.label}: the build changed {root}"
                           + "".join(f"\n  - {x}" for x in sorted(gone))
                           + "".join(f"\n  + {x}" for x in sorted(new)))

    binary, blob, kept, info = _collect(s, msgs, env)
    inputs = _file_inputs(s)
    write_json(target / TARGET_FACTS, {"toolchain": tc, "inputs": inputs})
    bsha = sha256_bytes(blob)
    ident = {"label": s.label, "kind": "cargo", "source": "worktree", "key": bsha[:16],
             "dir": str(proj), "workspace_root": str(root), "pins": pins, "package": s.package,
             "binary": s.binary, "binary_size": len(blob), "binary_sha256": bsha,
             "objects": _objects(kept, target), "toolchain": tc, "file_inputs": inputs,
             "cargo_lock": {"in_tree": True, "locked": True, "sha256": lock_sha},
             "tree_unchanged": True, "log": str(blog), "built": stamp(), **info}
    if top is None:
        ident.update(repo=None, head=None, dirty=None, diff_sha256=None,
                     provenance=f"worktree {proj} (not in git)")
    else:
        head = _git(proj, "rev-parse", "HEAD").stdout.decode().strip()
        dirty = bool(before["status"])
        ident.update(repo=str(top), head=head, dirty=dirty, status=before["status"],
                     diff_sha256=before["diff_sha256"],
                     provenance=(f"worktree {proj} on {head[:12]}"
                                 + (f", dirty, diff {before['diff_sha256'][:12]}" if dirty
                                    else ", clean")))
    return _store(s, SCHEDULERS / f"{slug(s.label)}-{bsha[:16]}", binary, kept, ident)


def _binary(s: Scheduler) -> BuiltScheduler:
    b = Path(s.binary)
    if not b.is_file():
        raise FileNotFoundError(f"{s.label}: binary {b} not found")
    pins = _check_pins(s.label, b.parent, s.sha256)
    objs = []
    for o in map(Path, s.objects):
        if not o.is_file():
            raise FileNotFoundError(f"{s.label}: object {o} not found")
        with open(o, "rb") as f:
            if not _is_bpf(f.read(20)):
                raise RuntimeError(f"{s.label}: {o} is not a BPF ELF object")
        objs.append({"name": o.name, "path": str(o), "size": o.stat().st_size,
                     "sha256": sha256_file(o)})
    bsha = sha256_file(b)
    ident = {"label": s.label, "kind": "binary", "source": "binary",
             "provenance": f"binary {b} {bsha[:12]}", "binary": str(b),
             "binary_size": b.stat().st_size, "binary_sha256": bsha, "objects": objs,
             "pins": pins}
    return BuiltScheduler(s.label, "binary", b, tuple(Path(o["path"]) for o in objs),
                          s.args, s.ops, ident)


def ensure(s: Scheduler, keep_build: bool = False) -> BuiltScheduler:
    if s.kind == "none":
        return BuiltScheduler(s.label, "none", None, (), s.args, s.ops,
                              {"label": s.label, "kind": "none", "source": "none",
                               "provenance": "none: the kernel's own scheduler"})
    if s.kind == "binary":
        return _binary(s)
    if s.kind == "cargo":
        return _worktree(s) if s.worktree else _commit(s, keep_build)
    raise ValueError(f"{s.label}: unknown scheduler kind {s.kind!r}")
