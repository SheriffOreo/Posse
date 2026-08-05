#!/usr/bin/env bash
# Task 185: read-only snapshot of LIVE worker tmux sessions, for injection into
# a relaunched worker's resume prompt (companion to scratch_gpu_snapshot.sh and
# scratch_cpu_snapshot.sh).
#   Usage: bash scratch_subagent_snapshot.sh [worker_name]
# Why: sub-workers spawned via scratch_spawn_worker.sh run as their OWN tmux
# sessions under the tmux server — a surgical interrupt of the parent (which
# signals only the parent's pane shell + claude) structurally cannot touch
# them, and even the fallback `tmux kill-session -t =<parent>` kills only that
# one session. This snapshot lists every live registered worker session so a
# resumed parent RECONCILES with its still-running subagents instead of
# re-spawning duplicates. NEVER mutates anything.
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
WORKER="${1:-}"
python3 - "$WORKER" <<'PY'
import json, os, subprocess, sys, time

worker = sys.argv[1] if len(sys.argv) > 1 else ""
now = time.time()
INFRA = {"inbox", "watchdog", "gpu_manager"}

def fmt_t(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"

def ago(ts):
    try:
        d = int(now - float(ts))
    except Exception:
        return "?"
    if d < 90: return f"{d}s"
    if d < 5400: return f"{d//60}m"
    return f"{d//3600}h{(d%3600)//60:02d}m"

try:
    out = subprocess.run(["tmux", "ls", "-F", "#{session_name}\t#{session_created}"],
                         capture_output=True, text=True, timeout=10).stdout
except Exception:
    out = ""
sessions = []
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) == 2:
        sessions.append((parts[0], parts[1]))

jobs = {}
try:
    for j in json.load(open("scratch_full_logs/watchdog_jobs.json")):
        jobs[j.get("name")] = j
except Exception:
    pass

print(f"=== LIVE WORKER SESSIONS @ {fmt_t(now)}"
      + (f" (snapshot for worker: {worker})" if worker else "") + " ===")
shown = 0
for name, created in sorted(sessions):
    if name in INFRA or name == worker:
        continue
    j = jobs.get(name, {})
    state = j.get("state", "unregistered")
    sentinel = "yes" if os.path.exists(f"scratch_full_logs/worker_{name}.done") else "no"
    logf = f"scratch_full_logs/worker_{name}.log"
    logstr = f"log write {ago(os.path.getmtime(logf))} ago" if os.path.exists(logf) else "no log"
    mb = f"scratch_full_logs/inbox/mailbox_{name}.md"
    try:
        mbsz = os.path.getsize(mb)
    except OSError:
        mbsz = 0
    mbstr = f", {mbsz}B unread mail" if mbsz else ""
    print(f"  {name:<22} up {ago(created)} (since {fmt_t(created)})  watchdog={state}  done-sentinel={sentinel}  {logstr}{mbstr}")
    shown += 1
if not shown:
    print("  (no other worker sessions are live)")
print("Any of these that YOU spawned (scratch_spawn_worker.sh <sub_name> ...) is still")
print("running its task — do NOT re-spawn it; check its log / emails and coordinate.")
PY
