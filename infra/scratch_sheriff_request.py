#!/usr/bin/env python3
"""Case 384a (Phase 1 of the sheriff redesign): the deputy->sheriff REQUEST QUEUE.

This is the ONE way a deputy asks the sheriff to perform a SHERIFF-ONLY records
operation. Deputies may only ever APPEND to their precinct's ledger + case log;
everything that rewrites history -- retracting a case-log line, editing a ledger,
creating/deleting a precinct -- and allocating a fresh case number is the sheriff's
authority alone. A deputy cannot do those itself (scratch_records refuses a
non-``sheriff`` role); instead it POSTS a request here, the sheriff daemon
(scratch_sheriff.py) decides it with a Claude call, and -- only if approved -- the
sheriff performs the op under its own lock and journals it.

DESIGN (mirrors scratch_web_case.py / scratch_case_seq.py):

  records/sheriff_requests/
    pending/<id>.json   a deputy just posted it; the sheriff has not decided yet
    done/<id>.json      approved + performed (annotated with decision + result)
    denied/<id>.json    denied, or an identity-check failure (annotated with reason)

  request JSON = {
    id, op, precinct, target, deputy, session, reason, ts,
    ...op-specific extras (new_content|drop_contains, description|mode|model,
       case_description), and after a decision: decision, decided_ts, result|denied_reason
  }

OPS (the sheriff-only operations a deputy may request):
    log_remove      retract ALL case-log lines for a task    (target = task#)
    ledger_modify   rewrite/prune a precinct ledger          (target = precinct;
                        carries new_content OR drop_contains)
    precinct_create create a new precinct                    (target = name;
                        carries description/mode/model)
    precinct_delete delete a precinct  (Phase D: guarded -- interlock + emailed YES
                        confirm + 14-day recoverable trash; see scratch_sheriff.py)
    precinct_restore restore a soft-deleted precinct from trash (within 14 days)
    case_number     allocate a fresh case number for the requesting deputy and make
                        it the deputy's active case (target = precinct;
                        carries case_description)

ANTI-IMPERSONATION (``verify_identity``). A request is trusted only if its
``deputy`` + ``session`` match the live worker registry (scratch_agents_registry.json
``workers[<deputy>].session``): the session id is the secret handed to that deputy at
spawn, so proving it owns that session proves it is that deputy. A forged deputy or a
wrong session is rejected. No deputy is ever handed ``role='sheriff'`` -- the
sheriff-only records ops keep refusing a non-sheriff role, and are invoked ONLY by the
sheriff daemon after approval.

CONCURRENCY. Mutations (submit, move-to-done/denied, journal append) take the same
exclusive-advisory-lock + atomic-replace discipline as scratch_records.py. Reads are
lock-free. ``TSOMP_RECORDS_ROOT`` overrides the records root (tests);
``TSOMP_REGISTRY_PATH`` overrides the worker registry (tests).

CLI:
    scratch_sheriff_request.py request --op OP --precinct P --deputy D --session S \\
        --reason R [--target T] [--new-content ... | --new-content-file f |
        --drop-contains ...] [--description ...] [--mode mutable|fixed]
        [--model ...] [--case-description ...] [--wait SECONDS]
    scratch_sheriff_request.py get     --id ID            # the record (any state) as JSON
    scratch_sheriff_request.py status  --id ID            # pending|done|denied (+ result/reason)
    scratch_sheriff_request.py verify  --deputy D --session S    # ok|forged (debug/tests)
    scratch_sheriff_request.py pending                    # count pending requests
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS_ROOT = REPO_ROOT / "scratch_full_logs" / "records"
DEFAULT_REGISTRY = REPO_ROOT / "scratch_agents_registry.json"
QUEUE_DIRNAME = "sheriff_requests"
_STATES = ("pending", "done", "denied")

# The sheriff-only operations a deputy (or, for the precinct-lifecycle ops, the
# receptionist on the user's behalf) may request. Phase D (Case 384d) makes
# ``precinct_delete`` a real, guarded op (soft-delete + emailed YES confirm + 14-day
# trash) and adds ``precinct_restore``.
# The CRITIC ops work the same way: the judge registry (records/critics.json) is
# sheriff-owned exactly like precincts.json, so adding/changing/retiring a judge is
# a request, never a self-serve write -- whether it originates from a deputy, from
# the dashboard's Judges tab, or from a user e-mail via the receptionist.
OPS = ("log_remove", "ledger_modify", "precinct_create", "precinct_delete",
       "precinct_restore", "case_number",
       "critic_add", "critic_update", "critic_remove")

# Phase D: the precinct-lifecycle ops are USER-authorized (the user asks; the
# receptionist HANDS OFF). They may be posted WITHOUT a deputy session -- with an
# ``origin="receptionist"`` and a ``requester`` that is an allowed user email --
# because the real authority is the user's mailbox: create/restore are non-
# destructive + user-notified, and delete cannot complete without the user's emailed
# YES (the hard gate). Every OTHER op still requires a verified (deputy, session).
# The critic ops join them -- the user is the one who decides the fleet should have a
# new judge, so "e-mail the receptionist" and "the dashboard Judges tab" must both be
# able to post one without a deputy session. The sheriff still decides every one of
# them; nothing here is self-approving.
RECEPTIONIST_ORIGIN = "receptionist"
USER_AUTHORIZED_OPS = ("precinct_create", "precinct_delete", "precinct_restore",
                       "critic_add", "critic_update", "critic_remove")
try:                                        # reuse the inbox allow-list if importable
    import scratch_inbox as _inbox          # noqa: E402
    ALLOWED_REQUESTERS = set(_inbox.ALLOWED)
except Exception:                           # inbox not importable → env allow-list
    ALLOWED_REQUESTERS = {a.strip() for a in
                          os.environ.get("INFRA_MAIL_ALLOWED", "").split(",") if a.strip()}

# Phase D delete-confirmation subtree (keyed by an emailed TOKEN, decoupled from the
# request id so a stray reply can never wedge the queue):
#   sheriff_requests/awaiting_confirm/<token>.json   sheriff-written: a delete the
#       user must confirm (carries precinct, force, expires_ts, orig request id)
#   sheriff_requests/confirmations/<token>.json      router-written: the user's YES/NO
CONFIRM_AWAITING_DIR = "awaiting_confirm"
CONFIRM_REPLIES_DIR = "confirmations"
# The subject tag the sheriff stamps on a delete-confirm email and matches on reply.
CONFIRM_TAG_RE = re.compile(r"sheriff-confirm:([0-9a-fA-F]{6,})")

_DEPUTY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _records_root() -> Path:
    env = os.environ.get("TSOMP_RECORDS_ROOT")
    return Path(env) if env else DEFAULT_RECORDS_ROOT


def _queue_root() -> Path:
    return _records_root() / QUEUE_DIRNAME


def _state_dir(state: str, *, create: bool = False) -> Path:
    if state not in _STATES:
        raise ValueError(f"invalid state {state!r} (allowed: {_STATES})")
    d = _queue_root() / state
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _registry_path() -> Path:
    env = os.environ.get("TSOMP_REGISTRY_PATH")
    return Path(env) if env else DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# locking + atomic write (mirrors scratch_records)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _exclusive_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_sreq_", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# anti-impersonation identity check
# ---------------------------------------------------------------------------
def _registry_workers() -> dict:
    """The worker registry's ``workers`` map (deputy -> {session, ...}); {} if absent."""
    d = _read_json(_registry_path())
    if isinstance(d, dict) and isinstance(d.get("workers"), dict):
        return d["workers"]
    return {}


def verify_identity(deputy: str, session: str) -> bool:
    """True iff ``deputy`` is a registered live worker AND ``session`` matches the
    session bound to it at spawn (scratch_agents_registry.json). This is the
    anti-impersonation gate: the session id is the secret the system handed the
    deputy at spawn, so a matching (deputy, session) proves the caller really is
    that deputy. A forged/unknown deputy, an empty session, or a session that does
    not match the registry entry all return False."""
    if not deputy or not session:
        return False
    entry = _registry_workers().get(str(deputy))
    if not isinstance(entry, dict):
        return False
    return str(entry.get("session") or "") == str(session)


# ---------------------------------------------------------------------------
# submit / lookup / move
# ---------------------------------------------------------------------------
def _new_id() -> str:
    """A unique, roughly-sortable request id: millis + pid + random suffix."""
    return f"{int(time.time() * 1000)}_{os.getpid()}_{secrets.token_hex(3)}"


def submit(op, precinct, deputy, session, reason, target="", *, ts=None,
           origin=None, requester=None, force=False, **extra) -> dict:
    """Post a new request to pending/. LOCK + atomic. Returns the request record.

    Does NOT itself verify identity or decide anything -- that is the sheriff's job
    on its next pass. It only validates the op/shape and writes the queue file, so a
    requester can always post and get a decision (approve OR deny-with-reason) back.

    Two authorization shapes (checked at DECISION time by the sheriff, not here):
      * DEPUTY request (the default): needs a real ``deputy`` name + its spawn
        ``session`` (verified against the live registry -- anti-impersonation).
      * RECEPTIONIST hand-off (Phase D): ``origin="receptionist"`` for a precinct-
        lifecycle op (create/delete/restore), carrying a ``requester`` that is an
        allowed user email. No deputy session is needed (deputy defaults to
        'receptionist'); the user's mailbox is the real authority.
    ``force`` (delete only) records the intent to bypass the active-deputy/non-empty-
    case interlock; the emailed YES confirmation is STILL required."""
    if op not in OPS:
        raise ValueError(f"invalid op {op!r} (allowed: {OPS})")
    origin = str(origin or "").strip() or None
    requester = str(requester or "").strip() or None
    if origin == RECEPTIONIST_ORIGIN:
        if op not in USER_AUTHORIZED_OPS:
            raise ValueError(f"origin=receptionist is only for {USER_AUTHORIZED_OPS}, not {op!r}")
        if requester not in ALLOWED_REQUESTERS:
            raise ValueError("a receptionist hand-off requires a requester that is an "
                             f"allowed user email (got {requester!r})")
        deputy = str(deputy or "").strip() or RECEPTIONIST_ORIGIN
        session = str(session or "").strip()          # session optional for a hand-off
    else:
        deputy = str(deputy or "").strip()
        if not _DEPUTY_RE.match(deputy):
            raise ValueError(f"invalid deputy name {deputy!r}")
        if not str(session or "").strip():
            raise ValueError("session is required (the deputy's own spawn session id)")
        session = str(session).strip()
    if not _DEPUTY_RE.match(deputy):
        raise ValueError(f"invalid deputy name {deputy!r}")
    if not str(reason or "").strip():
        raise ValueError("reason is required (why the sheriff should approve this op)")
    rid = _new_id()
    rec = {
        "id": rid,
        "op": op,
        "precinct": str(precinct or "").strip(),
        "target": str(target or "").strip(),
        "deputy": deputy,
        "session": session,
        "reason": str(reason).strip(),
        "ts": ts if ts is not None else time.time(),
    }
    if origin:
        rec["origin"] = origin
    if requester:
        rec["requester"] = requester
    if force:
        rec["force"] = True
    for k, v in extra.items():
        if v is not None:
            rec[k] = v
    target_path = _state_dir("pending", create=True) / f"{rid}.json"
    with _exclusive_lock(target_path):
        _atomic_write(target_path, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return rec


def list_pending() -> list:
    """Sorted list of pending request file paths (oldest first). NON-BLOCKING."""
    d = _state_dir("pending")
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def _find(rid: str):
    """Locate a request by id across states; returns (state, path, record) or
    (None, None, None). NON-BLOCKING."""
    for state in _STATES:
        p = _state_dir(state) / f"{rid}.json"
        if p.is_file():
            return state, p, _read_json(p)
    return None, None, None


def get(rid: str):
    _, _, rec = _find(rid)
    return rec


def status(rid: str):
    state, _, rec = _find(rid)
    return {"id": rid, "state": state, "record": rec}


def move_to(src_path: Path, state: str, record: dict) -> Path:
    """Atomically move a request to ``done``/``denied`` with its annotated record.
    LOCK + atomic on the destination; the source is unlinked last so a crash leaves
    the request in AT MOST one place (worst case a harmless duplicate, never a torn
    file)."""
    if state not in ("done", "denied"):
        raise ValueError("move_to only targets done|denied")
    dest = _state_dir(state, create=True) / src_path.name
    with _exclusive_lock(dest):
        _atomic_write(dest, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(src_path)
    return dest


# ---------------------------------------------------------------------------
# the retraction / edit JOURNAL  (item B audit trail)
# ---------------------------------------------------------------------------
def journal_path(precinct: str) -> Path:
    """The per-precinct sheriff-action journal: records/<precinct>/retractions.log."""
    return _records_root() / str(precinct) / "retractions.log"


def journal_append(precinct, deputy, op, target, reason, decision, *, ts=None,
                   extra: str = "") -> None:
    """Append one audit line to the precinct's retraction/edit journal. LOCK + atomic.

    One tab-separated line: <iso-utc>\\t<decision>\\t<op>\\t<deputy>\\t<target>\\t<reason>.
    Every sheriff decision on a records-changing request (approve->performed and
    deny) is journaled here so who/when/op/target/reason is permanently auditable."""
    if not precinct:
        return
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))

    def _clean(v):
        return re.sub(r"\s+", " ", str(v).replace("\t", " ")).strip()

    line = "\t".join(_clean(x) for x in (when, decision, op, deputy, target, reason))
    if extra:
        line += "\t" + _clean(extra)
    target_path = journal_path(precinct)
    with _exclusive_lock(target_path):
        cur = ""
        with contextlib.suppress(FileNotFoundError):
            cur = target_path.read_text(encoding="utf-8")
        if cur and not cur.endswith("\n"):
            cur += "\n"
        if not cur:
            cur = ("# sheriff retraction/edit journal (Case 384a)\n"
                   "# format: <iso_utc>\\t<decision>\\t<op>\\t<deputy>\\t<target>\\t<reason>\n")
        _atomic_write(target_path, cur + line + "\n")


def journal_read(precinct: str) -> str:
    with contextlib.suppress(FileNotFoundError):
        return journal_path(precinct).read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Phase D: the SYSTEM-level precinct-lifecycle journal (records/sheriff_lifecycle.log).
# Precinct create / delete / restore / purge are DIRECTORY-level events, not
# within-precinct edits -- and a delete's audit must SURVIVE the precinct itself
# moving to trash (a per-precinct journal would either vanish into trash or, worse,
# recreate the just-deleted precinct dir). So the whole delete saga (await-confirm ->
# confirmed/cancelled, plus purge) is journaled here at the records root instead.
# ---------------------------------------------------------------------------
def lifecycle_journal_path() -> Path:
    return _records_root() / "sheriff_lifecycle.log"


def lifecycle_journal_append(precinct, deputy, op, decision, reason, *, ts=None,
                             extra: str = "") -> None:
    """Append one audit line to the system precinct-lifecycle journal. LOCK + atomic.
    Line: <iso-utc>\\t<decision>\\t<op>\\t<precinct>\\t<deputy>\\t<reason>[\\t<extra>]."""
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))

    def _clean(v):
        return re.sub(r"\s+", " ", str(v).replace("\t", " ")).strip()

    line = "\t".join(_clean(x) for x in (when, decision, op, precinct, deputy, reason))
    if extra:
        line += "\t" + _clean(extra)
    target_path = lifecycle_journal_path()
    with _exclusive_lock(target_path):
        cur = ""
        with contextlib.suppress(FileNotFoundError):
            cur = target_path.read_text(encoding="utf-8")
        if cur and not cur.endswith("\n"):
            cur += "\n"
        if not cur:
            cur = ("# sheriff precinct-lifecycle journal (Case 384d)\n"
                   "# format: <iso_utc>\\t<decision>\\t<op>\\t<precinct>\\t<deputy>\\t<reason>\n")
        _atomic_write(target_path, cur + line + "\n")


def lifecycle_journal_read() -> str:
    with contextlib.suppress(FileNotFoundError):
        return lifecycle_journal_path().read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Phase D (Case 384d): the delete-confirmation subtree.
#   The sheriff APPROVES the intent to delete + writes an AWAITING record keyed by a
#   fresh TOKEN, then emails the user "reply YES ... [sheriff-confirm:<token>]". The
#   inbox router MECHANICALLY matches a YES/NO reply carrying that token and drops a
#   CONFIRMATION record (also keyed by token). The sheriff's zero-API confirm pass
#   joins the two: a YES -> perform the soft-delete; a NO or a timeout -> auto-cancel.
#   Keying everything by an opaque token (not the request id / subject text) is what
#   keeps it robust: a stray reply with no live token is simply ignored.
# ---------------------------------------------------------------------------
def _awaiting_dir(*, create: bool = False) -> Path:
    d = _queue_root() / CONFIRM_AWAITING_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _confirm_dir(*, create: bool = False) -> Path:
    d = _queue_root() / CONFIRM_REPLIES_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def new_token() -> str:
    """A fresh opaque confirmation token (the delete's one-time secret)."""
    return secrets.token_hex(8)


def write_awaiting(token: str, record: dict) -> Path:
    """Write/replace the sheriff's AWAITING-confirmation record for ``token``."""
    p = _awaiting_dir(create=True) / f"{token}.json"
    with _exclusive_lock(p):
        _atomic_write(p, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return p


def read_awaiting(token: str):
    if not token:
        return None
    return _read_json(_awaiting_dir() / f"{token}.json")


def list_awaiting() -> list:
    d = _awaiting_dir()
    return sorted(d.glob("*.json")) if d.is_dir() else []


def remove_awaiting(token: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(_awaiting_dir() / f"{token}.json")


def record_confirmation(token: str, answer: str, *, uid=None, ts=None) -> Path:
    """Drop the user's YES/NO for ``token`` (router-written). LOCK + atomic."""
    p = _confirm_dir(create=True) / f"{token}.json"
    rec = {"token": token, "answer": str(answer).strip().lower(),
           "uid": uid, "ts": ts if ts is not None else time.time()}
    with _exclusive_lock(p):
        _atomic_write(p, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    return p


def read_confirmation(token: str):
    if not token:
        return None
    return _read_json(_confirm_dir() / f"{token}.json")


def remove_confirmation(token: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(_confirm_dir() / f"{token}.json")


def parse_confirm_token(subject: str = "", body: str = "") -> str:
    """Extract a ``sheriff-confirm:<token>`` token from a reply's subject or body,
    else ''. The sheriff stamps it in the subject; matching either field is robust
    to mail clients that rewrite the subject."""
    m = CONFIRM_TAG_RE.search((subject or "") + "\n" + (body or ""))
    return m.group(1).lower() if m else ""


def _dequote(body: str) -> str:
    """The user's OWN reply text: drop quoted original lines ('>' / 'On … wrote:')
    so a YES/NO is read from what the user actually typed, not the quoted email."""
    lines = []
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^On .*wrote:$", s):
            break
        lines.append(ln)
    return "\n".join(lines)


def parse_confirm_answer(body: str = "") -> str:
    """Classify the user's reply as 'yes' | 'no' | 'unclear'. FAIL-CLOSED: a clear
    NO (or any ambiguity) never deletes -- only a clear affirmative confirms. NO
    words win over YES words so a mixed reply cancels rather than deletes."""
    top = _dequote(body)[:400].lower()
    has_no = re.search(r"\b(no|cancel|canceled|cancelled|don'?t|do not|stop|abort|keep)\b", top)
    has_yes = re.search(r"\b(yes|confirm|confirmed|delete|proceed|go ahead|approve|approved)\b", top)
    if has_no:
        return "no"
    if has_yes:
        return "yes"
    return "unclear"


def confirm_check(subject: str = "", body: str = "", *, uid=None) -> dict:
    """The router's mechanical hook (Phase D). If a reply carries a live delete-
    confirmation token AND a CLEAR yes/no, record the confirmation and report it
    INTERCEPTED (the router marks the mail seen and does NOT route it to a worker).
    Otherwise report a PASSTHROUGH -- a reply with no token, an unknown/expired
    token, or an unclear answer routes normally, so a stray reply never wedges the
    queue and a delete is never triggered by anything but an explicit YES."""
    token = parse_confirm_token(subject, body)
    if not token:
        return {"intercepted": False, "reason": "no-token"}
    awaiting = read_awaiting(token)
    if not isinstance(awaiting, dict):
        return {"intercepted": False, "token": token, "reason": "no-live-awaiting"}
    answer = parse_confirm_answer(body)
    if answer not in ("yes", "no"):
        return {"intercepted": False, "token": token, "reason": "unclear-answer"}
    record_confirmation(token, answer, uid=uid)
    return {"intercepted": True, "token": token, "answer": answer,
            "precinct": awaiting.get("precinct")}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="scratch_sheriff_request.py",
        description="Deputy->sheriff request queue for sheriff-only records ops (Case 384a).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("request", help="post a sheriff-only op request to the queue")
    r.add_argument("--op", required=True, choices=list(OPS))
    r.add_argument("--precinct", default="", help="the deputy's precinct (blank for a create/delete hand-off; use --target)")
    r.add_argument("--deputy", default="", help="requesting deputy (omit for a receptionist hand-off)")
    r.add_argument("--session", default="", help="the requesting deputy's own spawn session id")
    r.add_argument("--reason", required=True)
    r.add_argument("--target", default="", help="task# (log_remove) / precinct (ledger_modify, delete) / name (create)")
    r.add_argument("--new-content", dest="new_content", default=None)
    r.add_argument("--new-content-file", dest="new_content_file", default=None)
    r.add_argument("--drop-contains", dest="drop_contains", default=None)
    r.add_argument("--description", default=None, help="precinct_create: description")
    r.add_argument("--mode", default=None, choices=["mutable", "fixed"], help="precinct_create: ledger mode")
    r.add_argument("--model", default=None, help="precinct_create: default model")
    r.add_argument("--case-description", dest="case_description", default=None,
                   help="case_number: short description of the new case")
    r.add_argument("--critic-prompt", dest="critic_prompt", default=None,
                   help="critic_add/critic_update: the judge's CUSTOM prompt (persona) text")
    r.add_argument("--critic-prompt-file", dest="critic_prompt_file", default=None,
                   help="critic_add/critic_update: read the custom prompt from a file")
    r.add_argument("--critic-display-name", dest="critic_display_name", default=None,
                   help="critic_add/critic_update: human-readable name")
    r.add_argument("--origin", default=None,
                   help="'receptionist' for a user-authorized precinct create/delete/restore hand-off")
    r.add_argument("--requester", default=None,
                   help="the requesting user's email (required for a receptionist hand-off)")
    r.add_argument("--force", action="store_true",
                   help="precinct_delete: bypass the active-deputy/non-empty-case interlock "
                        "(the emailed YES confirmation is STILL required)")
    r.add_argument("--wait", type=float, default=0.0,
                   help="poll for the decision up to N seconds (<=4s cadence); prints the final record")

    g = sub.add_parser("get", help="print a request record (any state) as JSON")
    g.add_argument("--id", required=True)

    s = sub.add_parser("status", help="print a request's state (pending|done|denied) + result/reason")
    s.add_argument("--id", required=True)

    v = sub.add_parser("verify", help="print ok|forged for a (deputy, session) pair")
    v.add_argument("--deputy", required=True)
    v.add_argument("--session", required=True)

    sub.add_parser("pending", help="count pending requests")

    c = sub.add_parser("confirm-check",
                       help="Phase D: mechanically match a YES/NO reply to a live delete-confirm "
                            "token; prints 'INTERCEPTED <token> <answer>' or 'PASSTHROUGH <reason>'")
    c.add_argument("--subject", default="")
    c.add_argument("--body", default=None)
    c.add_argument("--bodyfile", default=None, help="read the reply body from a file")
    c.add_argument("--uid", default=None)

    a = ap.parse_args(argv)
    if a.cmd == "request":
        extra = {}
        if a.new_content_file:
            extra["new_content"] = Path(a.new_content_file).read_text(encoding="utf-8")
        elif a.new_content is not None:
            extra["new_content"] = a.new_content
        if a.critic_prompt_file:
            extra["critic_prompt"] = Path(a.critic_prompt_file).read_text(encoding="utf-8")
        elif a.critic_prompt is not None:
            extra["critic_prompt"] = a.critic_prompt
        for k in ("drop_contains", "description", "mode", "model", "case_description",
                  "critic_display_name"):
            val = getattr(a, k)
            if val is not None:
                extra[k] = val
        rec = submit(a.op, a.precinct, a.deputy, a.session, a.reason, target=a.target,
                     origin=a.origin, requester=a.requester, force=a.force, **extra)
        rid = rec["id"]
        if a.wait and a.wait > 0:
            deadline = time.time() + a.wait
            while time.time() < deadline:
                st = status(rid)
                if st["state"] in ("done", "denied"):
                    print(json.dumps(st))
                    return 0
                time.sleep(min(4.0, max(0.5, deadline - time.time())))
            print(json.dumps(status(rid)))
            return 0
        print(rid)
    elif a.cmd == "get":
        rec = get(a.id)
        print(json.dumps(rec, indent=2) if rec is not None else "null")
        return 0 if rec is not None else 1
    elif a.cmd == "status":
        print(json.dumps(status(a.id)))
    elif a.cmd == "verify":
        ok = verify_identity(a.deputy, a.session)
        print("ok" if ok else "forged")
        return 0 if ok else 1
    elif a.cmd == "pending":
        print(len(list_pending()))
    elif a.cmd == "confirm-check":
        body = a.body
        if body is None and a.bodyfile:
            with contextlib.suppress(OSError):
                body = Path(a.bodyfile).read_text(encoding="utf-8", errors="replace")
        res = confirm_check(a.subject or "", body or "", uid=a.uid)
        if res.get("intercepted"):
            print(f"INTERCEPTED {res['token']} {res['answer']}")
            return 0
        print(f"PASSTHROUGH {res.get('reason', 'no-token')}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
