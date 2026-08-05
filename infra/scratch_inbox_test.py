#!/usr/bin/env python3
"""Task 382 #2: regression test for subject de-folding (scratch_inbox.clean_subject).
Self-contained.  Run:  python scratch_inbox_test.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_inbox as ib  # noqa: E402


def test_folded_subject_becomes_one_line_and_is_complete():
    folded = ("Re: Task 377 START: deputy attribution, new case #s, Status\n"
              " GPU/columns, per-precinct Create-case form")
    out = ib.clean_subject(folded)
    assert "\n" not in out, "subject still has a newline"
    assert out == ("Re: Task 377 START: deputy attribution, new case #s, Status "
                   "GPU/columns, per-precinct Create-case form"), repr(out)
    assert out.endswith("Create-case form"), "subject was truncated"


def test_crlf_and_tabs_and_runs_collapse():
    assert ib.clean_subject("a\r\n b\t\tc   d") == "a b c d"


def test_plain_subject_unchanged():
    assert ib.clean_subject("Re: hello world") == "Re: hello world"


def test_none_and_empty_safe():
    assert ib.clean_subject(None) == ""
    assert ib.clean_subject("") == ""
    assert ib.clean_subject("   ") == ""


# --- Task 396: routing is EXACTLY two matches (reply-thread, precinct tag); no keyword matching ---
def test_route_precinct_tag_in_subject():
    with _temp_records({}, precincts=["query"]):
        assert ib.route(None, None, "[query] winner naming mismatch", "body") == "precinct:query"


def test_route_precinct_tag_in_body():
    with _temp_records({}, precincts=["omp"]):
        assert ib.route(None, None, "winner naming", "precinct: omp\n\ndetails") == "precinct:omp"


def test_route_no_legacy_keyword_matching():
    # The greedy keyword matcher is REMOVED: text that used to hit a dead pre-precinct
    # agent by keyword now falls to the receptionist front desk, not that agent.
    assert ib.route(None, None, "metrics disconnected",
                    "poor fidelity jsd vs queries mismatch disconnect") == "precinct:receptionist"


def test_route_unaddressed_is_receptionist():
    assert ib.route(None, None, "please help me", "no tag anywhere here") == "precinct:receptionist"


def test_route_pipe_header_is_not_a_precinct_tag():
    # the "[precinct|case|deputy|model]" email header contains "|" -> NOT a tag.
    assert ib.route(None, None, "Re: [infra|391|case_ui|opus] thanks", "great") == "precinct:receptionist"


def test_drop_email_case_writes_record_and_is_idempotent():
    import os, json, tempfile
    d = tempfile.mkdtemp(prefix="t396_")
    keys = ("TSOMP_RECORDS_ROOT", "TSOMP_WEBCASES_ROOT", "TSOMP_INBOX_ROOT")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"TSOMP_RECORDS_ROOT": d + "/rec",
                       "TSOMP_WEBCASES_ROOT": d + "/web",
                       "TSOMP_INBOX_ROOT": d + "/inbox"})
    try:
        case, path = ib.drop_email_case("393", "query", "teammate@example.com",
                                        "[query] fix winner naming", "precinct: query\n\nfix it", [])
        rec = json.loads(path.read_text())
        assert rec["source"] == "email", rec
        assert rec["precinct"] == "query", rec
        assert rec["deputy_hint"] == f"fix_winner_naming_{case}", rec["deputy_hint"]
        assert rec["requester"] == "teammate@example.com", rec
        assert path.name == "email_393.json", path.name
        # idempotent per uid: same case number, same record file (overwrite, never double-spawn)
        case2, path2 = ib.drop_email_case("393", "query", "teammate@example.com",
                                          "[query] fix winner naming", "precinct: query\n\nfix it", [])
        assert case2 == case and str(path2) == str(path)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# --- Case 402: a header-less REPLY (In-Reply-To/References dropped by the client) is
#     recovered from its "Re: Case N" subject instead of being dumped on the receptionist ---
import os as _os
import json as _json
import tempfile as _tempfile
from contextlib import contextmanager as _contextmanager


@_contextmanager
def _temp_records(mapping, extra_workers=(), precincts=()):
    """Point _task_precinct_map + known_precincts at a temp records root (via
    TSOMP_RECORDS_ROOT) and inject any extra_workers into the in-memory registry;
    restore both on exit. The temp dir gets BOTH a task_precinct.json (from ``mapping``)
    and a precincts.json seeded with the precincts this test needs: those referenced by
    the task map, plus any explicit ``precincts`` (tag targets), plus the receptionist."""
    d = _tempfile.mkdtemp(prefix="c402_")
    old = _os.environ.get("TSOMP_RECORDS_ROOT")
    _os.environ["TSOMP_RECORDS_ROOT"] = d
    (Path(d) / "task_precinct.json").write_text(_json.dumps(mapping))
    names = {m.get("precinct") for m in mapping.values()
             if isinstance(m, dict) and m.get("precinct")}
    names |= set(precincts) | {"receptionist"}
    (Path(d) / "precincts.json").write_text(
        _json.dumps({"precincts": {n: {} for n in names}}))
    added = []
    for w in extra_workers:
        if w not in ib.REG["workers"]:
            ib.REG["workers"][w] = {"type": "resume", "session": "sess-" + w}
            added.append(w)
    try:
        yield
    finally:
        if old is None:
            _os.environ.pop("TSOMP_RECORDS_ROOT", None)
        else:
            _os.environ["TSOMP_RECORDS_ROOT"] = old
        for w in added:
            ib.REG["workers"].pop(w, None)


def test_route_headerless_reply_recovers_owner_deputy():
    # THE Case-402 bug: uid-416 arrived with no In-Reply-To/References. The "Re: Case 400"
    # subject must route it back to case 400's registered deputy, exactly like a header match.
    with _temp_records({"400": {"deputy": "d400", "precinct": "infra"}}, extra_workers=["d400"]):
        assert ib.route(None, None,
                        "Re: Case 400: S2.3 expanded with a concrete example per bullet",
                        "some follow-up text") == "d400"


def test_route_headerless_reply_recovers_precinct_when_deputy_gone():
    # Owner deputy no longer registered -> route to the RIGHT precinct (fresh deputy there),
    # never the content-guessing receptionist (which is how case 401 became 'paper').
    with _temp_records({"400": {"precinct": "infra"}}):
        assert ib.route(None, None, "Re: Case 400: anything", "body") == "precinct:infra"


def test_route_headerless_reply_unknown_case_is_receptionist():
    with _temp_records({"400": {"deputy": "d400", "precinct": "infra"}}, extra_workers=["d400"]):
        assert ib.route(None, None, "Re: Case 999: mystery", "body") == "precinct:receptionist"


def test_route_non_reply_case_mention_is_not_recovered():
    # A fresh contact merely MENTIONING "Case 400" (no Re:/Fwd:) must NOT hijack case 400.
    with _temp_records({"400": {"deputy": "d400", "precinct": "infra"}}, extra_workers=["d400"]):
        assert ib.route(None, None, "Case 400 looks wrong, please help",
                        "no tag") == "precinct:receptionist"


def test_route_header_match_wins_over_subject():
    # A valid In-Reply-To always beats subject recovery (header is authoritative).
    orig = ib.sent_map
    ib.sent_map = lambda: {"mid-hdr@x": "hdr_worker"}
    ib.REG["workers"]["hdr_worker"] = {"type": "resume", "session": "s"}
    try:
        with _temp_records({"401": {"deputy": "d401", "precinct": "paper"}}, extra_workers=["d401"]):
            assert ib.route("<mid-hdr@x>", None, "Re: Case 401: x", "body") == "hdr_worker"
    finally:
        ib.sent_map = orig
        ib.REG["workers"].pop("hdr_worker", None)


def test_route_explicit_tag_wins_over_subject_recovery():
    # A deliberate [precinct] tag on the reply overrides the subject's owning case.
    with _temp_records({"400": {"deputy": "d400", "precinct": "infra"}},
                       extra_workers=["d400"], precincts=["query"]):
        assert ib.route(None, None, "Re: Case 400: move this [query]",
                        "body") == "precinct:query"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_inbox test suite  ({len(tests)} tests) ===")
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
