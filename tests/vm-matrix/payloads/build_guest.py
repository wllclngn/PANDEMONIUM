#!/usr/bin/env python3
# build_guest.py -- assemble one initramfs part. Runs INSIDE the builder image
# as the invoking user. Stdlib only; never imports rig/.
#
#   argv[1]   base | sched | kmod
#   /in       the part's inputs, staged by rig/guest.py   (ro)
#   /out      <part>.cpio.gz                              (rw)
#
#   base   /in/vmm-init.c, /in/workloads/*.c, /in/montauk, /in/veristat
#   sched  /in/sched.conf, /in/bin/<binary> (absent for kind none), /in/obj/*
#   kmod   /in/modules.order (staged names, dependencies first), /in/modules/*
#
# Shared libraries are resolved with ldd HERE: the bundled binaries run in the
# guest against what this image has, so it must share the host's distro
# lineage. The archive is written by this file rather than cpio(1): entries in
# path order, owned by root, mtime 0, so identical inputs give identical bytes.

import gzip
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

IN, OUT = Path("/in"), Path("/out")
BASE_DIRS = ("bin", "proc", "sys", "sys/fs/bpf", "dev", "tmp", "scratch", "etc/vmm",
             "usr/lib", "lib64")
CFLAGS = ("-O2", "-static", "-Wall", "-Wextra", "-pthread")
# The kernel's built-in initramfs normally provides it; carried here too so a
# kernel built without that default still hands PID 1 a console.
CONSOLE = ("dev/console", stat.S_IFCHR | 0o600, (5, 1))
# Libraries a bundled binary loads with dlopen, which ldd cannot see. montauk
# loads libbpf at runtime and runs with --trace disabled when it is absent; a
# guest whose scheduler happened to link libbpf masked that, a kind-none cell
# did not.
DLOPEN = {"montauk": ("libbpf.so.1",)}
SYSTEM_LIB = Path("/usr/lib")


def log(m: str) -> None:
    print(f"[build-guest] {m}", flush=True)


def compile_static(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gcc", *CFLAGS, "-o", str(dest), str(src)], check=True)
    dest.chmod(0o755)


def bundle(src: Path, root: Path, dest_rel: str, strip: bool,
           dlopen: tuple[str, ...] = ()) -> list[str]:
    """Copy src plus every shared library ldd resolves for it, the loader into
    lib64 and the rest into usr/lib, then each dlopen'd soname with its own
    dependencies. A library that cannot be found is fatal: the guest would fail
    at exec, or run degraded, with nothing on the console to say why."""
    dest = root / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(0o755)
    if strip:
        subprocess.run(["strip", "--strip-unneeded", str(dest)], check=True)
    libs = copy_deps(src, root)
    for soname in dlopen:
        lib = SYSTEM_LIB / soname
        if not lib.is_file():
            sys.exit(f"{src.name} loads {soname} at runtime; absent from the builder image")
        d = root / "usr/lib" / soname
        if not d.exists():
            shutil.copyfile(lib, d)
            d.chmod(0o755)
        libs += [f"usr/lib/{soname}", *copy_deps(lib, root)]
    return list(dict.fromkeys(libs))


def copy_deps(src: Path, root: Path) -> list[str]:
    r = subprocess.run(["ldd", str(src)], capture_output=True, text=True)
    text = r.stdout + r.stderr
    if "not a dynamic executable" in text or "statically linked" in text:
        return []
    if r.returncode != 0:
        sys.exit(f"ldd {src.name} failed (rc={r.returncode}):\n{text}")
    # A library missing outright is on stdout; a symbol version this image's
    # library lacks (a binary built against a newer glibc) is on stderr.
    missing = [ln.strip() for ln in text.splitlines() if "not found" in ln]
    if missing:
        sys.exit(f"{src.name}: libraries or symbol versions absent from the builder image:\n  "
                 + "\n  ".join(missing))
    libs = []
    for line in r.stdout.splitlines():
        line = line.strip()
        path = None
        if "=>" in line:
            rhs = line.split("=>", 1)[1].strip()
            if rhs.startswith("/"):
                path = rhs.split()[0]
        elif line.startswith("/"):
            path = line.split()[0]
        if not path or not Path(path).is_file():
            continue
        p = Path(path)
        d = (root / "lib64" / p.name if "ld-linux" in p.name or p.parent.name == "lib64"
             else root / "usr/lib" / p.name)
        d.parent.mkdir(parents=True, exist_ok=True)
        if not d.exists():
            shutil.copyfile(p, d)
            d.chmod(0o755)
        libs.append(str(d.relative_to(root)))
    return libs


def decompress(src: Path) -> bytes:
    data = src.read_bytes()
    if src.name.endswith(".zst"):
        try:
            from compression import zstd
            return zstd.decompress(data)
        except ImportError:
            return subprocess.run(["zstd", "-dcq"], input=data, capture_output=True,
                                  check=True).stdout
    if src.name.endswith(".xz"):
        return lzma.decompress(data)
    if src.name.endswith(".gz"):
        return gzip.decompress(data)
    return data


def build_base(root: Path) -> None:
    for d in BASE_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    compile_static(IN / "vmm-init.c", root / "init")
    srcs = sorted((IN / "workloads").glob("*.c"))
    for src in srcs:
        compile_static(src, root / "bin" / src.stem)
    log(f"compiled init and {len(srcs)} workloads (static)")
    for name in ("montauk", "veristat"):
        libs = bundle(IN / name, root, f"bin/{name}", strip=True, dlopen=DLOPEN.get(name, ()))
        log(f"{name} bundled + stripped ({(root / 'bin' / name).stat().st_size} bytes), "
            f"libs: {' '.join(Path(lib).name for lib in libs) or 'none'}")


def build_sched(root: Path) -> None:
    (root / "etc/vmm").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(IN / "sched.conf", root / "etc/vmm/sched.conf")
    (root / "etc/vmm/sched.conf").chmod(0o644)
    # The scheduler under test ships unstripped: its bytes are the ones built.
    for b in sorted((IN / "bin").glob("*")) if (IN / "bin").is_dir() else []:
        libs = bundle(b, root, f"bin/{b.name}", strip=False)
        log(f"{b.name} bundled, libs: {' '.join(Path(lib).name for lib in libs) or 'none'}")
    for o in sorted((IN / "obj").glob("*")) if (IN / "obj").is_dir() else []:
        dest = root / "obj" / o.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(o, dest)
        dest.chmod(0o644)
        log(f"object {o.name} ({dest.stat().st_size} bytes)")


def build_kmod(root: Path) -> None:
    moddir = root / "lib/modules/vmm"
    moddir.mkdir(parents=True, exist_ok=True)
    (root / "etc/vmm").mkdir(parents=True, exist_ok=True)
    order = []
    for name in (IN / "modules.order").read_text().split():
        data = decompress(IN / "modules" / name)
        if data[:4] != b"\x7fELF":
            sys.exit(f"{name}: not an ELF module after decompression")
        ko = name[:name.index(".ko") + 3]
        (moddir / ko).write_bytes(data)
        (moddir / ko).chmod(0o644)
        order.append(ko)
        log(f"module {ko} ({len(data)} bytes)")
    (root / "etc/vmm/modules.order").write_text("".join(f"{ko}\n" for ko in order))
    (root / "etc/vmm/modules.order").chmod(0o644)


def _pad(n: int) -> bytes:
    return b"\0" * (-n & 3)


def write_cpio(root: Path, dest: Path, nodes=()) -> int:
    """newc archive of root plus synthetic device nodes, gzip at level 9 with
    no name or timestamp in the gzip header. Returns the entry count."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            rel = (Path(dirpath) / n).relative_to(root)
            entries.append((rel.parts, str(rel), None))
    for name, mode, rdev in nodes:
        entries.append((Path(name).parts, name, (mode, rdev)))
    entries.sort(key=lambda e: e[0])
    tmp = dest.with_name(f".{dest.name}.tmp")
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                                               fileobj=raw, mtime=0) as gz:
        ino = 0

        def add(name: str, mode: int, data: bytes = b"", nlink: int = 1, rdev=(0, 0)):
            nonlocal ino
            ino += 1
            nb = name.encode() + b"\0"
            fields = (ino, mode, 0, 0, nlink, 0, len(data), 0, 0, rdev[0], rdev[1], len(nb), 0)
            hdr = b"070701" + b"".join(b"%08X" % v for v in fields)
            gz.write(hdr + nb + _pad(len(hdr) + len(nb)))
            gz.write(data + _pad(len(data)))

        for _, name, node in entries:
            if node:
                add(name, node[0], rdev=node[1])
                continue
            p = root / name
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                add(name, stat.S_IFDIR | 0o755, nlink=2)
            elif stat.S_ISLNK(st.st_mode):
                add(name, stat.S_IFLNK | 0o777, os.readlink(p).encode())
            elif stat.S_ISREG(st.st_mode):
                add(name, stat.S_IFREG | stat.S_IMODE(st.st_mode), p.read_bytes())
        add("TRAILER!!!", 0)
    tmp.replace(dest)
    return ino - 1


def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else ""
    build = {"base": build_base, "sched": build_sched, "kmod": build_kmod}.get(part)
    if build is None:
        sys.exit(f"usage: build_guest.py base|sched|kmod (got {part!r})")
    root = Path(tempfile.mkdtemp(prefix=f"vmm-{part}-"))
    try:
        build(root)
        out = OUT / f"{part}.cpio.gz"
        n = write_cpio(root, out, (CONSOLE,) if part == "base" else ())
        log(f"{part}: {n} entries, {out.stat().st_size} bytes")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
