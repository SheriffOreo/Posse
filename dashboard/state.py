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
import models          # Case 561: alias -> label/service for the live model column

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
_SUBJ_TASK = re.compile(r"\b(?:task|case)[ _]?#?(\d+)", re.I)  # Case 427: "Case N" too

# Mechanical acks the router / web-case bridge send on the user's behalf — never
# part of the substantive case conversation (Case 427). Two shapes:
#   * creation ack:  subject "Case <n> created|assigned (<precinct>): ..."
#   * router ack:    body opens with "Auto-ack from the inbox router: ..."
_ACK_SUBJECT = re.compile(r"^\s*(?:re:\s*)?case\s+\d+\s+(?:created|assigned)\s*\(", re.I)


def is_autoack(subject, body):
    """True for a mechanical acknowledgement (case-creation echo or inbox-router
    auto-ack) that should be hidden from a case's conversation + deliverables."""
    if _ACK_SUBJECT.search(subject or ""):
        return True
    return "auto-ack from the inbox router" in (body or "").lower()


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
# precincts (Task 372 — the Sheriff & Deputies records room)
# --------------------------------------------------------------------------- #
def _read_text(p, default=""):
    try:
        return Path(p).read_text(errors="replace")
    except Exception:
        return default


def _tmux_alive(name):
    try:
        out = subprocess.run(["tmux", "ls"], capture_output=True, text=True,
                             timeout=5).stdout
        return any(line.split(":", 1)[0].strip() == name for line in out.splitlines())
    except Exception:
        return False


def _parse_case_log(text):
    """Case-log data lines -> [{task, deputy, path, summary}] (skips '#' headers).

    Task 377 #1: NEW rows have four fields <task>\\t<deputy>\\t<path>\\t<summary>;
    OLD rows (pre-377) have three <task>\\t<path>\\t<summary> and render with an
    empty deputy. Disambiguated purely by field count (all fields are tab-sanitized,
    so the count is exact)."""
    rows = []
    for ln in text.splitlines():
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) >= 4:                       # new: task, deputy, path, summary
            rows.append({"task": parts[0], "deputy": parts[1],
                         "path": parts[2], "summary": parts[3]})
        elif len(parts) == 3:                     # old: task, path, summary
            rows.append({"task": parts[0], "deputy": "",
                         "path": parts[1], "summary": parts[2]})
        elif len(parts) == 2:
            rows.append({"task": parts[0], "deputy": "",
                         "path": parts[1], "summary": ""})
    return rows


def task_precinct_map():
    d = _read_json(config.TASK_PRECINCT, {}) or {}
    return d if isinstance(d, dict) else {}


def active_deputies():
    """Task 382 #1: the active-deputies map (deputy -> {case, description, precinct})
    the Status board trusts over the launch-script defaults. Written today by the
    deputy when it takes a new case; by the sheriff in the approved redesign."""
    d = _read_json(config.ACTIVE_DEPUTIES, {}) or {}
    return d if isinstance(d, dict) else {}


def _deputies_for(name, tmap):
    """Task-number deputies whose precinct == name, newest task first.

    Task 377 #1: the WORKING DEPUTY is authoritative — prefer `deputy` (set by the
    spawn) over `agent`, and never show the lesser `handler` (triage lineage) as
    the deputy. `handler` is surfaced as its own column so the routing lineage is
    still visible, just not conflated with who did the work."""
    out = []
    for tid, e in tmap.items():
        if isinstance(e, dict) and e.get("precinct") == name:
            out.append({"task": tid,
                        "deputy": e.get("deputy") or e.get("agent"),
                        "handler": e.get("handler") or "",
                        "session": e.get("session"), "basis": e.get("basis")})
    out.sort(key=lambda r: int(r["task"]) if str(r["task"]).isdigit() else -1,
             reverse=True)
    return out


def sheriff_status():
    """Live status of the sheriff daemon: alive?, A/B (TOKENS, Task 376), whether
    API (LLM) compaction is on, the GLOBAL sheriff model (Task 384b / Phase C), and
    the last ledger actions. A/B + llm are parsed from the sheriff's own log
    ('sheriff up: ... A=.. B=.. tokens ... llm=True model=fable'); the model is read
    LIVE from the persisted global config (sheriff_config.json) so a UI change shows
    immediately without waiting for a daemon restart line."""
    alive = _tmux_alive("sheriff")
    log = _read_text(config.SHERIFF_LOG)
    lines = [l for l in log.splitlines() if l.strip()]
    A = B = None
    for l in reversed(lines):
        m = re.search(r"A=(\d+)\s+B=(\d+)", l)
        if m:
            A, B = int(m.group(1)), int(m.group(2))
            break
    llm = True   # Phase C: API compaction is the default; only an explicit llm=False overrides
    for l in reversed(lines):
        m = re.search(r"llm=(True|False)", l)
        if m:
            llm = (m.group(1) == "True")
            break
    # Global sheriff model: live from the persisted config, fable if unset/garbled.
    model = "fable"
    cfg = _read_json(config.SHERIFF_CONFIG, {}) or {}
    if isinstance(cfg, dict) and cfg.get("model") in ("fable", "opus", "sonnet", "haiku"):
        model = cfg["model"]
    actions = [l for l in lines if "COMPACTED" in l or "APPLIED" in l][-5:]
    return {"alive": alive, "A": A, "B": B, "llm": llm, "model": model, "unit": "tokens",
            "recent": lines[-1] if lines else "",
            "actions": list(reversed(actions))}


def precincts():
    """Overview of every precinct from precincts.json, augmented with live
    ledger length, case-log count, and case-file count. Cached briefly."""
    def build():
        d = _read_json(config.PRECINCTS_JSON, {}) or {}
        pr = d.get("precincts", {}) if isinstance(d, dict) else {}
        tmap = task_precinct_map()
        out = []
        for name in sorted(pr):
            e = pr[name] if isinstance(pr[name], dict) else {}
            if e.get("status") == "deleted":     # Phase D: soft-deleted -> off the active list
                continue
            ledger = _read_text(config.RECORDS / name / "ledger.md")
            log = _read_text(config.RECORDS / name / "log.tsv")
            cases_dir = config.RECORDS / name / "cases"
            try:
                ncases = sum(1 for _ in cases_dir.iterdir())
            except Exception:
                ncases = 0
            out.append({
                "name": name,
                "mode": e.get("ledger_mode", "mutable"),
                "model": e.get("model", "opus"),   # Task 376: per-precinct default model
                "description": e.get("description", ""),
                "ledger_chars": len(ledger),
                "ledger_tokens": (len(ledger) + 3) // 4,
                "log_cases": len(_parse_case_log(log)),
                "case_files": ncases,
                "deputies": len(_deputies_for(name, tmap)),
            })
        return out
    return _cached("precincts", 10, build)


# ---------------------------------------------------------------------------
# Case 551: critics ("Judges"). READ-ONLY -- the registry is sheriff-owned, so the
# dashboard only renders it and files PROPOSALS onto the sheriff request queue.
# ---------------------------------------------------------------------------
def critics(include_retired=False):
    """The critic roster from records/critics.json, newest-facing fields only.
    The default critic sorts first. Never raises: a missing/corrupt registry is an
    empty roster (the create-case form then simply offers 'none')."""
    def build():
        d = _read_json(config.CRITICS_JSON, {}) or {}
        cs = d.get("critics", {}) if isinstance(d, dict) else {}
        out = []
        for cid in sorted(cs):
            e = cs[cid] if isinstance(cs[cid], dict) else {}
            status = e.get("status", "active")
            out.append({
                "id": cid,
                "display_name": e.get("display_name") or cid,
                "description": e.get("description", ""),
                "model": e.get("model") or "",
                "status": status,
                "added_by": e.get("added_by", ""),
                "added_ts": e.get("added_ts"),
                "request_id": e.get("request_id", ""),
                "prompt_chars": len(_read_text(config.RECORDS / (e.get("prompt_file") or ""))),
            })
        out.sort(key=lambda c: (c["id"] != "anonymous", c["id"]))
        return out
    all_ = _cached("critics", 10, build)
    return all_ if include_retired else [c for c in all_ if c["status"] == "active"]


def critic_charter():
    """The FIXED system prompt every critic shares."""
    return _read_text(config.CRITICS_DIR / "CHARTER.md")


def critic_prompt(cid):
    """One critic's CUSTOM (persona) prompt."""
    for c in critics(include_retired=True):
        if c["id"] == cid:
            d = _read_json(config.CRITICS_JSON, {}) or {}
            e = (d.get("critics") or {}).get(cid) or {}
            return _read_text(config.RECORDS / (e.get("prompt_file") or ""))
    return ""


def critic_requests(limit=20):
    """Recent critic_* sheriff requests across pending/done/denied, newest first --
    so the Judges tab can show that 'add a judge' really is gated on approval."""
    def build():
        out = []
        for stt in ("pending", "done", "denied"):
            d = config.SHERIFF_REQUESTS / stt
            if not d.is_dir():
                continue
            for p in d.glob("*.json"):
                r = _read_json(p, {}) or {}
                if not str(r.get("op", "")).startswith("critic_"):
                    continue
                out.append({
                    "id": r.get("id", p.stem), "state": stt, "op": r.get("op"),
                    "critic": r.get("target", ""), "deputy": r.get("deputy", ""),
                    "requester": r.get("requester", ""), "reason": r.get("reason", ""),
                    "ts": r.get("ts") or 0,
                    "decision": r.get("decision", ""),
                    "decision_reason": r.get("decision_reason") or r.get("denied_reason", ""),
                })
        out.sort(key=lambda r: r["ts"] or 0, reverse=True)
        return out
    return _cached("critic_requests", 10, build)[:limit]


def critic_reviews(limit=25):
    """Recent review rounds across all cases (newest case first) so the tab shows
    the judges actually being used, not just configured."""
    def build():
        root = config.CRITIC_REVIEWS
        if not root.is_dir():
            return []
        out = []
        for cd in root.iterdir():
            if not cd.is_dir() or not cd.name.startswith("case_"):
                continue
            case = cd.name[5:]
            rounds = []
            for rd in cd.iterdir():
                if not rd.is_dir() or not rd.name.startswith("round_"):
                    continue
                meta = _read_json(rd / "meta.json", {}) or {}
                v = _read_json(rd / "verdict.json", {}) or {}
                verdict = str(v.get("verdict", "")).upper().replace("SIGNOFF", "SIGN-OFF")
                if verdict == "SIGN-OFF" and (v.get("must_fix") or []):
                    verdict = "REVISE"      # mirror the harness's fail-closed coercion
                try:
                    n = int(rd.name[6:])
                except ValueError:
                    continue
                rounds.append({"round": n, "critic": meta.get("critic", ""),
                               "ts": meta.get("ts") or 0, "verdict": verdict,
                               "one_line": v.get("one_line", ""),
                               "must_fix": len(v.get("must_fix") or [])})
            if not rounds:
                continue
            rounds.sort(key=lambda r: r["round"])
            out.append({"case": case, "rounds": rounds,
                        "critic": rounds[-1]["critic"],
                        "latest": rounds[-1]["verdict"],
                        "signed_off": any(r["verdict"] == "SIGN-OFF" for r in rounds),
                        "ts": max(r["ts"] or 0 for r in rounds)})
        out.sort(key=lambda r: r["ts"] or 0, reverse=True)
        return out
    return _cached("critic_reviews", 15, build)[:limit]


# Case 555: a case id used to build a path under critic_reviews/. Query params are
# attacker-controlled, so a review id is whitelisted (no dots, no separators) before
# it is ever joined onto a path -- '../../etc' must never resolve.
_REVIEW_CASE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _round_dirs(case):
    """Sorted (round_no, dir) for one case's review rounds; [] if the id is bad or
    the case has never been reviewed."""
    if not _REVIEW_CASE_RE.match(str(case or "")):
        return []
    cd = config.CRITIC_REVIEWS / f"case_{case}"
    if not cd.is_dir():
        return []
    out = []
    for rd in cd.iterdir():
        if not rd.is_dir() or not rd.name.startswith("round_"):
            continue
        try:
            out.append((int(rd.name[6:]), rd))
        except ValueError:
            continue
    out.sort(key=lambda t: t[0])
    return out


def _as_finding(x):
    """Normalise one must_fix/should_fix entry. The charter asks for
    {location, problem, fix} dicts in must_fix and plain strings elsewhere, but a
    judge is an LLM: accept either shape rather than dropping the finding."""
    if isinstance(x, dict):
        return {"location": str(x.get("location", "")),
                "problem": str(x.get("problem", "") or x.get("issue", "")),
                "fix": str(x.get("fix", "") or x.get("suggestion", ""))}
    return {"location": "", "problem": str(x), "fix": ""}


def _as_lines(x):
    """should_fix / keep / unverified -> a list of display strings (a judge that
    wrote dicts there still renders)."""
    if not isinstance(x, list):
        return []
    out = []
    for i in x:
        if isinstance(i, dict):
            f = _as_finding(i)
            out.append(" — ".join(p for p in (f["location"], f["problem"], f["fix"]) if p))
        else:
            out.append(str(i))
    return [s for s in out if s.strip()]


def critic_review_detail(case):
    """Case 555: EVERY round of one case's review, in full — the structured
    verdict.json fields plus the verbatim verdict.md the judge actually wrote.

    Rounds come back in chronological order (round 1 first) so the arc reads the
    way it happened: REVISE -> fix -> SIGN-OFF. prompt.md is deliberately NOT
    inlined (20-33 KB per round); its size is reported and
    :func:`critic_review_prompt` fetches it on demand.

    Never raises: an unknown/ill-formed case id, or a round still in flight, comes
    back as an empty/pending entry rather than an error."""
    rounds = []
    for n, rd in _round_dirs(case):
        meta = _read_json(rd / "meta.json", {}) or {}
        v = _read_json(rd / "verdict.json", {}) or {}
        md = _read_text(rd / "verdict.md")
        raw = str(v.get("verdict", "")).upper().replace("SIGNOFF", "SIGN-OFF")
        must = [_as_finding(x) for x in (v.get("must_fix") or [])]
        # Mirror the harness's fail-closed coercion (scratch_critic: a SIGN-OFF
        # carrying must-fixes is a REVISE) and SAY so, so the badge can never
        # disagree with the findings printed under it.
        verdict, coerced = raw, False
        if raw == "SIGN-OFF" and must:
            verdict, coerced = "REVISE", True
        rounds.append({
            "round": n,
            "critic": meta.get("critic", ""),
            "model": meta.get("model", ""),
            "anon": meta.get("anon", ""),
            "ts": meta.get("ts") or 0,
            "artifacts": [str(a) for a in (meta.get("artifacts") or [])],
            "missing_artifacts": [str(a) for a in (meta.get("missing_artifacts") or [])],
            "verdict": verdict,
            "raw_verdict": raw,
            "coerced": coerced,
            "one_line": str(v.get("one_line", "")),
            "must_fix": must,
            "should_fix": _as_lines(v.get("should_fix")),
            "keep": _as_lines(v.get("keep")),
            "unverified": _as_lines(v.get("unverified")),
            "error": str(v.get("error", "")),
            "verdict_md": md,
            "prompt_chars": len(_read_text(rd / "prompt.md")),
            # A round whose judge has not written verdict.json yet is IN FLIGHT,
            # not a silent blank — the modal labels it as such.
            "pending": not v and not md,
        })
    # "latest" is the most recent round that actually RULED. A trailing round the
    # judge has been given but not yet answered is reported as in_flight instead of
    # blanking the verdict -- an unfinished round must not read as "no verdict".
    ruled = [r for r in rounds if r["verdict"]]
    return {
        "case": str(case),
        "rounds": rounds,
        "critic": rounds[-1]["critic"] if rounds else "",
        "latest": ruled[-1]["verdict"] if ruled else "",
        "in_flight": bool(rounds and rounds[-1]["pending"]),
        "signed_off": any(r["verdict"] == "SIGN-OFF" for r in rounds),
    }


def critic_review_prompt(case, round_no):
    """The exact prompt one review round was given (charter + persona + assignment).
    Loaded on demand so the ruling payload stays small."""
    try:
        want = int(round_no)
    except (TypeError, ValueError):
        return ""
    for n, rd in _round_dirs(case):
        if n == want:
            return _read_text(rd / "prompt.md")
    return ""


def precinct_detail(name):
    """Full detail for one precinct: ledger, parsed case log (newest first),
    case files, deputies, mode."""
    d = _read_json(config.PRECINCTS_JSON, {}) or {}
    pr = d.get("precincts", {}) if isinstance(d, dict) else {}
    if name not in pr:
        return None
    e = pr[name] if isinstance(pr[name], dict) else {}
    if e.get("status") == "deleted":     # Phase D: a soft-deleted precinct is not shown
        return None
    ledger = _read_text(config.RECORDS / name / "ledger.md")
    log = _read_text(config.RECORDS / name / "log.tsv")
    rows = _parse_case_log(log)
    rows.sort(key=lambda r: int(r["task"]) if str(r["task"]).isdigit() else -1,
              reverse=True)
    return {
        "name": name,
        "mode": e.get("ledger_mode", "mutable"),
        "model": e.get("model", "opus"),   # Task 376: per-precinct default model
        "description": e.get("description", ""),
        "ledger": ledger,
        "ledger_chars": len(ledger),
        "ledger_tokens": (len(ledger) + 3) // 4,
        "cases": rows,
        "deputies": _deputies_for(name, task_precinct_map()),
    }


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


def _case_log_summary(precinct, case):
    """The case-log summary (human description) for one case in a precinct, or ''."""
    if not precinct:
        return ""
    for r in _parse_case_log(_read_text(config.RECORDS / precinct / "log.tsv")):
        if r.get("task") == str(case):
            return r.get("summary", "") or ""
    return ""


def case_meta(task_id, fallback_precinct=None):
    """(precinct, summary) for a case id — the two facts the History day view needs.

    Case 447: `precinct` comes from the authoritative task->precinct map (falling
    back to a precinct scraped from the spec, e.g. an old case not in the map);
    `summary` is the case's ONE-SENTENCE case-log line — the LAST (most recent) row
    for it in that precinct's log, so a re-logged case (e.g. a v2 delivery) shows
    its latest summary. Summary is '' for a case that isn't closed/logged yet, and
    the UI then falls back to the request text."""
    e = task_precinct_map().get(str(task_id))
    prec = (e.get("precinct") if isinstance(e, dict) else None) or fallback_precinct
    summ = ""
    if prec:
        for r in _parse_case_log(_read_text(config.RECORDS / prec / "log.tsv")):
            if r.get("task") == str(task_id):
                summ = r.get("summary", "") or summ
    return prec, summ


# --------------------------------------------------------------------------- #
# Case 471: a ONE-SENTENCE summary of what a case REQUESTED, for the History +
# Status descriptions.
#
# Feng's ask: describe each case by a one-sentence summary of WHAT HE ASKED FOR,
# not the long case-log "what was done" summary the pages showed before. The
# request text lives in the case spec (the "## Task description" block the
# web-case bridge writes for BOTH web-form and precinct-tagged-email cases, or the
# first heading for legacy specs). A good one-sentence summary of a varied,
# multi-part request needs a model, so summaries are PRECOMPUTED by
# scratch_case_summarize.py into case_request_summaries.json; the dashboard only
# READS that cache (staying zero-API) and falls back to a deterministic
# first-sentence for any case not yet summarized.
# --------------------------------------------------------------------------- #
_FORM_DESC_RE = re.compile(r"##\s*Task description[^\n]*\n+(.*?)(?:\n##\s|\Z)", re.S | re.I)
_SUBJECT_LEAD = re.compile(r"^\s*SUBJECT:\s*(?:\[[^\]]*\]\s*)?[^\n]*\n+", re.I)
_CASE_LEAD = re.compile(r"^\s*(?:case|task)\s*#?\d+\s*[—\-:]\s*", re.I)


def request_text(task_id):
    """The user's ORIGINAL request for a case (verbatim), or '' if unavailable.

    Reads the case spec (scratch_full_logs/inbox/task_<id>.md), preferring the
    '## Task description' block the web-case bridge writes for web-form AND
    precinct-tagged-email cases; falls back to the spec's first heading (minus the
    'Case N —' lead) for legacy hand-written specs. Memoised per render."""
    def _load():
        body = _read_text(config.INBOX / f"task_{task_id}.md")
        if not body:
            return ""
        m = _FORM_DESC_RE.search(body)
        if m:
            req = m.group(1).strip()
            if req and req.lower() != "(no description provided)":
                return req
        for ln in body.splitlines():                 # legacy: first heading
            s = ln.strip()
            if s.startswith("#"):
                return _CASE_LEAD.sub("", s.lstrip("# ").strip()).strip()
        return ""
    return _cached(f"reqtext:{task_id}", 30, _load)


def request_oneliner(text, cap=160):
    """Deterministic one-line reduction of a request: drop a leading
    'SUBJECT:'/'Case N —', flatten newlines + list markers, then keep the whole
    thing if it is already short, else its first sentence, capped at ~cap chars on
    a word boundary. The zero-API FALLBACK used when a case has no cached model
    summary."""
    if not text:
        return ""
    t = _SUBJECT_LEAD.sub("", text.strip())
    t = _CASE_LEAD.sub("", t)
    t = re.sub(r"[\r\n]+\s*(?:[-*•]|\d{1,2}[.)])?\s*", " ", t)  # newlines/bullets -> space
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    if len(t) > cap:                                 # long -> first sentence only
        m = re.search(r"^(.+?[.?!])(?:\s|$)", t)
        if m:
            t = m.group(1).strip()
    if len(t) > cap:                                 # still long -> hard cap
        t = t[:cap].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return t


def _summary_cache():
    """{case_id(str): {'summary':..}} of precomputed model request-summaries,
    written by scratch_case_summarize.py. Read-only, short-lived memo."""
    return _cached(
        "req_summaries", 30,
        lambda: _read_json(config.SCRATCH / "case_request_summaries.json", {}) or {},
    )


def request_summary(task_id, fallback_precinct=None):
    """One-sentence summary of a case's REQUEST for the History/Status description
    (Case 471): the precomputed model summary when present, else the deterministic
    first-sentence. Always zero-API. `fallback_precinct` is accepted for a uniform
    call signature with case_meta but is unused here."""
    ent = _summary_cache().get(str(task_id))
    if isinstance(ent, dict) and (ent.get("summary") or "").strip():
        return ent["summary"].strip()
    return request_oneliner(request_text(task_id))


def _latest_attributed_case(name):
    """Newest NUMERIC case in the task->precinct map ATTRIBUTED to this worker
    (deputy == name or agent == name), as (case:str|None, precinct:str|None, summary).

    Case 396: for a long-lived agent (e.g. 'paper') that receives emailed cases via
    the interrupt path -- which stamps NEITHER the active-deputies board NOR an
    exported TSOMP_CASE -- this structured, backfill-maintained signal is a far
    better 'current case' than the worker's FROZEN spawn-time prompt file, which is
    stuck on its BIRTH case (the 'paper #367' Status-page bug: the paper agent long
    ago moved on to 379/381/383/387, but its prompt file never changed). Numeric
    only, so an alphanumeric sub-case owned by ANOTHER worker (391b, 397) is never
    mis-attributed here."""
    best = None
    for tid, e in task_precinct_map().items():
        if not (isinstance(e, dict) and str(tid).isdigit()):
            continue
        if e.get("deputy") == name or e.get("agent") == name:
            n = int(tid)
            if best is None or n > best[0]:
                best = (n, e.get("precinct"))
    if best is None:
        return None, None, ""
    case = str(best[0])
    return case, best[1], _case_log_summary(best[1], case)


def _worker_precinct_case(name):
    """Task 377 #3: (precinct, case) for a worker — primarily from its launch /
    relaunch script's exported WORKER_PRECINCT / TSOMP_CASE (exactly what the running
    worker stamps on its emails), falling back to the task->precinct map. Either may
    be None if unresolved."""
    precinct = case = None
    for fn in (f"scratch_worker_{name}_launch.sh", f"scratch_worker_{name}_relaunch.sh"):
        try:
            txt = (config.STATE_ROOT / fn).read_text(errors="replace")
        except Exception:
            continue
        mp = re.search(r'export\s+WORKER_PRECINCT="?([^"\n]*)"?', txt)
        mc = re.search(r'export\s+TSOMP_CASE="?([^"\n]*)"?', txt)
        if mp and mp.group(1).strip():
            precinct = precinct or mp.group(1).strip()
        if mc and mc.group(1).strip():
            case = case or mc.group(1).strip()
        if precinct and case:
            break
    if precinct is None or case is None:
        # Case 396: neither the board nor the launch script pinned this worker's
        # case. Prefer the newest case ATTRIBUTED to it in the task->precinct map (a
        # backfill-maintained signal) over its frozen spawn-time prompt file, which
        # is stuck on the worker's BIRTH case (the 'paper #367' bug — a long-lived
        # agent whose emailed cases restamp neither the board nor the prompt). Fall
        # back to the prompt-derived task only if the worker is unattributed there.
        acase, aprec, _ = _latest_attributed_case(name)
        if acase is not None:
            case = case or acase
            precinct = precinct or aprec
        else:
            tid, _ = _worker_task_info(name)
            if tid is not None:
                case = case or str(tid)
                e = task_precinct_map().get(str(tid))
                if precinct is None and isinstance(e, dict):
                    precinct = e.get("precinct")
    return precinct, case


def _worker_model_fields(entry):
    """Case 561: the CURRENT model/service of a worker, for the Status board.

    Reads the watchdog roster, which the switch handler rewrites as part of
    applying a switch -- so this is the live value, not the launch-time one. Falls
    back to the platform default for a pre-561 entry that never stored a model
    (every such worker is a claude worker, which is what resolve() returns).
    """
    alias = (entry.get("model") or "").strip()
    service = (entry.get("service") or "").strip()
    label = alias or "?"
    try:
        if alias:
            alias = models.alias_of(alias)
            label = models.label(alias)
            # A roster entry written before Case 557 has no `service` field; the
            # model itself names its vendor, so derive rather than show a blank.
            service = service or models.service_of(alias)
        else:
            service = service or models.DEFAULT_SERVICE
            alias = models.default_model(service)
            label = models.label(alias)
    except Exception:
        pass
    return {
        "model": alias,
        "model_label": label,
        "service": service,
        "switches": int(entry.get("switches") or 0),
        "last_switch": entry.get("last_switch") or "",
    }


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
        prompt_tid, ttitle = _worker_task_info(name)
        prec, case = _worker_precinct_case(name)
        # Task 382 #1: the active-deputies file (sheriff-owned in the redesign) is
        # authoritative for a deputy's CURRENT case/description/precinct — a deputy
        # relaunched on an email reply and now working a NEW case updates it, so the
        # board flips off its previous case. Overrides the launch-script defaults.
        ad = active_deputies().get(name)
        board_desc = None
        if isinstance(ad, dict):
            if ad.get("case"):
                case = str(ad["case"])
            if ad.get("description"):
                board_desc = ttitle = ad["description"]
            if ad.get("precinct"):
                prec = ad["precinct"]
        # Case 396: when the case was NOT pinned by the board and the resolved case
        # differs from the FROZEN prompt-file birth case, the prompt-derived title is
        # stale too — describe the row by the resolved case's case-log summary
        # instead (paper: '#387 — Budgeted-encoding draft', not the Task-367 spec).
        if not board_desc and case and (prompt_tid is None or str(case) != str(prompt_tid)):
            s = _case_log_summary(prec, case)
            if s:
                ttitle = s
        # Case 471: the Status "case description" should be a ONE-SENTENCE summary of
        # what the case REQUESTED (same as the History page), not the truncated title
        # heading (board description) or the what-was-done case-log summary. Prefer it
        # whenever the request is resolvable; the board/summary logic above stays the
        # fallback for a case with no readable request (e.g. a long-lived agent's
        # email case that never wrote a spec).
        if case:
            rs = request_summary(case)
            if rs:
                ttitle = rs
        # Task 391 #2: the clickable case# must open the ACTUAL case. Reconcile the
        # numeric detail id (`task`) with the authoritative `case`: a numeric case
        # links to that case; an ALPHANUMERIC sub-case (e.g. 384e) has no numeric
        # email conversation, so leave `task` None — do NOT fall back to a mis-parsed
        # prompt-derived number (that opened the WRONG case, e.g. 331). The UI then
        # links the case file instead.
        tid = prompt_tid
        if case:
            cs = str(case)
            tid = int(cs) if cs.isdigit() else None
        rows.append(
            {
                "name": name,
                "state": st,
                "state_label": _STATE_LABEL.get(st, st or "?"),
                # Task 327: the task this worker is currently on (number + title),
                # resolved from its prompt's TASK section for the Status page.
                "task": tid,
                "task_title": ttitle,
                # Task 377 #3: deputy=worker name; its precinct + case number so the
                # Status "Active Workers" table can lead with deputy / case# / precinct.
                "precinct": prec or "",
                "case": case or (str(tid) if tid else ""),
                # Task 391 #2: the case-file path so an alphanumeric sub-case (no
                # numeric conversation) links to its case file instead of a wrong case.
                "case_file": (f"scratch_full_logs/inbox/task_{case}.md" if case else None),
                "requester": e.get("requester"),
                "desc": rinfo.get("desc") or "",
                "keywords": rinfo.get("match", []),
                "session": e.get("session"),
                "relaunched": e.get("relaunched", 0),
                "reset_epoch": e.get("reset_epoch"),
                "last_progress_ts": e.get("last_progress_ts"),
                "mailbox_pending": _mailbox_pending(name),
                # Case 561: the model this case is running RIGHT NOW. It is not a
                # constant any more -- a deputy switches its own model mid-case
                # (scratch_model_switch.py), so the roster's `model`/`service` are
                # rewritten by the watchdog on every switch and are the live truth.
                # `switches` is how many times this case has changed model.
                **_worker_model_fields(e),
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


def gpu_stats():
    """On-demand per-GPU stats from nvidia-smi (Task 377 #3). Deliberately NOT part
    of the 8s /api/status auto-poll — only the Status page's Refresh button calls
    this (via /api/gpu_stats), so nvidia-smi is invoked on demand, not on a timer.
    Returns {available, ts, gpus:[{index,name,util,mem_used,mem_total,mem_pct,temp}],
    error?}. Degrades gracefully when there is no GPU / nvidia-smi is missing."""
    cmd = ["nvidia-smi",
           "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
           "--format=csv,noheader,nounits"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except FileNotFoundError:
        return {"available": False, "ts": time.time(), "gpus": [],
                "error": "nvidia-smi not found on the dashboard host (no NVIDIA GPU?)"}
    except Exception as ex:
        return {"available": False, "ts": time.time(), "gpus": [],
                "error": f"nvidia-smi could not be run: {ex}"}
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()[:300] or f"rc={p.returncode}"
        return {"available": False, "ts": time.time(), "gpus": [],
                "error": f"nvidia-smi error: {msg}"}

    def _num(s):
        try:
            return float(s)
        except Exception:
            return None

    gpus = []
    for line in p.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 6:
            continue
        idx, nm, util, used, total, temp = parts[:6]
        used_n, total_n = _num(used), _num(total)
        pct = round(100.0 * used_n / total_n, 1) if (used_n is not None and total_n) else None
        gpus.append({"index": idx, "name": nm, "util": _num(util),
                     "mem_used": used_n, "mem_total": total_n, "mem_pct": pct,
                     "temp": _num(temp)})
    return {"available": True, "ts": time.time(), "gpus": gpus}


# --------------------------------------------------------------------------- #
# usage-limit state (Task 274)
# --------------------------------------------------------------------------- #
def limit_state():
    d = _read_json(config.LIMIT_STATE)
    if not isinstance(d, dict) or not d.get("active"):
        return None
    return d


# Human labels for the two axes the banner must state (Feng, Case 582: "Make sure
# on the banner state what service what limit"). Kept as a local table rather than
# imported from the live registry: this reader must work against a STATE_ROOT that
# is only a state directory, and an unknown id falls through to itself rather than
# rendering blank — the Case-561 trap where a mirror helper died in its own except
# and showed an empty service.
_SERVICE_LABEL = {"claude": "Claude", "chatgpt": "ChatGPT"}
_KIND_LABEL = {"weekly": "weekly", "session": "5-hour session",
               "5-hour": "5-hour session", "daily": "daily",
               "credits": "workspace-credit", "usage": "usage"}


def limit_states(now=None):
    """EVERY live usage limit: each vendor's ACCOUNT-wide marker plus any
    per-MODEL cap, each already labelled for display.

    Case 582. Two independent reasons the page showed nothing while ChatGPT was
    walled: limit_state() reads only claude's marker (Case 557 made them
    per-service and did not update this reader), and a per-model weekly cap was
    recorded in no file at all until Case 582 added one. Expired records are
    filtered here, so a stale marker cannot keep a banner up forever.

    Each entry adds: service_label, kind_label, scope ('account'|'model'),
    scope_label, and reset_known — False meaning the vendor stated no time and the
    epoch is our own retry guess, which the banner must not present as fact."""
    now = time.time() if now is None else now
    out = []
    for svc, path in config.LIMIT_STATE_SERVICES.items():
        d = _read_json(path)
        if not isinstance(d, dict) or not d.get("active"):
            continue
        if now >= (d.get("reset_epoch") or 0):
            continue                       # reset has passed — not a live wall
        rec = dict(d)
        rec["service"] = rec.get("service") or svc
        rec["scope"] = "account"
        rec.setdefault("reset_known", True)   # markers written before Case 582
        out.append(rec)
    models = _read_json(config.MODEL_LIMIT_STATE, {})
    if isinstance(models, dict):
        for rec in models.values():
            if not isinstance(rec, dict) or not rec.get("active"):
                continue
            if now >= (rec.get("reset_epoch") or 0):
                continue
            rec = dict(rec)
            rec["scope"] = "model"
            out.append(rec)
    for rec in out:
        svc = rec.get("service") or "claude"
        rec["service_label"] = _SERVICE_LABEL.get(svc, svc)
        rec["kind_label"] = _KIND_LABEL.get(rec.get("kind"), rec.get("kind") or "usage")
        rec["scope_label"] = (f"{rec['service_label']} account"
                              if rec["scope"] == "account"
                              else f"{rec['service_label']} model "
                                   f"{rec.get('model') or '?'}")
    out.sort(key=lambda r: r.get("reset_epoch") or 0)
    return out


# --------------------------------------------------------------------------- #
# Sheriff = SYSTEM MANAGER (Task 384c / Phase A1)
# --------------------------------------------------------------------------- #
def system_manager():
    """The unified "Sheriff = SYSTEM MANAGER" view. The Sheriff owns TWO always-on
    supervision loops, presented here as one mental model:

      * DEPUTY SUPERVISION -- the watchdog (``scratch_watchdog.py``): deputy
        liveness, crash / usage-limit recovery + relaunch. Surfaced from the existing
        readers ``daemon_status()`` (is the watchdog up?), ``workers()`` (the roster),
        and ``limit_state()`` (the shared usage-limit marker).
      * PRECINCT RECORDS-HEALTH -- the sheriff daemon (``scratch_sheriff.py``):
        ledger sizes, compaction, the deputy->sheriff request queue. Surfaced from
        ``sheriff_status()``.

    PURE PRESENTATION (Phase A1 is rebrand + supervise, NO behavior change): this
    only COMPOSES the existing read-only readers -- it does not change what any of
    them measures and makes no new measurement of its own. Both loops keep running
    exactly as today; this is simply the single view over the two. The two daemons
    stay separate processes (A2, a full code merge, is a later phase)."""
    sh = sheriff_status()
    watchdog_up = any(d["name"] == "watchdog" and d["alive"] for d in daemon_status())
    roster = workers(active_only=True)
    by_state, relaunches, mailbox_pending = {}, 0, 0
    for w in roster:
        st = w.get("state") or "?"
        by_state[st] = by_state.get(st, 0) + 1
        relaunches += int(w.get("relaunched") or 0)
        if w.get("mailbox_pending"):
            mailbox_pending += 1
    return {
        # the two loops the one system manager owns
        "sheriff_up": bool(sh.get("alive")),   # records-health loop
        "watchdog_up": watchdog_up,            # deputy-supervision loop
        # records-health detail (sheriff_status, carried through unchanged)
        "records": sh,
        # deputy-supervision summary OVER the watchdog roster (workers, unchanged)
        "deputies": {
            "active": len(roster),
            "by_state": by_state,
            "relaunches": relaunches,
            "mailbox_pending": mailbox_pending,
        },
        "limit": limit_state(),
    }


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
    # Case 427: the AUTHORITATIVE owner is the deputy recorded for this case at
    # spawn/take (the same task_precinct.json the Status board trusts). Web cases
    # (deputy web_<n>) resolve here directly; old migrated rows carry only a
    # precinct (no deputy) -> fall through to the heuristics below.
    rec = task_precinct_map().get(str(task_id))
    if isinstance(rec, dict):
        dep = rec.get("deputy") or rec.get("agent")
        if dep:
            return dep
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
    try:
        lines = Path(config.SENT_EMAILS).read_text(errors="replace").splitlines()
    except Exception:
        return []
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("agent") == agent:
            rows.append(r)
    # Case 427: a token-less email from a deputy DEDICATED to this case (every
    # case token it ever used is task_id) still belongs to the case — otherwise
    # the per-milestone figures a web deputy sends under a fresh "Re: <topic>"
    # subject (no "Case N" token) are silently dropped from deliverables.
    dedicated = False
    if task_id is not None:
        toks = set()
        for r in rows:
            m = _SUBJ_TASK.search(r.get("subject", "") or "")
            if m:
                toks.add(int(m.group(1)))
        dedicated = toks <= {task_id}
    out, seen = [], set()
    for r in rows:
        if task_id is not None:
            if is_autoack(r.get("subject", "") or "", r.get("body", "") or ""):
                continue
            m = _SUBJ_TASK.search(r.get("subject", "") or "")
            tok = int(m.group(1)) if m else None
            if tok is not None:
                if tok != task_id:
                    continue
            elif not dedicated:
                continue
        for p in (r.get("attachments") or []):
            rp = config.resolve_download(p)
            if rp is not None:
                key = str(rp)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": rp.name, "path": key, "emailed": True,
                            "downloadable": True})
            else:
                # Case 512: the deputy DID email this file, but it resolves outside
                # the dashboard's download roots — e.g. another precinct's project
                # dir (deepcap deliverables under /DeepStore) or an ephemeral /tmp
                # figure. SILENTLY DROPPING it is the reported bug: the panel showed
                # "(none found)" even though a PDF was demonstrably emailed. Surface
                # it as a non-downloadable entry (name shown, full path in tooltip),
                # deduped by the raw path so a FINAL and its follow-up re-attach of
                # the same file collapse to one row.
                key = "raw::" + str(p)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": os.path.basename(str(p)) or str(p),
                            "path": str(p), "emailed": True,
                            "downloadable": False})
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
