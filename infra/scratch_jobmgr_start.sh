#!/usr/bin/env bash
# Task 216 F1a: start (or verify) the always-on jobmgr daemon — idempotent.
# Same pattern as the watchdog/inbox sessions (detached tmux, survives
# disconnects) plus an auto-restart loop so a crash never leaves sleeping
# workers un-wakeable. The daemon itself holds a flock, so an accidental
# second copy exits immediately instead of double-delivering wakes.
#   Usage: bash scratch_jobmgr_start.sh
cd /home/steven/Projects/time-series-omp
if tmux has-session -t "=jobmgr" 2>/dev/null; then
  echo "jobmgr tmux session already running:"
  tmux list-panes -t "=jobmgr" -F '  pane #{pane_pid} #{pane_current_command}'
  exit 0
fi
mkdir -p scratch_full_logs/jobs
tmux new-session -d -s jobmgr "bash -lc 'source ~/anaconda3/etc/profile.d/conda.sh && conda activate tsomp && while true; do python3 scratch_jobmgr.py 2>&1 | tee -a scratch_full_logs/jobs/jobmgr_stdout.log; echo \"[jobmgr-wrapper] daemon exited rc=\$? \$(date) — restarting in 10s\" | tee -a scratch_full_logs/jobs/jobmgr_stdout.log; sleep 10; done'"
sleep 2
if tmux has-session -t "=jobmgr" 2>/dev/null; then
  echo "started jobmgr tmux session (log: scratch_full_logs/jobs/jobmgr.log)"
else
  echo "ERR: jobmgr session did not come up" >&2
  exit 1
fi
