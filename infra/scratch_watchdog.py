#!/usr/bin/env python3
"""Always-on watchdog for spawned task workers.

A spawned worker is a headless `claude -p` process in tmux. If it dies before
finishing (session-limit kill, or a crash), nothing restarts it on its own. This
watchdog watches each tracked worker's process and:
  - emails the user when a worker is killed,
  - relaunches it (resuming its session, so it keeps full context) — immediately
    for a crash, or right after the reset for a session-limit kill — and emails
    again when it does.
Every email lists the currently-active workers.

It is PURE PYTHON (only sends email + spawns tmux) so it never calls the LLM and
can't hit the usage limit itself.

State file: scratch_full_logs/watchdog_jobs.json — a list of jobs:
  {name, session, log, relaunch, done_sentinel, requester,
   state, reset_epoch, relaunched}
States stored: running | waiting_reset | waiting_jobs | done | failed.
classify() additionally yields transient states (no_marker | interrupted |
exited_incomplete) that tick() resolves into a relaunch or a stored state.
Jobs are added by scratch_spawn_worker.sh (and can be seeded by hand).

Task 216 (uid=217): 'waiting_jobs' = the worker parked itself on a submitted
jobmgr job (scratch_job_sleep.sh) and is waiting for the event-wake. The
watchdog LEAVES IT ALONE exactly like done/failed — no exited_incomplete
relaunch loop, no dark alarms — but the roster shows it honestly as
waiting_jobs instead of masquerading as done. scratch_interrupt_worker.sh
(the wake path jobmgr uses, and the mail-interrupt path) already sets state
back to 'running', so waking needs no watchdog change.

Task 123 hardening:
  - liveness = tmux has-session '=name' (exact). NOT `pgrep -f <session-id>`:
    that matches unrelated processes (the user's VS Code window resuming the
    same session id kept 'paper' looking alive all afternoon on Jul 8 while its
    mail sat stranded).
  - a dead worker with NO exit marker (tmux kill-session SIGHUPs bash before the
    `EXITED rc=` echo runs) is now handled: relaunched at once if it has unread
    mail (interrupt flow), else declared crashed after DEAD_TICKS dead polls.
  - relaunches are rate-limited per job (RELAUNCH_MIN_GAP) — no relaunch storms.
  - NO-REPLY alarm: the inbox router writes a receipt json per delivered email;
    if the receiving agent sends nothing for NO_REPLY_SEC after a delivery, the
    user is alerted once (receipt renamed *.alerted; satisfied ones -> *.ok).

Task 156 hardening (the noaa_fullds 42h dead-worker incident):
  - F1 sentinel-gated completion: EXITED rc=0 alone is NOT completion any more.
    rc=0 with the done-sentinel -> done; rc=0 WITHOUT it -> exited_incomplete
    (armed-waiter death: the worker ended its turn to "wait" — nothing
    re-invokes a headless claude -p) -> relaunched with exponential backoff
    (2,4,8,16,32 min; MAX_RELAUNCH cap, then failed).
  - F3 audible transitions: every LIVE running->done emails one line (name,
    ~runtime, sentinel yes/no, log tail) — a mis-classified "done" is visible in
    minutes, not 40 h. Plus a generic dead-no-relaunch alarm: a non-terminal job
    dead & un-relaunched > DARK_ALERT_MIN alerts once per dark episode.
  - F5 heartbeat: an ALIVE worker whose log is unchanged > STALE_MIN alerts once
    per stall episode; NEVER auto-kills (long blocking scoring calls are
    legitimately silent for hours).

Task 176 (noaa_fullds rapid-death investigation):
  - survival resets the attempt counter: a relaunched worker whose claude is
    alive (non-zombie, inside its own tmux pane tree) >= SURVIVE_RESET_SEC
    after the relaunch gets relaunched=0 — MAX_RELAUNCH now bounds a rapid
    relaunch LOOP instead of accumulating across unrelated deaths forever.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scratch_inbox import parse_reset_epoch, parse_limit_notice   # reset-time + kind parsing
import scratch_notify_email as notify

ROOT = Path(__file__).resolve().parent
# TSOMP_WATCHDOG_JOBS / TSOMP_JOBS_DIR: test-only path overrides (Task 274), matching
# scratch_jobmgr.py, so the limit tests run against isolated state; unset in prod.
JOBS = Path(os.environ.get("TSOMP_WATCHDOG_JOBS")
            or (ROOT / "scratch_full_logs" / "watchdog_jobs.json"))
LOG = ROOT / "scratch_full_logs" / "watchdog.log"
SENT = ROOT / "scratch_full_logs" / "sent_emails.jsonl"
RECEIPTS = ROOT / "scratch_full_logs" / "inbox" / "receipts"
# Task 274: the account-global limit marker BOTH daemons read (see DESIGN.md).
# The 5-hour session and weekly limits both bill the operator's Claude Max subscription,
# so a limit hit blocks EVERY headless worker at once — one shared marker, not a
# per-worker flag, is the single source of truth jobmgr keys its hold/handover on.
_JOBS_DIR = Path(os.environ.get("TSOMP_JOBS_DIR") or (ROOT / "scratch_full_logs" / "jobs"))
LIMIT_STATE = _JOBS_DIR / "limit_state.json"
WAKES_DIR = _JOBS_DIR / "wakes"
LIMIT_HANDOVER_GRACE = 300   # sec after reset to defer a waiting_reset relaunch to
                             # jobmgr's handover before the watchdog relaunches anyway
MAX_RELAUNCH = 5
SURVIVE_RESET_SEC = 300   # Task 176: a relaunched worker alive this long gets
                          # its attempt counter reset to 0 — the cap should stop
                          # rapid relaunch LOOPS, not accumulate forever across
                          # unrelated deaths days apart (noaa_fullds reached 4/5
                          # through mail interrupts + two self-kill episodes).
POLL = 45
DEAD_TICKS = 3            # dead polls with no marker before declaring a crash
RELAUNCH_MIN_GAP = 120    # seconds between relaunches of the same job
NO_REPLY_SEC = 15 * 60    # alert if an agent stays silent this long after mail
DARK_ALERT_MIN = 10       # F3: dead & un-relaunched this long (min) -> alarm
STALE_MIN = 60            # F5: alive but log unchanged this long (min) -> alert
HEARTBEAT_PERSIST = 300   # F5: min sec between heartbeat-field JSON writes
# Task 165 F2: infra sessions checked for a stale (deleted) script inode.
# Only `bash <script>` pane leaders are detectable (bash keeps the running
# script open on fd 255). 'watchdog' and 'gpu_manager' run python via
# bash/zsh -c — python reads its source once and closes it, so a stale copy
# of those can never be seen from /proc; do not list them here.
INFRA_STALE_SESSIONS = ("inbox",)
STALE_ALERT_GAP = 24 * 3600   # at most one stale-inode email per session/day
STALE_CONFIRM_TICKS = 2       # consecutive stale ticks before alerting: v2's
                              # RESTART_LOOP self-update legitimately shows a
                              # deleted inode for <=30 s; one 45 s poll gap
                              # filters that out, a real stale loop persists


def log(msg):
    line = f"[watchdog] {datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load():
    try:
        return json.loads(JOBS.read_text())
    except Exception:
        return []


def save(j):
    JOBS.write_text(json.dumps(j, indent=2))


# ---------------------------------------------------------------- Task 274: limit state
def read_limit_state():
    try:
        return json.loads(LIMIT_STATE.read_text())
    except Exception:
        return None


def write_limit_state(kind, reset_epoch, reset_str, source):
    """Write/refresh the account-global limit marker (Task 274). Keeps reset_epoch
    at the MAX seen and never downgrades kind from 'weekly', so a straggler
    detection with a shorter/wrong reset can't shorten the hold. Atomic publish."""
    cur = read_limit_state() or {}
    now = time.time()
    active = bool(cur.get("active"))
    if active:
        if (cur.get("reset_epoch") or 0) >= (reset_epoch or 0):
            reset_epoch = cur.get("reset_epoch")
            reset_str = cur.get("reset_str") or reset_str
        if cur.get("kind") == "weekly":
            kind = "weekly"
    obj = {"active": True, "kind": kind, "reset_epoch": reset_epoch,
           "reset_str": reset_str, "source_worker": source,
           "hit_ts": cur.get("hit_ts", now) if active else now, "updated_ts": now}
    try:
        LIMIT_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIMIT_STATE.parent / f".{LIMIT_STATE.name}.tmp"
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.rename(LIMIT_STATE)               # atomic
    except Exception as e:
        log(f"write_limit_state error: {e}")
    return obj


def clear_limit_state():
    try:
        LIMIT_STATE.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        log(f"clear_limit_state error: {e}")
        return False


def has_held_wake(owner):
    """True iff jobmgr is holding a limit-blocked completion wake for `owner`
    (Task 274). The watchdog defers that worker's reset-relaunch to jobmgr, which
    relaunches AND hands over the finished job in one shot — avoiding a double
    relaunch. Any error -> False (fall through to the watchdog's own relaunch)."""
    try:
        for p in WAKES_DIR.glob("*.json"):
            try:
                r = json.loads(p.read_text())
            except Exception:
                continue
            if r.get("owner_agent") == owner and r.get("held_by_limit"):
                return True
    except Exception:
        pass
    return False


def should_defer_reset_relaunch(job, now):
    """Task 274: at reset, the watchdog DEFERS a waiting_reset worker's relaunch to
    jobmgr when jobmgr is holding a finished-job wake for it — jobmgr's relaunch
    carries the job handover, so one relaunch does both (no double-relaunch). Bounded
    by LIMIT_HANDOVER_GRACE: if jobmgr is wedged and the held wake is still around
    that long after reset, the watchdog relaunches anyway (the held wake then delivers
    to the now-live worker via a mailbox append)."""
    return (has_held_wake(job["name"])
            and now - (job.get("reset_epoch") or 0) < LIMIT_HANDOVER_GRACE)


def adopt_self_monitored(name):
    """Task 274 G3: shell out to the handoff helper so jobmgr adopts this worker's
    self-monitored scratch_detach.sh jobs at limit-kill time (owner_agent=name,
    wake_on_done=true, output_path known). Returns a one-line summary for the
    kill email. Kept as a shell-out (not inlined) so the watchdog never needs to
    know jobmgr's record schema."""
    helper = ROOT / "scratch_jobmgr_adopt.sh"
    if not helper.exists():
        return "adopt helper missing (scratch_jobmgr_adopt.sh) — self-monitored jobs NOT handed off"
    try:
        out = subprocess.run(["bash", str(helper), name],
                             capture_output=True, text=True, timeout=60)
        line = (out.stdout.strip().splitlines() or [""])[-1]
        if out.returncode != 0:
            return f"adopt helper rc={out.returncode}: {line or out.stderr.strip()[:160]}"
        return line or "no self-monitored jobs to adopt"
    except Exception as e:
        return f"adopt helper error: {e}"


def tmux_has(name):
    """Exact-match tmux session check ('=name'; bare -t would prefix-match)."""
    return subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def alive(job):
    """A worker is alive iff ITS tmux session exists. Session-id pgrep is only a
    secondary confirmation inside the tmux; alone it is meaningless (VS Code /
    one-shot handlers resume the same session id outside tmux)."""
    return tmux_has(job["name"])


def claude_alive_nonzombie(job):
    """Task 176: True iff a LIVE (non-zombie) claude process is running inside
    the worker's OWN tmux pane tree. Used to gate the attempt-counter reset;
    alive() (= tmux has-session) is NOT enough evidence for that:
      - the wrapper bash keeps the session alive while claude is dead (death
        bundle collection, sentinel-trap gaps between claude runs);
      - during a surgical kill the pane shell is SIGSTOPped first, so a killed
        claude sits as a ZOMBIE (dead-but-not-yet-reaped) for a moment while
        tmux still reports the session — must not count as survival;
      - a bare pgrep on the session id matches unrelated processes (Task 123:
        VS Code / one-shot handlers resume the same session id outside tmux).
    Any error -> False (no reset without positive evidence)."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-s", "-t", f"={job['name']}",
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
                    continue          # zombie: dead-but-not-yet-reaped
                try:
                    argv0 = open(f"/proc/{k}/cmdline", "rb").read().split(b"\0")[0]
                except OSError:
                    continue
                if os.path.basename(argv0.decode(errors="replace")) == "claude":
                    return True
        return False
    except Exception:
        return False


def unread_mail(job):
    mb = ROOT / "scratch_full_logs" / "inbox" / f"mailbox_{job['name']}.md"
    try:
        return mb.stat().st_size > 0
    except OSError:
        return False


def last_run_tail(logpath, limit=4000):
    """The log tail since the most recent start/RELAUNCH marker = the latest run."""
    try:
        lines = Path(logpath).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    start = 0
    for i, l in enumerate(lines):
        if "starting" in l or "RELAUNCH" in l:
            start = i
    return "\n".join(lines[start:])[-limit:]


def classify(job):
    """Return (state, reset_epoch, rc) for a job — rc is the parsed 'EXITED
    rc=N' string (None when there is no marker; Task 170 F4 surfaces it in the
    crash email). 'no_marker' = dead without any exit evidence (typical of
    tmux kill-session) — tick() decides what to do with it."""
    if alive(job):
        return ("running", None, None)
    if Path(job.get("done_sentinel", "/nonexistent")).exists():
        return ("done", None, None)
    tail = last_run_tail(job["log"])
    # any usage limit (session / weekly / 5-hour / daily) — "...limit · resets ..."
    if re.search(r"(session|weekly|usage|daily|hour) limit", tail, re.I) and re.search(r"resets", tail, re.I):
        m = re.search(r"resets[^\n]*", tail, re.I)
        return ("limit_killed", parse_reset_epoch(m.group(0) if m else ""), None)
    rcs = re.findall(r"EXITED rc=(-?\d+)", tail)
    if rcs and rcs[-1] == "0":
        # Task 156 F1: rc=0 alone is NOT completion. True completion returned
        # "done" at the sentinel check above; an rc=0 exit WITHOUT the sentinel
        # is an armed-waiter death (the worker ended its turn to "wait" —
        # nothing re-invokes a headless claude -p). tick() relaunches it.
        return ("exited_incomplete", None, "0")
    if rcs:
        return ("crashed", None, rcs[-1])   # non-zero exit, no limit notice
    if unread_mail(job):
        return ("interrupted", None, None)  # tmux-killed with mail waiting -> relaunch to drain
    return ("no_marker", None, None)     # dead, no evidence yet; needs DEAD_TICKS polls


SIGDECODE = {129: "SIGHUP", 130: "SIGINT", 134: "SIGABRT", 137: "SIGKILL",
             139: "SIGSEGV", 143: "SIGTERM"}


def rc_decode(rc):
    """Task 170 F4: one-line human decode of a worker exit rc (string or None)."""
    if rc is None:
        return ("no exit marker — the pane died before the rc echo could run "
                "(typical of tmux kill-session)")
    try:
        n = int(rc)
    except (TypeError, ValueError):
        return f"rc={rc}"
    if n > 128:
        return f"rc={n} = 128+{n - 128} ({SIGDECODE.get(n, 'signal ' + str(n - 128))})"
    return f"rc={n} (plain nonzero exit, not a signal death)"


def active_names(jobs):
    return [j["name"] for j in jobs if j["state"] == "running" and alive(j)]


def roster(jobs):
    act = active_names(jobs)
    lines = []
    for j in jobs:
        st = j["state"]
        if st == "running" and not alive(j):
            st = "exited(checking)"
        extra = ""
        if st == "waiting_reset" and j.get("reset_epoch"):
            k = j.get("limit_kind")
            extra = (f" ({k + ' ' if k else ''}limit; relaunch "
                     f"~{datetime.fromtimestamp(j['reset_epoch']):%a %H:%M})")
        if st == "waiting_jobs":
            extra = " (parked; jobmgr wakes it on job completion)"
        lines.append(f"  - {j['name']}: {st}{extra}")
    return (f"Actively working agents ({len(act)}): "
            f"{', '.join(act) if act else 'none'}\n\nAll tracked workers:\n" + "\n".join(lines))


def email(subject, body, jobs):
    try:
        notify.send(subject, body + "\n\n" + roster(jobs), agent="watchdog")
    except Exception as e:
        log(f"email failed: {e}")


def relaunch(job):
    subprocess.run(["tmux", "kill-session", "-t", f"={job['name']}"],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    subprocess.Popen(["tmux", "new-session", "-d", "-s", job["name"],
                      f"bash {job['relaunch']}"])
    job["relaunched"] = job.get("relaunched", 0) + 1
    job["last_relaunch_ts"] = time.time()
    job["state"] = "running"
    job["reset_epoch"] = None
    # Task 170 F3: stamp the inbox loop's guard file too — the two
    # auto-relaunchers share both guard surfaces, so neither can double-fire
    # on the same death (harmless double-stamp otherwise).
    try:
        (ROOT / "scratch_full_logs" / "inbox" / f"last_relaunch_{job['name']}").touch()
    except OSError:
        pass
    log(f"relaunched {job['name']} (attempt {job['relaunched']})")


def relaunch_allowed(job):
    return time.time() - job.get("last_relaunch_ts", 0) >= RELAUNCH_MIN_GAP


def backoff_allowed(job):
    """Task 156 F1: exponential relaunch gap for exited_incomplete — 2,4,8,16,32
    min before relaunch 1..MAX_RELAUNCH (the crashed path keeps the flat
    RELAUNCH_MIN_GAP)."""
    gap = 120 * (2 ** min(job.get("relaunched", 0), MAX_RELAUNCH - 1))
    return time.time() - (job.get("last_relaunch_ts") or 0) >= gap


def approx_runtime(logpath):
    """Best-effort ' after ~N.Nh' from the log's first start/RELAUNCH marker
    date (bash `date` format) vs the log's mtime; '' if not derivable."""
    try:
        p = Path(logpath)
        first = p.read_text(errors="replace").splitlines()[0]
        m = re.search(r"(?:starting|RELAUNCH)\s+(\w{3}\s+\w{3}\s+\d+\s+"
                      r"[\d:]+\s*(?:[AP]M)?)\s+\S+\s+(\d{4})", first)
        stamp = re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2)}").strip()
        fmt = "%a %b %d %I:%M:%S %p %Y" if ("AM" in stamp or "PM" in stamp) \
            else "%a %b %d %H:%M:%S %Y"
        start = datetime.strptime(stamp, fmt)
        hrs = (p.stat().st_mtime - start.timestamp()) / 3600
        return f" after ~{hrs:.1f}h" if 0 <= hrs < 24 * 365 else ""
    except Exception:
        return ""


def sent_entries():
    out = []
    try:
        for ln in SENT.read_text().splitlines():
            try:
                r = json.loads(ln)
                out.append((r.get("agent") or "", float(r.get("ts") or 0)))
            except Exception:
                pass
    except Exception:
        pass
    return out


def reply_guard(jobs):
    """Alert once if an agent sends nothing for NO_REPLY_SEC after a delivery.
    The mechanical acks (router / relaunch scripts) count as replies, so this
    only fires when even those failed — the last-resort net."""
    if not RECEIPTS.is_dir():
        return
    receipts = sorted(RECEIPTS.glob("*.json"))
    if not receipts:
        return
    sent = sent_entries()
    now = time.time()
    for rp in receipts:
        try:
            r = json.loads(rp.read_text())
        except Exception:
            continue
        agent, ts = r.get("agent", "?"), float(r.get("ts") or 0)
        if any(a == agent and t > ts for a, t in sent):
            rp.rename(str(rp) + ".ok")
            continue
        if now - ts > NO_REPLY_SEC:
            mins = int((now - ts) / 60)
            log(f"NO-REPLY: {agent} silent {mins} min after uid={r.get('uid')}")
            email(f"⏰ watchdog: no reply from '{agent}' {mins} min after your email",
                  f"Your email (uid {r.get('uid')}, '{r.get('subject','')[:120]}') was delivered "
                  f"to worker '{agent}' at {datetime.fromtimestamp(ts):%H:%M} but it has sent "
                  f"nothing since. Worker tmux alive: {tmux_has(agent)}. Unread mailbox: "
                  f"scratch_full_logs/inbox/mailbox_{agent}.md", jobs)
            rp.rename(str(rp) + ".alerted")


_stale_ticks = {}             # session -> consecutive ticks seen stale (in-memory)


def stale_inode(session):
    """Return the deleted script path if the tmux session's pane leader is a
    bash script running from a DELETED inode (bash holds the script on fd 255),
    else None. That means the file was rewritten on disk after the loop started
    and the edits are NOT live — the Task 165 root cause (the v1 inbox router
    ran 6 days after its v2 rewrite). Read-only; None on any error (session or
    fd 255 missing, non-script pane leader)."""
    try:
        pane = subprocess.run(["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_pid}"],
                              capture_output=True, text=True).stdout.split()[0]
        p = os.readlink(f"/proc/{pane}/fd/255")
        return p if p.endswith("(deleted)") else None
    except Exception:
        return None


def stale_infra_guard(jobs):
    """Task 165 F2: alert when an infra loop executes deleted code. EMAIL ONLY —
    never kills or restarts infra (that stays a manual decision). Requires
    STALE_CONFIRM_TICKS consecutive stale polls (filters the <=30 s deleted-inode
    window of v2's legit RESTART_LOOP self-update), then emails at most once per
    STALE_ALERT_GAP per session via a stamp file."""
    now = time.time()
    for s in INFRA_STALE_SESSIONS:
        path = stale_inode(s)
        if not path:
            _stale_ticks.pop(s, None)
            continue
        _stale_ticks[s] = _stale_ticks.get(s, 0) + 1
        if _stale_ticks[s] < STALE_CONFIRM_TICKS:
            continue
        stamp = ROOT / "scratch_full_logs" / f"stale_{s}_alerted"
        try:
            if now - stamp.stat().st_mtime < STALE_ALERT_GAP:
                continue
        except OSError:
            pass
        stamp.touch()
        script = path.replace(" (deleted)", "")
        log(f"STALE-INODE: tmux '{s}' runs deleted code ({path})")
        email(f"⚠️ watchdog: '{s}' loop runs DELETED code",
              f"The tmux session '{s}' is executing a deleted inode:\n  {path}\n"
              f"The script was rewritten on disk after the loop started, so the "
              f"on-disk code is NOT what is running (Task 165 root cause: the v1 "
              f"inbox router ran 6 days stale this way). I am NOT touching it — "
              f"restarting infra stays manual. To adopt the on-disk code:\n"
              f"  tmux kill-session -t '={s}' && tmux new-session -d -s {s} 'bash {script}'\n"
              f"(For 'inbox' specifically, `touch scratch_full_logs/inbox/RESTART_LOOP` "
              f"is gentler — the v2 loop execs its new inode itself within 30 s.)", jobs)


def heartbeat(j, jobs, now):
    """Task 156 F5: staleness alert for alive-but-silent workers. Track the
    worker log's size+mtime across polls (persisted on the job so the check
    survives tick()'s reload); if unchanged for > STALE_MIN, alert ONCE per
    stall episode. NEVER auto-kill — a single multi-hour blocking call (e.g.
    the epa_air ~220 CPU-hr scoring suite) legitimately writes nothing until it
    returns. Progress-field writes are throttled to one per HEARTBEAT_PERSIST
    to keep JSON churn near today's level; the stall clock error this adds is
    <= HEARTBEAT_PERSIST, tiny vs STALE_MIN. Returns True if the job changed."""
    try:
        st = Path(j["log"]).stat()
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        return False
    if j.get("last_log_size") != size or j.get("last_log_mtime") != mtime:
        # log moved = progress; end any stall episode (persist immediately so
        # the alarm re-arms), else persist on the throttle boundary only
        if (j.pop("_stale_alerted", None) is not None
                or now - (j.get("last_progress_ts") or 0) >= HEARTBEAT_PERSIST):
            j["last_log_size"] = size
            j["last_log_mtime"] = mtime
            j["last_progress_ts"] = now
            return True
        return False
    if j.get("last_progress_ts") is None:
        j["last_log_size"] = size
        j["last_log_mtime"] = mtime
        j["last_progress_ts"] = now
        return True
    mins = (now - j["last_progress_ts"]) / 60
    if mins > STALE_MIN and not j.get("_stale_alerted"):
        j["_stale_alerted"] = True
        log(f"STALE: {j['name']} alive but log unchanged {int(mins)} min")
        email(f"⏳ watchdog: '{j['name']}' is alive but silent ({int(mins)} min)",
              f"Heartbeat alert (Task 156 F5): worker '{j['name']}' has a live tmux session "
              f"but its log ({j['log']}) has not changed for {int(mins)} min. NOT killing it — "
              f"a single long blocking call (big scoring/training runs) can legitimately stay "
              f"quiet for hours. If this is unexpected, inspect with: tmux attach -t ={j['name']}",
              jobs)
        return True
    return False


def dark_guard(jobs, now):
    """Task 156 F3(ii): generic dead-no-relaunch alarm. Any tracked job with
    state not in (done, failed) whose tmux is dead and which nothing has
    relaunched for > DARK_ALERT_MIN gets ONE alert per dark episode (the flag
    clears when the session is alive again). This is the belt-and-braces net
    for unknown-unknowns: dead-and-dark is caught in minutes, not 40 h.
    Returns True if any job changed."""
    changed = False
    for j in jobs:
        # waiting_jobs (Task 216): parked for a jobmgr event-wake — dead tmux
        # is its NORMAL condition, so the dead-and-dark alarm must not fire.
        if j["state"] in ("done", "failed", "waiting_jobs"):
            continue
        if alive(j):
            if j.pop("_dark_since", None) is not None:
                changed = True
            if j.pop("_dark_alerted", None) is not None:
                changed = True
            continue
        if j.get("_dark_since") is None:
            j["_dark_since"] = now
            changed = True
            continue
        ref = max(j["_dark_since"], j.get("last_relaunch_ts") or 0)
        mins = (now - ref) / 60
        if mins > DARK_ALERT_MIN and not j.get("_dark_alerted"):
            j["_dark_alerted"] = True
            changed = True
            extra = ""
            if j["state"] == "waiting_reset" and j.get("reset_epoch"):
                extra = (f" NOTE: it is waiting for a usage-limit reset (relaunch "
                         f"~{datetime.fromtimestamp(j['reset_epoch']):%H:%M}), so "
                         f"dead-until-then is expected — this is just the audible trace.")
            log(f"DARK: {j['name']} ({j['state']}) dead & un-relaunched {int(mins)} min")
            email(f"🚨 watchdog: '{j['name']}' is dead and nothing has relaunched it "
                  f"({int(mins)} min)",
                  f"Generic dead-no-relaunch alarm (Task 156 F3): worker '{j['name']}' is in "
                  f"state '{j['state']}', its tmux session is gone, and no relaunch has "
                  f"happened for {int(mins)} min.{extra} Log: {j['log']}", jobs)
    return changed


def tick():
    jobs = load()
    changed = False
    now = time.time()
    # Task 274: maintain the account-global limit marker. Clear it once the reset
    # passes (jobmgr also treats now>=reset_epoch as over, on its faster tick, so
    # held handovers fire promptly — this just deletes the stale file).
    ls = read_limit_state()
    if ls and ls.get("active") and now >= (ls.get("reset_epoch") or 0):
        if clear_limit_state():
            log(f"{ls.get('kind','usage')} limit reset "
                f"(~{datetime.fromtimestamp(ls.get('reset_epoch') or now):%a %H:%M}) "
                f"— cleared shared limit marker")
    for j in jobs:
        st = j["state"]
        # waiting_jobs (Task 216, uid=217): the worker parked itself on a
        # jobmgr job (scratch_job_sleep.sh). Leave it alone — jobmgr's wake
        # (scratch_interrupt_worker.sh) sets state back to 'running'. Treating
        # the unknown state as running would classify the parked worker
        # exited_incomplete and relaunch-loop it, defeating the event-wake.
        if st in ("done", "failed", "waiting_jobs"):
            continue
        if st == "waiting_reset":
            if now >= (j.get("reset_epoch") or 0):
                # Task 274: if a finished job is HELD by the limit for this worker,
                # jobmgr owns the relaunch (it carries the job handover) — defer to
                # it so we don't double-relaunch (bounded by LIMIT_HANDOVER_GRACE).
                if should_defer_reset_relaunch(j, now):
                    continue
                kindlabel = j.get("limit_kind", "usage")
                relaunch(j); changed = True
                email(f"🔄 watchdog: relaunched '{j['name']}' after the {kindlabel} limit reset",
                      f"The {kindlabel} limit reset, so I relaunched worker '{j['name']}' "
                      f"(resume {j['session'][:8]}…, attempt {j['relaunched']}). It continues "
                      f"with full context.", jobs)
            continue
        # st == running
        state, reset, rc = classify(j)
        if state == "running":
            if j.pop("_dead_ticks", None):
                changed = True       # came back (e.g. relaunched by the inbox loop)
            # Task 176: survival resets the attempt counter. First liveness
            # check that finds a relaunched worker with a live NON-ZOMBIE
            # claude >= SURVIVE_RESET_SEC after its relaunch clears the count
            # (limit itself stays MAX_RELAUNCH). last_relaunch_ts is stamped by
            # relaunch() here AND by scratch_interrupt_worker.sh, so both
            # relaunch paths arm the same clock; jobs predating the field
            # (no last_relaunch_ts) simply never reset — sane default.
            if (j.get("relaunched", 0) > 0
                    and (j.get("last_relaunch_ts") or 0) > 0
                    and now - j["last_relaunch_ts"] >= SURVIVE_RESET_SEC
                    and claude_alive_nonzombie(j)):
                log(f"{j['name']} survived {int(now - j['last_relaunch_ts'])}s "
                    f"since relaunch -> attempt counter reset "
                    f"({j['relaunched']} -> 0)")
                j["relaunched"] = 0
                j.pop("_rapid_deaths", None)   # fresh episode accounting too
                changed = True
            if heartbeat(j, jobs, now):        # F5: alive-but-silent tracking
                changed = True
            continue
        if state == "no_marker":
            j["_dead_ticks"] = j.get("_dead_ticks", 0) + 1; changed = True
            if j["_dead_ticks"] < DEAD_TICKS:
                continue             # give the marker/relaunch a few polls to appear
            state = "crashed"        # persistent silent death (tmux killed, no mail)
        j.pop("_dead_ticks", None)
        if state == "done":
            j["state"] = "done"; j.pop("_rapid_deaths", None); changed = True
            log(f"{j['name']} completed")
            # F3: every LIVE running->done transition is audible (jobs already
            # done at load time never reach here — tick() skips them above), so
            # a future mis-classified "completed" surfaces in minutes.
            sent = "yes" if Path(j.get("done_sentinel", "/nonexistent")).exists() else "no"
            tail = last_run_tail(j["log"])[-400:]
            email(f"✅ watchdog: '{j['name']}' completed (sentinel: {sent})",
                  f"Worker '{j['name']}' finished{approx_runtime(j['log'])} — observed live "
                  f"running->done; done-sentinel present: {sent}. Log tail:\n...{tail}", jobs)
            continue
        if state == "limit_killed":
            # Task 274: capture WHICH limit (session/weekly/…) + the reset string,
            # publish the account-global marker, and hand this worker's
            # self-monitored jobs to jobmgr — all before the informative email.
            notice = None
            try:
                notice = parse_limit_notice(last_run_tail(j["log"]))
            except Exception as e:
                log(f"parse_limit_notice error for {j['name']}: {e}")
            kind = notice[0] if notice else "usage"
            reset_str = notice[2] if notice else ""
            j["state"] = "waiting_reset"; j["reset_epoch"] = reset
            j["limit_kind"] = kind; j["limit_reset_str"] = reset_str
            j["limit_hit_ts"] = now; changed = True
            write_limit_state(kind, reset, reset_str, j["name"])
            # Task 274: a weekly reset should be days out — flag a suspiciously-soon
            # one (likely a mis-parse) so a bad reset can't drive a relaunch storm.
            if kind == "weekly" and reset and reset - now < 12 * 3600:
                log(f"WARN {j['name']}: weekly limit but reset is only "
                    f"{int((reset-now)/60)} min out ('{reset_str}') — verify the parse")
            adopt_summary = adopt_self_monitored(j["name"])
            rt = datetime.fromtimestamp(reset).strftime("%a %H:%M") if reset else "?"
            kindlabel = {"weekly": "weekly", "session": "5-hour session",
                         "5-hour": "5-hour session", "daily": "daily",
                         "usage": "usage"}.get(kind, kind)
            log(f"{j['name']} killed by {kind} limit; reset ~{rt}; adopt: {adopt_summary}")
            email(f"⚠️ watchdog: '{j['name']}' hit the {kindlabel} limit — auto-relaunch at reset",
                  f"Worker '{j['name']}' hit the {kindlabel} limit at "
                  f"{datetime.fromtimestamp(now):%H:%M} and its claude process exited "
                  f"(not paused). Reset: {reset_str or ('~' + rt)} — I relaunch it "
                  f"automatically at ~{rt} (resume {j['session'][:8]}…, full context kept).\n\n"
                  f"Its CPU/GPU jobs keep running (setsid-detached — unaffected by the kill). "
                  f"Self-monitored jobs handed to jobmgr: {adopt_summary}. If any finish "
                  f"before the reset, jobmgr emails you the job + output path and holds the "
                  f"handover until relaunch.\n\n"
                  f"(Account-global limit: other headless workers hit the same wall and are "
                  f"handled the same way.)", jobs)
            continue
        if state == "interrupted":
            # tmux gone + unread mail = interrupt flow; relaunch (rate-limited)
            # drains the mailbox and sends the mechanical ack. Quiet: no email
            # here, the relaunch script acks the user itself.
            if j.get("relaunched", 0) >= MAX_RELAUNCH:
                j["state"] = "failed"; changed = True
                log(f"{j['name']} interrupted but exceeded {MAX_RELAUNCH} relaunches")
                email(f"❌ watchdog: '{j['name']}' has unread mail but hit the relaunch cap",
                      f"Worker '{j['name']}' has unread mail in its mailbox but was already "
                      f"relaunched {MAX_RELAUNCH}×; giving up. It needs a manual look.", jobs)
            elif relaunch_allowed(j):
                log(f"{j['name']} tmux gone with unread mail -> relaunching to drain mailbox")
                relaunch(j); changed = True
            continue
        if state == "exited_incomplete":
            # Task 156 F1: rc=0 but no done-sentinel — an armed-waiter death,
            # not completion. Resume it (the relaunch script's no-mail branch
            # already injects the snapshots + a "you were relaunched" note,
            # exactly what a re-woken foreground poller needs), spaced by
            # exponential backoff instead of the flat gap.
            if j.get("relaunched", 0) < MAX_RELAUNCH:
                if not backoff_allowed(j):
                    continue         # inside the backoff window; retry next poll
                k = j.get("relaunched", 0) + 1
                log(f"{j['name']} exited rc=0 without done-sentinel; relaunching "
                    f"({k}/{MAX_RELAUNCH})")
                email(f"⚠️ watchdog: '{j['name']}' exited rc=0 WITHOUT finishing — relaunching",
                      f"Worker '{j['name']}' exited cleanly (rc=0) but never touched its "
                      f"done-sentinel, so its task is NOT finished — most likely it ended its "
                      f"turn to 'wait' (nothing re-invokes a headless worker). Resuming it now "
                      f"(attempt {k}/{MAX_RELAUNCH}; the gap doubles each attempt: 2,4,8,16,32 "
                      f"min).", jobs)
                relaunch(j); changed = True
            else:
                j["state"] = "failed"; changed = True
                log(f"{j['name']} failed after {MAX_RELAUNCH} relaunches")
                email(f"❌ watchdog: '{j['name']}' failed after {MAX_RELAUNCH} relaunches",
                      f"Worker '{j['name']}' keeps exiting incomplete; giving up after "
                      f"{MAX_RELAUNCH} attempts. It needs a manual look.", jobs)
            continue
        if state == "crashed":
            if j.get("relaunched", 0) < MAX_RELAUNCH:
                if not relaunch_allowed(j):
                    continue         # back off; retry next poll
                # Task 170 F4: informative crash email — rc + signal decode +
                # log tail + "automatic recovery" framing (one tick, one email).
                # Task 170 F6: if the worker died again within RELAUNCH_MIN_GAP*2
                # of our own relaunch (rapid re-death episode, cf_scatter Jul 5
                # pattern), send ONE digest for the episode instead of an alarm
                # per death; further rapid deaths only log.
                k = j.get("relaunched", 0) + 1
                decode = rc_decode(rc)
                since = int(now - (j.get("last_relaunch_ts") or 0))
                rapid = since < RELAUNCH_MIN_GAP * 2
                if rapid:
                    j["_rapid_deaths"] = j.get("_rapid_deaths", 0) + 1
                else:
                    j.pop("_rapid_deaths", None)
                tail = last_run_tail(j["log"])[-700:]
                deaths = ROOT / "scratch_full_logs" / f"worker_{j['name']}_deaths.log"
                forensics = (f"\n\nF5 forensics for this death: {deaths.name} (and any "
                             f"scratch_full_logs/strace_{j['name']}_*.log)."
                             if deaths.exists() else "")
                log(f"{j['name']} crashed ({decode}); relaunching (attempt {k}/{MAX_RELAUNCH}"
                    + (f", rapid re-death #{j['_rapid_deaths']} after {since}s" if rapid else "") + ")")
                if not rapid:
                    email(f"⚠️ watchdog: '{j['name']}' exited incomplete ({decode.split(' = ')[0]}) "
                          f"— auto-relaunching ({k}/{MAX_RELAUNCH})",
                          f"Worker '{j['name']}' exited without completing.\n"
                          f"Exit: {decode}.\n\n"
                          f"This is an automatic recovery — I am relaunching it now with full "
                          f"context (resume {j['session'][:8]}…). No action needed unless it "
                          f"repeats (attempt {k}/{MAX_RELAUNCH}).{forensics}\n\n"
                          f"Log tail of the run that died:\n...{tail}", jobs)
                elif j["_rapid_deaths"] == 1:
                    email(f"⚠️ watchdog: '{j['name']}' died again {since}s after relaunch — "
                          f"coalescing further alarms",
                          f"Worker '{j['name']}' was relaunched and died again within {since}s "
                          f"(exit: {decode}) — a rapid re-death episode. I keep relaunching "
                          f"automatically up to the cap (this is attempt {k}/{MAX_RELAUNCH}) but "
                          f"will NOT email for further rapid deaths in this episode — one digest "
                          f"per episode; you will still get the ❌ email if it hits the cap."
                          f"{forensics}\n\n"
                          f"Log tail of the run that died:\n...{tail}", jobs)
                # _rapid_deaths > 1: log-only (digest already sent for this episode)
                relaunch(j); changed = True
            else:
                j["state"] = "failed"; j.pop("_rapid_deaths", None); changed = True
                log(f"{j['name']} failed after {MAX_RELAUNCH} relaunches")
                email(f"❌ watchdog: '{j['name']}' failed after {MAX_RELAUNCH} relaunches",
                      f"Worker '{j['name']}' keeps exiting incomplete; giving up after "
                      f"{MAX_RELAUNCH} attempts (last exit: {rc_decode(rc)}). It needs a "
                      f"manual look.", jobs)
            continue
    try:
        if dark_guard(jobs, now):    # F3: generic dead-no-relaunch alarm
            changed = True
    except Exception as e:
        log(f"dark_guard error: {e}")
    if changed:
        save(jobs)
    try:
        reply_guard(jobs)
    except Exception as e:
        log(f"reply_guard error: {e}")
    try:
        stale_infra_guard(jobs)   # Task 165 F2: stale-inode alarm (email only)
    except Exception as e:
        log(f"stale_infra_guard error: {e}")


if __name__ == "__main__":
    if "--once" in sys.argv:
        tick()
        sys.exit(0)
    log(f"loop started (poll {POLL}s)")
    while True:
        try:
            tick()
        except Exception as e:
            log(f"error: {e}")
        time.sleep(POLL)
