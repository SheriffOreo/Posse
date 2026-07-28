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
                "to": r.get("to"),
                "ts": r.get("ts"),
                "message_id": r.get("message_id"),
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
    by = {n["task_id"]: dict(n, children=[]) for n in lin["nodes"]}
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
    obj = {tid: dict(_pub_node(nodes[tid]), on_day=(tid in day_set), children=[])
           for tid in rel}
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
def task_conversation(task_id):
    nodes = _all_task_nodes()
    node = nodes.get(task_id)
    inbound, outbound = _indexes()
    # resolve the worker agent (spec --agent, else worker-prompt fallback) — used
    # both for subject threading below and as the conversation header label.
    agent = state.agent_for_task(task_id, node.get("agent") if node else None)
    root_in = next((e for e in inbound if e["uid"] == task_id), None)
    norm = None
    if root_in:
        norm = root_in["norm"]
    elif agent:
        # derive a subject from the resolved agent's first outbound
        cand = [o for o in outbound if o.get("agent") == agent]
        if cand:
            norm = min(cand, key=lambda o: o.get("ts") or 0)["norm"]

    msgs = []
    for e in inbound:
        if norm and e["norm"] == norm:
            msgs.append(
                {"dir": "in", "ts": e["ts"], "from": e["from"],
                 "subject": e["subject"],
                 "body": _full_body(e["file"]),
                 "attachments": _attachments_for(e["uid"])}
            )
    for o in outbound:
        match_subj = norm and o["norm"] == norm
        if match_subj:
            msgs.append(
                {"dir": "out", "ts": o["ts"], "agent": o.get("agent"),
                 "to": o.get("to"), "subject": o["subject"],
                 "body": "", "attachments": []}
            )
    msgs = [m for m in msgs if m.get("ts")]
    msgs.sort(key=lambda m: m["ts"])
    return {
        "task_id": task_id,
        "agent": agent,
        "subject": (root_in or {}).get("subject") or (node or {}).get("title"),
        "messages": msgs,
        "note": "Thread reconstructed by normalized-subject matching (In-Reply-To is "
                "not persisted). Inbound bodies + attachments are shown in full; "
                "outbound bodies/attachments are NOT stored (sent_emails.jsonl keeps "
                "metadata only), so only their subject + time appear.",
    }
