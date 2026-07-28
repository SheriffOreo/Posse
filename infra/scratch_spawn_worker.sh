#!/usr/bin/env bash
# Spawn a PERSISTENT tmux worker agent to take charge of a task to completion,
# emailing the requester updates. Used by the email handler so that emailed TASKS
# get a real working agent (not run inline in the one-shot handler, which exits).
#
# Usage: scratch_spawn_worker.sh <name> <taskfile> <requester_email> [keywords] [--dry]
#   <name>      short tmux/worker name (lowercase, [a-z0-9_-]); also the email --agent tag
#   <taskfile>  path to a self-contained task spec (markdown)
#   <requester> email to send updates to (the person who emailed)
#   [keywords]  comma-separated reply-routing keywords (default: name)
#   [--dry]     validate + print without registering or launching
#
# Task 123 hardening: the generated relaunch script drains the mailbox under
# flock and sends a MECHANICAL ack email before invoking claude (reply latency
# no longer depends on the model); the worker prompt carries a REPLY-FIRST rule.
# Task 124: interrupt kills are safe for GPU work (jobs run under the separate
# gpu_manager session), so the relaunch script injects a scratch_gpu_snapshot.sh
# block into the resume prompt and the prompt preamble tells the agent its GPU
# jobs survived (never scancel/qdel/restart-from-scratch — those never existed).
# Task 153B: interrupt kills are surgical (scratch_kill_agent_only.py SIGKILLs
# only the claude process), so plain CPU children survive too; the relaunch
# script additionally injects a scratch_cpu_snapshot.sh block listing them.
# Task 156 (noaa_fullds 42h dead-worker incident): F4 — both generated scripts
# now carry a sentinel trap: claude exiting rc=0 WITHOUT the done-sentinel (an
# armed-waiter death) gets up to 3 bounded --resume nudges; rc!=0 is never
# looped on (crash handling stays with the watchdog). F6 — the prompt preamble
# and both relaunch RESUME_PROMPT branches now teach the correct wait idiom
# (foreground polling of gpu_queue/done/) instead of just prohibiting waiting.
set -euo pipefail
cd /home/steven/Projects/time-series-omp
# Task 216 F2: workers default to Opus 4.8 (half Fable's per-token price; the
# July audit showed worker turns are dominated by bash+bookkeeping, not
# frontier reasoning). Fable stays an explicit opt-in for hard tasks:
#   WORKER_MODEL=fable bash scratch_spawn_worker.sh <name> <spec> <requester>
WORKER_MODEL="${WORKER_MODEL:-opus}"
NAME="${1:?worker name}"; TASKFILE="${2:?task spec file}"; REQUESTER="${3:?requester email}"
KEYWORDS="${4:-$NAME}"; DRY=""
[ "${4:-}" = "--dry" ] && { KEYWORDS="$NAME"; DRY=1; }
[ "${5:-}" = "--dry" ] && DRY=1
NAME="$(echo "$NAME" | tr 'A-Z ' 'a-z_' | tr -cd 'a-z0-9_-')"
[ -f "$TASKFILE" ] || { echo "ERR: taskfile not found: $TASKFILE" >&2; exit 1; }
SID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
PROMPT="scratch_full_logs/worker_${NAME}_prompt.md"
LAUNCH="scratch_worker_${NAME}_launch.sh"
LOG="scratch_full_logs/worker_${NAME}.log"

# 1) build the worker prompt = synchronous-discipline preamble + the task spec
{
  cat <<EOF
You are a PERSISTENT worker agent running headless in tmux for the time-series-omp
project (/home/steven/Projects/time-series-omp). Env:
  source ~/anaconda3/etc/profile.d/conda.sh && conda activate tsomp

FIRST: read CLAUDE.md in the repo root — it is the onboarding doc (project overview +
the conventions every agent here operates under). The rules below summarize it.

CRITICAL: you are a headless \`claude -p\` process. There is NO background
re-invocation and NO "armed waiter" — if you background a job and end your turn,
it DIES and the task never finishes. Run ALL work SYNCHRONOUSLY in the FOREGROUND
(each batch a blocking bash call that returns only when done) and DO NOT end your
turn until every deliverable exists and you have sent the FINAL email.

THE WAIT RULES (Task 216 — the July \$10k lesson; follow exactly): nothing
re-invokes you when a job finishes by itself — you either poll in the foreground
or hand the wait to the jobmgr daemon and sleep. Choose by ESTIMATED RUNTIME,
threshold 50 minutes:
  <= 50 min -> POLL: launch via scratch_detach.sh / submit_gpu.py, then poll the
    result file in a blocking FOREGROUND loop with sleep chunks of AT MOST 240 s
    (4 min). NEVER sleep longer per chunk: the prompt cache TTL is 5 min, so a
    <=4-min cadence keeps every poll a warm cache READ (12.5x cheaper than the
    cold context REWRITE that a longer gap forces).
  >  50 min -> SUBMIT + SLEEP (event-wake): submit the job to the jobmgr daemon:
      bash scratch_submit_job.sh $NAME <est_seconds> [--gpu] -- <command...>
    finish any other pending work, then park yourself and END YOUR TURN:
      bash scratch_job_sleep.sh $NAME
    jobmgr wakes you the moment the job finishes ('JOB <id> DONE ...' injected
    via the interrupt+relaunch path). scratch_job_sleep.sh is what makes ending
    your turn SAFE here — it parks you so neither the sentinel trap nor the
    watchdog relaunch-loops you; the wake reverses it. If a user email wakes you
    early, handle it, then re-park (scratch_job_sleep.sh again) if your job is
    still running. Do NOT end your turn to 'wait' any other way — that exits the
    process and nothing will bring it back.

DETACH RULE (Task 165): any compute expected to run >2 min MUST be launched via
  bash scratch_detach.sh $NAME <job> -- <command...>
(own session + pid file, output streams to scratch_full_logs/${NAME}_<job>.output),
then handled per THE WAIT RULES above (foreground <=4-min polls, or jobmgr
submit+sleep when >50 min — scratch_submit_job.sh does the detach for you). NEVER
use the harness run_in_background mechanism for such compute — those tasks die
with your claude process even on a NORMAL exit (end of turn / usage limit), not
just on interrupts. scratch_cpu_snapshot.sh $NAME lists your detached jobs on any
resume.

Email the requester ($REQUESTER) via:
  python scratch_notify_email.py "<subject>" "<body>" --agent $NAME --to $REQUESTER [--attach <fig>]
The mailer auto-addresses Steven ("Hi Steven,") and signs your agent name at the end —
write just the message body, no greeting or signature of your own.
Send: (a) a short START/plan email now, (b) milestone update(s), (c) a FINAL email
with results + any key figure attached. Keep emails terse with concrete numbers.

LIVE FEEDBACK — MANDATORY INTERRUPT PROTOCOL:
Your session may be killed mid-task by the inbox router when the user emails you.
Upon ANY resumption (start or relaunch), your VERY FIRST ACTION must be:

  MAILBOX=\$(bash scratch_read_mailbox.sh $NAME)
  # If MAILBOX is non-empty: read it, reply to it, and decide whether to change plan

REPLY-FIRST RULE: whenever you resume with an email injected at the front of this
prompt (or find your mailbox non-empty), your FIRST action must be to send a reply
email — the full answer if quick, else a 2-line ACK with your plan + ETA — BEFORE
resuming any heavy work. A mechanical auto-ack may already have been sent; your
substantive reply must still follow.

If the mailbox contains a plan-change instruction:
  1. Email the user an ACK with your new plan
  2. Your jobs SURVIVE interrupts — GPU and CPU alike (Task 153B: only your claude
     process is killed). GPU: submit_gpu work keeps running under the separate
     gpu_manager tmux and its results land in gpu_queue/done/. CPU: plain children
     (foreground eval/scoring/report/driver runs) keep running as orphans, their
     stdout/stderr still streaming into the .output file named in the CPU snapshot.
     Snapshots of both are injected at the top of your resume prompt; reprint with:
       bash scratch_gpu_snapshot.sh $NAME   and   bash scratch_cpu_snapshot.sh $NAME
     Do NOT re-submit or restart anything listed RUNNING/PENDING/ALIVE — reconcile
     finished work (gpu_queue/done/, job artifacts / .output files) and resume
     monitoring the rest.
  3. Only if the new plan makes one of YOUR OWN queued jobs obsolete, cancel it by
     deleting its json from gpu_queue/pending/ (never touch running/ or done/).
  4. Kill only sub-agents whose work the new plan supersedes:
     tmux kill-session -t "=<sub_agent_name>" (their GPU jobs survive under gpu_manager
     too, but a whole-session kill DOES take down the sub-agent's plain CPU children —
     use python3 scratch_kill_agent_only.py <sub_agent_name> instead if those must live)
  5. Adjust the plan and continue from what already exists — do not restart work that
     is still running or already finished.

Even without an interrupt, continue to check the mailbox between every major step
(before and after each long operation) using: bash scratch_read_mailbox.sh $NAME

When you are FULLY finished (FINAL email sent, all deliverables exist), run:
  touch scratch_full_logs/worker_${NAME}.done
so the watchdog knows you completed and will not relaunch you.

If you need to delegate a heavy subtask to a sub-agent, ALWAYS launch it via:
  bash scratch_spawn_worker.sh <sub_name> <subtask_spec.md> $REQUESTER "<keywords>"
(never a raw \`claude\`/setsid/background process) — that auto-registers the sub-agent
with the watchdog so it is monitored and auto-relaunched on a session-limit kill too.
Pick a unique <sub_name> and write a self-contained subtask spec.

Guardrails: no git commit/push; do not delete data/outputs; do not kill running
jobs; stay within the lane the task specifies.

================================ TASK ================================
EOF
  cat "$TASKFILE"
} > "${PROMPT}.tmp"

if [ -n "$DRY" ]; then
  echo "[dry] would register worker '$NAME' session=$SID keywords='$KEYWORDS'"
  echo "[dry] prompt -> ${PROMPT} ($(wc -l < "${PROMPT}.tmp") lines); tmux session '$NAME'"
  echo "[dry] ----- generated worker prompt -----"
  cat "${PROMPT}.tmp"
  rm -f "${PROMPT}.tmp"; exit 0
fi
mv "${PROMPT}.tmp" "$PROMPT"

# 2) register so the user's replies to this worker's emails route back to it
python3 - "$NAME" "$SID" "$KEYWORDS" <<'PY'
import json, sys
name, sid, kw = sys.argv[1], sys.argv[2], sys.argv[3]
p = "scratch_agents_registry.json"
r = json.load(open(p))
r["workers"][name] = {"type": "resume", "session": sid,
                      "match": [k.strip().lower() for k in kw.split(",") if k.strip()],
                      "desc": f"email-spawned task worker ({name})"}
json.dump(r, open(p, "w"), indent=2)
print(f"registered worker {name}")
PY

# 3) generate the first-launch script (--session-id creates the session) and a
#    relaunch script (--resume keeps context) the watchdog uses to revive it.
#    Task 170: the relaunch template lives in scratch_gen_relaunch.sh (single
#    source of truth — regen loops reuse it for existing workers); the launch
#    script below carries the same F5 instrumentation (strace arm + forensic
#    death bundle on signal-shaped exits).
RELAUNCH="scratch_worker_${NAME}_relaunch.sh"
cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
cd /home/steven/Projects/time-series-omp
source ~/anaconda3/etc/profile.d/conda.sh
conda activate tsomp
# Task 223: auth switch (default = Claude Max subscription, not the API key).
source /home/steven/Projects/time-series-omp/scratch_claude_auth.sh
export PATH="\$HOME/.npm-global/bin:\$PATH"
echo "[$NAME] starting \$(date) session=$SID" > $LOG
# Task 170 F5(b): forensic bundle on signal-shaped claude exits.
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
# Task 170 F5(a): optional signal-only strace (WORKER_STRACE=1 or flag file).
STRACE_PREFIX=()
if { [ "\${WORKER_STRACE:-}" = "1" ] || [ -f "scratch_full_logs/inbox/strace_${NAME}" ]; } && command -v strace >/dev/null 2>&1; then
  STRACE_LOG="scratch_full_logs/strace_${NAME}_\$(date +%s).log"
  STRACE_PREFIX=(strace -f -tt -e trace=signal -o "\$STRACE_LOG")
  echo "[$NAME] F5 strace armed -> \$STRACE_LOG" >> $LOG
fi
# Task 176: prompt via STDIN, not argv — task-spec text in the claude argv let
# a worker's own \`pkill -f <pattern named in its spec>\` kill its own claude
# (the noaa_fullds Jul-14 self-kill class). Pipeline rc = claude's rc.
"\${STRACE_PREFIX[@]}" claude --session-id "$SID" -p \\
  --dangerously-skip-permissions --model $WORKER_MODEL --effort max --max-turns 800 --verbose < "$PROMPT" >> $LOG 2>&1
RC=\$?
echo "[$NAME] EXITED rc=\$RC \$(date)" >> $LOG
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
    --dangerously-skip-permissions --model $WORKER_MODEL --effort max --max-turns 800 --verbose >> $LOG 2>&1
  RC=\$?
  echo "[$NAME] SENTINEL-TRAP EXITED rc=\$RC \$(date)" >> $LOG
  death_bundle "\$RC"
done
EOF
# Task 170: the relaunch script comes from the canonical template (F1
# origin-aware ack + F5 instrumentation + INBOX_DRYRUN dry-exec). One
# implementation for new AND regenerated workers. Task 216 F2: pass the model
# through so launch and relaunch agree.
bash scratch_gen_relaunch.sh "$NAME" "$SID" "$REQUESTER" "$WORKER_MODEL"

# 4) register the job with the watchdog so a killed worker gets revived
python3 - "$NAME" "$SID" "$LOG" "$RELAUNCH" "$REQUESTER" <<'PY'
import json, sys
from pathlib import Path
name, sid, logf, relaunch, requester = sys.argv[1:6]
p = Path("scratch_full_logs/watchdog_jobs.json")
try:
    jobs = json.loads(p.read_text())
except Exception:
    jobs = []
jobs = [j for j in jobs if j.get("name") != name]   # de-dup
jobs.append({"name": name, "session": sid, "log": logf, "relaunch": relaunch,
             "done_sentinel": f"scratch_full_logs/worker_{name}.done",
             "requester": requester, "state": "running",
             "reset_epoch": None, "relaunched": 0})
p.write_text(json.dumps(jobs, indent=2))
print(f"watchdog now tracking {name}")
PY

rm -f "scratch_full_logs/worker_${NAME}.done"      # clear any stale completion flag
tmux new-session -d -s "$NAME" "bash $LAUNCH"
echo "spawned worker '$NAME' (session $SID) in tmux; log=$LOG requester=$REQUESTER (watchdog-tracked)"
