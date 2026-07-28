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
        rows.append(
            {
                "name": name,
                "state": st,
                "state_label": _STATE_LABEL.get(st, st or "?"),
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
