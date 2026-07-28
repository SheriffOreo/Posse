#!/usr/bin/env python3
"""GPU job manager — run THIS in your own (GPU-enabled) terminal.

Why: Claude Code's tool sandbox cannot open a CUDA context (cuInit ->
CUDA_ERROR_NO_DEVICE), so agents cannot run on the GPU directly. Instead they
SUBMIT jobs (via submit_gpu.py) into gpu_queue/pending/. This manager — running
in your normal shell, which DOES see the Ada GPU — executes them strictly one
at a time on GPU 0 and writes results to gpu_queue/done/.

Usage:
    conda activate tsomp
    python gpu_manager.py                 # serial, GPU 0
    python gpu_manager.py --gpu 0 --poll 1.0

It loops forever; Ctrl-C to stop. FIFO by submit time. One job at a time =>
respects the single-GPU constraint automatically.
"""
import argparse, json, os, signal, subprocess, sys, time, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
Q = ROOT / "gpu_queue"
PENDING, RUNNING, DONE, LOGS = Q/"pending", Q/"running", Q/"done", Q/"logs"


def setup():
    for d in (PENDING, RUNNING, DONE, LOGS):
        d.mkdir(parents=True, exist_ok=True)


def claim(job_path: Path):
    """Atomically move a pending job into running/ (skip if another took it)."""
    target = RUNNING / job_path.name
    try:
        job_path.rename(target)
        return target
    except (FileNotFoundError, OSError):
        return None


def _kill_group(proc):
    """Kill the subprocess's whole process group (SIGTERM then SIGKILL) so a
    timed-out job's GPU grandchildren don't survive as orphans holding memory."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def run_job(job_path: Path, gpu: str):
    job = json.loads(job_path.read_text())
    jid = job["id"]
    cmd = job["cmd"]                       # shell string
    cwd = job.get("cwd", str(ROOT))
    timeout = job.get("timeout", 3600)
    extra_env = job.get("env", {}) or {}

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)  # GPU by default, single device
    env.update({k: str(v) for k, v in extra_env.items()})

    out_log = LOGS / f"{jid}.out"
    err_log = LOGS / f"{jid}.err"
    print(f"[manager] RUN {jid}: {cmd}  (cwd={cwd}, timeout={timeout}s)", flush=True)
    t0 = time.time()
    rc, timed_out = None, False
    with open(out_log, "w") as o, open(err_log, "w") as e:
        # start_new_session=True isolates the shell + its GPU grandchildren in
        # their own process group so a timeout can kill the WHOLE tree; plain
        # subprocess.run(timeout=) only SIGKILLs the `sh -c` wrapper, leaving the
        # real training process orphaned and still holding GPU memory.
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env,
                                stdout=o, stderr=e, start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, rc = True, 124
            _kill_group(proc)
    dur = round(time.time() - t0, 1)

    def tail(p, n=40):
        try:
            return "".join(open(p).read().splitlines(keepends=True)[-n:])
        except Exception:
            return ""

    result = {
        "id": jid, "exit_code": rc, "timed_out": timed_out, "duration_s": dur,
        "cmd": cmd, "cwd": cwd, "out_log": str(out_log), "err_log": str(err_log),
        "stdout_tail": tail(out_log), "stderr_tail": tail(err_log),
        "finished_at": time.time(),
    }
    # atomic publish: tmp then rename
    tmp = DONE / f".{jid}.json.tmp"
    tmp.write_text(json.dumps(result, indent=2))
    tmp.rename(DONE / f"{jid}.json")
    job_path.unlink(missing_ok=True)
    status = "TIMEOUT" if timed_out else f"rc={rc}"
    print(f"[manager] DONE {jid}: {status} in {dur}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", default="0", help="CUDA device index to expose")
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args()
    setup()
    # Recover any jobs stuck in running/ from a previous crash -> requeue.
    for stuck in RUNNING.glob("*.json"):
        stuck.rename(PENDING / stuck.name)
    print(f"[manager] watching {PENDING} on GPU {args.gpu}. Ctrl-C to stop.", flush=True)
    try:
        import torch
        print(f"[manager] torch.cuda.is_available()={torch.cuda.is_available()} "
              f"device={'n/a' if not torch.cuda.is_available() else torch.cuda.get_device_name(0)}",
              flush=True)
    except Exception as ex:
        print(f"[manager] (torch check skipped: {ex})", flush=True)
    while True:
        try:
            jobs = sorted(PENDING.glob("*.json"))
            if not jobs:
                time.sleep(args.poll); continue
            claimed = claim(jobs[0])
            if claimed:
                run_job(claimed, args.gpu)
        except Exception:
            # One bad job must never kill the daemon: log + keep serving. A job
            # that was claimed stays quarantined in running/ and is requeued on
            # the next daemon restart (see the running/ recovery above).
            traceback.print_exc()
            print("[manager] ERROR in main loop (continuing)", flush=True)
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
