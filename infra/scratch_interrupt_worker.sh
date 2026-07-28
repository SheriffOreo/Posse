#!/usr/bin/env bash
# Task 170 F2: THE single first-class entrypoint for "inject a message into a
# worker and interrupt-relaunch it". Encodes, race-safely, the exact sequence
# killfix hand-rolled in Task 167 (and scratch_inbox_deliver.sh now calls this
# instead of hand-rolling its own copy — one relaunch implementation).
#
#   Usage: scratch_interrupt_worker.sh <name>
#            [--relay-from <agent>]      origin for agent-injected relays
#            [--uid N] [--from ADDR]     origin for user mail (router path)
#            [--subject S]               originating subject (written to the
#                                        uid's receipt if missing -> the
#                                        relaunch ack threads onto it, F6)
#            [--message-file F]          body to append (omit if the mailbox
#                                        already holds the message)
#
# Sequence (order matters):
#   1. append the F1-headered message under flock  (scratch_mailbox_append.sh)
#   1.6 Task 185 no-mail-loss carry-forward: if the PREVIOUS boot drained mail
#      into its resume prompt <180s ago (inflight_<name>.md still present =
#      its claude has not plausibly processed it yet), splice that mail BACK
#      at the head of the mailbox under flock, so the boot we are about to
#      trigger re-reads it together with the new mail. Rapid-fire interrupts
#      therefore batch mail instead of losing the previous message.
#   2. stamp ALL relaunch/interrupt guards BEFORE the kill so neither
#      auto-relauncher (inbox fallback / watchdog) double-fires:
#        - touch scratch_full_logs/inbox/last_relaunch_<name>
#        - touch scratch_full_logs/inbox/last_interrupt_<name>  (forensics only
#          since Task 185 — no cooldown reads it any more)
#        - touch scratch_full_logs/inbox/relaunching_<name>  (Task 185 boot
#          marker: scratch_inbox_deliver.sh coalesces mail arriving while the
#          boot it announces has not yet drained the mailbox; the relaunch
#          drain removes it atomically under the mailbox flock)
#        - watchdog_jobs.json: state=running + last_relaunch_ts=now
#        - clear the done-sentinel (a 'done' worker must be revivable)
#   3. surgical kill (scratch_kill_agent_only.py; fallback tmux kill-session)
#      — kills ONLY the worker's claude + pane shell. CPU children survive
#      (manifest -> survivors_<name>.jsonl), GPU work runs under the separate
#      gpu_manager tmux, and spawned subagent workers are separate tmux
#      sessions under the tmux server — structurally untouchable from here.
#   4. tmux new-session -d -s <name> "bash <relaunch script>"
#   5. verify ALIVE (on failure: remove the boot marker so deliveries stop
#      coalescing into a boot that never happened)
#   6. one log line to scratch_full_logs/inbox_agent.log
#
# INBOX_DRYRUN=1: append/stamps/kill still happen for REAL (point this at a
# THROWAWAY agent only!); the relaunch is PRINTED instead of executed and the
# verify is skipped. The relaunch script itself also honors INBOX_DRYRUN
# (prints the claude command instead of running it).
#
# Callers own the POLICY (the uninterruptible list + the boot coalesce — see
# scratch_inbox_deliver.sh; there are NO read-deferring cooldowns since Task
# 185); this script is the MECHANISM only. Exit 0 = relaunched & alive (or
# dry-run).
cd /home/steven/Projects/time-series-omp
LOG=scratch_full_logs/inbox_agent.log
IDIR=scratch_full_logs/inbox
NAME="${1:?worker name}"; shift
RELAY_FROM=""; MSG_UID=""; MSG_FROM=""; MSG_SUBJECT=""; BODYFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --relay-from)   RELAY_FROM="${2:?}"; shift 2;;
    --uid)          MSG_UID="${2:?}"; shift 2;;
    --from)         MSG_FROM="${2:?}"; shift 2;;
    --subject)      MSG_SUBJECT="${2:?}"; shift 2;;
    --message-file) BODYFILE="${2:?}"; shift 2;;
    *) echo "ERR: unknown arg $1" >&2; exit 2;;
  esac
done
case "$NAME" in *[!a-z0-9_-]*|"") echo "ERR: bad worker name '$NAME'" >&2; exit 2;; esac
DRY="${INBOX_DRYRUN:-}"

log() { echo "[interrupt${DRY:+-DRYRUN}] $(date) $*" >> "$LOG"; }

# 0) resolve the relaunch script FIRST — abort before mutating anything.
REL=$(python3 -c "
import json
try: jobs = json.load(open('scratch_full_logs/watchdog_jobs.json'))
except Exception: jobs = []
m = [j for j in jobs if j.get('name')=='$NAME']
print((m[-1].get('relaunch') or '') if m else '')" 2>/dev/null)
[ -n "$REL" ] || REL="scratch_worker_${NAME}_relaunch.sh"
if [ ! -f "$REL" ]; then
  echo "ERR: no relaunch script for '$NAME' ($REL missing; not in watchdog_jobs.json either)" >&2
  exit 1
fi

# 1) append the message (if given) with a truthful F1 origin header.
if [ -n "$BODYFILE" ]; then
  if [ -n "$MSG_UID" ]; then
    ORIGIN="uid=$MSG_UID from ${MSG_FROM:-user}"
  elif [ -n "$RELAY_FROM" ]; then
    ORIGIN="relay from agent $RELAY_FROM"
  else
    echo "ERR: --message-file needs an origin: --uid N [--from ADDR] or --relay-from <agent>" >&2
    exit 2
  fi
  bash scratch_mailbox_append.sh "$NAME" "$ORIGIN" "$BODYFILE" >/dev/null || {
    echo "ERR: mailbox append failed for $NAME" >&2; exit 1; }
fi
# F6: make sure the uid has a receipt carrying the subject, so the relaunch
# ack can thread onto the originating email (router already writes one; this
# covers direct/manual uid use).
if [ -n "$MSG_UID" ] && [ -n "$MSG_SUBJECT" ] && ! ls "$IDIR/receipts/${MSG_UID}".json* >/dev/null 2>&1; then
  python3 - "$NAME" "$MSG_UID" "$MSG_SUBJECT" "$IDIR/receipts/${MSG_UID}.json" <<'PY' 2>>"$LOG" || true
import json, sys, time
agent, uid, subject, dest = sys.argv[1:5]
json.dump({"agent": agent, "uid": uid, "subject": subject, "ts": time.time()}, open(dest, "w"))
PY
fi

# 1.6) Task 185 no-mail-loss carry-forward (see header). The inflight blob
# keeps its '=== mail ...' origin headers, so uid parsing / ack semantics of
# the next boot stay truthful. Older than 600s = leftover from an undefined
# state (deadman-clear died?) -> drop it rather than replay ancient mail.
INF="$IDIR/inflight_${NAME}.md"
if [ -s "$INF" ]; then
  IAGE=$(( $(date +%s) - $(stat -c %Y "$INF" 2>/dev/null || echo 0) ))
  if [ "$IAGE" -lt 600 ]; then
    ( flock 9
      { cat "$INF"
        # newline seam: a blob missing its final \n would glue the next '==='
        # header onto its last line and hide that uid from the origin parser
        [ -n "$(tail -c1 "$INF" 2>/dev/null)" ] && echo
        cat "$IDIR/mailbox_${NAME}.md" 2>/dev/null
      } > "$IDIR/mailbox_${NAME}.reinject.tmp" || true
      mv "$IDIR/mailbox_${NAME}.reinject.tmp" "$IDIR/mailbox_${NAME}.md"
      : > "$INF"
      rm -f "$IDIR/inflight_${NAME}.gen"
    ) 9>"$IDIR/mailbox_${NAME}.lock"
    log "$NAME: re-injected ${IAGE}s-old in-flight mail ahead of the new mail (previous boot had not plausibly processed it yet — nothing is lost on rapid-fire interrupts)"
  else
    : > "$INF"; rm -f "$IDIR/inflight_${NAME}.gen"
    log "$NAME: dropped stale inflight blob (${IAGE}s old)"
  fi
fi

# 2) stamp every guard surface BEFORE the kill (Task 168 problem #2: the two
# auto-relaunchers keep unshared guards — stamp both), plus the Task 185 boot
# marker that lets the router coalesce mail into THIS boot's drain.
touch "$IDIR/last_relaunch_${NAME}" "$IDIR/last_interrupt_${NAME}" "$IDIR/relaunching_${NAME}"
rm -f "scratch_full_logs/worker_${NAME}.done"
python3 - "$NAME" <<'PY' 2>>"$LOG" || true
import json, sys, time
p = "scratch_full_logs/watchdog_jobs.json"
jobs = json.load(open(p))
for j in jobs:
    if j.get("name") == sys.argv[1]:
        j["state"] = "running"
        j["last_relaunch_ts"] = time.time()
json.dump(jobs, open(p, "w"), indent=2)
PY

# 3) surgical kill (claude only; CPU/GPU children + subagent tmux sessions
# survive). Fallback: whole tmux session. Either failing is fine when the
# worker is already dead.
if KOUT=$(python3 scratch_kill_agent_only.py "$NAME" 2>>"$LOG"); then
  KILLMSG="surgical kill ($KOUT)"
else
  tmux kill-session -t "=$NAME" 2>/dev/null || true
  KILLMSG="surgical kill unavailable -> tmux kill-session (worker may have been dead already)"
fi

# 4) relaunch + 5) verify
if [ -n "$DRY" ]; then
  echo "[DRYRUN-RELAUNCH $NAME] would run: tmux new-session -d -s $NAME \"bash $REL\""
  log "$NAME: ${BODYFILE:+mail appended (${ORIGIN:-}), }guards+boot-marker stamped, $KILLMSG; DRYRUN -> relaunch printed, verify skipped"
  echo "OK(dryrun): $NAME guards stamped + $KILLMSG; relaunch printed"
  exit 0
fi
tmux new-session -d -s "$NAME" "bash $REL"
sleep 1
if tmux has-session -t "=$NAME" 2>/dev/null; then
  log "$NAME: ${BODYFILE:+mail appended (${ORIGIN:-}), }guards+boot-marker stamped, $KILLMSG, relaunched via $REL -> ALIVE"
  echo "OK: $NAME relaunched via $REL and ALIVE${BODYFILE:+ (mail origin: ${ORIGIN:-})}"
  exit 0
else
  rm -f "$IDIR/relaunching_${NAME}"   # no boot is coming — stop coalescing into it
  log "ERROR $NAME: relaunch via $REL did NOT come up (guards stamped, boot marker removed; auto-relaunchers will retry after the guard window)"
  echo "ERR: $NAME relaunch did not come up; watchdog/inbox fallback will retry after ~2 min" >&2
  exit 3
fi
