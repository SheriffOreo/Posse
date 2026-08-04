#!/usr/bin/env python3
"""Send a short email update (optionally with attachments) via Gmail SMTP.

Credentials from ~/.smtp_env (SMTP_USER, SMTP_PASS=app password, SMTP_TO).
Each send gets a Message-ID and is logged (with --agent key) to
scratch_full_logs/sent_emails.jsonl so the inbox router can map a user's reply
back to the agent that sent the original update.

Usage:
    python scratch_notify_email.py "subject" "body" [--attach f.png ...] [--agent eval]
        [--thread "<originating subject>"]

Task 170 F6 (OPT-IN threading; default off so plan/milestone/FINAL emails are
unaffected): --thread "<subject>" makes this email part of the originating
email's Gmail thread: the Subject becomes "Re: <base>" (base = the thread
subject stripped of leading Re:/Fwd:), and, best-effort, In-Reply-To/References
are set to the Message-ID of our most recent sent email whose subject matches
that base (from sent_emails.jsonl) — in the usual flow that is the update the
user replied to, so the whole incident lands in one Gmail conversation.
"""
import argparse, json, mimetypes, os, re, smtplib, sys, time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

LOG = Path(__file__).resolve().parent / "scratch_full_logs" / "sent_emails.jsonl"
# The operator this posse reports to. Email sent to INFRA_OPERATOR_EMAIL opens with
# "Hi <INFRA_OPERATOR_NAME>,"; set both during onboarding. Empty by default so the
# released code carries no personal identity (a general greeting is used instead).
OPERATOR_EMAIL = os.environ.get("INFRA_OPERATOR_EMAIL", "").strip().lower()
OPERATOR_NAME = os.environ.get("INFRA_OPERATOR_NAME", "there").strip() or "there"

# Task 376 #1: a context header stamped just under the greeting on every outgoing
# email, so the operator can see at a glance which precinct / case / deputy (and model)
# an email is from. Fields come from the deputy's environment (WORKER_PRECINCT +
# TSOMP_CASE + TSOMP_MODEL, exported by the launch/relaunch scripts) and the
# --agent tag; ANY unknown field is omitted (a manual, non-deputy send with only
# --agent still stamps e.g. "[deputy: precincts]"). ASCII-safe (pipe separator).
_GREETINGS = ("hi ", "hello", "hey ", "dear ", "greetings")
_HEADER_RE = re.compile(r"^\[(?:precinct|case|deputy|model):", re.I)


def _clean_field(v):
    """One-line, bracket-free field value (never breaks the single-line header)."""
    return re.sub(r"[\[\]\r\n]", "", str(v)).strip()


def _active_deputies_path():
    """Where scratch_deputy_state.py keeps the active-deputies board
    (<records_root>/active_deputies.json). TSOMP_RECORDS_ROOT overrides the root
    (tests) — kept in lockstep with scratch_deputy_state._path()."""
    root = os.environ.get("TSOMP_RECORDS_ROOT")
    base = Path(root) if root else (Path(__file__).resolve().parent
                                    / "scratch_full_logs" / "records")
    return base / "active_deputies.json"


def _board_case_precinct(agent):
    """Best-effort (case, precinct) for `agent` from the active-deputies board — the
    SAME source of truth the dashboard Status board reads, updated whenever a deputy
    TAKES a new case (scratch_deputy_state.py set). This is what keeps the stamp
    honest: without it the case comes from the TSOMP_CASE literal baked into the
    deputy's launch/relaunch script at spawn time, which goes stale the moment the
    deputy takes a follow-up case (Case 394: case_ui stamped [case: 377] while it
    was actually on 391, because its relaunch script still exported the birth case
    377 and per-take `export TSOMP_CASE=...` doesn't survive across bash calls).
    Returns (None, None) on any problem so the caller falls back to the env."""
    a = (agent or "").strip()
    if not a:
        return (None, None)
    try:
        d = json.loads(_active_deputies_path().read_text())
        e = d.get(a) if isinstance(d, dict) else None
        if not isinstance(e, dict):
            return (None, None)
        c, p = e.get("case"), e.get("precinct")
        c = str(c) if c is not None and str(c) != "" else None
        p = str(p) if p is not None and str(p) != "" else None
        return (c, p)
    except Exception:
        return (None, None)


def context_header(agent):
    """The '[precinct: P | case: N | deputy: A | model: M]' line for this send, or
    '' when no field is known. BOARD-FIRST (Case 394): the deputy's CURRENT case and
    precinct come from the active-deputies board (updated on every case TAKE),
    falling back to the WORKER_PRECINCT / TSOMP_CASE env baked into the launch/relaunch
    script when the board has no entry for `agent`. Model is always from TSOMP_MODEL.
    Any missing field is omitted."""
    a = _clean_field(agent or "")
    b_case, b_prec = _board_case_precinct(a)
    p = _clean_field(b_prec if b_prec is not None else os.environ.get("WORKER_PRECINCT", ""))
    c = _clean_field(b_case if b_case is not None else os.environ.get("TSOMP_CASE", ""))
    m = _clean_field(os.environ.get("TSOMP_MODEL", ""))
    fields = []
    if p:
        fields.append(f"precinct: {p}")
    if c:
        fields.append(f"case: {c}")
    if a:
        fields.append(f"deputy: {a}")
    if m:
        fields.append(f"model: {m}")
    return "[" + " | ".join(fields) + "]" if fields else ""


def _insert_header(b, header):
    """Put `header` just under the greeting (if the body opens with one), else at
    the very top. Idempotent: a body that already carries a context header near the
    top is left unchanged."""
    if not header:
        return b
    for ln in b.splitlines()[:6]:
        if _HEADER_RE.match(ln.strip()):
            return b                       # already stamped — don't double up
    lines = b.split("\n")
    first = lines[0].strip().lower() if lines else ""
    if first.startswith(_GREETINGS):
        rest = "\n".join(lines[1:]).lstrip("\n")
        return lines[0] + "\n\n" + header + ("\n\n" + rest if rest else "")
    return header + "\n\n" + b


def style_body(body, agent, to):
    """Communication preference: email to the operator (INFRA_OPERATOR_EMAIL) opens
    with 'Hi <name>,' and every agent signs its name at the end. A context header
    (precinct / case / deputy / model) is stamped just under the greeting. Idempotent
    — won't double up if any of these is already present."""
    b = (body or "").strip("\n")
    if to and OPERATOR_EMAIL and to.lower() == OPERATOR_EMAIL:
        head = b.lstrip().lower()
        if not head.startswith(_GREETINGS):
            b = f"Hi {OPERATOR_NAME},\n\n" + b
    b = _insert_header(b, context_header(agent))
    if agent:
        tail = "\n".join(b.splitlines()[-4:]).lower()
        already = (agent.lower() in tail and ("—" in tail or "--" in tail
                   or tail.strip().startswith("- ") or "regards" in tail or "best," in tail))
        if not already:
            b = b + f"\n\n— {agent}"
    return b


def load_env():
    env = {}
    p = Path.home() / ".smtp_env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: os.environ[k] for k in ("SMTP_USER", "SMTP_PASS", "SMTP_TO") if k in os.environ})
    return env


def _thread_base(subject):
    """Strip leading Re:/Fwd: prefixes (repeatedly) for thread-subject matching."""
    s = (subject or "").strip()
    while True:
        m = re.match(r"^(re|fwd?)\s*:\s*", s, re.I)
        if not m:
            return s
        s = s[m.end():]


def _thread_ref(base):
    """Message-ID of our most recent sent email whose subject matches `base`
    (normalized), from sent_emails.jsonl. None if no match — subject-reuse
    alone still threads in Gmail's UI in that case."""
    want = _thread_base(base).lower()
    ref = None
    try:
        for ln in LOG.read_text().splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if _thread_base(r.get("subject", "")).lower() == want and r.get("message_id"):
                ref = r["message_id"]          # keep the latest match
    except Exception:
        pass
    return ref


def send(subject, body, attachments=None, agent=None, to=None, thread=None):
    e = load_env()
    user, pw = e.get("SMTP_USER"), e.get("SMTP_PASS")
    to = to or e.get("SMTP_TO")
    if not (user and pw and to):
        print("missing SMTP_USER/SMTP_PASS/SMTP_TO (set ~/.smtp_env)", file=sys.stderr)
        return 2
    msg = EmailMessage()
    mid = make_msgid(domain="gmail.com")
    ref = None
    if thread:                       # Task 170 F6: opt-in same-thread delivery
        base = _thread_base(thread)
        if base:
            subject = f"Re: {base}"
            ref = _thread_ref(base)
            if ref:
                msg["In-Reply-To"] = ref
                msg["References"] = ref
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = user, to, subject, mid
    # Compute the styled body once: reused for the actual send, the dryrun record,
    # and (Task 323 D2) the sent_emails.jsonl row so the dashboard can show the
    # real outbound body + deliverables instead of "not stored".
    styled = style_body(body, agent, to)
    msg.set_content(styled)
    sent_files, sent_paths = [], []
    for fp in (attachments or []):
        path = Path(fp)
        if not path.exists():
            print(f"warn: attachment not found, skipping: {fp}", file=sys.stderr)
            continue
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        sent_files.append(path.name)
        sent_paths.append(str(path.resolve()))   # absolute path -> dashboard deliverables (D2/D3)
    if os.environ.get("TSOMP_MAIL_DRYRUN"):
        # Test seam (Task 274): record the email instead of sending it over SMTP,
        # so the watchdog/jobmgr limit tests can assert on outgoing mail without a
        # real Gmail send. Never set this in production.
        drp = LOG.parent / "mail_dryrun.jsonl"
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(drp, "a") as f:
                f.write(json.dumps({"dryrun": True, "subject": msg["Subject"], "to": to,
                                    "agent": agent, "ts": time.time(),
                                    "body": styled, "attachments": sent_paths}) + "\n")
        except Exception as ex:
            print(f"warn: mail-dryrun record failed: {ex}", file=sys.stderr)
        print(f"MAIL_DRYRUN '{msg['Subject']}' -> {to}"
              + (f" (agent={agent})" if agent else "") + " [recorded, not sent]")
        return 0
    # SMTP endpoint defaults to Gmail; override SMTP_HOST/SMTP_PORT in ~/.smtp_env
    # (or the environment) to send through any other SMTP-over-SSL provider.
    smtp_host = e.get("SMTP_HOST") or os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(e.get("SMTP_PORT") or os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"message_id": mid, "agent": agent or "unknown",
               "subject": subject, "to": to, "ts": time.time(),
               "body": styled, "attachments": sent_paths}   # Task 323 D2
        if thread:
            rec["thread"] = _thread_base(thread)
            rec["in_reply_to"] = ref
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as ex:
        print(f"warn: could not log sent email: {ex}", file=sys.stderr)
    print(f"sent '{subject}' -> {to}" + (f" [+{', '.join(sent_files)}]" if sent_files else "")
          + (f" (agent={agent})" if agent else "")
          + (f" (threaded{' via ' + ref if ref else ' by subject'})" if thread else ""))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("subject")
    ap.add_argument("body", nargs="?", default="(no body)")
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--agent", default=None, help="key of the sending agent (for reply routing)")
    ap.add_argument("--to", default=None, help="recipient override (default SMTP_TO); use to reply to the actual sender")
    ap.add_argument("--thread", default=None, metavar="SUBJECT",
                    help="opt-in (Task 170 F6): thread this email onto the originating "
                         "email's subject — Subject becomes 'Re: <base>' and In-Reply-To/"
                         "References point at our latest sent email with that subject")
    a = ap.parse_args()
    sys.exit(send(a.subject, a.body, a.attach, a.agent, a.to, a.thread))
