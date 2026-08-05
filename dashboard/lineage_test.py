#!/usr/bin/env python3
"""Case 427: tests for CASE-CENTRIC conversation reconstruction.

The History-tab detail used to key a case's conversation off inbox/<uid>.txt and a
"Task N"-only subject token. After case 391a decoupled the Gmail uid from the case
number, that (a) pulled an UNRELATED thread whose Gmail uid merely equalled the case
number (the case-426 "Weekly slides" bug), (b) never surfaced a web-form request
(which is not an email), (c) dropped the deputy's "Case N" mail and its figures, and
(d) let a historical number COLLISION across deputies leak in.

These tests build a hermetic STATE_ROOT (env INFRA_STATE_ROOT) with fixture inbox +
sent-mail + records, then assert task_conversation()/deliverables_for() scope to the
right case. Self-contained (no pytest).  Run:  python lineage_test.py
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lineage_test_")
os.environ["INFRA_STATE_ROOT"] = _TMP  # MUST precede the config import

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import state  # noqa: E402
import lineage  # noqa: E402

_PASS = _FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}")


def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return str(path)


def _email(agent, subject, to="user@example.com", ts=0.0, body="", attachments=None):
    return json.dumps({"agent": agent, "subject": subject, "to": to, "ts": ts,
                       "body": body, "attachments": attachments or []})


def _setup():
    """A web case (500) that collides with an unrelated Gmail uid 500, plus a
    historical number collision: a DIFFERENT agent also sent 'Case 500' mail."""
    sc = Path(_TMP) / "scratch_full_logs"
    inbox = sc / "inbox"
    records = sc / "records"
    figs = sc / "figs"

    # web-case spec (the request lives here, NOT in an email)
    _w(inbox / "task_500.md",
       "parent_task: none\nprecinct: eval\ndeputy: web_500\n\n"
       "# Case 500 — Run the coops_tide sweep\n\n"
       "This case was submitted through the dashboard **Create new case** form.\n\n"
       "## Task description (verbatim from the form)\n\n"
       "Run the sweep on coops_tide with the 8 baselines.\n\n"
       "## Uploaded files (read these directly)\n\n"
       f"  - {_touch(sc / 'web_cases' / 'att' / 'shot.png')}\n\n"
       "## Worker discipline\n\nEmail user@example.com a plan.\n")

    # UNRELATED inbound whose Gmail uid == the case number (the collision)
    _w(inbox / "500.txt",
       "FROM: someone@example.com\nSUBJECT: Re: Weekly slides — totally unrelated\n\n"
       "This email merely has Gmail uid 500; it is NOT case 500.\n")

    # records: case 500 -> web_500 ; uid_case has no 500 (web case)
    _w(records / "task_precinct.json",
       json.dumps({"500": {"precinct": "eval", "deputy": "web_500", "basis": "spawn"},
                   "409": {"precinct": "paper", "deputy": "paper", "basis": "spawn"}}))
    _w(records / "uid_case.json", json.dumps({}))

    fig1 = _touch(figs / "fig1.png")
    final = _touch(figs / "final.png")
    formtxt = _touch(sc / "web_cases" / "att" / "submitted_form.txt")

    rows = [
        # creation auto-ack (must be EXCLUDED from convo + deliverables)
        _email("web_500", "Case 500 created (eval): Run the coops_tide sweep",
               ts=100, body="echo", attachments=[formtxt]),
        # router auto-ack (must be EXCLUDED)
        _email("web_500", "Re: your email", ts=101,
               body="Auto-ack from the inbox router: got it"),
        _email("web_500", "Case 500: starting", ts=110, body="on it"),
        # token-LESS milestone from the DEDICATED deputy (must be INCLUDED, with fig)
        _email("web_500", "Re: coops_tide sweep — timegan sweet spot", ts=120,
               body="ms", attachments=[fig1]),
        _email("web_500", "Case 500: FINAL — done", ts=130, body="final",
               attachments=[final]),
        # historical COLLISION: a different deputy's 'Case 500' mail (must be EXCLUDED)
        _email("paper", "Case 500: unrelated paper work", ts=140, body="nope"),
    ]
    _w(sc / "sent_emails.jsonl", "\n".join(rows) + "\n")
    state._cache.clear()


def test_autoack():
    check("autoack: creation subject",
          state.is_autoack("Case 500 created (eval): x", ""))
    check("autoack: assigned subject",
          state.is_autoack("Case 12 assigned (infra): y", ""))
    check("autoack: router body",
          state.is_autoack("Re: your email", "Auto-ack from the inbox router: hi"))
    check("autoack: normal mail is not an ack",
          not state.is_autoack("Case 500: FINAL — done", "final"))


def test_conversation_scoped():
    conv = lineage.task_conversation(500)
    subs = [m.get("subject", "") for m in conv["messages"]]
    joined = " || ".join(subs)
    check("convo: deputy resolved to web_500", conv["agent"] == "web_500")
    check("convo: header is the case title, not the foreign thread",
          "coops_tide" in (conv["subject"] or "") and "Weekly slides" not in (conv["subject"] or ""))
    # the uid-500 collision email must NOT appear
    check("convo: unrelated uid-500 thread excluded",
          "totally unrelated" not in joined and "Weekly slides" not in joined)
    # initial web-form request surfaced, first, with the uploaded file
    check("convo: initial is the web-form request", conv["messages"][0].get("kind") == "initial")
    check("convo: initial carries the form description",
          "Run the sweep on coops_tide" in (conv["messages"][0].get("body") or ""))
    check("convo: initial carries the uploaded file",
          any(a["name"] == "shot.png" for a in conv["messages"][0].get("attachments", [])))
    # auto-acks excluded
    check("convo: creation ack excluded", "created" not in joined)
    check("convo: router ack excluded",
          not any("Auto-ack from the inbox router" in (m.get("body") or "") for m in conv["messages"]))
    # token-less dedicated-deputy milestone included
    check("convo: token-less milestone from dedicated deputy included",
          "timegan sweet spot" in joined)
    # historical collision from another deputy excluded
    check("convo: overloaded-number mail from a different deputy excluded",
          "unrelated paper work" not in joined)
    # FINAL tagged, and it is the last message
    check("convo: FINAL tagged on the deputy's last email",
          conv["messages"][-1].get("kind") == "final" and "FINAL" in conv["messages"][-1]["subject"])


def test_deliverables_scoped():
    d = state.deliverables_for(500, None)
    names = {f["name"] for f in d["files"]}
    check("deliv: token-less milestone figure attached", "fig1.png" in names)
    check("deliv: FINAL figure attached", "final.png" in names)
    check("deliv: creation-ack form echo NOT a deliverable", "submitted_form.txt" not in names)


def main():
    _setup()
    for t in (test_autoack, test_conversation_scoped, test_deliverables_scoped):
        try:
            t()
        except Exception:
            global _FAIL
            _FAIL += 1
            print(f"[FAIL] {t.__name__} raised:\n{traceback.format_exc()}")
    print(f"\n{_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
