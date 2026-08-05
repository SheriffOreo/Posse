#!/usr/bin/env python3
"""Case 384e / Phase E: tests for the JTF backend bridge (scratch_jtf.py).

Covers the precinct-vs-deputy SLOT RESOLUTION (norm_slot + resolve_slots dedup), the
per-participant spec / must-take assignment construction (roster carried, critic only
on the lead, must-take header present), robustness (a malformed record is quarantined
to failed/ and never wedges the queue; one bad slot never aborts the group), and the
real claim->done move (spawn / interrupt / email all stubbed). Self-contained under a
throwaway TSOMP_JTF_ROOT + TSOMP_INBOX_ROOT.  Run: python scratch_jtf_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

_JROOT = tempfile.mkdtemp(prefix="tsomp_jtf_test_")
_IROOT = tempfile.mkdtemp(prefix="tsomp_jtf_inbox_")
_RROOT = tempfile.mkdtemp(prefix="tsomp_jtf_records_")
os.environ["TSOMP_JTF_ROOT"] = _JROOT
os.environ["TSOMP_INBOX_ROOT"] = _IROOT
os.environ["TSOMP_RECORDS_ROOT"] = _RROOT
os.environ["TSOMP_CASE_BASE"] = "500000"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_jtf as jtf  # noqa: E402

# The bridge validates precinct slots against the live directory — stub it to a
# fixed set so tests never depend on the real precincts.json.
jtf.known_precincts = lambda: {"infra", "omp", "eval", "paper", "query"}

# Capture the two side-effecting materializers + the ack instead of spawning tmux
# / interrupting workers / sending email. Each records the kwargs it was called with.
_SPAWNS, _ASSIGNS, _ACKS = [], [], []
_real_spawn = jtf._spawn_precinct
_real_assign = jtf._assign_deputy


def _cap_spawn(**k):
    # still write the spec (so we can assert on it), but never spawn
    inbox = jtf._inbox_dir(); inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"task_{k['case']}.md").write_text(jtf._participant_spec(
        jtf_id=k["jtf_id"], role=k["role"], case=k["case"], precinct=k["precinct"],
        deputy=k["deputy"], description=k["description"], roster=k["roster"],
        critic=k["critic"], requester=k["requester"]))
    _SPAWNS.append(k)


def _cap_assign(**k):
    _ASSIGNS.append(k)
    return True


jtf._spawn_precinct = lambda **k: _cap_spawn(**k)
jtf._assign_deputy = lambda **k: _cap_assign(**k)
jtf._ack = lambda *a, **k: _ACKS.append(k)


def _write_pending(sid, **record):
    record.setdefault("id", sid)
    record.setdefault("source", "web")
    record.setdefault("requester", "teammate@example.com")
    p = Path(_JROOT) / "pending" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record))
    return p


# ----- slot resolution (the precinct-vs-deputy primitive) --------------------
def test_norm_slot_precinct_and_deputy():
    assert jtf.norm_slot({"kind": "precinct", "name": "Infra"}) == {"kind": "precinct", "name": "infra"}
    assert jtf.norm_slot({"kind": "deputy", "name": "case_ui"}) == {"kind": "deputy", "name": "case_ui"}


def test_norm_slot_rejects_bad_kind_and_empty_name():
    for bad in [{"kind": "team", "name": "x"}, {"kind": "deputy", "name": ""},
                {"name": "x"}, "notadict"]:
        try:
            jtf.norm_slot(bad)
        except ValueError:
            continue
        raise AssertionError(f"norm_slot accepted a bad slot: {bad!r}")


def test_resolve_slots_dedupes_deputy_not_precinct():
    # a deputy that appears as lead + collaborator is kept only once (lead);
    # the same precinct twice stays twice (two distinct fresh deputies).
    lead, collabs = jtf.resolve_slots({
        "lead": {"kind": "deputy", "name": "dup"},
        "collaborators": [{"kind": "deputy", "name": "dup"},
                          {"kind": "precinct", "name": "infra"},
                          {"kind": "precinct", "name": "infra"}]})
    assert lead == {"kind": "deputy", "name": "dup"}
    assert collabs == [{"kind": "precinct", "name": "infra"},
                       {"kind": "precinct", "name": "infra"}], collabs


def test_resolve_slots_requires_lead_and_collaborator():
    for rec in [{"collaborators": [{"kind": "precinct", "name": "infra"}]},
                {"lead": {"kind": "deputy", "name": "x"}, "collaborators": []}]:
        try:
            jtf.resolve_slots(rec)
        except ValueError:
            continue
        raise AssertionError(f"resolve_slots accepted an incomplete JTF: {rec!r}")


# ----- materialization -------------------------------------------------------
def test_precinct_lead_and_deputy_collab_full_flow():
    _SPAWNS.clear(); _ASSIGNS.clear(); _ACKS.clear()
    p = _write_pending("j1",
                       lead={"kind": "precinct", "name": "infra"},
                       collaborators=[{"kind": "deputy", "name": "case_ui"},
                                      {"kind": "precinct", "name": "omp"}],
                       critic=True,
                       description="Audit the OMP decode cross-channel claim.")
    res = jtf.process_one(p, dry=False)
    assert res["ok"], res
    jid = res["jtf_id"]
    # lead is a precinct slot -> spawned as jtf<G>_lead; one deputy collab assigned;
    # one precinct collab spawned.
    assert len(_SPAWNS) == 2 and len(_ASSIGNS) == 1, (_SPAWNS, _ASSIGNS)
    lead = res["lead"]
    assert lead["kind"] == "precinct" and lead["deputy"] == f"jtf{jid}_lead", lead
    # the lead's spec carries the critic instruction; a collaborator's does NOT.
    lead_spec = (Path(_IROOT) / f"task_{lead['case']}.md").read_text()
    assert "## Your role: LEAD" in lead_spec and "CRITIC was requested" in lead_spec, lead_spec[:400]
    assert "scratch_spawn_anon.sh" in lead_spec
    # roster names every member
    assert "case_ui (specific deputy)" in lead_spec and "precinct omp" in lead_spec, lead_spec
    # the must-take assignment for the deputy collaborator
    a = _ASSIGNS[0]
    msg = jtf._assignment_message(jtf_id=jid, role=a["role"], case=a["case"],
                                  deputy=a["deputy"], description=a["description"],
                                  roster=a["roster"], critic=a["critic"],
                                  requester=a["requester"])
    assert msg.startswith("JTF ASSIGNMENT (must-take)"), msg[:60]
    assert "MUST-TAKE PROTOCOL" in msg and f"case {a['case']}" in msg
    # record moved pending -> done
    assert not p.exists() and (Path(_JROOT) / "done" / "j1.json").exists()
    assert _ACKS, "ack not sent"


def test_critic_only_on_lead_not_collaborators():
    _SPAWNS.clear(); _ASSIGNS.clear()
    p = _write_pending("j2",
                       lead={"kind": "deputy", "name": "leaddep"},
                       collaborators=[{"kind": "precinct", "name": "infra"}],
                       critic=True, description="d")
    res = jtf.process_one(p, dry=False)
    assert res["ok"], res
    # lead is a specific deputy -> its assignment carries the critic instruction
    lead_assign = [a for a in _ASSIGNS if a["role"] == "lead"][0]
    assert lead_assign["critic"] is True
    # the precinct collaborator's spawn must NOT carry critic
    collab_spawn = [s for s in _SPAWNS if s["role"] == "collab"][0]
    assert collab_spawn["critic"] is False, collab_spawn


def test_unknown_precinct_goes_to_failed():
    _SPAWNS.clear()
    p = _write_pending("j3",
                       lead={"kind": "precinct", "name": "nosuch"},
                       collaborators=[{"kind": "precinct", "name": "infra"}],
                       description="d")
    res = jtf.process_one(p, dry=False)
    assert not res["ok"], res
    assert (Path(_JROOT) / "failed" / "j3.json").exists()
    assert not p.exists(), "bad record left in pending (would wedge the queue)"
    assert not _SPAWNS, "nothing should spawn for a rejected JTF"


def test_missing_lead_goes_to_failed():
    p = _write_pending("j4", collaborators=[{"kind": "precinct", "name": "infra"}],
                       description="d")
    res = jtf.process_one(p, dry=False)
    assert not res["ok"] and (Path(_JROOT) / "failed" / "j4.json").exists(), res


def test_unreadable_record_goes_to_failed():
    p = Path(_JROOT) / "pending" / "j5.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    res = jtf.process_one(p, dry=False)
    assert not res["ok"] and (Path(_JROOT) / "failed" / "j5.json").exists(), res


def test_one_bad_slot_does_not_abort_group():
    # a specific-deputy assign that FAILS (returns False) must not sink the whole JTF;
    # the group still completes and moves to done, with that participant marked not-ok.
    _ASSIGNS.clear()
    jtf._assign_deputy = lambda **k: (_ASSIGNS.append(k) or False)   # simulate unreachable
    try:
        p = _write_pending("j6",
                           lead={"kind": "precinct", "name": "infra"},
                           collaborators=[{"kind": "deputy", "name": "ghost"}],
                           description="d")
        res = jtf.process_one(p, dry=False)
        assert res["ok"], res
        ghost = [c for c in res["collaborators"] if c["name"] == "ghost"][0]
        assert ghost["ok"] is False, ghost
        assert (Path(_JROOT) / "done" / "j6.json").exists()
    finally:
        jtf._assign_deputy = lambda **k: _cap_assign(**k)


def test_process_all_flock_and_batch():
    for i in range(3):
        _write_pending(f"b{i}",
                       lead={"kind": "precinct", "name": "infra"},
                       collaborators=[{"kind": "precinct", "name": "omp"}],
                       description=f"batch {i}")
    res = jtf.process_all(dry=False)
    ok = [r for r in res if r.get("ok")]
    assert len(ok) >= 3, res


def test_must_take_protocol_in_deputy_preamble():
    # The JTF must-take protocol MUST be baked into the deputy prompt preamble so an
    # assigned deputy knows to stop -> hand over -> notify sheriff -> take.
    src = (Path(__file__).resolve().parent / "scratch_spawn_worker.sh").read_text()
    assert "JTF MUST-TAKE PROTOCOL" in src, "must-take protocol missing from the preamble"
    for marker in ["must-take", "JTF ASSIGNMENT (must-take)", "HAND OVER",
                   "NOTIFY THE SHERIFF", "scratch_spawn_anon.sh"]:
        assert marker in src, f"preamble missing must-take marker: {marker!r}"


def test_dry_run_does_not_move_record():
    p = _write_pending("d1",
                       lead={"kind": "precinct", "name": "infra"},
                       collaborators=[{"kind": "precinct", "name": "omp"}],
                       description="d")
    res = jtf.process_one(p, dry=True)
    assert res["ok"], res
    assert p.exists(), "dry run must NOT consume the pending record"
    assert not (Path(_JROOT) / "done" / "d1.json").exists()


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_jtf test suite  ({len(tests)} tests) ===")
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
        shutil.rmtree(_JROOT, ignore_errors=True)
        shutil.rmtree(_IROOT, ignore_errors=True)
        shutil.rmtree(_RROOT, ignore_errors=True)
    sys.exit(rc)
