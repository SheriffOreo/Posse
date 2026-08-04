#!/usr/bin/env python3
"""Task 323 D1: persist the parent-task lineage edge for a newly-created task spec.

A *case number* (a.k.a. task number) identifies one unit of work; its spec lives
at `scratch_full_logs/inbox/task_<case>.md`. Case 391a: the case number is now
ALLOCATED from the single continuous sequence (scratch_case_seq.py) — the inbox
handler passes it here as `--uid <case>`; the Gmail uid is NOT the case number any
more (it is only a routing/dedup key). This helper is number-agnostic: it stamps
whatever number it is given. The parent link (which prior case this one follows up
on) is known mechanically at routing/triage time but was never written down, so the
dashboard had to guess. This helper stamps a machine-readable

    parent_task: <N>        (or  parent_task: none  when independent)

at the TOP of the spec, computed reliably in this priority order:

  1. the task number in the reply subject "Re: Task <N> ..."  (the reply IS a
     follow-up of N — strongest signal);
  2. else the matched worker's most-recent prior task number (a resumed worker
     continuing its own thread), tracked in inbox/worker_last_task.json;
  3. else `none` (a fresh, independent request).

The dashboard's lineage.py already consumes `parent_task:` as its highest-
confidence edge, so once stamped a task links with no dashboard change. This is
owned by the inbox handler (the reliable source of truth) rather than the
freeform triage LLM; the LLM instruction is only a belt-and-suspenders.

Usage (called by scratch_inbox_handle.sh after a spec is created):
    python scratch_task_parent.py stamp --uid 320 --agent paper \
        --subject "Re: Task 319 FINAL: ..." --spec .../inbox/task_320.md [--dry]

`--dry` computes and prints the parent WITHOUT editing the spec or updating the
last-task map (used by the isolation tests).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX_DIR = ROOT / "scratch_full_logs" / "inbox"
LAST_TASK = INBOX_DIR / "worker_last_task.json"       # {agent: last_task_uid}
LAST_TASK_LOCK = INBOX_DIR / "worker_last_task.lock"

# a parent_task field already present in the spec (mirror of lineage._PARENT_FIELD)
_HAS_PARENT = re.compile(r"^\s*parent[_ ]?task\s*[:=]", re.I | re.M)
# Task 372: a precinct field already present in the spec.
_HAS_PRECINCT = re.compile(r"^\s*precinct\s*[:=]", re.I | re.M)
# "Task 319" / "Case 319" / "task_319" / "Case #319" anywhere in a subject line.
# Case 402: "Case N" must match too — all reply subjects now read "Case N" (the
# Task->Case rename), so a Task-only pattern missed every real parent and fell back
# to the worker's last task (e.g. case-400's follow-up computed parent=393, not 400).
_SUBJECT_TASK = re.compile(r"\b(?:task|case)[ _]?#?(\d+)", re.I)


def _subject_parent(subject, uid):
    """The task number named in a reply subject 'Re: Task N ...', or None. Never
    returns the child's own uid (a reply names its PARENT, but guard anyway)."""
    if not subject:
        return None
    m = _SUBJECT_TASK.search(subject)
    if not m:
        return None
    n = int(m.group(1))
    return n if n != uid else None


def _load_last_task():
    try:
        d = json.loads(LAST_TASK.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def compute_parent(uid, agent, subject, last_map):
    """(parent, basis) where parent is an int or None. Priority: reply-subject ->
    worker's most-recent prior task -> None. `last_map` is the current
    {agent: last_uid} (the value BEFORE recording this uid)."""
    p = _subject_parent(subject, uid)
    if p is not None:
        return p, "reply-subject"
    prev = last_map.get(agent)
    try:
        prev = int(prev)
    except (TypeError, ValueError):
        prev = None
    if prev is not None and prev != uid:
        return prev, "worker-last-task"
    return None, "none"


def _stamp_spec(spec, parent):
    """Prepend `parent_task: <parent|none>` to the spec (atomic). No-op if a
    parent_task line already exists. Returns True if it wrote the line."""
    text = spec.read_text(errors="replace")
    if _HAS_PARENT.search(text):
        return False
    line = f"parent_task: {parent if parent is not None else 'none'}\n"
    tmp = spec.with_suffix(spec.suffix + ".ptmp")
    tmp.write_text(line + text)
    os.replace(tmp, spec)
    return True


def _stamp_precinct_spec(spec, precinct):
    """Task 372: prepend `precinct: <name>` to the spec (atomic). No-op if a
    precinct line already exists. Returns True if it wrote the line."""
    text = spec.read_text(errors="replace")
    if _HAS_PRECINCT.search(text):
        return False
    line = f"precinct: {precinct}\n"
    tmp = spec.with_suffix(spec.suffix + ".pctmp")
    tmp.write_text(line + text)
    os.replace(tmp, spec)
    return True


def _record_precinct_map(uid, precinct, agent, basis):
    """Task 372: persist task->precinct in records/task_precinct.json (via the
    precinct module). Best-effort: never breaks the parent-stamp path.

    Task 377 #1 (deputy authority): the triage lineage (`agent`, the one-shot
    handler / resumed session that routed this email) is recorded as the LESSER
    ``handler`` field — NOT ``agent`` — so it can never clobber the spawn's real
    ``deputy``/``agent`` attribution. Before this fix the handler stamp ran AFTER
    the spawn stamp and overwrote agent=<real deputy> with agent=<handler>, which
    is exactly why the dashboard showed the triage lineage instead of the working
    deputy (Task 376 showed "precincts" for deputy "precinct_refine")."""
    try:
        import scratch_precinct as pc
        pc.stamp_map(uid, precinct=precinct, handler=agent or None,
                     basis=basis or "handler")
    except Exception as ex:
        print(f"warn: could not stamp task_precinct[{uid}]={precinct}: {ex}",
              file=sys.stderr)


def _record_last_task(agent, uid):
    """Set worker_last_task[agent] = uid under an flock (the map is shared across
    per-agent handlers). Best-effort: a failure here never breaks stamping."""
    import fcntl
    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        with open(LAST_TASK_LOCK, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                d = json.loads(LAST_TASK.read_text())
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
            d[agent] = uid
            tmp = LAST_TASK.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2))
            os.replace(tmp, LAST_TASK)
            fcntl.flock(lk, fcntl.LOCK_UN)
    except Exception as ex:
        print(f"warn: could not record worker_last_task[{agent}]={uid}: {ex}",
              file=sys.stderr)


def cmd_stamp(a):
    uid = int(a.uid)
    agent = a.agent or "unknown"
    spec = Path(a.spec)
    if not spec.exists():
        print(f"no-spec: {spec} (nothing to stamp)")
        return 0
    last_map = _load_last_task()
    parent, basis = compute_parent(uid, agent, a.subject or "", last_map)
    precinct = getattr(a, "precinct", None) or None
    if a.dry:
        print(f"DRY parent_task={parent if parent is not None else 'none'} "
              f"basis={basis} precinct={precinct or 'none'} (uid={uid} agent={agent})")
        return 0
    wrote = _stamp_spec(spec, parent)
    # Task 372: also stamp the precinct onto the spec + the persisted map.
    if precinct:
        _stamp_precinct_spec(spec, precinct)
        _record_precinct_map(uid, precinct, agent, "handler")
    _record_last_task(agent, uid)     # track for the NEXT task's priority-2
    pv = parent if parent is not None else "none"
    if wrote:
        print(f"stamped parent_task={pv} basis={basis} -> {spec.name}")
    else:
        print(f"kept existing parent_task line in {spec.name} "
              f"(would-be parent={pv} basis={basis}); recorded last_task[{agent}]={uid}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stamp")
    st.add_argument("--uid", required=True)
    st.add_argument("--agent", default="unknown")
    st.add_argument("--subject", default="")
    st.add_argument("--spec", required=True)
    st.add_argument("--precinct", default=None,
                    help="Task 372: also stamp this precinct onto the spec + the map")
    st.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.cmd == "stamp":
        sys.exit(cmd_stamp(a))
