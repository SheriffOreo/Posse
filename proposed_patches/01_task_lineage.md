# Proposed (NOT APPLIED): make task lineage exact

**Status:** design only. Nothing here has been applied to the live infra —
applying it edits `scratch_inbox.py` / `scratch_inbox_handle.sh`, which are live
daemons, and needs Steven's go-ahead + a daemon bounce.

## Problem

Follow-ups are dispatched as **new** `task_<uid>.md` specs, but the parent link is
**not persisted**. `sent_emails.jsonl` stores only `{agent, subject, to, ts,
message_id}` — no `In-Reply-To`. So the dashboard reconstructs lineage best-effort
(explicit `task_A->B` chains, "follow-up of Task N" phrasing, subject-thread
fallback) and only ~27/168 tasks get an inferred parent. The information needed for
an exact link **already flows through `scratch_inbox.py`** at routing time — it is
just thrown away.

## Where the exact parent already exists

`scratch_inbox.py::route(in_reply_to, references, subject, body)` (around line 274)
matches the reply's `In-Reply-To` / `References` against `message_id`s logged in
`sent_emails.jsonl` and recovers the **parent agent**. At that moment we also know
the parent **message_id** and **subject**. The new task's number is the reply
**uid** (`task_<uid>.md`). So the child→parent edge is fully determined right there.

## Minimal, additive change (illustrative diff — do not apply blind)

1) **Persist the routed parent** — in `scratch_inbox.py`, when a reply is routed by
   header match, append one line to `scratch_full_logs/inbox/lineage.jsonl`:

```python
# after route() resolves a header match to (agent, parent_message_id):
import json, time
with open(INBOX_DIR / "lineage.jsonl", "a") as fh:
    fh.write(json.dumps({
        "child_uid": int(uid),
        "parent_message_id": parent_message_id,
        "parent_agent": agent,
        "parent_subject": subject,
        "ts": time.time(),
    }) + "\n")
```

2) **Tag outbound emails with their task number** — so a `parent_message_id`
   resolves to a parent *task*, add the sender's current task to each
   `sent_emails.jsonl` row. `scratch_notify_email.py` already writes that row; give
   it an optional `--task N` (or have it read the worker's `task_<n>.md` it was
   launched with) and store `"task": N`.

3) **Dashboard**: `lineage.py` prefers `lineage.jsonl` edges (basis
   `"persisted"`) over the current heuristics. One `_read_json`-style loop; the
   rest of the graph/df code is unchanged.

## Even simpler stopgap (no daemon edit)

Have the triage handler write the parent as a field in the spec it already creates.
`scratch_inbox_handle.sh` builds the task spec via a `claude -p` triage; append to
that prompt: *"Begin the spec with a line `parent_task: <N>` where <N> is the task
number of the thread you are replying to, or `none`."* `lineage.py` already parses a
`parent_task:` field (basis `"parent-field"`), so this lights up immediately with
zero dashboard changes. Less reliable than (1)+(2) because it depends on the LLM,
but it is a one-line prompt addition.

## Recommendation

Ship (1)+(2): it is small, additive, race-free, and makes lineage **exact** going
forward while the heuristic reconstruction continues to cover history.
