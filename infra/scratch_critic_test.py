#!/usr/bin/env python3
"""Case 551 -- self-contained tests for the critic subsystem.

Runs entirely against a throwaway records root + reviews root; spawns NO agents
(the anonymous-worker spawn is stubbed) and makes NO Claude calls.

    python scratch_critic_test.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="critic_test_"))
os.environ["TSOMP_RECORDS_ROOT"] = str(_TMP / "records")
os.environ["TSOMP_CRITIC_REVIEWS_ROOT"] = str(_TMP / "reviews")
os.environ["TSOMP_REGISTRY_PATH"] = str(_TMP / "registry.json")
os.environ["TSOMP_WEBCASES_ROOT"] = str(_TMP / "web_cases")
os.environ["TSOMP_INBOX_ROOT"] = str(_TMP / "inbox")
(_TMP / "records").mkdir(parents=True, exist_ok=True)
(_TMP / "registry.json").write_text(json.dumps(
    {"workers": {"depA": {"session": "sessA"}}}), encoding="utf-8")

import scratch_records as rec                                        # noqa: E402
import scratch_critic as cri                                         # noqa: E402
import scratch_sheriff_request as sreq                               # noqa: E402

_N = 0
_FAIL = []


def ok(cond, msg):
    global _N
    _N += 1
    if not cond:
        _FAIL.append(msg)
        print(f"  FAIL: {msg}")


def _as_sheriff():
    """Authorize this process as the sheriff (what the daemon does at import)."""
    rec.authorize_sheriff(rec.sheriff_token(create=True))


def _as_deputy():
    rec._SHERIFF_AUTHORIZED = False


PROMPT = "# A test critic\n\nJudge whether the thing is good.\n"


TEST_REQUESTER = "operator@example.com"


def _setup():
    """Seed the fixtures every test may rely on, so the suite is order-independent
    (the runner walks tests alphabetically)."""
    # The receptionist allow-list is INSTANCE state (operator.json / INFRA_MAIL_ALLOWED),
    # so pin it: the hand-off tests must assert the queue's authorization RULE, not
    # whichever e-mails this particular install happens to trust.
    sreq.ALLOWED_REQUESTERS = {TEST_REQUESTER}
    _as_sheriff()
    cri.register("tcritic", display_name="Test Critic", description="a test",
                 prompt=PROMPT, model="opus", added_by="depA", request_id="r1")
    cri.register("anonymous", display_name="Anonymous Critic", prompt=PROMPT)
    cri.register("gone", display_name="Goner", prompt=PROMPT)
    cri.remove("gone")
    _as_deputy()


# ---------------------------------------------------------------------------
# charter
# ---------------------------------------------------------------------------
def test_packaged_charter_seed_matches_the_live_charter():
    """Case 551 round-3 finding: the DEFAULT_CHARTER constant is what a FRESH install
    seeds, so if it drifts from the sheriff-approved on-disk charter a new deployment
    silently gets a different (older) charter than production."""
    # Deliberately the REAL production path, not cri._charter_path() -- under the
    # throwaway TSOMP_RECORDS_ROOT the on-disk charter was itself just seeded FROM
    # DEFAULT_CHARTER, so comparing the two there would be tautological.
    prod = ROOT / "scratch_full_logs" / "records" / "critics" / "CHARTER.md"
    if prod.is_file():
        ok(cri.DEFAULT_CHARTER == prod.read_text(encoding="utf-8"),
           "the packaged charter seed must equal the sheriff-approved production charter")
    else:
        ok(True, "no production charter on this host; seed-vs-production check skipped")
    ok(cri.DEFAULT_CHARTER.splitlines()[0].startswith("# THE JUDGE CHARTER"),
       "the seed carries the current (judge) charter, not a stale critic-era copy")


def test_charter_materializes_and_is_stable():
    t = cri.charter_text()
    ok("SIGN-OFF" in t and "REVISE" in t and "REJECT" in t, "charter names all 3 verdicts")
    ok("verdict.json" in t and "verdict.md" in t, "charter states the output contract")
    ok(cri._charter_path().is_file(), "charter is materialized on disk for auditing")
    ok(cri.charter_text() == t, "charter is stable across reads")


# ---------------------------------------------------------------------------
# registry: the sheriff gate
# ---------------------------------------------------------------------------
def test_deputy_cannot_write_the_registry():
    _as_deputy()
    for fn, args in ((cri.register, ("rogue",)), (cri.update, ("rogue",)),
                     (cri.remove, ("rogue",))):
        try:
            fn(*args, prompt=PROMPT) if fn is not cri.remove else fn(*args)
            ok(False, f"{fn.__name__} must refuse an unauthorized process")
        except PermissionError as ex:
            ok("propose" in str(ex), f"{fn.__name__} refusal points at propose()")
        except Exception as ex:
            ok(False, f"{fn.__name__} raised {type(ex).__name__} not PermissionError: {ex}")
    ok(cri.get_critic("rogue") is None, "nothing was written by the refused calls")


def test_sheriff_can_register_and_read_back():
    _as_sheriff()
    out = cri.register("regread", display_name="Read Back", description="a test",
                       prompt=PROMPT, model="opus", added_by="depA", request_id="r1")
    ok(out["id"] == "regread", "register returns the id")
    c = cri.get_critic("regread")
    ok(c and c["display_name"] == "Read Back", "critic reads back")
    ok(c["approved_by"] == "sheriff", "provenance records the sheriff approval")
    ok(c["request_id"] == "r1", "provenance records the originating request")
    ok(cri.custom_prompt("regread").strip() == PROMPT.strip(), "custom prompt round-trips")
    ok((cri._records_root() / c["prompt_file"]).is_file(), "prompt file lives under records/")


def test_duplicate_add_refused_but_update_works():
    _as_sheriff()
    cri.register("dup", prompt=PROMPT, description="orig")
    try:
        cri.register("dup", prompt=PROMPT)
        ok(False, "a duplicate critic_add must be refused")
    except ValueError as ex:
        ok("already exists" in str(ex), "duplicate refusal explains itself")
    cri.update("dup", description="changed", prompt="# v2\n\nNew taste.\n")
    ok(cri.get_critic("dup")["description"] == "changed", "update changes metadata")
    ok("New taste" in cri.custom_prompt("dup"), "update rewrites the prompt file")


def test_reserved_and_malformed_ids_refused():
    _as_sheriff()
    for bad in ("charter", "none", "off", "Bad Id", "9lives", "", "x" * 40):
        try:
            cri.register(bad, prompt=PROMPT)
            ok(False, f"id {bad!r} must be refused")
        except ValueError:
            ok(True, f"id {bad!r} refused")


def test_charter_update_via_update_charter():
    _as_sheriff()
    before = cri.charter_text()
    cri.update("charter", prompt=before + "\n## 8. ADDENDUM\n\nTest addendum.\n")
    ok("ADDENDUM" in cri.charter_text(), "the charter is updatable under sheriff authority")
    cri.update("charter", prompt=before)                       # restore
    ok("ADDENDUM" not in cri.charter_text(), "charter restored")


def test_retire_is_soft_and_default_is_protected():
    _as_sheriff()
    ok(cri.get_critic("gone")["status"] == cri.STATUS_RETIRED, "retire is a status flip")
    ok(cri.is_critic("gone") is False, "a retired critic is not selectable")
    ok("gone" not in [c["id"] for c in cri.list_critics()], "retired hidden by default")
    ok("gone" in [c["id"] for c in cri.list_critics(include_retired=True)], "--all shows it")
    ok(cri.custom_prompt("gone").strip() == PROMPT.strip(),
       "the retired critic's prompt SURVIVES so old sign-offs stay reproducible")
    try:
        cri.remove("anonymous")
        ok(False, "the default critic must not be retirable")
    except ValueError as ex:
        ok("default" in str(ex), "refusal explains the default critic is protected")


# ---------------------------------------------------------------------------
# choice normalization (the form field / e-mail tag -> critic id)
# ---------------------------------------------------------------------------
def test_normalize_choice():
    _as_sheriff()
    ok(cri.normalize_choice("") == "", "empty -> no critic (the DEFAULT)")
    for v in ("none", "no", "off", "0", "false", "NONE", "  None  "):
        ok(cri.normalize_choice(v) == "", f"{v!r} -> no critic")
    for v in ("yes", "true", "1", "default"):
        ok(cri.normalize_choice(v) == "anonymous", f"{v!r} -> the default critic")
    ok(cri.normalize_choice("tcritic") == "tcritic", "a known id passes through")
    ok(cri.normalize_choice("TCritic") == "tcritic", "case-insensitive")
    ok(cri.normalize_choice("nosuchcritic") == "", "an unknown id degrades to NO critic")
    ok(cri.normalize_choice("gone") == "", "a retired id degrades to NO critic")


# ---------------------------------------------------------------------------
# prompt composition
# ---------------------------------------------------------------------------
def test_compose_layers_charter_then_persona_then_assignment():
    _as_sheriff()
    p = cri.compose("tcritic", case=551, request_text="do the thing",
                    artifacts=["/tmp/a.pdf"], outdir="/tmp/out", round_no=1)
    ok(p.index("THE JUDGE CHARTER") < p.index("YOUR PERSONA") < p.index("THIS ASSIGNMENT"),
       "charter -> persona -> assignment, in that order")
    ok("do the thing" in p, "the original request is carried in")
    ok("/tmp/a.pdf" in p, "the artifact path is carried in")
    ok("/tmp/out/verdict.json" in p, "the output contract names the exact paths")
    ok("ROUND: 1" in p, "the round is stated")


def test_compose_round2_carries_the_previous_verdict():
    _as_sheriff()
    prev = {"verdict": "REVISE", "one_line": "close", "round": 1,
            "must_fix": [{"location": "p2", "problem": "no baseline", "fix": "add one"}]}
    p = cri.compose("tcritic", case=551, request_text="r", artifacts=["a"],
                    outdir="/tmp/o", round_no=2, previous=prev, deputy_note="added a baseline")
    ok("no baseline" in p, "the previous must-fix is shown back to the critic")
    ok("added a baseline" in p, "the deputy's note on what changed is included")
    ok("NOT ADDRESSED" in p, "round-2 instructions demand a per-item status")


def test_compose_flags_missing_artifacts_list():
    _as_sheriff()
    p = cri.compose("tcritic", case=1, request_text="r", artifacts=[], outdir="/o")
    ok("REVISE finding" in p, "no artifacts is itself flagged as a finding")


# ---------------------------------------------------------------------------
# verdict parsing (the machine contract)
# ---------------------------------------------------------------------------
def _round(case, n, *, js=None, md=None):
    d = cri.round_dir(case, n, create=True)
    if js is not None:
        (d / "verdict.json").write_text(json.dumps(js), encoding="utf-8")
    if md is not None:
        (d / "verdict.md").write_text(md, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"critic": "tcritic", "ts": 1.0,
                                             "artifacts": ["a"]}), encoding="utf-8")
    return d


def test_parse_verdict_json_signoff():
    d = _round(9001, 1, js={"verdict": "SIGN-OFF", "one_line": "good", "must_fix": []})
    v = cri.parse_verdict(d)
    ok(v["verdict"] == "SIGN-OFF" and v["signed_off"] is True, "clean sign-off parses")


def test_parse_verdict_falls_back_to_markdown():
    d = _round(9002, 1, md="VERDICT: REVISE\nneeds a baseline\n")
    v = cri.parse_verdict(d)
    ok(v["verdict"] == "REVISE" and not v["signed_off"], "md first-line fallback works")


def test_parse_verdict_normalizes_token_spellings():
    for spelling in ("SIGNOFF", "sign off", "Sign-Off"):
        d = _round(9003, 1, js={"verdict": spelling, "must_fix": []})
        ok(cri.parse_verdict(d)["verdict"] == "SIGN-OFF", f"{spelling!r} normalizes")
        shutil.rmtree(cri.case_dir(9003))


def test_signoff_with_mustfix_is_coerced_to_revise():
    d = _round(9004, 1, js={"verdict": "SIGN-OFF", "one_line": "ok",
                            "must_fix": [{"location": "x", "problem": "y", "fix": "z"}]})
    v = cri.parse_verdict(d)
    ok(v["verdict"] == "REVISE" and v["signed_off"] is False,
       "a self-contradictory SIGN-OFF cannot let the deputy close")
    ok("coerced" in v, "the coercion is recorded, not silent")


def test_unknown_verdict_token_defaults_to_revise():
    d = _round(9005, 1, js={"verdict": "LGTM", "must_fix": []})
    ok(cri.parse_verdict(d)["verdict"] == "REVISE", "an unknown token fails CLOSED")


def test_missing_verdict_is_empty_not_a_signoff():
    d = cri.round_dir(9006, 1, create=True)
    ok(cri.parse_verdict(d) == {}, "no verdict files -> {} (never a sign-off)")


# ---------------------------------------------------------------------------
# history / rounds / sign-off state
# ---------------------------------------------------------------------------
def test_history_and_signed_off_and_next_round():
    _round(9010, 1, js={"verdict": "REVISE", "one_line": "a",
                        "must_fix": [{"location": "l", "problem": "p", "fix": "f"}]})
    ok(cri.next_round(9010) == 2, "next_round follows the highest existing round")
    _round(9010, 2, js={"verdict": "SIGN-OFF", "one_line": "b", "must_fix": []})
    h = cri.history(9010)
    ok([r["round"] for r in h] == [1, 2], "history is ordered")
    ok(h[0]["verdict"] == "REVISE" and h[1]["signed_off"], "verdicts per round")
    so = cri.signed_off(9010)
    ok(so and so["round"] == 2, "signed_off finds the sign-off round")
    ok(cri.signed_off(9011) == {}, "a case with no rounds is not signed off")
    ok(cri.next_round(9011) == 1, "a fresh case starts at round 1")


def test_signed_off_ignores_an_earlier_signoff_only_if_none_later():
    """A later REVISE does not erase an earlier sign-off record, but the deputy's
    gate is 'is there any sign-off', so keep the semantics explicit."""
    _round(9012, 1, js={"verdict": "SIGN-OFF", "must_fix": []})
    _round(9012, 2, js={"verdict": "REVISE",
                        "must_fix": [{"location": "l", "problem": "p", "fix": "f"}]})
    ok(cri.signed_off(9012)["round"] == 1, "the latest SIGN-OFF round is reported")


# ---------------------------------------------------------------------------
# the review round (spawn stubbed -- no agents, no API)
# ---------------------------------------------------------------------------
def test_review_writes_prompt_and_meta_and_returns_verdict():
    _as_sheriff()
    seen = {}

    def fake_spawn(name, prompt_path, model):
        seen["name"], seen["model"] = name, model
        d = Path(prompt_path).parent
        (d / "verdict.json").write_text(json.dumps(
            {"verdict": "SIGN-OFF", "one_line": "all good", "must_fix": []}), encoding="utf-8")
        (d / "verdict.md").write_text("VERDICT: SIGN-OFF\n\nall good\n", encoding="utf-8")

    v = cri.review("tcritic", case=9020, artifacts=[str(ROOT / "scratch_critic.py")],
                   request_text="build the thing", spawn=fake_spawn, poll=0.01, timeout=30)
    ok(v["verdict"] == "SIGN-OFF" and v["signed_off"], "review returns the parsed verdict")
    ok(v["round"] == 1 and v["critic"] == "tcritic", "round + critic are attributed")
    d = cri.round_dir(9020, 1)
    ok((d / "prompt.md").is_file(), "the exact prompt is archived for audit")
    ok((d / "meta.json").is_file(), "round metadata is archived")
    ok(seen["name"].startswith("critic_9020_tcritic_r1"), "anon worker name encodes case/critic/round")
    ok(seen["model"] == "opus", "the critic's registered model is used")
    ok("build the thing" in (d / "prompt.md").read_text(), "the request is in the archived prompt")


def test_review_auto_increments_and_shows_previous():
    _as_sheriff()
    captured = {}

    def fake_spawn(name, prompt_path, model):
        d = Path(prompt_path).parent
        captured["prompt"] = Path(prompt_path).read_text()
        (d / "verdict.json").write_text(json.dumps(
            {"verdict": "REVISE", "one_line": "no",
             "must_fix": [{"location": "L1", "problem": "unchecked claim", "fix": "check it"}]}),
            encoding="utf-8")

    cri.review("tcritic", case=9021, artifacts=["x"], spawn=fake_spawn, poll=0.01, timeout=30)
    v2 = cri.review("tcritic", case=9021, artifacts=["x"], spawn=fake_spawn, poll=0.01,
                    timeout=30, deputy_note="fixed it")
    ok(v2["round"] == 2, "the round auto-increments")
    ok("unchecked claim" in captured["prompt"], "round 2 shows the critic its own must-fix")
    ok("fixed it" in captured["prompt"], "round 2 carries the deputy's note")


def test_review_waits_for_a_late_verdict_md():
    """Case 551 live finding: a critic writes verdict.json and verdict.md as two
    separate calls, so the JSON can land first. review() must not return a round whose
    human-readable review is still in flight."""
    _as_sheriff()
    state = {"calls": 0}

    def fake_spawn(name, prompt_path, model):
        d = Path(prompt_path).parent
        (d / "verdict.json").write_text(json.dumps(
            {"verdict": "SIGN-OFF", "one_line": "ok", "must_fix": []}), encoding="utf-8")
        # verdict.md deliberately NOT written yet

    old = cri._anon_alive

    def alive(name):
        state["calls"] += 1
        if state["calls"] == 2:      # on the 2nd liveness check, the critic finishes
            d = cri.round_dir(9030, 1)
            (d / "verdict.md").write_text("VERDICT: SIGN-OFF\n\nthe real review\n",
                                          encoding="utf-8")
        return True

    cri._anon_alive = alive
    try:
        v = cri.review("tcritic", case=9030, artifacts=["x"], spawn=fake_spawn,
                       poll=0.01, timeout=30)
    finally:
        cri._anon_alive = old
    ok(v["verdict"] == "SIGN-OFF", "the verdict still parses")
    ok("the real review" in v.get("markdown", ""), "review WAITED for the critic's own md")
    ok(not v.get("md_reconstructed"), "a critic-written md is never reconstructed over")


def test_review_reconstructs_md_when_the_critic_never_writes_it():
    _as_sheriff()

    def fake_spawn(name, prompt_path, model):
        d = Path(prompt_path).parent
        (d / "verdict.json").write_text(json.dumps(
            {"verdict": "REVISE", "one_line": "no",
             "must_fix": [{"location": "L1", "problem": "p", "fix": "f"}],
             "should_fix": ["s"]}), encoding="utf-8")

    old = cri._anon_alive
    cri._anon_alive = lambda name: False        # critic exited without writing md
    try:
        v = cri.review("tcritic", case=9031, artifacts=["x"], spawn=fake_spawn,
                       poll=0.01, timeout=30)
    finally:
        cri._anon_alive = old
    ok(v["verdict"] == "REVISE", "verdict parses from json alone")
    ok(v.get("md_reconstructed") is True, "the missing md is reconstructed")
    p = cri.round_dir(9031, 1) / "verdict.md"
    ok(p.is_file(), "a readable verdict.md is written to disk")
    txt = p.read_text()
    ok(txt.startswith("VERDICT: REVISE"), "reconstructed md keeps the contract first line")
    ok("L1" in txt and "reconstructed by the harness" in txt,
       "it carries the findings AND says it was reconstructed")


def test_ensure_verdict_md_never_clobbers_a_real_one():
    d = _round(9032, 1, js={"verdict": "SIGN-OFF", "must_fix": []},
               md="VERDICT: SIGN-OFF\n\nauthored by the critic\n")
    v = cri.parse_verdict(d)
    cri.ensure_verdict_md(d, v)
    ok("authored by the critic" in (d / "verdict.md").read_text(),
       "an existing critic-written verdict.md survives untouched")


def test_review_unknown_critic_raises():
    try:
        cri.review("ghost", case=9022, artifacts=["x"], spawn=lambda *a: None)
        ok(False, "an unknown critic must raise, not silently pass")
    except KeyError as ex:
        ok("ghost" in str(ex), "the error names the unknown critic")


def test_review_reports_a_dead_critic_instead_of_hanging():
    _as_sheriff()
    old = cri._anon_alive
    cri._anon_alive = lambda name: False
    try:
        v = cri.review("tcritic", case=9023, artifacts=["x"],
                       spawn=lambda *a: None, poll=0.01, timeout=30)
    finally:
        cri._anon_alive = old
    ok(v["signed_off"] is False, "a dead critic is NEVER a sign-off")
    ok("without writing a verdict" in v.get("error", ""), "the failure is reported explicitly")


def test_review_times_out_without_signing_off():
    _as_sheriff()
    old = cri._anon_alive
    cri._anon_alive = lambda name: True
    try:
        v = cri.review("tcritic", case=9024, artifacts=["x"],
                       spawn=lambda *a: None, poll=0.01, timeout=0.05)
    finally:
        cri._anon_alive = old
    ok(v["signed_off"] is False and "timed out" in v.get("error", ""),
       "a timeout fails CLOSED with an explicit error")


# ---------------------------------------------------------------------------
# propose -> the sheriff request queue
# ---------------------------------------------------------------------------
def test_propose_writes_a_request_and_never_the_registry():
    _as_deputy()
    out = cri.propose("newcritic", display_name="New", description="d", prompt=PROMPT,
                      deputy="depA", session="sessA", reason="the group wants it")
    ok(out["state"] == "pending", "propose leaves the request pending")
    r = out["record"]
    ok(r["op"] == "critic_add" and r["target"] == "newcritic", "op + target are set")
    ok(r["critic_prompt"] == PROMPT, "the prompt travels inside the request")
    ok(r["critic_display_name"] == "New", "the display name travels")
    ok(cri.get_critic("newcritic") is None, "propose writes NOTHING to the registry")


def test_propose_requires_a_prompt_and_a_reason():
    _as_deputy()
    try:
        cri.propose("nop", deputy="depA", session="sessA", reason="x")
        ok(False, "critic_add without a prompt must be refused")
    except ValueError as ex:
        ok("prompt" in str(ex), "refusal names the missing prompt")
    try:
        cri.propose("nop", prompt=PROMPT, deputy="depA", session="sessA", reason="")
        ok(False, "a request without a reason must be refused")
    except ValueError as ex:
        ok("reason" in str(ex), "refusal names the missing reason")


def test_propose_rejects_a_forged_deputy_session_at_submit():
    _as_deputy()
    try:
        cri.propose("forged", prompt=PROMPT, deputy="", session="", reason="r")
        ok(False, "a request with no deputy identity must be refused")
    except ValueError:
        ok(True, "an identity-less deputy request is refused at submit")


def test_receptionist_handoff_is_allowed_for_critic_ops():
    """The user asks by e-mail; the receptionist hands off WITHOUT a deputy session."""
    _as_deputy()
    out = cri.propose("usercritic", prompt=PROMPT, reason="Steven asked by e-mail",
                      origin="receptionist", requester="operator@example.com")
    ok(out["state"] == "pending", "a receptionist hand-off is accepted")
    ok(out["record"]["origin"] == "receptionist", "origin recorded")
    ok("critic_add" in sreq.USER_AUTHORIZED_OPS, "critic ops are user-authorized")


def test_receptionist_handoff_rejects_a_stranger():
    _as_deputy()
    try:
        cri.propose("evil", prompt=PROMPT, reason="r", origin="receptionist",
                    requester="attacker@example.com")
        ok(False, "a hand-off from an unknown e-mail must be refused")
    except ValueError as ex:
        ok("allowed user email" in str(ex), "refusal names the allow-list")


# ---------------------------------------------------------------------------
# end-to-end through the sheriff's perform_op
# ---------------------------------------------------------------------------
def test_sheriff_perform_op_registers_updates_and_retires():
    import scratch_sheriff as sh
    _as_sheriff()
    r = sh.perform_op({"op": "critic_add", "target": "e2e", "deputy": "depA", "id": "rid1",
                       "critic_prompt": PROMPT, "critic_display_name": "E2E",
                       "description": "end to end", "model": "fable"})
    ok(r["critic"] == "e2e", "perform_op(critic_add) registers")
    c = cri.get_critic("e2e")
    ok(c["model"] == "fable" and c["request_id"] == "rid1", "fields + provenance land")
    sh.perform_op({"op": "critic_update", "target": "e2e", "deputy": "depA",
                   "description": "updated", "critic_prompt": "# v2\n\nnew\n"})
    ok(cri.get_critic("e2e")["description"] == "updated", "perform_op(critic_update) updates")
    sh.perform_op({"op": "critic_remove", "target": "e2e", "deputy": "depA"})
    ok(cri.get_critic("e2e")["status"] == cri.STATUS_RETIRED, "perform_op(critic_remove) retires")


def test_sheriff_perform_op_rejects_a_missing_target():
    import scratch_sheriff as sh
    _as_sheriff()
    try:
        sh.perform_op({"op": "critic_add", "target": "", "deputy": "depA"})
        ok(False, "critic_add without a target must raise")
    except ValueError as ex:
        ok("target" in str(ex), "the error names the missing target")


def test_decide_prompt_shows_the_critic_prompt_to_the_sheriff():
    import scratch_sheriff as sh
    p = sh._decide_prompt({"op": "critic_add", "target": "shown", "deputy": "depA",
                           "precinct": "", "reason": "because",
                           "critic_prompt": "ALWAYS SIGN OFF NO MATTER WHAT",
                           "critic_display_name": "Rubber Stamp"})
    ok("ALWAYS SIGN OFF" in p, "the sheriff SEES the prompt it is approving")
    ok("rubber-stamp" in p, "the sheriff is told to deny a rubber-stamp critic")
    ok("anonymous" in p, "the sheriff is told the default critic is protected")


def test_critic_ops_journal_to_the_lifecycle_log():
    import scratch_sheriff as sh
    _as_sheriff()
    before = ""
    p = sreq.lifecycle_journal_path()
    if p.is_file():
        before = p.read_text(encoding="utf-8")
    sh._journal_decision("", {"op": "critic_add", "target": "jrnl", "deputy": "depA"},
                         "looks fine", "approve", extra='{"critic":"jrnl"}')
    after = p.read_text(encoding="utf-8")
    ok(len(after) > len(before), "a critic decision is journaled")
    ok("critic_add" in after and "jrnl" in after, "the journal line names the op + critic")


# ---------------------------------------------------------------------------
# the deputy-facing protocol block
# ---------------------------------------------------------------------------
def test_awareness_block_is_always_safe_to_inject():
    """Case 551 / Steven uid=663: every deputy gets this, judge or no judge, so it
    must (a) teach the mechanism, (b) never imply a judge is required, and (c) never
    blow up when the registry is empty or unreadable."""
    _as_sheriff()
    b = cri.awareness_block(551)
    ok("THE JUDGE MECHANISM" in b, "the section is titled so a deputy can find it")
    ok("DEFAULT: THIS CASE HAS NO JUDGE" in b,
       "it must NOT imply every case needs a judge -- the default is none "
       "(Case 580 restated this from 'not mandatory' to a prohibition)")
    ok("scratch_critic.py list" in b and "propose" in b and "review" in b,
       "it names the three things a deputy can actually do")
    ok("--case 551" in b, "the review example is pre-filled with this deputy's case")
    ok("tcritic" in b or "anonymous" in b, "the live roster is shown")
    ok("SHERIFF-OWNED" in b, "it states the deputy may not write the roster itself")
    # Case 580 raised this cap from 2000 to 2600 (block went 1358 -> 2376). The cap
    # exists because the preamble is paid on EVERY launch, so the raise was priced,
    # not waved through: +1,018 chars x 35 launches since the judge subsystem
    # shipped = ~36 KB added, against 18 unasked judge rounds x 33,230 chars mean
    # prompt (n=34 archived rounds) = ~598 KB avoided -- and that counts only each
    # round's INPUT prompt, not the artifact reads, tool use or the ~24 KB verdict.
    # One prevented round pays for ~33 launches. Keep the cap: the next person to
    # add a paragraph here should have to justify it the same way.
    ok(len(b) < 2600, f"kept short (preamble cost is paid every launch); got {len(b)}")


def test_awareness_block_does_not_licence_an_unasked_judge_run():
    """Case 580 (Feng: "I didn't tell case 576 to use judge, but it did use the judge").

    The v1 block taught the mechanism and then handed out a standing licence to use
    it -- "Review your OWN work voluntarily before a FINAL (any case, no permission
    needed)". 7 of the 13 cases that ran a judge had never been given one (551, 559,
    563, 566, 568, 570, 576) and not one of their specs carried a JUDGE PROTOCOL
    section, so all 18 of those rounds rested on that sentence. This pins the
    replacement so the invitation cannot creep back in a later edit."""
    _as_sheriff()
    b = cri.awareness_block(576)
    low = b.lower()

    # 1. the exact v1 licence, and the words that carried it, are gone
    ok("no permission needed" not in low,
       "the standing licence 'any case, no permission needed' must not return")
    ok("voluntarily" not in low,
       "'voluntarily' framed an unasked review as a free choice; it is not")

    # 2. the default is stated as a prohibition, not merely as 'not required'
    ok("DO NOT RUN ONE UNLESS YOU WERE ASKED TO" in b,
       "the header states the prohibition, so it is read even if the body is skimmed")
    ok("DEFAULT: THIS CASE HAS NO JUDGE" in b, "the default is spelled out")
    ok("do not offer one unprompted" in low,
       "offering a review unprompted is the same spend, so it is barred too")

    # 3. the permitted triggers are enumerated AND closed -- an open-ended list is
    #    what let 'being thorough' become a reason.
    ok("there is no fourth reason" in low, "the trigger list is explicitly closed")
    for n in ("1.", "2.", "3."):
        ok(n in b, f"trigger {n} is enumerated")
    ok("JUDGE PROTOCOL" in b, "trigger 1 = the case was created with a judge")
    ok("Steven asks" in b, "trigger 2 = the requester asked for a review")
    ok("running a judge IS the work" in b, "trigger 3 = judges are the case's subject")

    # 4. the two rationalisations actually observed in the 7 cases are named and
    #    refused by name -- 'I was being thorough' and 'I wasn't sure it was good'.
    ok("being thorough" in low, "'thoroughness' is refused as a reason")
    ok("unsure" in low and "NOT one of the three" in b,
       "self-doubt is refused as a reason, with the cheaper alternative given")
    ok("reviewing\nyour own report" in b or "your own report" in low,
       "trigger 3 must not be read as licensing self-review of a judge case's report")

    # 5. the deputy is told the price, because the cost is the whole argument
    ok("whole extra Claude worker" in low or "costs a worker" in low,
       "a round is priced in the block, so the deputy knows what it is spending")

    # 6. it still TEACHES -- removing the licence must not remove the knowledge,
    #    or a deputy legitimately asked for a review cannot comply (trigger 2).
    ok("scratch_critic.py review" in b, "the runnable review command is still present")
    ok("hand-roll" in low, "hand-rolling a critic instead is still barred")


def test_awareness_block_survives_an_unreadable_registry():
    old = cri.list_critics
    cri.list_critics = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    try:
        b = cri.awareness_block(7)
    finally:
        cri.list_critics = old
    ok("THE JUDGE MECHANISM" in b, "still renders without the roster line")
    ok("Currently registered" not in b, "the roster line is simply omitted")


def test_protocol_block_is_actionable():
    _as_sheriff()
    b = cri.protocol_block("tcritic", 551)
    ok("scratch_critic.py review --critic tcritic --case 551" in b,
       "the block contains the exact runnable command")
    ok(b.startswith("JUDGE PROTOCOL"), "Case 551 uid=662: named 'judge', not 'critic'")
    ok("SIGN-OFF" in b and "REVISE" in b and "REJECT" in b, "all three outcomes are handled")
    ok("round 4" in b, "there is an escape hatch from an endless loop")
    ok("falsified record" in b, "hand-editing a verdict is explicitly forbidden")


# ---------------------------------------------------------------------------
# Case 569: the JUDGE-AUTHORING block (the receptionist's marching orders when a
# user proposes a judge from the dashboard by DESCRIBING it rather than writing
# its prompt).
# ---------------------------------------------------------------------------
def test_authoring_block_is_a_complete_brief():
    _as_sheriff()
    b = cri.authoring_block(name="The Reproducibility Judge", cid="repro", case=569,
                            model="opus", requester="operator@example.com",
                            deputy="judge_569")
    ok(b.startswith("YOUR JOB ON THIS CASE"), "the deputy is told its job in line 1")
    ok("The Reproducibility Judge" in b and "repro" in b, "name + id are stated")
    ok("You may not\nwrite the registry yourself" in b,
       "the REDLINE is restated: the deputy never writes the roster")
    # it must send the deputy to READ before writing
    for cmd in ["scratch_critic.py charter", "show --critic anonymous",
                "show --critic vyas"]:
        ok(cmd in b, f"the brief does not tell the deputy to read: {cmd}")
    # ...and the runnable filing command, with THIS proposal's values
    ok("propose --critic repro --op critic_add" in b, "no runnable propose command")
    ok('--display-name "The Reproducibility Judge"' in b, "display name not passed through")
    ok("--model opus" in b, "the user's model choice is not passed through")
    ok("--origin receptionist --requester operator@example.com" in b,
       "the hand-off origin/requester are missing (the request would be rejected)")
    ok("--deputy judge_569" in b, "the deputy is not identified on the request")
    ok("--prompt-file" in b and "Task 176" in b,
       "the prompt must go via a file, not argv (the self-kill footgun)")
    ok("close case 569" in b, "the brief does not close the loop on this case")


def test_authoring_block_teaches_the_actual_lessons():
    """The brief exists so the prompt is written well, not merely written."""
    _as_sheriff()
    b = cri.authoring_block(name="X Judge", cid="x", case=1, deputy="d")
    ok("IT DOES NOT REPEAT THE CHARTER" in b, "the #1 failure mode is not named")
    ok("PRIORITY ORDER" in b, "priority order -- the thing that makes a judge a judge")
    ok("EXECUTABLE, NOT ADJECTIVES" in b, "no guidance on testable criteria")
    ok("must_fix" in b and "should_fix" in b, "blocking vs non-blocking not explained")
    ok("Case 556" in b, "the one-note-critic decay lesson is not carried over")
    ok("CLONE" in b, "nothing stops a copy of an existing judge")
    ok("sheriff_request.py pending" in b,
       "Case 569 dogfood: the brief must send the deputy to the IN-FLIGHT queue too -- "
       "the id check cannot see a semantic duplicate filed an hour earlier")
    ok("YOU write this" in b,
       "the deputy is not told the roster description + sheriff reason are its job")


def test_authoring_block_omits_the_model_when_none_was_picked():
    _as_sheriff()
    b = cri.authoring_block(name="X Judge", cid="x", case=1, deputy="d")
    ok("--model" not in b, "a phantom --model flag with no value")
    ok("picked a MODEL" not in b, "claims a model choice that was never made")


def test_authoring_block_quotes_live_sizes_not_hardcoded_ones():
    """The 'how long should it be' answer must come from the live roster -- a
    hardcoded number goes stale the first time a persona is rewritten."""
    _as_sheriff()
    b = cri.authoring_block(name="X Judge", cid="x", case=1, deputy="d")
    n = len(cri.custom_prompt("tcritic") or "")
    ok(f"{n:,} chars" in b, f"the live size of tcritic ({n}) is not quoted")
    ok(f"{len(cri.charter_text()):,} chars" in b, "the live charter size is not quoted")


def test_authoring_block_does_not_inline_the_charter():
    """Pasting the charter in would double it in the deputy's context AND create a
    second copy that drifts -- exactly what the brief tells the deputy not to do."""
    _as_sheriff()
    b = cri.authoring_block(name="X Judge", cid="x", case=1, deputy="d")
    ok(len(b) < 9000, f"the brief must stay a brief; got {len(b)}")
    ok(cri.charter_text()[:200] not in b, "the charter body was inlined")


def test_authoring_block_survives_an_unreadable_registry():
    old = cri.list_critics
    cri.list_critics = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    try:
        b = cri.authoring_block(name="X Judge", cid="x", case=1, deputy="d")
    finally:
        cri.list_critics = old
    ok(b.startswith("YOUR JOB ON THIS CASE"), "still renders without the roster")
    ok("(none registered yet)" in b, "the roster line degrades to a placeholder")


# ---------------------------------------------------------------------------
# first-install seed (the judges this repo ships with)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _fresh_root():
    """Point the records root at an EMPTY dir for one test. The shared fixture root
    already holds an 'anonymous', which would mask exactly what seeding must do."""
    prev = os.environ["TSOMP_RECORDS_ROOT"]
    d = Path(tempfile.mkdtemp(prefix="critic_seed_", dir=str(_TMP)))
    os.environ["TSOMP_RECORDS_ROOT"] = str(d)
    try:
        yield d
    finally:
        os.environ["TSOMP_RECORDS_ROOT"] = prev


def test_packaged_judge_prompts_ship_with_the_repo():
    """The seed copies tracked repo files; if they are missing the release is inert."""
    for cid in cri.PACKAGED_JUDGES:
        p = cri.packaged_judge_path(cid)
        ok(p.is_file(), f"packaged prompt for {cid!r} exists ({p.name})")
        ok(len(p.read_text(encoding="utf-8").strip()) > 500,
           f"packaged prompt for {cid!r} is a real prompt, not a stub")


def test_seed_installs_the_shipped_judges_and_the_charter():
    with _fresh_root():
        done = cri.seed_critics()
        ok(sorted(done) == ["anonymous", "vyas"], "seeds exactly the shipped judges")
        ok(cri.is_critic("anonymous") and cri.is_critic("vyas"), "both are live")
        ok(cri.charter_text().strip().startswith("# THE JUDGE CHARTER"),
           "the charter is materialized too")
        ok(cri.custom_prompt("vyas") == cri.packaged_judge_path("vyas").read_text(
            encoding="utf-8"), "the installed persona is the packaged file verbatim")
        ok(cri.get_critic("vyas")["model"] == "opus", "packaged model carried over")
        ok(cri.get_critic("anonymous")["model"] is None, "anonymous uses the deputy default")
        ok(cri.normalize_choice("default") == "anonymous",
           "a fresh install resolves the default judge")


def test_seed_is_idempotent():
    with _fresh_root():
        cri.seed_critics()
        before = json.loads((Path(os.environ["TSOMP_RECORDS_ROOT"]) / "critics.json")
                            .read_text(encoding="utf-8"))
        ok(cri.seed_critics() == [], "a second seed installs nothing")
        after = json.loads((Path(os.environ["TSOMP_RECORDS_ROOT"]) / "critics.json")
                           .read_text(encoding="utf-8"))
        ok(before == after, "and does not rewrite the registry")


def test_seed_never_overrides_an_operator_decision():
    """Retiring a shipped judge, or editing its prompt, must survive every later run
    (setup.py calls seed on every install/upgrade)."""
    with _fresh_root() as d:
        cri.seed_critics()
        _as_sheriff()
        cri.remove("vyas")                      # operator retires it
        cri.update("anonymous", prompt="# edited locally\n")
        _as_deputy()
        ok(cri.seed_critics() == [], "seed re-installs neither")
        reg = json.loads((d / "critics.json").read_text(encoding="utf-8"))
        ok(reg["critics"]["vyas"]["status"] == "retired", "the retired judge stays retired")
        ok(cri.custom_prompt("anonymous") == "# edited locally\n",
           "the locally edited prompt is preserved")


def test_seed_force_reinstalls():
    with _fresh_root():
        cri.seed_critics()
        _as_sheriff()
        cri.update("anonymous", prompt="# edited locally\n")
        _as_deputy()
        ok(sorted(cri.seed_critics(force=True)) == ["anonymous", "vyas"],
           "--force re-installs the packaged judges")
        ok(cri.custom_prompt("anonymous") == cri.packaged_judge_path("anonymous")
           .read_text(encoding="utf-8"), "and restores the packaged prompt")


def _run():
    _setup()
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except Exception as ex:
            _FAIL.append(f"{name} raised {type(ex).__name__}: {ex}")
            print(f"  ERROR in {name}: {type(ex).__name__}: {ex}")
    print(f"\n{_N - len(_FAIL)}/{_N} assertions passed across {len(tests)} tests")
    if _FAIL:
        print(f"{len(_FAIL)} FAILURE(S):")
        for f in _FAIL:
            print(f"  - {f}")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(_run())
