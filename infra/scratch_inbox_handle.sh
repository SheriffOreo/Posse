#!/usr/bin/env bash
# Handle ONE routed reply: $1=agent $2=type(resume|general) $3=session $4=bodyfile $5=uid
# Resumes the owning worker (resume) or runs a fresh handler (general); the
# worker/handler emails the user back. Runs claude to completion (backgrounded by
# the loop). Stays within guardrails.
# Task 123 hardening:
#   - handlers are SERIALIZED per agent with flock (two concurrent
#     `claude --resume` on one session race the transcript);
#   - claude's real rc is captured via PIPESTATUS (was: rc of tee);
#   - usage/session-limit -> defer until reset (as before); any OTHER failure ->
#     the uid is marked UNREAD + deferred 15 min (max 3 retries, then a
#     dead-letter alert email) instead of being silently lost as "seen".
cd /home/steven/Projects/time-series-omp
source ~/anaconda3/etc/profile.d/conda.sh
conda activate tsomp
# Task 223: auth switch (default = Claude Max subscription, not the API key).
source /home/steven/Projects/time-series-omp/scratch_claude_auth.sh
export PATH="$HOME/.npm-global/bin:$PATH"
AGENT="$1"; TYPE="$2"; SESSION="$3"; BODY="$(cat "$4" 2>/dev/null)"; UID_M="$5"
LOG=scratch_full_logs/inbox_agent.log
OUTF="scratch_full_logs/inbox/last_handle_${UID_M:-x}.out"
# Per-worker model override from the registry (e.g. "model": "fable" for sessions
# whose context exceeds the default model's window). Defaults to opus.
MODEL=$(python3 -c "import json;print(json.load(open('scratch_agents_registry.json'))['workers'].get('$AGENT',{}).get('model') or 'opus')" 2>/dev/null || echo opus)
# Reply back to whoever sent the reply (parsed from the "FROM:" line of the body file).
SENDER="$(sed -n 's/^FROM:[[:space:]]*//p' "$4" 2>/dev/null | head -1)"
case "$SENDER" in stevenfd@cmu.edu|fenghaod@andrew.cmu.edu) ;; *) SENDER="" ;; esac

# Serialize handlers per agent (wait up to 30 min; then defer instead of racing).
exec 9>"scratch_full_logs/inbox/handle_${AGENT}.lock"
if ! flock -w 1800 9; then
  echo "[inbox] $(date) handler lock timeout agent=$AGENT uid=$UID_M -> defer 15 min" >> $LOG
  [ -n "$UID_M" ] && python scratch_inbox.py defer --uid "$UID_M" --reset "resets $(date -d '+15 minutes' '+%-I:%M%p')" >> $LOG 2>&1
  exit 1
fi

read -r -d '' COMMON <<EOF
You are TRIAGING an email reply from the user (allowed senders: stevenfd@cmu.edu / fenghaod@andrew.cmu.edu) to an automated update from the "$AGENT" worker of the time-series-omp project (/home/steven/Projects/time-series-omp). This reply is from: ${SENDER:-the user}. Act RIGHT AWAY and address all email back to the sender.
Reply by email with: python scratch_notify_email.py "Re: <subject>" "<body>" --agent $AGENT${SENDER:+ --to $SENDER}  (attach a figure with --attach if helpful). The mailer auto-greets Steven ("Hi Steven,") and signs your agent name — write just the body, no greeting/signature.

Decide which case this message is, then act:

(1) QUESTION / status / small clarification (no real work, answerable by reading files): answer concisely and email the answer NOW. Done.

(2) A TASK requiring actual work — running code, scoring/training, generating files or figures, multi-step analysis, anything that takes more than a moment: DO NOT do the work yourself in this turn (you are a one-shot handler and will exit — the work would die). Instead:
   a. Write a SELF-CONTAINED task spec to scratch_full_logs/inbox/task_${UID_M:-x}.md — concrete steps, exact input paths, deliverables, output locations, constraints/lane, and that it must email ${SENDER:-the requester} a plan + milestone + FINAL update. Reuse known context (datasets in outputs_sweep/, dataset/noaa, eval/ framework, reports/).
   b. Launch a PERSISTENT working agent to own it:
        bash scratch_spawn_worker.sh <short_name> scratch_full_logs/inbox/task_${UID_M:-x}.md ${SENDER:-stevenfd@cmu.edu} "<comma,keywords>"
      Pick a short lowercase <short_name> (e.g. task_daily_rerun) and routing keywords from the task.
   c. Email ${SENDER:-the requester} a brief ACK + plan NOW: say a dedicated agent (<short_name>) is launched in tmux and will email updates + the final result. Then STOP. The spawned agent does the work.

ATTACHMENTS: if the message below lists an "ATTACHMENTS" section, those files were saved to disk at the given paths — read them directly. When you delegate a task, copy those exact file paths into the task spec so the spawned worker can read them (the paths stay valid).
Guardrails: no git commit/push, no deleting data/outputs, no killing running jobs; keep emails terse.
The user's reply (subject + body):
-----
$BODY
-----
Act now.
EOF

echo "[inbox] $(date) dispatch agent=$AGENT type=$TYPE" >> $LOG
if [ "$TYPE" = "resume" ] && [ "$SESSION" != "None" ] && [ -n "$SESSION" ]; then
  if [ "$AGENT" = "paper" ]; then
    PROMPT="$COMMON
You ARE the paper agent (OMPGen SIGMOD paper, repo /home/steven/Papers/Time_series_OMP) with full prior context. For case (2) — any real paper task (writing/outlining sections, LaTeX edits, figures, compiling experiments into prose) — write the task spec to scratch_full_logs/inbox/task_${UID_M:-x}.md and launch the PERSISTENT paper worker (it resumes YOUR OWN session in tmux with full context; do NOT use the generic scratch_spawn_worker.sh for paper work):
    bash scratch_spawn_paper_worker.sh scratch_full_logs/inbox/task_${UID_M:-x}.md ${SENDER:-fenghaod@andrew.cmu.edu}
(If it reports the worker is already running, the task was delivered to its live mailbox — say so in your ACK.) Then email the ACK and stop. Remember: all paper text goes inside \claude{...}; never delete collaborator content."
  else
    PROMPT="$COMMON
You ARE the $AGENT worker with full prior context, which helps you answer questions and write a precise task spec. For any real task, still DELEGATE via scratch_spawn_worker.sh per case (2) — do NOT run the heavy work in this one-shot turn. Put enough context (file paths, prior results) into the task spec so the spawned agent can run standalone; eval work stays under eval/."
  fi
  # Task 176: prompt via STDIN, not argv — mail/snapshot text in the claude argv
  # is matchable by any pkill/pgrep -f (the noaa_fullds self-kill class), and it
  # leaks the mail body into `ps`. PIPESTATUS index shifts: claude is now [1].
  printf '%s' "$PROMPT" | claude --resume "$SESSION" -p --dangerously-skip-permissions --model "$MODEL" --max-turns 200 --verbose 2>&1 | tee "$OUTF" >> $LOG
  RC=${PIPESTATUS[1]}
else
  PROMPT="$COMMON
You may READ project files to answer or to write the task spec: reports/ (sweep results, COST_FIDELITY.md, FIDELITY_b*.md), scratch_full_logs/ (status/logs), eval/ (eval framework). For questions just read and report; for tasks, delegate via scratch_spawn_worker.sh per case (2)."
  # Task 176: stdin prompt delivery (see above); claude rc is PIPESTATUS[1].
  printf '%s' "$PROMPT" | claude -p --dangerously-skip-permissions --model sonnet --max-turns 120 --verbose 2>&1 | tee "$OUTF" >> $LOG
  RC=${PIPESTATUS[1]}
fi
echo "[inbox] $(date) done agent=$AGENT rc=$RC" >> $LOG

# Failure paths must never lose mail silently (uid already marked seen by the loop):
# 1) usage/session limit -> defer until the stated reset (router retries then);
# 2) any other nonzero rc -> unread + retry in 15 min, max 3 attempts, then a
#    dead-letter alert so the user knows that message needs a manual look.
if [ -n "$UID_M" ]; then
  if grep -qiE "hit your (session|usage) limit|reset(s)? (your )?(session|usage|limit)|usage limit" "$OUTF" 2>/dev/null; then
    RESET_LINE=$(grep -ioE "resets[^.]*" "$OUTF" 2>/dev/null | head -1)
    echo "[inbox] $(date) LIMIT hit while handling uid=$UID_M ('$RESET_LINE') -> defer + mark unread" >> $LOG
    python scratch_inbox.py defer --uid "$UID_M" --reset "$RESET_LINE" >> $LOG 2>&1
  elif [ "${RC:-0}" -ne 0 ]; then
    CNTF="scratch_full_logs/inbox/retry_${UID_M}.count"
    N=$(( $(cat "$CNTF" 2>/dev/null || echo 0) + 1 )); echo "$N" > "$CNTF"
    if [ "$N" -le 3 ]; then
      echo "[inbox] $(date) handler FAILED rc=$RC uid=$UID_M (attempt $N/3) -> unmark + defer 15 min" >> $LOG
      python scratch_inbox.py defer --uid "$UID_M" --reset "resets $(date -d '+15 minutes' '+%-I:%M%p')" >> $LOG 2>&1
    else
      echo "[inbox] $(date) handler FAILED rc=$RC uid=$UID_M after $N attempts -> dead-letter alert" >> $LOG
      python scratch_notify_email.py "inbox dead-letter: your email to '$AGENT' needs a manual look" \
        "The handler for your email (uid $UID_M, routed to '$AGENT') failed $N times (last rc=$RC) and gave up. Your message is saved at $4; handler output at $OUTF. Nothing was lost, but no agent replied — please re-send or check the logs." \
        --agent "$AGENT" ${SENDER:+--to "$SENDER"} >> $LOG 2>&1 || true
    fi
  else
    rm -f "scratch_full_logs/inbox/retry_${UID_M}.count"
  fi
fi
