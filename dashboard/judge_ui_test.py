#!/usr/bin/env python3
"""Case 569: tests for the reworked 'Propose a new judge' surface.

The form used to ask for a judge id AND a display name, a one-line description, the
judge's whole system prompt, and the reason the sheriff should approve it -- then
filed the critic_add itself. It now asks for a NAME and a DESCRIPTION of what the
judge should care about, and opens a receptionist CASE whose deputy writes the
prompt and files the request.

Covers: the page render + JS (old fields gone, new ones wired), the name->id
derivation, and the POST handler driven end to end against a throwaway queue dir --
what it rejects, and the exact record it drops for the bridge. Pure/offline: no
server is started, nothing is spawned, no email is sent.  Run: python judge_ui_test.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config   # noqa: E402
import pages    # noqa: E402
import server   # noqa: E402


# --------------------------------------------------------------------------- #
# a minimal stand-in for the HTTP plumbing _propose_critic needs
# --------------------------------------------------------------------------- #
class FakeHandler(server.Handler):
    def __init__(self, payload):
        body = json.dumps(payload).encode()
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self.replied = None

    def _json(self, obj, code=200):
        self.replied = (code, obj)
        return obj


def _post(payload):
    h = FakeHandler(payload)
    h._propose_critic()
    return h.replied


def _queued():
    """Every record sitting in the (redirected) web-case pending dir."""
    d = config.WEB_CASES / "pending"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def _clear():
    d = config.WEB_CASES / "pending"
    if d.exists():
        shutil.rmtree(d)


# --------------------------------------------------------------------------- #
# the page + its JS
# --------------------------------------------------------------------------- #
def test_form_has_one_name_field_and_a_description():
    p = pages.judges_page()
    assert "id='jname'" in p, "the Name field is missing"
    assert "id='jdesc'" in p, "the Description field is missing"
    assert "id='jmodel'" in p, "the Model selector was dropped"
    # the four fields Case 569 removed
    for gone, what in [("id='jid'", "separate Judge id field"),
                       ("id='jprompt'", "custom-prompt textarea"),
                       ("id='jreason'", "sheriff-reason field")]:
        assert gone not in p, f"the {what} is still on the form"
    assert "One-line description" not in p, "the one-line description field is still there"
    # Description must be a textarea (prose), not the old single-line input
    i = p.index("id='jdesc'")
    assert "textarea" in p[max(0, i - 120):i], "Description is not a textarea"


def test_form_says_what_actually_happens():
    p = pages.judges_page()
    for needle in ["receptionist", "sheriff", "critic_add"]:
        assert needle in p, f"the form never mentions {needle!r}"
    assert "prompt you wrote" not in p, "stale copy: the user no longer writes the prompt"


def test_js_posts_name_and_description_only():
    js = pages._JUDGES_JS
    assert "/judges/propose" in js, "submit not wired"
    assert "name:name" in js and "description:desc" in js, "new payload not posted"
    for stale in ["prompt:prompt", "reason:reason", "display_name:"]:
        assert stale not in js, f"stale field still posted: {stale}"
    # the reply now names a CASE, not a sheriff request id
    assert "d.case" in js, "the confirmation does not report the case number"


# --------------------------------------------------------------------------- #
# name -> id
# --------------------------------------------------------------------------- #
def test_id_derivation():
    f = server.Handler._judge_id_from_name
    cases = {
        "The Vyas Judge": "vyas",                 # article + trailing noun dropped
        "vyas": "vyas",
        "The Reproducibility Judge": "reproducibility",
        "Reproducibility Critic": "reproducibility",
        "Cost Reviewer": "cost",
        "A Careful Reader": "careful_reader",
        "Judge": "judge",                         # sole word survives: never empty
        "  spaced   out  ": "spaced_out",
        "Mr. O'Brien": "mr_o_brien",
        "7 of 9": "j_7_of_9",                     # must start with a letter
    }
    for name, want in cases.items():
        got = f(name)
        assert got == want, f"{name!r} -> {got!r}, wanted {want!r}"


def test_id_derivation_never_returns_junk():
    f = server.Handler._judge_id_from_name
    for name in ["", "   ", "!!!", None, "---"]:
        assert f(name) == "", f"{name!r} produced an id"
    assert len(f("x" * 200)) <= 32, "id not length-capped"


# --------------------------------------------------------------------------- #
# the POST handler
# --------------------------------------------------------------------------- #
_GOOD_DESC = ("Refuse to sign off unless every number in the report traces back to a "
              "command I can re-run myself.")


def test_rejects_thin_submissions():
    for payload, why in [
        ({"name": "X", "description": _GOOD_DESC}, "one-character name"),
        ({"name": "", "description": _GOOD_DESC}, "empty name"),
        ({"name": "The Cost Judge", "description": "be strict"}, "one-line description"),
        ({"name": "The Cost Judge", "description": ""}, "empty description"),
        ({"name": "!!!", "description": _GOOD_DESC}, "name with no usable id"),
        ({"name": "The Cost Judge", "description": _GOOD_DESC, "model": "gpt-9"},
         "unknown model"),
    ]:
        code, obj = _post(payload)
        assert code == 400 and not obj["ok"], f"accepted a {why}: {obj}"
    assert not _queued(), "a rejected submission still queued a case"


def test_rejects_reserved_and_taken_ids():
    code, obj = _post({"name": "charter", "description": _GOOD_DESC})
    assert code == 400 and "reserved" in obj["error"], obj
    # 'anonymous' and 'vyas' are on the live roster
    code, obj = _post({"name": "The Anonymous Judge", "description": _GOOD_DESC})
    assert code == 400 and "already exists" in obj["error"], obj
    assert not _queued(), "a rejected submission still queued a case"


def test_good_submission_queues_a_receptionist_case():
    _clear()
    code, obj = _post({"name": "The Reproducibility Judge",
                       "description": _GOOD_DESC, "model": "opus"})
    assert code == 200 and obj["ok"], obj
    assert obj["id"] == "reproducibility" and obj["case"], obj
    recs = _queued()
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["precinct"] == "receptionist", r["precinct"]
    assert r["source"] == "web_judge"
    assert r["judge_proposal"] == {"id": "reproducibility",
                                   "name": "The Reproducibility Judge",
                                   "model": "opus"}
    assert r["description"] == _GOOD_DESC, "the user's words must reach the deputy verbatim"
    assert r["deputy_hint"] == f"judge_{r['case']}", r["deputy_hint"]
    assert r["case"] == obj["case"]
    # the judge is a proposal, not this case's reviewer
    assert r["critic"] is None, "the proposal must not also assign a judge to its own case"
    _clear()


def test_model_is_optional():
    _clear()
    code, obj = _post({"name": "The Cost Judge", "description": _GOOD_DESC})
    assert code == 200 and obj["ok"], obj
    assert _queued()[0]["judge_proposal"]["model"] == "", "empty model not carried as empty"
    _clear()


def test_handler_never_writes_the_registry():
    """The dashboard proposes; only the sheriff writes records/critics.json."""
    reg = Path(config.STATE_ROOT) / "scratch_full_logs" / "records" / "critics.json"
    before = reg.read_bytes() if reg.exists() else None
    _clear()
    _post({"name": "The Traceability Judge", "description": _GOOD_DESC})
    after = reg.read_bytes() if reg.exists() else None
    assert before == after, "the proposal path touched the critic registry"
    _clear()


def main():
    # Redirect the queue at a throwaway dir so a test run never drops a real case.
    tmp = Path(tempfile.mkdtemp(prefix="judge_ui_test_"))
    config.WEB_CASES = tmp / "web_cases"
    (config.WEB_CASES / "pending").mkdir(parents=True, exist_ok=True)
    # ...and stub the allocator. _allocate_case shells out to the LIVE
    # scratch_case_seq.py, so an un-stubbed run burns a real case number per test
    # (it burned three before this was caught). Case numbers are a shared, global,
    # monotonic resource -- a test may never consume one.
    server.Handler._allocate_case = lambda self: 900569
    passed = failed = 0
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                fn()
                print(f"[PASS] {name}")
                passed += 1
            except Exception:
                print(f"[FAIL] {name}")
                traceback.print_exc()
                failed += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"=== SUMMARY: {passed} passed, {failed} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
