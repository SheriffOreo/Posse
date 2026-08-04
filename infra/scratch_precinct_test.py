#!/usr/bin/env python3
"""Case 402: regression tests for scratch_precinct — the parent-precinct resolver must
recognize "Case N" subjects, not only "Task N". The Task->Case rename left this matcher
Task-only, so every "Re: Case N: ..." reply failed to recover its parent precinct and
the receptionist re-guessed it from content (how case-400's infra follow-up became a
'paper' deputy, case 401). Self-contained.  Run:  python scratch_precinct_test.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_precinct as pc  # noqa: E402


def test_subj_regex_matches_case_and_task():
    assert pc._SUBJ_TASK.search("Re: Case 400: x").group(1) == "400"
    assert pc._SUBJ_TASK.search("Re: Task 400: x").group(1) == "400"
    assert pc._SUBJ_TASK.search("Re: case_400 foo").group(1) == "400"
    assert pc._SUBJ_TASK.search("Re: Case #400 foo").group(1) == "400"


def test_resolve_recovers_parent_precinct_from_case_subject():
    orig = pc._read_map
    pc._read_map = lambda: {"400": {"precinct": "infra", "deputy": "design_doc_and_repo_400"}}
    try:
        prec, basis = pc.resolve("999999",
                                 "Re: Case 400: S2.3 expanded with a concrete example",
                                 "some body")
        assert prec == "infra", (prec, basis)      # was '' (receptionist) before the fix
        assert basis == "parent-task:400", basis
    finally:
        pc._read_map = orig


def test_resolve_task_word_still_works():
    orig = pc._read_map
    pc._read_map = lambda: {"319": {"precinct": "paper"}}
    try:
        prec, basis = pc.resolve("999999", "Re: Task 319 FINAL: ...", "")
        assert prec == "paper" and basis == "parent-task:319", (prec, basis)
    finally:
        pc._read_map = orig


def test_resolve_no_case_number_is_receptionist():
    orig = pc._read_map
    pc._read_map = lambda: {"400": {"precinct": "infra"}}
    try:
        prec, basis = pc.resolve("999999", "just a question, no number", "body")
        assert prec is None and basis == "receptionist", (prec, basis)
    finally:
        pc._read_map = orig


def test_resolve_explicit_tag_still_beats_parent():
    # An explicit registered [precinct] tag outranks the parent-case precinct.
    orig_map, orig_reg = pc._read_map, pc.registered_precincts
    pc._read_map = lambda: {"400": {"precinct": "infra"}}
    pc.registered_precincts = lambda: {"infra", "paper", "query"}
    try:
        prec, basis = pc.resolve("999999", "Re: Case 400: move it [query]", "body")
        assert prec == "query" and basis == "explicit-tag", (prec, basis)
    finally:
        pc._read_map, pc.registered_precincts = orig_map, orig_reg


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_precinct test suite  ({len(tests)} tests) ===")
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
