# spec.py -- a ticket: what to boot, what to load on it and what to expect.
#
# A ticket is a TOML file under tickets/, or one assembled from the command
# line. Every cell of the matrix is one (kernel, scheduler) pair. The effective
# spec is written into each run directory, so a result always carries the
# exact inputs that produced it.

import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from rig.common import slug

# Guest phases in run order. veristat and attach need a scheduler with BPF; the
# load phases run under whatever scheduler the cell names, including none.
PHASES = ("veristat", "attach", "strand", "ioflood", "idleping", "lockblock",
          "affinity", "mixed", "detach")
LOAD_PHASES = ("strand", "ioflood", "idleping", "lockblock", "affinity", "mixed")

SHA40 = re.compile(r"[0-9a-f]{40}")
# qemu's -m syntax, as vmrun reads it.
MEM = re.compile(r"[0-9]+[KMGT]?(i?B)?", re.IGNORECASE)
# The guest init's own bound on vmm.duration.
MAX_DURATION = 86400
# The guest prints a console heartbeat every 30 s while it waits on a child, so
# a stall window shorter than two heartbeats would reap a healthy guest.
MIN_STALL = 60
# REPO@COMMIT[:SUBDIR] on the command line; REPO may be a URL.
COMMIT_FORM = re.compile(r"(.+)@([0-9A-Fa-f]{7,64})(?::(.*))?")
REMOTE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|[^/@]+@[^/:]+:")
# --scheduler options per kind: (single-valued, repeatable).
SCHED_OPTIONS = {
    "none": ({"ops", "note"}, {"arg"}),
    "cargo": ({"bin", "pkg", "ops", "note", "ref"}, {"arg", "sha256", "mirror", "input"}),
    "binary": ({"ops", "note"}, {"obj", "arg", "sha256"}),
}


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Kernel:
    label: str
    kind: str                       # "git" | "image"
    url: str = ""                   # git: canonical repository
    sha: str = ""                   # git: full 40-hex commit
    ref: str = ""                   # git: branch carrying sha (shallow fallback)
    mirrors: tuple[str, ...] = ()   # git: extra remotes that serve fetch-by-SHA
    config: tuple[str, ...] = ()    # git: extra CONFIG_ lines over kernel.config
    cc: str = "gcc"                 # git: "gcc" | "clang" (LLVM=1)
    image: str = ""                 # image: path to a bzImage / vmlinuz
    modules: str = ""               # image: its /usr/lib/modules/<release>
    note: str = ""


@dataclass(frozen=True)
class Scheduler:
    label: str
    kind: str                       # "cargo" | "binary" | "none"
    repo: str = ""                  # cargo: local git repo holding the commit
    url: str = ""                   # cargo: remote fetched from when repo lacks it
    mirrors: tuple[str, ...] = ()   # cargo: extra remotes that serve fetch-by-SHA
    ref: str = ""                   # cargo: branch carrying commit (shallow fallback)
    commit: str = ""                # cargo: full commit exported and built
    worktree: str = ""              # cargo: build this directory as it stands
    path: str = "."                 # cargo: project dir relative to repo root
    package: str = ""               # cargo: cargo build -p PACKAGE
    binary: str = ""                # cargo: built binary name; binary: its path
    objects: tuple[str, ...] = ()   # binary: BPF objects for veristat
    args: tuple[str, ...] = ()      # argv after the binary inside the guest
    ops: str = ""                   # expected root/ops name; "" accepts any
    sha256: tuple[tuple[str, str], ...] = ()  # (relpath, sha256) provenance pins
    inputs: tuple[str, ...] = ()    # cargo: host files the build reads undeclared
    note: str = ""


@dataclass(frozen=True)
class Expect:
    kernels: tuple[str, ...] = ("*",)
    schedulers: tuple[str, ...] = ("*",)
    veristat: str = ""              # "success" | "failure"
    fail_progs: tuple[str, ...] = ()
    error: str = ""                 # substring the verifier log must carry
    attach: str = ""                # "ok" | "fail"
    phases: str = ""                # "ok"
    detach: str = ""                # "ok"

    def covers(self, kernel: str, scheduler: str) -> bool:
        return (("*" in self.kernels or kernel in self.kernels) and
                ("*" in self.schedulers or scheduler in self.schedulers))


@dataclass(frozen=True)
class Run:
    smp: int = 8
    threads: int = 2                # SMT threads per guest core
    numa: int = 1                   # guest NUMA nodes
    mem: str = "4G"
    duration: int = 20              # seconds per load phase
    reps: int = 1
    phases: tuple[str, ...] = PHASES
    timeout: int = 1800             # wall seconds for one cell, boot included
    stall_s: int = 180              # no output on any channel for this long; 0 off
    pin_vcpus: bool = False
    kvm: bool = True
    # The guest montauk also streams to ttyS2, so a capture survives a hang.
    # Off by default: montauk writes that sink from the thread that drains its
    # event ring, so the port's rate caps what --trace-out keeps as well.
    stream: bool = False


@dataclass(frozen=True)
class Ticket:
    id: str
    url: str = ""
    title: str = ""
    kernels: tuple[Kernel, ...] = ()
    schedulers: tuple[Scheduler, ...] = ()
    expects: tuple[Expect, ...] = ()
    run: Run = field(default_factory=Run)
    source: str = "cli"

    @property
    def slug(self) -> str:
        return slug(self.id)

    def cells(self) -> list[tuple[Kernel, Scheduler]]:
        return [(k, s) for k in self.kernels for s in self.schedulers]

    def expectation(self, kernel: str, scheduler: str) -> Expect:
        """Every [[expect]] covering the cell, later blocks overriding earlier
        ones field by field. Empty fields assert nothing."""
        merged = Expect(kernels=(kernel,), schedulers=(scheduler,))
        for e in self.expects:
            if not e.covers(kernel, scheduler):
                continue
            for f in fields(Expect):
                if f.name in ("kernels", "schedulers"):
                    continue
                v = getattr(e, f.name)
                if v:
                    merged = replace(merged, **{f.name: v})
        return merged


def _tuple(v) -> tuple:
    if v is None:
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return (v,)


def _expand(p: str) -> str:
    return str(Path(p).expanduser()) if p else p


def _build(cls, raw: dict, where: str):
    known = {f.name for f in fields(cls)}
    extra = set(raw) - known
    if extra:
        raise SpecError(f"{where}: unknown keys {sorted(extra)}")
    vals = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        v = raw[f.name]
        if f.name == "sha256":
            v = tuple(sorted(v.items()))
        elif str(f.type).startswith("tuple"):
            v = _tuple(v)
        vals[f.name] = v
    return cls(**vals)


def _kernel(raw: dict) -> Kernel:
    k = _build(Kernel, raw, f"kernel {raw.get('label', '?')}")
    k = replace(k, image=_expand(k.image), modules=_expand(k.modules))
    return k


def _scheduler(raw: dict) -> Scheduler:
    s = _build(Scheduler, raw, f"scheduler {raw.get('label', '?')}")
    return replace(s, repo=_expand(s.repo), worktree=_expand(s.worktree),
                   binary=_expand(s.binary) if s.kind == "binary" else s.binary,
                   objects=tuple(_expand(o) for o in s.objects),
                   inputs=tuple(_expand(p) for p in s.inputs))


def _validate_run(t: Ticket) -> None:
    r = t.run
    for f in fields(Run):
        v, default = getattr(r, f.name), getattr(Run(), f.name)
        if type(v) is not type(default):
            raise SpecError(f"run.{f.name} = {v!r}: expected a {type(default).__name__}")
    if not r.phases:
        raise SpecError(f"run.phases is empty; name at least one of {', '.join(PHASES)}")
    bad = set(r.phases) - set(PHASES)
    if bad:
        raise SpecError(f"unknown phases {sorted(bad)}; known: {', '.join(PHASES)}")
    gated = [p for p in r.phases if p in LOAD_PHASES or p == "detach"]
    if gated and "attach" not in r.phases and any(s.kind != "none" for s in t.schedulers):
        raise SpecError(f"phases {', '.join(gated)} run only under an attached scheduler: "
                        f"Add attach, or a cargo or binary scheduler skips every one of them")
    if not 1 <= r.duration <= MAX_DURATION:
        raise SpecError(f"run.duration {r.duration} is outside 1..{MAX_DURATION} seconds")
    for name in ("smp", "threads", "numa", "reps", "timeout"):
        if getattr(r, name) < 1:
            raise SpecError(f"run.{name} must be at least 1")
    if r.smp % (r.numa * r.threads):
        raise SpecError(f"run.smp {r.smp} is not a multiple of numa*threads = {r.numa * r.threads}")
    if r.stall_s and r.stall_s < MIN_STALL:
        raise SpecError(f"run.stall_s is 0 (off) or at least {MIN_STALL} seconds")
    if not MEM.fullmatch(r.mem):
        raise SpecError(f"run.mem {r.mem!r} is not a size such as 4G or 4096M")


def validate(t: Ticket) -> Ticket:
    if not t.id:
        raise SpecError("ticket.id is required")
    for group, items in (("kernel", t.kernels), ("scheduler", t.schedulers)):
        labels = [i.label for i in items]
        dup = {x for x in labels if labels.count(x) > 1}
        if dup:
            raise SpecError(f"duplicate {group} labels: {sorted(dup)}")
        if not items:
            raise SpecError(f"no {group}s in the matrix")
        # Run directories are named <slug(kernel)>__<slug(scheduler)>.
        named: dict[str, str] = {}
        for x in labels:
            if not x:
                raise SpecError(f"a {group} has no label")
            if "__" in slug(x):
                raise SpecError(f"{group} label {x!r}: '__' separates the kernel from the "
                                f"scheduler in run directory names")
            other = named.setdefault(slug(x), x)
            if other != x:
                raise SpecError(f"{group} labels {other!r} and {x!r} share the run "
                                f"directory name {slug(x)!r}")
    for k in t.kernels:
        if k.kind == "git":
            if not SHA40.fullmatch(k.sha):
                raise SpecError(f"kernel {k.label}: sha must be the full 40-hex commit")
            if not k.url:
                raise SpecError(f"kernel {k.label}: git kernels need url")
            if k.cc not in ("gcc", "clang"):
                raise SpecError(f"kernel {k.label}: cc is gcc or clang")
        elif k.kind == "image":
            if not k.image or not Path(k.image).is_file():
                raise SpecError(f"kernel {k.label}: image {k.image!r} not found")
            if k.modules and not Path(k.modules).is_dir():
                raise SpecError(f"kernel {k.label}: modules {k.modules!r} not a directory")
        else:
            raise SpecError(f"kernel {k.label}: kind is git or image")
    for s in t.schedulers:
        if s.inputs and s.kind != "cargo":
            raise SpecError(f"scheduler {s.label}: inputs are for cargo builds")
        if s.kind == "cargo":
            if s.worktree:
                if s.commit or s.repo or s.url or s.mirrors or s.ref:
                    raise SpecError(f"scheduler {s.label}: a worktree builds as it stands; "
                                    f"repo, url, mirrors, ref and commit belong to a commit build")
            elif not (s.commit and (s.repo or s.url)):
                raise SpecError(f"scheduler {s.label}: cargo needs a commit with repo or url, "
                                f"or a worktree")
            if s.commit and not SHA40.fullmatch(s.commit):
                raise SpecError(f"scheduler {s.label}: commit must be the full 40-hex SHA")
            if not s.binary:
                raise SpecError(f"scheduler {s.label}: cargo needs the built binary name")
        elif s.kind == "binary":
            if not s.binary or not Path(s.binary).is_file():
                raise SpecError(f"scheduler {s.label}: binary {s.binary!r} not found")
        elif s.kind != "none":
            raise SpecError(f"scheduler {s.label}: kind is cargo, binary or none")
    kl = {k.label for k in t.kernels}
    sl = {s.label for s in t.schedulers}
    for e in t.expects:
        bad = (set(e.kernels) - kl - {"*"}) | (set(e.schedulers) - sl - {"*"})
        if bad:
            raise SpecError(f"expect names unknown labels: {sorted(bad)}")
    _validate_run(t)
    return t


def _tables(raw: dict, name: str) -> list[dict]:
    v = raw.get(name, [])
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise SpecError(f"{name}: write each one as a [[{name}]] table")
    return v


def load(path: Path) -> Ticket:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except OSError as e:
        raise SpecError(f"ticket {path}: {e.strerror or e}") from e
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SpecError(f"ticket {path} is not valid TOML: {e}") from e
    tk = raw.get("ticket", {})
    run = raw.get("run", {})
    if not isinstance(tk, dict) or not isinstance(run, dict):
        raise SpecError(f"ticket {path}: [ticket] and [run] are tables")
    t = Ticket(id=tk.get("id", ""), url=tk.get("url", ""), title=tk.get("title", ""),
               kernels=tuple(_kernel(k) for k in _tables(raw, "kernel")),
               schedulers=tuple(_scheduler(s) for s in _tables(raw, "scheduler")),
               expects=tuple(_build(Expect, e, "expect") for e in _tables(raw, "expect")),
               run=_build(Run, run, "run"), source=str(Path(path).resolve()))
    return validate(t)


def _options(arg: str, opts: list[str], single: set[str], multi: set[str]) -> dict:
    """KEY=VALUE options after the head of a --kernel/--scheduler spec. A key in
    multi may repeat and collects a list, in order."""
    got: dict = {k: [] for k in multi}
    for o in opts:
        if not o:
            continue
        k, sep, v = o.partition("=")
        if not sep or k not in single | multi:
            raise SpecError(f"{arg!r}: unknown option {o!r}; known: "
                            f"{', '.join(sorted(single | multi))}")
        if k in multi:
            got[k].append(v)
        else:
            got[k] = v
    return got


def parse_kernel_arg(arg: str) -> Kernel:
    """LABEL=git:URL@SHA[#REF][,mirror=URL]...[,config=CONFIG_X=y]...[,cc=clang][,note=TEXT]
    LABEL=image:PATH[,modules=DIR][,note=TEXT]
    Option values cannot carry a comma."""
    label, _, rest = arg.partition("=")
    kind, _, body = rest.partition(":")
    head, *opts = body.split(",")
    if kind == "git":
        o = _options(arg, opts, {"cc", "note"}, {"mirror", "config"})
        head, _, ref = head.partition("#")
        url, _, sha = head.rpartition("@")
        raw = {"label": label, "kind": "git", "url": url, "sha": sha, "ref": ref,
               "mirrors": o["mirror"], "config": o["config"]}
        raw.update({k: o[k] for k in ("cc", "note") if k in o})
        return _kernel(raw)
    if kind == "image":
        o = _options(arg, opts, {"modules", "note"}, set())
        return _kernel({"label": label, "kind": "image", "image": head,
                        **{k: o[k] for k in ("modules", "note") if k in o}})
    raise SpecError(f"--kernel {arg!r}: LABEL=git:URL@SHA[#REF][,opt]... or "
                    f"LABEL=image:PATH[,opt]...")


def parse_scheduler_arg(arg: str) -> Scheduler:
    """LABEL=cargo:DIR[,opt]...                     (worktree as it stands)
    LABEL=cargo:REPO@COMMIT[:SUBDIR][,opt]...       (exported commit; REPO may be a URL)
    LABEL=binary:PATH[,obj=FILE.bpf.o]...[,opt]...
    LABEL=none[,opt]...                             (the kernel's own)
    Options: bin=NAME pkg=PKG ops=NAME note=TEXT ref=BRANCH, and repeatable
    arg=ARGV-ELEMENT sha256=RELPATH:HEX mirror=URL input=HOST-FILE. Option
    values cannot carry a comma."""
    label, _, rest = arg.partition("=")
    if rest == "none" or rest.startswith("none,"):
        kind, head, opts = "none", "", rest.split(",")[1:]
    else:
        kind, _, body = rest.partition(":")
        head, *opts = body.split(",")
    if kind not in SCHED_OPTIONS:
        raise SpecError(f"--scheduler {arg!r}: kind is cargo, binary or none")
    o = _options(arg, opts, *SCHED_OPTIONS[kind])
    raw: dict = {"label": label, "kind": kind, "args": o["arg"]}
    raw.update({k: o[k] for k in ("ops", "note") if k in o})
    pins = {}
    for p in o.get("sha256", ()):
        rel, sep, hx = p.rpartition(":")
        if not sep or not rel:
            raise SpecError(f"{arg!r}: sha256={p!r} is RELPATH:HEX")
        pins[rel] = hx
    if pins:
        raw["sha256"] = pins
    if kind == "cargo":
        raw.update(binary=o.get("bin", ""), inputs=o["input"], mirrors=o["mirror"],
                   ref=o.get("ref", ""))
        if "pkg" in o:
            raw["package"] = o["pkg"]
        m = COMMIT_FORM.fullmatch(head)
        if m:
            src, commit, sub = m.groups()
            raw.update(commit=commit, path=sub or ".")
            raw["url" if REMOTE.match(src) else "repo"] = src
        else:
            raw["worktree"] = head
    elif kind == "binary":
        raw.update(binary=head, objects=o["obj"])
    return _scheduler(raw)


def with_overrides(t: Ticket, kernels=(), schedulers=(), only_kernels=(),
                   only_schedulers=(), **run) -> Ticket:
    """CLI additions replace same-label entries; --only-* narrows the matrix;
    run overrides apply field by field. Narrowing also drops the labels it
    removed from every [[expect]] (and an expect left naming nothing), so a
    label the ticket never defined is still caught."""
    ks = {k.label: k for k in t.kernels}
    ks.update({k.label: k for k in kernels})
    ss = {s.label: s for s in t.schedulers}
    ss.update({s.label: s for s in schedulers})
    gone_k = {x for x in ks if x not in only_kernels} if only_kernels else set()
    gone_s = {x for x in ss if x not in only_schedulers} if only_schedulers else set()
    ks = {x: k for x, k in ks.items() if x not in gone_k}
    ss = {x: s for x, s in ss.items() if x not in gone_s}
    expects = []
    for e in t.expects:
        ek = tuple(x for x in e.kernels if x not in gone_k)
        es = tuple(x for x in e.schedulers if x not in gone_s)
        if ek and es:
            expects.append(replace(e, kernels=ek, schedulers=es))
    r = replace(t.run, **{k: v for k, v in run.items() if v is not None})
    return validate(replace(t, kernels=tuple(ks.values()), schedulers=tuple(ss.values()),
                            expects=tuple(expects), run=r))


# TOML basic strings must escape these; the rest of the control range as \uXXXX.
_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
        "\r": "\\r"}


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = "".join(_ESC.get(c) or (f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c)
                for c in str(v))
    return f'"{s}"'


def to_toml(t: Ticket) -> str:
    """The effective spec, written beside every result."""
    source = " ".join(t.source.splitlines())
    out = ["[ticket]", f"id = {_toml_value(t.id)}", f"url = {_toml_value(t.url)}",
           f"title = {_toml_value(t.title)}", f"# source = {source}", "", "[run]"]
    for f in fields(Run):
        out.append(f"{f.name} = {_toml_value(getattr(t.run, f.name))}")
    for name, items, cls in (("kernel", t.kernels, Kernel),
                             ("scheduler", t.schedulers, Scheduler),
                             ("expect", t.expects, Expect)):
        for it in items:
            out += ["", f"[[{name}]]"]
            pins = ()
            for f in fields(cls):
                v = getattr(it, f.name)
                if f.name == "sha256":
                    pins = v
                    continue
                if v in ("", ()) and f.name not in ("label", "kind"):
                    continue
                out.append(f"{f.name} = {_toml_value(v)}")
            if pins:
                out.append(f"[{name}.sha256]")
                out += [f"{_toml_value(p)} = {_toml_value(h)}" for p, h in pins]
    return "\n".join(out) + "\n"
