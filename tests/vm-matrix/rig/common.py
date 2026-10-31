# common.py -- paths, logging and content identity shared by every lane.

import hashlib
import json
import os
import re
import time
from pathlib import Path

HARNESS = Path(__file__).parent.parent.resolve()
TESTS = HARNESS.parent
PROJECT = TESTS.parent
# The 7.1-bug reproducer beside this harness owns the guest tracer build and
# the builder image; its digest-keyed caches are shared, not duplicated.
SIBLING = TESTS / "pandemonium-7_1-bug"

# Persistent: survives a reboot. Everything here is keyed by content.
CACHE = Path.home() / ".cache" / "pandemonium" / "vm-matrix"
KERNELS = CACHE / "kernels"
SCHEDULERS = CACHE / "schedulers"
IMAGES = CACHE / "images"
TICKETS = CACHE / "tickets"
# Bare clones the harness fetches pinned scheduler commits into, by remote.
REPOS = CACHE / "repos"

# Scratch: large and disposable (kernel worktrees, out-of-tree objects, cargo
# targets). Kept out of the source path, which has spaces.
BUILD = Path("/tmp/PANDEMONIUM-build/vm-matrix")
LOGS = Path("/tmp/PANDEMONIUM/vm-matrix")
KREPO = BUILD / "linux"

MONTAUK = Path("/usr/local/bin/montauk")


def log(msg: str, level: str = "INFO") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{level}]   {msg}", flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def digest(parts: dict) -> str:
    """A short key over named inputs. Order-independent: the names sort."""
    blob = "\n".join(f"{k}={parts[k]}" for k in sorted(parts))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


def stamp() -> str:
    # UTC: run directories sort by name, and local time repeats an hour at DST.
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())


def ids() -> tuple[str, str]:
    """HOST_UID / HOST_GID for containers that must hand ownership back."""
    return str(os.getuid()), str(os.getgid())
