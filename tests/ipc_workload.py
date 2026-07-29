# ipc_workload: the IPC latency engine. Relocated out of pandemonium_common (the
# shared scaffolding) because it is a workload kernel, not shared plumbing --
# consumed by prism-scale's measure_ipc (pandemonium-tests).
#
# The methodology that makes it trustworthy: ONE clean handoff pair per primitive
# looping a FIXED round count -- not many pairs contending and aggregated (the old
# prism-scale measure_ipc, whose cross-pair contention inflated p99/worst into
# noise). gc is disabled in the workload so the harness's own GC pauses don't
# pollute the latency tail; the processes rename themselves to IPC_COMM so
# `montauk --trace pand-ipc` can target them.
import signal
import subprocess
import sys
import time

from pandemonium_common import BINARY, percentile

IPC_COMM = "pand-ipc"
IPC_RTT_PRIMS = ["pipe", "socket", "eventfd", "sem"]
IPC_DEFAULT_ROUNDS = 20000
IPC_WARMUP_SECS = 2.0

_IPC_HEAD = (
    "import os,time,ctypes,ctypes.util,gc\n"
    "gc.disable()\n"
    "libc=ctypes.CDLL(ctypes.util.find_library('c'))\n"
    f"libc.prctl(15,b'{IPC_COMM}',0,0,0)\n"
)


def ipc_rtt_script(prim, rounds):
    """One handoff pair, `rounds` round-trips over `prim`; prints per-round us."""
    if prim == "pipe":
        setup = "r1,w1=os.pipe();r2,w2=os.pipe()\n"
        cwait, cwake = "os.read(r1,1)", "os.write(w2,b'x')"
        pwake, pwait = "os.write(w1,b'x')", "os.read(r2,1)"
    elif prim == "socket":
        setup = "import socket\nA,B=socket.socketpair()\n"
        cwait, cwake = "B.recv(1)", "B.send(b'x')"
        pwake, pwait = "A.send(b'x')", "A.recv(1)"
    elif prim == "eventfd":
        setup = "e1=os.eventfd(0);e2=os.eventfd(0)\n"
        cwait, cwake = "os.eventfd_read(e1)", "os.eventfd_write(e2,1)"
        pwake, pwait = "os.eventfd_write(e1,1)", "os.eventfd_read(e2)"
    elif prim == "sem":
        setup = "from multiprocessing import Semaphore\ns1=Semaphore(0);s2=Semaphore(0)\n"
        cwait, cwake = "s1.acquire()", "s2.release()"
        pwake, pwait = "s1.release()", "s2.acquire()"
    else:
        raise ValueError(prim)
    return (_IPC_HEAD + f"N={rounds}\n" + setup +
            "pid=os.fork()\n"
            "if pid==0:\n"
            " for _ in range(N):\n"
            f"  {cwait};{cwake}\n"
            " os._exit(0)\n"
            "lat=[]\n"
            "for _ in range(N):\n"
            f" t=time.monotonic();{pwake};{pwait}\n"
            " lat.append(time.monotonic()-t)\n"
            "os.waitpid(pid,0)\n"
            "import sys\n"
            "sys.stdout.write('\\n'.join(str(int(l*1e6)) for l in lat))\n")


def ipc_fanout_script(rounds, k):
    """1 parent -> K children; each round wakes all K and waits for all K
    (the 1:N server burst). Latency = burst-completion time."""
    return (_IPC_HEAD + f"R={rounds};K={k}\n"
            "P=[(os.pipe(),os.pipe()) for _ in range(K)]\n"
            "kids=[]\n"
            "for i in range(K):\n"
            " pid=os.fork()\n"
            " if pid==0:\n"
            "  (r1,w1),(r2,w2)=P[i]\n"
            "  for _ in range(R): os.read(r1,1);os.write(w2,b'x')\n"
            "  os._exit(0)\n"
            " kids.append(pid)\n"
            "lat=[]\n"
            "for _ in range(R):\n"
            " t=time.monotonic()\n"
            " for i in range(K): os.write(P[i][0][1],b'x')\n"
            " for i in range(K): os.read(P[i][1][0],1)\n"
            " lat.append(time.monotonic()-t)\n"
            "for p in kids: os.waitpid(p,0)\n"
            "import sys\n"
            "sys.stdout.write('\\n'.join(str(int(l*1e6)) for l in lat))\n")


def ipc_start_stress(cores, reserve=()):
    """Pin one non-yielding stress worker to each online CPU < cores, skipping
    any CPU in `reserve` (prism-ipc reserves cpu0 as montauk's drain core so it
    never drops events)."""
    workers = []
    for cpu in range(cores):
        if cpu in reserve:
            continue
        workers.append(subprocess.Popen(
            [str(BINARY), "stress-worker", "--cpu", str(cpu)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return workers


def ipc_stop_stress(workers):
    for w in workers:
        w.send_signal(signal.SIGINT)
    for w in workers:
        try:
            w.wait(timeout=5)
        except subprocess.TimeoutExpired:
            w.kill()
            w.wait()


def ipc_run_script(script, cpu_set=None):
    cmd = [sys.executable, "-c", script]
    if cpu_set is not None:
        cmd = ["taskset", "-c", cpu_set] + cmd
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=180)
        return out
    except subprocess.TimeoutExpired:
        p.kill()
        return ""


def ipc_lat_dist(out):
    v = [int(x) for x in out.split() if x.strip().lstrip("-").isdigit()]
    if not v:
        return None
    return {"n": len(v), "p50": int(percentile(v, 50)),
            "p99": int(percentile(v, 99)),
            "p999": int(percentile(v, 99.9)), "worst": max(v)}


def measure_ipc_cell(cores, rounds=IPC_DEFAULT_ROUNDS, prims=None,
                     fanout=True):
    """Clean per-primitive IPC RTT under stress at `cores`. One pair per
    primitive (not contending pairs), `rounds` each. Returns
    {rtt_<prim>: dist, 'fanout': dist}; dist = {n,p50,p99,p999,worst} or None."""
    if prims is None:
        prims = IPC_RTT_PRIMS
    cell = {}
    workers = ipc_start_stress(cores)
    time.sleep(IPC_WARMUP_SECS)
    try:
        for prim in prims:
            cell[f"rtt_{prim}"] = ipc_lat_dist(
                ipc_run_script(ipc_rtt_script(prim, rounds)))
        if fanout:
            cell["fanout"] = ipc_lat_dist(
                ipc_run_script(ipc_fanout_script(max(1, rounds // 4), cores)))
    finally:
        ipc_stop_stress(workers)
    return cell
