#!/usr/bin/env bash
# Task 372: start (or verify) the always-on SHERIFF daemon — idempotent.
# Same pattern as the jobmgr/watchdog sessions (detached tmux, survives
# disconnects) plus an auto-restart loop so a crash never leaves ledgers
# ungoverned. The daemon itself holds an flock, so an accidental second copy
# exits immediately instead of double-compacting.
#   Usage: bash scratch_sheriff_start.sh
# NOTE: the sheriff is NOT watchdog-tracked (like jobmgr/gpu_manager) — restart it
# only via THIS idempotent starter, never by hand.
# Task 376: the sheriff's A/B thresholds are in TOKENS now (A=20000 soft limit >
# B=10000 target, code defaults in scratch_sheriff.py). To override, export
# TSOMP_SHERIFF_A / TSOMP_SHERIFF_B (TOKENS, A must be > B) before starting.
#
# Task 384c / Phase A1 — the Sheriff is the single SYSTEM MANAGER. It owns TWO
# always-on loops:
#   (1) precinct RECORDS-HEALTH  — THIS daemon (scratch_sheriff.py): ledger
#       compaction + the deputy->sheriff change-request queue. Started/verified here.
#   (2) DEPUTY SUPERVISION       — the watchdog (scratch_watchdog.py): deputy
#       liveness / crash / usage-limit recovery + relaunch.
# A1 is presentation + ownership ONLY. This starter REPORTS the watchdog loop's
# liveness (read-only) so the system manager surfaces both loops, but it does NOT
# start/restart the watchdog. The watchdog is the most safety-critical daemon we have
# (it prevented the $10k incident) and — unlike this sheriff / jobmgr — holds NO
# single-instance flock, so a second copy would double-run (duplicate relaunches +
# emails). Never launch it from here; if it is down, bring it up deliberately with its
# own command (printed below).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$HERE"

# Read-only liveness report for the OTHER loop the system manager owns (the watchdog).
# Purely informational — starts/kills nothing.
report_watchdog() {
  if tmux has-session -t "=watchdog" 2>/dev/null; then
    echo "system-manager: deputy-supervision loop (watchdog) is UP"
  else
    echo "system-manager: WARNING — deputy-supervision loop (watchdog) is DOWN." >&2
    echo "  NOT auto-started here (safety-critical, no flock). Bring it up deliberately:" >&2
    echo "  tmux new-session -d -s watchdog \"bash -lc 'source \\\"$HERE/_daemon_env.sh\\\" && python3 scratch_watchdog.py 2>&1 | tee -a scratch_full_logs/watchdog_stdout.log'\"" >&2
  fi
}

if tmux has-session -t "=sheriff" 2>/dev/null; then
  echo "sheriff tmux session already running:"
  tmux list-panes -t "=sheriff" -F '  pane #{pane_pid} #{pane_current_command}'
  report_watchdog
  exit 0
fi
mkdir -p scratch_full_logs/sheriff
tmux new-session -d -s sheriff "bash -lc 'source \"$HERE/_daemon_env.sh\" && while true; do python3 scratch_sheriff.py 2>&1 | tee -a scratch_full_logs/sheriff/sheriff_stdout.log; echo \"[sheriff-wrapper] daemon exited rc=\$? \$(date) — restarting in 10s\" | tee -a scratch_full_logs/sheriff/sheriff_stdout.log; sleep 10; done'"
sleep 2
if tmux has-session -t "=sheriff" 2>/dev/null; then
  echo "started sheriff tmux session (log: scratch_full_logs/sheriff/sheriff.log)"
  report_watchdog
else
  echo "ERR: sheriff session did not come up" >&2
  exit 1
fi
