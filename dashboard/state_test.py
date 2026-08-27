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


def test_emailed_attachments_downloadable_flag():
    """Case 512: an emailed attachment OUTSIDE the download roots (another
    precinct's project dir, or an ephemeral /tmp figure) must still be SHOWN,
    marked downloadable=False -- NOT silently dropped (that drop is what rendered
    '(none found)' for a case whose FINAL truly emailed a PDF). In-root files stay
    downloadable=True; the creation auto-ack's form upload is still excluded; a
    re-attach in a follow-up dedups."""
    import json as _json
    tmp = Path(tempfile.mkdtemp())
    sent = tmp / "sent_emails.jsonl"
    rows = [
        # creation auto-ack: its submitted_form.txt must NOT become a deliverable
        {"agent": "web_z", "subject": "Case 777 created (deepcap): do the thing",
         "body": "", "attachments": [str(tmp / "submitted_form.txt")]},
        # FINAL: an in-root pdf + a cross-precinct pdf + an ephemeral /tmp figure
        {"agent": "web_z", "subject": "Case 777 FINAL: report (PDF attached)",
         "body": "final", "attachments": [
             "/root/state/reports/in_root.pdf",
             "/home/x/OtherProj/docs/out_of_root.pdf",
             "/tmp/fig777.png"]},
        # a follow-up re-attach of the SAME files (must dedup, not double up)
        {"agent": "web_z", "subject": "Re: Case 777 FINAL: updated report",
         "body": "v2", "attachments": [
             "/home/x/OtherProj/docs/out_of_root.pdf", "/tmp/fig777.png"]},
    ]
    sent.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")

    def fake_resolve(p):
        s = str(p)
        return Path(s) if s.startswith("/root/state/") else None

    orig_sent, orig_resolve = config.SENT_EMAILS, config.resolve_download
    config.SENT_EMAILS, config.resolve_download = str(sent), fake_resolve
    try:
        files = state._emailed_attachments("web_z", 777)
        by = {f["name"]: f for f in files}
        check("emailed512: in-root pdf downloadable",
              by.get("in_root.pdf", {}).get("downloadable") is True)
        check("emailed512: cross-precinct pdf SHOWN, non-downloadable",
              by.get("out_of_root.pdf", {}).get("downloadable") is False)
        check("emailed512: /tmp figure SHOWN, non-downloadable",
              by.get("fig777.png", {}).get("downloadable") is False)
        check("emailed512: creation-ack form upload excluded",
              "submitted_form.txt" not in by)
        check("emailed512: re-attached cross-precinct pdf appears once",
              sum(1 for f in files if f["name"] == "out_of_root.pdf") == 1)
    finally:
        config.SENT_EMAILS, config.resolve_download = orig_sent, orig_resolve


# --------------------------------------------------------------------------- #
# Case 555: critic_review_detail / critic_review_prompt — the full-ruling readers
# behind the Judge page's "read ruling" button. Hermetic: a fake critic_reviews
# tree in a tmpdir, so no live review is read and none can be written.
# --------------------------------------------------------------------------- #
def _fake_reviews(root):
    """case_777: r1 REVISE (dict must_fix) -> r2 SIGN-OFF -> r10 (numeric-sort
    canary) -> r11 pending (assigned, judge has not ruled).  case_778: a judge
    that self-contradicted (SIGN-OFF carrying a must-fix) + string-shaped
    findings, i.e. both tolerated deviations from the charter's schema."""
    import json as _json

    def w(case, rnd, meta, verdict=None, md=None, prompt=None):
        d = root / f"case_{case}" / f"round_{rnd}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(_json.dumps(meta))
        if verdict is not None:
            (d / "verdict.json").write_text(_json.dumps(verdict))
        if md is not None:
            (d / "verdict.md").write_text(md)
        if prompt is not None:
            (d / "prompt.md").write_text(prompt)

    m = {"critic": "anonymous", "model": "opus", "anon": "critic_777_anonymous_r1",
         "ts": 1000, "artifacts": ["/x/a.pdf", "/x/b.tex"], "missing_artifacts": []}
    w("777", 1, m,
      {"verdict": "REVISE", "one_line": "not yet",
       "must_fix": [{"location": "a.tex:12", "problem": "wrong number", "fix": "say 37"}],
       "should_fix": ["tighten §2"], "keep": ["the storage argument"],
       "unverified": ["remote sizes"], "round": 1},
      "VERDICT: REVISE\n\nround one prose " + "x" * 5000, "PROMPT ONE " + "p" * 900)
    w("777", 2, dict(m, ts=2000),
      {"verdict": "SIGN-OFF", "one_line": "fixed", "must_fix": [],
       "should_fix": [], "keep": ["fix landed"], "unverified": [], "round": 2},
      "VERDICT: SIGN-OFF\n\nround two prose", "PROMPT TWO")
    w("777", 10, dict(m, ts=3000),
      {"verdict": "SIGN-OFF", "one_line": "tenth", "round": 10},
      "VERDICT: SIGN-OFF\n\nround ten prose", "PROMPT TEN")
    w("777", 11, dict(m, ts=4000))                     # assigned, no verdict yet
    w("778", 1, {"critic": "vyas", "model": "opus", "ts": 5000, "artifacts": []},
      {"verdict": "signoff", "one_line": "contradiction",
       "must_fix": ["a bare string finding"],
       "should_fix": [{"location": "p3", "problem": "thin", "fix": "expand"}],
       "keep": [], "unverified": [], "round": 1},
      "VERDICT: SIGN-OFF\n\nbody", "P")


def test_critic_review_detail_all_rounds():
    import tempfile
    import config
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _fake_reviews(root)
        orig, config.CRITIC_REVIEWS = config.CRITIC_REVIEWS, root
        try:
            d = state.critic_review_detail("777")
            rs = d["rounds"]
            check("555 detail: every round returned", len(rs) == 4)
            check("555 detail: chronological, round_10 sorts NUMERICALLY after 2",
                  [r["round"] for r in rs] == [1, 2, 10, 11])
            check("555 detail: verdict.md returned in FULL (not truncated)",
                  len(rs[0]["verdict_md"]) == len("VERDICT: REVISE\n\nround one prose ") + 5000)
            check("555 detail: must_fix dict normalised",
                  rs[0]["must_fix"][0] == {"location": "a.tex:12",
                                           "problem": "wrong number", "fix": "say 37"})
            check("555 detail: should_fix/keep/unverified carried",
                  rs[0]["should_fix"] == ["tighten §2"] and rs[0]["keep"] == ["the storage argument"]
                  and rs[0]["unverified"] == ["remote sizes"])
            check("555 detail: meta carried (judge/model/ts/artifacts)",
                  rs[0]["critic"] == "anonymous" and rs[0]["model"] == "opus"
                  and rs[0]["ts"] == 1000 and rs[0]["artifacts"] == ["/x/a.pdf", "/x/b.tex"])
            check("555 detail: prompt NOT inlined, only sized",
                  "prompt" not in rs[0] and rs[0]["prompt_chars"] == len("PROMPT ONE ") + 900)
            check("555 detail: unruled round flagged pending, not blank-verdict",
                  rs[3]["pending"] is True and rs[3]["verdict"] == "")
            check("555 detail: latest = last RULED round; trailing unruled round "
                  "reported as in_flight, not as 'no verdict'",
                  d["latest"] == "SIGN-OFF" and d["in_flight"] is True
                  and d["signed_off"] is True and d["critic"] == "anonymous")
        finally:
            config.CRITIC_REVIEWS = orig


def test_critic_review_detail_failclosed_and_shapes():
    import tempfile
    import config
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _fake_reviews(root)
        orig, config.CRITIC_REVIEWS = config.CRITIC_REVIEWS, root
        try:
            r = state.critic_review_detail("778")["rounds"][0]
            # a SIGN-OFF carrying must-fixes is a REVISE (mirrors the harness), and
            # the modal must be able to SAY the badge was coerced.
            check("555 failclosed: 'signoff' normalised then downgraded",
                  r["verdict"] == "REVISE" and r["raw_verdict"] == "SIGN-OFF"
                  and r["coerced"] is True)
            check("555 shapes: string must_fix survives as a problem",
                  r["must_fix"] == [{"location": "", "problem": "a bare string finding",
                                     "fix": ""}])
            check("555 shapes: dict in should_fix flattened to a line",
                  r["should_fix"] == ["p3 — thin — expand"])
        finally:
            config.CRITIC_REVIEWS = orig


def test_critic_review_prompt_and_path_guard():
    import tempfile
    import config
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _fake_reviews(root)
        # a file the guard must never reach by climbing out of critic_reviews/
        (root.parent / "secret.md").write_text("TOP SECRET")
        orig, config.CRITIC_REVIEWS = config.CRITIC_REVIEWS, root
        try:
            check("555 prompt: fetched per round",
                  state.critic_review_prompt("777", 2) == "PROMPT TWO")
            check("555 prompt: string round number accepted",
                  state.critic_review_prompt("777", "2") == "PROMPT TWO")
            check("555 prompt: unknown/non-numeric round -> empty, no raise",
                  state.critic_review_prompt("777", 99) == ""
                  and state.critic_review_prompt("777", "x") == "")
            bad = ["../", "..", "777/../778", "/etc/passwd", "", None, "a" * 33,
                   "case_777", "777\x00", "77.7"]
            check("555 guard: traversal/junk case ids yield no rounds",
                  all(not state.critic_review_detail(b)["rounds"] for b in bad))
            check("555 guard: traversal case ids yield no prompt",
                  all(state.critic_review_prompt(b, 1) == "" for b in bad))
            check("555 guard: unknown but well-formed case is empty, not an error",
                  state.critic_review_detail("999") ==
                  {"case": "999", "rounds": [], "critic": "", "latest": "",
                   "in_flight": False, "signed_off": False})
        finally:
            config.CRITIC_REVIEWS = orig


def main():
    for fn in (test_critic_review_detail_all_rounds,
               test_critic_review_detail_failclosed_and_shapes,
               test_critic_review_prompt_and_path_guard,
               test_composes_both_loops,
               test_emailed_attachments_downloadable_flag,
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
