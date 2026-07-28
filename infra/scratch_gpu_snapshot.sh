#!/usr/bin/env bash
# Task 124: read-only snapshot of GPU work in flight, for injection into a
# relaunched worker's resume prompt (and for humans debugging the queue).
#   Usage: bash scratch_gpu_snapshot.sh [worker_name]
# Reads gpu_queue/{running,pending,done}/ plus a pgrep sweep for driver-like
# processes (to catch work NOT going through the queue). With a worker_name it
# also reports scratch_full_logs/<worker>*.pid liveness. NEVER mutates anything:
# no killing, no queue writes — safe to run at any time, including mid-training.
cd /home/steven/Projects/time-series-omp
WORKER="${1:-}"
python3 - "$WORKER" <<'PY'
import glob, json, os, subprocess, sys, time

worker = sys.argv[1] if len(sys.argv) > 1 else ""
now = time.time()

def load(p):
    try:
        return json.loads(open(p).read())
    except Exception:
        return None

def fmt_t(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"

def clip(s, n=170):
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."

hdr = f"=== GPU QUEUE SNAPSHOT @ {fmt_t(now)}"
print(hdr + (f" (worker: {worker})" if worker else "") + " ===")

running = sorted(glob.glob("gpu_queue/running/*.json"))
pending = sorted(glob.glob("gpu_queue/pending/*.json"))  # job ids start with a ms timestamp -> lexicographic = FIFO
done_all = sorted(glob.glob("gpu_queue/done/*.json"), key=os.path.getmtime)
done = done_all[-10:]

print(f"RUNNING now under gpu_manager ({len(running)}):")
for p in running:
    j = load(p) or {}
    elapsed = int(now - os.path.getmtime(p))  # mtime = when gpu_manager claimed it
    print(f"  [{j.get('id', os.path.basename(p))}] elapsed ~{elapsed}s of timeout {int(j.get('timeout', 0))}s")
    print(f"      cmd: {clip(j.get('cmd', '?'))}")
if not running:
    print("  (none)")

print(f"PENDING (queued FIFO, {len(pending)}):")
for i, p in enumerate(pending, 1):
    j = load(p) or {}
    print(f"  {i}. [{j.get('id', os.path.basename(p))}] timeout {int(j.get('timeout', 0))}s")
    print(f"      cmd: {clip(j.get('cmd', '?'))}")
if not pending:
    print("  (none)")

print(f"RECENTLY FINISHED (last {len(done)} of {len(done_all)} in gpu_queue/done/, newest first):")
for p in reversed(done):
    j = load(p) or {}
    status = "TIMEOUT" if j.get("timed_out") else f"rc={j.get('exit_code')}"
    fin = j.get("finished_at", os.path.getmtime(p))
    print(f"  [{j.get('id', os.path.basename(p))}] {status} dur={j.get('duration_s', '?')}s finished {fmt_t(fin)}")
    print(f"      cmd: {clip(j.get('cmd', ''), 140)}")
if not done:
    print("  (none)")

# Driver-like processes, tagged by whether they run under gpu_manager's tree
# (queue-managed) or outside it (work that will NOT appear in gpu_queue/done/).
# claude worker processes are excluded: their -p prompt text often contains
# 'driver_*.py' strings, which would false-positive the cmdline match.
try:
    out = subprocess.run(
        ["pgrep", "-af", r"driver_.*\.py|scratch_cf_run_model|scratch_eval_dataset"],
        capture_output=True, text=True).stdout
except Exception:
    out = ""

def is_claude(cmd):
    head = cmd.split()[0] if cmd.split() else ""
    return os.path.basename(head) == "claude"

procs = [l for l in out.splitlines()
         if l.strip() and not is_claude(l.partition(" ")[2])]

# The manager tree = descendants of the 'gpu_manager' tmux pane. (Matching
# 'gpu_manager.py' cmdlines instead would also hit claude prompts that merely
# mention the filename.)
mgr_root = 0
try:
    pane = subprocess.run(["tmux", "list-panes", "-t", "=gpu_manager", "-F", "#{pane_pid}"],
                          capture_output=True, text=True).stdout.strip().splitlines()
    mgr_root = int(pane[0]) if pane else 0
except Exception:
    pass

def ppid_of(pid):
    try:
        return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception:
        return 0

def under_manager(pid):
    hops = 0
    while pid > 1 and hops < 25:
        if pid == mgr_root:
            return True
        pid = ppid_of(pid)
        hops += 1
    return False

print(f"DRIVER-LIKE PROCESSES (pgrep driver_*.py|scratch_cf_run_model|scratch_eval_dataset, {len(procs)}):")
for l in procs:
    pid_s, _, cmd = l.partition(" ")
    try:
        tag = "[via gpu_queue]" if mgr_root and under_manager(int(pid_s)) else "[OUTSIDE the queue - untracked!]"
    except Exception:
        tag = "[?]"
    print(f"  pid {pid_s} {tag} {clip(cmd, 150)}")
if not procs:
    print("  (none)")

if worker:
    pidfiles = sorted(glob.glob(f"scratch_full_logs/{worker}*.pid"))
    print(f"TRACKED PID FILES (scratch_full_logs/{worker}*.pid, {len(pidfiles)}):")
    for p in pidfiles:
        try:
            pid = int(open(p).read().split()[0])
            alive = os.path.exists(f"/proc/{pid}")
            print(f"  {os.path.basename(p)}: pid {pid} {'ALIVE' if alive else 'dead'}")
        except Exception:
            print(f"  {os.path.basename(p)}: unreadable")
    if not pidfiles:
        print("  (none)")

if not running and not pending:
    tail = " Recent finishes are listed above." if done else ""
    print("No GPU jobs pending/running (queue empty)." + tail)
PY
