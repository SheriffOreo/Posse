#!/usr/bin/env bash
# Task 384 (Feng uid=384): spawn an ANONYMOUS worker — a helper agent a DEPUTY owns
# and manages entirely by itself. Unlike a sub-deputy (scratch_spawn_worker.sh), an
# anonymous worker:
#   * has NO case/task number and files NO paperwork (no precinct log/ledger/case file);
#   * is NOT registered with the sheriff/watchdog/registry, so the sheriff does NOT
#     monitor it and it never shows on the status board;
#   * reports ONLY to its launching deputy (it does not email the user);
#   * runs a prompt the deputy fully controls (a prompt file the deputy wrote).
# The launching DEPUTY is responsible for monitoring it (poll its log / tmux, or submit
# it to the job manager) and RELAUNCHING it if it crashes (just re-run this script or
# the generated launch script). The canonical example is a CRITIC agent.
#
#   Usage: scratch_spawn_anon.sh <name> <prompt_file> [--model M] [--max-turns N] [--dry]
#     <name>        short label ([a-z0-9_-]); the tmux session is anon_<name>
#     <prompt_file> the deputy-authored prompt (fed to claude via STDIN)
#     --model       fable|opus|sonnet|haiku (default opus)
#     --max-turns   claude max turns (default 60)
#     --dry         validate + print the plan; do not launch
#
# The anonymous worker's stdout streams to scratch_full_logs/anon_<name>.log — the
# deputy reads that (and/or a result file the deputy told it to write). Relaunch by
# re-running scratch_anon_<name>_launch.sh (regenerated here each spawn).
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root

NAME=""; PROMPT_FILE=""; MODEL="opus"; MAXTURNS="60"; DRY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:?}"; shift 2;;
    --max-turns) MAXTURNS="${2:?}"; shift 2;;
    --dry) DRY=1; shift;;
    *) if [ -z "$NAME" ]; then NAME="$1"; elif [ -z "$PROMPT_FILE" ]; then PROMPT_FILE="$1"; fi; shift;;
  esac
done
[ -n "$NAME" ] || { echo "ERR: <name> required" >&2; exit 1; }
[ -n "$PROMPT_FILE" ] || { echo "ERR: <prompt_file> required" >&2; exit 1; }
[ -f "$PROMPT_FILE" ] || { echo "ERR: prompt file not found: $PROMPT_FILE" >&2; exit 1; }
NAME="$(echo "$NAME" | tr 'A-Z ' 'a-z_' | tr -cd 'a-z0-9_-')"
case "$MODEL" in fable|opus|sonnet|haiku) ;; *) MODEL="opus" ;; esac

SESSION="anon_${NAME}"
SID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
LOG="scratch_full_logs/anon_${NAME}.log"
LAUNCH="scratch_anon_${NAME}_launch.sh"
# Snapshot the prompt so a relaunch is reproducible even if the deputy edits/removes
# the original (kept beside the launch script; NOT under inbox/ — no paperwork).
PROMPT_SNAP="scratch_full_logs/anon_${NAME}_prompt.md"

if [ -n "$DRY" ]; then
  echo "[dry] anonymous worker '$NAME' session=$SESSION sid=${SID:0:8} model=$MODEL max-turns=$MAXTURNS"
  echo "[dry] prompt=$PROMPT_FILE ($(wc -l < "$PROMPT_FILE") lines) -> snapshot $PROMPT_SNAP; log=$LOG; launch=$LAUNCH"
  echo "[dry] NOT registered (no watchdog/registry/precinct/active-deputies); deputy owns monitoring+relaunch"
  exit 0
fi

cp -f "$PROMPT_FILE" "$PROMPT_SNAP"
cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
# Anonymous worker '$NAME' launcher (Task 384). Deputy-owned; NOT watchdog-tracked.
# Portable runtime env (cd into infra/, optional conda env, Claude auth, claude on PATH):
source "\$(cd "\$(dirname "\${BASH_SOURCE[0]:-\$0}")" && pwd)/_daemon_env.sh"
echo "[anon:$NAME] starting \$(date) session=$SID model=$MODEL" >> $LOG
claude --session-id "$SID" -p --dangerously-skip-permissions --model $MODEL \\
  --max-turns $MAXTURNS --verbose < "$PROMPT_SNAP" >> $LOG 2>&1
echo "[anon:$NAME] EXITED rc=\$? \$(date)" >> $LOG
EOF
chmod +x "$LAUNCH"
: > "$LOG"
tmux new-session -d -s "$SESSION" "bash $LAUNCH"
echo "spawned ANONYMOUS worker '$NAME' (tmux $SESSION, session ${SID:0:8}, model $MODEL)"
echo "  monitor: tmux has-session -t '=$SESSION'  |  tail -f $LOG"
echo "  relaunch if it dies: bash $LAUNCH   (you own it — the sheriff/watchdog do NOT)"
