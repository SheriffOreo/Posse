"""
Read-only readers over the live tsomp state. Nothing here writes or mutates.

Every reader is defensive: a missing or malformed file yields an empty/blank
result rather than crashing the dashboard.
"""
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

import config

# --------------------------------------------------------------------------- #
# low-level helpers
# --------------------------------------------------------------------------- #
def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _read_jsons(dirp):
    out = []
    try:
        files = sorted(glob.glob(str(Path(dirp) / "*.json")))
    except Exception:
        return out
    for f in files:
        d = _read_json(f)
        if isinstance(d, dict):
            d.setdefault("_file", os.path.basename(f))
            out.append(d)
    return out


_MS = re.compile(r"[A-Za-z]?(\d{13})(?:\D|$)")
_SEC = re.compile(r"[A-Za-z]?(\d{10})(?:\D|$)")
# Task 327: "Task N" token in an email subject -> the task it belongs to. Kept in
# lockstep with lineage._TASK_TOKEN (agents title emails "Task N START/FINAL/...").
_SUBJ_TASK = re.compile(r"\btask[ _]?#?(\d+)", re.I)


def epoch_from_id(jid):
    """job_id 'j1785256280873_46236e' or gpu id '1785263694569_45e478' -> epoch sec."""
    if not jid:
        return None
    m = _MS.match(str(jid))
    if m:
        return int(m.group(1)) / 1000.0
    m = _SEC.match(str(jid))
    if m:
        return int(m.group(1))
    return None


def day_of(ts):
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:
        return None


# tiny TTL cache (single-user localhost dashboard) --------------------------- #
_cache = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


# --------------------------------------------------------------------------- #
# daemons + registry
# --------------------------------------------------------------------------- #
def daemon_status():
    names = ["watchdog", "jobmgr", "inbox", "gpu_manager"]
    alive = set()
    try:
        out = subprocess.run(
            ["tmux", "ls"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            alive.add(line.split(":", 1)[0].strip())
    except Exception:
        pass
    return [{"name": n, "alive": n in alive} for n in names]


def registry():
    d = _read_json(config.REGISTRY, {}) or {}
    w = d.get("workers", {}) if isinstance(d, dict) else {}
    return w if isinstance(w, dict) else {}


# --------------------------------------------------------------------------- #
# workers (from the watchdog roster)
# --------------------------------------------------------------------------- #
_STATE_LABEL = {
    "running": "running",
    "waiting_jobs": "parked (awaiting job)",
    "waiting_reset": "parked (usage-limit reset)",
    "waiting": "waiting",
    "done": "done",
    "failed": "failed",
}


def _worker_prompt_task(name):
    """Best full task text for a worker: the TASK section of its prompt file."""
    f = config.SCRATCH / f"worker_{name}_prompt.md"
    try:
        txt = f.read_text()
    except Exception:
        return ""
    m = re.search(r"={5,}\s*TASK\s*={5,}(.*)", txt, re.S)
    body = m.group(1) if m else txt
    return body.strip()


def _worker_task_info(name):
    """Task 327: (task_id|None, title) a worker is CURRENTLY on, from its prompt's
    TASK section — the number via _primary_task, the title from the first heading
    (its `# Task N — ...` line). Used to show task# + description on the Status page."""
    txt = _worker_prompt_task(name)
    if not txt:
        return None, ""
    tid = _primary_task(txt)
    title = ""
    for ln in txt.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            title = s.lstrip("# ").strip()
            break
    return tid, title


def _mailbox_pending(name):
    f = config.INBOX / f"mailbox_{name}.md"
    try:
        return f.stat().st_size > 0
    except Exception:
        return False


def workers(active_only=False):
    wd = _read_json(config.WATCHDOG_JOBS, []) or []
    reg = registry()
    rows = []
    for e in wd:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        st = e.get("state")
        if active_only and st in ("done", "failed"):
            continue
        rinfo = reg.get(name, {})
        tid, ttitle = _worker_task_info(name)
        rows.append(
            {
                "name": name,
                "state": st,
                "state_label": _STATE_LABEL.get(st, st or "?"),
                # Task 327: the task this worker is currently on (number + title),
                # resolved from its prompt's TASK section for the Status page.
                "task": tid,
                "task_title": ttitle,
                "requester": e.get("requester"),
                "desc": rinfo.get("desc") or "",
                "keywords": rinfo.get("match", []),
                "session": e.get("session"),
                "relaunched": e.get("relaunched", 0),
                "reset_epoch": e.get("reset_epoch"),
                "last_progress_ts": e.get("last_progress_ts"),
                "mailbox_pending": _mailbox_pending(name),
            }
        )
    rows.sort(
        key=lambda r: (
            r["state"] in ("done", "failed"),
            -(r.get("last_progress_ts") or 0),
        )
    )
    return rows


def worker_detail(name):
    for w in workers():
        if w["name"] == name:
            w = dict(w)
            w["task_text"] = _worker_prompt_task(name)
            return w
    return None


# --------------------------------------------------------------------------- #
# jobmgr jobs
# --------------------------------------------------------------------------- #
def _norm_job(j, source):
    jid = j.get("job_id") or j.get("id") or j.get("_file", "").replace(".json", "")
    launch = epoch_from_id(jid) or j.get("submit_ts") or j.get("start_ts")
    finish = j.get("finish_ts") or j.get("finished_at")
    if isinstance(finish, str):
        finish = None  # keep ISO strings out of numeric math; launch drives grouping
    return {
        "source": source,
        "job_id": jid,
        "owner_agent": j.get("owner_agent") or j.get("owner") or "?",
        "type": j.get("type") or ("gpu" if source == "gpu" else "cpu"),
        "status": j.get("status") or ("done" if source == "gpu" else "?"),
        "command": j.get("command") or j.get("cmd") or "",
        "est_seconds": j.get("est_seconds"),
        "duration_s": j.get("duration_s"),
        "rc": j.get("rc", j.get("exit_code")),
        "timed_out": j.get("timed_out", False),
        "launch_ts": launch,
        "finish_ts": finish,
        "output_path": j.get("output_path"),
        "out_log": j.get("out_log"),
        "err_log": j.get("err_log"),
        "gpu_job_id": j.get("gpu_job_id"),
        "cwd": j.get("cwd"),
        "day": day_of(launch),
    }


def jobmgr_jobs(active_only=True):
    buckets = ["pending", "running", "wakes", "sleeping"] if active_only else config.JOB_BUCKETS
    out = []
    for b in buckets:
        for j in _read_jsons(config.JOBS / b):
            nj = _norm_job(j, "jobmgr")
            nj["bucket"] = b
            out.append(nj)
    return out


def gpu_jobs_active():
    out = []
    for b in ("pending", "running"):
        for j in _read_jsons(config.GPU_QUEUE / b):
            nj = _norm_job(j, "gpu")
            nj["bucket"] = b
            nj["status"] = b
            out.append(nj)
    return out


def gpu_manager_alive():
    return any(d["name"] == "gpu_manager" and d["alive"] for d in daemon_status())


# --------------------------------------------------------------------------- #
# usage-limit state (Task 274)
# --------------------------------------------------------------------------- #
def limit_state():
    d = _read_json(config.LIMIT_STATE)
    if not isinstance(d, dict) or not d.get("active"):
        return None
    return d


# --------------------------------------------------------------------------- #
# job history index (all finished jobmgr + gpu jobs), cached
# --------------------------------------------------------------------------- #
def _build_history():
    items = []
    for j in _read_jsons(config.JOBS / "done"):
        items.append(_norm_job(j, "jobmgr"))
    for j in _read_jsons(config.GPU_QUEUE / "done"):
        items.append(_norm_job(j, "gpu"))
    return items


def history_index():
    # 60s TTL: 2300+ small json reads shouldn't happen on every click.
    return _cached("history", 60, _build_history)


def history_day_counts():
    counts = {}
    for it in history_index():
        d = it.get("day")
        if d:
            counts[d] = counts.get(d, 0) + 1
    # task launches also count as "activity" days
    for t in task_launches():
        d = t.get("day")
        if d:
            counts[d] = counts.get(d, 0) + 1
    return counts


def history_for_day(day):
    jobs = [it for it in history_index() if it.get("day") == day]
    jobs.sort(key=lambda r: -(r.get("launch_ts") or 0))
    tasks = [t for t in task_launches() if t.get("day") == day]
    tasks.sort(key=lambda r: -(r.get("ts") or 0))
    return {"day": day, "jobs": jobs, "tasks": tasks}


def job_detail(job_id):
    for it in history_index() + jobmgr_jobs(active_only=False) + gpu_jobs_active():
        if it.get("job_id") == job_id:
            return it
    return None


# --------------------------------------------------------------------------- #
# task -> agent resolution (for deliverables + the conversation header)
# --------------------------------------------------------------------------- #
# Most task_<N>.md specs carry the concrete worker name only as a `--agent <name>`
# placeholder, so lineage's spec-scrape often yields nothing. The reliable signal
# is the spawned worker's own prompt file, worker_<name>_prompt.md, whose TASK
# section starts with `# Task <N> — ...`. We map that back to (task -> agent).
def _primary_task(txt):
    """The task number a worker prompt was spawned for — taken from its TASK
    section heading, so we don't pick up parent/reference task numbers cited in
    the shared preamble."""
    parts = re.split(r"={5,}\s*TASK\s*={5,}", txt, 1)
    body = parts[1] if len(parts) > 1 else txt
    for pat in (r"(?m)^\s*#+\s*Task\s+(\d{2,4})\b",
                r"Task\s+(\d{2,4})\s*[—:-]",
                r"\btask_(\d{2,4})\b"):
        m = re.search(pat, body)
        if m:
            return int(m.group(1))
    return None


def _build_wpmap():
    m = {}
    for f in glob.glob(str(config.SCRATCH / "worker_*_prompt.md")):
        nm = re.match(r"worker_(.+)_prompt\.md$", os.path.basename(f))
        if not nm:
            continue
        try:
            tid = _primary_task(Path(f).read_text(errors="replace"))
            mt = os.path.getmtime(f)
        except Exception:
            continue
        if tid:
            m.setdefault(tid, []).append((nm.group(1), mt))
    return m


def worker_prompt_task_map():
    """task_id -> [(agent, prompt_mtime), ...] from worker prompt files."""
    return _cached("wpmap", 120, _build_wpmap)


def _job_owner_counts():
    def build():
        c = {}
        for it in history_index():
            a = it.get("owner_agent")
            if a and a != "?":
                c[a] = c.get(a, 0) + 1
        return c
    return _cached("job_owner_counts", 120, build)


def _build_email_task_agents():
    """task_id -> agent, learned from outbound email subjects' "Task N" token
    (majority vote per task). Task 327: this is the reliable task->worker signal
    for a PERSISTENT worker (e.g. `paper`), whose per-task prompt file only ever
    reflects its CURRENT task, so worker_prompt_task_map() misses its past ones."""
    votes = {}
    try:
        lines = Path(config.SENT_EMAILS).read_text(errors="replace").splitlines()
    except Exception:
        return {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        ag = r.get("agent")
        if not ag:
            continue
        m = _SUBJ_TASK.search(r.get("subject", "") or "")
        if m:
            c = votes.setdefault(int(m.group(1)), {})
            c[ag] = c.get(ag, 0) + 1
    return {tid: max(c.items(), key=lambda kv: kv[1])[0] for tid, c in votes.items()}


def email_task_agents():
    return _cached("email_task_agents", 120, _build_email_task_agents)


def agent_for_task(task_id, spec_agent=None):
    """Resolve the worker agent for a task: the spec's own --agent if present,
    else the worker-prompt fallback (preferring, among candidates, the agent that
    owns the most finished jobs so the deliverables lookup is meaningful), else
    (Task 327) the agent that emailed "Task N ..." — the only signal that survives
    for a persistent worker whose prompt file no longer names this task."""
    if spec_agent:
        return spec_agent
    cands = worker_prompt_task_map().get(task_id) or []
    if cands:
        owners = _job_owner_counts()
        cands = sorted(cands, key=lambda c: (-(owners.get(c[0], 0)), -c[1]))
        return cands[0][0]
    return email_task_agents().get(task_id)


# --------------------------------------------------------------------------- #
# per-task deliverables (best-effort) — for the task detail panel
# --------------------------------------------------------------------------- #
def _reports_for(task_id, agent, cap=25):
    """reports/ files whose name references this task id (precise) or the agent
    name (looser). Bounded to the top 2 levels + `cap` hits; every path is passed
    through config.download_allowed so we never surface a link that would 403."""
    root = config.STATE_ROOT / "reports"
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    tid = str(task_id)
    tokens = [f"task_{tid}", f"task-{tid}", f"task{tid}", f"_{tid}_"]
    ag = (agent or "").lower()
    out, seen = [], set()
    for pat in (str(root / "*"), str(root / "*" / "*")):
        for f in glob.glob(pat):
            if len(out) >= cap:
                break
            if f in seen or not os.path.isfile(f):
                continue
            name = os.path.basename(f).lower()
            hit = any(t in name for t in tokens) or (len(ag) >= 4 and ag in name)
            if hit and config.download_allowed(f):
                seen.add(f)
                out.append({"name": os.path.basename(f), "path": f})
    out.sort(key=lambda r: r["name"])
    return out


def _emailed_attachments(agent, task_id=None):
    """Task 323 D3: files this task's agent actually emailed, from the persisted
    `attachments` in sent_emails.jsonl — the AUTHORITATIVE deliverables (what the
    worker really sent the user). Filtered to what the guarded /download endpoint
    will serve, deduped by resolved path. Empty for agents that only sent emails
    before the attachment-logging change (those rows carry no `attachments`).

    Task 327: when `task_id` is given, only attachments from emails whose subject
    carries the matching "Task N" token count — so a persistent worker (e.g.
    `paper`, which titles every email "Task N ...") attributes each FINAL png to
    the right task instead of dumping all of its emailed files under every task."""
    if not agent:
        return []
    out, seen = [], set()
    try:
        lines = Path(config.SENT_EMAILS).read_text(errors="replace").splitlines()
    except Exception:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("agent") != agent:
            continue
        if task_id is not None:
            m = _SUBJ_TASK.search(r.get("subject", "") or "")
            if not (m and int(m.group(1)) == task_id):
                continue
        for p in (r.get("attachments") or []):
            rp = config.resolve_download(p)
            if rp is None:
                continue
            key = str(rp)
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": rp.name, "path": key, "emailed": True})
    return out


def deliverables_for(task_id, agent):
    """Best-effort deliverables for a task: the files the task's agent actually
    EMAILED (authoritative, Task 323 D3), plus jobmgr/gpu jobs it owns (their
    output / stdout / stderr) and reports/ files that reference the task id or
    agent. Only paths that config.download_allowed() accepts are shown, so every
    link resolves through the guarded /download endpoint."""
    agent = agent_for_task(task_id, agent)
    jobs, seen = [], set()
    if agent:
        for it in history_index() + jobmgr_jobs(active_only=False) + gpu_jobs_active():
            if it.get("owner_agent") != agent:
                continue
            jid = it.get("job_id")
            if jid in seen:
                continue
            seen.add(jid)
            paths = []
            for label, key in (("output", "output_path"),
                               ("stdout", "out_log"), ("stderr", "err_log")):
                p = it.get(key)
                if p and config.download_allowed(p):
                    paths.append({"label": label, "path": p})
            if paths:  # a job with no downloadable artifact isn't a deliverable
                jobs.append({"job_id": jid, "status": it.get("status"),
                             "type": it.get("type"), "day": it.get("day"),
                             "paths": paths})
    jobs.sort(key=lambda j: -(epoch_from_id(j["job_id"]) or 0))
    # Emailed attachments first (authoritative), then reports/ matches not already
    # covered by an emailed file (deduped by realpath). Task 327: scope the emailed
    # files to THIS task via the "Task N" subject token, not all of the agent's.
    emailed = _emailed_attachments(agent, task_id)
    seen_files = {os.path.realpath(f["path"]) for f in emailed}
    reports = [f for f in _reports_for(task_id, agent)
               if os.path.realpath(f["path"]) not in seen_files]
    return {"jobs": jobs, "files": emailed + reports}


# --------------------------------------------------------------------------- #
# task launches (task_<N>.md specs) — used by calendar + lineage
# --------------------------------------------------------------------------- #
def _task_files():
    try:
        return sorted(glob.glob(str(config.INBOX / "task_*.md")))
    except Exception:
        return []


def task_launches():
    return _cached("tasks", 60, _build_task_launches)


def _build_task_launches():
    out = []
    for f in _task_files():
        base = os.path.basename(f)
        m = re.match(r"task_(\d+)\.md$", base)
        if not m:
            continue  # skip per-model variants (task_<N>_<model>.md) for the top list
        tid = int(m.group(1))
        try:
            mtime = os.path.getmtime(f)
            head = Path(f).read_text(errors="replace")
        except Exception:
            mtime, head = None, ""
        title = ""
        for ln in head.splitlines():
            if ln.strip().startswith("#"):
                title = ln.lstrip("# ").strip()
                break
        out.append(
            {
                "task_id": tid,
                "title": title or f"Task {tid}",
                "ts": mtime,
                "day": day_of(mtime),
                "file": f,
            }
        )
    out.sort(key=lambda r: -(r.get("task_id") or 0))
    return out
