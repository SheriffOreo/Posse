#!/usr/bin/env python3
"""Task 384c / Phase A1: tests for the "Sheriff = SYSTEM MANAGER" aggregator
(state.system_manager). Self-contained (no pytest).  Run:  python state_test.py

system_manager() is PURE PRESENTATION: it only COMPOSES the existing read-only
readers (sheriff_status / daemon_status / workers / limit_state). These tests
monkeypatch those four so the composition is verified hermetically -- no live
files, no daemons -- which also documents the contract A1 must not break: the
aggregator changes NO measurement, it only re-presents.
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
import pages  # noqa: E402
import server  # noqa: E402

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

    def fake_workers(active_only=False, include_failed=False):
        seen["active_only"] = active_only
        seen["include_failed"] = include_failed
        # Case 591: mirror the REAL filter. It used to emulate the pre-591 one, so
        # it kept dropping `failed` no matter what workers() actually did and could
        # not have caught the Status board's include_failed leaking in here.
        allrows = [
            {"name": "run", "state": "running", "relaunched": 0, "mailbox_pending": False},
            {"name": "dead", "state": "failed", "relaunched": 5, "mailbox_pending": False},
            {"name": "old", "state": "done", "relaunched": 5, "mailbox_pending": False},
        ]
        return [w for w in allrows if not (active_only and (
            w["state"] == "done" or (w["state"] == "failed" and not include_failed)))]

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
        check("test_active_only: failed deputies are NOT counted as supervised",
              seen.get("include_failed") is False
              and "failed" not in sm["deputies"]["by_state"])
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


def test_workers_failed_filter_is_opt_in():
    """Case 591: exercise the REAL filter, not a fake of it. The Status board asks
    for failed deputies (include_failed=True); system_manager, the other
    active_only caller, counts supervised deputies and must not see them."""
    orig_root = config.STATE_ROOT
    tmp = tempfile.mkdtemp()
    config.STATE_ROOT = __import__("pathlib").Path(tmp)
    # Case 782 gates these controls on their backend being installed; this
    # test is about the state logic, so give the sandbox the programs.
    (config.STATE_ROOT / "scratch_watchdog_control.py").write_text("# stub\n")
    (config.STATE_ROOT / "scratch_deputy_message.py").write_text("# stub\n")
    roster = [{"name": "run", "state": "running", "session": "s"},
              {"name": "dead", "state": "failed", "session": "s"},
              {"name": "old", "state": "done", "session": "s"}]
    restore = _patch({
        "_read_json": lambda path, default=None: (
            roster if path == config.WATCHDOG_JOBS
            else (default if default is not None else {})),
        "registry": lambda: {},
        "active_deputies": lambda: {},
        "_worker_task_info": lambda name: (None, ""),
        "_latest_attributed_case": lambda name: (None, None, ""),
        "_case_log_summary": lambda prec, case: "",
        "_mailbox_pending": lambda name: False,
        "request_summary": lambda case: "",
    })
    try:
        names = lambda **kw: sorted(w["name"] for w in state.workers(**kw))
        check("workers: active_only alone still drops failed AND done",
              names(active_only=True) == ["run"])
        check("workers: include_failed adds failed but never done",
              names(active_only=True, include_failed=True) == ["dead", "run"])
        check("workers: unfiltered returns everything", names() == ["dead", "old", "run"])
        row = [w for w in state.workers() if w["name"] == "dead"][0]
        check("workers: a failed row offers relaunch and an operator model override",
              row["can_relaunch"] is True and row["can_switch"] is True)
        run = [w for w in state.workers() if w["name"] == "run"][0]
        check("workers: a running row offers switch, not relaunch",
              run["can_switch"] is True and run["can_relaunch"] is False)
    finally:
        restore()
        config.STATE_ROOT = orig_root


def test_can_message_needs_a_relaunch_script():
    """Case 616: the reply button is offered on the one condition that decides
    whether a message can be READ — a relaunch script on disk. Deliberately NOT
    gated on state (a failed or limit-parked deputy is exactly one you may need
    to write to), and the name is validated because it becomes a filename."""
    orig_root = config.STATE_ROOT
    tmp = Path(tempfile.mkdtemp())
    config.STATE_ROOT = tmp
    (tmp / "scratch_worker_alive_relaunch.sh").write_text("#!/bin/bash\n")
    # Case 782 gates these controls on their backend being installed; this
    # test is about the state logic, so give the sandbox the programs.
    (tmp / "scratch_deputy_message.py").write_text("# stub\n")
    (tmp / "scratch_watchdog_control.py").write_text("# stub\n")
    try:
        check("can_message: a deputy with a script", state.can_message("alive") is True)
        check("can_message: one without", state.can_message("ghost") is False)
        check("can_message: traversal refused",
              state.can_message("../../etc/passwd") is False)
        check("can_message: empty refused", state.can_message("") is False)
        # ...and it reaches the row, for EVERY state (drive the real workers()).
        roster = [{"name": "alive", "state": "failed", "session": "s"},
                  {"name": "ghost", "state": "running", "session": "s"}]
        restore = _patch({
            "_read_json": lambda path, default=None: (
                roster if path == config.WATCHDOG_JOBS
                else (default if default is not None else {})),
            "registry": lambda: {},
            "active_deputies": lambda: {},
            "_worker_task_info": lambda name: (None, ""),
            "_latest_attributed_case": lambda name: (None, None, ""),
            "_case_log_summary": lambda prec, case: "",
            "_mailbox_pending": lambda name: False,
            "request_summary": lambda case: "",
        })
        try:
            rows = {w["name"]: w for w in state.workers()}
            check("can_message: a FAILED deputy can still be written to",
                  rows["alive"]["can_message"] is True)
            check("can_message: a running one without a script cannot",
                  rows["ghost"]["can_message"] is False)
        finally:
            restore()
    finally:
        config.STATE_ROOT = orig_root


def test_precinct_cases_offer_followup_only_with_a_revivable_deputy():
    """Case 616: a follow-up revives the case's OWN deputy, so a row with no
    deputy (every pre-Task-377 case-log line) or a deputy whose script is gone
    gets no button — not a button that fails."""
    orig_root = config.STATE_ROOT
    tmp = Path(tempfile.mkdtemp())
    config.STATE_ROOT = tmp
    (tmp / "scratch_worker_web_600_relaunch.sh").write_text("#!/bin/bash\n")
    # Case 782: a follow-up is delivered by scratch_deputy_message.py; this test is
    # about which ROWS may be revived, so the sandbox gets the program.
    (tmp / "scratch_deputy_message.py").write_text("# stub\n")
    log = ("600\tweb_600\tcases/task_600.md\tnarrowed the vyas judge\n"
           "590\tweb_590\tcases/task_590.md\tdeputy is gone from disk\n"
           "100\tcases/task_100.md\tan old three-field row with no deputy\n")
    restore = _patch({
        "_read_json": lambda path, default=None: (
            {"precincts": {"infra": {"ledger_mode": "mutable", "model": "opus",
                                     "description": "d"}}}),
        "_read_text": lambda p: (log if str(p).endswith("log.tsv") else "ledger"),
        "_deputies_for": lambda name, m: [],
    })
    try:
        rows = {c["task"]: c for c in state.precinct_detail("infra")["cases"]}
        check("followup: offered when the deputy can be revived",
              rows["600"]["can_followup"] is True)
        check("followup: not offered when its script is gone",
              rows["590"]["can_followup"] is False)
        check("followup: not offered for a pre-377 row with no deputy",
              rows["100"]["deputy"] == "" and rows["100"]["can_followup"] is False)
    finally:
        restore()
        config.STATE_ROOT = orig_root


def test_legacy_duplicate_case_rows_are_one_current_case_everywhere():
    """Old physical duplicate rows must not become duplicate UI entries or summaries."""
    log = (
        "672\tweb_672\tcases/task_672.md\tfirst delivery\n"
        "672a\tweb_672a\tcases/task_672a.md\tsubtask stays distinct\n"
        "672\tweb_672\tcases/task_672.md\tsecond delivery\n"
        "673\tweb_673\tcases/task_673.md\tother case\n"
        "672\tweb_672\tcases/task_672.md\tlatest delivery\n"
    )
    parsed = state._parse_case_log(log)
    check("case log canonical: one 672 plus 672a and 673", len(parsed) == 3)
    by_task = {row["task"]: row for row in parsed}
    check("case log canonical: latest duplicate wins",
          by_task["672"]["summary"] == "latest delivery")
    check("case log canonical: subcase remains independent",
          by_task["672a"]["summary"] == "subtask stays distinct")

    restore = _patch({
        "_read_json": lambda path, default=None: (
            {"precincts": {"deepcap-paper": {
                "ledger_mode": "mutable", "model": "terra", "description": "paper",
            }}} if path == config.PRECINCTS_JSON
            else (default if default is not None else {})),
        "_read_text": lambda p: (log if str(p).endswith("log.tsv") else "ledger"),
        "_deputies_for": lambda name, m: [],
        "task_precinct_map": lambda: {"672": {"precinct": "deepcap-paper"}},
    })
    try:
        detail = state.precinct_detail("deepcap-paper")
        check("case log detail: three unique cases", len(detail["cases"]) == 3)
        check("case log summary: latest matches canonical detail",
              state._case_log_summary("deepcap-paper", "672") == "latest delivery")
        prec, summary = state.case_meta("672")
        check("case meta: latest matches canonical detail",
              prec == "deepcap-paper" and summary == "latest delivery")

        import pages
        original = state.precinct_detail
        state.precinct_detail = lambda name: detail if name == "deepcap-paper" else None
        try:
            page = pages.precinct_detail_page("deepcap-paper")
        finally:
            state.precinct_detail = original
        check("case log page: unique count is rendered",
              "3 closed cases" in page and "current index; one row per case" in page)
        check("case log page: only newest 672 summary renders",
              page.count("latest delivery") == 1 and "first delivery" not in page
              and "second delivery" not in page)
    finally:
        restore()


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


# --------------------------------------------------------------------------- #
# Case 681: dashboard account-status reader.  These are deliberately fixture
# files, not a live account setup: the test asserts the API exposes only an
# operator-facing label/email and never an account home, key path, or token.
# --------------------------------------------------------------------------- #
def test_account_limit_and_auth_states_are_labelled_and_curated():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        registry_path, state_path = root / "accounts.json", root / "account_state.json"
        legacy_limit = root / "legacy_limit_state.json"
        registry_path.write_text(json.dumps({
            "version": 1,
            "accounts": [
                {"id": "claude-default", "service": "claude",
                 "label": "Default Claude account", "email": None, "order": 0,
                 "config_dir": "/private/claude-home", "key_path": None},
                {"id": "claude-feng", "service": "claude", "label": "Ada",
                 "email": "operator@example.com", "order": 1,
                 "config_dir": "/private/feng-home", "key_path": "/private/key"},
                {"id": "chatgpt-default", "service": "chatgpt",
                 "label": "Default ChatGPT account", "email": None, "order": 0},
            ],
        }))
        state_path.write_text(json.dumps({
            "version": 1,
            "accounts": {
                "claude-default": {
                    "limit": {"active": True, "kind": "account", "reset_epoch": 240,
                              "message": "try again at 4pm", "updated_at": 1},
                    "auth": {"active": False, "message": "", "credential_mtime": 1},
                },
                "claude-feng": {
                    "limit": {"active": True, "kind": "account", "reset_epoch": 300,
                              "message": "limited", "updated_at": 1},
                    "auth": {"active": True, "message": "login expired",
                             "credential_mtime": 1, "refresh_token": "NEVER-EXPOSE-ME"},
                },
                # The passed reset must not keep a stale account banner alive.
                "chatgpt-default": {
                    "limit": {"active": True, "kind": "account", "reset_epoch": 90,
                              "message": "old", "updated_at": 1},
                    "auth": {"active": False, "message": "", "credential_mtime": None},
                },
            },
        }))
        # The old service-wide file remains during rollout, but its generic row
        # must disappear once account-local state names the live Claude account.
        legacy_limit.write_text(json.dumps({
            "active": True, "service": "claude", "kind": "account",
            "reset_epoch": 260, "source_worker": "legacy-worker",
        }))
        old_registry, old_state = config.ACCOUNT_REGISTRY, config.ACCOUNT_STATE
        old_services, old_models = config.LIMIT_STATE_SERVICES, config.MODEL_LIMIT_STATE
        config.ACCOUNT_REGISTRY, config.ACCOUNT_STATE = registry_path, state_path
        # Isolate this test from any live legacy marker or per-model test residue.
        config.LIMIT_STATE_SERVICES = {"claude": legacy_limit}
        config.MODEL_LIMIT_STATE = root / "models.json"
        try:
            limits = state.limit_states(now=100)
            by_account = {x.get("account_id"): x for x in limits}
            check("681 account limits: one record per live account",
                  set(by_account) == {"claude-default", "claude-feng"})
            check("681 account limits: named state suppresses generic legacy row",
                  all(x.get("source_worker") != "legacy-worker" for x in limits))
            check("681 account limits: label/email names the account",
                  by_account["claude-feng"]["scope_label"] ==
                  "Claude account Ada (operator@example.com)")
            check("681 account limits: default label survives registry fallback",
                  by_account["claude-default"]["scope_label"] ==
                  "Claude account Default Claude account")
            check("681 account limits: producer message reaches the banner field",
                  by_account["claude-default"]["reset_str"] == "try again at 4pm")
            auths = state.auth_states()
            check("681 auth states: only the blocked account is listed",
                  len(auths) == 1 and auths[0]["account_id"] == "claude-feng"
                  and auths[0]["reason"] == "login expired"
                  and auths[0]["scope_label"] ==
                  "Claude account Ada (operator@example.com)")
            exposed = json.dumps({"limits": limits, "auths": auths})
            check("681 account API: no config path, key path, or token leaks",
                  all(secret not in exposed for secret in
                      ("/private/claude-home", "/private/feng-home", "/private/key",
                       "NEVER-EXPOSE-ME", "credential_mtime", "refresh_token")))

            # The pre-681 marker belongs to the legacy/default profile.  A
            # sibling's account-local wall has its own reset and must not erase
            # that still-live default-account row from the Status banner.
            state_path.write_text(json.dumps({
                "accounts": {
                    "claude-feng": {
                        "limit": {"active": True, "kind": "account",
                                  "reset_epoch": 300, "message": "limited"},
                    },
                },
            }))
            sibling_rows = {x.get("account_id"): x for x in state.limit_states(now=100)}
            check("681 account limits: sibling state keeps legacy default visible",
                  set(sibling_rows) == {"claude-default", "claude-feng"}
                  and sibling_rows["claude-default"].get("source_worker") == "legacy-worker")

            config.ACCOUNT_REGISTRY = root / "registry-not-yet-created.json"
            fallback_limits = {x.get("account_id"): x for x in state.limit_states(now=100)}
            check("681 account limits: absent registry synthesizes the default label",
                  fallback_limits["claude-default"]["scope_label"] ==
                  "Claude account Default Claude account")

            # A legacy service-wide marker is still meaningful during rollout,
            # but it belonged to the synthesized default profile.  It must not
            # regress the banner to the ambiguous phrase "Claude account".
            state_path.write_text(json.dumps({"accounts": {}}))
            legacy_rows = state.limit_states(now=100)
            check("681 legacy limit: default profile is still named explicitly",
                  len(legacy_rows) == 1
                  and legacy_rows[0]["account_id"] == "claude-default"
                  and legacy_rows[0]["scope_label"] ==
                  "Claude account Default Claude account")
        finally:
            config.ACCOUNT_REGISTRY, config.ACCOUNT_STATE = old_registry, old_state
            config.LIMIT_STATE_SERVICES, config.MODEL_LIMIT_STATE = old_services, old_models


def test_account_status_api_and_banner_are_wired():
    """The browser gets the account labels through ``auths`` and prints them."""
    restore = _patch({
        "daemon_status": lambda: [],
        "workers": lambda **_kw: [],
        "gpu_manager_alive": lambda: False,
        "gpu_jobs_active": lambda: [],
        "jobmgr_jobs": lambda **_kw: [],
        "limit_state": lambda: None,
        "limit_states": lambda: [{"scope_label": "Claude account Ada"}],
        "auth_states": lambda: [{"scope_label": "Claude account Ada",
                                  "reason": "login expired"}],
    })
    try:
        payload = server._api_status()
        check("681 status API: account auth list is returned", payload["auths"] == [
            {"scope_label": "Claude account Ada", "reason": "login expired"}])
    finally:
        restore()
    check("681 Status banner: account label fallback is rendered",
          "x.account_label" in pages._STATUS_JS and "s.auths" in pages._STATUS_JS)
    check("681 Status banner: auth banner is explicit", "AUTH REQUIRED" in pages._STATUS_JS)



# --------------------------------------------------------------------------- #
# Case 760: JTF groups on the Status board
# --------------------------------------------------------------------------- #
def _jtf_fixture(tmp, members, onboard=None):
    """A jtf/ tree shaped exactly like the bridge leaves it, under a temp root."""
    import config as _cfg
    root = Path(tmp)
    (root / "done").mkdir(parents=True, exist_ok=True)
    (root / "done" / "g.json").write_text(json.dumps({
        "id": "g", "ts": 1, "processed_ts": 2, "jtf_id": 900, "critic": "vyas",
        "description": "Do the thing.", "requester": "op@example.com",
        "lead": {"kind": "precinct", "name": "infra", "deputy": "j900_lead",
                 "case": 901, "role": "lead", "tag": "LEAD", "ok": True},
        "collaborators": [
            {"kind": "precinct", "name": "eval", "deputy": f"j900_c{i}", "case": 901 + i,
             "role": "collab", "tag": f"C{i}", "ok": True} for i in (1, 2)]}))
    if onboard:
        (root / "onboard").mkdir(parents=True, exist_ok=True)
        (root / "onboard" / "o.json").write_text(json.dumps(onboard))
    old = _cfg.JTF
    _cfg.JTF = root
    return lambda: setattr(_cfg, "JTF", old)


def test_jtf_board_boxes_only_groups_with_a_deputy_on_the_board():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="c760_state_")
    undo = _jtf_fixture(tmp, None)
    # only the lead and C1 are on the board; C2 has been closed or killed
    restore = _patch({"workers": lambda active_only=False, include_failed=False: [
        {"name": "j900_lead"}, {"name": "j900_c1"}, {"name": "loner"}]})
    try:
        b = state.jtf_board()
        check("Case 760: one box per JTF with a live member", len(b) == 1)
        check("Case 760: the box lists only members that are ON the board",
              [m["deputy"] for m in b[0]["members"]] == ["j900_lead", "j900_c1"])
        check("Case 760: a member no longer on the board is still NAMED",
              [m["deputy"] for m in b[0]["absent"]] == ["j900_c2"])
        check("Case 760: the box carries its slot tags",
              [m["tag"] for m in b[0]["members"]] == ["LEAD", "C1"])
        check("Case 760: the box carries the judge and the description",
              b[0]["critic"] == "vyas" and b[0]["description"] == "Do the thing.")
    finally:
        restore(); undo()

    undo = _jtf_fixture(tmp, None)
    restore = _patch({"workers": lambda active_only=False, include_failed=False: [
        {"name": "loner"}]})
    try:
        check("Case 760: a JTF whose deputies have all gone is not boxed at all",
              state.jtf_board() == [])
    finally:
        restore(); undo()


def test_jtf_board_reports_an_onboarding_chain_in_flight():
    import tempfile, time as _t
    tmp = tempfile.mkdtemp(prefix="c760_state_ob_")
    chain = {"id": "o1", "jtf_id": 900, "state": "collecting", "new_tag": "C3",
             "new_deputy": "j900_c3", "slot": {"name": "paper"},
             "chain": ["j900_lead", "j900_c1", "j900_c2"],
             "tags": ["LEAD", "C1", "C2"], "stage": 1, "nudges": 2,
             "delivered_ts": _t.time() - 600}
    undo = _jtf_fixture(tmp, None, onboard=chain)
    restore = _patch({"workers": lambda active_only=False, include_failed=False: [
        {"name": "j900_lead"}, {"name": "j900_c1"}, {"name": "j900_c2"}]})
    try:
        o = state.jtf_board()[0]["onboarding"]
        check("Case 760: the board says which member holds the note",
              o["holder"] == "j900_c1" and o["holder_tag"] == "C1")
        check("Case 760: it says which slot is joining, and from where",
              o["new_tag"] == "C3" and o["precinct"] == "paper")
        check("Case 760: it counts the stage against the whole chain",
              o["stage"] == 1 and o["of"] == 3)
        check("Case 760: it reports the wait and the nudges",
              o["waiting_sec"] >= 600 and o["nudges"] == 2)
    finally:
        restore(); undo()

    for dead in ("done", "cancelled", "failed"):
        undo = _jtf_fixture(tmp, None, onboard={**chain, "state": dead})
        restore = _patch({"workers": lambda active_only=False, include_failed=False: [
            {"name": "j900_lead"}]})
        try:
            check(f"Case 760: a '{dead}' chain is not reported as in flight",
                  state.jtf_board()[0]["onboarding"] is None)
        finally:
            restore(); undo()


def test_waiting_jtf_is_a_parked_state_with_the_right_buttons():
    check("Case 760: waiting_jtf reads as parked on JTF standby",
          state._STATE_LABEL["waiting_jtf"] == "parked (JTF standby)")
    check("Case 760: a standby deputy can still be switched",
          "waiting_jtf" in state._SWITCHABLE_STATES)
    check("Case 760: a standby deputy can be relaunched",
          "waiting_jtf" in state._RELAUNCHABLE_STATES)
    # the mirror must agree with the module that actually validates the order
    # Cross-check the mirror against the module that really validates the order.
    # Case 782: this used to point at one developer's absolute path, so on every
    # other machine the run failed and BOTH checks below were skipped in silence.
    # It now reads this repo's own infra/, and says so when the module is absent.
    import subprocess as _sp
    infra = Path(__file__).resolve().parents[1] / "infra"
    if not (infra / "scratch_watchdog_control.py").is_file():
        print("[SKIP] control-module mirror: scratch_watchdog_control.py is not "
              "installed in this release")
        return
    out = _sp.run([sys.executable, "-c",
                   f"import sys; sys.path.insert(0, {str(infra)!r});"
                   "import scratch_watchdog_control as c;"
                   "print(','.join(c.SWITCHABLE_STATES) + '|' + ','.join(c.RELAUNCHABLE_STATES))"],
                  capture_output=True, text=True)
    check("Case 760: the control module could be read", out.returncode == 0)
    if out.returncode == 0:
        sw, rl = out.stdout.strip().split("|")
        check("Case 760: the dashboard mirror matches the control module (switch)",
              set(sw.split(",")) == set(state._SWITCHABLE_STATES))
        check("Case 760: the dashboard mirror matches the control module (relaunch)",
              set(rl.split(",")) == set(state._RELAUNCHABLE_STATES))


def test_backend_available_reports_what_the_install_actually_has():
    """Case 782: a control is only offered when the program that carries it out is
    installed. Otherwise the button writes an order nobody reads."""
    tmp = tempfile.mkdtemp()
    root = __import__("pathlib").Path(tmp)
    orig_root = config.STATE_ROOT
    config.STATE_ROOT = root
    try:
        check("backend_available: absent file is False",
              state.backend_available("scratch_watchdog_control.py") is False)
        (root / "scratch_watchdog_control.py").write_text("# stub\n")
        check("backend_available: present file is True",
              state.backend_available("scratch_watchdog_control.py") is True)
        (root / "adir").mkdir()
        check("backend_available: a directory is not a program",
              state.backend_available("adir") is False)
    finally:
        config.STATE_ROOT = orig_root


def test_reply_button_needs_the_message_program():
    """A relaunch script alone is not enough: scratch_deputy_message.py is what
    actually delivers, so without it the reply button must not appear."""
    tmp = tempfile.mkdtemp()
    root = __import__("pathlib").Path(tmp)
    orig_root = config.STATE_ROOT
    config.STATE_ROOT = root
    try:
        (root / "scratch_worker_dep_relaunch.sh").write_text("#!/bin/sh\n")
        check("can_message: no message program -> no reply button",
              state.can_message("dep") is False)
        (root / "scratch_deputy_message.py").write_text("# stub\n")
        check("can_message: program installed -> reply offered",
              state.can_message("dep") is True)
        check("can_message: still needs the deputy's relaunch script",
              state.can_message("nosuchdeputy") is False)
    finally:
        config.STATE_ROOT = orig_root


def test_deputy_orders_are_hidden_without_the_control_program():
    """switch / relaunch / kill are all carried out by the watchdog through
    scratch_watchdog_control.py; with it absent, no row offers them."""
    board = [{"name": "dep", "state": "running", "task": 1, "precinct": "infra",
              "model": "opus", "service": "claude", "relaunched": 0}]
    tmp = tempfile.mkdtemp()
    root = __import__("pathlib").Path(tmp)
    orig_root = config.STATE_ROOT
    config.STATE_ROOT = root
    restore = _patch({"_read_json": lambda path, default=None:
                      board if str(path).endswith("watchdog_jobs.json") else (default or []),
                      "registry": lambda: {},
                      "_mailbox_pending": lambda n: False})
    try:
        rows = state.workers()
        check("orders: one row rendered", len(rows) == 1)
        r = rows[0]
        check("orders: switch hidden without the control program", r["can_switch"] is False)
        check("orders: relaunch hidden without the control program", r["can_relaunch"] is False)
        check("orders: kill hidden without the control program", r["can_terminate"] is False)
        (root / "scratch_watchdog_control.py").write_text("# stub\n")
        r2 = state.workers()[0]
        check("orders: kill offered once the control program exists",
              r2["can_terminate"] is True)
        check("orders: switch offered for a running deputy", r2["can_switch"] is True)
    finally:
        restore()
        config.STATE_ROOT = orig_root


def test_jtf_box_hides_add_collaborator_without_the_chain():
    group = {"jtf_id": "781", "description": "d", "critic": "vyas",
             "members": [{"deputy": "lead", "tag": "LEAD", "role": "lead",
                          "case": 1, "name": "infra"}]}
    tmp = tempfile.mkdtemp()
    root = __import__("pathlib").Path(tmp)
    orig_root = config.STATE_ROOT
    config.STATE_ROOT = root
    restore = _patch({"jtf_groups": lambda: [dict(group)],
                      "jtf_onboarding": lambda: {},
                      "workers": lambda active_only=False, include_failed=False:
                          [{"name": "lead"}]})
    try:
        box = state.jtf_board()[0]
        check("jtf box: add-collaborator hidden without the chain program",
              box["can_add_collaborator"] is False)
        (root / "scratch_jtf_onboard.py").write_text("# stub\n")
        check("jtf box: offered once the chain program exists",
              state.jtf_board()[0]["can_add_collaborator"] is True)
    finally:
        restore()
        config.STATE_ROOT = orig_root


def main():
    for fn in (test_can_message_needs_a_relaunch_script,
               test_precinct_cases_offer_followup_only_with_a_revivable_deputy,
               test_legacy_duplicate_case_rows_are_one_current_case_everywhere,
               test_critic_review_detail_all_rounds,
               test_critic_review_detail_failclosed_and_shapes,
               test_critic_review_prompt_and_path_guard,
               test_account_limit_and_auth_states_are_labelled_and_curated,
               test_account_status_api_and_banner_are_wired,
               test_composes_both_loops,
               test_emailed_attachments_downloadable_flag,
               test_watchdog_down_and_limit_active,
               test_active_only_true_is_passed_to_workers,
               test_latest_attributed_case_newest_numeric,
               test_worker_precinct_case_prefers_attributed_over_frozen_prompt,
               test_workers_failed_filter_is_opt_in,
               test_workers_paper_fallback_shows_attributed_not_frozen,
               test_workers_board_still_overrides_fallback,
               test_jtf_board_boxes_only_groups_with_a_deputy_on_the_board,
               test_jtf_board_reports_an_onboarding_chain_in_flight,
               test_waiting_jtf_is_a_parked_state_with_the_right_buttons,
               test_backend_available_reports_what_the_install_actually_has,
               test_reply_button_needs_the_message_program,
               test_deputy_orders_are_hidden_without_the_control_program,
               test_jtf_box_hides_add_collaborator_without_the_chain):
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
