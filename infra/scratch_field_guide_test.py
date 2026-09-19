#!/usr/bin/env python3
"""Offline regression tests for Case 685's Deputies' Field Guide.

Everything runs under a temporary records root and registry.  No dashboard,
sheriff daemon, agent service, email, or live Field Guide state is touched.
Run: ``python scratch_field_guide_test.py``.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="tsomp_field_guide_test_"))
os.environ["TSOMP_RECORDS_ROOT"] = str(TEST_ROOT)
REGISTRY = TEST_ROOT / "registry.json"
REGISTRY.write_text(json.dumps({"workers": {"dep": {"session": "session-dep"}}}))
os.environ["TSOMP_REGISTRY_PATH"] = str(REGISTRY)
# The operator's allow-list decides who may hand a request to the sheriff without a
# deputy session (the dashboard's review button does exactly that). Set it BEFORE the
# import below: scratch_sheriff_request reads it once, at module load.
OPERATOR_EMAIL = "operator@example.com"
os.environ["INFRA_MAIL_ALLOWED"] = OPERATOR_EMAIL
sys.path.insert(0, str(ROOT))

import scratch_field_guide as guide  # noqa: E402
import scratch_records as rec  # noqa: E402
import scratch_sheriff as sheriff  # noqa: E402  (authorizes test sheriff process)
import scratch_sheriff_request as sreq  # noqa: E402


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# Exercise whatever this build ships rather than naming live categories: the
# release ships two, the upstream system ships more.
CAT_A = guide.CATEGORIES[0]
CAT_B = guide.CATEGORIES[-1]


def _clear():
    shutil.rmtree(TEST_ROOT / guide.FIELD_GUIDE_DIRNAME, ignore_errors=True)
    shutil.rmtree(TEST_ROOT / "sheriff_requests", ignore_errors=True)


def _lesson(category, number):
    return guide.add_lesson(
        category,
        f"Reusable lesson {number}: make the {category} deliverable expose one concrete reader takeaway.",
        deputy="dep", session="session-dep", case="685",
    )


def _pending_path(rid):
    return next(path for path in sreq.list_pending() if path.stem == rid)


def test_default_shape_and_standing_only_prompt():
    _clear()
    data = guide.show()
    assert [row["id"] for row in data["categories"]] == list(guide.CATEGORIES)
    assert not guide.standing_path().exists(), "reading defaults must not let a deputy seed policy"
    prompt = guide.prompt_block(deputy="dep", session="session-dep", case="685")
    assert "SHERIFF-OWNED" in prompt and "Pending lesson notes are" in prompt
    by_category = {row["id"]: row["guideline"] for row in data["categories"]}
    # This release ships a short starter guide: two categories the operator's
    # sheriff grows. The assertions below name what each one actually promises,
    # so a truncated or swapped default cannot pass silently.
    assert set(by_category) == {"code", "report"}
    assert "Minimize comments" in by_category["code"]
    assert "without a guided tour" in by_category["code"]
    assert "No AI slop" in by_category["report"]
    assert "One message per paragraph" in by_category["report"]
    # Every shipped category must reach the launch prompt, under its own heading.
    for cid in guide.CATEGORIES:
        assert f"### {guide.CATEGORY_LABELS[cid]}" in prompt, f"{cid} missing from prompt"
        assert by_category[cid][:40] in prompt, f"{cid} text missing from prompt"
    assert all(len(text) > 300 for text in by_category.values()), "a default looks truncated"


def test_lesson_is_optional_append_only_and_hidden_from_prompt():
    _clear()
    text = "A judge showed that labels read at normal viewing distance, not zoom level, decide whether a slide is usable."
    result = guide.add_lesson(CAT_B, text, deputy="dep", session="session-dep", case="685")
    assert result["pending"] == 1 and result["lesson"]["seq"] == 1
    assert not guide.standing_path().exists(), "a deputy lesson must not materialize/change standing policy"
    shown = guide.show(CAT_B)["categories"][0]
    assert shown["pending_lessons"][0]["text"] == text
    prompt = guide.prompt_block(deputy="dep", session="session-dep", case="685")
    assert text not in prompt, "pending lesson leaked into a fresh deputy prompt"
    try:
        guide.add_lesson(CAT_B, text, deputy="dep", session="session-dep", case="685")
    except guide.FieldGuideError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("duplicate lesson was accepted")
    try:
        guide.add_lesson(CAT_B, "This is a valid enough lesson but has a forged session.",
                         deputy="dep", session="forged", case="685")
    except guide.FieldGuideError as exc:
        assert "identity" in str(exc)
    else:
        raise AssertionError("forged deputy session was accepted")


def test_threshold_and_snapshot_preserve_late_lesson():
    _clear()
    for number in range(1, 10):
        _lesson(CAT_A, number)
    assert guide.automatic_categories() == [], "nine lessons must not trigger a rewrite"
    _lesson(CAT_A, 10)
    assert guide.automatic_categories() == [CAT_A], "ten lessons must trigger exactly that category"

    calls = []
    def runner(prompt):
        calls.append(prompt)
        # This simulates a deputy adding a new note while the sheriff is thinking.
        _lesson(CAT_A, 11)
        return FakeProc(
            "<FIELD_GUIDE>Give every visual one concrete message and a named hero. "
            "At target size, the three-second glance must make the eye land on and remember that hero.\n</FIELD_GUIDE>"
        )

    result = guide.sheriff_rewrite(CAT_A, runner=runner, trigger="automatic-threshold")
    assert result["lessons_reviewed"] == 10 and result["snapshot_through"] == 10
    assert len(calls) == 1 and "Reusable lesson 10" in calls[0]
    state = guide.show(CAT_A)["categories"][0]
    assert state["revision"] == 2 and state["incorporated_through"] == 10
    assert state["pending_count"] == 1 and state["pending_lessons"][0]["seq"] == 11


def test_failed_rewrite_keeps_all_lessons_pending():
    _clear()
    _lesson("report", 1)
    try:
        guide.sheriff_rewrite("report", runner=lambda prompt: FakeProc("not wrapped"),
                              trigger="deputy-request", reason="the report guide needs a clearer check")
    except guide.RewriteDeferred:
        pass
    else:
        raise AssertionError("malformed sheriff output was accepted")
    row = guide.show("report")["categories"][0]
    assert row["revision"] == 1 and row["pending_count"] == 1
    assert row["last_error"], "failed rewrite should leave an audit-visible error"


def test_requests_coalesce_and_operator_path_is_user_authorized():
    _clear()
    first = guide.queue_rewrite("code", reason="Add a missing reusable code-quality requirement.",
                                deputy="dep", session="session-dep", precinct="infra", case="685",
                                trigger="deputy-request")
    second = guide.queue_rewrite("code", reason="A duplicate click should not make a second request.",
                                 deputy="dep", session="session-dep", precinct="infra", case="685",
                                 trigger="deputy-request")
    assert first["queued"] is True and second["coalesced"] is True and second["id"] == first["id"]
    rec0 = sreq.get(first["id"])
    assert rec0["op"] == guide.REWRITE_OP and rec0["target"] == "code"
    op = guide.queue_rewrite(CAT_B, reason="Operator wants the pending lessons reviewed now.",
                             precinct="infra", origin="receptionist",
                             requester=OPERATOR_EMAIL, trigger="operator-manual")
    operator_record = sreq.get(op["id"])
    assert operator_record["origin"] == "receptionist"
    assert operator_record["trigger"] == "operator-manual"


def test_sheriff_tick_and_manual_request_use_rewrite_runner():
    _clear()
    for number in range(1, 11):
        _lesson(CAT_B, number)
    original = sheriff._field_guide_runner
    try:
        sheriff._field_guide_runner = lambda cfg: lambda prompt: FakeProc(
            "<FIELD_GUIDE>Each slide has one takeaway title and a named hero that wins the three-second test.</FIELD_GUIDE>"
        )
        assert sheriff.field_guide_pass(sheriff._cfg()) == 1
    finally:
        sheriff._field_guide_runner = original
    assert guide.show(CAT_B)["categories"][0]["pending_count"] == 0

    # The dashboard's manual button must run a real sheriff review even with
    # zero lessons; otherwise it would silently do nothing below the threshold.
    queued = guide.queue_rewrite("code", reason="Operator asks for a manual review of the short-comment rule.",
                                 precinct="infra", origin="receptionist",
                                 requester=OPERATOR_EMAIL, trigger="operator-manual")
    original = sheriff._field_guide_runner
    try:
        sheriff._field_guide_runner = lambda cfg: lambda prompt: FakeProc(
            "<FIELD_GUIDE>Write clear code, test real paths, and use concise comments only for information code cannot carry.</FIELD_GUIDE>"
        )
        sheriff.process_request(_pending_path(queued["id"]), sheriff._cfg())
    finally:
        sheriff._field_guide_runner = original
    done = sreq.status(queued["id"])
    assert done["state"] == "done" and done["record"]["result"]["changed"] is True
    assert done["record"]["result"]["lessons_reviewed"] == 0


def main():
    tests = [
        test_default_shape_and_standing_only_prompt,
        test_lesson_is_optional_append_only_and_hidden_from_prompt,
        test_threshold_and_snapshot_preserve_late_lesson,
        test_failed_rewrite_keeps_all_lessons_pending,
        test_requests_coalesce_and_operator_path_is_user_authorized,
        test_sheriff_tick_and_manual_request_use_rewrite_runner,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed.append(test.__name__)
            traceback.print_exc()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}: {', '.join(failed)}")
        return 1
    print(f"PASS {len(tests)}/{len(tests)} Field Guide checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
