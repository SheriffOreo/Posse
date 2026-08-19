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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # the infra/ dir = code + state root
cd "$HERE"
# Task 216 F2 / Task 376: the deputy's model is resolved by PRIORITY:
#   (i)  an explicit WORKER_MODEL env  (the inbox handler exports an email
#        'model:' tag as WORKER_MODEL, so a tag also lands here);
#   (ii) the PRECINCT's default model from the directory (paper=fable,
#        omp/query/eval/infra=opus — set via scratch_records.py directory register);
#   (iii) opus.
# Leave it empty here and resolve below, AFTER the precinct is known.
#   WORKER_MODEL=fable bash scratch_spawn_worker.sh <name> <spec> <requester>
WORKER_MODEL="${WORKER_MODEL:-}"
NAME="${1:?worker name}"; TASKFILE="${2:?task spec file}"; REQUESTER="${3:?requester email}"
KEYWORDS="${4:-$NAME}"; DRY=""
[ "${4:-}" = "--dry" ] && { KEYWORDS="$NAME"; DRY=1; }
[ "${5:-}" = "--dry" ] && DRY=1
NAME="$(echo "$NAME" | tr 'A-Z ' 'a-z_' | tr -cd 'a-z0-9_-')"
[ -f "$TASKFILE" ] || { echo "ERR: taskfile not found: $TASKFILE" >&2; exit 1; }

# Case 399: DEPUTY-NAME COLLISION GUARD. spawn_worker only ever creates a GENUINELY
# NEW deputy (delegate / web-case / sub-deputy); a finished deputy is relaunched via
# `claude --resume <session>`, never here. So if $NAME already names a REGISTERED
# worker OR a LIVE tmux session, spawning would (a) clobber that worker's registry
# entry — silently hijacking replies meant for it — and (b) fail `tmux new-session`
# (duplicate session), corrupting routing. Instead of clobbering, auto-dedupe to the
# first free `<name>-2`, `-3`, … The final name is echoed and used everywhere below.
# Escape hatch: TSOMP_NAME_GUARD=0 restores the old clobber-if-exists behavior.
_name_taken() {   # 0/true if $1 is a registered worker OR a live tmux session
  local n="$1"
  python3 - "$n" <<'PY' 2>/dev/null && return 0
import json, sys
n = sys.argv[1]
try:
    w = json.load(open("scratch_agents_registry.json")).get("workers", {})
except Exception:
    w = {}
sys.exit(0 if n in w else 1)
PY
  tmux has-session -t "=$n" 2>/dev/null && return 0
  return 1
}
if [ "${TSOMP_NAME_GUARD:-1}" != "0" ]; then
  ORIG_NAME="$NAME"; _sfx=2
  while _name_taken "$NAME"; do
    if [ "$_sfx" -gt 99 ]; then
      echo "ERR: could not find a free deputy name near '$ORIG_NAME' (tried -2..-99)" >&2; exit 1
    fi
    NAME="${ORIG_NAME}-${_sfx}"; _sfx=$((_sfx+1))
  done
  [ "$NAME" != "$ORIG_NAME" ] && \
    echo "[spawn_worker] name '$ORIG_NAME' already in use (registry/tmux) -> using '$NAME' to avoid a clash" >&2
fi

SID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
PROMPT="scratch_full_logs/worker_${NAME}_prompt.md"
LAUNCH="scratch_worker_${NAME}_launch.sh"
LOG="scratch_full_logs/worker_${NAME}.log"

# Task 372 (Sheriff & Deputies): the PRECINCT (department) this deputy works in.
# Resolution order: WORKER_PRECINCT env -> a 'precinct:' line in the task spec ->
# 'infra'. The deputy carries this precinct's ledger and, on completion, files two
# records for it (case log + ledger) before touching its done-sentinel.
WORKER_PRECINCT="${WORKER_PRECINCT:-}"
if [ -z "$WORKER_PRECINCT" ]; then
  WORKER_PRECINCT="$(sed -n 's/^[[:space:]]*precinct[[:space:]]*[:=][[:space:]]*//Ip' "$TASKFILE" 2>/dev/null | head -1 | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_.-')"
fi
[ -z "$WORKER_PRECINCT" ] && WORKER_PRECINCT="infra"
# The CASE NUMBER for this case (from a task_<uid>.md spec name; else the worker name).
# Task 384: accept digit-prefixed ALPHANUMERIC ids so SUB-DEPUTY subtask numbers like
# 200a / 200b (a subtask of case 200) work, not just plain integers. The '_'-delimited
# suffix files (task_<n>_advice.md) still resolve to <n> (the class stops at '_').
TASK_UID="$(basename "$TASKFILE" | sed -n 's/^task_\([0-9][0-9A-Za-z]*\).*/\1/p')"
[ -z "$TASK_UID" ] && TASK_UID="$NAME"
CASE_REL="scratch_full_logs/records/${WORKER_PRECINCT}/cases/task_${TASK_UID}.md"

# Task 376: resolve the deputy MODEL now that the precinct is known — explicit
# WORKER_MODEL wins, else the precinct's directory default, else opus. Validated
# against the allowed set so a bad email 'model:' tag can never poison the launch.
if [ -z "$WORKER_MODEL" ]; then
  WORKER_MODEL="$(python3 scratch_records.py directory model --dept "$WORKER_PRECINCT" 2>/dev/null | tr -d '[:space:]')"
fi
case "$WORKER_MODEL" in fable|opus|sonnet|haiku) ;; *) WORKER_MODEL="opus" ;; esac

# Task 377 #1: stamp a `deputy: <name>` line at the TOP of the case file (the task
# spec) — like the parent_task:/precinct: lines — so the case file self-documents
# who worked it. Idempotent (skips if a deputy line already exists) + atomic
# (temp+replace) + additive (never rewrites existing content). Done here, in the
# spawn, because only the spawn knows the real deputy name (the handler's $AGENT is
# the lesser triage lineage). The symlink into the precinct's cases/ resolves to
# this same inode, so the case file shows the line too. Skipped under --dry so a
# dry run stays side-effect-free.
[ -z "$DRY" ] && python3 - "$TASKFILE" "$NAME" <<'PY' || true
import os, re, sys
spec, name = sys.argv[1], sys.argv[2]
try:
    text = open(spec, encoding="utf-8", errors="replace").read()
except Exception:
    sys.exit(0)
if re.search(r"(?im)^[ \t>]*deputy[ \t]*[:=]", text):
    sys.exit(0)  # already stamped
tmp = spec + ".deptmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(f"deputy: {name}\n" + text)
os.replace(tmp, spec)
PY

# 1) build the worker prompt = synchronous-discipline preamble + the task spec
{
  cat <<EOF
You are a PERSISTENT deputy (worker agent) running headless in tmux for this Posse.
Your working directory is $HERE (the infra/ dir; also where runtime state lives).
Runtime env is already set up by _daemon_env.sh (cd + optional conda + Claude auth).

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
The mailer auto-addresses the operator by name and signs your agent name at the end —
write just the message body, no greeting or signature of your own.
Send: (a) a short START/plan email now, (b) milestone update(s), (c) a FINAL email
with results + any key figure attached. Keep emails terse with concrete numbers.

MODEL: you are running on model **$WORKER_MODEL** (the precinct's default unless the
request overrode it). STATE which model you are running on in your START email. Every
email you send is also auto-stamped, just under the greeting, with a context header —
[precinct: $WORKER_PRECINCT | case: $TASK_UID | deputy: $NAME | model: $WORKER_MODEL] —
so the operator always sees which precinct/case/deputy (and model) it came from.

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

FOLLOW-UP EMAIL PROTOCOL (Task 377 / Feng uid=378) — when a resumed email is a NEW request:
You were FULLY relaunched as a persistent, watchdog-tracked worker (NOT the old one-shot
handler that exited after replying), so you CAN take on new multi-step work and it will
survive to completion. When you resume on an injected email, first classify it:
  * CLARIFICATION / continuation of THIS case ($TASK_UID) — handle it inline; no new case number.
  * A genuinely NEW request/task — it is a NEW CASE. ALLOCATE its case number from the
    single continuous sequence (Case 391a): C=\$(python scratch_case_seq.py allocate). Do
    NOT reuse the Gmail uid as the case number — the uid ('uid=N' in the injected
    '=== mail uid=N from ... ===' header) only routed/acked this email; email AND web cases
    now share ONE monotonic sequence. DECIDE, and STATE the decision + the reason + the new
    case number \$C EXPLICITLY in your reply email:
      (A) TAKE IT yourself — you are persistent, so the work survives. Do:
            export TSOMP_CASE=\$C             # so your emails stamp the new case
            write scratch_full_logs/inbox/task_\$C.md, first lines:
              parent_task: $TASK_UID / precinct: $WORKER_PRECINCT / deputy: $NAME
            python scratch_precinct.py stamp --uid \$C --precinct $WORKER_PRECINCT --deputy $NAME --session $SID --basis take
            python scratch_deputy_state.py set --deputy $NAME --case \$C --precinct $WORKER_PRECINCT --description "<short new-case description>"
            ln -s "\$(readlink -f scratch_full_logs/inbox/task_\$C.md)" scratch_full_logs/records/$WORKER_PRECINCT/cases/task_\$C.md 2>/dev/null || true
          The scratch_deputy_state.py line flips the Status board onto your NEW case (Task 382 #1).
          From now on TITLE your emails "Case \$C: <short description>" — a FRESH subject; do NOT
          --thread the old case's subject (Task 382 #3). Then do the work to completion (plan +
          milestone + FINAL emails) and CLOSE the new case exactly like this one: log append --dept
          $WORKER_PRECINCT --deputy $NAME --task \$C ..., ledger append, then touch
          scratch_full_logs/worker_$NAME.done for that case.
      (B) DELEGATE to a NEW deputy — FIRST write an insight/guidance/advice file at
          scratch_full_logs/inbox/task_\${C}_advice.md (what you already know, gotchas, exact
          file paths, the recommended approach, what NOT to do — everything that makes the new
          deputy's job easier). Then write scratch_full_logs/inbox/task_\$C.md whose FIRST line
          is 'Read scratch_full_logs/inbox/task_\${C}_advice.md FIRST — guidance from $NAME', and:
            bash scratch_spawn_worker.sh <new_name> scratch_full_logs/inbox/task_\$C.md $REQUESTER "<keywords>"
          Email the ACK stating you DELEGATED, WHY, the new case number \$C, and the new deputy's name.
  (One continuous sequence, Case 391a: allocate EVERY new case number with
  python scratch_case_seq.py allocate — the Gmail uid is NEVER the case number, only a routing
  key. A WEB-FORM case likewise has no uid; the dashboard already allocated its number for you.)

JTF MUST-TAKE PROTOCOL (Joint Task Force assignments) — Case 384e:
A JTF (Joint Task Force) is a group of agents — one LEAD + collaborators (+ an optional
critic) — working ONE task. If you resume and your mailbox holds a message headed
"JTF ASSIGNMENT (must-take)", you have been assigned a JTF role and you MUST take it: you
may NOT delegate or decline it (this OVERRIDES the TAKE-vs-DELEGATE choice above). The
assignment names the JTF id, your ROLE, the case number <c> to use, the task, and your
fellow members. Act in this order:
  (a) IDLE / finished (no case in flight): just TAKE the JTF as case <c> — export
      TSOMP_CASE=<c>; write scratch_full_logs/inbox/task_<c>.md (first lines
      "parent_task: <JTF id> / precinct: $WORKER_PRECINCT / deputy: $NAME"); stamp it
      (python scratch_precinct.py stamp --uid <c> --precinct $WORKER_PRECINCT --deputy $NAME --session $SID --basis take);
      python scratch_deputy_state.py set --deputy $NAME --case <c> --precinct $WORKER_PRECINCT --description "JTF <id> <role>";
      symlink it into scratch_full_logs/records/$WORKER_PRECINCT/cases/; TITLE emails
      "Case <c>: JTF <id> — <role>"; work it to completion and CLOSE it (log/ledger/sentinel).
  (b) MID-TASK on another case: you must STOP and HAND OVER the current case FIRST, then
      take the JTF (never run two live cases in one context):
        1. Write scratch_full_logs/inbox/task_<curcase>_advice.md — a full handover:
           what you've done, what's left, gotchas, exact paths, next steps.
        2. Spawn a NEW deputy to CONTINUE the current case (reuse the DELEGATE path):
           write scratch_full_logs/inbox/task_<curcase>.md (first line 'Read
           scratch_full_logs/inbox/task_<curcase>_advice.md FIRST — handover from $NAME')
           then: bash scratch_spawn_worker.sh <newdep> scratch_full_logs/inbox/task_<curcase>.md $REQUESTER "<keywords>"
        3. NOTIFY THE SHERIFF of the swap so the board reflects it: point your
           active-deputies entry at the JTF case (python scratch_deputy_state.py set
           --deputy $NAME --case <c> --precinct $WORKER_PRECINCT --description "JTF <id> <role>"),
           and email $REQUESTER an ACK: you handed case <curcase> to <newdep> and are taking JTF <id>.
        4. TAKE the JTF as case <c> (as in (a)) and work it to completion.
  YOUR JTF ROLE: LEAD = own the deliverables; plan, delegate to + coordinate the
  collaborators (email $REQUESTER + RELAY to them via scratch_mailbox_append.sh /
  scratch_interrupt_worker.sh --relay-from $NAME), review + iterate; and IF a critic was
  requested, spawn an ANONYMOUS critic (bash scratch_spawn_anon.sh <name> <prompt_file>)
  to independently audit the deliverables before your FINAL (you OWN it — monitor +
  relaunch). COLLABORATOR = carry your precinct's knowledge, do your part, coordinate with
  the lead. A JTF role is a normal case: plan + milestone + FINAL emails, then close it.

Even without an interrupt, continue to check the mailbox between every major step
(before and after each long operation) using: bash scratch_read_mailbox.sh $NAME

DEPARTMENT (precinct): you are the deputy on this case in the "$WORKER_PRECINCT" precinct.
Its big-picture LEDGER, the GLOBAL PRECINCT DIRECTORY, and your precinct's recent CASE
LOG are appended at the very end of this prompt — skim them for prior context before you
start. All records are reached ONLY through the records-manager program
(python scratch_records.py ...), never the raw files.

When you are FULLY finished (FINAL email sent, all deliverables exist), CLOSE THE CASE:
file TWO records for your precinct, THEN touch the done-sentinel — in this exact order:
  1. python scratch_records.py log append --dept $WORKER_PRECINCT --role deputy --task $TASK_UID \\
       --deputy $NAME --summary "<ONE sentence: what this case delivered>" --case-file $CASE_REL
  2. python scratch_records.py ledger append --dept $WORKER_PRECINCT --role deputy \\
       --text "<ONE paragraph case report: what was asked, what you did, key numbers, deliverable paths>"
  3. touch scratch_full_logs/worker_${NAME}.done
The case log is an append-only INDEX (one line per closed case); the ledger is the
precinct's big-picture digest (the sheriff compacts it later — you only ever APPEND to
it). NEVER rewrite the ledger: if you find an existing ledger entry now wrong or
obsolete, email the operator to route a change request to the sheriff (the ledger's sole
editor). If a records command errors (e.g. a fixed ledger), skip that step and still
touch the sentinel — never let bookkeeping block completion.

Touching the done-sentinel tells the watchdog you completed so it will not relaunch you.

COLLABORATION — spawning helpers (Task 384). You have TWO ways to get help; never a
raw \`claude\`/setsid/background process:

  (1) ANONYMOUS WORKER — a helper YOU own and manage yourself. Use it for a
      throwaway helper whose result only you consume — e.g. a CRITIC that reviews
      your draft. It has NO case number, is NOT registered with the sheriff/watchdog
      (the sheriff does NOT monitor it, it does NOT show on the board), files NO
      paperwork, and reports ONLY to you. You write its prompt and you monitor it
      (poll its log / tmux, or submit it to the job manager) and relaunch it if it
      dies:
        bash scratch_spawn_anon.sh <name> <prompt_file> [--model M] [--max-turns N]
      -> tmux 'anon_<name>', stdout at scratch_full_logs/anon_<name>.log; relaunch
      with scratch_anon_<name>_launch.sh. It survives your own interrupts (separate
      tmux) but nothing else babysits it — that's your job.

  (2) SUB-DEPUTY — a NEW registered case with its own deputy. Use it for real work
      that needs tracking. It shows on the board, the sheriff monitors it, and it
      files paperwork (case file + case log + ledger) exactly like you. At launch it
      reads the precinct ledger + the standard preamble like any deputy; you pass
      ADDITIONAL input by writing its spec (scratch_full_logs/inbox/task_<id>.md).
        bash scratch_spawn_worker.sh <sub_name> scratch_full_logs/inbox/task_<id>.md $REQUESTER "<keywords>"
      CASE NUMBER by nature of the work: a genuine FOLLOW-UP gets a fresh number; a
      SUBTASK of YOUR current case ($TASK_UID) should be named ${TASK_UID}a, ${TASK_UID}b, …
      (e.g. task_${TASK_UID}a.md) so the logical parent is obvious. (The emailed
      reply that relaunched you is itself a special case of a sub-deputy launch.)

  WAITING ON SUB-DEPUTIES (sheriff wake). If you must wait for subtasks before you
  can continue, don't busy-wait — register a wake and park; the sheriff wakes you
  when they finish (their done-sentinels appear), injecting a 'SUBTASKS DONE' message:
        python scratch_subtask_wake.py register --parent $NAME --wait <sub1,sub2> [--mode all|any]
        python scratch_subtask_wake.py park --parent $NAME      # then END YOUR TURN
  (Or, for a short wait, just POLL their scratch_full_logs/worker_<sub>.done sentinels
  in a foreground loop with sleep chunks <=240 s, per THE WAIT RULES.)

Guardrails: no git commit/push; do not delete data/outputs; do not kill running
jobs; stay within the lane the task specifies.

================================ TASK ================================
EOF
  cat "$TASKFILE"
  # Task 372: seed the deputy with its precinct's ledger, the global directory,
  # and its precinct's recent case log, so it "carries the ledger" on launch.
  echo ""
  echo "===================== PRECINCT LEDGER ($WORKER_PRECINCT) ====================="
  python scratch_records.py ledger read --dept "$WORKER_PRECINCT" 2>/dev/null || true
  echo "===================== GLOBAL PRECINCT DIRECTORY ====================="
  python scratch_records.py directory read --md 2>/dev/null || true
  echo "================ CASE LOG ($WORKER_PRECINCT) — recent (tail) ================"
  tail -n 25 "scratch_full_logs/records/${WORKER_PRECINCT}/log.tsv" 2>/dev/null || true
} > "${PROMPT}.tmp"

if [ -n "$DRY" ]; then
  echo "[dry] would register worker '$NAME' session=$SID keywords='$KEYWORDS' precinct=$WORKER_PRECINCT task_uid=$TASK_UID"
  echo "[dry] prompt -> ${PROMPT} ($(wc -l < "${PROMPT}.tmp") lines); tmux session '$NAME'"
  echo "[dry] ----- generated worker prompt -----"
  cat "${PROMPT}.tmp"
  rm -f "${PROMPT}.tmp"; exit 0
fi
mv "${PROMPT}.tmp" "$PROMPT"

# 2) register so the user's replies to this worker's emails route back to it.
#    A fresh install has no registry yet (GitHub issue #1: a precinct-tagged email
#    got a "deputy spawned" ack but no deputy ran, because this step crashed on the
#    missing file under `set -euo pipefail`, before the tmux launch below). Tolerate
#    a missing / empty / corrupt file and start from {"workers": {}} so registration
#    can never abort the spawn. Mirrors the guard the collision-check above and
#    scratch_inbox.py already use.
python3 - "$NAME" "$SID" "$KEYWORDS" <<'PY'
import json, sys
name, sid, kw = sys.argv[1], sys.argv[2], sys.argv[3]
p = "scratch_agents_registry.json"
try:
    r = json.load(open(p))
    if not isinstance(r, dict):
        raise ValueError("registry is not a JSON object")
except (OSError, ValueError):
    r = {}
r.setdefault("workers", {})
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
# Portable runtime env: cd into the infra/ dir (code + state root), optionally
# activate a conda env (INFRA_CONDA_ENV), load Claude auth, and put claude on PATH.
source "$HERE/_daemon_env.sh"
# Task 325: auto-attribute any GPU job this worker enqueues (submit_gpu.py reads
# these) so the dashboard shows the owner instead of "?".
export GPU_JOB_OWNER="$NAME"
export TSOMP_AGENT="$NAME"
# Task 372/376: the precinct, case number, and resolved model — so notify_email
# stamps the context header ([precinct | case | deputy | model]) on every email
# and the deputy runs on the resolved model. Baked in (not just inherited) so a
# watchdog relaunch keeps them too (mirrors GPU_JOB_OWNER).
export WORKER_PRECINCT="$WORKER_PRECINCT"
export TSOMP_CASE="$TASK_UID"
export TSOMP_MODEL="$WORKER_MODEL"
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
# through so launch and relaunch agree. Task 372/376: pass the precinct + case
# number so the relaunch script re-exports WORKER_PRECINCT/TSOMP_CASE/TSOMP_MODEL
# (the context-header env) across a watchdog revival.
bash scratch_gen_relaunch.sh "$NAME" "$SID" "$REQUESTER" "$WORKER_MODEL" max "$WORKER_PRECINCT" "$TASK_UID"

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

# Task 372: file this case into its precinct — symlink the case file into the
# precinct's cases/ folder (non-destructive) and stamp the task->precinct->session
# map so the dashboard + reply-routing know this deputy's precinct. Idempotent.
mkdir -p "scratch_full_logs/records/${WORKER_PRECINCT}/cases" 2>/dev/null || true
[ -e "$CASE_REL" ] || ln -s "$(readlink -f "$TASKFILE")" "$CASE_REL" 2>/dev/null || true
python3 scratch_precinct.py stamp --uid "$TASK_UID" --precinct "$WORKER_PRECINCT" \
  --agent "$NAME" --session "$SID" --deputy "$NAME" --basis spawn >/dev/null 2>&1 || true

# Task 382 #1: seed this deputy's ACTIVE-DEPUTIES entry (its CURRENT case +
# description + precinct) so the Status board shows it correctly from the start.
# When the deputy later TAKES a new case it updates this entry itself (see the
# FOLLOW-UP EMAIL PROTOCOL in the preamble); the sheriff owns the writes in the
# approved redesign.
SPEC_TITLE="$(grep -m1 '^#' "$TASKFILE" 2>/dev/null | sed 's/^#\+[[:space:]]*//' | cut -c1-120)"
python3 scratch_deputy_state.py set --deputy "$NAME" --case "$TASK_UID" \
  --precinct "$WORKER_PRECINCT" ${SPEC_TITLE:+--description "$SPEC_TITLE"} >/dev/null 2>&1 || true

rm -f "scratch_full_logs/worker_${NAME}.done"      # clear any stale completion flag
tmux new-session -d -s "$NAME" "bash $LAUNCH"
echo "spawned worker '$NAME' (session $SID, precinct $WORKER_PRECINCT) in tmux; log=$LOG requester=$REQUESTER (watchdog-tracked)"
