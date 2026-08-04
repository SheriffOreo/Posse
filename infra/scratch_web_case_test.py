#!/usr/bin/env python3
"""Task 377 #4: tests for the web-case bridge (scratch_web_case.py).

Covers spec generation (parent/precinct/deputy stamped, description + uploads
carried), model validation (a bad model is ignored, not fatal), robustness (an
unreadable/invalid record is moved to failed/ and never wedges the queue), and the
real claim->done move (spawn + email stubbed). Self-contained; everything lives
under throwaway TSOMP_WEBCASES_ROOT + TSOMP_INBOX_ROOT.  Run: python scratch_web_case_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

_WROOT = tempfile.mkdtemp(prefix="tsomp_webcase_test_")
_IROOT = tempfile.mkdtemp(prefix="tsomp_webcase_inbox_")
os.environ["TSOMP_WEBCASES_ROOT"] = _WROOT
os.environ["TSOMP_INBOX_ROOT"] = _IROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_web_case as wc  # noqa: E402

# Stub the two side-effecting steps so tests never spawn tmux or send email.
_SPAWNS, _ACKS = [], []
wc._spawn = lambda record, **k: _SPAWNS.append((record.get("id"), k.get("case"), k.get("deputy")))
wc._ack = lambda record, **k: _ACKS.append((record.get("id"), k.get("case"), k.get("deputy")))


def _write_pending(sid, **record):
    record.setdefault("id", sid)
    record.setdefault("source", "web")
    p = Path(_WROOT) / "pending" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record))
    return p


def test_good_case_writes_spec_and_moves_to_done():
    _SPAWNS.clear(); _ACKS.clear()
    p = _write_pending("s1", precinct="infra", model="sonnet", parent="376",
                       description="Fix the GPU N/A fallback.\nAlso add a test.",
                       files=[], case=900100)
    res = wc.process_one(p, dry=False)
    assert res["ok"] and res["case"] == 900100 and res["deputy"] == "web_900100", res
    # spec written to the (overridden) inbox with the stamped header lines
    spec = Path(_IROOT) / "task_900100.md"
    assert spec.exists(), "spec not written"
    txt = spec.read_text()
    assert "parent_task: 376" in txt and "precinct: infra" in txt and "deputy: web_900100" in txt, txt[:200]
    assert "Fix the GPU N/A fallback." in txt
    # record moved pending -> done, and spawn + ack were both invoked
    assert not p.exists(), "pending record not consumed"
    assert (Path(_WROOT) / "done" / "s1.json").exists(), "record not moved to done"
    assert _SPAWNS and _ACKS, (_SPAWNS, _ACKS)


def test_new_task_has_parent_none():
    p = _write_pending("s2", precinct="infra", description="A standalone task", case=900101)
    wc.process_one(p, dry=False)
    txt = (Path(_IROOT) / "task_900101.md").read_text()
    assert "parent_task: none" in txt, txt[:120]


def test_bad_model_is_ignored_not_fatal():
    p = _write_pending("s3", precinct="infra", model="gpt5", description="d", case=900102)
    res = wc.process_one(p, dry=False)
    assert res["ok"], res                      # bad model must NOT fail the case
    assert (Path(_WROOT) / "done" / "s3.json").exists()


def test_missing_precinct_goes_to_failed():
    p = _write_pending("s4", precinct="", description="no precinct", case=900103)
    res = wc.process_one(p, dry=False)
    assert not res["ok"], res
    assert (Path(_WROOT) / "failed" / "s4.json").exists(), "bad record not quarantined"
    assert not p.exists(), "bad record left in pending (would wedge the queue)"


def test_unreadable_record_goes_to_failed():
    p = Path(_WROOT) / "pending" / "s5.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json")
    res = wc.process_one(p, dry=False)
    assert not res["ok"] and (Path(_WROOT) / "failed" / "s5.json").exists(), res


def test_allocates_case_when_server_did_not():
    # no 'case' in the record -> the bridge allocates one from the shared counter.
    # (Case 391a: production is ONE continuous sequence — no 900000 band; this test
    # just PINS TSOMP_CASE_BASE for a deterministic no-file seed, like the other
    # allocator suites.)
    os.environ["TSOMP_RECORDS_ROOT"] = _WROOT      # allocator counter under the throwaway root
    os.environ["TSOMP_CASE_BASE"] = "900000"
    p = _write_pending("s6", precinct="infra", description="allocate me")
    res = wc.process_one(p, dry=False)
    assert res["ok"] and int(res["case"]) >= 900000, res


def test_process_all_flock_and_batch():
    for i in range(3):
        _write_pending(f"b{i}", precinct="infra", description=f"batch {i}", case=900200 + i)
    res = wc.process_all(dry=False)
    ok = [r for r in res if r.get("ok")]
    assert len(ok) >= 3, res
    for i in range(3):
        assert (Path(_WROOT) / "done" / f"b{i}.json").exists()


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_web_case test suite  ({len(tests)} tests) ===")
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
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = _run()
    finally:
        shutil.rmtree(_WROOT, ignore_errors=True)
        shutil.rmtree(_IROOT, ignore_errors=True)
    sys.exit(rc)
