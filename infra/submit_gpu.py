#!/usr/bin/env python3
"""Submit a command to the GPU manager and block for its result.

Agents run inside a sandbox WITHOUT GPU access, so they must NOT run training
directly. Instead they call this to enqueue the command; gpu_manager.py (running
in the user's GPU-enabled terminal) executes it on the Ada GPU, one job at a
time, and writes the result back here.

CLI:
    python submit_gpu.py --cmd "python driver_timegan.py --data .../noaa --epochs 2 --time 300" \
                         --timeout 1800
Exits with the job's own exit code; prints stdout/stderr tails.

Importable:
    from submit_gpu import submit
    res = submit("python driver_x.py ...", timeout=1800)   # dict result
"""
import argparse, json, os, secrets, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
Q = ROOT / "gpu_queue"
PENDING, DONE = Q/"pending", Q/"done"


def submit(cmd, cwd=None, timeout=3600, env=None, wait_timeout=None, poll=2.0):
    """Enqueue cmd; block until the manager finishes it. Returns result dict.

    timeout      : per-job wall-clock limit enforced by the manager.
    wait_timeout : how long submit() waits for a result before giving up
                   (None => timeout + 600s slack).
    """
    PENDING.mkdir(parents=True, exist_ok=True)
    DONE.mkdir(parents=True, exist_ok=True)
    jid = f"{int(time.time()*1000)}_{secrets.token_hex(3)}"
    job = {"id": jid, "cmd": cmd, "cwd": cwd or str(ROOT),
           "timeout": timeout, "env": env or {}}
    tmp = PENDING / f".{jid}.json.tmp"
    tmp.write_text(json.dumps(job))
    tmp.rename(PENDING / f"{jid}.json")           # atomic enqueue

    if wait_timeout is None:
        wait_timeout = timeout + 600
    result_path = DONE / f"{jid}.json"
    deadline = time.time() + wait_timeout
    waited_warned = False
    while time.time() < deadline:
        if result_path.exists():
            return json.loads(result_path.read_text())
        if not waited_warned and time.time() > deadline - wait_timeout + 30:
            # one nudge if nothing has picked it up promptly
            if (PENDING / f"{jid}.json").exists():
                print("[submit] waiting for gpu_manager.py to pick up the job... "
                      "is it running in your terminal?", file=sys.stderr, flush=True)
            waited_warned = True
        time.sleep(poll)
    raise TimeoutError(f"No result for job {jid} within {wait_timeout}s. "
                       f"Is gpu_manager.py running?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--wait-timeout", type=float, default=None)
    args = ap.parse_args()
    res = submit(args.cmd, cwd=args.cwd, timeout=args.timeout,
                 wait_timeout=args.wait_timeout)
    print(f"=== job {res['id']} rc={res['exit_code']} "
          f"timed_out={res['timed_out']} dur={res['duration_s']}s ===")
    print("--- stdout tail ---\n" + (res.get("stdout_tail") or ""))
    if res.get("exit_code"):
        print("--- stderr tail ---\n" + (res.get("stderr_tail") or ""), file=sys.stderr)
    sys.exit(res["exit_code"] if res["exit_code"] is not None else 1)


if __name__ == "__main__":
    main()
