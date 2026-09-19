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
#     the uid is marked UNREAD + deferred 15 min instead of being lost as "seen".
# Task 362 (2026-07-29) retry/defer policy: each activation retries the claude
#   call INNER_RETRIES(=5) times in a row (INNER_SLEEP between tries) before giving
#   up on this activation; a non-limit failure then defers 15 min + emails the
#   sender, up to MAX_DEFERS(=3) deferrals, after which a FINAL give-up email
#   dead-letters the uid. Real usage/session limits still bypass this (defer to
#   the reset). Knobs are env-overridable named constants below (tests only).
# Portable runtime env: cd into infra/ (code + state root), optionally activate a
# conda env (INFRA_CONDA_ENV; unset => system python3), load the Claude auth switch
# (default = Max subscription, not the API key), and put the claude CLI on PATH.
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/_daemon_env.sh"

# --- Task 362 policy constants -------------------------------------------------
# the operator's spec: "try 5 times in a row; if all fail, defer 15 min and email; 15 min
# later do the same; after deferring 3 times if still failed, send a final email."
# Reading implemented = 3 DEFERS then a separate FINAL (worst case 4 activations x
# 5 tries = 20 attempts; 3 defer emails + 1 final). To use the alternative "2 defers
# then final" reading, set MAX_DEFERS=2 (one-line change). These are env-overridable
# ONLY so the offline test can shorten them; production (the inbox loop) never sets
# them, so the defaults below apply verbatim.
INNER_RETRIES="${INNER_RETRIES:-5}"   # claude tries per activation; break on rc=0
INNER_SLEEP="${INNER_SLEEP:-10}"      # seconds between tries (5 tries ~ 40-60s)
MAX_DEFERS="${MAX_DEFERS:-3}"         # 15-min deferrals before the final give-up
# Usage/session-limit signature -> skip the retry/defer machine, defer to the reset.
LIMIT_RE="hit your (session|usage) limit|reset(s)? (your )?(session|usage|limit)|usage limit"
# INBOX_DRYRUN=1 -> offline test mode: never touch IMAP (defer) or send real mail
# (routes notify_email through its TSOMP_MAIL_DRYRUN record-only seam). Unset in prod.
INBOX_DRYRUN="${INBOX_DRYRUN:-}"
[ -n "$INBOX_DRYRUN" ] && export TSOMP_MAIL_DRYRUN=1
# HANDLER_FORCE_FAIL (test hook, inert unless set): short-circuit the claude call and
# fake a failing run -- "limit" writes a usage-limit line, else an "API Error: 529"
# line; rc forced to 1. Never set in production.
HANDLER_FORCE_FAIL="${HANDLER_FORCE_FAIL:-}"
# -------------------------------------------------------------------------------
AGENT="$1"; TYPE="$2"; SESSION="$3"; BODY="$(cat "$4" 2>/dev/null)"; UID_M="$5"
LOG=scratch_full_logs/inbox_agent.log
OUTF="scratch_full_logs/inbox/last_handle_${UID_M:-x}.out"
# Per-worker model override from the registry (e.g. "model": "fable" for sessions
# whose context exceeds the default model's window). Defaults to opus.
MODEL=$(python3 -c "import json;print(json.load(open('scratch_agents_registry.json'))['workers'].get('$AGENT',{}).get('model') or 'opus')" 2>/dev/null || echo opus)
# Reply back to whoever sent the reply (parsed from the "FROM:" line of the body file).
SENDER="$(sed -n 's/^FROM:[[:space:]]*//p' "$4" 2>/dev/null | head -1)"
# Only trust senders on the allow-list (INFRA_MAIL_ALLOWED, comma-separated); blank
# out anything else (fail-closed). Empty list => no sender is ever trusted.
case ",${INFRA_MAIL_ALLOWED}," in *",${SENDER},"*) [ -n "$SENDER" ] || SENDER="" ;; *) SENDER="" ;; esac

# --- Case 391a: ONE continuous case-number sequence (email AND web) --------------
# A real emailed TASK gets an ALLOCATED case number from the single monotonic
# sequence (scratch_case_seq.py), NOT the Gmail uid. The uid is used ONLY for
# routing / dedup (IMAP seen-marking) / receipts / mailbox / defer counters below;
# it is NOT the visible case number any more (this reverses the Task 377 namespaced
# note). allocate-for-uid is IDEMPOTENT per uid, so a Task-362 retry of this handler
# reuses the SAME number and the spec task_<CASE>.md stays stable (no second case
# for one email). Any task spec the triage writes/spawns is named task_${CASE}.md.
if [ -n "$UID_M" ]; then
  CASE="$(python3 scratch_case_seq.py allocate-for-uid --uid "$UID_M" 2>>$LOG)"
fi
case "${CASE:-}" in ''|*[!0-9]*) CASE="${UID_M:-x}" ;; esac   # graceful fallback: never blank/non-numeric
echo "[inbox] $(date) case-number uid=$UID_M -> case=$CASE (uid decoupled; routing/dedup only)" >> $LOG

# --- Task 372 (Sheriff & Deputies): resolve the routing PRECINCT ----------------
# Matching mechanism (first match wins): explicit tag ('precinct: <name>' in body /
# '[<name>]' in subject) -> the reply's parent-task precinct -> else the RECEPTIONIST.
# Purely additive: when a precinct resolves, deputies spawned in this handler inherit
# it via WORKER_PRECINCT (so their two closing appends land in the right precinct);
# when none resolves, the RECEPTIONIST block below tells the triage to answer or route.
RSUBJ="$(sed -n 's/^SUBJECT:[[:space:]]*//p' "$4" 2>/dev/null | head -1)"
PRECINCT_RESOLVE="$(python3 scratch_precinct.py resolve --uid "${UID_M:-0}" --subject "$RSUBJ" --bodyfile "$4" 2>/dev/null)"
PRECINCT="$(printf '%s' "$PRECINCT_RESOLVE" | cut -f1)"
PRECINCT_BASIS="$(printf '%s' "$PRECINCT_RESOLVE" | cut -f2)"
[ -n "$PRECINCT" ] && export WORKER_PRECINCT="$PRECINCT"
echo "[inbox] $(date) precinct-resolve uid=$UID_M -> '${PRECINCT:-<receptionist>}' basis=$PRECINCT_BASIS" >> $LOG
# Task 376: an email 'model:' tag (body 'model: <name>' or subject '[model:<name>]')
# overrides the precinct's default model for ANY deputy spawned in this handler.
# Exported as WORKER_MODEL, which scratch_spawn_worker.sh honours as top priority.
MODEL_TAG="$(python3 scratch_precinct.py model --subject "$RSUBJ" --bodyfile "$4" 2>/dev/null | tr -d '[:space:]')"
if [ -n "$MODEL_TAG" ]; then
  export WORKER_MODEL="$MODEL_TAG"
  echo "[inbox] $(date) model-tag uid=$UID_M -> WORKER_MODEL=$MODEL_TAG" >> $LOG
fi
# Build the routing block (ledger content is injected via variable expansion, which
# bash does NOT re-scan for $()/backticks — so a ledger containing e.g. '$10k' is safe).
if [ -n "$PRECINCT" ]; then
  PLEDGER="$(python scratch_records.py ledger read --dept "$PRECINCT" 2>/dev/null | head -c 4000)"
  PDIR="$(python scratch_records.py directory read --md 2>/dev/null)"
  read -r -d '' PRECINCT_BLOCK <<EOF || true
PRECINCT ROUTING (Task 372): this contact is routed to the '$PRECINCT' precinct (basis: $PRECINCT_BASIS). WORKER_PRECINCT=$PRECINCT is ALREADY EXPORTED in your environment, so any deputy you launch with scratch_spawn_worker.sh is automatically filed under '$PRECINCT' and makes its two closing appends (case log + ledger) there — you do NOT pass the precinct yourself. Skim this precinct's ledger for prior context before you answer or delegate:
=== $PRECINCT LEDGER (big picture) ===
$PLEDGER
=== GLOBAL PRECINCT DIRECTORY ===
$PDIR
EOF
else
  RLEDGER="$(python scratch_records.py ledger read --dept receptionist 2>/dev/null)"
  PDIR="$(python scratch_records.py directory read --md 2>/dev/null)"
  read -r -d '' PRECINCT_BLOCK <<EOF || true
RECEPTIONIST ROUTING (Task 372; Phase-D hand-off, Case 384d): this is a FRESH contact that named no precinct and owns no known task (basis: $PRECINCT_BASIS). Act as the RECEPTIONIST (the front desk). You DO NOT create or delete precincts yourself and you own no records authority — only the SHERIFF does. Decide which case this is:
  * EASY QUESTION (answerable now by reading files): just email the answer. Do NOT allocate a task number, write NO case-log line and NO case file, spawn NO worker.
  * A NORMAL TASK (real work): (a) choose an EXISTING target precinct from the directory below (NEVER invent one, and NEVER 'directory register' — that authority is the sheriff's); if nothing fits, pick the closest and say so in your ACK. (b) write a self-contained spec to scratch_full_logs/inbox/task_${CASE}.md — its case number is ${CASE} (already allocated from the single continuous sequence; do NOT use the Gmail uid as the case number). (c) launch the deputy WITH the precinct exported so it files paperwork there:
        WORKER_PRECINCT=<precinct> bash scratch_spawn_worker.sh <short_name> scratch_full_logs/inbox/task_${CASE}.md ${SENDER:-$INFRA_OPERATOR_EMAIL} "<keywords>"
     (the spawn creates the case-file symlink, stamps the task->precinct map, and the deputy makes its two closing appends). Then email a brief ACK naming the precinct + deputy.
  * AN EXPLICIT "CREATE / DELETE PRECINCT X" INSTRUCTION: you HAND OFF to the sheriff (you never do it yourself). Post ONE request to the sheriff's queue, then email an ACK saying you handed it to the sheriff (which decides via an API call; a DELETE additionally emails ${SENDER:-the user} a "reply YES" confirmation and only soft-deletes on that YES, keeping a 14-day recoverable trash). Do NOT spawn a deputy for a create/delete and do NOT touch precincts.json. Commands:
        # create:
        python scratch_sheriff_request.py request --op precinct_create --target <newname> --origin receptionist --requester ${SENDER:-$INFRA_OPERATOR_EMAIL} --description "<one line>" --reason "user explicitly asked to create precinct <newname>"
        # delete (add --force ONLY if the user explicitly insists despite active work):
        python scratch_sheriff_request.py request --op precinct_delete --target <name> --origin receptionist --requester ${SENDER:-$INFRA_OPERATOR_EMAIL} --reason "user explicitly asked to delete precinct <name>"
     An UNKNOWN precinct tag is NOT a create signal — only an explicit user instruction to create is. If unsure, ask the user by email rather than creating anything.
=== RECEPTIONIST FIXED LEDGER (the system + the routing rules) ===
$RLEDGER
=== GLOBAL PRECINCT DIRECTORY ===
$PDIR
EOF
fi

# --- Task 362 helpers ----------------------------------------------------------
# Re-queue this uid (real IMAP defer), or just print it under INBOX_DRYRUN.
do_defer() {   # $1 = reset string, e.g. "resets 7:00PM"
  if [ -n "$INBOX_DRYRUN" ]; then
    echo "DRYRUN defer uid=$UID_M reset='$1'"
    echo "[inbox] $(date) DRYRUN defer uid=$UID_M reset='$1'" >> $LOG
  else
    python scratch_inbox.py defer --uid "$UID_M" --reset "$1" >> $LOG 2>&1
  fi
}
# Email the reply's sender (auto-greet/sign). Under INBOX_DRYRUN, notify_email is in
# TSOMP_MAIL_DRYRUN mode (records to mail_dryrun.jsonl, does not send); echo a marker.
notify() {     # $1 = subject, $2 = body
  [ -n "$INBOX_DRYRUN" ] && echo "DRYRUN email -> ${SENDER:-<no-sender>} :: $1"
  python scratch_notify_email.py "$1" "$2" --agent "$AGENT" ${SENDER:+--to "$SENDER"} >> $LOG 2>&1 || true
}
# Run claude with the given args (prompt piped from $PROMPT), retrying up to
# INNER_RETRIES times IN A ROW, breaking on rc=0 or a detected usage limit. Sets
# global RC to the last attempt's claude return code (Task 362).
run_with_retries() {   # $@ = claude args
  local try
  RC=1
  for try in $(seq 1 "$INNER_RETRIES"); do
    if [ -n "$HANDLER_FORCE_FAIL" ]; then
      if [ "$HANDLER_FORCE_FAIL" = "limit" ]; then
        printf '%s\n' "TEST HOOK ($HANDLER_FORCE_FAIL): simulated claude run" \
          "You have hit your usage limit · resets 7:00PM" | tee "$OUTF" >> $LOG
      else
        printf '%s\n' "TEST HOOK ($HANDLER_FORCE_FAIL): simulated claude run" \
          "API Error: 529 Overloaded" | tee "$OUTF" >> $LOG
      fi
      RC=1
    else
      # Task 176: prompt via STDIN (never argv); claude is pipe stage [1].
      printf '%s' "$PROMPT" | claude "$@" 2>&1 | tee "$OUTF" >> $LOG
      RC=${PIPESTATUS[1]}
    fi
    echo "[inbox] $(date) handler try $try/$INNER_RETRIES agent=$AGENT uid=$UID_M rc=$RC" >> $LOG
    [ "$RC" -eq 0 ] && break
    # A real usage/session limit will not clear by retrying -> stop early and let
    # the limit branch below defer to the actual reset.
    grep -qiE "$LIMIT_RE" "$OUTF" 2>/dev/null && break
    [ "$try" -lt "$INNER_RETRIES" ] && sleep "$INNER_SLEEP"
  done
}

# Serialize handlers per agent (wait up to 30 min; then defer instead of racing).
exec 9>"scratch_full_logs/inbox/handle_${AGENT}.lock"
if ! flock -w 1800 9; then
  echo "[inbox] $(date) handler lock timeout agent=$AGENT uid=$UID_M -> defer 15 min" >> $LOG
  [ -n "$UID_M" ] && do_defer "resets $(date -d '+15 minutes' '+%-I:%M%p')"
  exit 1
fi

read -r -d '' COMMON <<EOF
You are TRIAGING an email reply from the operator (allowed senders: ${INFRA_MAIL_ALLOWED}) to an automated update from the "$AGENT" deputy of this Posse. This reply is from: ${SENDER:-the operator}. Act RIGHT AWAY and address all email back to the sender.
Reply by email with: python scratch_notify_email.py "Re: <subject>" "<body>" --agent $AGENT${SENDER:+ --to $SENDER}  (attach a figure with --attach if helpful). The mailer auto-greets the operator by name and signs your agent name — write just the body, no greeting/signature.

Decide which case this message is, then act:

(1) QUESTION / status / small clarification (no real work, answerable by reading files): answer concisely and email the answer NOW. Done.

(2) A TASK requiring actual work — running code, scoring/training, generating files or figures, multi-step analysis, anything that takes more than a moment: DO NOT do the work yourself in this turn (you are a one-shot handler and will exit — the work would die). Instead:
   a. Write a SELF-CONTAINED task spec to scratch_full_logs/inbox/task_${CASE}.md — its case number is ${CASE}, ALREADY allocated from the single continuous sequence (do NOT use the Gmail uid as the case number). Include concrete steps, exact input paths, deliverables, output locations, constraints/lane, and that it must email ${SENDER:-the requester} a plan + milestone + FINAL update. Reuse context the requester has already given you (attached files, earlier cases in this precinct, and the precinct's records). Begin the spec file with a machine-readable line "parent_task: <N>" where <N> is the case number this message follows up on — take it from the reply subject "Re: Task <N> ..." if present, else write "parent_task: none" (the handler also stamps this mechanically, so just do your best).
   b. Launch a PERSISTENT working agent to own it:
        bash scratch_spawn_worker.sh <short_name> scratch_full_logs/inbox/task_${CASE}.md ${SENDER:-$INFRA_OPERATOR_EMAIL} "<comma,keywords>"
      Pick a short lowercase <short_name> (e.g. task_daily_rerun) and routing keywords from the task.
   c. Email ${SENDER:-the requester} a brief ACK + plan NOW. Task 377 CONVENTION — the ACK MUST EXPLICITLY STATE the take-vs-delegate decision and the reason, and name the case number (${CASE}, from the single continuous sequence — NOT the Gmail uid): here you are a one-shot triage handler that exits after replying, so you DELEGATE — say so plainly, e.g. "Spawning a NEW dedicated deputy '<short_name>' (case ${CASE}) because this is multi-step work that must outlive this one-shot handler; it will email you a plan, milestones, and the FINAL." (When the ORIGINAL deputy is instead relaunched persistently and takes a follow-up itself — Feng uid=378 — its reply states the opposite: "Taking this on myself as case <the number it allocates> …". Match that explicit style.) Then STOP. The spawned agent does the work.

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
  # Task 391 #3: the legacy hardcoded per-project special-case is REMOVED. It used to
  # resume one persistent hardcoded session via a bespoke launcher, which
  # bypassed the deputy model entirely — it filed NO case number, NO ledger paragraph,
  # and NO case-log line, and showed a stale case# on the board. The paper agent is now
  # a NORMAL deputy that delegates real work via scratch_spawn_worker.sh (a fresh
  # registered case + full paperwork) exactly like every other precinct.
    PROMPT="$COMMON

$PRECINCT_BLOCK

You ARE the $AGENT worker with full prior context, which helps you answer questions and write a precise task spec. For any real task, still DELEGATE via scratch_spawn_worker.sh per case (2) — do NOT run the heavy work in this one-shot turn. Put enough context (file paths, prior results) into the task spec so the spawned agent can run standalone; eval work stays under eval/."
  # Task 176: prompt via STDIN, not argv — mail/snapshot text in the claude argv
  # is matchable by any pkill/pgrep -f (the noaa_fullds self-kill class), and it
  # leaks the mail body into `ps`. Task 362: retry INNER_RETRIES times in a row.
  run_with_retries --resume "$SESSION" -p --dangerously-skip-permissions --model "$MODEL" --max-turns 200 --verbose
  # Task 394: a DEAD session (a legacy pre-precinct agent whose transcript no longer
  # exists — e.g. the retired 'mismatch'/'query'/'bench'/'qsel' workers) makes every
  # `claude --resume` try fail with "No conversation found with session ID". Do NOT
  # retry-loop/defer on that — FALL THROUGH to the general precinct path so a fresh
  # deputy takes the case. This self-heals ANY dead-session registry match, not just
  # the one Feng hit, and is the belt-and-suspenders behind the precinct-aware routing.
  if [ "${RC:-0}" -ne 0 ] && grep -qiE "No conversation found|no session found|session .*not found|conversation .*not found" "$OUTF" 2>/dev/null; then
    echo "[inbox] $(date) resume session $SESSION DEAD for $AGENT -> fresh precinct spawn fallthrough (uid=394)" >> $LOG
    PROMPT="$COMMON

$PRECINCT_BLOCK

NOTE: the prior session for '$AGENT' no longer exists, so treat this as a FRESH precinct contact — do NOT try to resume anything. For a question just read files and answer; for a real task, DELEGATE a proper deputy via scratch_spawn_worker.sh per case (2) so it outlives this one-shot handler."
    run_with_retries -p --dangerously-skip-permissions --model sonnet --max-turns 120 --verbose
  fi
else
  PROMPT="$COMMON

$PRECINCT_BLOCK

You may READ project files to answer or to write the task spec: reports/ (sweep results, COST_FIDELITY.md, FIDELITY_b*.md), scratch_full_logs/ (status/logs), eval/ (eval framework). For questions just read and report; for tasks, delegate via scratch_spawn_worker.sh per case (2)."
  # Task 176: stdin prompt delivery; Task 362: retry INNER_RETRIES times in a row.
  run_with_retries -p --dangerously-skip-permissions --model sonnet --max-turns 120 --verbose
fi
echo "[inbox] $(date) done agent=$AGENT rc=$RC" >> $LOG

# Task 323 D1 / Case 391a: persist the parent_task + precinct lineage onto the task
# spec the triage just created (task_<CASE>.md — CASE is the allocated case number,
# NOT the Gmail uid), and key the task_precinct MAP by CASE so a later reply
# "Re: Task <CASE>" resolves this case's precinct. Parent basis: reply-subject
# "Re: Task N" -> N, else this worker's most-recent prior case, else none. Purely
# additive — a failure here can never affect routing. The dashboard consumes the
# stamped `parent_task:` field directly, so new follow-ups link with no UI change.
SPEC="scratch_full_logs/inbox/task_${CASE}.md"
if [ -n "$CASE" ] && [ -f "$SPEC" ]; then
  python3 scratch_task_parent.py stamp --uid "$CASE" --agent "$AGENT" \
    --subject "$RSUBJ" --spec "$SPEC" ${PRECINCT:+--precinct "$PRECINCT"} >> $LOG 2>&1 || true
fi

# Failure paths must never lose mail silently (uid already marked seen by the loop):
# 1) usage/session limit -> defer until the stated reset (router retries then), NOT
#    into the 5x/defer machine (retrying before the reset is pointless);
# 2) any OTHER nonzero rc (transient 529/network/etc.) AFTER the INNER_RETRIES
#    in-a-row tries above already failed -> defer 15 min + email the sender, up to
#    MAX_DEFERS times (persisted counter defer_<uid>.count), then a FINAL give-up
#    email + dead-letter (leave the uid seen, do NOT defer again).
if [ -n "$UID_M" ]; then
  if grep -qiE "$LIMIT_RE" "$OUTF" 2>/dev/null; then
    RESET_LINE=$(grep -ioE "resets[^.]*" "$OUTF" 2>/dev/null | head -1)
    echo "[inbox] $(date) LIMIT hit while handling uid=$UID_M ('$RESET_LINE') -> defer to reset (skip 5x/defer machine)" >> $LOG
    do_defer "$RESET_LINE"
  elif [ "${RC:-0}" -ne 0 ]; then
    CNTF="scratch_full_logs/inbox/defer_${UID_M}.count"
    D=$(( $(cat "$CNTF" 2>/dev/null || echo 0) + 1 )); echo "$D" > "$CNTF"
    RSUBJ="$(sed -n 's/^SUBJECT:[[:space:]]*//p' "$4" 2>/dev/null | head -1)"
    LASTERR=$(grep -iE "API Error|overload|error|429|529" "$OUTF" 2>/dev/null | tail -1 | cut -c1-200)
    [ -z "$LASTERR" ] && LASTERR="(no error line captured; last rc=$RC)"
    if [ "$D" -le "$MAX_DEFERS" ]; then
      NEXT="$(date -d '+15 minutes' '+%-I:%M%p')"
      echo "[inbox] $(date) handler FAILED rc=$RC uid=$UID_M (${INNER_RETRIES}x in a row) -> defer 15 min (defer $D/$MAX_DEFERS)" >> $LOG
      do_defer "resets $NEXT"
      notify "inbox: handler for your email (uid $UID_M -> '$AGENT') failed ${INNER_RETRIES}x -- deferring 15 min (defer $D/$MAX_DEFERS)" \
"The one-shot triage handler for your email failed ${INNER_RETRIES} times in a row and will retry automatically -- no action needed.

  agent:       $AGENT
  uid:         $UID_M
  your email:  ${RSUBJ:-(subject unavailable)}
  last error:  $LASTERR
  next retry:  ~$NEXT  (deferral $D of $MAX_DEFERS)

If all $MAX_DEFERS deferrals fail I will send one final notice."
    else
      echo "[inbox] $(date) handler FAILED rc=$RC uid=$UID_M after ${INNER_RETRIES}x across $MAX_DEFERS deferrals -> FINAL give-up (dead-letter)" >> $LOG
      notify "inbox dead-letter: your email to '$AGENT' needs a manual look (gave up after $MAX_DEFERS deferrals)" \
"The triage handler for your email failed ${INNER_RETRIES} times in a row across $MAX_DEFERS deferrals (~$(( MAX_DEFERS * 15 ))+ min) and has given up.

  agent:       $AGENT
  uid:         $UID_M
  your email:  ${RSUBJ:-(subject unavailable)}
  last error:  $LASTERR
  saved body:  $4
  handler out: $OUTF

Nothing was lost, but no agent replied -- please re-send or check the logs."
      rm -f "$CNTF"
    fi
  else
    rm -f "scratch_full_logs/inbox/defer_${UID_M}.count" "scratch_full_logs/inbox/retry_${UID_M}.count"
  fi
fi
