"""
Task 353 / Case 384e — JTF (Joint Task Force) read-helpers for the dashboard's
assignment page (renamed from "Cowork" in Phase E).

The agent index powers the /jtf search popup: find an agent by NAME or by a TASK it has
done (for a *specific-deputy* slot). It is read-only, built by joining
scratch_agents_registry.json (worker -> keywords/desc) with the task<->agent signals the
dashboard already computes (lineage titles + state.email_task_agents +
state.worker_prompt_task_map). The precinct options a slot can also take come straight
from state.precincts(); they are not resolved here.

The JTF REQUEST writer is NOT here: the dashboard's authed POST /api/jtf (server.py)
validates a submission and drops a record the inbox-side bridge (tsomp scratch_jtf.py)
materializes into spawned/assigned deputies.
"""
import os

import config
import lineage
import state


def _agent_tasks():
    """agent -> [{id,title}, ...] (newest first) for every task we can attribute to it.

    Union of two signals the dashboard already resolves: the outbound-email "Task N"
    vote (state.email_task_agents, the reliable signal for persistent workers) and the
    worker-prompt map (state.worker_prompt_task_map). Titles come from the lineage
    nodes."""
    titles = {}
    try:
        for n in lineage.build_lineage()["nodes"]:
            titles[n["task_id"]] = n.get("title") or f"Task {n['task_id']}"
    except Exception:
        pass
    by_agent = {}  # agent -> set(task_id)
    try:
        for tid, ag in state.email_task_agents().items():
            if ag:
                by_agent.setdefault(ag, set()).add(tid)
    except Exception:
        pass
    try:
        for tid, cands in state.worker_prompt_task_map().items():
            for ag, _mt in (cands or []):
                by_agent.setdefault(ag, set()).add(tid)
    except Exception:
        pass
    out = {}
    for ag, tids in by_agent.items():
        out[ag] = [{"id": t, "title": titles.get(t, f"Task {t}")}
                   for t in sorted(tids, reverse=True)]
    return out


def _last_seen(agent):
    """Best-effort last-activity epoch for an agent: the mtime of its worker prompt
    file (rewritten on every spawn/relaunch). None if it has none."""
    try:
        return os.path.getmtime(config.SCRATCH / f"worker_{agent}_prompt.md")
    except Exception:
        return None


def agent_index(q="", limit=40):
    """Agents matching `q` (by name, keyword, task number, or task title), newest-seen
    first. Empty `q` -> the most recently active agents. Read-only.

    Returns [{agent, tasks:[{id,title}], n_tasks, last_seen, blurb, keywords}]."""
    reg = state.registry()
    tasks = _agent_tasks()
    names = set(reg) | set(tasks)
    ql = (q or "").strip().lower()
    rows = []
    for name in names:
        r = reg.get(name, {}) if isinstance(reg.get(name), dict) else {}
        kw = r.get("match", []) or []
        desc = r.get("desc", "") or ""
        tks = tasks.get(name, [])
        if ql:
            hay = " ".join([
                name.lower(), desc.lower(),
                " ".join(str(k).lower() for k in kw),
                " ".join(f'{t["id"]} {t["title"]}'.lower() for t in tks),
            ])
            num_hit = ql.isdigit() and any(str(t["id"]) == ql for t in tks)
            if ql not in hay and not num_hit:
                continue
        blurb = (tks[0]["title"] if tks else desc)[:120]
        rows.append({
            "agent": name,
            "tasks": tks[:8],
            "n_tasks": len(tks),
            "last_seen": _last_seen(name),
            "blurb": blurb,
            "keywords": list(kw),
        })
    rows.sort(key=lambda r: (-(r["last_seen"] or 0), -r["n_tasks"], r["agent"]))
    return rows[:limit]
