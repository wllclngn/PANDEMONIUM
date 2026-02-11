#!/usr/bin/env python3
"""PANDEMONIUM -- BUILD, RUN, BENCHMARK, REPORT

AUTOMATES THE FULL CYCLE:
  1. BUILD WITH CARGO (CARGO_TARGET_DIR=/tmp/pandemonium-build TO AVOID SPACES IN PATH)
  2. RUN WITH SUDO (SCHED_EXT REQUIRES ROOT)
  3. CAPTURE SCHEDULER OUTPUT + DMESG
  4. DISPLAY COMBINED REPORT
"""

import subprocess
import sys
import os
import signal
import shutil
import time
import math
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = "/tmp/pandemonium-build"
BINARY = os.path.join(TARGET_DIR, "release", "pandemonium")
LOG_DIR = "/tmp/pandemonium"


def check_deps():
    """CHECK THAT ALL REQUIRED TOOLS ARE INSTALLED"""
    missing = []
    for tool in ["cargo", "rustc", "clang", "sudo"]:
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(f"ERROR: MISSING TOOLS: {', '.join(missing)}")
        if "cargo" in missing or "rustc" in missing:
            print("  INSTALL RUST: https://rustup.rs")
        if "clang" in missing:
            print("  INSTALL CLANG: pacman -S clang")
        sys.exit(1)

    # CHECK KERNEL CONFIG
    kconfig = "/proc/config.gz"
    if os.path.exists(kconfig):
        import gzip
        with gzip.open(kconfig, "rt") as f:
            config = f.read()
        if "CONFIG_SCHED_CLASS_EXT=y" not in config:
            print("WARNING: CONFIG_SCHED_CLASS_EXT=y NOT FOUND IN KERNEL CONFIG")
            print("  SCHED_EXT MAY NOT BE AVAILABLE")


def build(release=True):
    """BUILD PANDEMONIUM"""
    mode = "RELEASE" if release else "DEBUG"
    print(f"BUILDING PANDEMONIUM ({mode})...")

    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = TARGET_DIR

    cmd = ["cargo", "build"]
    if release:
        cmd.append("--release")

    result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)

    if result.returncode != 0:
        print("BUILD FAILED:")
        print(result.stderr)
        sys.exit(1)

    size = os.path.getsize(BINARY)
    print(f"BUILD COMPLETE. BINARY: {BINARY} ({size / 1024:.0f} KB)")
    print()


def capture_dmesg_cursor():
    """GET JOURNALCTL CURSOR SO WE CAN FILTER NEW MESSAGES LATER"""
    result = subprocess.run(
        ["journalctl", "-k", "--no-pager", "-n", "1", "--show-cursor"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    # LAST LINE IS "-- cursor: s=...;i=...;..."
    for line in reversed(result.stdout.strip().split("\n")):
        if line.startswith("-- cursor:"):
            return line.split(":", 1)[1].strip()
    return None


def capture_dmesg_after(cursor):
    """CAPTURE KERNEL MESSAGES SINCE CURSOR, FILTERED FOR SCX/PANDEMONIUM"""
    cmd = ["journalctl", "-k", "--no-pager"]
    if cursor:
        cmd.extend(["--after-cursor", cursor])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""

    relevant = []
    for line in result.stdout.strip().split("\n"):
        if not line or line.startswith("-- "):
            continue
        low = line.lower()
        if any(kw in low for kw in ["sched_ext", "scx", "pandemonium"]):
            relevant.append(line)
    return "\n".join(relevant)


def run(args=None):
    """RUN PANDEMONIUM WITH SUDO. RETURNS (SCHEDULER_OUTPUT, DMESG, RETURNCODE)"""
    if not os.path.exists(BINARY):
        print(f"ERROR: BINARY NOT FOUND AT {BINARY}")
        print("  RUN WITH --build FIRST")
        sys.exit(1)

    cmd = ["sudo", BINARY]
    if args:
        cmd.extend(args)

    print(f"RUNNING: {' '.join(cmd)}")
    print("=" * 60)
    print()

    # CAPTURE JOURNALCTL CURSOR BEFORE RUN
    cursor = capture_dmesg_cursor()

    # RUN WITH STDOUT/STDERR PIPED SO WE CAN TEE TO TERMINAL + CAPTURE
    output_lines = []
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True,
        )
        for line in proc.stdout:
            print(line, end="")
            output_lines.append(line)
        proc.wait()
    except KeyboardInterrupt:
        # CTRL+C: SEND SIGINT TO THE CHILD (WHICH TRIGGERS ITS SHUTDOWN)
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                # DRAIN REMAINING OUTPUT (SHUTDOWN MESSAGES)
                for line in proc.stdout:
                    print(line, end="")
                    output_lines.append(line)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print()

    scheduler_output = "".join(output_lines)

    print()
    print("=" * 60)
    returncode = proc.returncode if proc else -1
    print(f"PANDEMONIUM EXITED WITH CODE {returncode}")

    # CAPTURE DMESG
    time.sleep(0.2)  # BRIEF PAUSE FOR KERNEL LOG FLUSH
    dmesg = capture_dmesg_after(cursor)

    return scheduler_output, dmesg, returncode


def save_logs(scheduler_output, dmesg, returncode):
    """SAVE ALL OUTPUT TO /tmp/pandemonium/"""
    os.makedirs(LOG_DIR, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # SAVE SCHEDULER OUTPUT
    sched_path = os.path.join(LOG_DIR, f"run-{stamp}.log")
    with open(sched_path, "w") as f:
        f.write(scheduler_output)

    # SAVE DMESG
    dmesg_path = os.path.join(LOG_DIR, f"dmesg-{stamp}.log")
    with open(dmesg_path, "w") as f:
        f.write(dmesg if dmesg else "(NO RELEVANT KERNEL MESSAGES)\n")

    # SAVE COMBINED REPORT
    report_path = os.path.join(LOG_DIR, f"report-{stamp}.log")
    with open(report_path, "w") as f:
        f.write(f"PANDEMONIUM RUN -- {stamp}\n")
        f.write(f"EXIT CODE: {returncode}\n")
        f.write("=" * 60 + "\n")
        f.write("SCHEDULER OUTPUT\n")
        f.write("=" * 60 + "\n")
        f.write(scheduler_output)
        f.write("\n" + "=" * 60 + "\n")
        f.write("KERNEL LOG (DMESG)\n")
        f.write("=" * 60 + "\n")
        f.write(dmesg if dmesg else "(NO RELEVANT KERNEL MESSAGES)\n")

    # SYMLINK latest -> MOST RECENT
    latest = os.path.join(LOG_DIR, "latest.log")
    if os.path.islink(latest):
        os.unlink(latest)
    os.symlink(report_path, latest)

    return sched_path, dmesg_path, report_path


def report(scheduler_output, dmesg, returncode):
    """DISPLAY COMBINED REPORT AND SAVE LOGS"""
    # SAVE TO DISK
    sched_path, dmesg_path, report_path = save_logs(scheduler_output, dmesg, returncode)

    # PRINT DMESG SECTION
    print()
    print("=" * 60)
    print("KERNEL LOG (DMESG)")
    print("=" * 60)
    if dmesg:
        for line in dmesg.split("\n"):
            print(f"  {line}")
    else:
        print("  (NO RELEVANT KERNEL MESSAGES)")

    # PRINT STATUS
    print()
    if returncode == 0:
        print("STATUS: CLEAN EXIT")
    elif returncode == -2 or returncode == 130:
        print("STATUS: USER INTERRUPTED (CTRL+C)")
    else:
        print(f"STATUS: EXIT CODE {returncode}")

    # PRINT LOG LOCATIONS
    print()
    print(f"LOGS SAVED TO {LOG_DIR}/")
    print(f"  SCHEDULER: {sched_path}")
    print(f"  DMESG:     {dmesg_path}")
    print(f"  COMBINED:  {report_path}")
    print(f"  LATEST:    {os.path.join(LOG_DIR, 'latest.log')}")


def timed_run(cmd):
    """RUN A COMMAND AND RETURN WALL-CLOCK TIME IN SECONDS"""
    print(f"  RUNNING: {cmd}")
    start = time.monotonic()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        print(f"  COMMAND FAILED (EXIT {result.returncode}):")
        print(result.stderr[:500])
        return None
    print(f"  COMPLETED IN {elapsed:.2f}s")
    return elapsed


def is_scx_active():
    """CHECK IF SCHED_EXT IS THE ACTIVE SCHEDULER"""
    try:
        with open("/sys/kernel/sched_ext/root/ops", "r") as f:
            ops = f.read().strip()
        return len(ops) > 0
    except FileNotFoundError:
        return False


def mean_stdev(values):
    """COMPUTE MEAN AND STANDARD DEVIATION"""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    m = sum(values) / n
    if n == 1:
        return m, 0.0
    variance = sum((x - m) ** 2 for x in values) / (n - 1)
    return m, math.sqrt(variance)


def benchmark(cmd, iterations, clean_cmd=None, sched_args=None):
    """A/B BENCHMARK: EEVDF VS PANDEMONIUM"""
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print("=" * 60)
    print("PANDEMONIUM A/B BENCHMARK")
    print("=" * 60)
    print(f"COMMAND:    {cmd}")
    print(f"ITERATIONS: {iterations}")
    if clean_cmd:
        print(f"CLEAN CMD:  {clean_cmd}")
    print()

    # PHASE 1: EEVDF BASELINE (NO PANDEMONIUM)
    if is_scx_active():
        print("ERROR: SCHED_EXT IS ALREADY ACTIVE. STOP IT BEFORE BENCHMARKING.")
        sys.exit(1)

    print("PHASE 1: EEVDF BASELINE")
    print("-" * 40)
    eevdf_times = []
    for i in range(iterations):
        print(f"  ITERATION {i + 1}/{iterations}")
        if clean_cmd:
            subprocess.run(clean_cmd, shell=True, capture_output=True)
        t = timed_run(cmd)
        if t is None:
            print("  ABORTING BENCHMARK: COMMAND FAILED")
            sys.exit(1)
        eevdf_times.append(t)
    print()

    # PHASE 2: START PANDEMONIUM
    print("PHASE 2: STARTING PANDEMONIUM")
    print("-" * 40)
    if not os.path.exists(BINARY):
        print(f"ERROR: BINARY NOT FOUND AT {BINARY}")
        print("  RUN WITH --build FIRST")
        sys.exit(1)

    pand_proc = subprocess.Popen(
        ["sudo", BINARY] + (sched_args or []),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )

    # WAIT FOR PANDEMONIUM TO BECOME ACTIVE
    for attempt in range(20):
        time.sleep(0.5)
        if is_scx_active():
            print("  PANDEMONIUM IS ACTIVE")
            break
    else:
        print("  ERROR: PANDEMONIUM DID NOT ACTIVATE WITHIN 10S")
        pand_proc.send_signal(signal.SIGINT)
        pand_proc.wait(timeout=5)
        sys.exit(1)
    print()

    # PHASE 3: PANDEMONIUM BENCHMARK
    print("PHASE 3: PANDEMONIUM BENCHMARK")
    print("-" * 40)
    pand_times = []
    for i in range(iterations):
        print(f"  ITERATION {i + 1}/{iterations}")
        if clean_cmd:
            subprocess.run(clean_cmd, shell=True, capture_output=True)
        t = timed_run(cmd)
        if t is None:
            print("  ABORTING BENCHMARK: COMMAND FAILED")
            pand_proc.send_signal(signal.SIGINT)
            pand_proc.wait(timeout=5)
            sys.exit(1)
        pand_times.append(t)
    print()

    # PHASE 4: STOP PANDEMONIUM
    print("PHASE 4: STOPPING PANDEMONIUM")
    pand_proc.send_signal(signal.SIGINT)
    try:
        pand_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pand_proc.kill()
        pand_proc.wait()
    print("  PANDEMONIUM STOPPED")
    print()

    # PHASE 5: RESULTS
    eevdf_mean, eevdf_std = mean_stdev(eevdf_times)
    pand_mean, pand_std = mean_stdev(pand_times)

    if eevdf_mean > 0:
        delta_pct = ((pand_mean - eevdf_mean) / eevdf_mean) * 100
    else:
        delta_pct = 0.0

    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append("BENCHMARK RESULTS")
    report_lines.append("=" * 60)
    report_lines.append(f"COMMAND: {cmd}")
    report_lines.append(f"ITERATIONS: {iterations}")
    report_lines.append("")
    report_lines.append(f"EEVDF:       {eevdf_mean:.2f}s +/- {eevdf_std:.2f}s")
    report_lines.append(f"  RUNS: {', '.join(f'{t:.2f}s' for t in eevdf_times)}")
    report_lines.append(f"PANDEMONIUM: {pand_mean:.2f}s +/- {pand_std:.2f}s")
    report_lines.append(f"  RUNS: {', '.join(f'{t:.2f}s' for t in pand_times)}")
    report_lines.append("")
    if delta_pct < 0:
        report_lines.append(f"DELTA: {delta_pct:+.1f}% (PANDEMONIUM IS {abs(delta_pct):.1f}% FASTER)")
    elif delta_pct > 0:
        report_lines.append(f"DELTA: {delta_pct:+.1f}% (PANDEMONIUM IS {delta_pct:.1f}% SLOWER)")
    else:
        report_lines.append("DELTA: 0.0% (NO DIFFERENCE)")
    report_lines.append("=" * 60)

    for line in report_lines:
        print(line)

    # SAVE BENCHMARK REPORT
    bench_path = os.path.join(LOG_DIR, f"benchmark-{stamp}.log")
    with open(bench_path, "w") as f:
        f.write("\n".join(report_lines) + "\n")
    print(f"\nSAVED TO {bench_path}")


def pw_top_snapshot():
    """CAPTURE PW-TOP BATCH OUTPUT. RETURNS LIST OF (NAME, ERR_COUNT) TUPLES."""
    try:
        proc = subprocess.Popen(
            ["pw-top", "-b"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # PW-TOP -b DOESN'T EXIT -- READ 2 FRAMES THEN KILL
        time.sleep(1.5)
        proc.kill()
        stdout, _ = proc.communicate(timeout=2)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        return []
    entries = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("S "):
            continue
        # FORMAT: S/R/C  ID  QUANT  RATE  WAIT  BUSY  W/Q  B/Q  ERR  FORMAT  NAME
        parts = line.split()
        if len(parts) < 9:
            continue
        if parts[0] not in ("R", "S", "C"):
            continue
        try:
            err = int(parts[8])
            name = " ".join(parts[9:])  # NAME MAY CONTAIN SPACES
            # STRIP LEADING + FOR FOLLOWER NODES
            name = name.lstrip("+ ").strip()
            entries.append((name, err))
        except (ValueError, IndexError):
            pass
    return entries


def pw_audio_playing():
    """CHECK IF ANY AUDIO SINK-INPUT IS ACTIVE VIA PACTL."""
    result = subprocess.run(
        ["pactl", "list", "sink-inputs", "short"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and len(result.stdout.strip()) > 0


def pw_get_xruns(name_filter=None):
    """GET XRUN (ERR) COUNT FROM PW-TOP. OPTIONALLY FILTER BY NAME SUBSTRING."""
    entries = pw_top_snapshot()
    total = 0
    for name, err in entries:
        if name_filter is None or name_filter.lower() in name.lower():
            total += err
    return total


def bench_mixed():
    """A/B MIXED WORKLOAD BENCHMARK: COMPILE WHILE AUDIO PLAYS.
    MEASURES COMPILATION TIME + PIPEWIRE XRUNS (AUDIO GLITCHES)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print("=" * 60)
    print("PANDEMONIUM MIXED WORKLOAD BENCHMARK")
    print("=" * 60)
    print()

    # CHECK AUDIO IS PLAYING
    if not pw_audio_playing():
        print("ERROR: NO AUDIO PLAYING.")
        print("  START OUROBOROS (OR ANY AUDIO PLAYER) AND PLAY MUSIC FIRST.")
        sys.exit(1)

    # SHOW ACTIVE AUDIO STREAMS
    entries = pw_top_snapshot()
    audio_streams = [(n, e) for n, e in entries if n.startswith("R") or "Music" in n or "ouroboros" in n.lower()]
    if not audio_streams:
        # JUST SHOW ALL RUNNING NODES
        audio_streams = entries
    print("ACTIVE PIPEWIRE NODES:")
    for name, err in entries:
        if err >= 0:
            print(f"  {name} (xruns: {err})")
    print()

    if not os.path.exists(BINARY):
        print(f"ERROR: BINARY NOT FOUND AT {BINARY}")
        print("  RUN WITH --build FIRST")
        sys.exit(1)

    build_cmd = f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
    clean_cmd = f"cargo clean --target-dir {TARGET_DIR}"

    if is_scx_active():
        print("ERROR: SCHED_EXT IS ALREADY ACTIVE. STOP IT BEFORE BENCHMARKING.")
        sys.exit(1)

    results = []

    # PHASE 1: EEVDF BASELINE
    print("PHASE 1: EEVDF (DEFAULT SCHEDULER)")
    print("-" * 40)
    subprocess.run(clean_cmd, shell=True, capture_output=True)
    xruns_before = pw_get_xruns()
    print(f"  XRUNS BEFORE: {xruns_before}")
    t = timed_run(build_cmd)
    if t is None:
        print("  BUILD FAILED")
        sys.exit(1)
    xruns_after = pw_get_xruns()
    eevdf_xruns = xruns_after - xruns_before
    print(f"  XRUNS AFTER:  {xruns_after} (DELTA: {eevdf_xruns})")
    results.append(("EEVDF", t, eevdf_xruns))
    print()

    # PHASE 2: START PANDEMONIUM
    print("PHASE 2: STARTING PANDEMONIUM")
    print("-" * 40)
    pand_proc = subprocess.Popen(
        ["sudo", BINARY, "--build-mode"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    for attempt in range(20):
        time.sleep(0.5)
        if is_scx_active():
            print("  PANDEMONIUM IS ACTIVE")
            break
    else:
        print("  ERROR: PANDEMONIUM DID NOT ACTIVATE WITHIN 10S")
        pand_proc.send_signal(signal.SIGINT)
        pand_proc.wait(timeout=5)
        sys.exit(1)

    # LET SCHEDULER STABILIZE
    time.sleep(2)
    print()

    # PHASE 3: PANDEMONIUM BENCHMARK
    print("PHASE 3: PANDEMONIUM (BUILD-MODE)")
    print("-" * 40)
    subprocess.run(clean_cmd, shell=True, capture_output=True)
    xruns_before = pw_get_xruns()
    print(f"  XRUNS BEFORE: {xruns_before}")
    t = timed_run(build_cmd)
    if t is None:
        print("  BUILD FAILED")
        pand_proc.send_signal(signal.SIGINT)
        pand_proc.wait(timeout=5)
        sys.exit(1)
    xruns_after = pw_get_xruns()
    pand_xruns = xruns_after - xruns_before
    print(f"  XRUNS AFTER:  {xruns_after} (DELTA: {pand_xruns})")
    results.append(("PANDEMONIUM", t, pand_xruns))
    print()

    # PHASE 4: STOP PANDEMONIUM
    print("PHASE 4: STOPPING PANDEMONIUM")
    pand_proc.send_signal(signal.SIGINT)
    try:
        pand_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pand_proc.kill()
        pand_proc.wait()
    print("  PANDEMONIUM STOPPED")
    print()

    # PHASE 5: RESULTS
    eevdf_name, eevdf_time, eevdf_xr = results[0]
    pand_name, pand_time, pand_xr = results[1]

    if eevdf_time > 0:
        delta_pct = ((pand_time - eevdf_time) / eevdf_time) * 100
    else:
        delta_pct = 0.0

    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append("MIXED WORKLOAD BENCHMARK RESULTS")
    report_lines.append("=" * 60)
    report_lines.append("WORKLOAD: CARGO BUILD --RELEASE + AUDIO PLAYBACK")
    report_lines.append("")
    report_lines.append(f"{'SCHEDULER':<16} {'BUILD TIME':>12} {'AUDIO XRUNS':>12}")
    report_lines.append(f"{'-'*16} {'-'*12} {'-'*12}")
    report_lines.append(f"{'EEVDF':<16} {eevdf_time:>11.2f}s {eevdf_xr:>12}")
    report_lines.append(f"{'PANDEMONIUM':<16} {pand_time:>11.2f}s {pand_xr:>12}")
    report_lines.append("")
    if delta_pct < 0:
        report_lines.append(f"BUILD DELTA: {delta_pct:+.1f}% (PANDEMONIUM IS {abs(delta_pct):.1f}% FASTER)")
    elif delta_pct > 0:
        report_lines.append(f"BUILD DELTA: {delta_pct:+.1f}% (PANDEMONIUM IS {delta_pct:.1f}% SLOWER)")
    else:
        report_lines.append("BUILD DELTA: 0.0% (NO DIFFERENCE)")

    xrun_delta = pand_xr - eevdf_xr
    if xrun_delta < 0:
        report_lines.append(f"XRUN DELTA:  {xrun_delta:+d} (PANDEMONIUM HAS FEWER AUDIO GLITCHES)")
    elif xrun_delta > 0:
        report_lines.append(f"XRUN DELTA:  {xrun_delta:+d} (PANDEMONIUM HAS MORE AUDIO GLITCHES)")
    else:
        report_lines.append("XRUN DELTA:  0 (SAME AUDIO QUALITY)")
    report_lines.append("=" * 60)

    for line in report_lines:
        print(line)

    # SAVE REPORT
    bench_path = os.path.join(LOG_DIR, f"mixed-{stamp}.log")
    with open(bench_path, "w") as f:
        f.write("\n".join(report_lines) + "\n")
    print(f"\nSAVED TO {bench_path}")


PROBE_SRC = os.path.join(ROOT, "tools", "probe.c")
PROBE_BIN = os.path.join(LOG_DIR, "probe")


def ensure_probe_binary():
    """COMPILE THE C PROBE IF NEEDED. RETURNS PATH TO BINARY."""
    if not os.path.exists(PROBE_SRC):
        print(f"ERROR: PROBE SOURCE NOT FOUND AT {PROBE_SRC}")
        sys.exit(1)
    # RECOMPILE IF SOURCE IS NEWER THAN BINARY
    if not os.path.exists(PROBE_BIN) or \
       os.path.getmtime(PROBE_SRC) > os.path.getmtime(PROBE_BIN):
        os.makedirs(LOG_DIR, exist_ok=True)
        result = subprocess.run(
            ["cc", "-O2", "-o", PROBE_BIN, PROBE_SRC],
            capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ERROR: FAILED TO COMPILE PROBE: {result.stderr}")
            sys.exit(1)
    return PROBE_BIN


def percentile(sorted_vals, p):
    """COMPUTE P-TH PERCENTILE FROM A SORTED LIST."""
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * p / 100.0)
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return sorted_vals[idx]


def bench_contention():
    """A/B CONTENTION BENCHMARK: COMPILATION + INTERACTIVE PROBE.
    MEASURES WAKEUP LATENCY OF AN INTERACTIVE TASK UNDER BATCH PRESSURE.
    PROBE IS A SEPARATE C PROCESS (NO GIL, NO PYTHON OVERHEAD)."""

    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe_bin = ensure_probe_binary()

    print("=" * 60)
    print("PANDEMONIUM CONTENTION BENCHMARK")
    print("=" * 60)
    print("WORKLOAD: CARGO BUILD --RELEASE + INTERACTIVE PROBE (10MS SLEEP/WAKE)")
    print()

    if not os.path.exists(BINARY):
        print(f"ERROR: BINARY NOT FOUND AT {BINARY}")
        print("  RUN WITH --build FIRST")
        sys.exit(1)

    build_cmd = f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
    clean_cmd = f"cargo clean --target-dir {TARGET_DIR}"

    if is_scx_active():
        print("ERROR: SCHED_EXT IS ALREADY ACTIVE. STOP IT BEFORE BENCHMARKING.")
        sys.exit(1)

    results = []

    for phase_name, sched_args in [("EEVDF (DEFAULT)", None), ("PANDEMONIUM (BUILD-MODE)", ["--build-mode"])]:
        print(f"PHASE: {phase_name}")
        print("-" * 40)

        pand_proc = None
        if sched_args is not None:
            # START PANDEMONIUM
            pand_proc = subprocess.Popen(
                ["sudo", BINARY] + sched_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            for attempt in range(20):
                time.sleep(0.5)
                if is_scx_active():
                    print("  PANDEMONIUM IS ACTIVE")
                    break
            else:
                print("  ERROR: PANDEMONIUM DID NOT ACTIVATE WITHIN 10S")
                pand_proc.send_signal(signal.SIGINT)
                pand_proc.wait(timeout=5)
                sys.exit(1)
            time.sleep(2)  # STABILIZE

        # CLEAN BUILD
        subprocess.run(clean_cmd, shell=True, capture_output=True)

        # START INTERACTIVE PROBE (SEPARATE C PROCESS -- NO GIL)
        probe_proc = subprocess.Popen(
            [probe_bin],
            stdout=subprocess.PIPE,
            text=True,
        )

        # RUN BUILD
        print("  BUILDING...")
        build_start = time.monotonic()
        build_result = subprocess.run(build_cmd, shell=True, capture_output=True, text=True)
        build_time = time.monotonic() - build_start

        if build_result.returncode != 0:
            print(f"  BUILD FAILED (EXIT {build_result.returncode})")
            probe_proc.terminate()
            probe_proc.wait()
            if pand_proc:
                pand_proc.send_signal(signal.SIGINT)
                pand_proc.wait(timeout=5)
            sys.exit(1)

        # WAIT FOR PROBE TO SETTLE (1 MORE SECOND OF DATA)
        time.sleep(1)

        # STOP PROBE
        probe_proc.terminate()
        probe_stdout, _ = probe_proc.communicate(timeout=5)

        # STOP PANDEMONIUM IF RUNNING
        if pand_proc:
            pand_proc.send_signal(signal.SIGINT)
            try:
                pand_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pand_proc.kill()
                pand_proc.wait()
            print("  PANDEMONIUM STOPPED")

        # PARSE PROBE OUTPUT (ONE OVERSHOOT VALUE PER LINE)
        overshoots = []
        for line in probe_stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    overshoots.append(float(line))
                except ValueError:
                    pass
        sorted_os = sorted(overshoots)
        n = len(sorted_os)
        med = percentile(sorted_os, 50)
        p99 = percentile(sorted_os, 99)
        worst = sorted_os[-1] if sorted_os else 0

        print(f"  BUILD TIME:  {build_time:.2f}s")
        print(f"  PROBE SAMPLES: {n}")
        print(f"  MEDIAN OVERSHOOT: {med:.0f}us")
        print(f"  P99 OVERSHOOT:    {p99:.0f}us")
        print(f"  WORST OVERSHOOT:  {worst:.0f}us")
        print()

        results.append((phase_name, build_time, n, med, p99, worst))

    # REPORT
    eevdf = results[0]
    pand = results[1]

    build_delta = ((pand[1] - eevdf[1]) / eevdf[1]) * 100 if eevdf[1] > 0 else 0
    med_delta = pand[3] - eevdf[3]
    p99_delta = pand[4] - eevdf[4]

    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append("CONTENTION BENCHMARK RESULTS")
    report_lines.append("=" * 60)
    report_lines.append("WORKLOAD: CARGO BUILD --RELEASE + INTERACTIVE PROBE (10MS SLEEP/WAKE)")
    report_lines.append("")
    report_lines.append(f"{'SCHEDULER':<24} {'BUILD':>8} {'SAMPLES':>8} {'MEDIAN':>8} {'P99':>8} {'WORST':>8}")
    report_lines.append(f"{'-'*24} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for name, bt, n, med, p99, worst in results:
        report_lines.append(f"{name:<24} {bt:>7.2f}s {n:>8} {med:>7.0f}us {p99:>7.0f}us {worst:>7.0f}us")
    report_lines.append("")
    if build_delta < 0:
        report_lines.append(f"BUILD DELTA: {build_delta:+.1f}% (PANDEMONIUM IS {abs(build_delta):.1f}% FASTER)")
    elif build_delta > 0:
        report_lines.append(f"BUILD DELTA: {build_delta:+.1f}% (PANDEMONIUM IS {build_delta:.1f}% SLOWER)")
    else:
        report_lines.append("BUILD DELTA: 0.0% (NO DIFFERENCE)")
    if med_delta < 0:
        report_lines.append(f"MEDIAN LATENCY DELTA: {med_delta:+.0f}us (PANDEMONIUM IS {abs(med_delta):.0f}us BETTER)")
    elif med_delta > 0:
        report_lines.append(f"MEDIAN LATENCY DELTA: {med_delta:+.0f}us (PANDEMONIUM IS {med_delta:.0f}us WORSE)")
    else:
        report_lines.append("MEDIAN LATENCY DELTA: 0us (SAME)")
    if p99_delta < 0:
        report_lines.append(f"P99 LATENCY DELTA: {p99_delta:+.0f}us (PANDEMONIUM IS {abs(p99_delta):.0f}us BETTER)")
    elif p99_delta > 0:
        report_lines.append(f"P99 LATENCY DELTA: {p99_delta:+.0f}us (PANDEMONIUM IS {p99_delta:.0f}us WORSE)")
    else:
        report_lines.append("P99 LATENCY DELTA: 0us (SAME)")
    report_lines.append("=" * 60)

    for line in report_lines:
        print(line)

    bench_path = os.path.join(LOG_DIR, f"contention-{stamp}.log")
    with open(bench_path, "w") as f:
        f.write("\n".join(report_lines) + "\n")
    print(f"\nSAVED TO {bench_path}")


def test_gate():
    """RUN RUST TEST GATE (LAYERS 1-4)"""
    print()
    print("PANDEMONIUM TEST GATE (RUST)")
    print("=" * 60)

    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = TARGET_DIR

    # LAYER 1: UNIT TESTS (NO ROOT)
    print("LAYER 1: RUST UNIT TESTS")
    l1 = subprocess.run(
        ["cargo", "test", "--release"],
        cwd=ROOT, env=env,
    )
    if l1.returncode != 0:
        print("LAYER 1 FAILED -- SKIPPING REMAINING LAYERS")
        sys.exit(1)
    print()

    # LAYERS 2-4: INTEGRATION TESTS (REQUIRES ROOT)
    print("LAYERS 2-4: INTEGRATION (REQUIRES ROOT)")
    result = subprocess.run(
        ["sudo", "-E", f"CARGO_TARGET_DIR={TARGET_DIR}",
         "cargo", "test", "--test", "gate", "--release",
         "--", "--ignored", "--test-threads=1", "full_gate"],
        cwd=ROOT, env=env,
    )
    sys.exit(result.returncode)


def main():
    args = sys.argv[1:]

    # PARSE OUR FLAGS
    our_args = []
    pand_args = []
    benchmark_cmd = None
    benchmark_iter = 3
    clean_cmd_val = None

    i = 0
    while i < len(args):
        if args[i] == "--":
            pand_args = args[i + 1:]
            break
        elif args[i] == "--benchmark-cmd" and i + 1 < len(args):
            benchmark_cmd = args[i + 1]
            i += 2
            continue
        elif args[i] == "--benchmark-iter" and i + 1 < len(args):
            benchmark_iter = int(args[i + 1])
            i += 2
            continue
        elif args[i] == "--clean-cmd" and i + 1 < len(args):
            clean_cmd_val = args[i + 1]
            i += 2
            continue
        elif args[i] in ("--build", "--check", "--dmesg-only", "--help", "--benchmark", "--test", "--observe", "--bench-self", "--bench-mixed", "--bench-contention", "--calibrate"):
            our_args.append(args[i])
        else:
            pand_args.append(args[i])
        i += 1

    if "--help" in our_args:
        print("PANDEMONIUM v1.0.0 -- BUILD, RUN, BENCHMARK, REPORT")
        print()
        print("USAGE: python3 pandemonium.py [FLAGS] [-- PANDEMONIUM_ARGS]")
        print()
        print("FLAGS:")
        print("  --build                         FORCE REBUILD BEFORE RUNNING")
        print("  --check                         CHECK DEPENDENCIES ONLY")
        print("  --dmesg-only                    SHOW RECENT PANDEMONIUM DMESG AND EXIT")
        print("  --test                          RUN RUST TEST GATE (UNIT + INTEGRATION)")
        print("  --observe                       RUN WITH --verbose --dump-log (WATCH TIERS LIVE)")
        print("  --bench-self                    A/B BENCHMARK USING OWN COMPILATION AS WORKLOAD")
        print("  --bench-mixed                   A/B BENCHMARK: COMPILE WHILE AUDIO PLAYS (XRUNS)")
        print("  --bench-contention              A/B BENCHMARK: COMPILE + INTERACTIVE PROBE (LATENCY)")
        print("  --calibrate                     COLLECT LAT_CRI HISTOGRAM AND SUGGEST THRESHOLDS")
        print("  --benchmark                     RUN A/B BENCHMARK (EEVDF VS PANDEMONIUM)")
        print("  --benchmark-cmd 'CMD'           COMMAND TO BENCHMARK (REQUIRED WITH --benchmark)")
        print("  --benchmark-iter N              ITERATIONS PER SCHEDULER (DEFAULT: 3)")
        print("  --clean-cmd 'CMD'               RUN BEFORE EACH ITERATION (E.G. 'make clean')")
        print("  --help                          THIS MESSAGE")
        print()
        print("PANDEMONIUM ARGS (AFTER --):")
        print("  --build-mode              ENABLE BUILD-SYSTEM COMM-NAME BOOST (OFF BY DEFAULT)")
        print("  --slice-ns <NS>           BASE TIME SLICE IN NANOSECONDS (DEFAULT: 5000000)")
        print("  --slice-min <NS>          MIN SLICE (DEFAULT: 500000 = 0.5MS)")
        print("  --slice-max <NS>          MAX SLICE (DEFAULT: 20000000 = 20MS)")
        print("  --verbose                 PRINT VERBOSE OUTPUT")
        print("  --dump-log                DUMP FULL EVENT LOG ON EXIT")
        print()
        print("EXAMPLES:")
        print("  python3 pandemonium.py --build")
        print("  python3 pandemonium.py --observe                  WATCH TIER DISTRIBUTION LIVE")
        print("  python3 pandemonium.py --bench-self               A/B VS EEVDF (OWN BUILD)")
        print("  python3 pandemonium.py --benchmark --benchmark-cmd 'make -C /path -j$(nproc)'")
        sys.exit(0)

    check_deps()

    if "--check" in our_args:
        print("ALL DEPENDENCIES OK")
        sys.exit(0)

    if "--dmesg-only" in our_args:
        result = subprocess.run(
            ["journalctl", "-k", "--no-pager"],
            capture_output=True, text=True,
        )
        found = 0
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                low = line.lower()
                if any(kw in low for kw in ["sched_ext", "scx", "pandemonium"]):
                    print(line)
                    found += 1
        if found == 0:
            print("(NO SCHED_EXT/PANDEMONIUM MESSAGES IN KERNEL LOG)")
        sys.exit(0)

    # BUILD IF REQUESTED OR IF BINARY DOESN'T EXIST
    if "--build" in our_args or not os.path.exists(BINARY):
        build()

    # TEST GATE
    if "--test" in our_args:
        test_gate()
        sys.exit(0)

    # OBSERVE MODE: RUN WITH VERBOSE + DUMP-LOG
    if "--observe" in our_args:
        scheduler_output, dmesg, returncode = run(["--verbose", "--dump-log"] + pand_args)
        report(scheduler_output, dmesg, returncode)
        sys.exit(0)

    # MIXED WORKLOAD BENCHMARK: COMPILE WHILE AUDIO PLAYS
    if "--bench-mixed" in our_args:
        bench_mixed()
        sys.exit(0)

    # CONTENTION BENCHMARK: COMPILE + INTERACTIVE PROBE
    if "--bench-contention" in our_args:
        bench_contention()
        sys.exit(0)

    # CALIBRATE: COLLECT HISTOGRAM AND SUGGEST THRESHOLDS
    if "--calibrate" in our_args:
        if not os.path.exists(BINARY):
            print(f"ERROR: BINARY NOT FOUND AT {BINARY}")
            print("  RUN WITH --build FIRST")
            sys.exit(1)
        print("STARTING PANDEMONIUM IN CALIBRATION MODE...")
        scheduler_output, dmesg, returncode = run(["--calibrate"] + pand_args)
        sys.exit(0)

    # SELF-BENCHMARK: A/B USING OWN COMPILATION AS WORKLOAD
    if "--bench-self" in our_args:
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = TARGET_DIR
        build_cmd = f"CARGO_TARGET_DIR={TARGET_DIR} cargo build --release"
        clean_cmd_self = f"cargo clean --target-dir {TARGET_DIR}"
        iters = benchmark_iter if benchmark_iter != 3 else 3
        benchmark(build_cmd, iters, clean_cmd_self, sched_args=["--build-mode"])
        sys.exit(0)

    # BENCHMARK MODE
    if "--benchmark" in our_args:
        if not benchmark_cmd:
            print("ERROR: --benchmark REQUIRES --benchmark-cmd 'COMMAND'")
            sys.exit(1)
        benchmark(benchmark_cmd, benchmark_iter, clean_cmd_val)
        sys.exit(0)

    scheduler_output, dmesg, returncode = run(pand_args)
    report(scheduler_output, dmesg, returncode)


if __name__ == "__main__":
    main()
