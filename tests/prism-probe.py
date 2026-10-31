#!/usr/bin/env python3
"""PANDEMONIUM prism-probe: the latency probe under the longrun and mixed loads,
traced whole.

A thin front-end over pandemonium-tests.py's prism-probe command, the way
prism-ipc fronts prism-scale --ipc, so it shares the one scheduler lifecycle and
the one capture launcher the suite has. All arguments pass straight through
(--iterations, --schedulers, --pandemonium-only, --duration, --scenarios,
--trace). It is a capture and refuses to run without --trace.
"""
import subprocess
import sys
from pathlib import Path

_TESTS = Path(__file__).parent.resolve() / "pandemonium-tests.py"

if __name__ == "__main__":
    sys.exit(subprocess.run(
        [sys.executable, str(_TESTS), "prism-probe"] + sys.argv[1:],
        cwd=Path(__file__).parent.parent.resolve(),
    ).returncode)
