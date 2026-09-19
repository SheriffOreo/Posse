#!/usr/bin/env python3
"""Case 761: DOCKET — standing work that rides out on a schedule.

A *entry* is a saved spec for work that should happen again and again: a single
case in one precinct, or a whole JTF. It holds everything the dashboard's
"Create new case" / "New JTF" forms collect (precinct, work split, judge,
composition, prompt) plus a schedule. When a entry comes due this module drops a
BRAND-NEW pending record for the existing bridges — ``web_cases/pending/`` for a
case, ``jtf/pending/`` for a JTF — so the inbox loop spawns FRESH deputies from
the spec. Nothing is ever resumed or reused from a previous run; a entry is a
recipe, not a worker.

  scratch_docket.py run          <- cron, every minute: fire whatever is due
  scratch_docket.py list --json  <- what the dashboard's Docket tab renders
  scratch_docket.py save         <- create/edit (JSON object on stdin)
  scratch_docket.py pause|resume|cancel --id <id>

This module OWNS the store (``scratch_full_logs/docket/``). The dashboard reads
those files directly and delegates every write here, so validation of precincts,
models, services and judges lives in exactly one place.

This replaces the hard-wired scratch_weekly_slides_kickoff.sh flow. That script
and its three cron lines are left installed but inert (the Case-638
``weekly_slides_PAUSED`` marker makes all three no-ops), so nothing can double-fire
once the imported Weekly-slides entry is resumed. Its two safety-net pokes are not
reimplemented here: per Feng (uid=1379) the deadlines they policed now live in the
entry's own prompt, which is the LEAD's to hit.

Env overrides, for tests: ``TSOMP_DOCKET_ROOT`` (the store),
``TSOMP_WEBCASES_ROOT`` and ``TSOMP_JTF_ROOT`` (where a fired entry drops its
record — same variables scratch_web_case.py / scratch_jtf.py read).
"""
import argparse
import calendar
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_REQUESTER = os.environ.get("INFRA_OPERATOR_EMAIL", "")   # operator's address

# A due time missed by more than this is recorded as missed and skipped, never
# fired late: waking the Saturday-09:00 slides JTF on Tuesday because the box was
# off all weekend is worse than not running it at all.
MISSED_GRACE_SEC = 6 * 3600

FREQS = ("once", "hourly", "daily", "weekly", "monthly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")   # index = datetime.weekday()
KINDS = ("case", "jtf")
MAX_RUNS_KEPT = 20
# Bound on stepping a stale schedule forward; 500 covers years of any frequency.
MAX_CATCHUP_STEPS = 500


def _root() -> Path:
    env = os.environ.get("TSOMP_DOCKET_ROOT")
    return Path(env) if env else REPO_ROOT / "scratch_full_logs" / "docket"


def _webcases_pending() -> Path:
    """Drop dir for a fired case entry. Same env var and default as
    scratch_web_case.py, which owns this directory contract."""
    env = os.environ.get("TSOMP_WEBCASES_ROOT")
    root = Path(env) if env else REPO_ROOT / "scratch_full_logs" / "web_cases"
    return root / "pending"


def _jtf_pending() -> Path:
    """Drop dir for a fired JTF entry. Same env var and default as
    scratch_jtf.py, which owns this directory contract."""
    env = os.environ.get("TSOMP_JTF_ROOT")
    root = Path(env) if env else REPO_ROOT / "scratch_full_logs" / "jtf"
    return root / "pending"


def _dirs() -> dict:
    root = _root()
    d = {"root": root, "cancelled": root / "cancelled"}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _write_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock(block=True):
    """One writer/runner at a time. The cron runner and a dashboard write must not
    interleave: both read-modify-write the same record files.

    The runner asks for it non-blocking (yields False instead of waiting), because
    it is started every minute: if a pass ever stalled, blocking here would pile up
    one waiting process per minute until the box ran out of them."""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / ".lock", "w")
    held = False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX if block else fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
        yield True
    except BlockingIOError:
        yield False
    finally:
        if held:
            with contextlib.suppress(Exception):
                fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #
def _hhmm(at) -> tuple:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(at or ""))
    if not m:
        raise ValueError(f"time must be HH:MM, got {at!r}")
    hh, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mi <= 59):
        raise ValueError(f"time out of range: {at!r}")
    return hh, mi


def next_occurrence(entry, after):
    """First fire time strictly after `after` (epoch seconds, local time), or None
    when the schedule has no further occurrence (a spent `once`)."""
    freq = str(entry.get("freq") or "")
    if freq == "once":
        start = entry.get("start")
        return float(start) if start and float(start) > after else None
    hh, mi = _hhmm(entry.get("at"))
    base = datetime.fromtimestamp(after)
    if freq == "hourly":
        cand = base.replace(minute=mi, second=0, microsecond=0)
        if cand.timestamp() <= after:
            cand += timedelta(hours=1)
        return cand.timestamp()
    if freq == "daily":
        cand = base.replace(hour=hh, minute=mi, second=0, microsecond=0)
        if cand.timestamp() <= after:
            cand += timedelta(days=1)
        return cand.timestamp()
    if freq == "weekly":
        dow = int(entry.get("dow", 0))
        cand = base.replace(hour=hh, minute=mi, second=0, microsecond=0)
        cand += timedelta(days=(dow - cand.weekday()) % 7)
        if cand.timestamp() <= after:
            cand += timedelta(days=7)
        return cand.timestamp()
    if freq == "monthly":
        dom = int(entry.get("dom", 1))
        year, month = base.year, base.month
        # A month shorter than `dom` clamps to its last day, so "the 31st" still
        # runs in February instead of silently skipping four months a year.
        for _ in range(14):
            cand = datetime(year, month, min(dom, calendar.monthrange(year, month)[1]), hh, mi)
            if cand.timestamp() > after:
                return cand.timestamp()
            month += 1
            if month > 12:
                month, year = 1, year + 1
        return None
    raise ValueError(f"unknown frequency {freq!r}")


def schedule_label(entry) -> str:
    """Human phrasing of the repeat, e.g. 'every Saturday at 09:00'."""
    freq = str(entry.get("freq") or "")
    at = str(entry.get("at") or "")
    if freq == "once":
        start = entry.get("start")
        return ("once, " + datetime.fromtimestamp(float(start)).strftime("%Y-%m-%d %H:%M")
                if start else "once (no date set)")
    if freq == "hourly":
        return f"every hour at :{at.split(':')[-1]}"
    if freq == "daily":
        return f"every day at {at}"
    if freq == "weekly":
        return f"every {WEEKDAYS[int(entry.get('dow', 0)) % 7]} at {at}"
    if freq == "monthly":
        return f"day {int(entry.get('dom', 1))} of each month at {at}"
    return freq or "(no schedule)"


# --------------------------------------------------------------------------- #
# prompt placeholders
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(r"\{d([+-]\d+)?:([^{}]*)\}")
_NAMED = {"{date}": "%Y-%m-%d", "{time}": "%H:%M",
          "{datetime}": "%Y-%m-%d %H:%M", "{weekday}": "%A"}


def render_prompt(prompt, when) -> str:
    """Substitute the date placeholders in a entry prompt at fire time.

    ``{date} {time} {datetime} {weekday}`` and the general ``{d:FMT}`` /
    ``{d+N:FMT}`` / ``{d-N:FMT}`` (strftime of the fire date, shifted N days) —
    which is how the weekly-slides spec still says "Saturday Sep 19" and names
    the Sunday deadline. Braces that match nothing are left untouched, so a
    prompt containing literal JSON or LaTeX survives unharmed."""
    dt = datetime.fromtimestamp(when)
    out = str(prompt or "")
    for token, fmt in _NAMED.items():
        out = out.replace(token, dt.strftime(fmt))
    return _PLACEHOLDER.sub(
        lambda m: (dt + timedelta(days=int(m.group(1) or 0))).strftime(m.group(2)), out)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def known_precincts() -> set:
    try:
        import scratch_jtf
        return scratch_jtf.known_precincts()
    except Exception:
        return set()


def known_judges() -> set:
    try:
        import scratch_critic
        return {c["id"] for c in scratch_critic.list_critics()}
    except Exception:
        return set()


def _models():
    try:
        import scratch_models
        return scratch_models
    except Exception:
        return None


def _check_lane(lanes, label, model_key, service_key, is_mode=False):
    """Validate one service+model pair. An empty pair means 'inherit', which is
    what an untouched form control posts. The WORK lane posts a MODE id rather
    than a service (the create-case form's contract), so `is_mode` says which
    vocabulary the service value is checked against."""
    sm = _models()
    if sm is None:
        return
    model, service = lanes.get(model_key) or "", lanes.get(service_key) or ""
    valid = sm.MODE_IDS if is_mode else sm.SERVICE_IDS
    if service and service not in valid:
        raise ValueError(f"invalid {label} service {service!r}")
    if model and model not in sm.ALL_ALIASES:
        raise ValueError(f"invalid {label} model {model!r}")
    if model and service:
        svc = sm.deputy_service(service) if is_mode else service
        if sm.service_of(model) != svc:
            raise ValueError(f"{label} model {model!r} does not belong to service {svc!r}")


def _clean_slot(raw, precincts, role):
    """Normalize one JTF slot to {kind,name[,lanes]}. A precinct slot is a fresh
    case there and carries its own work split; a specific deputy keeps its own
    configured model, so a lane on one is rejected rather than quietly dropped."""
    if not isinstance(raw, dict):
        raise ValueError(f"{role} must be an object")
    kind = str(raw.get("kind") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    if kind not in ("precinct", "deputy") or not name:
        raise ValueError(f"{role} must name a precinct or a specific deputy")
    if kind == "precinct" and precincts and name not in precincts:
        raise ValueError(f"unknown precinct {name!r} for {role}")
    out = {"kind": kind, "name": name}
    lanes = {k: str(raw.get(k) or "").strip().lower()
             for k in ("service", "model", "report_service", "report_model")}
    if kind == "deputy":
        if any(lanes.values()):
            raise ValueError(f"{role} is a specific deputy and keeps its own model")
        return out
    _check_lane(lanes, f"{role} work", "model", "service", is_mode=True)
    _check_lane(lanes, f"{role} report", "report_model", "report_service")
    out.update({k: v for k, v in lanes.items() if v})
    return out


def validate(raw, existing=None) -> dict:
    """Turn a submitted entry into the record we store, or raise ValueError with
    a message meant for the operator. `existing` is the record being edited, so a
    partial edit keeps the fields it did not send."""
    base = dict(existing or {})
    data = dict(base)
    data.update({k: v for k, v in (raw or {}).items() if k not in ("id", "runs", "created")})

    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("a entry needs a name")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("a prompt is required — it is what the deputy is told to do")

    freq = str(data.get("freq") or "").strip().lower()
    if freq not in FREQS:
        raise ValueError(f"frequency must be one of {', '.join(FREQS)}")
    out = {"id": base.get("id"), "name": name, "kind": kind, "prompt": prompt,
           "freq": freq, "paused": bool(data.get("paused", base.get("paused", False))),
           "requester": str(data.get("requester") or base.get("requester")
                            or DEFAULT_REQUESTER),
           "created": base.get("created") or time.time(),
           "created_by": base.get("created_by") or str(data.get("created_by") or "dashboard"),
           "runs": list(base.get("runs") or [])}
    if freq == "once":
        start = data.get("start")
        if not start:
            raise ValueError("a one-off entry needs a date and time")
        out["start"] = float(start)
        out["at"] = datetime.fromtimestamp(out["start"]).strftime("%H:%M")
    else:
        hh, mi = _hhmm(data.get("at"))
        out["at"] = f"{hh:02d}:{mi:02d}"
    if freq == "weekly":
        dow = int(data.get("dow", 0))
        if not 0 <= dow <= 6:
            raise ValueError("weekday must be 0 (Monday) .. 6 (Sunday)")
        out["dow"] = dow
    if freq == "monthly":
        dom = int(data.get("dom", 1))
        if not 1 <= dom <= 31:
            raise ValueError("day of month must be 1..31")
        out["dom"] = dom

    precincts, judges = known_precincts(), known_judges()
    judge = str(data.get("judge") or "").strip().lower()
    if judge and judges and judge not in judges:
        raise ValueError(f"unknown judge {judge!r}")
    out["judge"] = judge
    judge_lanes = {"judge_model": str(data.get("judge_model") or "").strip().lower(),
                   "judge_service": str(data.get("judge_service") or "").strip().lower()}
    _check_lane(judge_lanes, "judge", "judge_model", "judge_service")
    out.update(judge_lanes)

    if kind == "case":
        precinct = str(data.get("precinct") or "").strip()
        if not precinct:
            raise ValueError("a case entry needs a precinct")
        if precincts and precinct not in precincts:
            raise ValueError(f"unknown precinct {precinct!r}")
        out["precinct"] = precinct
        lanes = {k: str(data.get(k) or "").strip().lower()
                 for k in ("service", "model", "report_service", "report_model")}
        _check_lane(lanes, "work", "model", "service", is_mode=True)
        _check_lane(lanes, "report", "report_model", "report_service")
        out.update(lanes)
    else:
        out["lead"] = _clean_slot(data.get("lead"), precincts, "lead")
        collabs = data.get("collaborators")
        if not isinstance(collabs, list) or not collabs:
            raise ValueError("a JTF entry needs at least one collaborator")
        out["collaborators"] = [_clean_slot(c, precincts, f"collaborator C{i + 1}")
                                for i, c in enumerate(collabs)]

    out["updated"] = time.time()
    out["next_run"] = next_occurrence(out, time.time())
    return out


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path_for(pid) -> Path:
    if not _ID_RE.match(str(pid or "")):
        raise ValueError(f"bad entry id {pid!r}")
    return _root() / f"{pid}.json"


def load(pid):
    try:
        return json.loads(_path_for(pid).read_text())
    except Exception:
        return None


def entries() -> list:
    """Every live entry (cancelled ones live in cancelled/ and are not listed),
    soonest first, with the spent and paused ones after the scheduled ones."""
    out = []
    root = _root()
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        with contextlib.suppress(Exception):
            rec = json.loads(path.read_text())
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
    out.sort(key=lambda r: (bool(r.get("paused")), r.get("next_run") is None,
                            r.get("next_run") or 0))
    return out


def save(raw) -> dict:
    """Create (no id) or edit (id of a live entry) one entry."""
    with _lock():
        _dirs()
        pid = str((raw or {}).get("id") or "").strip()
        existing = load(pid) if pid else None
        if pid and existing is None:
            raise ValueError(f"no such entry {pid!r}")
        rec = validate(raw, existing)
        rec["id"] = pid or (time.strftime("p%Y%m%d%H%M%S") + "_" + os.urandom(3).hex())
        _write_atomic(_path_for(rec["id"]), rec)
        return rec


def set_paused(pid, paused) -> dict:
    with _lock():
        rec = load(pid)
        if rec is None:
            raise ValueError(f"no such entry {pid!r}")
        rec["paused"] = bool(paused)
        rec["updated"] = time.time()
        if not paused:
            # Resuming re-arms from NOW, so a entry stood down for a month does
            # not wake up owing a run it can no longer usefully do.
            rec["next_run"] = next_occurrence(rec, time.time())
            if rec["next_run"] is None:
                raise ValueError("this one-off entry's date has passed — "
                                 "set a new date and time, then resume it")
        _write_atomic(_path_for(pid), rec)
        return rec


def cancel(pid) -> dict:
    """Retire a entry. The record is MOVED to cancelled/, never deleted, so the
    spec and its run history stay readable."""
    with _lock():
        dirs = _dirs()
        rec = load(pid)
        if rec is None:
            raise ValueError(f"no such entry {pid!r}")
        rec["cancelled_at"] = time.time()
        rec["next_run"] = None
        _write_atomic(dirs["cancelled"] / f"{pid}.json", rec)
        with contextlib.suppress(FileNotFoundError):
            os.remove(_path_for(pid))
        return rec


# --------------------------------------------------------------------------- #
# firing
# --------------------------------------------------------------------------- #
def _allocate_case():
    try:
        import scratch_case_seq
        return scratch_case_seq.allocate()
    except Exception:
        return None       # the bridge allocates its own when the record carries none


def _case_record(rec, sid, prompt, case):
    """The web-case record scratch_web_case.py consumes — the same shape the
    dashboard's Create-new-case POST drops."""
    return {"id": sid, "ts": time.time(), "precinct": rec["precinct"],
            "model": rec.get("model") or None, "service": rec.get("service") or None,
            "writer_model": None, "parent": None,
            "description": prompt, "files": [], "case": case,
            "critic": rec.get("judge") or None,
            "report_model": rec.get("report_model") or None,
            "report_service": rec.get("report_service") or None,
            "judge_model": rec.get("judge_model") or None,
            "judge_service": rec.get("judge_service") or None,
            "source": "docket", "requester": rec.get("requester") or DEFAULT_REQUESTER,
            "entry": {"id": rec["id"], "name": rec["name"]}}


def _jtf_record(rec, sid, prompt):
    """The JTF record scratch_jtf.py materializes — the same shape the dashboard's
    POST /api/jtf drops."""
    return {"id": sid, "ts": time.time(),
            "lead": rec["lead"], "collaborators": rec["collaborators"],
            "critic": rec.get("judge") or "",
            "judge_model": rec.get("judge_model") or None,
            "judge_service": rec.get("judge_service") or None,
            "description": prompt,
            "source": "docket", "requester": rec.get("requester") or DEFAULT_REQUESTER,
            "entry": {"id": rec["id"], "name": rec["name"]}}


def fire(rec, when, dry=False) -> dict:
    """Queue ONE fresh run of `rec`. Drops a pending record for the bridge that
    owns this kind of work; the inbox loop picks it up within its 30 s pass and
    spawns brand-new deputies. Nothing from a previous run is reused."""
    sid = time.strftime("%Y%m%d%H%M%S", time.localtime(when)) + "_" + os.urandom(4).hex()
    prompt = render_prompt(rec.get("prompt"), when)
    if rec["kind"] == "case":
        case = None if dry else _allocate_case()
        record, pend = _case_record(rec, sid, prompt, case), _webcases_pending()
    else:
        case, record, pend = None, _jtf_record(rec, sid, prompt), _jtf_pending()
    run = {"ts": when, "sid": sid, "status": "queued", "case": case}
    if dry:
        run["status"] = "dry"
        print(f"[dry-fire] {rec['id']} ({rec['kind']}) -> {pend / (sid + '.json')}")
        return run
    pend.mkdir(parents=True, exist_ok=True)
    _write_atomic(pend / f"{sid}.json", record)
    return run


def _after_the_consumed_slot(rec, now):
    """The next scheduled time once a manual run has consumed the upcoming slot.

    Advances from the slot the entry was ALREADY showing, not from the click, so
    the schedule keeps its original phase: a Saturday-09:00 entry run by hand on
    Thursday moves to the FOLLOWING Saturday rather than firing again two days
    later. Keeps stepping while the candidate is already past, which is what a
    stale next_run (box asleep over several occurrences) would otherwise leave
    behind. Returns None for a spent one-off, which has no next time."""
    cand = rec.get("next_run")
    if not cand:
        return None
    cand = next_occurrence(rec, float(cand))
    for _ in range(MAX_CATCHUP_STEPS):
        if cand is None or cand > now:
            return cand
        cand = next_occurrence(rec, cand)
    return cand


def run_now(pid) -> dict:
    """Fire one entry immediately, by hand, and consume its upcoming slot.

    The record dropped is the same shape the scheduler drops, so the manual run
    spawns fresh deputies through the same bridges; only the run's `status` marks
    it as manual. A paused entry fires and stays paused — it has no live schedule
    to advance, and un-pausing on a one-off click would be a decision the operator
    did not make."""
    with _lock() as mine:
        if not mine:
            raise ValueError("the docket is busy, try again in a moment")
        rec = load(pid)
        if rec is None:
            raise ValueError(f"no such entry {pid!r}")
        now = time.time()
        run = dict(fire(rec, now), status="manual")
        rec["runs"] = ([run] + list(rec.get("runs") or []))[:MAX_RUNS_KEPT]
        rec["last_run"] = run
        if not rec.get("paused"):
            rec["next_run"] = _after_the_consumed_slot(rec, now)
        rec["updated"] = now
        _write_atomic(_path_for(pid), rec)
        return rec


def run_due(now=None, dry=False) -> list:
    """Fire every entry due at `now`, then re-arm it. Returns one row per entry
    acted on. A due time older than MISSED_GRACE_SEC is recorded as missed and
    skipped rather than fired late."""
    now = time.time() if now is None else float(now)
    acted = []
    with _lock(block=False) as mine:
        if not mine:
            return acted
        for rec in entries():
            due = rec.get("next_run")
            if rec.get("paused") or not due or float(due) > now:
                continue
            due = float(due)
            if now - due > MISSED_GRACE_SEC:
                run = {"ts": due, "sid": "", "status": "missed", "case": None}
            else:
                try:
                    run = fire(rec, due, dry=dry)
                except Exception as ex:
                    run = {"ts": due, "sid": "", "status": "error",
                           "case": None, "note": str(ex)}
            if dry:
                acted.append({"id": rec["id"], "name": rec["name"], **run})
                continue
            rec["runs"] = ([run] + list(rec.get("runs") or []))[:MAX_RUNS_KEPT]
            rec["last_run"] = run
            rec["next_run"] = next_occurrence(rec, now)
            rec["updated"] = time.time()
            _write_atomic(_path_for(rec["id"]), rec)
            acted.append({"id": rec["id"], "name": rec["name"], **run})
    return acted


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_ts(ts):
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _print_table(rows):
    if not rows:
        print("no entries")
        return
    for r in rows:
        state = "paused" if r.get("paused") else ("done" if not r.get("next_run") else "active")
        print(f"{r['id']:26s} {state:7s} {r['kind']:4s} {schedule_label(r):34s} "
              f"next {_fmt_ts(r.get('next_run')):16s} {r['name']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_docket.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="every live entry")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one entry")
    p.add_argument("--id", required=True)
    p = sub.add_parser("save", help="create (no id) or edit (with id); JSON on stdin")
    p.add_argument("--file", help="read the JSON from this file instead of stdin")
    for op in ("pause", "resume", "cancel"):
        p = sub.add_parser(op)
        p.add_argument("--id", required=True)
    p = sub.add_parser("run-now", help="fire one entry by hand and consume its next slot")
    p.add_argument("--id", required=True)
    p = sub.add_parser("run", help="fire whatever is due (this is the cron entry point)")
    p.add_argument("--now", type=float, default=None, help="pretend it is this epoch")
    p.add_argument("--dry", action="store_true", help="report, drop nothing")
    p = sub.add_parser("due", help="what run would fire, without firing it")
    p.add_argument("--now", type=float, default=None)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "list":
            rows = entries()
            if args.json:
                # the phrasing of a repeat is a display concern, and the dashboard
                # must not re-implement the schedule maths to render it.
                rows = [dict(r, schedule_label=schedule_label(r)) for r in rows]
                print(json.dumps({"ok": True, "docket": rows}, indent=2))
            else:
                _print_table(rows)
            return 0
        if args.cmd == "show":
            rec = load(args.id)
            if rec is None:
                print(json.dumps({"ok": False, "error": f"no such entry {args.id!r}"}))
                return 1
            print(json.dumps({"ok": True, "entry": rec}, indent=2))
            return 0
        if args.cmd == "save":
            text = Path(args.file).read_text() if args.file else sys.stdin.read()
            rec = save(json.loads(text))
            print(json.dumps({"ok": True, "entry": rec}, indent=2))
            return 0
        if args.cmd in ("pause", "resume"):
            rec = set_paused(args.id, args.cmd == "pause")
            print(json.dumps({"ok": True, "entry": rec}, indent=2))
            return 0
        if args.cmd == "cancel":
            rec = cancel(args.id)
            print(json.dumps({"ok": True, "entry": rec}, indent=2))
            return 0
        if args.cmd == "run-now":
            rec = run_now(args.id)
            print(json.dumps({"ok": True, "entry": rec}, indent=2))
            return 0
        if args.cmd == "due":
            now = time.time() if args.now is None else args.now
            rows = [{"id": r["id"], "name": r["name"], "kind": r["kind"],
                     "next_run": r.get("next_run")}
                    for r in entries()
                    if not r.get("paused") and r.get("next_run")
                    and float(r["next_run"]) <= now]
            print(json.dumps({"ok": True, "due": rows}, indent=2))
            return 0
        acted = run_due(now=args.now, dry=args.dry)   # args.cmd == "run"
        for row in acted:                     # silent when nothing fired: this runs every minute
            print(f"{datetime.now():%F %T} fired {row['id']} ({row['name']}) "
                  f"status={row['status']} sid={row.get('sid') or '-'} "
                  f"case={row.get('case') or '-'}")
        return 0
    except Exception as ex:
        print(json.dumps({"ok": False, "error": str(ex)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
