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
import pathlib
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


def test_lineage_direct_reply_only():
    """Case 447: lineage is DIRECT-REPLY ONLY (the parent_task stamp); the old
    heuristics are off by default so a case is never INFERRED to be a follow-up.
    Also: day-view nodes carry their precinct + (Case 471) a one-sentence summary
    of the REQUEST as the readable description (not the case-log 'what was done'
    summary, and not the truncated title heading)."""
    sc = Path(_TMP) / "scratch_full_logs"
    inbox = sc / "inbox"
    records = sc / "records"
    # a GENUINE direct-reply child of 500 (explicit parent_task stamp) -> must link
    _w(inbox / "task_502.md",
       "parent_task: 500\nprecinct: eval\ndeputy: web_502\n\n"
       "# Case 502 — follow up on the sweep\n\nReply body.\n")
    # a case that only LOOKS like a reply (inbound uid-503 subject 'Re: Case 500')
    # but has parent_task: none -> must stay STANDALONE (this is the tldr-style bug)
    _w(inbox / "task_503.md",
       "parent_task: none\nprecinct: tldr\ndeputy: web_503\n\n"
       "# Case 503 — Can you plan the group trip, which is about random stuff regard\n\n"
       "Unrelated request.\n")
    _w(inbox / "503.txt",
       "FROM: user@example.com\nSUBJECT: Re: Case 500 sweep\n\n"
       "Gmail uid 503 merely mentions Case 500 in its subject.\n")
    _w(records / "task_precinct.json",
       json.dumps({"500": {"precinct": "eval", "deputy": "web_500", "basis": "spawn"},
                   "502": {"precinct": "eval", "deputy": "web_502", "basis": "spawn"},
                   "503": {"precinct": "tldr", "deputy": "web_503", "basis": "spawn"}}))
    _w(records / "tldr" / "log.tsv",
       "# case_log dept=tldr\n"
       "503\tweb_503\tscratch_full_logs/inbox/task_503.md\t"
       "Planned the group day trip: a clean one-sentence summary.\n")
    state._cache.clear()

    lin = lineage.build_lineage()
    by = {n["task_id"]: n for n in lin["nodes"]}
    check("447: genuine parent_task link kept (502->500)",
          by[502]["parent"] == 500 and by[502]["basis"] == "parent-field")
    check("447: false reply-subject NOT inferred (503 stays standalone)",
          by[503]["parent"] is None)

    dl = lineage.history_day_lineage(by[503]["day"])
    n503 = None
    for t in dl["trees"]:
        stack = [t]
        while stack:
            x = stack.pop()
            if x["task_id"] == 503:
                n503 = x
            stack.extend(x.get("children", []))
    check("447: day node carries its precinct", bool(n503) and n503.get("precinct") == "tldr")
    # Case 471: desc is now a one-sentence summary of the REQUEST (here the deterministic
    # fallback = the request/heading text, since no model summary is cached), NOT the
    # case-log 'what was done' summary ("Planned the group day trip") it used to show.
    check("471: day-node desc is the REQUEST summary, not the case-log summary",
          bool(n503) and n503.get("desc", "").startswith("Can you plan the group trip")
          and "Planned the group day trip" not in n503.get("desc", ""))


def test_request_summary_471():
    """Case 471: request_summary is the History/Status description -- a precomputed
    model summary when cached, else a deterministic first-sentence of the REQUEST
    text; request_text pulls the verbatim form block, and the reducer strips a
    leading SUBJECT:/Case N and caps a long request to one sentence."""
    sc = Path(_TMP) / "scratch_full_logs"
    inbox = sc / "inbox"
    # 600: multi-sentence complaint-then-ask, WITH a precomputed model summary cached
    _w(inbox / "task_600.md",
       "parent_task: none\nprecinct: infra\ndeputy: web_600\n\n"
       "# Case 600 — Now the thing is too long and unclear. I want it to be\n\n"
       "## Task description (verbatim from the form)\n\n"
       "Now the thing is too long and unclear. I want it to be a one sentence "
       "summary of my request, not a summary of the work.\n\n"
       "## Worker discipline\n\nEmail user.\n")
    _w(sc / "case_request_summaries.json",
       json.dumps({"600": {"summary": "Shorten descriptions to one sentence about the request.",
                            "hash": "x"}}))
    # 601: SUBJECT-prefixed one-line request, NO cache -> deterministic fallback
    _w(inbox / "task_601.md",
       "parent_task: none\nprecinct: infra\ndeputy: web_601\n\n"
       "# Case 601 — dashboard down\n\n"
       "## Task description (verbatim from the form)\n\n"
       "SUBJECT: [infra] dashboard down\n\nCan you investigate why the dashboard is down.\n\n"
       "## Worker discipline\n\nEmail user.\n")
    state._cache.clear()

    check("471: cached model summary is used verbatim",
          state.request_summary(600) == "Shorten descriptions to one sentence about the request.")
    check("471: request_text is the verbatim form block",
          state.request_text(601).startswith("SUBJECT: [infra] dashboard down"))
    check("471: fallback strips SUBJECT and yields the request",
          state.request_summary(601) == "Can you investigate why the dashboard is down.")
    # reducer: a long multi-sentence request -> first sentence, capped
    ol = state.request_oneliner("Please do the big thing. " + ("word " * 80))
    check("471: reducer keeps the first sentence", ol.startswith("Please do the big thing."))
    check("471: reducer caps length", len(ol) <= 161)
    check("471: reducer keeps an already-short request whole",
          state.request_oneliner("Add two buttons to the page.") == "Add two buttons to the page.")
    check("471: no spec -> empty (page then uses the title)",
          state.request_summary(999999) == "")


def test_lineage_tab_removed_but_reconstruction_kept():
    """Case 761: Feng asked for the Lineage TAB and any support only it used to go.
    What must survive is everything other surfaces read from this module: History's
    day mini-trees, a case's conversation + ancestors panel, and the task titles the
    JTF / Patrols agent picker searches."""
    import pages, server, jtf, lineage as lin

    assert not hasattr(pages, "lineage_page"), "the Lineage page is still defined"
    assert not hasattr(pages, "_LINEAGE_JS"), "the Lineage page JS is still defined"
    assert not hasattr(lin, "build_forest"), "the forest builder is still defined"
    nav = pages._nav("status")
    assert "/lineage" not in nav and ">Lineage<" not in nav, "the nav still links the tab"
    src = pathlib.Path(server.__file__).read_text()
    for route in ('"/lineage"', '"/api/forest"', '"/api/lineage"'):
        assert route not in src, f"server still routes {route}"
    assert not (pathlib.Path(server.__file__).parent / "lineage_figure.py").exists(), \
        "the forest figure dev tool is still present"

    # kept, and still reachable from the surfaces that use them
    for name in ("build_lineage", "history_day_lineage", "task_conversation", "lineage_for"):
        assert hasattr(lin, name), f"lineage.{name} was removed but is still used"
    assert "lineage.history_day_lineage" in src, "History lost its day trees"
    assert "lineage.task_conversation" in src and "lineage.lineage_for" in src, \
        "the case conversation panel lost its reconstruction"
    assert "build_lineage" in pathlib.Path(jtf.__file__).read_text(), \
        "the agent picker lost the task titles it searches"
    check("761: Lineage tab gone; History, conversations and the agent picker keep "
          "their reconstruction", True)


def main():
    _setup()
    for t in (test_autoack, test_conversation_scoped, test_deliverables_scoped,
              test_lineage_direct_reply_only, test_request_summary_471,
              test_lineage_tab_removed_but_reconstruction_kept):
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
