#!/usr/bin/env python3
"""Task 384c / Phase A1: tests for the "Sheriff = SYSTEM MANAGER" aggregator
(state.system_manager). Self-contained (no pytest).  Run:  python state_test.py

system_manager() is PURE PRESENTATION: it only COMPOSES the existing read-only
readers (sheriff_status / daemon_status / workers / limit_state). These tests
monkeypatch those four so the composition is verified hermetically -- no live
files, no daemons -- which also documents the contract A1 must not break: the
aggregator changes NO measurement, it only re-presents.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402

_PASS = _FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}")


def _patch(monkey):
    """Install fake sub-readers; return a restore() that puts the originals back."""
    orig = {k: getattr(state, k) for k in monkey}
    for k, v in monkey.items():
        setattr(state, k, v)
    return lambda: [setattr(state, k, v) for k, v in orig.items()]


def test_composes_both_loops():
    restore = _patch({
        "sheriff_status": lambda: {"alive": True, "A": 20000, "B": 10000,
                                   "llm": True, "model": "fable", "actions": []},
        "daemon_status": lambda: [{"name": "watchdog", "alive": True},
                                  {"name": "jobmgr", "alive": True},
                                  {"name": "inbox", "alive": True},
                                  {"name": "gpu_manager", "alive": False}],
        "workers": lambda active_only=False: [
            {"name": "a", "state": "running", "relaunched": 0, "mailbox_pending": False},
            {"name": "b", "state": "running", "relaunched": 2, "mailbox_pending": True},
            {"name": "c", "state": "waiting_jobs", "relaunched": 1, "mailbox_pending": False},
        ],
        "limit_state": lambda: None,
    })
    try:
        sm = state.system_manager()
        # both loops surfaced under ONE view
        check("test_composes_both_loops: sheriff_up", sm["sheriff_up"] is True)
        check("test_composes_both_loops: watchdog_up", sm["watchdog_up"] is True)
        # records-health carried through unchanged (same object the reader returned)
        check("test_composes_both_loops: records model carried",
              sm["records"]["model"] == "fable" and sm["records"]["A"] == 20000)
        # deputy-supervision summary OVER the roster
        d = sm["deputies"]
        check("test_composes_both_loops: active count", d["active"] == 3)
        check("test_composes_both_loops: by_state", d["by_state"] == {"running": 2, "waiting_jobs": 1})
        check("test_composes_both_loops: relaunches summed", d["relaunches"] == 3)
        check("test_composes_both_loops: mailbox_pending counted", d["mailbox_pending"] == 1)
        check("test_composes_both_loops: no limit", sm["limit"] is None)
    finally:
        restore()


def test_watchdog_down_and_limit_active():
    """watchdog liveness is read from daemon_status; a live usage limit passes through."""
    restore = _patch({
        "sheriff_status": lambda: {"alive": False, "A": None, "B": None,
                                   "llm": True, "model": "opus", "actions": []},
        "daemon_status": lambda: [{"name": "watchdog", "alive": False},
                                  {"name": "jobmgr", "alive": True}],
        "workers": lambda active_only=False: [],
        "limit_state": lambda: {"active": True, "kind": "session",
                                "reset_str": "3pm", "source_worker": "paper"},
    })
    try:
        sm = state.system_manager()
        check("test_watchdog_down: watchdog_up False", sm["watchdog_up"] is False)
        check("test_watchdog_down: sheriff_up False", sm["sheriff_up"] is False)
        check("test_watchdog_down: empty roster", sm["deputies"]["active"] == 0
              and sm["deputies"]["relaunches"] == 0 and sm["deputies"]["by_state"] == {})
        check("test_watchdog_down: limit surfaced",
              sm["limit"] and sm["limit"]["kind"] == "session")
    finally:
        restore()


def test_active_only_true_is_passed_to_workers():
    """Deputy supervision must summarize the ACTIVE roster (active_only=True), so a
    done/failed worker never inflates the 'active' count."""
    seen = {}

    def fake_workers(active_only=False):
        seen["active_only"] = active_only
        # emulate workers(active_only=True) already filtering terminal states
        allrows = [
            {"name": "run", "state": "running", "relaunched": 0, "mailbox_pending": False},
            {"name": "old", "state": "done", "relaunched": 5, "mailbox_pending": False},
        ]
        return [w for w in allrows if not (active_only and w["state"] in ("done", "failed"))]

    restore = _patch({
        "sheriff_status": lambda: {"alive": True, "model": "fable", "actions": []},
        "daemon_status": lambda: [{"name": "watchdog", "alive": True}],
        "workers": fake_workers,
        "limit_state": lambda: None,
    })
    try:
        sm = state.system_manager()
        check("test_active_only: workers called with active_only=True",
              seen.get("active_only") is True)
        check("test_active_only: terminal worker excluded from count",
              sm["deputies"]["active"] == 1)
        check("test_active_only: terminal relaunches not summed",
              sm["deputies"]["relaunches"] == 0)
    finally:
        restore()


# --------------------------------------------------------------------------- #
# Case 396: a long-lived agent's Status-page case must not be its FROZEN spawn
# prompt (the 'paper #367' bug). workers() must resolve the NEWEST case attributed
# to the worker in the task->precinct map when neither the board nor a launch-script
# TSOMP_CASE pins it -- but the board, when present, still wins (Task 382 #1).
# --------------------------------------------------------------------------- #
import tempfile  # noqa: E402
import config    # noqa: E402


def test_latest_attributed_case_newest_numeric():
    restore = _patch({
        "task_precinct_map": lambda: {
            "367": {"agent": "paper", "precinct": "paper"},
            "387": {"agent": "paper", "precinct": "paper"},   # newest paper-agent case
            "391b": {"deputy": "paper_ledger_rewrite", "precinct": "paper"},  # alnum, other owner
            "395": {"deputy": "web_395", "precinct": "paper"},               # numeric, other owner
        },
        "_case_log_summary": lambda prec, case: f"sum:{prec}:{case}",
    })
    try:
        case, prec, summ = state._latest_attributed_case("paper")
        check("latest_attributed: newest numeric wins (387 not 367)", case == "387")
        check("latest_attributed: precinct carried", prec == "paper")
        check("latest_attributed: summary from case log", summ == "sum:paper:387")
        # an alphanumeric sub-case owned by another worker is never mis-attributed
        oc, _, _ = state._latest_attributed_case("paper_ledger_rewrite")
        check("latest_attributed: numeric-only (skips 391b)", oc is None)
        # unattributed worker -> empty
        nc, np_, ns = state._latest_attributed_case("nobody")
        check("latest_attributed: unattributed -> (None,None,'')",
              nc is None and np_ is None and ns == "")
    finally:
        restore()


def test_worker_precinct_case_prefers_attributed_over_frozen_prompt():
    """With NO launch-script TSOMP_CASE (point STATE_ROOT at an empty dir so the
    read fails), the map attribution (387) must beat the frozen prompt tid (367)."""
    tmp = tempfile.mkdtemp()
    orig_root = config.STATE_ROOT
    config.STATE_ROOT = __import__("pathlib").Path(tmp)
    restore = _patch({
        "_latest_attributed_case": lambda name: ("387", "paper", "x") if name == "paper"
                                                 else (None, None, ""),
        "_worker_task_info": lambda name: (367, "Task 367 — birth spec"),
        "task_precinct_map": lambda: {"367": {"precinct": "paper"}},
    })
    try:
        prec, case = state._worker_precinct_case("paper")
        check("worker_precinct_case: attributed case beats prompt (387)", case == "387")
        check("worker_precinct_case: attributed precinct (paper)", prec == "paper")
        # control: an UNATTRIBUTED worker still falls back to the prompt tid
        restore2 = _patch({"_latest_attributed_case": lambda name: (None, None, "")})
        try:
            prec2, case2 = state._worker_precinct_case("legacy")
            check("worker_precinct_case: unattributed -> prompt tid fallback", case2 == "367")
            check("worker_precinct_case: unattributed -> prompt precinct", prec2 == "paper")
        finally:
            restore2()
    finally:
        restore()
        config.STATE_ROOT = orig_root


def _paper_roster_patch(board):
    """workers() with a single running 'paper' whose prompt is frozen at Task 367,
    no launch TSOMP_CASE, and attribution 387 in the map. `board` is the
    active-deputies dict the test wants to simulate."""
    tmp = tempfile.mkdtemp()
    config.STATE_ROOT = __import__("pathlib").Path(tmp)
    return _patch({
        "_read_json": lambda path, default=None: (
            [{"name": "paper", "state": "running", "session": "s"}]
            if path == config.WATCHDOG_JOBS else (default if default is not None else {})),
        "registry": lambda: {},
        "active_deputies": lambda: board,
        "_worker_task_info": lambda name: (367, "Task 367 — Spec follow-ups (birth)"),
        "_latest_attributed_case": lambda name: ("387", "paper", "Budgeted-encoding draft"),
        "_case_log_summary": lambda prec, case: "Budgeted-encoding draft"
                             if str(case) == "387" else "",
        "_mailbox_pending": lambda name: False,
    })


def test_workers_paper_fallback_shows_attributed_not_frozen():
    orig_root = config.STATE_ROOT
    restore = _paper_roster_patch(board={})   # no board entry -> fallback path
    try:
        row = state.workers()[0]
        check("workers paper: case 387 not 367", row["case"] == "387")
        check("workers paper: precinct paper", row["precinct"] == "paper")
        check("workers paper: clickable task 387", row["task"] == 387)
        check("workers paper: description refreshed (not Task-367 spec)",
              row["task_title"] == "Budgeted-encoding draft")
        check("workers paper: case_file points at 387",
              row["case_file"] == "scratch_full_logs/inbox/task_387.md")
    finally:
        restore()
        config.STATE_ROOT = orig_root


def test_workers_board_still_overrides_fallback():
    """Task 382 #1 invariant preserved: a board entry is authoritative and wins over
    the map-attribution fallback (and its description is NOT overwritten)."""
    orig_root = config.STATE_ROOT
    restore = _paper_roster_patch(board={
        "paper": {"case": "390", "description": "boarded desc", "precinct": "paper"}})
    try:
        row = state.workers()[0]
        check("workers paper: board case 390 wins", row["case"] == "390")
        check("workers paper: board description wins", row["task_title"] == "boarded desc")
    finally:
        restore()
        config.STATE_ROOT = orig_root


def main():
    for fn in (test_composes_both_loops,
               test_watchdog_down_and_limit_active,
               test_active_only_true_is_passed_to_workers,
               test_latest_attributed_case_newest_numeric,
               test_worker_precinct_case_prefers_attributed_over_frozen_prompt,
               test_workers_paper_fallback_shows_attributed_not_frozen,
               test_workers_board_still_overrides_fallback):
        try:
            fn()
        except Exception:
            global _FAIL
            _FAIL += 1
            print(f"[FAIL] {fn.__name__} raised:")
            traceback.print_exc()
    print(f"=== SUMMARY: {_PASS} passed, {_FAIL} failed ===")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
