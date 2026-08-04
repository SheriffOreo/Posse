#!/usr/bin/env bash
# Spawn the PERSISTENT paper worker in tmux: resumes the paper-agent session
# (dfd9b9e8, full OMPGen-paper context) headless with a task spec, emails updates,
# and registers with the watchdog. Unlike scratch_spawn_worker.sh, this does NOT
# create a fresh session — the tmux worker IS the paper agent.
#
# Usage: scratch_spawn_paper_worker.sh <taskfile> [requester_email]
set -euo pipefail
cd /home/steven/Projects/time-series-omp
TASKFILE="${1:?task spec file}"; REQUESTER="${2:-${INFRA_OPERATOR_EMAIL:-}}"
[ -f "$TASKFILE" ] || { echo "ERR: taskfile not found: $TASKFILE" >&2; exit 1; }
SID="dfd9b9e8-f43e-4b93-9b61-a99cf5ea0ec1"
NAME="paper"
PROMPT="scratch_full_logs/worker_${NAME}_prompt.md"
LOG="scratch_full_logs/worker_${NAME}.log"

# One paper worker at a time: if it's already running, deliver via its mailbox.
if tmux has-session -t "$NAME" 2>/dev/null; then
  { echo "=== queued task delivered $(date) ==="; cat "$TASKFILE"; echo; } \
    >> "scratch_full_logs/inbox/mailbox_${NAME}.md"
  echo "paper worker already running -> task appended to its live mailbox"
  exit 0
fi

# Build the worker prompt = paper-specific discipline preamble + the task spec
{
  cat <<EOF
You are the PERSISTENT PAPER WORKER: the OMPGen SIGMOD paper agent resumed
headless in tmux (session name: paper). You have your full prior paper context.
The paper repo is /home/steven/Papers/Time_series_OMP (paper.tex + body/*.tex);
work there via absolute paths. Build with: pdflatex -interaction=nonstopmode paper.tex
(TinyTeX; run from the repo dir; use tlmgr to install any missing package).

WRITING RULES (non-negotiable):
- EVERY piece of text you add to the paper goes inside \claude{...} (light-red
  highlight macro in paper.tex). Never delete or rewrite collaborators' text —
  add alongside, or comment via \claude{} notes.
- soul caveats inside \claude{}: no control spaces ("vs.\ " -> "vs.\@ "); fragile
  macros must be \soulregister'ed (see paper.tex preamble).
- Verify the paper compiles after every edit batch.

CRITICAL: you are a headless \`claude -p\` process. There is NO background
re-invocation — run ALL work SYNCHRONOUSLY in the FOREGROUND and DO NOT end your
turn until every deliverable exists and you have sent the FINAL email.

Email the requester ($REQUESTER) via:
  python scratch_notify_email.py "<subject>" "<body>" --agent paper --to $REQUESTER [--attach <file>]
(the mailer adds greeting/signature — write just the body). Send: (a) a short
START/plan email now, (b) milestone updates, (c) a FINAL email with the result
(attach the built paper.pdf when the task changed the paper).

LIVE FEEDBACK: your session may be killed by the inbox router when the user
emails you. On ANY start/resume, FIRST run:
  MAILBOX=\$(bash scratch_read_mailbox.sh paper)
and if non-empty: read it, reply, and adjust the plan. Re-check the mailbox
between major steps.

When FULLY finished (FINAL email sent): touch scratch_full_logs/worker_paper.done
Guardrails: no git commit/push; never delete collaborator content; keep emails terse.

================================ TASK ================================
EOF
  cat "$TASKFILE"
} > "$PROMPT"

# Fresh run: clear the done sentinel, (re)register the watchdog job
rm -f "scratch_full_logs/worker_${NAME}.done"
python3 - "$REQUESTER" <<'PY'
import json, sys
from pathlib import Path
p = Path("scratch_full_logs/watchdog_jobs.json")
jobs = json.loads(p.read_text()) if p.exists() else []
job = {"name": "paper", "session": "dfd9b9e8-f43e-4b93-9b61-a99cf5ea0ec1",
       "log": "scratch_full_logs/worker_paper.log",
       "relaunch": "scratch_worker_paper_relaunch.sh",
       "done_sentinel": "scratch_full_logs/worker_paper.done",
       "requester": sys.argv[1], "state": "running",
       "reset_epoch": None, "relaunched": 0}
jobs = [j for j in jobs if j.get("name") != "paper"] + [job]
p.write_text(json.dumps(jobs, indent=2))
print("watchdog job registered: paper")
PY

echo "[paper] starting (SPAWN) $(date) task=$TASKFILE requester=$REQUESTER" >> "$LOG"
tmux new-session -d -s "$NAME" \
  "cd /home/steven/Projects/time-series-omp && source ~/anaconda3/etc/profile.d/conda.sh && conda activate tsomp && source /home/steven/Projects/time-series-omp/scratch_claude_auth.sh && export PATH=\"\$HOME/.npm-global/bin:\$PATH\" && claude --resume $SID -p \"\$(cat $PROMPT)\" --dangerously-skip-permissions --model fable --effort max --max-turns 800 --verbose >> $LOG 2>&1; echo \"[paper] EXITED rc=\$? \$(date)\" >> $LOG"
echo "paper worker launched in tmux session '$NAME' (resume $SID, model fable)"
