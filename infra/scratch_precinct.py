#!/usr/bin/env python3
"""Task 372: the precinct MATCHING MECHANISM + the persisted task->precinct map.

How the operator specifies a precinct in an email, and how a contact is routed
(precedence -- FIRST match wins):

  1. EXPLICIT TAG (highest priority): a line
         precinct: <name>
     anywhere in the body, or a bracket tag  [<name>]  in the subject. Routed
     straight to that precinct -- but a name that is NOT a registered precinct
     falls through (basis 'explicit-tag-unknown:<name>') so the receptionist can
     CREATE it.
  2. REPLY PARENT: the parent task's precinct, from the persisted
     task_precinct.json map. The parent task number is taken from 'Task N' in the
     reply subject, or from the stamped 'parent_task:' line in this uid's spec.
  3. ELSE None -> the RECEPTIONIST decides the precinct (or just answers).

Also owns the persisted map (records/task_precinct.json):
    { "<task#>": {precinct, agent, session, deputy, handler, basis} }
which the dashboard reads for authoritative precinct membership, and which
precedence #2 above consults for replies. All map mutations are flock-guarded +
atomic (os.replace).

Task 377 #1 (deputy authority): the WORKING DEPUTY is authoritative. ``deputy``
(and ``agent``, which the spawn stamps to the same real deputy) is who did the
work; ``handler`` is the lesser TRIAGE-lineage name (the one-shot handler / the
resumed session that routed the email). The triage stamp writes ``handler`` and
NEVER clobbers the spawn's ``deputy``/``agent`` — so the dashboard always shows
the real deputy, not the triage lineage (the Task 376/377 confusion).

CLI:
    scratch_precinct.py resolve --uid N --subject "..." [--bodyfile f | --body "..."]
        -> prints "<precinct-or-empty>\\t<basis>"
    scratch_precinct.py stamp   --uid N [--precinct P] [--agent A] [--session S]
                                        [--deputy D] [--handler H] [--basis B]
    scratch_precinct.py get     --uid N            # prints the map entry as JSON
"""
import argparse
import fcntl
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECORDS = ROOT / "scratch_full_logs" / "records"
MAP_PATH = RECORDS / "task_precinct.json"
MAP_LOCK = RECORDS / "task_precinct.json.lock"
INBOX = ROOT / "scratch_full_logs" / "inbox"

sys.path.insert(0, str(ROOT))
import scratch_records as rec  # noqa: E402

# 'precinct: <name>' (or 'precinct = <name>'), tolerant of leading '>' quoting.
_TAG_BODY = re.compile(r"^[ \t>]*precinct[ \t]*[:=][ \t]*([A-Za-z0-9_.-]+)", re.I | re.M)
_TAG_SUBJ = re.compile(r"\[([A-Za-z0-9_.-]+)\]")
# Case 402: match "Case N" as well as "Task N". Every reply subject now reads
# "Re: Case N: ..." (the Task->Case rename), so a Task-only pattern silently matched
# NOTHING and the parent precinct was never recovered — the receptionist then re-guessed
# it from content (how case-400's infra follow-up became a 'paper' deputy, case 401).
_SUBJ_TASK = re.compile(r"\b(?:task|case)[ _]?#?(\d+)", re.I)

# Task 376: an email may override the spawned deputy's MODEL with a 'model: <name>'
# line in the body or a '[model:<name>]' tag in the subject. Only a VALID model
# (rec._MODELS) matches, so free text like "model: the standard approach" never
# false-matches, and '[model:opus]' can't be mistaken for a precinct '[name]' tag
# (the ':' is outside the precinct tag's char class).
_MODEL_ALT = "|".join(rec._MODELS)
_TAG_MODEL_BODY = re.compile(rf"^[ \t>]*model[ \t]*[:=][ \t]*({_MODEL_ALT})\b", re.I | re.M)
_TAG_MODEL_SUBJ = re.compile(rf"\[[ \t]*model[ \t]*[:=]?[ \t]*({_MODEL_ALT})[ \t]*\]", re.I)


def registered_precincts():
    return set(rec.directory_read().get("precincts", {}).keys())


def _read_map():
    try:
        d = json.loads(MAP_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get(uid):
    return _read_map().get(str(uid))


def stamp_map(uid, precinct=None, agent=None, session=None, deputy=None,
              handler=None, basis=None):
    """Merge-update task_precinct.json[uid] with the provided fields. LOCK + atomic.
    Only non-empty fields overwrite; the rest are preserved.

    Task 377 #1: ``handler`` is the lesser TRIAGE-lineage name; it is stored in
    its own field and NEVER overwrites ``agent``/``deputy`` (the real working
    deputy). The triage stamp passes ``handler=`` (not ``agent=``) so the spawn's
    deputy attribution survives — see scratch_task_parent._record_precinct_map."""
    RECORDS.mkdir(parents=True, exist_ok=True)
    with open(MAP_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            d = _read_map()
            e = d.get(str(uid), {}) if isinstance(d.get(str(uid)), dict) else {}
            for k, v in (("precinct", precinct), ("agent", agent), ("session", session),
                         ("deputy", deputy), ("handler", handler), ("basis", basis)):
                if v is not None and v != "":
                    e[k] = v
            d[str(uid)] = e
            tmp = MAP_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2))
            os.replace(tmp, MAP_PATH)
            return e
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def remove_map(uid):
    """Delete task_precinct.json[uid] (LOCK + atomic). Returns True if a key was
    removed. Task 377 (Feng uid=380): the sheriff uses this to retract a test/case
    map entry; it is also the primitive a future precinct-delete needs. Mirrors
    stamp_map's flock + atomic-replace discipline."""
    RECORDS.mkdir(parents=True, exist_ok=True)
    with open(MAP_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            d = _read_map()
            if str(uid) not in d:
                return False
            del d[str(uid)]
            tmp = MAP_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2))
            os.replace(tmp, MAP_PATH)
            return True
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _explicit_tag(subject, body, reg):
    """A body 'precinct:' tag (any name), else a subject '[name]' tag that names a
    REGISTERED precinct (so '[URGENT]' etc. never false-match). Returns name|None."""
    m = _TAG_BODY.search(body or "")
    if m:
        return m.group(1).lower()
    for m in _TAG_SUBJ.finditer(subject or ""):
        name = m.group(1).lower()
        if name in reg:
            return name
    return None


def _parent_precinct(uid, subject):
    """The parent task's precinct via the map: parent from 'Task N' in the reply
    subject or the stamped parent_task in this uid's spec. Returns (precinct, N)."""
    cand = []
    for m in _SUBJ_TASK.finditer(subject or ""):
        n = int(m.group(1))
        if str(n) != str(uid):
            cand.append(n)
    spec = INBOX / f"task_{uid}.md"
    if spec.exists():
        mm = re.search(r"^\s*parent[_ ]?task\s*[:=]\s*(\d+)",
                       spec.read_text(errors="replace"), re.I | re.M)
        if mm:
            cand.append(int(mm.group(1)))
    m = _read_map()
    for n in cand:
        e = m.get(str(n))
        if isinstance(e, dict) and e.get("precinct"):
            return e["precinct"], n
    return None, None


def parse_model(subject="", body=""):
    """Return a VALID model named by an email 'model:' tag (body 'model: <name>'
    first, else subject '[model:<name>]'), lowercased, or None. Task 376: the
    inbox handler exports this as WORKER_MODEL so the spawned deputy overrides the
    precinct default."""
    m = _TAG_MODEL_BODY.search(body or "")
    if m:
        return m.group(1).lower()
    m = _TAG_MODEL_SUBJ.search(subject or "")
    if m:
        return m.group(1).lower()
    return None


def resolve(uid, subject="", body=""):
    """Return (precinct_or_None, basis) per the documented precedence."""
    reg = registered_precincts()
    tag = _explicit_tag(subject, body, reg)
    if tag:
        if tag in reg:
            return tag, "explicit-tag"
        return None, f"explicit-tag-unknown:{tag}"   # receptionist may create it
    pp, pn = _parent_precinct(uid, subject)
    if pp:
        return pp, f"parent-task:{pn}"
    return None, "receptionist"


def _cli(argv=None):
    ap = argparse.ArgumentParser(prog="scratch_precinct.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="print '<precinct>\\t<basis>' per the matching rule")
    r.add_argument("--uid", required=True)
    r.add_argument("--subject", default="")
    r.add_argument("--body", default=None)
    r.add_argument("--bodyfile", default=None)

    s = sub.add_parser("stamp", help="merge-update the task->precinct map (lock+atomic)")
    s.add_argument("--uid", required=True)
    s.add_argument("--precinct", default=None)
    s.add_argument("--agent", default=None)
    s.add_argument("--session", default=None)
    s.add_argument("--deputy", default=None)
    s.add_argument("--handler", default=None,
                   help="Task 377: lesser triage-lineage name (never clobbers deputy/agent)")
    s.add_argument("--basis", default=None)

    g = sub.add_parser("get", help="print the map entry for a task as JSON")
    g.add_argument("--uid", required=True)

    rm = sub.add_parser("remove", help="delete a task's map entry (lock+atomic; Task 377)")
    rm.add_argument("--uid", required=True)

    mo = sub.add_parser("model", help="print a VALID model named by an email 'model:' tag, else empty")
    mo.add_argument("--subject", default="")
    mo.add_argument("--body", default=None)
    mo.add_argument("--bodyfile", default=None)

    a = ap.parse_args(argv)
    if a.cmd == "resolve":
        body = a.body
        if body is None and a.bodyfile:
            try:
                body = Path(a.bodyfile).read_text(errors="replace")
            except Exception:
                body = ""
        precinct, basis = resolve(a.uid, a.subject or "", body or "")
        sys.stdout.write(f"{precinct or ''}\t{basis}\n")
    elif a.cmd == "model":
        body = a.body
        if body is None and a.bodyfile:
            try:
                body = Path(a.bodyfile).read_text(errors="replace")
            except Exception:
                body = ""
        sys.stdout.write((parse_model(a.subject or "", body or "") or "") + "\n")
    elif a.cmd == "stamp":
        e = stamp_map(a.uid, precinct=a.precinct, agent=a.agent, session=a.session,
                      deputy=a.deputy, handler=a.handler, basis=a.basis)
        print(json.dumps({a.uid: e}))
    elif a.cmd == "get":
        print(json.dumps(get(a.uid)))
    elif a.cmd == "remove":
        print(f"removed map[{a.uid}]" if remove_map(a.uid) else f"no map entry for {a.uid}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
