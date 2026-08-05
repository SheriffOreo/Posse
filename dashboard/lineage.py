"""
Task lineage + per-task conversation reconstruction.

WHY THIS IS BEST-EFFORT
-----------------------
Follow-ups are dispatched by the inbox handler as new task_<uid>.md specs, but the
parent link is NOT persisted anywhere (sent_emails.jsonl stores only
agent/subject/to/ts/message_id -- no In-Reply-To). So we reconstruct lineage from
three signals, in decreasing confidence:

  1. EXPLICIT chain notation in a spec body, e.g. "task_274->275->276->316"
     or "Task 274 -> 275" -> exact edges.
  2. FOLLOW-UP phrasing, e.g. "follow-up of Task 274", "supersedes Task 261".
  3. THREAD fallback: tasks whose originating email shares a normalized subject
     are chained by time.

Each edge carries a `basis` so the UI can show how confident it is. The permanent
fix (recommended to Steven, patch in ../proposed_patches/) is to have
scratch_inbox_handle.sh stamp `parent_task:` into every task spec at creation.
"""
import glob
import json
import os
import re
import time
from pathlib import Path

import config
import state

_ARROW = r"(?:->|-->|=>|→)"
_CHAIN = re.compile(r"task[_ ]?(\d+(?:\s*" + _ARROW + r"\s*\d+)+)", re.I)
_FOLLOWUP = re.compile(
    r"(?:follow[- ]?up (?:to|of|for)|supersed\w*|continu\w*(?: from)?|builds on|"
    r"parent[_ ]?task[:=]?)\s*task[_ ]?(\d+)",
    re.I,
)
_PARENT_FIELD = re.compile(r"^\s*parent[_ ]?task\s*[:=]\s*(\d+)\s*$", re.I | re.M)


def _norm_subject(s):
    s = (s or "").strip()
    s = re.sub(r"^(?:\s*(?:re|fwd?|fw)\s*:\s*)+", "", s, flags=re.I)
    s = re.sub(r"\[[^\]]*\]", "", s)  # drop [tags]
    s = re.sub(r"--\s*task\s*\d+.*$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


# The "Task N" token agents reliably put in every outbound subject
# ("Task 324 START/milestone/FINAL/DONE", or a "Re: Task 324 ..." ack). Used to
# attribute an OUTBOUND email to its task explicitly, overriding subject-thread
# matching (a persistent worker like `paper` spans many task numbers, so its
# normalized subject alone mis-groups emails). See task_conversation().
_TASK_TOKEN = re.compile(r"\b(?:task|case)[ _]?#?(\d+)", re.I)  # Case 427: "Case N" too


def subject_task(subject):
    """Integer N parsed from a 'Task N ...' / 'Re: Task N ...' subject, else None."""
    m = _TASK_TOKEN.search(subject or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# inbound / outbound email indexes
# --------------------------------------------------------------------------- #
def _inbound_index():
    out = []
    for f in glob.glob(str(config.INBOX / "[0-9]*.txt")):
        base = os.path.basename(f)
        m = re.match(r"(\d+)\.txt$", base)
        if not m:
            continue
        uid = int(m.group(1))
        try:
            txt = Path(f).read_text(errors="replace")
            ts = os.path.getmtime(f)
        except Exception:
            continue
        subj, frm = "", ""
        for ln in txt.splitlines()[:6]:
            if ln.upper().startswith("SUBJECT:"):
                subj = ln.split(":", 1)[1].strip()
            elif ln.upper().startswith("FROM:"):
                frm = ln.split(":", 1)[1].strip()
        body = txt.split("\n\n", 1)[1] if "\n\n" in txt else txt
        out.append(
            {
                "dir": "in",
                "uid": uid,
                "subject": subj,
                "norm": _norm_subject(subj),
                "from": frm,
                "ts": ts,
                "snippet": " ".join(body.split())[:280],
                "file": f,
            }
        )
    return out


def _outbound_index():
    out = []
    try:
        lines = Path(config.SENT_EMAILS).read_text(errors="replace").splitlines()
    except Exception:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        out.append(
            {
                "dir": "out",
                "agent": r.get("agent"),
                "subject": r.get("subject", ""),
                "norm": _norm_subject(r.get("subject", "")),
                # Task 327: explicit "Task N" token -> the task this email belongs
                # to (None if the subject has no such token). Overrides norm-subject
                # thread matching in task_conversation() so a persistent worker's
                # "Task 324 FINAL" no longer leaks into a sibling task's thread.
                "task": subject_task(r.get("subject", "")),
                "to": r.get("to"),
                "ts": r.get("ts"),
                "message_id": r.get("message_id"),
                # Task 323 D2: persisted outbound body + attachment paths (absent on
                # rows written before the change -> empty, handled by the UI).
                "body": r.get("body", "") or "",
                "attachments": r.get("attachments", []) or [],
            }
        )
    return out


def _indexes():
    return state._cached("email_idx", 60, lambda: (_inbound_index(), _outbound_index()))


# --------------------------------------------------------------------------- #
# task nodes
# --------------------------------------------------------------------------- #
def _task_node(f):
    base = os.path.basename(f)
    m = re.match(r"task_(\d+)\.md$", base)
    if not m:
        return None
    tid = int(m.group(1))
    try:
        body = Path(f).read_text(errors="replace")
        ts = os.path.getmtime(f)
    except Exception:
        body, ts = "", None
    title = f"Task {tid}"
    for ln in body.splitlines():
        if ln.strip().startswith("#"):
            title = ln.lstrip("# ").strip()
            break
    agent = None
    am = re.search(r"--agent\s+([A-Za-z0-9_]+)", body)
    if am:
        agent = am.group(1)
    return {"task_id": tid, "title": title, "ts": ts, "day": state.day_of(ts),
            "agent": agent, "body": body}


def _all_task_nodes():
    nodes = {}
    for f in glob.glob(str(config.INBOX / "task_*.md")):
        n = _task_node(f)
        if n:
            nodes[n["task_id"]] = n
    return nodes


def build_lineage():
    return state._cached("lineage", 60, _build_lineage)


def _build_lineage():
    nodes = _all_task_nodes()
    ids = set(nodes)
    edges = {}  # child_id -> {"parent": pid, "basis": ...}

    def _set(child, parent, basis):
        if parent in ids and parent != child and child not in edges:
            edges[child] = {"parent": parent, "basis": basis}

    # 1) explicit arrow chains  (task_274->275->276->316). Intermediate task
    #    numbers may not exist as spec files, so link each element to its nearest
    #    PRECEDING element that is a real node (274->276->316 with 275 absent still
    #    yields 316 -> 274).
    for tid, n in nodes.items():
        for m in _CHAIN.finditer(n["body"]):
            seq = [int(x) for x in re.findall(r"\d+", m.group(1))]
            for i in range(1, len(seq)):
                for j in range(i - 1, -1, -1):
                    if seq[j] in ids:
                        _set(seq[i], seq[j], "explicit-chain")
                        break
    # 2) follow-up phrasing / parent_task field
    for tid, n in nodes.items():
        pf = _PARENT_FIELD.search(n["body"])
        if pf:
            _set(tid, int(pf.group(1)), "parent-field")
        else:
            fm = _FOLLOWUP.search(n["body"])
            if fm:
                _set(tid, int(fm.group(1)), "followup-phrase")
    # 2b) reply-subject (Task 323 retro): a task whose ORIGINATING email subject is
    #     "Re: Task N ..." is a follow-up of N. Lower confidence than the persisted
    #     parent_task field (an LLM-free but heuristic read), higher than same-subject
    #     threading. This retroactively links historical chains (e.g.
    #     317->319->320->322) whose specs predate the parent_task stamp.
    # (env gate INFRA_LINEAGE_NO_REPLY_SUBJECT lets one A/B the heuristic — e.g.
    #  render the pre-Task-323 "before" state; default is ON.)
    if not os.environ.get("INFRA_LINEAGE_NO_REPLY_SUBJECT"):
        inbound_by_uid = {e["uid"]: e for e in _indexes()[0]}
        for tid in nodes:
            if tid in edges:
                continue
            m = re.search(r"\b(?:task|case)[ _]?#?(\d+)", inbound_by_uid.get(tid, {}).get("subject", ""), re.I)
            if m:
                _set(tid, int(m.group(1)), "reply-subject")
    # 3) subject-thread fallback: chain same-subject tasks by ascending id
    threads = {}
    inbound = {e["uid"]: e for e in _indexes()[0]}
    for tid, n in nodes.items():
        subj = inbound.get(tid, {}).get("norm")
        if subj:
            threads.setdefault(subj, []).append(tid)
    for subj, members in threads.items():
        members.sort()
        for a, b in zip(members, members[1:]):
            _set(b, a, "subject-thread")

    node_list = []
    for tid, n in sorted(nodes.items()):
        node_list.append(
            {
                "task_id": tid,
                "title": n["title"],
                "agent": n["agent"],
                "day": n["day"],
                "ts": n["ts"],
                "parent": edges.get(tid, {}).get("parent"),
                "basis": edges.get(tid, {}).get("basis"),
            }
        )
    return {"nodes": node_list, "count": len(node_list),
            "linked": len(edges), "roots": sum(1 for x in node_list if not x["parent"])}


def build_forest():
    """Group the flat lineage into rooted trees for display.

    Returns nested trees (each node gets a `children` list), sorted largest-first,
    plus the standalone tasks kept SEPARATE (a root with no children). The whole
    point of the forest view is to show the 13 real trees and *summarize* the ~130
    singletons rather than plot them as disconnected dots (the old hairball)."""
    return state._cached("forest", 60, _build_forest)


def _build_forest():
    lin = build_lineage()
    # Task 353A: the flat lineage nodes carry only the spec-scraped --agent, which is
    # usually None for a persistent worker. Resolve each node's OWNING worker via
    # state.agent_for_task (prompt map -> "Task N" email map), mirroring
    # history_day_lineage (Task 327), so the forest's treeNodeLi wchip shows "· <agent>".
    by = {}
    for n in lin["nodes"]:
        node = dict(n, children=[])
        node["agent"] = state.agent_for_task(n["task_id"], n.get("agent"))
        by[n["task_id"]] = node
    for n in lin["nodes"]:
        p = n["parent"]
        if p is not None and p in by:
            by[p]["children"].append(by[n["task_id"]])
    for n in by.values():
        n["children"].sort(key=lambda c: c["task_id"])

    def _size(n):
        return 1 + sum(_size(c) for c in n["children"])

    def _depth(n):
        return 1 + max([_depth(c) for c in n["children"]], default=0)

    roots = [n for n in by.values() if not n["parent"]]
    trees = [n for n in roots if n["children"]]
    singles = [n for n in roots if not n["children"]]
    for t in trees:
        t["size"] = _size(t)
        t["depth"] = _depth(t)
    # largest tree first; the deep chains and stars read top-left → bottom-right
    trees.sort(key=lambda t: (-t["size"], t["task_id"]))
    singles.sort(key=lambda n: n["task_id"])
    return {
        "trees": trees,
        "singletons": singles,
        "n_tasks": lin["count"],
        "n_linked": lin["linked"],
        "n_trees": len(trees),
        "n_singletons": len(singles),
        "n_in_trees": lin["count"] - len(singles),
    }


# basis → (human label, confidence rank 0=highest). Single source of truth for the
# legend in both the standalone figure and the in-dashboard view.
BASIS_INFO = {
    "explicit-chain": ("explicit chain (task_a->b in spec)", 0),
    "parent-field":   ("parent_task: field", 0),
    "reply-subject":  ("originating email subject 'Re: Task N'", 1),
    "followup-phrase": ("follow-up phrasing", 1),
    "subject-thread": ("same email subject (heuristic)", 2),
}


def lineage_for(task_id):
    """Ancestors + descendants of one task, for the detail view."""
    lin = build_lineage()
    by = {n["task_id"]: n for n in lin["nodes"]}
    if task_id not in by:
        return None
    # ancestors
    chain = []
    cur = by[task_id].get("parent")
    seen = set()
    while cur in by and cur not in seen:
        seen.add(cur)
        chain.append(by[cur])
        cur = by[cur].get("parent")
    chain.reverse()
    # direct children
    kids = [n for n in lin["nodes"] if n.get("parent") == task_id]
    kids.sort(key=lambda r: r["task_id"])
    return {"ancestors": chain, "self": by[task_id], "children": kids}


def _pub_node(n):
    return {"task_id": n["task_id"], "title": n["title"], "agent": n.get("agent"),
            "day": n.get("day"), "ts": n.get("ts"),
            "parent": n.get("parent"), "basis": n.get("basis")}


def history_day_lineage(day):
    """Tasks launched on `day`, arranged as compact mini-trees grouped by root.

    For each task launched that day we include its ancestor chain up to the root
    (so a mini-tree is rooted at the true root) and its DIRECT children, tagging
    every node on_day / off_day. Off-day nodes are context — the UI renders them
    muted with a link. Nodes not needed to connect a day-task to its root or its
    children are pruned, so the trees stay compact rather than the full forest."""
    lin = build_lineage()
    nodes = {n["task_id"]: n for n in lin["nodes"]}
    day_ids = sorted((tid for tid, n in nodes.items() if n.get("day") == day),
                     reverse=True)
    # children adjacency (parent id -> [child ids])
    kids = {}
    for tid, n in nodes.items():
        p = n.get("parent")
        if p in nodes:
            kids.setdefault(p, []).append(tid)
    # relevant set: each day-task + ancestors (to root) + direct children
    rel = set()
    for d in day_ids:
        rel.add(d)
        cur, seen = nodes[d].get("parent"), set()
        while cur in nodes and cur not in seen:
            seen.add(cur)
            rel.add(cur)
            cur = nodes[cur].get("parent")
        for k in kids.get(d, []):
            rel.add(k)
    day_set = set(day_ids)
    # Task 327: resolve each node's OWNING worker so the History mini-tree can show
    # it. _pub_node carries only the spec-scraped --agent (usually None for a
    # persistent worker), so fall back through state.agent_for_task (prompt map ->
    # "Task N" email map).
    obj = {}
    for tid in rel:
        pub = _pub_node(nodes[tid])
        pub["agent"] = state.agent_for_task(tid, pub.get("agent"))
        obj[tid] = dict(pub, on_day=(tid in day_set), children=[])
    roots = []
    for tid in rel:
        p = nodes[tid].get("parent")
        if p in rel:
            obj[p]["children"].append(obj[tid])
        else:
            roots.append(obj[tid])
    for o in obj.values():
        o["children"].sort(key=lambda c: c["task_id"])
    roots.sort(key=lambda r: -r["task_id"])
    return {"day": day, "trees": roots, "n_day_tasks": len(day_ids),
            "day_ids": day_ids}


# --------------------------------------------------------------------------- #
# per-task conversation
# --------------------------------------------------------------------------- #
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}


def _full_body(path):
    """Complete inbound email body: whole file minus the leading header block
    (FROM:/SUBJECT: lines up to the first blank line). No truncation."""
    try:
        txt = Path(path).read_text(errors="replace")
    except Exception:
        return ""
    return txt.split("\n\n", 1)[1] if "\n\n" in txt else txt


def _attachments_for(uid):
    """Files saved for an inbound message under inbox/att/<uid>/ (already a
    DOWNLOAD_ROOT), flagged is_image so the UI can inline them."""
    d = config.INBOX / "att" / str(uid)
    out = []
    try:
        for f in sorted(d.iterdir()):
            if f.is_file():
                out.append({"name": f.name, "path": str(f.resolve()),
                            "is_image": f.suffix.lower() in _IMG_EXT})
    except Exception:
        pass
    return out


def _out_attachments(paths):
    """Task 323 D2/D3: validate persisted outbound attachment paths for display —
    keep only those the guarded /download endpoint would actually serve, tagging
    is_image so the UI can inline figures (same shape as _attachments_for)."""
    out = []
    for p in (paths or []):
        rp = config.resolve_download(p)
        if rp is None:
            continue
        out.append({"name": rp.name, "path": str(rp),
                    "is_image": rp.suffix.lower() in _IMG_EXT})
    return out


# --------------------------------------------------------------------------- #
# case identity — authoritative maps (Case 427)
# --------------------------------------------------------------------------- #
_WEB_MARKER = "submitted through the dashboard"
_FORM_DESC = re.compile(r"##\s*Task description[^\n]*\n+(.*?)(?:\n##\s|\Z)", re.S | re.I)
_FORM_UPLOADS = re.compile(r"##\s*Uploaded files.*?\n(.*?)(?:\n##\s|\Z)", re.S | re.I)


def _case_deputy(task_id):
    """The deputy that OWNS this case, from the spawn/board record
    (task_precinct.json) — authoritative for both web and email cases. None for
    old migrated rows that recorded only a precinct (caller falls back)."""
    rec = state.task_precinct_map().get(str(task_id))
    if isinstance(rec, dict):
        return rec.get("deputy") or rec.get("agent")
    return None


def _uid_case_map():
    try:
        d = json.loads((config.RECORDS / "uid_case.json").read_text())
        return {int(k): int(v) for k, v in d.items()}
    except Exception:
        return {}


def _origin_uid(task_id, is_web):
    """Gmail uid of the email that SPAWNED this case, or None.

    Web cases have no originating email (the request lives in the spec). A
    decoupled (post-391a) email case is looked up in uid_case.json (its case
    number != the Gmail uid). A legacy email case reused the inbound uid AS the
    case number, so <case>.txt is the origin — but ONLY when that uid isn't
    linked to a DIFFERENT case, which is exactly the collision that made case 426
    show the unrelated 'Weekly slides' thread (Gmail uid 426 belongs elsewhere)."""
    if is_web:
        return None
    uc = _uid_case_map()
    for uid, case in uc.items():
        if case == task_id:
            return uid
    if task_id in uc and uc[task_id] != task_id:
        return None  # <case>.txt is a different case's email — a number collision
    if (config.INBOX / f"{task_id}.txt").exists():
        return task_id  # legacy: case number == inbound uid
    return None


def _is_web_case(body):
    return _WEB_MARKER in (body or "").lower()


def _form_description(body):
    """Verbatim web-form request from a web-case spec, else None."""
    m = _FORM_DESC.search(body or "")
    return m.group(1).strip() if m else None


def _form_uploads(body):
    """Downloadable files the user attached to a web-form submission (from the
    spec's '## Uploaded files' block), tagged is_image for inline display."""
    m = _FORM_UPLOADS.search(body or "")
    if not m:
        return []
    out, seen = [], set()
    for p in re.findall(r"(/\S+)", m.group(1)):
        rp = config.resolve_download(p.rstrip(").,"))
        if rp is None or str(rp) in seen:
            continue
        seen.add(str(rp))
        out.append({"name": rp.name, "path": str(rp),
                    "is_image": rp.suffix.lower() in _IMG_EXT})
    return out


def task_conversation(task_id):
    """Reconstruct ONE case's conversation (Case 427): the user's initial request,
    the in/out emails that actually belong to the case, and the deputy's FINAL —
    with mechanical auto-acks excluded and unrelated threads kept out.

    Attribution is case-centric, not Gmail-uid-centric: a "Case N"/"Task N" subject
    token binds an email to its case; a token-less email from a deputy DEDICATED to
    this case (its only case is N — e.g. a per-case web_<n> worker) belongs here
    too; a token-less user reply that threads onto one of the case's subjects is a
    reply. This fixes the uid==case collision that pulled a foreign thread in, and
    surfaces web-form requests that were never emails."""
    nodes = _all_task_nodes()
    node = nodes.get(task_id)
    body = node["body"] if node else ""
    inbound, outbound = _indexes()

    is_web = _is_web_case(body)
    # authoritative owner, else the heuristic resolver (spec --agent -> board ->
    # worker-prompt -> "Case N" email vote).
    deputy = _case_deputy(task_id) or state.agent_for_task(
        task_id, node.get("agent") if node else None)

    origin_uid = _origin_uid(task_id, is_web)
    origin_in = next((e for e in inbound if e["uid"] == origin_uid), None) if origin_uid else None

    # deputy dedicated to this case? (every case token it used is task_id) -> a
    # token-less email it sent belongs here.
    dtoks = {o["task"] for o in outbound
             if o.get("agent") == deputy and o.get("task") is not None}
    dedicated = bool(deputy) and dtoks <= {task_id}

    def _out_belongs(o):
        # Deputy-anchored (Case 427): the number alone is not enough — historical
        # collisions overload one number across deputies (e.g. paper's "Task 399"
        # vs infra's "Case 399"). Attribute to the case's own deputy; only if the
        # deputy is unknown do we fall back to a bare token match.
        a, tok = o.get("agent"), o.get("task")
        if tok is not None and tok != task_id:
            return False                     # names a DIFFERENT case -> never
        if deputy:
            if a != deputy:
                return False
            return tok == task_id or dedicated
        return tok == task_id                # deputy unknown -> best-effort token

    # anchor norms: the originating email + every outbound we attribute to the case
    # -> pulls in token-less user REPLIES to a milestone.
    anchor = set()
    if origin_in and origin_in.get("norm"):
        anchor.add(origin_in["norm"])
    for o in outbound:
        if _out_belongs(o) and o.get("norm"):
            anchor.add(o["norm"])

    # subjects of same-numbered mail sent by a DIFFERENT deputy (a historical
    # number collision) — a user reply that threads onto one of these is about the
    # OTHER work, not this case, even though it carries the matching number.
    foreign_norms = {o["norm"] for o in outbound
                     if o.get("task") == task_id and o.get("agent") != deputy and o.get("norm")}

    def _in_belongs(e):
        if e.get("norm") and e["norm"] in foreign_norms:
            return False
        tok = subject_task(e["subject"])
        if tok is not None:
            return tok == task_id
        return bool(e.get("norm")) and e["norm"] in anchor

    # ---- gather the real emails (auto-acks dropped) ----------------------- #
    emails = []
    for e in inbound:
        if origin_uid and e["uid"] == origin_uid:
            continue  # the originating email is added as the initial, below
        if not _in_belongs(e):
            continue
        fb = _full_body(e["file"])
        if state.is_autoack(e["subject"], fb):
            continue
        emails.append({"dir": "in", "ts": e["ts"], "from": e["from"],
                       "subject": e["subject"], "body": fb,
                       "attachments": _attachments_for(e["uid"]), "kind": "message"})
    for o in outbound:
        if not _out_belongs(o):
            continue
        if state.is_autoack(o["subject"], o.get("body", "")):
            continue
        emails.append({"dir": "out", "ts": o["ts"], "agent": o.get("agent"),
                       "to": o.get("to"), "subject": o["subject"],
                       "body": o.get("body", ""),
                       "attachments": _out_attachments(o.get("attachments")),
                       "kind": "message"})
    emails = [m for m in emails if m.get("ts")]
    emails.sort(key=lambda m: m["ts"])

    # ---- initial task description (first message) ------------------------- #
    msgs = []
    if is_web:
        desc = _form_description(body) or (node or {}).get("title") or ""
        rm = re.search(r"Email\s+(\S+@\S+)", body)
        # sort strictly first even if the spec mtime drifted past the first email
        first_ts = emails[0]["ts"] if emails else ((node or {}).get("ts") or 0)
        init_ts = (node or {}).get("ts")
        if init_ts is None or (emails and init_ts >= first_ts):
            init_ts = first_ts - 1
        msgs.append({"dir": "in", "ts": init_ts,
                     "from": (rm.group(1) if rm else "you (web form)"),
                     "subject": (node or {}).get("title") or f"Case {task_id}",
                     "body": desc, "attachments": _form_uploads(body),
                     "kind": "initial"})
    elif origin_in:
        msgs.append({"dir": "in", "ts": origin_in["ts"], "from": origin_in["from"],
                     "subject": origin_in["subject"],
                     "body": _full_body(origin_in["file"]),
                     "attachments": _attachments_for(origin_in["uid"]),
                     "kind": "initial"})

    msgs.extend(emails)
    # the initial request always leads, then chronological (an email-origin file's
    # mtime can drift past the deputy's first reply, so don't rank it purely by ts).
    msgs.sort(key=lambda m: (0 if m.get("kind") == "initial" else 1, m["ts"]))
    # tag the deputy's last outbound as FINAL
    for m in reversed(msgs):
        if m["dir"] == "out":
            m["kind"] = "final"
            break

    header_subject = (origin_in or {}).get("subject") or (node or {}).get("title")
    return {
        "task_id": task_id,
        "agent": deputy,
        "subject": header_subject,
        "messages": msgs,
        "note": "Scoped to this case (Case 427): your initial request, the in/out "
                "emails attributed to the case by deputy + Case/Task number, and the "
                "deputy's FINAL. Mechanical auto-acks are excluded; unrelated threads "
                "that merely share a Gmail message number are no longer pulled in.",
    }
