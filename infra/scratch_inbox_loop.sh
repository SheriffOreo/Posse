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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "$HERE/_daemon_env.sh"
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

# Resolve a worker's relaunch script. The watchdog roster is NOT durable — it holds
# only currently-tracked workers, and a truncation or a relaunch-without-spawn drops
# an entry permanently — while the script itself persists on disk for every deputy
# ever spawned. Consulting the roster ALONE therefore dropped most ended deputies
# onto the one-shot handler, which resumes them with none of their case settings.
# The interrupt entrypoint already had this fallback; the loop lacked it.
relaunch_script_for() {
  local a="$1" rel=""
  rel=$(python3 -c "import json;j=[x for x in json.load(open('scratch_full_logs/watchdog_jobs.json')) if x.get('name')=='$a'];print(j[-1].get('relaunch') or '' if j else '')" 2>/dev/null)
  if [ -z "$rel" ] || [ ! -f "$rel" ]; then rel="scratch_worker_${a}_relaunch.sh"; fi
  [ -f "$rel" ] && printf '%s' "$rel"
}

# Put a revived deputy back on the settings its case was created with.
#  - restore-lane: a SPLIT case that closed in the REPORT lane would otherwise come
#    back as the report writer. The work lane is the one a deputy runs on.
#  - roster re-seed: only a fresh SPAWN appends to the watchdog roster, so a deputy
#    revived after its entry was lost would run UNSUPERVISED. Re-add it here.
restore_case_settings() {
  local a="$1" rel="$2"
  python3 scratch_model_switch.py restore-lane --worker "$a" --lane work >> $LOG 2>&1 || true
  python3 - "$a" "$rel" <<'PY' >> $LOG 2>&1 || true
import json, os, sys, time
name, rel = sys.argv[1], sys.argv[2]
p = "scratch_full_logs/watchdog_jobs.json"
jobs = json.load(open(p))
entry = next((j for j in jobs if isinstance(j, dict) and j.get("name") == name), None)
if entry is None:
    reg = json.load(open("scratch_agents_registry.json")).get("workers", {}).get(name, {})
    # Key names must match the watchdog's schema, not merely look like it:
    # classify() reads job["done_sentinel"] and relaunch() reads job["relaunched"].
    # A "sentinel"/"attempts" entry would make the watchdog blind to the deputy's
    # own closure -> exited_incomplete -> relaunch loop -> failed, i.e. the re-seed
    # meant to restore supervision would guarantee an alarm instead.
    jobs.append({"name": name, "session": reg.get("session", ""), "relaunch": rel,
                 "log": f"scratch_full_logs/worker_{name}.log",
                 "done_sentinel": f"scratch_full_logs/worker_{name}.done",
                 "state": "running", "relaunched": 0, "last_relaunch_ts": time.time(),
                 "reseeded_by": "followup"})
    tmp = p + ".tmp_reseed"
    with open(tmp, "w") as f:
        json.dump(jobs, f, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)
    print(f"[inbox] re-seeded watchdog roster entry for {name}")
PY
}

while true; do
  OUT=$(python scratch_inbox.py next 2>>$LOG)
  if [[ "$OUT" == UID=* ]]; then
    MUID=$(echo "$OUT"   | grep -oP 'UID=\K[^\t]+')
    AGENT=$(echo "$OUT" | grep -oP 'AGENT=\K[^\t]+')
    TYPE=$(echo "$OUT"  | grep -oP 'TYPE=\K[^\t]+')
    SESSION=$(echo "$OUT" | grep -oP 'SESSION=\K[^\t]+')
    BODYFILE=$(echo "$OUT" | grep -oP 'BODYFILE=\K[^\t]+')
    SUBJECT=$(echo "$OUT"  | grep -oP 'SUBJECT=\K.*$' | cut -f1)
    # Phase D (Case 384d): a YES/NO reply to a sheriff DELETE-CONFIRMATION carries a
    # live token. The sheriff is a zero-API daemon (not a mail target), so we match
    # the token MECHANICALLY and drop a confirmation record the sheriff reads next
    # pass -- and do NOT route this reply to any worker. Robust + decoupled: only a
    # LIVE token + a CLEAR yes/no intercepts; anything else (no token, stale token,
    # unclear answer) reports PASSTHROUGH and routes normally, so a stray reply can
    # never wedge anything or trigger a delete.
    CONFIRM=$(python scratch_sheriff_request.py confirm-check --subject "$SUBJECT" --bodyfile "$BODYFILE" --uid "$MUID" 2>>$LOG)
    if [[ "$CONFIRM" == INTERCEPTED* ]]; then
      echo "[inbox] $(date) sheriff delete-confirm reply uid=$MUID -> $CONFIRM (recorded for the sheriff; not routed)" >> $LOG
      python scratch_inbox.py mark --uid "$MUID" >> $LOG 2>&1
      continue
    fi
    # LIVE worker (identity = its tmux session, exact match) -> mailbox delivery
    # + immediate interrupt (Task 185 policy lives in scratch_inbox_deliver.sh);
    # marking-seen happens inside the helper only after the durable mailbox
    # copy is verified.
    if [ "$TYPE" = "resume" ] && [ -n "$SESSION" ] && [ "$SESSION" != "None" ] && tmux has-session -t "=$AGENT" 2>/dev/null; then
      bash scratch_inbox_deliver.sh "$AGENT" "$MUID" "$BODYFILE" "$SUBJECT" >> $LOG 2>&1
    else
      # Feng uid=378 (Task 377): a follow-up to a resume-type deputy whose tmux is
      # GONE (it finished + touched its done-sentinel) must FULLY RELAUNCH that
      # deputy as a persistent, WATCHDOG-TRACKED worker — so it can TAKE the new
      # case to completion if it decides to — NOT the one-shot triage handler that
      # exits right after replying (which physically prevented the original deputy
      # from taking the case). We deliver the mail to its mailbox (origin-headed) +
      # write a receipt + mark seen HERE; the persistent-relaunch fallback below
      # (unread-mail + tmux-gone + registered relaunch script) brings the deputy
      # back THIS SAME iteration with the mail injected at the front of its prompt.
      # Only a brand-new / no-session contact still uses the one-shot handler.
      REL=$(relaunch_script_for "$AGENT")
      if [ "$TYPE" = "resume" ] && [ -n "$SESSION" ] && [ "$SESSION" != "None" ] && [ -n "$REL" ] && [ -f "$REL" ]; then
        SENDER_L="$(sed -n 's/^FROM:[[:space:]]*//p' "$BODYFILE" 2>/dev/null | head -1)"
        echo "[inbox] $(date) follow-up uid=$MUID for finished deputy $AGENT -> mailbox + PERSISTENT relaunch (uid=378 policy) ($SUBJECT)" >> $LOG
        # Restore the case's OWN settings before the relaunch runs.
        restore_case_settings "$AGENT" "$REL"
        # A finished deputy left a stale done-sentinel; clear it so the relaunched,
        # watchdog-tracked deputy is properly tracked as RUNNING while it works the
        # new case (it re-touches its sentinel when it closes the follow-up). Without
        # this the sentinel-trap would treat its rc=0 as "already done" and never
        # nudge it, and the watchdog would think it completed.
        rm -f "scratch_full_logs/worker_${AGENT}.done"
        if bash scratch_mailbox_append.sh "$AGENT" "uid=$MUID from ${SENDER_L:-user}" "$BODYFILE" >> $LOG 2>&1; then
          python3 - "$AGENT" "$MUID" "$SUBJECT" "$IDIR/receipts/$MUID.json" <<'PY' 2>>$LOG || true
import json, sys, time
agent, uid, subject, dest = sys.argv[1:5]
json.dump({"agent": agent, "uid": uid, "subject": subject, "ts": time.time()}, open(dest, "w"))
PY
          python scratch_inbox.py mark --uid "$MUID" >> $LOG 2>&1
        else
          echo "[inbox] $(date) mailbox append failed for $AGENT uid=$MUID -> left unseen for retry" >> $LOG
        fi
      else
        echo "[inbox] $(date) new reply uid=$MUID -> agent=$AGENT ($SUBJECT)" >> $LOG
        setsid bash scratch_inbox_handle.sh "$AGENT" "$TYPE" "$SESSION" "$BODYFILE" "$MUID" < /dev/null >> $LOG 2>&1 &
        python scratch_inbox.py mark --uid "$MUID" >> $LOG 2>&1
      fi
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
    REL=$(relaunch_script_for "$A")
    if [ -n "$REL" ] && [ -f "$REL" ]; then
      touch "$IDIR/last_relaunch_$A"
      echo "[inbox] $(date) unread live-mail for $A -> relaunching persistent worker via $REL" >> $LOG
      # Same settings restore as the routed follow-up path above — this branch
      # revives an ended deputy too, just via its unread mailbox.
      restore_case_settings "$A" "$REL"
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
  # Task 377 #4: process any web-form "Create new case" submissions dropped by the
  # dashboard POST. Fire-and-forget + self-claiming (flock) + robust (each record is
  # moved to failed/ on error), so a bad upload can NEVER wedge the router. The
  # bridge writes the spec, spawns the deputy, and emails the ACK-with-attachments.
  if compgen -G "scratch_full_logs/web_cases/pending/*.json" > /dev/null 2>&1; then
    echo "[inbox] $(date) web-case submission(s) pending -> running scratch_web_case.py bridge" >> $LOG
    setsid bash -c "cd \"$HERE\" && python3 scratch_web_case.py process" < /dev/null >> $LOG 2>&1 &
  fi
  # Case 384e / Phase E: materialize any JTF (Joint Task Force) submissions dropped by
  # the dashboard POST /api/jtf. Same fire-and-forget + self-claiming (flock) + robust
  # (bad record -> failed/) contract as the web-case bridge, so a bad JTF submission can
  # NEVER wedge the router. The bridge spawns precinct-slot deputies, injects must-take
  # assignments into specific-deputy slots, and emails the ACK.
  if compgen -G "scratch_full_logs/jtf/pending/*.json" > /dev/null 2>&1; then
    echo "[inbox] $(date) JTF submission(s) pending -> running scratch_jtf.py bridge" >> $LOG
    setsid bash -c "cd \"$HERE\" && python3 scratch_jtf.py process" < /dev/null >> $LOG 2>&1 &
  fi
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
