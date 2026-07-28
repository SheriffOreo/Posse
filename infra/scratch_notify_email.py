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
STEVEN = "stevenfd@cmu.edu"   # the user: always greeted by name (see style_body)


def style_body(body, agent, to):
    """Communication preference: emails to Steven open with 'Hi Steven,' and every
    agent signs its name at the end. Idempotent — won't double up if already present."""
    b = (body or "").strip("\n")
    if to and to.lower() == STEVEN:
        head = b.lstrip().lower()
        if not (head.startswith("hi steven") or head.startswith("hello steven")
                or head.startswith("steven")):
            b = "Hi Steven,\n\n" + b
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
    msg.set_content(style_body(body, agent, to))
    sent_files = []
    for fp in (attachments or []):
        path = Path(fp)
        if not path.exists():
            print(f"warn: attachment not found, skipping: {fp}", file=sys.stderr)
            continue
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        sent_files.append(path.name)
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
                                    "body": style_body(body, agent, to)}) + "\n")
        except Exception as ex:
            print(f"warn: mail-dryrun record failed: {ex}", file=sys.stderr)
        print(f"MAIL_DRYRUN '{msg['Subject']}' -> {to}"
              + (f" (agent={agent})" if agent else "") + " [recorded, not sent]")
        return 0
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"message_id": mid, "agent": agent or "unknown",
               "subject": subject, "to": to, "ts": time.time()}
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
