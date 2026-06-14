#!/usr/bin/env python3
"""PANDEMONIUM bench-ipc: IPC round-trip latency.

A thin front-end over bench-scale's --ipc mode. bench-ipc therefore shares the
EXACT scheduler lifecycle (pre-flight, per-scheduler start_and_wait + settle,
per-width restrict_cpus, telemetry-on-stop), the per-scheduler crash detection,
the logging format, and the .prom output that bench-scale and every other
bench-* use. There is no separate IPC engine or lifecycle here -- that is the
whole point: the one that does not crash sched_ext is bench-scale's, so we use
it. All arguments pass straight through (--pandemonium-only, --core-counts,
--iterations, --schedulers, ...).
"""
import subprocess
import sys
from pathlib import Path

_TESTS = Path(__file__).parent.resolve() / "pandemonium-tests.py"

if __name__ == "__main__":
    sys.exit(subprocess.run(
        [sys.executable, str(_TESTS), "bench-scale", "--ipc"] + sys.argv[1:],
        cwd=Path(__file__).parent.parent.resolve(),
    ).returncode)
