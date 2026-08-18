#!/usr/bin/env python3
"""prism_install_gate -- the install/uninstall symmetry gate for prism.py.

THE DEFECT THIS EXISTS TO CATCH, found in a 2026-08-05 field report. prism's
ephemeral install placed montauk and montauk --analyze and nothing else, while
sublimation -- which every stream and numeric step in prism.py routes through --
shipped in the same build directory and was simply never copied. A user who
opted to uninstall afterward therefore ran the ENTIRE report on prism's Python
fallbacks, announced once in a warning above a successful install. The mirror
half was the same asymmetry read backwards: whatever the install placed, the
uninstall had to remove, and a binary added to one list without the other leaves
the system dirty after a run that reported success.

Neither half fails loudly. An install that quietly omits a tool still produces a
report, and an uninstall that quietly leaves a binary still prints "removed".
That is exactly the shape a container test would catch and a developer machine
never will, because every machine here already has all three binaries installed
permanently -- which is how this rotted unnoticed in the first place.

So this gate reads the SETS rather than running an install: the binaries the
ephemeral path copies, the binaries the removal path deletes and the binaries
the removal path verifies afterward must all be the same set. It is a static
check by design, needing no root, no network and no container, so it can run in
the ordinary test sweep instead of only where a clean machine exists.

    python3 prism_install_gate.py
"""
import ast
import sys
from pathlib import Path

PRISM = Path(__file__).resolve().parent / "prism.py"

# Every binary the ephemeral install is responsible for. Adding one here without
# adding it to prism.py's install AND removal paths is the failure this catches.
EXPECTED = {"MONTAUK_INSTALLED", "SUBLIMATION_INSTALLED"}


def _names_in(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _function(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def check() -> list[str]:
    tree = ast.parse(PRISM.read_text())
    failures = []

    ensure = _function(tree, "ensure_montauk")
    remove = _function(tree, "remove_montauk_if_ours")
    if ensure is None:
        return ["ensure_montauk() not found in prism.py"]
    if remove is None:
        return ["remove_montauk_if_ours() not found in prism.py"]

    placed = _names_in(ensure) & EXPECTED
    deleted = _names_in(remove) & EXPECTED

    missing_install = EXPECTED - placed
    if missing_install:
        failures.append(
            f"ensure_montauk() never places: {', '.join(sorted(missing_install))} "
            f"-- an ephemeral run would use the Python fallback for it silently")
    missing_remove = EXPECTED - deleted
    if missing_remove:
        failures.append(
            f"remove_montauk_if_ours() never removes: "
            f"{', '.join(sorted(missing_remove))} -- uninstall leaves the system dirty")

    # The removal must also VERIFY, not merely call rm: an uninstall that reports
    # success without re-checking the filesystem is the exact failure reported
    # from the field, where the option ran and the binaries stayed.
    src = ast.get_source_segment(PRISM.read_text(), remove) or ""
    if "is_file()" not in src:
        failures.append(
            "remove_montauk_if_ours() does not re-check the filesystem after "
            "removing -- it cannot tell a successful uninstall from a failed one")

    # prism routes stream work through sublimation's real verbs. `grep` is not
    # one of them; an unknown command exits 2 and the rc guard sends every call
    # to the Python fallback, which is how this hid.
    text = PRISM.read_text()
    for bad in ('"grep"', "'grep'"):
        for line in text.splitlines():
            if bad in line and "sublimation" in line and not line.strip().startswith("#"):
                failures.append(f"sublimation has no grep verb, use search: {line.strip()}")
    return failures


def main() -> int:
    failures = check()
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"ok: install and uninstall agree on {len(EXPECTED)} binaries, "
          f"removal verifies, sublimation verbs valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
