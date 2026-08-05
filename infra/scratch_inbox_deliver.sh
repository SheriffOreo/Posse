#!/usr/bin/env bash
# Deliver ONE inbound email to a LIVE worker's mailbox and interrupt it so the
# mail is read IMMEDIATELY. Factored out of scratch_inbox_loop.sh so the
# interrupt policy is testable in isolation (Task 123 hardening).
#   Usage: scratch_inbox_deliver.sh <agent> <uid> <bodyfile> <subject>
# Policy (Task 123 -> 124 -> 153B -> 170 -> 185):
#   - append mail to the worker's mailbox under flock, VERIFY it, write a
#     delivery receipt (watchdog no-reply alarm), only then mark the uid seen;
#   - Task 185: EVERY inbound email triggers an immediate surgical
#     interrupt+relaunch (scratch_interrupt_worker.sh). The old INT_COOLDOWN
#     branch — which parked mail arriving <120s after the last interrupt with a
#     "read at its next checkpoint" ack — is GONE: it deferred reads (the operator's
#     uid=183) and the parked mail could even be stolen by an orphaned poll
#     loop of the killed claude (root cause, Task 185; scratch_read_mailbox.sh
#     now also refuses orphan drains).
#   - The ONLY non-interrupt path (besides the explicit uninterruptible list,
#     empty by default) is the NON-BLOCKING boot coalesce: if a relaunch of
#     this worker is mid-boot and provably has NOT yet drained the mailbox
#     (relaunching_<agent> marker still present — the boot drain removes it
#     atomically with the drain, under the same mailbox flock), our appended
#     mail is guaranteed to be scooped by that in-flight boot drain seconds
#     from now. Killing the half-booted claude would only delay the read.
#     Back-to-back emails thus batch into ONE relaunch and are all read at
#     once; NOTHING is ever deferred to a "next checkpoint".
#   - The surgical kill leaves CPU children, GPU-queue work AND spawned
#     subagent tmux sessions alive; snapshots of all three are injected into
#     the resume prompt. Mail drained by a boot that gets killed <180s later
#     is re-injected by the interrupt entrypoint (inflight carry-forward), so
#     rapid-fire interrupts cannot lose mail either.
# INBOX_DRYRUN=1: print instead of sending email / marking seen (used by tests;
# the mailbox append and any kill still happen for real; the entrypoint prints
# its relaunch instead of executing it).
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
AGENT="${1:?agent}"; MUID="${2:?uid}"; BODYFILE="${3:?bodyfile}"; SUBJECT="${4:-}"
LOG=scratch_full_logs/inbox_agent.log
IDIR=scratch_full_logs/inbox
UNINT="${INBOX_UNINT_FILE:-$IDIR/uninterruptible.txt}"   # env-overridable for tests
BOOT_FRESH_S=${BOOT_FRESH_S:-45}   # relaunching_<agent> older than this = crashed boot -> interrupt normally
REQUESTER="${INFRA_OPERATOR_EMAIL:-}"   # operator's address (set during onboarding)
mkdir -p "$IDIR/receipts"

log() { echo "[inbox] $(date) $*" >> "$LOG"; }

ack_user() {  # $1 = one-line status; mechanical ack so the user always hears back
  if [ -n "${INBOX_DRYRUN:-}" ]; then echo "[DRYRUN-ACK agent=$AGENT] $1"; return 0; fi
  # Task 170 F6: thread the ack onto the user's email (subject reuse +
  # In-Reply-To of our matching sent update when findable).
  python scratch_notify_email.py "Re: ${SUBJECT:-your email}" \
    "Auto-ack from the inbox router: $1" --agent "$AGENT" --to "$REQUESTER" \
    ${SUBJECT:+--thread "$SUBJECT"} \
    >> "$LOG" 2>&1 || log "WARN ack email failed for $AGENT uid=$MUID"
}

MB="$IDIR/mailbox_${AGENT}.md"
# 1) durable copy first (locked against the worker's read-and-clear).
# Task 170 F1: the append goes through the shared helper, which writes the
# origin header '=== mail uid=N from <sender> delivered <date> ===' — the
# relaunch scripts parse that origin to send a truthful ack (or none).
SENDER="$(sed -n 's/^FROM:[[:space:]]*//p' "$BODYFILE" 2>/dev/null | head -1)"
if ! bash scratch_mailbox_append.sh "$AGENT" "uid=$MUID from ${SENDER:-user}" "$BODYFILE" >/dev/null 2>>"$LOG"; then
  # append failed (disk/path error): leave the uid UNSEEN so the next poll retries
  log "ERROR mailbox append failed for $AGENT uid=$MUID -> left unseen for retry"
  exit 1
fi
# 2) receipt for the watchdog's "delivered but no reply" alarm
python3 - "$AGENT" "$MUID" "$SUBJECT" "$IDIR/receipts/$MUID.json" <<'PY' 2>>"$LOG" || true
import json, sys, time
agent, uid, subject, dest = sys.argv[1:5]
json.dump({"agent": agent, "uid": uid, "subject": subject, "ts": time.time()}, open(dest, "w"))
PY
log "delivered uid=$MUID to LIVE mailbox of $AGENT"
# 3) only now is the Gmail-side flag safe to set
if [ -n "${INBOX_DRYRUN:-}" ]; then echo "[DRYRUN] would mark uid=$MUID seen"
else python scratch_inbox.py mark --uid "$MUID" >> "$LOG" 2>&1; fi

# 4) interrupt decision — Task 185: default is ALWAYS interrupt (immediate read).
if grep -v '^#' "$UNINT" 2>/dev/null | grep -Fxq "$AGENT"; then
  log "NO-KILL uid=$MUID: $AGENT is on the explicit uninterruptible list -> mailbox only + router ack"
  ack_user "your email was delivered to '$AGENT''s live mailbox. This worker is on the explicit uninterruptible list (kept only for truly precious sessions; the list is empty by default), so it was not restarted; it polls its mailbox frequently between steps and the watchdog alerts if it stays silent >15 min."
  exit 0
fi
# Task 185 boot coalesce (non-blocking; never defers a read — see header).
BOOT="$IDIR/relaunching_${AGENT}"
COALESCED=""
BAGE=""
if [ -f "$BOOT" ]; then
  BAGE=$(( $(date +%s) - $(stat -c %Y "$BOOT" 2>/dev/null || echo 0) ))
  if [ "$BAGE" -lt "$BOOT_FRESH_S" ]; then
    # Re-check under the mailbox lock: the boot drain removes the marker under
    # this same lock, so marker-present here proves the drain has not run yet
    # and WILL scoop the mail we appended in step 1.
    COALESCED=$( ( flock 9; [ -f "$BOOT" ] && echo yes || true ) 9>"$IDIR/mailbox_${AGENT}.lock" )
  fi
fi
if [ -n "$COALESCED" ]; then
  log "COALESCE uid=$MUID: a relaunch of $AGENT is mid-boot (marker ${BAGE}s old) — mail rides the in-flight boot drain and is read immediately, batched with the mail that triggered that boot; no second kill of a half-booted claude"
  # No router ack needed: the booting relaunch's origin-aware mechanical ack
  # quotes every uid it drained — including this one — so the user hears
  # exactly one truthful "resumed on your email (uid=...)" ack.
  exit 0
fi
# Task 170 F2 + 185: hand the mechanics to the single interrupt entrypoint
# (mail is ALREADY in the mailbox, so no --message-file; the uid + subject ride
# along for the receipt/threaded relaunch ack). The entrypoint re-injects any
# <180s-old in-flight mail from the previous boot (no loss on rapid fire),
# stamps every guard, creates the boot marker, surgically kills (CPU/GPU/
# subagents survive), relaunches IMMEDIATELY and verifies.
if IOUT=$(bash scratch_interrupt_worker.sh "$AGENT" --uid "$MUID" ${SUBJECT:+--subject "$SUBJECT"} 2>>"$LOG"); then
  log "INTERRUPT uid=$MUID: $IOUT — CPU/GPU/subagent work left alive; relaunch injects the mailbox at the front of the resume prompt and handles the (origin-aware) mechanical ack"
else
  log "WARN interrupt entrypoint failed for $AGENT uid=$MUID ($IOUT); mail is safe in the mailbox — watchdog/inbox fallback will drain it after the guard window"
fi
