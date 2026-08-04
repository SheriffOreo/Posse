#!/usr/bin/env bash
# Task 170: generate scratch_worker_<name>_relaunch.sh from the CANONICAL
# relaunch template. Factored out of scratch_spawn_worker.sh so that existing
# workers' relaunch scripts can be regenerated in place whenever the template
# evolves (spawn_worker calls this at spawn time; regen loops call it per
# worker). Requires scratch_full_logs/worker_<name>_prompt.md to exist.
#
#   Usage: scratch_gen_relaunch.sh <name> <session_id> <requester_email> [model] [effort] [precinct] [task_uid]
#
# Template features (history):
#   Task 123: drain mailbox under flock + mechanical ack BEFORE claude runs.
#   Task 124/153B: GPU+CPU survivor snapshots injected into the resume prompt.
#   Task 156: sentinel trap (rc=0 without done-sentinel -> bounded nudges).
#   Task 170 F1: origin-aware ack — ack ONLY when a genuine user 'uid=' origin
#     is present in the drained mail's '=== ' headers (legacy '(uid=NNN)'
#     headers count too); agent relays get NO mechanical ack. The uid ack is
#     threaded onto the originating email's subject (receipt lookup + F6
#     --thread).
#   Task 170 F5: (a) strace wrapper when WORKER_STRACE=1 or flag file
#     scratch_full_logs/inbox/strace_<name> exists (and strace is on PATH) —
#     signal-only trace to scratch_full_logs/strace_<name>_<epoch>.log;
#     (b) unconditional forensic death bundle appended to
#     scratch_full_logs/worker_<name>_deaths.log when claude exits with a
#     signal-shaped rc (129/137/139/143).
#   Task 170: INBOX_DRYRUN=1 now also SKIPS the claude invocation (prints the
#     constructed command instead) so the whole script is dry-runnable.
#   Task 176: the resume prompt is fed to claude via STDIN, never argv. The
#     prompt embeds ps-derived snapshot lines (and mail text); as an argv token
#     those strings sat in the worker's own /proc cmdline, so an agent running
#     `pkill -f <its own job's cmdline>` matched and SIGTERMed its own claude +
#     strace + tool shell (the noaa_fullds Jul-14 00:38/00:41 rapid deaths).
#     Stdin delivery removes the whole failure class. Also Task 176: the
#     generated script is written to a temp file and mv'd into place — a live
#     wrapper bash lazily reads its script from the old inode (fd 255), so
#     in-place `cat >` truncation would corrupt a running relaunch wrapper.
#   Task 185 (immediate reads, no cooldown): the boot drain now (a) runs its
#     critical section unconditionally and removes the relaunching_<name> boot
#     marker ATOMICALLY with the drain under the mailbox flock — that marker
#     (stamped by scratch_interrupt_worker.sh pre-kill) is how the router
#     coalesces mail arriving mid-boot into THIS drain instead of killing a
#     half-booted claude (the mail is still read immediately, batched); and
#     (b) parks the drained blob in inflight_<name>.md (+ .gen nonce) so that
#     if THIS claude is interrupted again before it plausibly processed the
#     mail (<180s), the interrupt entrypoint splices the blob back into the
#     mailbox and the next boot re-reads it — rapid-fire interrupts batch
#     mail, never lose it. A deadman subshell (survives the surgical kill by
#     design) clears the blob after 180s; the wrapper also clears it once
#     claude exits. The resume prompt additionally carries a SUBAGENT block
#     (scratch_subagent_snapshot.sh): spawned sub-workers are separate tmux
#     sessions the surgical kill structurally cannot touch — the resumed
#     parent must reconcile with them instead of re-spawning duplicates.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # the infra/ dir = code + state root
cd "$HERE"
NAME="${1:?worker name}"; SID="${2:?session id}"; REQUESTER="${3:?requester email}"
# Task 216 F2: default relaunch model is Opus 4.8 (half Fable's per-token
# price); Fable stays an explicit opt-in via the 4th positional arg. NOTE:
# regenerating an EXISTING worker's relaunch script without naming its model
# now flips it to opus — pass 'fable' explicitly for the pinned-to-fable
# sessions (paper, query, omp — see scratch_agents_registry.json / memories).
MODEL="${4:-opus}"; EFFORT="${5:-max}"
# Task 372/376: the precinct + case number so the relaunch script re-exports the
# context-header env (WORKER_PRECINCT/TSOMP_CASE/TSOMP_MODEL) across a revival.
PRECINCT="${6:-}"; TASK_UID="${7:-}"
LOG="scratch_full_logs/worker_${NAME}.log"
PROMPT="scratch_full_logs/worker_${NAME}_prompt.md"
RELAUNCH="scratch_worker_${NAME}_relaunch.sh"
[ -f "$PROMPT" ] || { echo "ERR: prompt file missing: $PROMPT" >&2; exit 1; }
# Recovery for older regen callers that don't pass precinct/task: read them back
# from the stable markers spawn_worker baked into the prompt (the 'PRECINCT LEDGER
# (<name>)' banner and the done-hook '--task <uid>' line). Keeps the relaunch
# header correct even when the caller omits the new positional args.
if [ -z "$PRECINCT" ]; then
  PRECINCT="$(grep -oE 'PRECINCT LEDGER \([^)]+\)' "$PROMPT" 2>/dev/null | head -1 | sed -E 's/.*\(([^)]+)\).*/\1/')"
fi
if [ -z "$TASK_UID" ]; then
  TASK_UID="$(grep -oE -- '--task[[:space:]]+[A-Za-z0-9_-]+' "$PROMPT" 2>/dev/null | head -1 | awk '{print $2}')"
fi

cat > "$RELAUNCH.tmp_gen" <<EOF
#!/usr/bin/env bash
# Portable runtime env: cd into the infra/ dir (code + state root), optionally
# activate a conda env (INFRA_CONDA_ENV), load Claude auth, and put claude on PATH.
source "$HERE/_daemon_env.sh"
# Task 325: auto-attribute any GPU job this worker enqueues (submit_gpu.py reads
# these) so the dashboard shows the owner instead of "?".
export GPU_JOB_OWNER="$NAME"
export TSOMP_AGENT="$NAME"
# Task 372/376: re-export the context-header env so a watchdog-revived deputy's
# emails still carry [precinct | case | deputy | model]. Baked at generation time
# (values below are literals) — mirrors GPU_JOB_OWNER surviving a relaunch.
export WORKER_PRECINCT="$PRECINCT"
export TSOMP_CASE="$TASK_UID"
export TSOMP_MODEL="$MODEL"
echo "[$NAME] RELAUNCH \$(date) resume=$SID" >> $LOG

# Task 170 F5(b): forensic bundle on signal-shaped claude exits (129=SIGHUP,
# 137=SIGKILL, 139=SIGSEGV, 143=SIGTERM). Captures what the Task 168 memo
# could not reconstruct after the fact: journal slice, peer-registry dir,
# processes still matching this session.
death_bundle() {
  case "\$1" in 129|137|139|143) ;; *) return 0;; esac
  { echo "=== [$NAME] death \$(date) rc=\$1 (129=SIGHUP 137=SIGKILL 139=SIGSEGV 143=SIGTERM) ==="
    echo "--- journalctl -S -3min (best-effort; may lack perms) ---"
    journalctl -S -3min --no-pager 2>/dev/null | tail -100 || true
    echo "--- ls -la ~/.claude/sessions/ ---"
    ls -la ~/.claude/sessions/ 2>/dev/null || true
    echo "--- ps -ef matching session $SID ---"
    ps -ef | grep "$SID" | grep -v grep || true
    echo "=== end death bundle rc=\$1 ==="
  } >> "scratch_full_logs/worker_${NAME}_deaths.log" 2>&1 || true
}

# Boot drain (Task 123 flock + Task 185 boot-marker/inflight semantics — see
# scratch_gen_relaunch.sh header). The critical section ALWAYS runs: it scoops
# any unread mail AND removes the relaunching_${NAME} boot marker atomically,
# closing the router's coalesce window at exactly the moment the scoop is
# guaranteed to include coalesced mail.
MB="scratch_full_logs/inbox/mailbox_${NAME}.md"
MAIL_CONTENT=\$( ( flock 9
  if [ -s "\$MB" ]; then
    cat "\$MB"
    cat "\$MB" >> "scratch_full_logs/inbox/mailbox_${NAME}.processed.md"
    : > "\$MB"
  fi
  rm -f "scratch_full_logs/inbox/relaunching_${NAME}"
  ) 9>"scratch_full_logs/inbox/mailbox_${NAME}.lock" )
# Task 185 inflight carry-forward: park the drained blob so an interrupt that
# kills this claude before it plausibly processed the mail (<180s) re-injects
# it (scratch_interrupt_worker.sh) and the next boot re-reads it. The deadman
# subshell below survives the surgical kill (it is a plain CPU child) and
# clears the blob after 180s; the .gen nonce keeps it from clearing a NEWER
# boot's blob.
BOOT_GEN=""
if [ -n "\$MAIL_CONTENT" ]; then
  BOOT_GEN="\$(date +%s%N)"
  printf '%s\n' "\$MAIL_CONTENT" > "scratch_full_logs/inbox/inflight_${NAME}.md"   # trailing \n: header-seam safety on re-inject
  printf '%s' "\$BOOT_GEN" > "scratch_full_logs/inbox/inflight_${NAME}.gen"
  ( sleep 180
    ( flock 9
      [ "\$(cat "scratch_full_logs/inbox/inflight_${NAME}.gen" 2>/dev/null)" = "\$BOOT_GEN" ] \\
        && rm -f "scratch_full_logs/inbox/inflight_${NAME}.md" "scratch_full_logs/inbox/inflight_${NAME}.gen"
    ) 9>"scratch_full_logs/inbox/mailbox_${NAME}.lock"
  ) >/dev/null 2>&1 &
fi
clear_inflight() {  # once claude has run, the injected mail counts as processed
  ( flock 9
    [ "\$(cat "scratch_full_logs/inbox/inflight_${NAME}.gen" 2>/dev/null)" = "\$BOOT_GEN" ] \\
      && rm -f "scratch_full_logs/inbox/inflight_${NAME}.md" "scratch_full_logs/inbox/inflight_${NAME}.gen"
  ) 9>"scratch_full_logs/inbox/mailbox_${NAME}.lock" 2>/dev/null || true
}
# Task 124: the kill did NOT touch GPU work (jobs run under the separate
# gpu_manager session). Task 153B: the kill is surgical (claude process only),
# so plain CPU children survived too. Task 185: spawned subagent workers are
# separate tmux sessions — also untouched. Capture all three so the resumed
# agent is not blind to them and never re-submits, re-runs or re-spawns live
# work.
GPU_SNAPSHOT="\$(bash scratch_gpu_snapshot.sh "$NAME" 2>&1 || echo '(gpu snapshot tool failed)')"
GPU_BLOCK="=== GPU TASKS STILL RUNNING (survived the interrupt) ===
Your tmux kill did NOT cancel GPU work: jobs submitted via submit_gpu keep running under
the separate gpu_manager session and their results land in gpu_queue/done/ as usual.
Do NOT re-submit or restart anything listed as RUNNING/PENDING below — reconcile jobs
that finished while you were down (RECENTLY FINISHED below) from gpu_queue/done/, poll
gpu_queue/done/<id>.json for the rest, and resume monitoring. Only change plan if the
email tells you to. (Processes tagged OUTSIDE-the-queue are alive but untracked — their
results will NOT appear in gpu_queue/done/. Since Task 153B plain CPU children of your
old process survive the interrupt too — see the CPU block below.)

\$GPU_SNAPSHOT
=== END GPU TASKS ==="
# Task 153B: list the CPU jobs that survived the surgical kill (manifest written
# by scratch_kill_agent_only.py at interrupt time, re-verified live here).
CPU_SNAPSHOT="\$(bash scratch_cpu_snapshot.sh "$NAME" 2>&1 || echo '(cpu snapshot tool failed)')"
CPU_BLOCK="=== CPU JOBS THAT SURVIVED THE INTERRUPT (Task 153B) ===
The interrupt killed ONLY your claude process. Plain CPU children you were running
(foreground eval/scoring/report/driver runs, backgrounded shells) kept running as
orphans; each one's stdout/stderr keeps streaming into the .output file listed below.
Do NOT re-run anything listed ALIVE — tail its .output file / check its artifacts and
resume monitoring instead. Jobs listed 'gone' finished or died while you were down:
check their artifacts before deciding anything needs a re-run.
(Task 185 note: your own mailbox poll loops that survived the kill can no longer eat
your mail — scratch_read_mailbox.sh refuses to drain for callers with no live claude
ancestor. A leftover 'sleep 180' survivor is the inflight deadman-clear; ignore it.)

\$CPU_SNAPSHOT
=== END CPU JOBS ==="
# Task 185: subagent workers you spawned survived too — reconcile, don't re-spawn.
SUB_SNAPSHOT="\$(bash scratch_subagent_snapshot.sh "$NAME" 2>&1 || echo '(subagent snapshot tool failed)')"
SUB_BLOCK="=== SUBAGENT WORKERS STILL RUNNING (survived the interrupt) ===
Worker tmux sessions are independent of your process tree: the interrupt killed ONLY
your claude, never your spawned sub-workers. Any sub-worker YOU launched via
scratch_spawn_worker.sh that is listed below is still working its task — do NOT
re-spawn it; check its log / emails and resume coordinating. (The list shows all live
registered worker sessions, yours and others' — kill NOTHING here unless your task
explicitly supersedes it.)

\$SUB_SNAPSHOT
=== END SUBAGENT WORKERS ==="
if [ -n "\$MAIL_CONTENT" ]; then
  # Task 170 F1: parse the ORIGIN of the drained mail from its '=== ' header
  # lines. 'uid=NNN' (new 'mail uid=N from <sender>' headers AND the legacy
  # '(uid=NNN)' router header) = user mail -> truthful mechanical ack, threaded
  # onto the originating subject. No uid anywhere = agent relay(s) only -> NO
  # mechanical ack (the resumed worker sends the substantive reply itself;
  # an auto-ack here would misattribute agent relays as user mail — Task 168 Q1).
  MAIL_UIDS=\$(printf '%s\n' "\$MAIL_CONTENT" | grep '^=== ' | grep -oE 'uid=[0-9]+' | sed 's/uid=//' | sort -un | paste -sd, -)
  if [ -n "\$MAIL_UIDS" ]; then
    FIRST_UID="\${MAIL_UIDS%%,*}"
    THREAD_SUBJ=\$(python3 -c 'import glob,json,sys; fs=sorted(glob.glob("scratch_full_logs/inbox/receipts/%s.json*" % sys.argv[1])); print(json.load(open(fs[0])).get("subject","") if fs else "")' "\$FIRST_UID" 2>/dev/null || true)
    MAIL_KIND="an incoming email from the user (uid=\$MAIL_UIDS)"
    if [ -n "\${INBOX_DRYRUN:-}" ]; then
      echo "[DRYRUN-ACK $NAME] would send user-ack: 'ACK: worker $NAME is reading your email now (uid=\$MAIL_UIDS)' thread='\$THREAD_SUBJ'"
    else
      python scratch_notify_email.py "ACK: worker $NAME is reading your email now (uid=\$MAIL_UIDS)" "Auto-ack from the relaunch script: your email (uid=\$MAIL_UIDS) interrupted '$NAME', which just resumed with your mail injected at the front of its context and is reading it NOW. Its CPU jobs, GPU jobs and spawned subagents kept running uninterrupted throughout. A substantive reply follows; the watchdog alerts if it stays silent >15 min." --agent "$NAME" --to "$REQUESTER" \${THREAD_SUBJ:+--thread "\$THREAD_SUBJ"} >/dev/null 2>&1 || true
    fi
  else
    MAIL_KIND="a message relayed by another agent (internal relay — the mail's header names the sender agent; the user did NOT email you just now)"
    if [ -n "\${INBOX_DRYRUN:-}" ]; then
      echo "[DRYRUN-NOACK $NAME] relay-only mail (no uid= origin) -> no mechanical ack"
    fi
    echo "[$NAME] drained relay-only mail (no uid= origin) -> no mechanical ack" >> $LOG
  fi
  RESUME_PROMPT="URGENT: You were interrupted by \$MAIL_KIND. Your GPU jobs, CPU jobs and spawned subagent workers were NOT killed — snapshots of all three follow the message below. Read the message, then FIRST send a brief reply email NOW (the full answer, or a 2-line plan + ETA if it needs long work) BEFORE resuming any other work:

REMINDER — THE WAIT RULES (Task 216): nothing re-invokes you when a job finishes by itself except the jobmgr daemon, and only for jobs you submitted to it. Waits <= 50 min estimated: STAY ALIVE and poll the result file (gpu_queue/done/<id>.json or scratch_full_logs/jobs/done/<id>.json) in a blocking FOREGROUND loop with sleep chunks of AT MOST 240 s — 4 min keeps the prompt cache warm (the TTL is 5 min; a longer gap makes every poll a full-context rewrite, 12.5x dearer). Waits > 50 min: submit the job via 'bash scratch_submit_job.sh $NAME <est_seconds> [--gpu] -- <command...>', finish any other pending work, then park with 'bash scratch_job_sleep.sh $NAME' and END YOUR TURN — jobmgr wakes you with 'JOB <id> DONE ...' injected. Never end your turn to 'wait' any other way — that exits the process and nothing will bring it back.

=== INCOMING MESSAGE ===
\$MAIL_CONTENT
=== END MESSAGE ===

\$GPU_BLOCK

\$CPU_BLOCK

\$SUB_BLOCK

\$(cat "$PROMPT")"
else
  RESUME_PROMPT="NOTE: you were relaunched with no new mail (watchdog revival / session restart). Your GPU jobs, CPU jobs and spawned subagent workers were NOT killed — reconcile against the snapshots below before doing anything else. If you were relaunched because you ended your turn to 'wait' for a job: that only works when you had parked via scratch_job_sleep.sh on a jobmgr-submitted job — see THE WAIT RULES below.

REMINDER — THE WAIT RULES (Task 216): nothing re-invokes you when a job finishes by itself except the jobmgr daemon, and only for jobs you submitted to it. Waits <= 50 min estimated: STAY ALIVE and poll the result file (gpu_queue/done/<id>.json or scratch_full_logs/jobs/done/<id>.json) in a blocking FOREGROUND loop with sleep chunks of AT MOST 240 s — 4 min keeps the prompt cache warm (the TTL is 5 min; a longer gap makes every poll a full-context rewrite, 12.5x dearer). Waits > 50 min: submit the job via 'bash scratch_submit_job.sh $NAME <est_seconds> [--gpu] -- <command...>', finish any other pending work, then park with 'bash scratch_job_sleep.sh $NAME' and END YOUR TURN — jobmgr wakes you with 'JOB <id> DONE ...' injected. Never end your turn to 'wait' any other way — that exits the process and nothing will bring it back.

\$GPU_BLOCK

\$CPU_BLOCK

\$SUB_BLOCK

\$(cat "$PROMPT")"
fi

# Task 170 F5(a): optional strace instrumentation of the claude process —
# armed per-worker by 'touch scratch_full_logs/inbox/strace_${NAME}' or
# globally by WORKER_STRACE=1. Signal-only trace (bounded volume); names the
# si_pid of any SIGTERM sender, which is exactly what the Task 168 rc=143
# investigation lacked.
STRACE_PREFIX=()
if { [ "\${WORKER_STRACE:-}" = "1" ] || [ -f "scratch_full_logs/inbox/strace_${NAME}" ]; } && command -v strace >/dev/null 2>&1; then
  STRACE_LOG="scratch_full_logs/strace_${NAME}_\$(date +%s).log"
  STRACE_PREFIX=(strace -f -tt -e trace=signal -o "\$STRACE_LOG")
  echo "[$NAME] F5 strace armed -> \$STRACE_LOG" >> $LOG
fi

if [ -n "\${INBOX_DRYRUN:-}" ]; then
  echo "[DRYRUN-EXEC $NAME] would run: \${STRACE_PREFIX[*]} claude --resume $SID -p --dangerously-skip-permissions --model $MODEL --effort $EFFORT --max-turns 800 --verbose  <<< RESUME_PROMPT via stdin (\${#RESUME_PROMPT} chars)"
  exit 0
fi

# Task 176: prompt via STDIN, not argv — argv-embedded snapshot text made the
# worker's own \`pkill -f\` match its own claude (self-kill). Pipeline rc =
# claude's rc (last command), so RC/death_bundle semantics are unchanged.
printf '%s' "\$RESUME_PROMPT" | "\${STRACE_PREFIX[@]}" claude --resume "$SID" -p \\
  --dangerously-skip-permissions --model $MODEL --effort $EFFORT --max-turns 800 --verbose >> $LOG 2>&1
RC=\$?
clear_inflight
echo "[$NAME] RELAUNCH EXITED rc=\$RC \$(date)" >> $LOG
death_bundle "\$RC"
# Task 156 F4: sentinel trap. rc=0 without the done-sentinel = the worker ended
# its turn to 'wait' (armed-waiter) — nudge-resume it, bounded. rc!=0 is left
# to the watchdog (never loop on crashes).
SENTINEL="scratch_full_logs/worker_${NAME}.done"
NUDGES=0
while [ "\$RC" -eq 0 ] && [ ! -f "\$SENTINEL" ] && [ "\$NUDGES" -lt 3 ]; do
  NUDGES=\$((NUDGES+1))
  echo "[$NAME] SENTINEL-TRAP nudge \$NUDGES/3 \$(date)" >> $LOG
  printf '%s' "You exited rc=0 without your done-sentinel. If the task IS fully complete (FINAL email sent, all deliverables exist), run: touch \$SENTINEL — then stop. Otherwise resume the work SYNCHRONOUSLY in the foreground until everything is done, then touch the sentinel. To wait on a job: <=50 min estimated -> foreground poll loop with sleep chunks <=240 s; >50 min -> bash scratch_submit_job.sh $NAME <est_seconds> [--gpu] -- <command...> then bash scratch_job_sleep.sh $NAME and end your turn (jobmgr wakes you). Never end your turn to 'wait' any other way." | "\${STRACE_PREFIX[@]}" claude --resume "$SID" -p \\
    --dangerously-skip-permissions --model $MODEL --effort $EFFORT --max-turns 800 --verbose >> $LOG 2>&1
  RC=\$?
  echo "[$NAME] SENTINEL-TRAP EXITED rc=\$RC \$(date)" >> $LOG
  death_bundle "\$RC"
done
EOF
# Task 176: validate the temp, then swap atomically (mv = new inode; a live
# wrapper bash keeps executing its old inode untouched).
bash -n "$RELAUNCH.tmp_gen" || { echo "ERR: generated $RELAUNCH.tmp_gen fails bash -n" >&2; exit 1; }
mv -f "$RELAUNCH.tmp_gen" "$RELAUNCH"
echo "generated $RELAUNCH (name=$NAME session=${SID:0:8}… model=$MODEL effort=$EFFORT; prompt-delivery=stdin; task185 boot-marker/inflight/subagent)"
