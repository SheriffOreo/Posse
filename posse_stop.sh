#!/usr/bin/env bash
# posse_stop.sh — stop your Posse's always-on services: the dashboard + the daemons
# (inbox, watchdog, jobmgr, sheriff, docket, and — with --gpu — gpu_manager). Each runs in its
# own tmux session; killing the session stops its auto-restart wrapper AND the process.
#
# Deputies and detached jobs run in their OWN tmux sessions and are LEFT RUNNING — a
# shutdown of the management plane should not kill work in flight. Stop those
# individually with:  tmux kill-session -t =<name>
#
#   bash posse_stop.sh            # stop dashboard + inbox + watchdog + jobmgr + sheriff + docket
#   bash posse_stop.sh --gpu      # also stop the GPU resource manager
#   bash posse_stop.sh --dry-run  # show what WOULD stop (changes nothing)
#   bash posse_stop.sh --help
set -uo pipefail

usage() {
  cat <<'EOF'
posse_stop.sh — stop your Posse's daemons + dashboard.

  bash posse_stop.sh            stop dashboard + inbox + watchdog + jobmgr + sheriff + docket
  bash posse_stop.sh --gpu      also stop the GPU resource manager
  bash posse_stop.sh --dry-run  show what WOULD stop; change nothing
  bash posse_stop.sh --help     this help

Deputies and detached jobs run in their own tmux sessions and are left running.
Start everything again with posse_start.sh.
EOF
}

WANT_GPU=0
DRY=0
for a in "$@"; do
  case "$a" in
    --gpu) WANT_GPU=1 ;;
    --dry-run|-n) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "posse_stop.sh: unknown argument '$a' (try --help)" >&2; exit 2 ;;
  esac
done

command -v tmux >/dev/null 2>&1 || { echo "tmux not found — nothing to stop." >&2; exit 0; }

SESSIONS=(infra_dashboard inbox watchdog jobmgr sheriff docket)
[ "$WANT_GPU" = 1 ] && SESSIONS+=(gpu_manager)

running() { tmux has-session -t "=$1" 2>/dev/null; }

suffix=""; [ "$DRY" = 1 ] && suffix=" (dry-run — nothing will change)"
echo "Stopping your Posse${suffix}..."

any_running=0
for s in "${SESSIONS[@]}"; do
  if running "$s"; then
    any_running=1
    if [ "$DRY" = 1 ]; then
      printf '  • %s: WOULD stop\n' "$s"
    elif tmux kill-session -t "=$s" 2>/dev/null; then
      printf '  ✓ %s: stopped\n' "$s"
    else
      printf '  ! %s: kill reported an error\n' "$s"
    fi
  else
    printf '  – %s: not running\n' "$s"
  fi
done
[ "$any_running" = 0 ] && echo "  (none of the daemon sessions were running)"

# Heads-up about sessions we deliberately leave alone (deputies / jobs / anything else).
others="$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
          | grep -Ev '^(infra_dashboard|inbox|watchdog|jobmgr|sheriff|docket|gpu_manager)$' || true)"
if [ -n "$others" ]; then
  echo
  echo "Left running (deputies / jobs / other tmux sessions):"
  echo "$others" | sed 's/^/  - /'
  echo "Stop one with:  tmux kill-session -t =<name>"
fi

echo
echo "Start again with:  bash posse_start.sh"
