#!/usr/bin/env python3
# build-versions.py -- build PANDEMONIUM scheduler binaries for the matrix into
# the cache, each from its release commit via a throwaway git worktree off the
# monorepo PANDEMONIUM actually lives in (~/PANDEMONIUM is a stale, disconnected
# deployment copy -- capped at v5.14.0 with an older commit-subject convention
# -- never the source of truth; see ~/personal/PROGRAMMING/CLAUDE.md). Versions
# are identified by the real commit-subject convention in this history,
# "Updates: PANDEMONIUMv<V>, ...". Output: cache/versions/pandemonium-<V>.
#
# Usage: build-versions.py [5.12.0 5.13.0 ...]   (default: all 5.12-5.16)

import os
import subprocess
import sys
from pathlib import Path

CACHE = Path.home() / ".cache" / "pandemonium" / "7_1-bug"
VERSIONS = CACHE / "versions"
REPO = (Path.home() / "personal" / "PROGRAMMING" / "SYSTEM PROGRAMS" /
        "LINUX" / "PANDEMONIUM")
# Path of the PANDEMONIUM subtree relative to the monorepo root -- REPO is a
# subdirectory of a larger repo, so a worktree checks out the whole monorepo
# at that commit; Cargo.toml lives at this relative path inside it.
SUBDIR = "PROGRAMMING/SYSTEM PROGRAMS/LINUX/PANDEMONIUM"
DEFAULT = ["5.12.0", "5.13.0", "5.14.0", "5.15.0", "5.16.0"]


def commit_for(v: str) -> str | None:
    # Scoped to commits that touched the PANDEMONIUM subtree, both for
    # correctness (a monorepo commit elsewhere could coincidentally mention
    # the marker) and speed (skip history outside this subtree). "-C REPO"
    # already puts us AT that subdirectory, so the pathspec is "." (repo-root
    # -relative SUBDIR would look for a nested copy of itself and match
    # nothing).
    r = subprocess.run(["git", "-C", str(REPO), "log", "--all",
                        "--format=%H %s", "--", "."],
                       capture_output=True, text=True)
    marker = f"PANDEMONIUMv{v},"
    for line in r.stdout.splitlines():
        h, _, s = line.partition(" ")
        if marker in s:
            return h
    return None


def build(v: str) -> None:
    dst = VERSIONS / f"pandemonium-{v}"
    if dst.is_file():
        print(f"HAVE {v}")
        return
    commit = commit_for(v)
    if not commit:
        print(f"MISS {v}: no commit with 'PANDEMONIUMv{v},' touching {SUBDIR}")
        return
    wt = Path(f"/tmp/pand-wt-{v}")
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force",
                    str(wt)], capture_output=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach",
                    str(wt), commit], capture_output=True, text=True)
    manifest = wt / SUBDIR / "Cargo.toml"
    # CARGO_TARGET_DIR out of the worktree: the vendored libbpf-sys Makefile
    # mis-parses a space in the path ("SYSTEM PROGRAMS", present here too)
    # and fails the build if target/ is left at its default location inside
    # the manifest's own directory tree.
    target_dir = Path(f"/tmp/pandemonium-build-{v}")
    r = subprocess.run(["cargo", "build", "--release", "--manifest-path",
                        str(manifest)],
                       env={**os.environ, "CARGO_TARGET_DIR": str(target_dir)},
                       capture_output=True, text=True)
    binp = target_dir / "release" / "pandemonium"
    if r.returncode == 0 and binp.is_file():
        VERSIONS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", str(binp), str(dst)])
        print(f"OK   {v} ({dst.stat().st_size} bytes)")
    else:
        tail = r.stderr.strip().splitlines()[-3:]
        print(f"FAIL {v}: {' | '.join(tail)}")
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force",
                    str(wt)], capture_output=True)


def main() -> int:
    for v in (sys.argv[1:] or DEFAULT):
        build(v)
    print("\navailable:", ", ".join(sorted(p.name.replace("pandemonium-", "")
          for p in VERSIONS.glob("pandemonium-*"))) or "(none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
