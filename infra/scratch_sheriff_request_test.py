#!/usr/bin/env python3
"""Case 384a: tests for the deputy->sheriff REQUEST QUEUE (scratch_sheriff_request.py).

Locks the mechanical, ZERO-API half of the backbone:
  * queue round-trip (submit -> pending -> get/status -> move_to done/denied),
  * anti-impersonation (verify_identity: matching (deputy, session) ok; forged
    deputy, wrong session, or empty session rejected),
  * submit input validation, and
  * the retraction/edit JOURNAL (header + one audit line per call).

Self-contained (no pytest). Everything lives under a throwaway TSOMP_RECORDS_ROOT
with a throwaway TSOMP_REGISTRY_PATH, so no real records/registry is touched.
Run:  python scratch_sheriff_request_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

_TEST_ROOT = tempfile.mkdtemp(prefix="tsomp_sreq_test_")
os.environ["TSOMP_RECORDS_ROOT"] = _TEST_ROOT
os.environ["INFRA_MAIL_ALLOWED"] = "operator@example.com,teammate@example.com"
# a throwaway worker registry so verify_identity is deterministic + offline
_REG = Path(_TEST_ROOT) / "registry.json"
_REG.write_text(json.dumps({"workers": {
    "depA": {"session": "sessA", "match": ["a"]},
    "depB": {"session": "sessB", "match": ["b"]},
}}))
os.environ["TSOMP_REGISTRY_PATH"] = str(_REG)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_sheriff_request as sreq  # noqa: E402


def test_submit_round_trip_and_pending():
    r = sreq.submit("log_remove", "infra", "depA", "sessA", "retract a test case", target="900001")
    assert r["op"] == "log_remove" and r["deputy"] == "depA" and r["target"] == "900001"
    assert "id" in r and r["ts"] > 0
    # our request is queued (don't assume it is the only pending one -- the suite
    # shares one throwaway queue root, so match by id rather than an absolute count)
    names = {p.name for p in sreq.list_pending()}
    assert f"{r['id']}.json" in names
    got = sreq.get(r["id"])
    assert got and got["id"] == r["id"]
    st = sreq.status(r["id"])
    assert st["state"] == "pending" and st["record"]["op"] == "log_remove"


def test_submit_carries_op_extras():
    r = sreq.submit("ledger_modify", "omp", "depA", "sessA", "prune stale para",
                    drop_contains="OBSOLETE-XYZ")
    got = sreq.get(r["id"])
    assert got["drop_contains"] == "OBSOLETE-XYZ"
    r2 = sreq.submit("precinct_create", "", "depA", "sessA", "need a scratch precinct",
                     target="scratchp", description="a test precinct", mode="mutable", model="opus")
    got2 = sreq.get(r2["id"])
    assert got2["target"] == "scratchp" and got2["mode"] == "mutable" and got2["model"] == "opus"


def test_submit_validates_inputs():
    for bad in [
        lambda: sreq.submit("NOPE", "infra", "depA", "sessA", "x"),          # bad op
        lambda: sreq.submit("log_remove", "infra", "bad name!", "sessA", "x"),  # bad deputy
        lambda: sreq.submit("log_remove", "infra", "depA", "", "x"),          # empty session
        lambda: sreq.submit("log_remove", "infra", "depA", "sessA", ""),      # empty reason
    ]:
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for invalid submit input")


def test_verify_identity_matches_registry():
    assert sreq.verify_identity("depA", "sessA") is True
    assert sreq.verify_identity("depB", "sessB") is True


def test_verify_identity_rejects_forgery():
    assert sreq.verify_identity("depA", "sessB") is False    # right deputy, wrong session
    assert sreq.verify_identity("ghost", "sessA") is False   # unknown deputy
    assert sreq.verify_identity("depA", "") is False         # empty session
    assert sreq.verify_identity("", "sessA") is False        # empty deputy
    assert sreq.verify_identity("depB", "sessA") is False    # mismatched pair


def test_move_to_done_and_denied():
    r = sreq.submit("case_number", "infra", "depA", "sessA", "take a follow-up")
    path = sreq.list_pending()[-1]
    rec = sreq.get(r["id"])
    rec["result"] = {"case": 900123}
    sreq.move_to(path, "done", rec)
    assert not path.exists(), "source pending file must be gone after move"
    st = sreq.status(r["id"])
    assert st["state"] == "done" and st["record"]["result"]["case"] == 900123

    r2 = sreq.submit("log_remove", "infra", "depB", "sessB", "retract", target="5")
    p2 = [p for p in sreq.list_pending() if p.stem == r2["id"]][0]
    rec2 = sreq.get(r2["id"]); rec2["denied_reason"] = "not your case"
    sreq.move_to(p2, "denied", rec2)
    assert sreq.status(r2["id"])["state"] == "denied"


def test_journal_writes_audit_line():
    sreq.journal_append("infra", "depA", "log_remove", "900001",
                        "retracting a genuine test entry", "approve",
                        extra=json.dumps({"removed_lines": 1}))
    txt = sreq.journal_read("infra")
    assert "sheriff retraction/edit journal" in txt, "journal header must exist"
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    assert len(lines) == 1
    fields = lines[0].split("\t")
    # <iso_utc>\t<decision>\t<op>\t<deputy>\t<target>\t<reason>\t<extra>
    assert fields[1] == "approve" and fields[2] == "log_remove" and fields[3] == "depA"
    assert fields[4] == "900001" and "test entry" in fields[5]
    # a second append accumulates (append-only)
    sreq.journal_append("infra", "depB", "ledger_modify", "infra", "flagged correction", "deny")
    lines2 = [l for l in sreq.journal_read("infra").splitlines() if l and not l.startswith("#")]
    assert len(lines2) == 2 and lines2[-1].split("\t")[1] == "deny"


# ---------------------------------------------------------------------------
# Phase D (Case 384d): receptionist hand-off submits + the confirmation subtree
# ---------------------------------------------------------------------------
def test_receptionist_handoff_submit_authorized_without_session():
    # a precinct-lifecycle op may be posted with origin=receptionist + an allowed
    # requester and NO deputy session (the user's mailbox is the authority).
    r = sreq.submit("precinct_delete", "", "", "", "user asked to delete", target="oldp",
                    origin="receptionist", requester="operator@example.com", force=True)
    got = sreq.get(r["id"])
    assert got["op"] == "precinct_delete" and got["target"] == "oldp"
    assert got["origin"] == "receptionist" and got["requester"] == "operator@example.com"
    assert got["force"] is True and got["deputy"] == "receptionist"
    # create + restore also allowed via the hand-off
    for op in ("precinct_create", "precinct_restore"):
        rr = sreq.submit(op, "", "", "", "user asked", target="p1",
                         origin="receptionist", requester="teammate@example.com")
        assert sreq.get(rr["id"])["origin"] == "receptionist"


def test_receptionist_handoff_rejects_bad_requester_and_wrong_op():
    # non-allowed requester rejected
    try:
        sreq.submit("precinct_delete", "", "", "", "sneak", target="x",
                    origin="receptionist", requester="evil@example.com")
        raise AssertionError("bad requester must be rejected")
    except ValueError:
        pass
    # a non-lifecycle op cannot ride the receptionist hand-off
    try:
        sreq.submit("log_remove", "infra", "", "", "sneak", target="1",
                    origin="receptionist", requester="operator@example.com")
        raise AssertionError("log_remove via receptionist origin must be rejected")
    except ValueError:
        pass


def test_precinct_restore_is_a_valid_op():
    r = sreq.submit("precinct_restore", "", "depA", "sessA", "undo a delete", target="gone")
    assert sreq.get(r["id"])["op"] == "precinct_restore"


def test_confirmation_subtree_roundtrip():
    tok = sreq.new_token()
    assert tok and len(tok) >= 12
    sreq.write_awaiting(tok, {"token": tok, "precinct": "p", "expires_ts": 1})
    assert sreq.read_awaiting(tok)["precinct"] == "p"
    assert any(p.stem == tok for p in sreq.list_awaiting())
    sreq.record_confirmation(tok, "yes", uid="42")
    c = sreq.read_confirmation(tok)
    assert c["answer"] == "yes" and c["uid"] == "42"
    sreq.remove_confirmation(tok); sreq.remove_awaiting(tok)
    assert sreq.read_confirmation(tok) is None and sreq.read_awaiting(tok) is None


def test_parse_confirm_token_and_answer():
    assert sreq.parse_confirm_token("Re: [sheriff-confirm:abc123def456] Confirm", "") == "abc123def456"
    assert sreq.parse_confirm_token("no tag here", "body sheriff-confirm:DEADBEEF12 yo") == "deadbeef12"
    assert sreq.parse_confirm_token("nothing", "nothing") == ""
    assert sreq.parse_confirm_answer("YES, delete it") == "yes"
    assert sreq.parse_confirm_answer("please cancel") == "no"
    assert sreq.parse_confirm_answer("what is this?") == "unclear"
    # NO wins over YES for safety (fail-closed)
    assert sreq.parse_confirm_answer("no, don't -- I said yes earlier but changed my mind") == "no"
    # a quoted original that mentions YES must not be read as the user's answer
    body = "cancel please\n\n> On ... the sheriff wrote:\n> reply YES to confirm deletion"
    assert sreq.parse_confirm_answer(body) == "no"


def test_confirm_check_intercepts_only_live_token_with_clear_answer():
    # no token -> passthrough
    assert sreq.confirm_check("Re: hi", "yes")["intercepted"] is False
    # token but NO live awaiting -> passthrough (stray/expired token never wedges)
    assert sreq.confirm_check("Re: [sheriff-confirm:beefbeefbeef]", "yes")["intercepted"] is False
    # live token + clear YES -> intercepted + a confirmation is recorded
    tok = sreq.new_token()
    sreq.write_awaiting(tok, {"token": tok, "precinct": "delme", "expires_ts": 1})
    res = sreq.confirm_check(f"Re: [sheriff-confirm:{tok}] Confirm", "YES please", uid="7")
    assert res["intercepted"] is True and res["answer"] == "yes" and res["precinct"] == "delme"
    assert sreq.read_confirmation(tok)["answer"] == "yes"
    # live token + UNCLEAR answer -> passthrough (fail-closed; no confirmation written)
    tok2 = sreq.new_token()
    sreq.write_awaiting(tok2, {"token": tok2, "precinct": "delme2", "expires_ts": 1})
    assert sreq.confirm_check(f"Re: [sheriff-confirm:{tok2}]", "huh, why?")["intercepted"] is False
    assert sreq.read_confirmation(tok2) is None
    for t in (tok, tok2):
        sreq.remove_awaiting(t); sreq.remove_confirmation(t)


def test_lifecycle_journal_appends():
    sreq.lifecycle_journal_append("delp", "receptionist", "precinct_delete",
                                  "delete-confirmed", "user confirmed", extra="trash=.trash/delp@x")
    txt = sreq.lifecycle_journal_read()
    assert "precinct-lifecycle journal" in txt
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    assert lines and lines[-1].split("\t")[1] == "delete-confirmed"
    assert lines[-1].split("\t")[3] == "delp"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_sheriff_request test suite  ({len(tests)} tests) ===")
    print(f"    records root (throwaway): {_TEST_ROOT}")
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
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)
    sys.exit(rc)
