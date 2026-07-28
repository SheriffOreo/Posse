#!/usr/bin/env python3
"""Inbox reader for the email-router agent.

`next`  -> fetch the OLDEST unseen reply from an ALLOWED sender, decide which
           worker should handle it, dump the (de-quoted) body to a file, and
           print a parseable line. Does NOT mark the message seen.
`mark --uid <uid>` -> mark that message \\Seen (call after dispatching).

Only messages whose From is one of ALLOWED are ever returned. Routing:
1) the reply's In-Reply-To matches a Message-ID we logged in sent_emails.jsonl
   -> use that agent; else
2) keyword-match the reply subject+body against registry workers' "match" lists;
   else 3) registry "default".
"""
import argparse, json, re, sys, time
from datetime import datetime, timedelta
import imaplib, email
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED = {"stevenfd@cmu.edu", "fenghaod@andrew.cmu.edu"}
SENT = ROOT / "scratch_full_logs" / "sent_emails.jsonl"
REG = json.loads((ROOT / "scratch_agents_registry.json").read_text())
INBOX_DIR = ROOT / "scratch_full_logs" / "inbox"
# uid -> epoch before which the message is "blocked" (we hit a usage limit and
# want to retry only after it resets). cmd_next skips these until the time passes.
BLOCKED = ROOT / "scratch_full_logs" / "inbox_blocked.json"


def load_blocked():
    try:
        return json.loads(BLOCKED.read_text())
    except Exception:
        return {}


def save_blocked(d):
    BLOCKED.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED.write_text(json.dumps(d))


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
# Timezone-abbreviation -> IANA name, for reset notices that print e.g. "9pm EDT".
_TZ_ABBR = {"edt": "America/New_York", "est": "America/New_York",
            "cdt": "America/Chicago", "cst": "America/Chicago",
            "mdt": "America/Denver", "mst": "America/Denver",
            "pdt": "America/Los_Angeles", "pst": "America/Los_Angeles",
            "utc": "UTC", "gmt": "UTC"}


def _extract_tz(original, low):
    """A ZoneInfo for a timezone named in the notice, or None. Real Claude Code
    limit lines carry an explicit tz, e.g. 'resets 2:10am (America/New_York)' or
    an abbreviation like 'EDT' — the reset time is displayed in THAT zone, not
    necessarily the host's. Case-preserved IANA match first (ZoneInfo is
    case-sensitive), then the abbreviation table. None => caller assumes local
    time (the pre-Task-274 behaviour; correct whenever host tz == displayed tz)."""
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return None
    m = re.search(r"([A-Za-z]+/[A-Za-z_]+)", original or "")   # IANA, case intact
    if m:
        try:
            return ZoneInfo(m.group(1))
        except Exception:
            pass
    m = re.search(r"\b(edt|est|cdt|cst|mdt|mst|pdt|pst|utc|gmt)\b", low)
    if m:
        try:
            return ZoneInfo(_TZ_ABBR[m.group(1)])
        except Exception:
            pass
    return None


def _parse_clock(low):
    """(hour24, minute) from a clock time in `low`, or (None, None). Handles
    am/pm ('3pm', '2:10am', '3 p.m.') and 24-hour ('15:00'). 12am->0, 12pm->12."""
    tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", low)
    if tm:
        hr = int(tm.group(1)) % 12
        if tm.group(3) == "p":
            hr += 12
        return hr, int(tm.group(2) or 0)
    tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", low)     # 24-hour HH:MM
    if tm:
        return int(tm.group(1)), int(tm.group(2))
    return None, None


def limit_kind_of(text):
    """Classify WHICH Claude limit a notice is about (Task 274). The 'session
    limit' Claude Code prints IS the rolling 5-hour window; 'weekly limit' is the
    multi-day one. Returned: 'weekly' | 'session' | '5-hour' | 'daily' | 'usage'.
    Only 'weekly' has a days-scale reset — the rest are hours-scale."""
    low = (text or "").lower()
    if "weekly" in low or re.search(r"\bweek\b", low):
        return "weekly"
    if re.search(r"5\s*-?\s*hour", low):
        return "5-hour"
    if "session" in low:
        return "session"
    if "daily" in low or re.search(r"\bday\b", low):
        return "daily"
    if re.search(r"\bhour(ly)?\b", low):
        return "5-hour"
    return "usage"


def parse_reset_epoch(text, kind=None, now=None):
    """Turn a Claude limit notice into an epoch for the reset. Handles, in order:
    a relative reset ('...resets in 3 hours'); an ISO date ('2026-07-27[ 18:00]');
    a month-name dated reset ('...resets Jul 27, 6pm') -> that calendar date/time;
    and a bare time-of-day ('...resets 3:10am') -> next occurrence. Times are
    read in the notice's own timezone when it names one (see _extract_tz), else
    local. Falls back to +1h when no time is parseable.

    Task 274: `kind` (from limit_kind_of) and `now` (inject for tests) are
    optional and backward-compatible. For a 'weekly' notice that resolves to a
    suspiciously-soon reset (< 12h, i.e. only a time-of-day matched and no date),
    the value is still returned as-is — the watchdog logs the anomaly and the
    reset is self-correcting (a too-early relaunch simply re-hits the limit and
    re-schedules) — but a correctly dated 'Jul 27, 6pm' resolves exactly."""
    original = text or ""
    low = original.lower()
    tz = _extract_tz(original, low)
    if now is None:
        now = datetime.now(tz) if tz else datetime.now()
    elif tz is not None:
        # Evaluate everything in the NOTICE's timezone: a naive injected now is
        # read as notice-local; an aware one is converted to the same instant in
        # the notice tz. (Production uses now=None => already in the notice tz.)
        now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)

    def _make(y, mo, d, hr, mn):
        return datetime(y, mo, d, hr, mn, tzinfo=tz) if tz else datetime(y, mo, d, hr, mn)

    # 1) relative: "resets in 3 hours" / "in 45 min" / "in 2 days"
    rel = re.search(r"\bin\s+(\d+)\s*(minute|min|hour|hr|day)s?\b", low)
    if rel:
        secs = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}[rel.group(2)]
        return (now + timedelta(seconds=int(rel.group(1)) * secs)).timestamp()

    hr, mn = _parse_clock(low)

    # 2) ISO date  YYYY-MM-DD  (optionally with the clock time parsed above)
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if iso:
        y, mo, d = map(int, iso.groups())
        return _make(y, mo, d, hr or 0, mn or 0).timestamp()

    # 3) month-name date, e.g. "Jun 27" / "Jul 27, 6pm"
    dm = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})", low)
    if dm:
        month, day = _MONTHS[dm.group(1)], int(dm.group(2))
        year = now.year if (month, day) >= (now.month, now.day) else now.year + 1
        return _make(year, month, day, hr if hr is not None else 0, mn or 0).timestamp()

    # 4) bare time-of-day -> next occurrence (today if still future, else tomorrow)
    if hr is None:
        return (now + timedelta(hours=1)).timestamp()
    t = now.replace(hour=hr, minute=mn or 0, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


def parse_limit_notice(text, now=None):
    """If `text` looks like a Claude usage-limit notice (a 'limit' word AND a
    'reset' word), return (kind, reset_epoch, reset_str); else None. `kind` is
    from limit_kind_of; `reset_str` is the human 'resets …' fragment (original
    case, so the tz name survives). Shared by the watchdog (limit detection) and
    the standalone limit-parse test (Task 274 G1)."""
    low = (text or "").lower()
    if not re.search(r"\blimit\b", low) or not re.search(r"\breset", low):
        return None
    kind = limit_kind_of(low)
    rm = re.search(r"reset[s]?\b[^\n]*", text or "", re.I)   # original case for tz
    reset_str = rm.group(0).strip() if rm else (text or "")
    return kind, parse_reset_epoch(reset_str, kind=kind, now=now), reset_str


def env():
    e = {}
    for ln in (Path.home() / ".smtp_env").read_text().splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); e[k.strip()] = v.strip()
    return e


def conn():
    e = env()
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(e["SMTP_USER"], e["SMTP_PASS"])
    M.select("INBOX")
    return M


def _dec(s):
    if not s:
        return ""
    out = []
    for txt, enc in decode_header(s):
        out.append(txt.decode(enc or "utf-8", "replace") if isinstance(txt, bytes) else txt)
    return "".join(out)


def addr_of(frm):
    m = re.search(r"[\w.\-+]+@[\w.\-]+", frm or "")
    return m.group(0).lower() if m else ""


def body_of(msg):
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                break
    else:
        text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    # strip quoted reply history (best effort)
    lines = []
    for ln in text.splitlines():
        if re.match(r"^\s*On .*wrote:\s*$", ln) or ln.strip().startswith(">"):
            break
        lines.append(ln)
    return "\n".join(lines).strip()[:4000]


def save_attachments(msg, uid):
    """Save any real attachments to scratch_full_logs/inbox/att/<uid>/ and return
    the list of saved file paths (so the handler/worker can read them)."""
    saved = []
    if not msg.is_multipart():
        return saved
    outdir = INBOX_DIR / "att" / str(uid)
    n = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "")
        fname = part.get_filename()
        # an attachment = explicit attachment disposition, or any part with a filename
        if "attachment" not in disp.lower() and not fname:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        n += 1
        fname = _dec(fname) or f"attachment_{n}"
        fname = re.sub(r"[^\w.\-]+", "_", fname).strip("_") or f"attachment_{n}"
        outdir.mkdir(parents=True, exist_ok=True)
        dest = outdir / fname
        dest.write_bytes(payload)
        saved.append(str(dest))
    return saved


def sent_map():
    m = {}
    if SENT.exists():
        for ln in SENT.read_text().splitlines():
            try:
                r = json.loads(ln); m[r["message_id"].strip("<> ")] = r["agent"]
            except Exception:
                pass
    return m


def route(in_reply_to, references, subject, body):
    sm = sent_map()
    for mid in re.findall(r"<([^>]+)>", (in_reply_to or "") + " " + (references or "")):
        if mid.strip() in sm and sm[mid.strip()] in REG["workers"]:
            return sm[mid.strip()]
    hay = (subject + " " + body).lower()
    subj = (subject or "").lower()
    best, score = REG["default"], 0
    for key, w in REG["workers"].items():
        s = sum(1 for kw in w.get("match", []) if kw.lower() in hay)
        # Explicit addressing beats keyword counts: the worker's own name as a
        # word in the subject, or "<name> agent"/"agent <name>" anywhere.
        # Underscores in names also match spaces/hyphens ("code_review" ~ "code review").
        k = re.escape(key.lower()).replace("_", "[ _-]")
        if (re.search(rf"\b{k}\b", subj)
                or re.search(rf"\b{k} agent\b", hay) or re.search(rf"\bagent {k}\b", hay)):
            s += 10
        if s > score:
            best, score = key, s
    return best


def cmd_next():
    M = conn()
    typ, data = M.search(None, "UNSEEN")
    ids = data[0].split() if data and data[0] else []
    blocked = load_blocked()
    now = time.time()
    changed = False
    for i in ids:
        uid = i.decode()
        # a previous attempt hit a usage limit: skip until the reset time passes.
        if uid in blocked:
            if now < blocked[uid]:
                continue
            del blocked[uid]; changed = True  # reset passed -> eligible again
        typ, d = M.fetch(i, "(BODY.PEEK[])")
        msg = email.message_from_bytes(d[0][1])
        frm = addr_of(_dec(msg.get("From")))
        if frm not in ALLOWED:
            continue  # ignore non-allowed senders (leave unseen, never acted on)
        subject = _dec(msg.get("Subject"))
        body = body_of(msg)
        atts = save_attachments(msg, uid)
        # Phantom/empty messages (no subject, no body, no attachments — e.g. read
        # receipts or stray sends) must NOT interrupt a running worker: mark seen
        # and skip instead of dispatching.
        stripped_subject = re.sub(r"^(re|fwd?):\s*", "", subject.strip(), flags=re.I)
        if not stripped_subject and not body.strip() and not atts:
            M.store(i, "+FLAGS", "\\Seen")
            print(f"SKIPPED-EMPTY uid={uid} from={frm}", file=sys.stderr)
            continue
        agent = route(msg.get("In-Reply-To"), msg.get("References"), subject, body)
        w = REG["workers"][agent]
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        bf = INBOX_DIR / f"{uid}.txt"
        att_block = ""
        if atts:
            att_block = ("\nATTACHMENTS (saved to disk — read these file paths directly):\n"
                         + "\n".join(f"  - {p}" for p in atts) + "\n")
        bf.write_text(f"FROM: {frm}\nSUBJECT: {subject}\n\n{body}\n{att_block}")
        if changed:
            save_blocked(blocked)
        M.logout()
        print(f"UID={uid}\tAGENT={agent}\tTYPE={w['type']}\tSESSION={w.get('session')}\t"
              f"FROM={frm}\tSUBJECT={subject}\tATTACHMENTS={len(atts)}\tBODYFILE={bf}")
        return
    if changed:
        save_blocked(blocked)
    M.logout()
    print("NONE")


def cmd_mark(uid):
    M = conn()
    M.store(uid.encode(), "+FLAGS", "\\Seen")
    M.logout()
    print(f"marked {uid} seen")


def cmd_unmark(uid):
    M = conn()
    M.store(uid.encode(), "-FLAGS", "\\Seen")  # back to unread
    M.logout()
    print(f"marked {uid} unseen")


def cmd_defer(uid, reset):
    """Hit a usage limit handling this message: record a retry-after time, then
    mark it unread so it visibly remains pending and is re-handled after reset."""
    epoch = parse_reset_epoch(reset)
    b = load_blocked(); b[uid] = epoch; save_blocked(b)  # block BEFORE unmarking
    cmd_unmark(uid)
    print(f"deferred {uid} until {datetime.fromtimestamp(epoch):%Y-%m-%d %H:%M} ({reset!r})")


def cmd_session(agent):
    w = REG["workers"].get(agent)
    print((w or {}).get("session") or "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    mk = sub.add_parser("mark"); mk.add_argument("--uid", required=True)
    um = sub.add_parser("unmark"); um.add_argument("--uid", required=True)
    df = sub.add_parser("defer"); df.add_argument("--uid", required=True); df.add_argument("--reset", default="")
    se = sub.add_parser("session"); se.add_argument("--agent", required=True)
    a = ap.parse_args()
    if a.cmd == "next":
        cmd_next()
    elif a.cmd == "mark":
        cmd_mark(a.uid)
    elif a.cmd == "unmark":
        cmd_unmark(a.uid)
    elif a.cmd == "session":
        cmd_session(a.agent)
    else:
        cmd_defer(a.uid, a.reset)
