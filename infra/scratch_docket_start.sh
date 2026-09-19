#!/usr/bin/env bash
# Start (or verify) the docket runner — idempotent, same shape as the jobmgr and
# sheriff starters. The runner is what makes a scheduled entry actually fire: it
# calls `scratch_docket.py run` once a minute, which drops a fresh web-case or JTF
# record for whatever is due. Without it the Docket tab still saves entries, but
# nothing ever launches from them.
#
# `run` takes a non-blocking lock of its own, so an overlapping pass exits rather
# than piling up, and a missed occurrence older than six hours is skipped instead
# of fired late.
#   Usage: bash scratch_docket_start.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"
if tmux has-session -t "=docket" 2>/dev/null; then
  echo "docket tmux session already running:"
  tmux list-panes -t "=docket" -F '  pane #{pane_pid} #{pane_current_command}'
  exit 0
fi
mkdir -p scratch_full_logs/docket
tmux new-session -d -s docket "bash -lc 'source \"$HERE/_daemon_env.sh\" && while true; do python3 scratch_docket.py run >> scratch_full_logs/docket/runner.log 2>&1; sleep 60; done'"
sleep 2
if tmux has-session -t "=docket" 2>/dev/null; then
  echo "started docket tmux session (log: scratch_full_logs/docket/runner.log)"
else
  echo "ERR: docket session did not come up" >&2
  exit 1
fi
