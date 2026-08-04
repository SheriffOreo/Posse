#!/usr/bin/env python3
"""Task 384: tests for sheriff wake-on-subtask-completion (scratch_subtask_wake.py).
Self-contained; registry under a throwaway TSOMP_RECORDS_ROOT, sentinels under a
throwaway TSOMP_SCRATCH_ROOT.  Run:  python scratch_subtask_wake_test.py
"""
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

_REC = tempfile.mkdtemp(prefix="tsomp_wake_rec_")
_SCR = tempfile.mkdtemp(prefix="tsomp_wake_scr_")
os.environ["TSOMP_RECORDS_ROOT"] = _REC
os.environ["TSOMP_SCRATCH_ROOT"] = _SCR

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_subtask_wake as stw  # noqa: E402


def _sentinel(name):
    Path(_SCR, f"worker_{name}.done").write_text("")


def _rm_sentinel(name):
    p = Path(_SCR, f"worker_{name}.done")
    if p.exists():
        p.unlink()


def test_register_and_get():
    stw.register("parent1", "a,b", mode="all", note="waiting on a+b")
    r = stw.get("parent1")
    assert r["parent"] == "parent1" and r["wait_for"] == ["a", "b"], r
    assert r["mode"] == "all"


def test_is_ready_all_mode():
    stw.register("p_all", ["x", "y"], mode="all")
    _rm_sentinel("x"); _rm_sentinel("y")
    assert stw.is_ready(stw.get("p_all")) is False
    _sentinel("x")
    assert stw.is_ready(stw.get("p_all")) is False   # only 1 of 2
    _sentinel("y")
    assert stw.is_ready(stw.get("p_all")) is True     # both done


def test_is_ready_any_mode():
    stw.register("p_any", ["m", "n"], mode="any")
    _rm_sentinel("m"); _rm_sentinel("n")
    assert stw.is_ready(stw.get("p_any")) is False
    _sentinel("n")
    assert stw.is_ready(stw.get("p_any")) is True     # any one is enough


def test_done_subtasks():
    _sentinel("d1"); _rm_sentinel("d2")
    assert stw.done_subtasks(["d1", "d2"]) == ["d1"]


def test_park_refuses_without_wake():
    try:
        stw.park("nobody_registered")
    except SystemExit:
        pass
    else:
        raise AssertionError("park must refuse when no wake is registered")


def test_park_sets_sentinel_when_registered():
    stw.register("p_park", ["z"], mode="all")
    stw.park("p_park")
    assert Path(_SCR, "worker_p_park.done").exists(), "park must touch the done-sentinel"


def test_process_all_dry_wakes_only_ready():
    stw.register("ready_parent", ["s1"], mode="all")
    stw.register("waiting_parent", ["s2"], mode="all")
    _sentinel("s1"); _rm_sentinel("s2")
    woken = stw.process_all(dry=True)
    names = {w["parent"] for w in woken}
    assert "ready_parent" in names and "waiting_parent" not in names, woken


def test_process_all_clears_wake_after_waking():
    stw.register("clear_me", ["c1"], mode="all")
    _sentinel("c1")
    stw.process_all(dry=False)     # real: clears the wake (relaunch is best-effort/no-op here)
    assert stw.get("clear_me") is None, "wake must be cleared after waking"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_subtask_wake test suite  ({len(tests)} tests) ===")
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
    rc = 1
    try:
        rc = _run()
    finally:
        shutil.rmtree(_REC, ignore_errors=True)
        shutil.rmtree(_SCR, ignore_errors=True)
    sys.exit(rc)
