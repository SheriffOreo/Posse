#!/usr/bin/env bash
# posse_start.sh — start (or verify) your whole Posse: the always-on daemons + the
# dashboard. IDEMPOTENT: every service is guarded by a tmux session check, so
# re-running never spawns a duplicate — it just reports what is already up. Run it
# after a reboot, after editing config, or any time to confirm the system is healthy.
#
#   bash posse_start.sh            # start or verify: jobmgr, sheriff, inbox, watchdog, dashboard
#   bash posse_start.sh --gpu      # also start the optional GPU resource manager
#   bash posse_start.sh --dry-run  # show what WOULD start (changes nothing)
#   bash posse_start.sh --help
#
# Config is read from infra_env.local.sh (written by setup.py) if present, else the
# tracked infra_env.sh template. Stop everything again with posse_stop.sh.
set -uo pipefail

usage() {
  cat <<'EOF'
posse_start.sh — start (or verify) your whole Posse (idempotent).

  bash posse_start.sh            start or verify: jobmgr, sheriff, inbox, watchdog, dashboard
  bash posse_start.sh --gpu      also start the optional GPU resource manager
  bash posse_start.sh --dry-run  show what WOULD start; change nothing
  bash posse_start.sh --help     this help

Re-running is safe: each service is guarded by a tmux session check, so nothing is
started twice. Stop everything with posse_stop.sh.
EOF
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
INFRA="$HERE/infra"
DASH="$HERE/dashboard"

WANT_GPU=0
DRY=0
for a in "$@"; do
  case "$a" in
    --gpu) WANT_GPU=1 ;;
    --dry-run|-n) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "posse_start.sh: unknown argument '$a' (try --help)" >&2; exit 2 ;;
  esac
done

command -v tmux >/dev/null 2>&1 || {
  echo "ERR: tmux not found — install it first (see 'python3 setup.py' step 1)." >&2
  exit 1
}

# Load per-machine config: prefer the local file setup.py writes, else the template.
if   [ -f "$HERE/infra_env.local.sh" ]; then . "$HERE/infra_env.local.sh"
elif [ -f "$HERE/infra_env.sh" ];       then . "$HERE/infra_env.sh"
fi

running() { tmux has-session -t "=$1" 2>/dev/null; }
say()     { printf '  %s\n' "$*"; }

suffix=""; [ "$DRY" = 1 ] && suffix=" (dry-run — nothing will change)"
echo "Starting your Posse${suffix}..."

# 1) jobmgr + 2) sheriff — idempotent, flock-guarded starters (they self-verify).
for pair in "jobmgr:scratch_jobmgr_start.sh" "sheriff:scratch_sheriff_start.sh"; do
  name="${pair%%:*}"; script="${pair##*:}"
  if [ "$DRY" = 1 ]; then
    if running "$name"; then say "✓ $name: already running"; else say "• $name: WOULD start ($script)"; fi
  else
    bash "$INFRA/$script" 2>&1 | sed 's/^/    /'
  fi
done

# 3) GPU resource manager — optional (only with --gpu).
if [ "$WANT_GPU" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    if running gpu_manager; then say "✓ gpu_manager: already running"; else say "• gpu_manager: WOULD start"; fi
  else
    bash "$INFRA/scratch_gpu_manager_start.sh" 2>&1 | sed 's/^/    /'
  fi
fi

# 4) inbox + 5) watchdog — tmux-loop daemons; start only if their session is down.
# (The watchdog holds no flock — a second copy would double-relaunch — so it MUST be
#  guarded by the has-session check, exactly like setup.py does.)
start_loop() {  # session  inner-command
  local session="$1" inner="$2"
  if running "$session"; then say "✓ $session: already running"; return; fi
  if [ "$DRY" = 1 ]; then say "• $session: WOULD start (tmux -s $session)"; return; fi
  if tmux new-session -d -s "$session" "bash -lc '$inner'"; then
    say "✓ $session: started (tmux '$session')"
  else
    say "! $session: failed to start"
  fi
}
start_loop inbox    "bash \"$INFRA/scratch_inbox_loop.sh\""
start_loop watchdog "source \"$INFRA/_daemon_env.sh\" && python3 scratch_watchdog.py 2>&1 | tee -a scratch_full_logs/watchdog_stdout.log"

# 6) dashboard — its own idempotent starter (honors INFRA_DASH_PUBLIC/HOST/PORT).
if [ "$DRY" = 1 ]; then
  if running infra_dashboard; then say "✓ dashboard: already running"; else say "• dashboard: WOULD start (start_dashboard.sh)"; fi
else
  bash "$DASH/start_dashboard.sh" 2>&1 | sed 's/^/    /'
fi

echo
if [ "$DRY" = 1 ]; then
  echo "Dry-run only — nothing was changed. Re-run without --dry-run to start."
else
  echo "Verify:      tmux ls    and    curl -sk http://localhost:${INFRA_DASH_PORT:-8787}/healthz"
  echo "Stop it all: bash posse_stop.sh"
fi
