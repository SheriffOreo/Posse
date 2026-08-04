#!/usr/bin/env python3
"""Task 376: tests for the SHERIFF's TOKEN thresholds + the ZERO-API monitoring
guarantee.

Three properties are locked here:

  * A/B are TOKENS (A = soft limit 20000 > B = post-compaction target 10000), and
    a ledger is compacted only when its APPROX-TOKEN length crosses A, down to a
    B-token budget.
  * MONITORING IS ZERO-API: a monitoring pass (one_pass / maybe_compact below the
    limit, mechanical compaction above it, and a precinct inside its wait-and-retry
    backoff window) NEVER shells out to `claude`. The tests wrap subprocess.run with a
    guard that raises if any `claude` argv appears.
  * WAIT-AND-RETRY (Case 400a): when the default LLM compaction fails/limits, the
    sheriff leaves the ledger UNCHANGED (a brief overage by design, not a mechanical
    truncation) and retries a clean LLM compaction on a later pass, gated by a
    per-precinct backoff. TSOMP_SHERIFF_LLM=0 still forces mechanical compaction.

Self-contained (no pytest). Every record lives under a throwaway TSOMP_RECORDS_ROOT
and `_email` is stubbed to a recorder, so no real department data is touched and no
email is ever sent.  Run:  python scratch_sheriff_test.py
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

_TEST_ROOT = tempfile.mkdtemp(prefix="tsomp_sheriff_test_")
os.environ["TSOMP_RECORDS_ROOT"] = _TEST_ROOT
# never let TSOMP_SHERIFF_* from the caller's env skew the default tests (A/B, the
# Phase-C llm default, the global model resolution, or the compaction retry count)
os.environ.pop("TSOMP_SHERIFF_A", None)
os.environ.pop("TSOMP_SHERIFF_B", None)
os.environ.pop("TSOMP_SHERIFF_LLM", None)
os.environ.pop("TSOMP_SHERIFF_MODEL", None)         # Phase C: resolve from persisted config
os.environ.pop("TSOMP_SHERIFF_COMPACT_RETRIES", None)
os.environ.pop("TSOMP_SHERIFF_COMPACT_BACKOFF", None)  # Case 400a: keep the wait-and-retry backoff at its default
# Case 384a: a throwaway worker registry so verify_identity is deterministic/offline.
_REG = Path(_TEST_ROOT) / "registry.json"
_REG.write_text(json.dumps({"workers": {"depR": {"session": "sR"}}}))
os.environ["TSOMP_REGISTRY_PATH"] = str(_REG)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_records as rec  # noqa: E402
import scratch_sheriff as sh  # noqa: E402
import scratch_sheriff_request as sreq  # noqa: E402  (Case 384a)

# Stub the mailer so a compaction never sends real email; record calls so a test
# can assert the precinct stamp (Task 376 #1) is passed through.
_EMAILS = []
sh._email = lambda *a, **k: _EMAILS.append((a, k))

# Case 384a: stub the deputy notifier so request tests never shell out to the
# mailbox helper; record calls so a test can assert a denial notified the deputy.
_NOTIFIES = []
sh.notify_deputy = lambda deputy, subject, body: _NOTIFIES.append((deputy, subject, body))

cfg = sh._cfg()


def _clear_pending():
    """Empty the shared throwaway request queue so a request_pass test acts only on
    what it just submitted (the suite shares one records root)."""
    for p in sreq.list_pending():
        with contextlib.suppress(OSError):
            p.unlink()


def _clear_confirm():
    """Empty the Phase-D delete-confirmation subtree (awaiting + confirmations) so a
    confirm-flow test starts clean on the shared throwaway root."""
    for p in sreq.list_awaiting():
        with contextlib.suppress(OSError):
            p.unlink()
    cd = sreq._confirm_dir()
    if cd.is_dir():
        for p in cd.glob("*.json"):
            with contextlib.suppress(OSError):
                p.unlink()


@contextlib.contextmanager
def _approving(reason="ok"):
    """Patch sheriff_decide to APPROVE (Phase-D tests exercise the perform/confirm
    machinery, not the decision call, which has its own tests)."""
    orig = sh.sheriff_decide
    sh.sheriff_decide = lambda req, cfg, **k: {"decision": "approve", "reason": reason}
    try:
        yield
    finally:
        sh.sheriff_decide = orig


def _token_of(rid):
    """The confirm token the sheriff minted for an approved delete request rid."""
    return sreq.get(rid)["result"]["token"]


def _submit_pending(op, precinct, *, deputy="depR", session="sR", reason="because", target="", **kw):
    r = sreq.submit(op, precinct, deputy, session, reason, target=target, **kw)
    path = [p for p in sreq.list_pending() if p.stem == r["id"]][0]
    return r, path


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@contextlib.contextmanager
def _fake_claude(stdout="", stderr="", returncode=0):
    """Patch subprocess.run so a `claude` invocation returns a canned result (used to
    unit-test sheriff_decide's parsing/limit handling without a real API call)."""
    real = subprocess.run

    def fake(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if any("claude" in str(x) for x in argv):
            return _FakeProc(stdout, stderr, returncode)
        return real(cmd, *a, **k)

    subprocess.run = fake
    try:
        yield
    finally:
        subprocess.run = real


@contextlib.contextmanager
def _capture_claude(stdout="", stderr="", returncode=0, cap=None):
    """Like ``_fake_claude`` but ALSO records the claude ``argv`` + ``input`` prompt
    into ``cap`` (a dict), so a compaction test can assert the resolved global model
    reached the argv and the big-picture context reached the prompt (Phase C)."""
    real = subprocess.run

    def fake(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if any("claude" in str(x) for x in argv):
            if cap is not None:
                cap["argv"] = list(argv)
                cap["input"] = k.get("input", "")
            return _FakeProc(stdout, stderr, returncode)
        return real(cmd, *a, **k)

    subprocess.run = fake
    try:
        yield
    finally:
        subprocess.run = real


@contextlib.contextmanager
def _no_claude():
    """Fail the test if anything shells out to `claude` inside the block."""
    real = subprocess.run

    def guard(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if any("claude" in str(x) for x in argv):
            raise AssertionError(f"claude was invoked during monitoring: {argv}")
        return real(cmd, *a, **k)

    subprocess.run = guard
    try:
        yield
    finally:
        subprocess.run = real


def test_thresholds_are_tokens_A_gt_B():
    cfg = sh._cfg()
    assert cfg["A"] == 20000 and cfg["B"] == 10000, cfg      # TOKENS, not chars
    assert cfg["A"] > cfg["B"], "A (soft limit) must exceed B (target)"
    # Phase C: API compaction is the DEFAULT now (opt-OUT via TSOMP_SHERIFF_LLM=0)
    assert cfg["llm"] is True, "Phase C: API (LLM) compaction is the default"
    assert sh.CHARS_PER_TOKEN == 4
    # global sheriff model: env popped + no persisted config on this throwaway root
    # (the model-plumbing test cleans up after itself) -> the fable default
    assert cfg["model"] == "fable", cfg


def test_below_A_is_noop_and_zero_api():
    d = "_sh_below"
    rec.directory_register(d, ledger_mode="mutable")
    rec.ledger_append(d, "digest head paragraph.")
    for i in range(3):
        rec.ledger_append(d, f"case {i} " + "x" * 40)
    tok = rec.ledger_length(d)["approx_tokens"]
    assert tok < sh._cfg()["A"], "fixture must sit below the soft limit"
    n0 = len(_EMAILS)
    with _no_claude():
        did, out_tok = sh.maybe_compact(d, sh._cfg())
    assert did is False and out_tok == tok
    assert len(_EMAILS) == n0, "a below-limit ledger must not email"


def _big_ledger(dept, head="HEAD keep-me digest."):
    """Register `dept` and write a ledger that crosses a small (A=100) soft limit."""
    rec.directory_register(dept, ledger_mode="mutable")
    big = head + "\n\n" + "\n\n".join(f"c{i} " + "y" * 300 for i in range(20))
    rec.ledger_write(dept, big, role="sheriff")
    return rec.ledger_length(dept)["approx_tokens"]


def test_forced_mechanical_crossing_A_is_zero_api():
    # TSOMP_SHERIFF_LLM=0 (cfg['llm']=False) forces the deterministic mechanical path:
    # crossing A compacts to B and makes NO claude call (the Phase-C opt-out).
    d = "_sh_big"
    before = _big_ledger(d)
    cfg = dict(sh._cfg())
    cfg["A"], cfg["B"], cfg["llm"] = 100, 50, False        # TOKENS + force mechanical
    assert before > cfg["A"], "fixture must cross the soft limit"
    with _no_claude():                                      # mechanical -> no claude
        did, out_tok = sh.maybe_compact(d, cfg)
    assert did is True and out_tok < before
    assert out_tok <= cfg["B"] + 5, f"compacted to {out_tok} tok (target {cfg['B']})"
    txt = rec.ledger_read(d)
    assert txt.startswith("HEAD keep-me digest."), "heading/digest must be preserved"
    assert "never-compacted case log" in txt, "pointer to the case log must be added"
    # Task 376 #1: the compaction email is stamped with the precinct
    assert _EMAILS and _EMAILS[-1][1].get("precinct") == d, _EMAILS[-1]


def test_crossing_A_uses_api_by_default():
    # Phase C: with the default llm=True, crossing A makes an API call and uses its
    # output VERBATIM; the resolved global model reaches the argv and the big-picture
    # context (precinct list + a TARGET marker) reaches the prompt.
    d = "_sh_api"
    before = _big_ledger(d, head="HEAD api digest.")
    cfg = dict(sh._cfg())                                   # llm stays True (Phase-C default)
    cfg["A"], cfg["B"] = 100, 50
    assert before > cfg["A"]
    compacted = "HEAD api digest (API-compacted big picture).\n\nc19 latest kept."
    cap = {}
    with _capture_claude(stdout=compacted, cap=cap):
        did, out_tok = sh.maybe_compact(d, cfg)
    assert did is True
    assert rec.ledger_read(d) == compacted, "the API output must be used verbatim"
    assert "--model" in cap["argv"] and cfg["model"] in cap["argv"], cap["argv"]
    assert "PRECINCTS (" in cap["input"], "the precinct list (big-picture context) must be passed"
    assert d in cap["input"] and "TARGET" in cap["input"], "the target must be marked in the context"


def test_crossing_A_llm_failure_leaves_ledger_unchanged():
    # Case 400a WAIT-AND-RETRY: an API failure (rc!=0) with llm=True must NOT truncate
    # mechanically. The ledger is left byte-for-byte UNCHANGED (still over A, a brief
    # overage by design), no compaction email is sent, and a per-precinct backoff is
    # armed so a LATER pass retries a clean LLM compaction.
    d = "_sh_apifail"
    sh._COMPACT_BACKOFF.pop(d, None)
    before_tok = _big_ledger(d)
    before_txt = rec.ledger_read(d)
    cfg = dict(sh._cfg())                                   # llm stays True (Phase-C default)
    cfg["A"], cfg["B"] = 100, 50
    assert before_tok > cfg["A"], "fixture must cross the soft limit"
    n0 = len(_EMAILS)
    with _capture_claude(stdout="", stderr="boom", returncode=1):
        did, out_tok = sh.maybe_compact(d, cfg)
    assert did is False, "a failed LLM compaction must NOT change the ledger"
    assert out_tok == before_tok and out_tok > cfg["A"], "ledger stays over A (accepted overage)"
    assert rec.ledger_read(d) == before_txt, "the ledger must be left byte-for-byte UNCHANGED"
    assert len(_EMAILS) == n0, "no compaction email when nothing was compacted"
    assert d in sh._COMPACT_BACKOFF, "a per-precinct backoff must be armed for the retry"
    sh._COMPACT_BACKOFF.pop(d, None)


def test_crossing_A_usage_limit_leaves_ledger_unchanged():
    # A usage LIMIT likewise waits-and-retries: ledger UNCHANGED + backoff armed (never
    # a mechanical truncation, never a crash, never left unbounded permanently).
    d = "_sh_limit"
    sh._COMPACT_BACKOFF.pop(d, None)
    before_tok = _big_ledger(d)
    before_txt = rec.ledger_read(d)
    cfg = dict(sh._cfg())
    cfg["A"], cfg["B"] = 100, 50
    with _capture_claude(stdout="", stderr="Claude usage limit reached. resets 3pm", returncode=1):
        did, out_tok = sh.maybe_compact(d, cfg)
    assert did is False and out_tok == before_tok, "usage limit -> ledger unchanged, still over A"
    assert rec.ledger_read(d) == before_txt
    assert d in sh._COMPACT_BACKOFF
    sh._COMPACT_BACKOFF.pop(d, None)


def test_llm_failure_then_success_retries_and_compacts():
    # Case 400a: the failed attempt is RETRIED on a later pass. Pass 1 (API down) leaves
    # the ledger untouched + arms the backoff; while still inside the backoff window a
    # pass makes ZERO claude calls and does nothing; once it elapses a working LLM
    # compaction on a later pass finally shrinks the ledger. Proves wait-and-retry
    # (not one-shot give-up) and that the backoff clears on a clean success.
    d = "_sh_retry"
    sh._COMPACT_BACKOFF.pop(d, None)
    before_tok = _big_ledger(d, head="HEAD retry digest.")
    before_txt = rec.ledger_read(d)
    cfg = dict(sh._cfg())
    cfg["A"], cfg["B"] = 100, 50
    # pass 1: API down -> unchanged + backoff armed
    with _capture_claude(stdout="", stderr="boom", returncode=1):
        did1, tok1 = sh.maybe_compact(d, cfg)
    assert did1 is False and tok1 == before_tok and rec.ledger_read(d) == before_txt
    assert d in sh._COMPACT_BACKOFF
    # still inside the backoff window -> the retry is skipped WITHOUT any claude call
    with _no_claude():
        did_bo, tok_bo = sh.maybe_compact(d, cfg)
    assert did_bo is False and tok_bo == before_tok, "backoff window skips the retry, zero-API"
    assert rec.ledger_read(d) == before_txt
    # backoff elapses (simulate) -> a later pass with a working LLM compaction succeeds
    sh._COMPACT_BACKOFF.pop(d, None)
    good = "HEAD retry digest (clean API compaction).\n\nc19 latest kept."
    with _capture_claude(stdout=good):
        did2, out_tok2 = sh.maybe_compact(d, cfg)
    assert did2 is True and rec.ledger_read(d) == good, "the later clean compaction must apply"
    assert d not in sh._COMPACT_BACKOFF, "a clean success clears the backoff"


def test_api_output_with_limit_words_is_not_a_false_limit():
    # REGRESSION (live bug): a SUCCESSFUL compaction whose OUTPUT (stdout) contains
    # limit-like words ('session', 'reset') must be USED, not misclassified as a usage
    # limit. Genuine limit notices come on STDERR; stdout is arbitrary ledger content.
    d = "_sh_falsepos"
    before = _big_ledger(d)
    cfg = dict(sh._cfg())
    cfg["A"], cfg["B"] = 100, 50
    tricky = ("HEAD digest: the session-limit reset policy and weekly limit reset are "
              "documented here.\n\nc19 latest kept.")   # words that fooled the old parser
    with _capture_claude(stdout=tricky, stderr="", returncode=0):
        did, out_tok = sh.maybe_compact(d, cfg)
    assert did is True
    assert rec.ledger_read(d) == tricky, "a valid API compaction must be used verbatim"


def test_sheriff_decide_valid_decision_with_limit_words_not_deferred():
    # REGRESSION: a valid rc=0 decision whose REASON mentions 'limit'/'reset' must be
    # returned, not deferred (the limit check scans stderr only, and success wins first).
    req = {"id": "t3", "op": "ledger_modify", "precinct": "infra", "deputy": "depR", "reason": "x"}
    out = '{"decision":"deny","reason":"would reset history and hide a real case near a limit"}'
    with _fake_claude(stdout=out, returncode=0):
        dec = sh.sheriff_decide(req, cfg, retries=0)
    assert dec == {"decision": "deny", "reason": "would reset history and hide a real case near a limit"}


def test_one_pass_monitoring_makes_zero_api():
    # a full monitoring pass over a below-limit ledger must invoke NO claude
    rec.directory_register("_sh_mon", ledger_mode="mutable")
    rec.ledger_append("_sh_mon", "a small ledger, well below A.")
    with _no_claude():
        sh.one_pass(sh._cfg())


# ---------------------------------------------------------------------------
# Case 384a: the deputy->sheriff request queue (decisions mock the claude call)
# ---------------------------------------------------------------------------
def test_request_pass_empty_is_zero_api():
    # a request pass with NO pending requests must construct NO claude call
    _clear_pending()
    with _no_claude():
        assert sh.request_pass(cfg) == 0


def test_request_forged_identity_denied_zero_api():
    # a forged (deputy, session) is rejected BEFORE any claude call (identity gate)
    d = "_rq_fg"
    rec.directory_register(d, ledger_mode="mutable")
    _clear_pending()
    r = sreq.submit("log_remove", d, "depR", "WRONG-SESSION", "sneak a retract", target="1")
    n0 = len(_NOTIFIES)
    with _no_claude():                                  # must not reach sheriff_decide
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "denied"
    assert "identity check failed" in sreq.get(r["id"])["denied_reason"]
    assert len(_NOTIFIES) > n0, "a denied deputy must be notified"


def test_request_approve_log_remove_performs_and_journals():
    d = "_rq_lr"
    rec.directory_register(d, ledger_mode="mutable")
    rec.log_append(d, "900500", "a test case to retract", deputy="depR")
    assert "900500" in rec.log_read(d)
    _clear_pending()
    r, _ = _submit_pending("log_remove", d, target="900500", reason="retract a genuine test entry")
    orig = sh.sheriff_decide
    sh.sheriff_decide = lambda req, cfg, **k: {"decision": "approve", "reason": "genuine test entry"}
    try:
        sh.request_pass(cfg)
    finally:
        sh.sheriff_decide = orig
    assert sreq.status(r["id"])["state"] == "done"
    assert "900500" not in rec.log_read(d), "the case-log line must be retracted"
    assert sreq.get(r["id"])["result"]["removed_lines"] == 1
    jr = sreq.journal_read(d)
    assert "log_remove" in jr and "approve" in jr, "the op must be journaled"


def test_request_deny_notifies_and_journals():
    d = "_rq_dn"
    rec.directory_register(d, ledger_mode="mutable")
    _clear_pending()
    r, _ = _submit_pending("ledger_modify", d, reason="drop everything", new_content="")
    n0 = len(_NOTIFIES)
    orig = sh.sheriff_decide
    sh.sheriff_decide = lambda req, cfg, **k: {"decision": "deny", "reason": "would destroy real work"}
    try:
        sh.request_pass(cfg)
    finally:
        sh.sheriff_decide = orig
    assert sreq.status(r["id"])["state"] == "denied"
    assert sreq.get(r["id"])["denied_reason"] == "would destroy real work"
    assert len(_NOTIFIES) > n0
    assert "deny" in sreq.journal_read(d)


def test_request_defer_stays_pending():
    d = "_rq_df"
    rec.directory_register(d, ledger_mode="mutable")
    _clear_pending()
    r, _ = _submit_pending("case_number", d, reason="need a number")
    orig = sh.sheriff_decide
    sh.sheriff_decide = lambda req, cfg, **k: None          # api failure/limit -> defer
    try:
        sh.request_pass(cfg)
    finally:
        sh.sheriff_decide = orig
    assert sreq.status(r["id"])["state"] == "pending", "a deferred decision must leave it pending"
    _clear_pending()                                        # don't leak a valid pending req


def test_request_case_number_allocates_and_sets_active():
    import scratch_deputy_state as ds
    d = "_rq_cn"
    rec.directory_register(d, ledger_mode="mutable")
    _clear_pending()
    r, _ = _submit_pending("case_number", d, reason="take a follow-up",
                           case_description="a new taken case")
    orig = sh.sheriff_decide
    sh.sheriff_decide = lambda req, cfg, **k: {"decision": "approve", "reason": "allocate a case number"}
    try:
        sh.request_pass(cfg)
    finally:
        sh.sheriff_decide = orig
    assert sreq.status(r["id"])["state"] == "done"
    num = sreq.get(r["id"])["result"]["case"]
    assert isinstance(num, int) and num >= 900000
    e = ds.get("depR")
    assert e and str(e["case"]) == str(num) and e["precinct"] == d, e


# ---------------------------------------------------------------------------
# Case 384d / Phase D: precinct create/delete (interlock + emailed YES + trash)
# ---------------------------------------------------------------------------
def test_request_precinct_create_registers():
    _clear_pending()
    name = "_pd_create"
    r, _ = _submit_pending("precinct_create", "", target=name,
                           reason="user wants a new precinct", description="a phase-D new precinct")
    with _approving("clearly needed"):
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "done"
    assert name in rec.directory_read()["precincts"], "an approved create must register the precinct"
    assert rec.precinct_status(name) == "active"


def test_delete_clean_precinct_initiates_confirmation_not_delete():
    # approve + NO active deputies + NO cases -> awaiting the user's YES; NOTHING is
    # deleted yet, the precinct stays active, and the user is emailed a token.
    _clear_pending(); _clear_confirm()
    d = "_pd_clean"
    rec.directory_register(d, ledger_mode="mutable")
    n0 = len(_EMAILS)
    r, _ = _submit_pending("precinct_delete", "", target=d, reason="cleanup, no work here")
    # decision is mocked (approve), so perform_op + begin_confirmation are the only
    # code that runs -- and both are ZERO-API (the guard proves no claude is shelled).
    with _approving("ok to remove an empty precinct"), _no_claude():
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "done"
    res = sreq.get(r["id"])["result"]
    assert res["status"] == "awaiting_confirmation" and res["token"]
    assert rec.precinct_status(d) == "active", "must NOT delete before the user's YES"
    assert d in rec.directory_read()["precincts"]
    assert sreq.read_awaiting(res["token"]) is not None, "an awaiting record must be parked"
    # the user got a tokenized confirm email
    sent = [e for e in _EMAILS[n0:] if f"sheriff-confirm:{res['token']}" in e[0][0]]
    assert sent, "the sheriff must email the user a tokenized YES request"
    _clear_confirm()


def test_delete_interlock_blocks_active_deputies_unless_force():
    import scratch_deputy_state as ds
    _clear_pending(); _clear_confirm()
    d = "_pd_active"
    rec.directory_register(d, ledger_mode="mutable")
    ds.set_state("_idep", case="1", precinct=d)         # a live deputy on this precinct
    try:
        r, _ = _submit_pending("precinct_delete", "", target=d, reason="delete despite work")
        with _approving():
            sh.request_pass(cfg)
        assert sreq.status(r["id"])["state"] == "denied", "active deputies must block delete"
        assert "interlock" in sreq.get(r["id"])["denied_reason"]
        assert rec.precinct_status(d) == "active", "nothing deleted when interlock blocks"
        # force bypasses the interlock -> now it awaits the YES (still not deleted)
        r2, _ = _submit_pending("precinct_delete", "", target=d, reason="force delete", force=True)
        with _approving():
            sh.request_pass(cfg)
        assert sreq.status(r2["id"])["state"] == "done"
        assert sreq.get(r2["id"])["result"]["status"] == "awaiting_confirmation"
        assert rec.precinct_status(d) == "active", "force still needs the emailed YES"
    finally:
        ds.clear("_idep")
        _clear_confirm()


def test_delete_interlock_blocks_nonempty_cases():
    _clear_pending(); _clear_confirm()
    d = "_pd_cases"
    rec.directory_register(d, ledger_mode="mutable")
    rec.log_append(d, "700", "a real closed case", deputy="depR")   # non-empty cases
    r, _ = _submit_pending("precinct_delete", "", target=d, reason="delete with cases")
    with _approving():
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "denied"
    assert "non-empty cases" in sreq.get(r["id"])["denied_reason"]
    assert rec.precinct_status(d) == "active"
    _clear_confirm()


def test_delete_confirm_yes_soft_deletes_to_trash():
    _clear_pending(); _clear_confirm()
    d = "_pd_yes"
    rec.directory_register(d, ledger_mode="mutable")
    r, _ = _submit_pending("precinct_delete", "", target=d, reason="empty; ok to delete")
    with _approving():
        sh.request_pass(cfg)
    token = _token_of(r["id"])
    assert rec.precinct_status(d) == "active"
    # user replies YES -> router drops a confirmation -> confirm_pass soft-deletes
    sreq.record_confirmation(token, "yes")
    with _no_claude():                                  # confirm_pass is ZERO-API
        acted = sh.confirm_pass(cfg)
    assert acted == 1
    assert rec.precinct_status(d) == "deleted", "a YES must soft-delete"
    assert d not in rec.directory_read()["precincts"], "deleted -> dropped from the active list"
    assert d in rec.directory_read(include_deleted=True)["precincts"], "still recoverable on disk"
    assert sreq.read_awaiting(token) is None, "awaiting record cleared after acting"
    # the delete saga is journaled in the SYSTEM lifecycle log (survives the trash move)
    assert "delete-confirmed" in sreq.lifecycle_journal_read()
    _clear_confirm()
    # restore it so later tests / the shared root stay clean
    rec.precinct_restore(d, role="sheriff")


def test_delete_no_reply_no_delete_then_timeout_cancels():
    _clear_pending(); _clear_confirm()
    d = "_pd_no"
    rec.directory_register(d, ledger_mode="mutable")
    r, _ = _submit_pending("precinct_delete", "", target=d, reason="maybe delete")
    with _approving():
        sh.request_pass(cfg)
    token = _token_of(r["id"])
    # no confirmation yet + still within the window -> confirm_pass does nothing
    with _no_claude():
        assert sh.confirm_pass(cfg) == 0
    assert rec.precinct_status(d) == "active", "no YES -> no delete"
    assert sreq.read_awaiting(token) is not None
    # force the timeout: rewrite the awaiting record's expires_ts into the past
    a = sreq.read_awaiting(token); a["expires_ts"] = 1.0; sreq.write_awaiting(token, a)
    with _no_claude():
        acted = sh.confirm_pass(cfg)
    assert acted == 1
    assert rec.precinct_status(d) == "active", "timeout auto-cancels; still not deleted"
    assert sreq.read_awaiting(token) is None, "the timed-out awaiting record is cleared"
    assert "delete-cancelled" in sreq.lifecycle_journal_read()
    _clear_confirm()


def test_delete_confirm_no_cancels():
    _clear_pending(); _clear_confirm()
    d = "_pd_declined"
    rec.directory_register(d, ledger_mode="mutable")
    r, _ = _submit_pending("precinct_delete", "", target=d, reason="on second thought")
    with _approving():
        sh.request_pass(cfg)
    token = _token_of(r["id"])
    sreq.record_confirmation(token, "no")
    with _no_claude():
        sh.confirm_pass(cfg)
    assert rec.precinct_status(d) == "active", "a NO must never delete"
    assert sreq.read_awaiting(token) is None
    _clear_confirm()


def test_precinct_restore_via_request():
    _clear_pending(); _clear_confirm()
    d = "_pd_restore"
    rec.directory_register(d, ledger_mode="mutable")
    rec.precinct_soft_delete(d, role="sheriff")
    assert rec.precinct_status(d) == "deleted"
    r, _ = _submit_pending("precinct_restore", "", target=d, reason="undo the delete")
    with _approving("restore within retention"):
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "done"
    assert rec.precinct_status(d) == "active", "restore must bring it back to active"
    assert d in rec.directory_read()["precincts"]


def test_purge_pass_hard_deletes_after_retention():
    _clear_pending(); _clear_confirm()
    d = "_pd_purge"
    rec.directory_register(d, ledger_mode="mutable")
    # soft-delete with an ALREADY-elapsed retention (retention_days=0 -> purge_after=now)
    rec.precinct_soft_delete(d, role="sheriff", retention_days=0)
    assert rec.precinct_status(d) == "deleted"
    trash = Path(_TEST_ROOT) / ".trash"
    assert any(p.name.startswith(d + "@") for p in trash.iterdir()), "records must be in trash"
    with _no_claude():                                  # purge_pass is ZERO-API
        n = sh.purge_pass(cfg)
    assert n >= 1
    assert rec.precinct_status(d) is None, "purge drops the directory entry entirely"
    assert not any(p.name.startswith(d + "@") for p in trash.iterdir()), "trash hard-deleted"


def test_receptionist_delete_handoff_is_authorized_without_session():
    # a receptionist hand-off (no deputy session) for a lifecycle op is authorized by
    # an allowed requester email -- the user's mailbox is the real authority.
    _clear_pending(); _clear_confirm()
    d = "_pd_recept"
    rec.directory_register(d, ledger_mode="mutable")
    r = sreq.submit("precinct_delete", "", "", "", "user asked to delete", target=d,
                    origin="receptionist", requester="stevenfd@cmu.edu")
    with _approving():
        sh.request_pass(cfg)
    assert sreq.status(r["id"])["state"] == "done"
    assert sreq.get(r["id"])["result"]["status"] == "awaiting_confirmation"
    # and a FORGED hand-off (bad requester) is rejected at submit time
    try:
        sreq.submit("precinct_delete", "", "", "", "sneak", target=d,
                    origin="receptionist", requester="evil@example.com")
        assert False, "a non-allowed requester must be refused at submit"
    except ValueError:
        pass
    _clear_confirm()


def test_confirm_and_purge_passes_zero_api_when_idle():
    # with nothing awaiting and nothing past retention, both new passes are zero-API
    _clear_pending(); _clear_confirm()
    with _no_claude():
        assert sh.confirm_pass(cfg) == 0
        assert sh.purge_pass(cfg) == 0


def test_sheriff_decide_parses_approve_and_deny():
    req = {"id": "t1", "op": "log_remove", "precinct": "infra", "target": "1",
           "deputy": "depR", "reason": "x"}
    with _fake_claude(stdout='{"decision":"approve","reason":"ok"}'):
        assert sh.sheriff_decide(req, cfg, retries=0) == {"decision": "approve", "reason": "ok"}
    with _fake_claude(stdout='sure -> {"decision":"deny","reason":"no"} done'):
        dec = sh.sheriff_decide(req, cfg, retries=0)
    assert dec["decision"] == "deny" and dec["reason"] == "no"


def test_sheriff_decide_defers_on_limit_and_garbage():
    req = {"id": "t2", "op": "case_number", "precinct": "infra", "deputy": "depR", "reason": "x"}
    with _fake_claude(stdout="", stderr="Claude usage limit reached. resets 3pm", returncode=1):
        assert sh.sheriff_decide(req, cfg, retries=0) is None       # limit -> DEFER
    with _fake_claude(stdout="i cannot output json"):
        assert sh.sheriff_decide(req, cfg, retries=1) is None       # unparseable -> DEFER


def test_sheriff_global_model_config_and_env():
    # Phase C: the GLOBAL sheriff model resolves env override > persisted config > fable,
    # is validated + sheriff-only to set, and _cfg() reads it. Cleans up so the later
    # test_thresholds_are_tokens_A_gt_B sees the fable default again.
    import scratch_records as rec2
    cfgpath = Path(_TEST_ROOT) / "sheriff_config.json"
    os.environ.pop("TSOMP_SHERIFF_MODEL", None)
    cfgpath.unlink(missing_ok=True)
    assert sh._sheriff_model() == "fable" and sh._cfg()["model"] == "fable"
    # persisted config via the records helper -> _cfg() + _sheriff_model() read it
    rec2.sheriff_model_set("opus", role="sheriff")
    assert rec2.sheriff_model_get() == "opus"
    assert sh._sheriff_model() == "opus" and sh._cfg()["model"] == "opus"
    # an explicit env override beats the persisted config
    os.environ["TSOMP_SHERIFF_MODEL"] = "sonnet"
    assert sh._sheriff_model() == "sonnet"
    os.environ.pop("TSOMP_SHERIFF_MODEL", None)
    # sheriff-only + validated
    try:
        rec2.sheriff_model_set("opus", role="deputy"); assert False, "role guard missing"
    except PermissionError:
        pass
    try:
        rec2.sheriff_model_set("gpt-9", role="sheriff"); assert False, "model validation missing"
    except ValueError:
        pass
    cfgpath.unlink(missing_ok=True)                         # restore the default for later tests


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_sheriff test suite  ({len(tests)} tests) ===")
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
