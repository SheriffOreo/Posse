#!/usr/bin/env bash
# Launch (or verify) the claude_infra dashboard in its OWN detached tmux session,
# wrapped in a while-true auto-restart loop so it survives a crash / process exit.
# Idempotent: safe to run repeatedly (mirrors scratch_gpu_manager_start.sh).
#
# NOTE: this is NOT one of the always-on infra daemons (watchdog / jobmgr / inbox /
# gpu_manager) and it does not touch them — it is a separate, read-only viewer
# service on its own session name. The server is pure Python stdlib, so it needs
# only a python3 (no conda env).
#
#   Usage: bash start_dashboard.sh
set -euo pipefail
SESSION="infra_dashboard"
HERE="$(cd "$(dirname "$0")" && pwd)"

export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-/home/steven/Projects/time-series-omp}"
export INFRA_DASH_HOST="${INFRA_DASH_HOST:-127.0.0.1}"
export INFRA_DASH_PORT="${INFRA_DASH_PORT:-8787}"
PY="${PYTHON:-python3}"
LOG="$HERE/instance/dashboard.log"

if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "dashboard tmux session '$SESSION' already running:"
  tmux list-panes -t "=$SESSION" -F '  pane #{pane_pid} #{pane_current_command}'
  echo "  URL: http://$INFRA_DASH_HOST:$INFRA_DASH_PORT"
  exit 0
fi
mkdir -p "$HERE/instance"

tmux new-session -d -s "$SESSION" \
  "bash -lc 'cd \"$HERE\" && export INFRA_STATE_ROOT=\"$INFRA_STATE_ROOT\" INFRA_DASH_HOST=\"$INFRA_DASH_HOST\" INFRA_DASH_PORT=\"$INFRA_DASH_PORT\"; while true; do \"$PY\" server.py 2>&1 | tee -a \"$LOG\"; echo \"[dashboard-wrapper] server exited rc=\$? \$(date) — restarting in 5s\" | tee -a \"$LOG\"; sleep 5; done'"
sleep 2
if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "started dashboard tmux session '$SESSION' (log: $LOG)"
  echo "  URL:    http://$INFRA_DASH_HOST:$INFRA_DASH_PORT"
  echo "  tunnel: ssh -L $INFRA_DASH_PORT:localhost:$INFRA_DASH_PORT <this-host>"
else
  echo "ERR: dashboard session '$SESSION' did not come up" >&2
  exit 1
fi
