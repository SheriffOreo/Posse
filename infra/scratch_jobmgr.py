#!/usr/bin/env python3
"""jobmgr — always-on job-management daemon for ALL CPU and GPU tasks (Task 216 F1a).

Why this exists (Task 212 token incident): worker agents babysat long GPU/CPU
jobs by sleeping in ~10-min chunks inside giant (0.5–1M token) claude contexts.
The prompt cache TTL is 5 min, so nearly every poll re-wrote the full context
cold: 501.3M tokens = $6,208 = 63.5% of July. The fix is event-driven waiting:
an agent SUBMITS its long job here, parks itself (ends its turn), and jobmgr
WAKES it when the job finishes — via the EXISTING interrupt/relaunch path — so
a multi-hour wait costs ONE context rewrite instead of dozens.

Doctrine (Task 216 F1c): submit-and-sleep if the job's estimated runtime is
> 50 min; foreground-poll with sleep chunks <= 4 min (240 s) if <= 50 min.
50 min is the exact break-even: warm polling costs 15 reads/hr x $1/MTok = $15
per Mtok-of-context per hour, one wake rewrite costs $12.5 per MTok-of-context,
and 12.5/15 h = 50 min — independent of context size.

PURE PYTHON — zero Claude API calls, ever. It only stats files, moves json
records, sends SMTP email (scratch_notify_email), and shells out to the
existing wake helpers (scratch_interrupt_worker.sh / scratch_mailbox_append.sh).
The claude relaunch inside a wake belongs to the OWNING agent, not to jobmgr.

Disk state (all under scratch_full_logs/jobs/):
    pending/<id>.json    written by submit before dispatch (transient)
    running/<id>.json    dispatched; being monitored
    done/<id>.json       finished (status done|died), enriched with rc etc.
    wakes/<id>.json      undelivered wake tickets (hot set — the tick loop
                         scans THIS, never the ever-growing done/)
    rc/<id>.rc           exit code dropped by the CPU shim (scratch_jobmgr_run.sh)
    msg/<id>.txt         wake message bodies (what gets injected/appended)
    sleeping/<owner>.json  sleep markers written by `--sleep` (scratch_job_sleep.sh)
    jobmgr.log           daemon log

Job record fields (Task 216 spec): job_id, owner_agent, type (cpu|gpu),
command, submit_ts, est_seconds, status (pending|running|done|died),
wake_on_done, output_path, result_path; plus pid_file/gpu_job_id, start_ts,
finish_ts, rc, timed_out, wake_pending, woken_ts, overdue_alerted.

How a job runs:
    CPU  dispatched via the existing scratch_detach.sh (own session, survives
         any claude exit) wrapped in scratch_jobmgr_run.sh, which execs the
         command and drops rc/<id>.rc when it finishes — an unambiguous
         completion event (a detached orphan's rc is unobservable otherwise).
         The <owner>_job_<id>.pid/.output files land where scratch_cpu_snapshot.sh
         already looks, so resumed owners see these jobs in their CPU snapshot.
    GPU  enqueued into the EXISTING gpu_queue/pending/ (same record shape as
         submit_gpu.py, but non-blocking); gpu_manager runs it untouched and
         jobmgr watches gpu_queue/done/<gid>.json.

How a wake fires (reusing Task 170/185 machinery — never hand-rolled):
    owner sleeping (marker present) or its claude dead
        -> bash scratch_interrupt_worker.sh <owner> --relay-from jobmgr
           --message-file msg/<id>.txt      (inject -> guard-stamp -> surgical
           kill -> relaunch -> verify; relay origin => no mechanical user-ack)
    owner live (non-zombie claude in its own tmux pane tree)
        -> bash scratch_mailbox_append.sh <owner> "relay from agent jobmgr" msg
           (the worker reads it at its next mailbox checkpoint; if it dies
           first, the watchdog relaunch boot-drains the mailbox — covered)

How an agent SLEEPS without being relaunch-looped (`--sleep`, via
scratch_job_sleep.sh): touch scratch_full_logs/worker_<owner>.done (satisfies
the wrapper's sentinel trap: no rc=0 nudges) + set state="waiting_jobs" in
watchdog_jobs.json (Task 216 uid=217: a first-class watchdog state — the
watchdog leaves it alone like done/failed but rosters it honestly as
"waiting_jobs (parked; jobmgr wakes it on job completion)") + write
sleeping/<owner>.json. The existing scratch_interrupt_worker.sh wake already
reverses ALL of it (rm sentinel, state=running, last_relaunch_ts stamped).
`--sleep` refuses to park an owner with no wake_on_done job in flight
(nothing would ever wake it).

Safety nets:
    - overdue: running job past max(2x est, est+3600) -> ONE email alert
      (never kills anything; gpu_manager's own timeout still applies to GPU).
    - orphaned pending: record still in pending/ >120 s (submit CLI died
      mid-dispatch) -> marked died + wake ticket, so the owner hears about it.
    - stale sleep marker: owner parked but nothing in flight to wake it
      (>10 min) -> ONE email alert with un-park instructions (no auto-unpark).
    - wake retries: failed deliveries retried with backoff; ONE alert email
      after 3 straight failures; at-least-once semantics across daemon
      restarts (tickets persist in wakes/).
    - single instance: flock on jobs/jobmgr.lock; a second daemon exits.

Start (documented in CLAUDE.md §3; survives disconnects like inbox/watchdog):
    bash scratch_jobmgr_start.sh          # idempotent; tmux session 'jobmgr'

CLI (agents use the thin wrappers):
    bash scratch_submit_job.sh <owner> <est_seconds> [--gpu] [--no-wake]
                               [--timeout S] -- <command...>
    bash scratch_job_sleep.sh <owner>
    python3 scratch_jobmgr.py --status [owner]
    python3 scratch_jobmgr.py --once          # one monitoring tick (testing)
"""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# TSOMP_JOBS_DIR / TSOMP_WATCHDOG_JOBS: test-only path overrides (Task 274) so the
# limit tests run against an isolated state dir; unset in production (defaults below).
JOBS = Path(os.environ.get("TSOMP_JOBS_DIR") or (ROOT / "scratch_full_logs" / "jobs"))
PENDING, RUNNING, DONE = JOBS / "pending", JOBS / "running", JOBS / "done"
WAKES, RCDIR, MSG, SLEEPING = JOBS / "wakes", JOBS / "rc", JOBS / "msg", JOBS / "sleeping"
ADOPTED = JOBS / "adopted"           # Task 274 G3: idempotency markers for adopted jobs
LIMIT_STATE = JOBS / "limit_state.json"   # Task 274 G4: account-global limit marker
LOG = JOBS / "jobmgr.log"
LOCK = JOBS / "jobmgr.lock"
GPUQ = ROOT / "gpu_queue"
WATCHDOG_JOBS = Path(os.environ.get("TSOMP_WATCHDOG_JOBS")
                     or (ROOT / "scratch_full_logs" / "watchdog_jobs.json"))
# Wake-helper commands (test seam, Task 274): overridable so the limit tests point
# the relaunch/append at an argv-recording stub instead of the real machinery — which
# would otherwise mutate the LIVE watchdog_jobs.json. Unset in production (defaults).
INTERRUPT_CMD = os.environ.get("TSOMP_INTERRUPT_CMD") or str(ROOT / "scratch_interrupt_worker.sh")
MAILBOX_APPEND_CMD = os.environ.get("TSOMP_MAILBOX_APPEND_CMD") or str(ROOT / "scratch_mailbox_append.sh")

POLL = 10                    # daemon tick (file stats only — cheap)
PENDING_ORPHAN_SEC = 120     # pending record older than this = submit CLI died
GONE_GRACE_SEC = 30          # cpu pid gone this long with no rc file = died
GPU_VANISH_GRACE_SEC = 120   # gpu job in no gpu_queue dir this long = cancelled
WAKE_RETRY_BACKOFF = [30, 60, 120, 300, 600]   # then every 600 s
WAKE_ALERT_AFTER = 3         # failed deliveries before the ONE alert email
STALE_SLEEP_SEC = 600        # parked with nothing in flight this long = alert
HEARTBEAT_SEC = 1800         # proof-of-life log line
SUBMIT_SLEEP_MIN_EST = 50 * 60   # doctrine threshold (Task 216 F1c): >50 min
                                 # => submit+sleep; <=50 min => poll <=4 min


def log(msg):
    line = f"[jobmgr] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def email(subject, body):
    """SMTP only (like the watchdog) — never a model call."""
    try:
        sys.path.insert(0, str(ROOT))
        import scratch_notify_email as notify
        notify.send(subject, body, agent="jobmgr")
    except Exception as e:
        log(f"email failed: {e}")


def setup():
    for d in (PENDING, RUNNING, DONE, WAKES, RCDIR, MSG, SLEEPING, ADOPTED):
        d.mkdir(parents=True, exist_ok=True)


def limit_active(now):
    """Task 274 G4: (True, state) if the account-global limit marker says a limit
    is in force right now, else (False, state_or_None). 'In force' = the marker
    exists, is active, and its reset_epoch has NOT passed — jobmgr treats a passed
    reset as over even before the watchdog deletes the file, so held handovers fire
    promptly on jobmgr's fast (10 s) tick. The marker is written/cleared by the
    watchdog (scratch_watchdog.py write_limit_state/clear_limit_state)."""
    st = read_json(LIMIT_STATE)
    if isinstance(st, dict) and st.get("active") and now < (st.get("reset_epoch") or 0):
        return True, st
    return False, (st if isinstance(st, dict) else None)


def _starttime_of(pid):
    """Field 22 of /proc/<pid>/stat — (pid, starttime) is a pid-reuse-safe identity
    for an adopted self-monitored job (its argv does NOT carry our job_id, so the
    submitted-job cmdline guard can't be used)."""
    try:
        stat = open(f"/proc/{pid}/stat").read()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except Exception:
        return None


def write_json(path: Path, obj):
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.rename(path)                        # atomic publish


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def records(d: Path):
    out = []
    for p in sorted(d.glob("*.json")):
        r = read_json(p)
        if r is None:
            log(f"WARN unreadable record skipped: {p}")
            continue
        out.append((p, r))
    return out


# ---------------------------------------------------------------- liveness
# Copied from scratch_watchdog.py (Task 176) — keep in sync. Deliberately a
# copy, not an import: jobmgr must not fail to boot because another infra
# module got a syntax error.

def tmux_has(name):
    return subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def claude_alive_nonzombie(name):
    """True iff a LIVE (non-zombie) claude process runs inside the worker's
    OWN tmux pane tree. Any error -> False (no wake-skipping without positive
    evidence of liveness)."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-s", "-t", f"={name}",
                            "-F", "#{pane_pid}"], capture_output=True, text=True)
        roots = [int(p) for p in r.stdout.split()] if r.returncode == 0 else []
        if not roots:
            return False
        kids, zstate = {}, {}
        for line in subprocess.run(["ps", "-eo", "pid=,ppid=,stat="],
                                   capture_output=True, text=True).stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                p, pp = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            kids.setdefault(pp, []).append(p)
            zstate[p] = parts[2]
        stack, seen = list(roots), set()
        while stack:
            for k in kids.get(stack.pop(), []):
                if k in seen:
                    continue
                seen.add(k)
                stack.append(k)
                if zstate.get(k, "Z").startswith("Z"):
                    continue
                try:
                    argv0 = open(f"/proc/{k}/cmdline", "rb").read().split(b"\0")[0]
                except OSError:
                    continue
                if os.path.basename(argv0.decode(errors="replace")) == "claude":
                    return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------- submit

def new_id():
    import secrets
    return f"j{int(time.time() * 1000)}_{secrets.token_hex(3)}"


def cmd_submit(argv):
    """--submit <owner> <est_seconds> [--gpu] [--no-wake] [--timeout S] -- cmd...

    Writes the pending record, dispatches (scratch_detach.sh for CPU,
    gpu_queue enqueue for GPU), then promotes it to running/. The command is
    an argv vector (exec'd verbatim for CPU; shlex-joined into the shell
    string gpu_manager expects for GPU) — wrap shell lines as: -- bash -c '…'.
    """
    if len(argv) < 2:
        print(__doc__.split("CLI (agents use the thin wrappers):")[1])
        return 2
    owner, est = argv[0], argv[1]
    if not owner or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in owner):
        print(f"ERR: bad owner name '{owner}' (lowercase [a-z0-9_-])", file=sys.stderr)
        return 2
    try:
        est = int(float(est))
        assert est > 0
    except Exception:
        print(f"ERR: est_seconds must be a positive number, got '{argv[1]}'", file=sys.stderr)
        return 2
    argv = argv[2:]
    gpu, wake, timeout = False, True, None
    while argv and argv[0] != "--":
        if argv[0] == "--gpu":
            gpu = True
        elif argv[0] == "--no-wake":
            wake = False
        elif argv[0] == "--timeout" and len(argv) > 1:
            timeout = int(float(argv[1])); argv = argv[1:]
        else:
            print(f"ERR: unknown option '{argv[0]}'", file=sys.stderr)
            return 2
        argv = argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("ERR: no command given (usage: ... -- <command...>)", file=sys.stderr)
        return 2

    setup()
    jid = new_id()
    jtype = "gpu" if gpu else "cpu"
    cmd_str = shlex.join(argv)
    rec = {"job_id": jid, "owner_agent": owner, "type": jtype, "command": cmd_str,
           "submit_ts": time.time(), "est_seconds": est, "status": "pending",
           "wake_on_done": wake, "output_path": None,
           "result_path": str((DONE / f"{jid}.json").relative_to(ROOT))}
    write_json(PENDING / f"{jid}.json", rec)

    try:
        if gpu:
            gid = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            gtimeout = timeout if timeout else max(2 * est, est + 600)
            (GPUQ / "pending").mkdir(parents=True, exist_ok=True)
            (GPUQ / "done").mkdir(parents=True, exist_ok=True)
            # Task 325: attribute the GPU job to its jobmgr owner so gpu_manager
            # carries owner_agent through to gpu_queue/done and the dashboard
            # shows the owner instead of "?".
            gjob = {"id": gid, "cmd": cmd_str, "cwd": str(ROOT),
                    "timeout": gtimeout, "env": {}, "owner_agent": owner}
            gtmp = GPUQ / "pending" / f".{gid}.json.tmp"
            gtmp.write_text(json.dumps(gjob))
            gtmp.rename(GPUQ / "pending" / f"{gid}.json")   # atomic enqueue
            rec.update(gpu_job_id=gid, gpu_timeout=gtimeout,
                       output_path=str((GPUQ / "logs" / f"{gid}.out").relative_to(ROOT)))
        else:
            out = subprocess.run(
                ["bash", str(ROOT / "scratch_detach.sh"), owner, f"job_{jid}", "--",
                 "bash", str(ROOT / "scratch_jobmgr_run.sh"), jid, "--"] + argv,
                cwd=ROOT, capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise RuntimeError(f"scratch_detach.sh failed rc={out.returncode}: "
                                   f"{out.stderr.strip() or out.stdout.strip()}")
            rec.update(pid_file=f"scratch_full_logs/{owner}_job_{jid}.pid",
                       output_path=f"scratch_full_logs/{owner}_job_{jid}.output")
    except Exception as e:
        rec.update(status="died", error=f"dispatch failed: {e}", finish_ts=time.time())
        write_json(DONE / f"{jid}.json", rec)
        (PENDING / f"{jid}.json").unlink(missing_ok=True)
        print(f"ERR: dispatch failed for {jid}: {e}", file=sys.stderr)
        return 1

    rec.update(status="running", start_ts=time.time())
    write_json(RUNNING / f"{jid}.json", rec)
    (PENDING / f"{jid}.json").unlink(missing_ok=True)

    print(f"submitted {jid} ({jtype}, est {est}s, wake_on_done={wake}) owner={owner}")
    print(f"  record: scratch_full_logs/jobs/running/{jid}.json")
    print(f"  output: {rec['output_path']}")
    print(f"  result: {rec['result_path']} (appears on completion)")
    if wake:
        if est <= SUBMIT_SLEEP_MIN_EST:
            print(f"NOTE: est <= 50 min — doctrine says POLL this one (foreground, sleep "
                  f"chunks <= 240 s) instead of sleeping; submit+sleep is for > 50 min.")
        print(f"NEXT: finish any other pending work, then park yourself:")
        print(f"  bash scratch_job_sleep.sh {owner}")
        print(f"and END YOUR TURN. jobmgr will wake you (existing interrupt+relaunch "
              f"path, 'JOB {jid} DONE ...' injected) when it finishes.")
    else:
        print(f"NEXT: poll {rec['result_path']} in the FOREGROUND with sleep chunks "
              f"<= 240 s (4 min keeps the prompt cache warm; never exceed it).")
    return 0


# ---------------------------------------------------------------- sleep

def _watchdog_set_state(owner, state):
    """Set the owner's watchdog state (modify-only; never adds entries).
    Same read-modify-write the existing interrupt entrypoint uses."""
    jobs = read_json(WATCHDOG_JOBS)
    if not isinstance(jobs, list):
        return False
    hit = False
    for j in jobs:
        if j.get("name") == owner:
            j["state"] = state
            hit = True
    if hit:
        write_json(WATCHDOG_JOBS, jobs)
    return hit


def cmd_sleep(owner):
    """--sleep <owner>: park a worker for event-wake (see module docstring)."""
    setup()
    inflight = [r for _, r in records(RUNNING) + records(PENDING)
                if r.get("owner_agent") == owner and r.get("wake_on_done")]
    undelivered = [r for _, r in records(WAKES) if r.get("owner_agent") == owner]
    if not inflight and not undelivered:
        print(f"REFUSED: no wake_on_done job in flight for '{owner}' — parking would "
              f"sleep forever. Submit first (scratch_submit_job.sh) or poll instead.",
              file=sys.stderr)
        return 2
    (ROOT / "scratch_full_logs" / f"worker_{owner}.done").touch()
    tracked = _watchdog_set_state(owner, "waiting_jobs")
    write_json(SLEEPING / f"{owner}.json",
               {"owner": owner, "ts": time.time(),
                "jobs": [r["job_id"] for r in inflight],
                "note": "parked by scratch_job_sleep.sh; jobmgr wakes on completion "
                        "(scratch_interrupt_worker.sh reverses the sentinel/state marks)"})
    ids = ", ".join(r["job_id"] for r in inflight) or "(wake already due)"
    print(f"parked '{owner}' (sentinel touched; watchdog state="
          f"{'waiting_jobs' if tracked else 'n/a — not watchdog-tracked'}; sleeping marker set)")
    print(f"waiting on: {ids}")
    if undelivered:
        print(f"NOTE: {len(undelivered)} finished job(s) already have a wake due — "
              f"you will be woken almost immediately.")
    print("END YOUR TURN NOW (exit rc=0). jobmgr will interrupt-relaunch you with "
          "'JOB <id> DONE ...' injected when the job finishes.")
    return 0


# ---------------------------------------------------------------- monitoring

def finish(path: Path, rec, status, rc=None, timed_out=False, note=None):
    """Promote running -> done (+ wake ticket if wake_on_done)."""
    rec.update(status=status, rc=rc, timed_out=bool(timed_out),
               finish_ts=time.time())
    if note:
        rec["note"] = note
    rec["duration_s"] = round(rec["finish_ts"] - rec.get("start_ts", rec["finish_ts"]), 1)
    if rec.get("wake_on_done"):
        rec["wake_pending"] = True
    write_json(DONE / f"{rec['job_id']}.json", rec)
    if rec.get("wake_on_done"):
        write_json(WAKES / f"{rec['job_id']}.json", rec)
    path.unlink(missing_ok=True)
    log(f"{rec['job_id']} ({rec['type']}, owner={rec['owner_agent']}) -> {status}"
        f" rc={rc} dur={rec['duration_s']}s{' ' + note if note else ''}")


def check_cpu(path: Path, rec):
    rcfile = RCDIR / f"{rec['job_id']}.rc"
    if rcfile.exists():
        try:
            rc = int(rcfile.read_text().strip())
        except Exception:
            rc = None
        finish(path, rec, "done", rc=rc)
        return
    if rec.get("adopted"):
        # Task 274 G3: an adopted self-monitored job has NO rc shim, so its exit
        # code is unobservable. Monitor by pid identity (pid + /proc starttime,
        # pid-reuse-safe); on disappearance finish as DONE (rc unknown), not died —
        # the process merely exited, which for an adopted job IS completion.
        pid = rec.get("pid")
        st = _starttime_of(pid) if pid else None
        alive = st is not None and (rec.get("starttime") is None or st == rec["starttime"])
        if alive:
            if rec.pop("_gone_since", None) is not None:
                write_json(path, rec)
            return
        gone = rec.get("_gone_since")
        if gone is None:
            rec["_gone_since"] = time.time()
            write_json(path, rec)
        elif time.time() - gone > GONE_GRACE_SEC:
            finish(path, rec, "done", rc=None,
                   note="adopted self-monitored job finished (exit code unobservable — not "
                        "wrapped by jobmgr_run; completion detected by the process exiting)")
        return
    # no rc yet: is the detached process still alive?
    alive = False
    try:
        pid = int((ROOT / rec["pid_file"]).read_text().strip())
        cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
        alive = rec["job_id"] in cmdline      # pid-reuse guard: shim argv carries the id
    except Exception:
        alive = False
    if alive:
        if rec.pop("_gone_since", None) is not None:
            write_json(path, rec)
        return
    gone = rec.get("_gone_since")
    if gone is None:
        rec["_gone_since"] = time.time()
        write_json(path, rec)
    elif time.time() - gone > GONE_GRACE_SEC:
        finish(path, rec, "died", rc=None,
               note="process gone with no rc file (killed? shim never ran?)")


def check_gpu(path: Path, rec):
    gid = rec.get("gpu_job_id", "")
    done = read_json(GPUQ / "done" / f"{gid}.json")
    if done is not None:
        finish(path, rec, "done", rc=done.get("exit_code"),
               timed_out=done.get("timed_out", False))
        return
    if (GPUQ / "pending" / f"{gid}.json").exists() or (GPUQ / "running" / f"{gid}.json").exists():
        if rec.pop("_gone_since", None) is not None:
            write_json(path, rec)
        return
    gone = rec.get("_gone_since")            # in no gpu_queue dir at all
    if gone is None:
        rec["_gone_since"] = time.time()
        write_json(path, rec)
    elif time.time() - gone > GPU_VANISH_GRACE_SEC:
        finish(path, rec, "died", rc=None,
               note="gpu job vanished from gpu_queue (pending json deleted = cancelled?)")


def check_overdue(path: Path, rec, now):
    if rec.get("adopted"):
        return                       # Task 274: adopted jobs have unknown length — no overdue alert
    limit = max(2 * rec.get("est_seconds", 0), rec.get("est_seconds", 0) + 3600)
    elapsed = now - rec.get("start_ts", now)
    if elapsed > limit and not rec.get("overdue_alerted"):
        rec["overdue_alerted"] = True
        write_json(path, rec)
        log(f"OVERDUE {rec['job_id']} (owner={rec['owner_agent']}): "
            f"{int(elapsed)}s elapsed vs est {rec['est_seconds']}s")
        email(f"⏳ jobmgr: job {rec['job_id']} overdue "
              f"({int(elapsed / 60)} min vs est {int(rec['est_seconds'] / 60)} min)",
              f"Job {rec['job_id']} (owner {rec['owner_agent']}, {rec['type']}) is still "
              f"running after {int(elapsed / 60)} min — estimate was "
              f"{int(rec['est_seconds'] / 60)} min.\n"
              f"Command: {rec['command']}\nOutput: {rec.get('output_path')}\n"
              f"I never kill jobs — this is informational. If its owner is parked "
              f"asleep it stays parked until the job finishes or you intervene.")


def _job_block(rec):
    """The per-job lines of a wake message (owner + output are the handover)."""
    status = rec["status"].upper()
    if rec.get("timed_out"):
        status = "TIMED OUT (gpu_manager enforced the timeout)"
    lines = [f"JOB {rec['job_id']} {status}: rc={rec.get('rc')} after "
             f"{rec.get('duration_s', '?')}s (est {rec.get('est_seconds')}s, {rec['type']}"
             + (", adopted self-monitored" if rec.get('adopted') else "") + ").",
             f"  Output: {rec.get('output_path')}",
             f"  Result record: {rec['result_path']}",
             f"  Command was: {rec['command']}"]
    if rec.get("note"):
        lines.append(f"  Note: {rec['note']}")
    return "\n".join(lines)


def combined_wake_message(owner, recs):
    """ONE handover message covering all of `owner`'s just-finished jobs (Task 274
    batching), so a worker whose several jobs completed is relaunched once."""
    others = [r for _, r in records(RUNNING) if r.get("owner_agent") == owner]
    held = any(r.get("held_released_ts") for r in recs)
    head = (f"{len(recs)} of your jobs finished" if len(recs) > 1 else "Your job finished")
    if held:
        head += " while you were held out by a usage limit (now reset)"
    lines = [head + ":", ""]
    for r in recs:
        lines.append(_job_block(r))
        lines.append("")
    if others:
        lines.append(f"You still have {len(others)} job(s) running under jobmgr: "
                     + ", ".join(f"{r['job_id']} (est {r.get('est_seconds')}s)" for r in others)
                     + ". To wait on them again, re-park: bash scratch_job_sleep.sh " + owner)
        lines.append("")
    lines.append("(Automated completion wake from jobmgr — reconcile the result(s), then "
                 "continue your task. Reminder: poll <=240 s chunks for <=50-min waits; "
                 "submit+sleep via scratch_submit_job.sh / scratch_job_sleep.sh for longer.)")
    return "\n".join(lines)


def _hold_ticket(path, rec, st, now):
    """Task 274 G4: a job finished while a limit is active. Relaunching its owner
    now would just re-hit the limit, so HOLD the ticket and email the operator ONCE per
    job (id + responsible agent + command + rc + OUTPUT PATH + held-until-reset).
    The ticket stays in wakes/ and is delivered when the limit clears. After the
    first hold this is a cheap no-op each tick (no re-write, no re-email) — delivery
    on clear is prompt because deliver_owner_wakes re-checks limit state every tick
    BEFORE any retry throttle."""
    if rec.get("held_notified"):
        return
    rec["held_by_limit"] = True
    rec["held_notified"] = True
    rec["held_since"] = now
    write_json(path, rec)
    d = DONE / f"{rec['job_id']}.json"
    drec = read_json(d)
    if drec is not None:
        drec["held_by_limit"] = True
        drec["held_since"] = now
        write_json(d, drec)
    kind = (st or {}).get("kind", "usage")
    reset_epoch = (st or {}).get("reset_epoch")
    rt = datetime.fromtimestamp(reset_epoch).strftime("%a %H:%M") if reset_epoch else "?"
    log(f"HOLD wake {rec['job_id']} (owner={rec['owner_agent']}) — {kind} limit active, "
        f"reset ~{rt}; emailed once, ticket held")
    email(f"⏸ jobmgr: job {rec['job_id']} DONE (owner {rec['owner_agent']}) — held until the "
          f"{kind} limit resets",
          f"A job finished, but the {kind} usage limit is active so I cannot relaunch its "
          f"owner yet — I am HOLDING the completion for handover at reset.\n\n"
          f"Job: {rec['job_id']} ({rec['type']}"
          + (", adopted self-monitored" if rec.get('adopted') else "") + ")\n"
          f"Responsible agent (owner): {rec['owner_agent']}\n"
          f"Status: {rec.get('status')}  rc={rec.get('rc')}  after {rec.get('duration_s', '?')}s\n"
          f"Command: {rec.get('command')}\n"
          f"Output path: {rec.get('output_path')}\n"
          f"Result record: {rec.get('result_path')}\n\n"
          f"I will relaunch {rec['owner_agent']} the moment the {kind} limit resets (~{rt}) and "
          f"hand this finished job over (batched with any others it has). No action needed.")


def deliver_owner_wakes(owner, items, now):
    """Deliver ALL ready wake tickets for one owner as ONE relaunch (Task 274
    batching) — an owner whose several jobs finished gets a single handover, not a
    relaunch per job. Honors the limit hold, the per-ticket retry backoff, and the
    existing interrupt-vs-mailbox choice. items = [(ticket_path, rec), ...].

    When a limit is active the whole set is HELD (email once per job, keep tickets);
    when it is not active (normal path, or the limit just cleared) the set is
    delivered together. The no-limit single-job case is byte-for-byte the prior
    behaviour with a one-job combined message."""
    # Limit check FIRST (before any retry throttle) so held tickets deliver PROMPTLY
    # the tick the limit clears — "relaunch right away at reset".
    active, st = limit_active(now)
    if active:
        for p, rec in items:
            _hold_ticket(p, rec, st, now)
        return
    # not active: respect the soonest scheduled retry across this owner's tickets
    if now < min((r.get("_next_wake_ts", 0) for _, r in items), default=0):
        return
    # limit not active (or just cleared): deliver a single combined handover
    for _, rec in items:
        if rec.pop("held_by_limit", None):
            rec["held_released_ts"] = now
    recs = [rec for _, rec in items]
    msgfile = MSG / f"owner_{owner}.txt"
    already_appended = any(rec.get("_appended") for _, rec in items)
    if not already_appended:
        msgfile.write_text(combined_wake_message(owner, recs))
    sleeping = (SLEEPING / f"{owner}.json").exists()
    live = claude_alive_nonzombie(owner)
    # Sleeping marker is authoritative (a parked owner consented to the interrupt
    # wake even if its claude still breathes) — closes the mailbox-append/exit race.
    use_interrupt = sleeping or not live
    tool = "scratch_interrupt_worker.sh" if use_interrupt else "scratch_mailbox_append.sh"
    if use_interrupt:
        cmd = ["bash", INTERRUPT_CMD, owner, "--relay-from", "jobmgr"]
        if not already_appended:                      # rc=3 retry already appended it
            cmd += ["--message-file", str(msgfile)]
    else:
        cmd = ["bash", MAILBOX_APPEND_CMD, owner, "relay from agent jobmgr", str(msgfile)]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
        ok = out.returncode == 0
        rc3 = use_interrupt and out.returncode == 3   # appended but relaunch-verify failed
        detail = (out.stdout + out.stderr).strip().splitlines()[-1:] or [""]
    except Exception as e:
        ok, rc3, detail = False, False, [str(e)]
    if ok:
        wt = time.time()
        for p, rec in items:
            rec["woken_ts"] = wt
            rec["wake_pending"] = False
            rec["wake_via"] = "interrupt" if use_interrupt else "mailbox_append"
            rec.pop("held_by_limit", None)
            write_json(DONE / f"{rec['job_id']}.json", rec)
            p.unlink(missing_ok=True)                 # ticket consumed
        if sleeping:
            (SLEEPING / f"{owner}.json").unlink(missing_ok=True)
        log(f"WAKE delivered for {owner} ({len(items)} job(s): "
            f"{', '.join(r['job_id'] for _, r in items)}) via {tool} "
            f"(sleeping={sleeping}, live={live}): {detail[0]}")
        return
    n = 0
    for p, rec in items:
        n = rec.get("_wake_attempts", 0) + 1
        rec["_wake_attempts"] = n
        if rc3:
            rec["_appended"] = True
        rec["_next_wake_ts"] = now + WAKE_RETRY_BACKOFF[min(n - 1, len(WAKE_RETRY_BACKOFF) - 1)]
        write_json(p, rec)
    log(f"WAKE FAILED for {owner} ({len(items)} job(s)) via {tool} (attempt {n}): {detail[0]}")
    if n == WAKE_ALERT_AFTER:
        email(f"🚨 jobmgr: cannot wake '{owner}' for {len(items)} finished job(s)",
              f"{len(items)} job(s) for owner '{owner}' finished "
              f"({', '.join(r['job_id'] for _, r in items)}) but delivering the wake failed "
              f"{n} times (last: {detail[0]}).\n"
              f"I keep retrying with backoff; tickets are in scratch_full_logs/jobs/wakes/.\n"
              f"Typical cause: the owner has no relaunch script (not spawned via "
              f"scratch_spawn_worker.sh).")


def check_stale_sleepers(now):
    for p, marker in records(SLEEPING):
        owner = marker.get("owner", p.stem)
        inflight = any(r.get("owner_agent") == owner and r.get("wake_on_done")
                       for _, r in records(RUNNING) + records(PENDING))
        ticket = any(r.get("owner_agent") == owner for _, r in records(WAKES))
        if inflight or ticket:
            if marker.pop("_stale_since", None) is not None:
                write_json(p, marker)
            continue
        if marker.get("_stale_alerted"):
            continue
        stale_since = marker.get("_stale_since")
        if stale_since is None:
            marker["_stale_since"] = now
            write_json(p, marker)
        elif now - stale_since > STALE_SLEEP_SEC:
            marker["_stale_alerted"] = True
            write_json(p, marker)
            log(f"STALE SLEEPER: {owner} parked with nothing in flight")
            email(f"🚨 jobmgr: '{owner}' is parked asleep with NOTHING to wake it",
                  f"Worker '{owner}' has a sleep marker but no running/pending "
                  f"wake_on_done job and no undelivered wake — nothing will ever wake "
                  f"it, and while parked the watchdog leaves it alone (state "
                  f"'waiting_jobs').\n"
                  f"To un-park manually:\n"
                  f"  rm -f scratch_full_logs/worker_{owner}.done scratch_full_logs/jobs/sleeping/{owner}.json\n"
                  f"  # set its state back to 'running' in scratch_full_logs/watchdog_jobs.json\n"
                  f"  bash scratch_interrupt_worker.sh {owner} --relay-from jobmgr "
                  f"--message-file <note>   # or let the watchdog revive it\n"
                  f"I am NOT un-parking it automatically.")


def tick():
    now = time.time()
    # dedupe: a crash between done-publish and running-unlink leaves both
    for p, rec in records(RUNNING):
        if (DONE / f"{rec['job_id']}.json").exists():
            p.unlink(missing_ok=True)
    # orphaned pending records (submit CLI died mid-dispatch)
    for p, rec in records(PENDING):
        if now - rec.get("submit_ts", now) > PENDING_ORPHAN_SEC:
            finish(p, rec, "died", rc=None,
                   note="orphaned in pending/ — submit CLI died before dispatch; "
                        "NOT auto-dispatched (may or may not have started)")
    for p, rec in records(RUNNING):
        (check_gpu if rec.get("type") == "gpu" else check_cpu)(p, rec)
    for p, rec in records(RUNNING):          # re-read: finish() may have moved some
        check_overdue(p, rec, now)
    # Task 274: group undelivered wake tickets by owner so an owner with several
    # finished jobs gets ONE relaunch carrying all of them (and, under a limit, one
    # hold decision per owner). Single-ticket owners take the byte-identical path.
    by_owner = {}
    for p, rec in records(WAKES):
        by_owner.setdefault(rec.get("owner_agent", "?"), []).append((p, rec))
    for owner, items in by_owner.items():
        deliver_owner_wakes(owner, items, now)
    check_stale_sleepers(now)


# ---------------------------------------------------------------- status

def cmd_status(owner=None):
    setup()
    want = (lambda r: owner is None or r.get("owner_agent") == owner)
    print(f"=== jobmgr status {datetime.now():%Y-%m-%d %H:%M:%S}"
          f"{' (owner=' + owner + ')' if owner else ''} ===")
    for name, d in (("PENDING", PENDING), ("RUNNING", RUNNING)):
        rows = [r for _, r in records(d) if want(r)]
        print(f"{name} ({len(rows)}):")
        for r in rows:
            el = int(time.time() - r.get("start_ts", r.get("submit_ts", time.time())))
            print(f"  {r['job_id']} {r['type']} owner={r['owner_agent']} "
                  f"est={r.get('est_seconds')}s elapsed={el}s wake={r.get('wake_on_done')}"
                  f" cmd: {r['command'][:90]}")
    rows = [r for _, r in records(WAKES) if want(r)]
    print(f"WAKES UNDELIVERED ({len(rows)}):" )
    for r in rows:
        print(f"  {r['job_id']} -> {r['owner_agent']} (attempts={r.get('_wake_attempts', 0)})")
    marks = [m for _, m in records(SLEEPING) if owner is None or m.get("owner") == owner]
    print(f"SLEEPING owners ({len(marks)}):")
    for m in marks:
        print(f"  {m.get('owner')} since {datetime.fromtimestamp(m['ts']):%H:%M:%S} "
              f"waiting on {m.get('jobs')}")
    done = sorted(DONE.glob("*.json"), key=lambda p: p.stat().st_mtime)[-10:]
    print(f"DONE (last {len(done)}):")
    for p in done:
        r = read_json(p) or {}
        if not want(r):
            continue
        print(f"  {r.get('job_id')} {r.get('status')} rc={r.get('rc')} "
              f"owner={r.get('owner_agent')} dur={r.get('duration_s')}s "
              f"woken={'yes' if r.get('woken_ts') else ('n/a' if not r.get('wake_on_done') else 'PENDING')}")
    return 0


# ---------------------------------------------------------------- main

def main():
    args = sys.argv[1:]
    if args[:1] == ["--submit"]:
        sys.exit(cmd_submit(args[1:]))
    if args[:1] == ["--sleep"]:
        if len(args) != 2:
            print("usage: scratch_jobmgr.py --sleep <owner>", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_sleep(args[1]))
    if args[:1] == ["--status"]:
        sys.exit(cmd_status(args[1] if len(args) > 1 else None))

    setup()
    if args[:1] == ["--once"]:
        tick()
        sys.exit(0)

    # single-instance guard: hold the flock for the daemon's lifetime
    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[jobmgr] another instance holds the lock — exiting", file=sys.stderr)
        sys.exit(1)
    lockf.write(f"{os.getpid()}\n")
    lockf.flush()

    log(f"loop started (poll {POLL}s, pid {os.getpid()}) — ZERO Claude API calls by design")
    last_beat = time.time()
    while True:
        try:
            tick()
        except Exception as e:
            log(f"tick error: {e}")
        if time.time() - last_beat > HEARTBEAT_SEC:
            last_beat = time.time()
            n_run = len(list(RUNNING.glob("*.json")))
            n_sleep = len(list(SLEEPING.glob("*.json")))
            log(f"heartbeat: {n_run} running, {n_sleep} sleeping owner(s)")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
