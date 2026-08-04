#!/usr/bin/env bash
# Task 283: start (or verify) the always-on gpu_manager daemon — idempotent.
# Same durable pattern as scratch_jobmgr_start.sh: a detached tmux session that
# survives disconnects, wrapped in an auto-restart loop so the daemon never
# stays dead after a crash / process exit. (The Jul-25 outage was a bare
# `python gpu_manager.py` pane getting closed with nothing to bring it back —
# gpu_manager is not, and cannot cheaply be, tracked by scratch_watchdog.py.)
# gpu_manager.py re-queues any job stuck in running/ on startup, so restarting
# mid-job is safe.
#   Usage: bash scratch_gpu_manager_start.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"
if tmux has-session -t "=gpu_manager" 2>/dev/null; then
  echo "gpu_manager tmux session already running:"
  tmux list-panes -t "=gpu_manager" -F '  pane #{pane_pid} #{pane_current_command}'
  exit 0
fi
mkdir -p scratch_full_logs gpu_queue/pending gpu_queue/running gpu_queue/done gpu_queue/logs
tmux new-session -d -s gpu_manager "bash -lc 'source \"$HERE/_daemon_env.sh\" && while true; do python3 gpu_manager.py 2>&1 | tee -a scratch_full_logs/gpu_manager.log; echo \"[gpu_manager-wrapper] daemon exited rc=\$? \$(date) — restarting in 10s\" | tee -a scratch_full_logs/gpu_manager.log; sleep 10; done'"
sleep 2
if tmux has-session -t "=gpu_manager" 2>/dev/null; then
  echo "started gpu_manager tmux session (log: scratch_full_logs/gpu_manager.log)"
else
  echo "ERR: gpu_manager session did not come up" >&2
  exit 1
fi
