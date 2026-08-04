#!/usr/bin/env python3
"""Case 402: regression tests for scratch_task_parent — the parent-task stamp must read
"Case N" subjects, not only "Task N". The Task->Case rename left _SUBJECT_TASK Task-only,
so a reply "Re: Case 400 ..." never yielded parent=400 and the stamp fell back to the
worker's last task (case 401's stamp computed parent=393, not 400). Pure functions, no
IO.  Run:  python scratch_task_parent_test.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_task_parent as tp  # noqa: E402


def test_subject_parent_matches_case():
    assert tp._subject_parent("Re: Case 400: S2.3 expanded", 401) == 400


def test_subject_parent_matches_task():
    assert tp._subject_parent("Re: Task 319 FINAL: ...", 320) == 319


def test_subject_parent_ignores_own_uid():
    # A reply names its PARENT; never resolve to the child's own case number.
    assert tp._subject_parent("Re: Case 402: foo", 402) is None


def test_subject_parent_none_when_no_number():
    assert tp._subject_parent("just a question", 402) is None


def test_compute_parent_prefers_case_subject():
    parent, basis = tp.compute_parent(401, "d400", "Re: Case 400: x", {"d400": 393})
    assert parent == 400 and basis == "reply-subject", (parent, basis)


def test_compute_parent_falls_back_to_last_task_without_case():
    parent, basis = tp.compute_parent(401, "d400", "no case number here", {"d400": 393})
    assert parent == 393 and basis == "worker-last-task", (parent, basis)


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_task_parent test suite  ({len(tests)} tests) ===")
    passed, failed = 0, []
    for t in tests:
        try:
            t()
        except Exception:
            failed.append(t.__name__)
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"[PASS] {t.__name__}")
    print(f"=== SUMMARY: {passed} passed, {len(failed)} failed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
