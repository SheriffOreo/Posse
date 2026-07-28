#!/usr/bin/env bash
# Always-on inbox router (tmux 'inbox') — v3, Task 185 immediate-read policy.
# Polls Gmail every 30s for unseen replies from allowed senders and routes each
# to the owning worker with a REPLY GUARANTEE:
#   - worker LIVE (its tmux exists): scratch_inbox_deliver.sh appends the mail
#     to its mailbox and (Task 185) IMMEDIATELY surgically interrupt+relaunches
#     it so the mail is read as the resumed worker's first action — there is NO
#     read-deferring cooldown any more. The only no-kill paths: the explicit
#     uninterruptible list (empty by default) and the non-blocking boot
#     coalesce (a relaunch already mid-boot that provably has not yet drained
#     the mailbox scoops the new mail into the same immediate read). CPU jobs,
#     GPU jobs and spawned subagent tmux sessions survive every interrupt.
#     The user always gets a mechanical ack within seconds (from the router on
#     a no-kill, from the relaunch script after a kill).
#   - worker DEAD with unread mailbox: relaunch via its registered relaunch
#     script (drains the mailbox into the resume prompt + acks), guarded by a
#     short per-agent stamp so a crash-looping relaunch cannot storm.
#   - otherwise: one-shot triage handler (scratch_inbox_handle.sh, serialized
#     per agent with flock and retry-on-crash instead of losing mail).
# Liveness = tmux has-session '=name' (exact). NEVER pgrep -f <session-id>: that
# matches unrelated processes (e.g. the user's VS Code window resuming the same
# session), which is exactly how paper's mail got stranded on Jul 8.
# SELF-UPDATE: edit this file (atomic mv), `bash -n` it, then
# `touch scratch_full_logs/inbox/RESTART_LOOP` — the loop execs its new inode
# between iterations (same pid, no double-poller window, no tmux kill needed).
cd /home/steven/Projects/time-series-omp
source ~/anaconda3/etc/profile.d/conda.sh
conda activate tsomp
export PATH="$HOME/.npm-global/bin:$PATH"
LOG=scratch_full_logs/inbox_agent.log
IDIR=scratch_full_logs/inbox
# Task 185: dead-worker relaunch guard only (crash-recovery pacing, NOT a read
# deferral — live-path mail never waits on this). 120->45s: a relaunch either
# comes up well within 45s or died; shorter guard = faster recovery of a
# crashed relaunch's unread mail.
REL_COOLDOWN=45
mkdir -p "$IDIR/receipts"
echo "[inbox] loop v3 started $(date) md5=$(md5sum "$0" | cut -d' ' -f1)" >> $LOG

age_of() { [ -f "$1" ] && echo $(( $(date +%s) - $(stat -c %Y "$1") )) || echo 999999; }

while true; do
  OUT=$(python scratch_inbox.py next 2>>$LOG)
  if [[ "$OUT" == UID=* ]]; then
    MUID=$(echo "$OUT"   | grep -oP 'UID=\K[^\t]+')
    AGENT=$(echo "$OUT" | grep -oP 'AGENT=\K[^\t]+')
    TYPE=$(echo "$OUT"  | grep -oP 'TYPE=\K[^\t]+')
    SESSION=$(echo "$OUT" | grep -oP 'SESSION=\K[^\t]+')
    BODYFILE=$(echo "$OUT" | grep -oP 'BODYFILE=\K[^\t]+')
    SUBJECT=$(echo "$OUT"  | grep -oP 'SUBJECT=\K.*$' | cut -f1)
    # LIVE worker (identity = its tmux session, exact match) -> mailbox delivery
    # + immediate interrupt (Task 185 policy lives in scratch_inbox_deliver.sh);
    # marking-seen happens inside the helper only after the durable mailbox
    # copy is verified.
    if [ "$TYPE" = "resume" ] && [ -n "$SESSION" ] && [ "$SESSION" != "None" ] && tmux has-session -t "=$AGENT" 2>/dev/null; then
      bash scratch_inbox_deliver.sh "$AGENT" "$MUID" "$BODYFILE" "$SUBJECT" >> $LOG 2>&1
    else
      echo "[inbox] $(date) new reply uid=$MUID -> agent=$AGENT ($SUBJECT)" >> $LOG
      setsid bash scratch_inbox_handle.sh "$AGENT" "$TYPE" "$SESSION" "$BODYFILE" "$MUID" < /dev/null >> $LOG 2>&1 &
      python scratch_inbox.py mark --uid "$MUID" >> $LOG 2>&1
    fi
  fi
  # Fallback: a worker exited (or a relaunch crashed) leaving unread live-mail.
  # If its tmux is gone, relaunch it so the mail is drained + acked. (A LIVE
  # worker with a non-empty mailbox needs nothing here: user mail always rides
  # a deliver.sh interrupt or an in-flight boot drain; agent relays are read at
  # the worker's own mailbox polls.)
  shopt -s nullglob
  for MB in "$IDIR"/mailbox_*.md; do
    [ -s "$MB" ] || continue
    A=$(basename "$MB" .md); A=${A#mailbox_}
    case "$A" in *.processed) continue;; esac          # archives, not live mailboxes
    S=$(python scratch_inbox.py session --agent "$A" 2>/dev/null)
    { [ -z "$S" ] || [ "$S" = "None" ]; } && continue
    tmux has-session -t "=$A" 2>/dev/null && continue  # alive -> deliver.sh/boot-drain owns it
    [ "$(age_of "$IDIR/last_relaunch_$A")" -lt "$REL_COOLDOWN" ] && continue  # crash-loop guard
    # Persistent worker with a registered relaunch script: relaunch it — the
    # relaunch drains the mailbox in-process (race-free), sends the mechanical
    # ack, and injects the mail at the front of the resume prompt.
    REL=$(python3 -c "import json;j=[x for x in json.load(open('scratch_full_logs/watchdog_jobs.json')) if x.get('name')=='$A'];print(j[-1].get('relaunch') or '' if j else '')" 2>/dev/null)
    if [ -n "$REL" ] && [ -f "$REL" ]; then
      touch "$IDIR/last_relaunch_$A"
      echo "[inbox] $(date) unread live-mail for $A -> relaunching persistent worker via $REL" >> $LOG
      # Task 170 F3: stamp BOTH guard surfaces — this inbox stamp (above) AND
      # the watchdog's last_relaunch_ts — so the two auto-relaunchers can
      # never double-fire on the same death (they kept unshared guards).
      python3 - "$A" <<'PY' 2>>$LOG
import json, sys, time
p = "scratch_full_logs/watchdog_jobs.json"; jobs = json.load(open(p))
for j in jobs:
    if j.get("name") == sys.argv[1]:
        j["state"] = "running"
        j["last_relaunch_ts"] = time.time()
json.dump(jobs, open(p, "w"), indent=2)
PY
      tmux new-session -d -s "$A" "bash $REL"
      continue
    fi
    # Otherwise: snapshot-then-dispatch a one-shot triage. mv is atomic (locked
    # against router appends), so the backgrounded handler reads a stable file.
    SNAP="$IDIR/dispatch_${A}_$(date +%s).txt"
    { flock 9; mv "$MB" "$SNAP"; } 9>"$IDIR/mailbox_${A}.lock"
    echo "[inbox] $(date) unread live-mail for $A but worker exited -> resuming to process ($SNAP)" >> $LOG
    setsid bash scratch_inbox_handle.sh "$A" "resume" "$S" "$SNAP" "" < /dev/null >> $LOG 2>&1 &
    cat "$SNAP" >> "$IDIR/mailbox_${A}.processed.md"
  done
  shopt -u nullglob
  # SELF-UPDATE hook: adopt an edited script without killing the tmux.
  if [ -f "$IDIR/RESTART_LOOP" ]; then
    rm -f "$IDIR/RESTART_LOOP"
    if bash -n "$0" 2>>$LOG; then
      echo "[inbox] $(date) RESTART_LOOP -> exec new inode md5=$(md5sum "$0" | cut -d' ' -f1)" >> $LOG
      exec bash "$0"
    else
      echo "[inbox] $(date) RESTART_LOOP requested but bash -n FAILED -> keeping current code" >> $LOG
    fi
  fi
  sleep 30
done
