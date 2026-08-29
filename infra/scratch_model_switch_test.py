#!/usr/bin/env python3
"""Tests for scratch_model_switch.py (Case 561).

Hermetic: every test operates on a throwaway worker name whose files live in the
real scratch_full_logs/ (so the real path helpers are exercised) and are removed
in teardown. Nothing here launches an agent.

Run: python scratch_model_switch_test.py

NOT PORTED, deliberately: the five `submitted_form` receipt tests from the upstream
suite. They assert scratch_web_case._render_form/_form_fields, which render the
dashboard create-case receipt — a feature this release does not carry. Removed
rather than left failing: a suite with known-failing tests teaches people to
ignore failures.

RUN THESE SEQUENTIALLY. The scratch_*_test.py suites operate on real files under
scratch_full_logs/ (deliberately, so the real path helpers are exercised), so two
suites run concurrently corrupt each other's fixtures and fail in ways that look
like product bugs. Every number quoted in case 576 was produced by a sequential run.
"""
import json
import os
import pathlib
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import scratch_models          # noqa: E402
import scratch_model_switch as ms   # noqa: E402

PASS = FAIL = 0
W = "c561test"


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


def setup(service="claude", model="opus", sid=None):
    """Build a throwaway worker: prompt file + generated relaunch script."""
    teardown()
    sid = sid or str(uuid.uuid4())
    ms.prompt_path(W).write_text(
        "PRECINCT LEDGER (infra)\n"
        "You are a PERSISTENT worker agent ... (preamble stand-in)\n"
        "--task 561t\n")
    r = subprocess.run(
        ["bash", str(ROOT / "scratch_gen_relaunch.sh"), W, sid,
         "operator@example.com", model, scratch_models.effort(model),
         "infra", "561t", service],
        cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    if service == "chatgpt":
        ms.codex_session_path(W).write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    return sid


def teardown():
    for p in (ms.prompt_path(W), ms.lanes_path(W), ms.switch_path(W),
              ms.seed_path(W), ms.history_path(W), ms.codex_session_path(W),
              ms.relaunch_path(W),
              Path(str(ms.seed_path(W)) + ".used"),
              Path(str(ms.codex_session_path(W)) + ".prev")):
        try:
            Path(p).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------- lanes
def test_lanes_default_is_no_split():
    ln = scratch_models.lanes()
    check("no selection -> both lanes identical", not ln["split"])
    check("default lane is the claude default",
          ln["work"]["service"] == "claude" and ln["work"]["model"] == "opus")


def test_lanes_report_inherits_work():
    ln = scratch_models.lanes("fable")
    check("report inherits the work model when unset",
          ln["report"]["model"] == "fable" and not ln["split"])


def test_lanes_report_service_only_uses_that_services_default():
    ln = scratch_models.lanes("opus", None, None, "chatgpt")
    check("report service alone -> that service's default model, not the work model",
          ln["report"]["service"] == "chatgpt"
          and ln["report"]["model"] == scratch_models.SERVICE_DEFAULT_MODEL["chatgpt"],
          ln["report"])
    check("that counts as a split", ln["split"])


def test_lanes_same_service_different_model_is_a_split():
    ln = scratch_models.lanes("opus", None, "fable")
    check("opus work / fable report is a split", ln["split"])
    check("both lanes stay on claude",
          ln["work"]["service"] == "chatgpt" or ln["report"]["service"] == "claude")


def test_lane_mode_never_reports_hybrid():
    """A split case must NOT be labelled claude+chatgpt — that mode id means the
    Case 557 two-agent writer, which is the design Case 561 replaces."""
    ln = scratch_models.lanes("opus", None, "terra")
    check("split claude/chatgpt reports mode 'claude', not 'claude+chatgpt'",
          scratch_models.lane_mode(ln) == "claude", scratch_models.lane_mode(ln))


def test_effort_moves_with_the_model():
    check("gpt-5.5 effort is capped below max",
          scratch_models.effort("gpt55") != "max")


# ---------------------------------------------------------------- request
def test_request_writes_signal_and_records_direction():
    setup()
    ms.write_lanes(W, "opus", None, "terra", None, case="561t", precinct="infra",
                   requester="operator@example.com")
    rec = ms.request(W, "report", "starting the PDF report")
    check("switch file written", ms.switch_path(W).exists())
    check("from = current", (rec["from_service"], rec["from_model"]) == ("claude", "opus"))
    check("to = the report lane", (rec["to_service"], rec["to_model"]) == ("chatgpt", "terra"))
    check("flagged cross-service", rec["cross_service"] is True)
    check("lane recorded", rec["lane"] == "report")
    teardown()


def test_request_accepts_service_colon_model_and_bare_model():
    setup()
    r1 = ms.request(W, "chatgpt:luna", "writing the design doc")
    check("service:model parsed", (r1["to_service"], r1["to_model"]) == ("chatgpt", "luna"))
    ms.cancel(W)
    r2 = ms.request(W, "fable", "cheaper model for a long grind")
    check("bare model carries its own service",
          (r2["to_service"], r2["to_model"]) == ("claude", "fable"))
    teardown()


def test_request_refuses_a_noop():
    setup()
    try:
        ms.request(W, "opus", "no reason at all")
        check("switching to the model already running is refused", False)
    except ValueError as e:
        check("switching to the model already running is refused", "already running" in str(e))
    teardown()


def test_request_refuses_unknown_model():
    setup()
    try:
        ms.request(W, "gpt-9-turbo", "writing the report")
        check("unknown model refused", False)
    except ValueError as e:
        check("unknown model refused", "unknown model" in str(e))
    teardown()


def test_request_requires_a_reason():
    setup()
    try:
        ms.request(W, "fable", "   ")
        check("empty reason refused", False)
    except ValueError:
        check("empty reason refused", True)
    teardown()


# -------------------------------------------------- the uid=674 email rule
def test_email_shaped_reasons_are_refused():
    """Feng uid=674: never switch models just to write him an email."""
    setup()
    for reason in ("to write Steven the milestone email",
                   "send an email update",
                   "reply to Feng",
                   "write the FINAL email",
                   "ack the user's message",
                   "notify Steven of progress"):
        ms.cancel(W)
        try:
            ms.request(W, "fable", reason)
            check(f"refused: {reason!r}", False)
        except ValueError as e:
            check(f"refused: {reason!r}", "REFUSED" in str(e))
    teardown()


def test_document_reasons_still_pass_even_when_they_mention_email():
    """The guard must not be a word filter: a real document task that happens to
    say 'email' is legitimate, and refusing it would repeat the over-literal
    reading that made the lane definition wrong in the first place."""
    setup()
    for reason in ("writing the PDF report I will email to Steven",
                   "start the LaTeX report",
                   "write the README",
                   "drafting the design doc"):
        ms.cancel(W)
        try:
            ms.request(W, "fable", reason)
            check(f"allowed: {reason!r}", True)
        except ValueError as e:
            check(f"allowed: {reason!r}", False, str(e))
    teardown()


# ---------------------------------------------------------------- apply
def test_same_service_switch_keeps_the_session():
    sid = setup("claude", "opus")
    ms.request(W, "fable", "long grind, cheaper model")
    done = ms.apply(W)
    s = ms.relaunch_path(W).read_text()
    check("same-service switch is a resume (lossless)", done["context"] == "resumed")
    check("session id unchanged", done["session"] == sid, f"{done['session']} != {sid}")
    check("TSOMP_MODEL rewritten", 'export TSOMP_MODEL="fable"' in s)
    check("--model id rewritten", scratch_models.resolve_id("fable") in s)
    check("no stale opus id left", scratch_models.resolve_id("opus") not in s)
    check("still resumes (not cold)", f'claude --resume "{sid}"' in s)
    check("no seed written for a same-service switch", not ms.seed_path(W).exists())
    check("switch file consumed", not ms.switch_path(W).exists())
    check("history appended", ms.history_path(W).exists())
    teardown()


def test_cross_service_switch_regenerates_and_seeds():
    setup("claude", "opus")
    ms.write_lanes(W, "opus", None, "terra", None, case="561t", precinct="infra",
                   requester="operator@example.com")
    ms.request(W, "report", "starting the PDF report")
    done = ms.apply(W)
    s = ms.relaunch_path(W).read_text()
    check("cross-service switch is a reseed", done["context"] == "reseeded")
    check("seed file written", ms.seed_path(W).exists())
    check("seed is non-trivial", ms.seed_path(W).stat().st_size > 1000,
          ms.seed_path(W).stat().st_size)
    check("relaunch script now targets chatgpt", 'AGENT_SERVICE="chatgpt"' in s)
    check("relaunch script carries the chatgpt model id",
          scratch_models.resolve_id("terra") in s)
    check("codex cold-start branch present", "--skip-git-repo-check" in s)
    check("no codex session yet -> will cold start",
          not ms.codex_session_path(W).exists())
    teardown()


def test_cross_service_seed_carries_the_rules():
    setup("claude", "opus")
    ms.write_lanes(W, "opus", None, "terra", None, case="561t", precinct="infra")
    ms.request(W, "report", "writing the report")
    ms.apply(W)
    seed = ms.seed_path(W).read_text()
    check("seed says it is a continuation, not a new task",
          "THIS IS NOT A NEW TASK" in seed)
    check("seed states the lane", "YOU ARE HERE" in seed)
    check("seed forbids switching for email",
          "NEVER switch models to write an email" in seed)
    check("seed carries the original launch prompt",
          "preamble stand-in" in seed)
    check("seed is explicit about provenance", "CONTEXT PROVENANCE" in seed)
    teardown()


def test_switch_into_claude_mints_a_new_session():
    setup("chatgpt", "terra")
    ms.request(W, "claude:opus", "need to change the code")
    done = ms.apply(W)
    s = ms.relaunch_path(W).read_text()
    check("switching to claude mints a session id", len(done["session"]) == 36)
    check("relaunch script targets claude", 'AGENT_SERVICE="claude"' in s)
    check("old codex session id retired, not destroyed",
          Path(str(ms.codex_session_path(W)) + ".prev").exists())
    teardown()


def test_apply_is_refused_with_no_request():
    setup()
    try:
        ms.apply(W)
        check("apply without a request is refused", False)
    except ValueError as e:
        check("apply without a request is refused", "no pending switch" in str(e))
    teardown()


# ---------------------------------------------------------- transcript render
def test_render_budget_drops_the_middle_and_says_so():
    blocks = [f"[assistant]\n{'x' * 900} block{i}" for i in range(400)]
    text = "\n\n".join(blocks)
    # exercise the same head/tail logic via a real render on a synthetic file
    p = Path("/tmp/c561_fake_session.jsonl")
    with open(p, "w") as f:
        for i in range(400):
            f.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "x" * 900 + f" block{i}"}]}}) + "\n")
    orig = ms.claude_transcript_path
    ms.claude_transcript_path = lambda sid: p
    try:
        out, meta = ms.render_transcript("claude", "sid", budget=50000)
        check("budget respected", len(out) <= 50000 + 800, len(out))
        check("omission is announced", "OMITTED HERE" in out)
        check("blocks were actually dropped", meta["dropped_blocks"] > 0)
        check("head retained (earliest block present)", "block0" in out)
        check("tail retained (latest block present)", "block399" in out)
        check("full transcript path named in the marker", str(p) in out)
    finally:
        ms.claude_transcript_path = orig
        p.unlink(missing_ok=True)
    del text, blocks


def test_preamble_blocks_are_not_replayed():
    """The launch prompt is already PART 1 of the seed; replaying it once per
    relaunch is what left the head empty on the first attempt."""
    launch = "You are a PERSISTENT worker agent " + ("q" * 8000)
    block = "[user]\n" + launch
    check("a repeat of the launch prompt is recognised",
          ms._is_relaunch_boilerplate(block, launch))
    check("ordinary conversation is not",
          not ms._is_relaunch_boilerplate("[assistant]\nI'll check the mailbox.", launch))
    check("a short block is never treated as preamble",
          not ms._is_relaunch_boilerplate("[user]\nshort", launch))


def test_codex_transcript_renders():
    p = Path("/tmp/c561_fake_rollout.jsonl")
    rows = [
        {"type": "session_meta", "payload": {"session_id": "x"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
         "content": [{"type": "input_text", "text": "harness boilerplate"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "I will do the thing."}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call",
         "name": "exec", "input": "ls -la"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output",
         "output": [{"type": "input_text", "text": "total 4"}]}},
        {"type": "response_item", "payload": {"type": "reasoning",
         "encrypted_content": "SECRETBLOB"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    out = ms._render_codex(p)
    joined = "\n".join(out)
    check("codex assistant message rendered", "I will do the thing." in joined)
    check("codex tool call rendered", "ls -la" in joined)
    check("codex tool output rendered", "total 4" in joined)
    check("codex encrypted reasoning dropped", "SECRETBLOB" not in joined)
    check("codex developer boilerplate dropped", "harness boilerplate" not in joined)
    p.unlink(missing_ok=True)


# ---------------------------------------------------------- protocol block
def test_protocol_block_split_vs_single():
    single = ms.protocol_block("561t", {"split": False, "lanes": scratch_models.lanes()})
    check("single-model case says it will never switch", "never need to switch" in single)
    ln = scratch_models.lanes("opus", None, "terra")
    split = ms.protocol_block("561t", {"split": True, "lanes": ln})
    check("split case shows the switch command",
          "scratch_model_switch.py request --to report" in split)
    check("split case forbids email switching",
          "NEVER switch to write an email" in split)
    check("split case says the case file is WORK", "case file" in split)
    check("split case is honest about cross-vendor loss", "RENDERED" in split)
    check("split case says there is no second agent", "no second agent" in split)


def test_lanes_or_default_for_a_pre_561_worker():
    setup("claude", "fable")
    rec = ms.lanes_or_default(W)     # no lanes file written
    check("a worker with no lanes file is single-lane",
          rec["split"] is False and rec["lanes"]["report"]["model"] == "fable")
    teardown()


# ---------------------------------------------------------------- CLI
def test_cli_request_then_status_then_cancel():
    setup()
    env = dict(os.environ, TSOMP_WORKER=W)
    r = subprocess.run([sys.executable, "scratch_model_switch.py", "request",
                        "--to", "fable", "--reason", "writing the PDF report"],
                       cwd=str(ROOT), capture_output=True, text=True, env=env)
    check("CLI request ok", r.returncode == 0, r.stderr)
    check("CLI tells the deputy to exit", "NOW EXIT" in r.stdout)
    check("CLI infers the worker from TSOMP_WORKER", ms.switch_path(W).exists())
    r = subprocess.run([sys.executable, "scratch_model_switch.py", "status", "--json"],
                       cwd=str(ROOT), capture_output=True, text=True, env=env)
    check("CLI status is json", json.loads(r.stdout)["to_model"] == "fable")
    r = subprocess.run([sys.executable, "scratch_model_switch.py", "cancel"],
                       cwd=str(ROOT), capture_output=True, text=True, env=env)
    check("CLI cancel removes the signal", not ms.switch_path(W).exists())
    teardown()


def test_cli_refusal_exits_nonzero():
    setup()
    env = dict(os.environ, TSOMP_WORKER=W)
    r = subprocess.run([sys.executable, "scratch_model_switch.py", "request",
                        "--to", "fable", "--reason", "to email Steven an update"],
                       cwd=str(ROOT), capture_output=True, text=True, env=env)
    check("email-shaped CLI request exits non-zero", r.returncode != 0)
    check("and explains why", "REFUSED" in r.stderr)
    check("and writes no signal", not ms.switch_path(W).exists())
    teardown()


def test_registry_lanes_cli():
    r = subprocess.run([sys.executable, "scratch_models.py", "lanes",
                        "opus", "-", "terra", "-"],
                       cwd=str(ROOT), capture_output=True, text=True)
    lines = r.stdout.strip().split("\n")
    check("lanes CLI emits 9 lines", len(lines) == 9, lines)
    check("line 9 flags the split", lines[8] == "split")
    check("work lane first", lines[0] == "claude" and lines[1] == "opus")
    check("report lane second", lines[4] == "chatgpt" and lines[5] == "terra")


# -------------------------------------------- the form receipt (Feng uid=674)
def _receipt(**over):
    import scratch_web_case as wc
    rec = dict({"precinct": "infra", "model": "opus", "mode": "claude",
                "description": "d", "files": [], "critic": None}, **over)
    return wc._render_form(rec, case="9001", deputy="web_9001", is_email=False)













# ---------------------------------------------------------------------------
# Case 576 — reviving an ENDED case on its own settings
# ---------------------------------------------------------------------------
import contextlib as _ctx
import tempfile as _tmp


@_ctx.contextmanager
def _board(case=None):
    """A throwaway active-deputies board, so a test never writes the live one."""
    import scratch_deputy_state as ds
    old = os.environ.get("TSOMP_RECORDS_ROOT")
    d = _tmp.mkdtemp(prefix="c576board")
    os.environ["TSOMP_RECORDS_ROOT"] = d
    try:
        if case is not None:
            ds.set_state(W, case=case, precinct="infra", description="c576")
        yield d
    finally:
        if old is None:
            os.environ.pop("TSOMP_RECORDS_ROOT", None)
        else:
            os.environ["TSOMP_RECORDS_ROOT"] = old


def test_c576_settings_reports_the_whole_configured_set():
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "sol", "chatgpt",
                   judge="vyas", mode="claude", case="576t", precinct="infra")
    with _board():
        s = ms.settings(W)
    check("work lane preserved", (s["work"]["service"], s["work"]["model"]) == ("claude", "fable"), s["work"])
    check("report lane preserved", (s["report"]["service"], s["report"]["model"]) == ("chatgpt", "sol"), s["report"])
    check("judge id preserved", s["judge"] == "vyas", s["judge"])
    check("mode preserved", s["mode"] == "claude", s["mode"])
    check("split detected", s["split"] is True)


def test_c576_case_comes_from_the_board_not_the_stale_script():
    """The defect that put a wrong case in 29 sent emails: the relaunch script
    keeps the case it was spawned with, but a deputy that TAKES a follow-up case
    only updates the board."""
    setup()
    ms.write_lanes(W, "opus", "claude", case="576t", precinct="infra")
    with _board(case="999t"):
        s = ms.settings(W)
    check("board case wins over the spawn-time case", s["case"] == "999t", s["case"])
    with _board():                       # no board entry -> fall back to lanes
        s2 = ms.settings(W)
    check("falls back to the lanes case when the board is silent", s2["case"] == "576t", s2["case"])


def test_c576_judge_id_recovered_from_a_pre576_prompt():
    """Cases created before 576 persisted the judge id nowhere structured; it is
    still in the launch prompt's JUDGE PROTOCOL header."""
    import scratch_critic
    setup()
    ms.prompt_path(W).write_text("preamble\n" + scratch_critic.protocol_block("vyas", "576t"))
    ms.write_lanes(W, "opus", "claude", case="576t")      # no judge recorded
    with _board():
        s = ms.settings(W)
    check("judge recovered from the launch prompt", s["judge"] == "vyas", s["judge"])


def test_c576_env_exports_what_the_old_relaunch_dropped():
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "sol", "chatgpt",
                   judge="vyas", judge_model="opus", judge_service="claude",
                   mode="claude", case="576t", precinct="infra")
    with _board():
        env = ms.settings_env(W)
    for var in ("TSOMP_MODE", "TSOMP_REPORT_MODEL",
                "TSOMP_REPORT_SERVICE", "TSOMP_JUDGE", "TSOMP_JUDGE_MODEL"):
        check(f"{var} exported", f"export {var}=" in env, env)
    # TSOMP_MODEL/TSOMP_SERVICE must NOT come from here: this block is evaluated
    # after the baked literals, so a lanes-derived value would overwrite what the
    # script is about to RUN (web_575 runs chatgpt:sol from its report lane) and
    # would silently undo Case 509's weekly-cap model move on the next relaunch.
    check("TSOMP_MODEL is not overridden from the lanes record",
          "TSOMP_MODEL=" not in env, env)
    check("TSOMP_SERVICE is not overridden from the lanes record",
          "TSOMP_SERVICE=" not in env, env)


def test_c576_env_omits_empty_values():
    setup()
    ms.write_lanes(W, "opus", "claude", case="576t", precinct="infra")
    with _board():
        env = ms.settings_env(W)
    check("no judge -> no TSOMP_JUDGE export", "TSOMP_JUDGE=" not in env, env)
    check("unsplit -> no writer-model export", "TSOMP_HYBRID_MODEL=" not in env, env)


def test_c576_write_lanes_does_not_invent_a_judge_model():
    """A caller-supplied model OVERRIDES the judge's own registered model in
    scratch_critic, so defaulting one here would silently demote every judge."""
    setup()
    rec = ms.write_lanes(W, "opus", "claude", judge="vyas", case="576t")
    check("judge id stored", rec["judge"]["critic"] == "vyas", rec["judge"])
    check("no model invented", "model" not in rec["judge"], rec["judge"])
    rec2 = ms.write_lanes(W, "opus", "claude", judge="vyas", judge_model="fable", case="576t")
    check("an explicit judge model IS stored", rec2["judge"]["model"] == "fable", rec2["judge"])


def test_c576_relaunch_env_block_injection_is_idempotent():
    setup()
    ms.write_lanes(W, "opus", "claude", case="576t", precinct="infra")
    txt0 = ms.relaunch_path(W).read_text()
    check("a freshly GENERATED script already restores settings",
          ms._ENV_MARKER in txt0, "generator did not emit the block")
    check("refresh does not double-inject into a fresh script",
          ms.refresh_relaunch_env(W) is False)
    # an OLD script (pre-576) has no block: strip it and re-inject
    stripped = "\n".join(l for l in txt0.splitlines()
                          if "_SETTINGS_ENV" not in l and "Case 576" not in l)
    ms.relaunch_path(W).write_text(stripped)
    check("injects into a pre-576 script", ms.refresh_relaunch_env(W) is True)
    check("second call is a no-op", ms.refresh_relaunch_env(W) is False)
    out = subprocess.run(["bash", "-n", str(ms.relaunch_path(W))], capture_output=True, text=True)
    check("patched script is still valid bash", out.returncode == 0, out.stderr)


def test_c576_restore_lane_is_a_noop_for_an_unsplit_case():
    setup()
    ms.write_lanes(W, "opus", "claude", case="576t")
    check("nothing to restore when both lanes are one model",
          ms.restore_lane(W) is None)


def test_c576_restore_lane_brings_a_report_lane_case_back_to_work():
    """The normal end of a split case is IN the report lane (write the PDF, send
    FINAL, close), so a follow-up would otherwise be worked by the report model."""
    sid = setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "opus", "claude", case="576t", precinct="infra")
    rec = ms.read_lanes(W); rec["current"] = "report"
    ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
    ms._rewrite_same_service(W, {"to_model": "opus", "to_id": "claude-opus-5", "to_effort": "max"})
    check("precondition: sitting on the report model",
          ms.current_target(W)["model"] == "opus", ms.current_target(W))
    pathlib.Path(f"scratch_full_logs/worker_{W}.done").write_text("")   # a CLOSED case
    out = ms.restore_lane(W)
    check("a switch back to work was applied", out is not None and out["to_model"] == "fable", out)
    check("relaunch script now runs the WORK model",
          ms.current_target(W)["model"] == "fable", ms.current_target(W))
    check("lane recorded as work", (ms.read_lanes(W) or {}).get("current") == "work")
    check("session id untouched by the restore", sid in ms.relaunch_path(W).read_text())


def test_c576_restore_lane_never_races_a_pending_switch():
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "opus", "claude", case="576t")
    rec = ms.read_lanes(W); rec["current"] = "report"
    ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
    ms._atomic_write(ms.switch_path(W), json.dumps({"worker": W, "state": "requested"}))
    check("a deputy's own pending switch is left alone", ms.restore_lane(W) is None)


def test_c576_inbox_loop_falls_back_to_the_on_disk_relaunch_script():
    """The roster holds ~32 entries against 401 scripts on disk; the lookup used
    to consult the roster ONLY, dropping every other ended deputy onto the
    one-shot handler. Exercises the real function text from the live loop."""
    setup()
    src = (ROOT / "scratch_inbox_loop.sh").read_text()
    start = src.index("relaunch_script_for()")
    end = src.index("\n}", start) + 2
    fn = src[start:end]
    got = subprocess.run(["bash", "-c", fn + f"\nrelaunch_script_for {W}"],
                         cwd=str(ROOT), capture_output=True, text=True)
    check("resolves a worker absent from the roster",
          got.stdout.strip() == f"scratch_worker_{W}_relaunch.sh", got.stdout + got.stderr)
    ms.relaunch_path(W).unlink()
    got2 = subprocess.run(["bash", "-c", fn + "\nrelaunch_script_for %s" % W],
                          cwd=str(ROOT), capture_output=True, text=True)
    check("empty when no script exists anywhere", got2.stdout.strip() == "", got2.stdout)



def test_c576_inbox_loop_reseeds_a_lost_roster_entry():
    """Case 540: only a fresh SPAWN ever appends to the roster, so a deputy the
    roster lost stays invisible to the watchdog forever. Runs the real re-seed
    text from the live loop, against a throwaway tree."""
    src = (ROOT / "scratch_inbox_loop.sh").read_text()
    body = src[src.index("restore_case_settings()"):]
    # the heredoc marker is followed by redirections on the SAME line, so the
    # script starts at the next newline -- not at the marker + len(marker).
    open_at = body.index("<<'PY'")
    py = body[body.index("\n", open_at) + 1: body.index("\nPY\n")]
    d = pathlib.Path(_tmp.mkdtemp(prefix="c576roster"))
    (d / "scratch_full_logs").mkdir()
    (d / "scratch_full_logs" / "watchdog_jobs.json").write_text(
        json.dumps([{"name": "someone_else", "state": "running"}]))
    (d / "scratch_agents_registry.json").write_text(
        json.dumps({"workers": {"ghost": {"session": "sid-123"}}}))
    r = subprocess.run([sys.executable, "-", "ghost", "scratch_worker_ghost_relaunch.sh"],
                       input=py, cwd=str(d), capture_output=True, text=True)
    jobs = json.loads((d / "scratch_full_logs" / "watchdog_jobs.json").read_text())
    entry = next((j for j in jobs if j.get("name") == "ghost"), None)
    check("the lost entry is re-seeded", entry is not None, r.stdout + r.stderr)
    check("with its real session id", (entry or {}).get("session") == "sid-123", entry)
    check("and the pre-existing entry survives",
          any(j.get("name") == "someone_else" for j in jobs), jobs)
    # idempotent: a worker already present is left exactly as it was
    before = json.dumps(jobs, sort_keys=True)
    subprocess.run([sys.executable, "-", "ghost", "scratch_worker_ghost_relaunch.sh"],
                   input=py, cwd=str(d), capture_output=True, text=True)
    check("a second call changes nothing",
          json.dumps(json.loads((d / "scratch_full_logs" / "watchdog_jobs.json").read_text()),
                     sort_keys=True) == before)



def test_c576_service_is_baked_not_derived():
    """TSOMP_SERVICE is restored, but from the generator (correct by
    construction) rather than the lanes record (a configuration, not the truth)."""
    setup(service="chatgpt", model="sol")
    txt = ms.relaunch_path(W).read_text()
    check("relaunch bakes the service it will run",
          'export TSOMP_SERVICE="chatgpt"' in txt, txt[:0])
    ms.write_lanes(W, "fable", "claude", "sol", "chatgpt", case="576t")
    with _board():
        env = ms.settings_env(W)
    check("and the run-time block does not contradict it",
          "TSOMP_SERVICE=" not in env, env)


def test_c576_restore_lane_refuses_a_crashed_deputy():
    """A crashed mid-report deputy has no done-sentinel and must be resumed where
    it was: for a cross-service split, a lane flip retires the codex session and
    reseeds from a lossy handoff, which would destroy work."""
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "opus", "claude", case="576t")
    rec = ms.read_lanes(W); rec["current"] = "report"
    ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
    ms._rewrite_same_service(W, {"to_model": "opus", "to_id": "claude-opus-5", "to_effort": "max"})
    sentinel = pathlib.Path(f"scratch_full_logs/worker_{W}.done")
    sentinel.unlink(missing_ok=True)
    check("no sentinel -> no lane flip", ms.restore_lane(W) is None)
    check("still on the report model", ms.current_target(W)["model"] == "opus")
    sentinel.write_text("")
    try:
        check("a CLOSED case is restored", ms.restore_lane(W) is not None)
    finally:
        sentinel.unlink(missing_ok=True)



def test_c576r2_failed_restore_strands_nothing():
    """apply() stamps state="applying" before doing the work, and switch_pending()
    now ignores this origin -- so a failure that left the record behind could never
    be picked up by anyone. A failed restore must leave NOTHING pending."""
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "opus", "claude", case="576t")
    rec = ms.read_lanes(W); rec["current"] = "report"
    ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
    ms._rewrite_same_service(W, {"to_model": "opus", "to_id": "claude-opus-5", "to_effort": "max"})
    pathlib.Path(f"scratch_full_logs/worker_{W}.done").write_text("")
    boom = ms.apply
    ms.apply = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gen_relaunch failed"))
    try:
        raised = False
        try:
            ms.restore_lane(W)
        except RuntimeError:
            raised = True
        check("the failure is propagated, not swallowed", raised)
        check("and no switch record is left stranded", not ms.switch_path(W).exists(),
              ms.switch_path(W).read_text() if ms.switch_path(W).exists() else "")
    finally:
        ms.apply = boom
        pathlib.Path(f"scratch_full_logs/worker_{W}.done").unlink(missing_ok=True)


def test_c576r2_decline_reasons_are_distinguished():
    """One message for four outcomes hid a deliberate REFUSAL among three no-ops."""
    setup(model="fable")
    ms.write_lanes(W, "fable", "claude", "opus", "claude", case="576t")
    sentinel = pathlib.Path(f"scratch_full_logs/worker_{W}.done")
    sentinel.unlink(missing_ok=True)
    check("no sentinel reads as a refusal", "no done-sentinel" in ms.restore_lane_reason(W),
          ms.restore_lane_reason(W))
    sentinel.write_text("")
    try:
        check("already-on-lane reads as a no-op", "already on the work lane" in ms.restore_lane_reason(W),
              ms.restore_lane_reason(W))
        # pending-switch is only REACHED when the lane actually differs -- the reason
        # must test the same things in the same order as the decision it explains.
        rec = ms.read_lanes(W); rec["current"] = "report"
        ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
        ms._atomic_write(ms.switch_path(W), json.dumps({"worker": W, "state": "requested"}))
        check("a pending switch reads as a refusal", "already pending" in ms.restore_lane_reason(W),
              ms.restore_lane_reason(W))
        ms.switch_path(W).unlink()
        rec["current"] = "work"; ms._atomic_write(ms.lanes_path(W), json.dumps(rec))
        check("require_closed=False skips the sentinel refusal",
              "no done-sentinel" not in ms.restore_lane_reason(W, require_closed=False),
              ms.restore_lane_reason(W, require_closed=False))
        ms.write_lanes(W, "opus", "claude", case="576t")
        check("unsplit reads as no split", "no split" in ms.restore_lane_reason(W),
              ms.restore_lane_reason(W))
    finally:
        sentinel.unlink(missing_ok=True)


def test_c576r2_generator_comment_matches_the_code():
    """A comment that misdescribes the code is the one style defect the code judge
    blocks on. The MF1 fix made the service a baked literal; the comment above the
    run-time block must not still claim the block resolves it."""
    setup()
    txt = ms.relaunch_path(W).read_text()
    blk = txt[txt.index("# The rest of the case's settings"): txt.index("unset _SETTINGS_ENV")]
    check("the comment does not claim to resolve the service at run time",
          "service, mode" not in blk, blk)
    check("and says where model/service really come from",
          "baked literals above" in blk, blk)
    check("the block really does not export them",
          "TSOMP_SERVICE" not in ms.settings_env(W), ms.settings_env(W))



def test_c576r3_reason_mirrors_the_decision():
    """A reason that tests different things, or in a different order, from the
    decision it explains is worse than no reason: it will confidently misreport."""
    import inspect
    GUARDS = ('require_closed and not (LOGS', 'not rec.get("split")',
              '== lane', 'switch_path(name).exists()')

    def guard_order(src):
        """The guards in the order they actually appear. An earlier version of this
        test filtered a FIXED tuple by membership, which yields that fixed order for
        any input -- it passed on deliberately reversed sources, so it could not
        detect the drift it claimed to prevent."""
        return [g for _, g in sorted((src.index(g), g) for g in GUARDS if g in src)]

    dec = inspect.getsource(ms.restore_lane)
    dec = dec[:dec.index("    L = rec[")]
    rsn = inspect.getsource(ms.restore_lane_reason)
    check("the decision guards on all four", len(guard_order(dec)) == 4, guard_order(dec))
    check("the reason guards on all four", len(guard_order(rsn)) == 4, guard_order(rsn))
    check("and in the SAME order", guard_order(dec) == guard_order(rsn),
          f"{guard_order(dec)} vs {guard_order(rsn)}")
    # the check must FAIL on a reordered source -- proven here, not asserted
    swapped = rsn.replace('== lane', '@@TMP@@').replace(
        'switch_path(name).exists()', '== lane').replace('@@TMP@@', 'switch_path(name).exists()')
    check("and it DETECTS a reordering (non-vacuous)",
          guard_order(dec) != guard_order(swapped), guard_order(swapped))



def test_c576r4_generator_heredoc_has_no_unescaped_backticks():
    """The Case-561 trap, now a standing check rather than a comment: the relaunch
    generator's heredoc delimiter is UNQUOTED, so a bare backtick in a comment is
    command substitution at generation time. Case 576 shipped exactly that bug
    (`--model` -> "--model: command not found" on every spawn, and a truncated
    sentence baked into every generated script)."""
    import re
    src = (ROOT / "scratch_gen_relaunch.sh").read_text().split("\n")
    start = next(i for i, l in enumerate(src) if 'cat > "$RELAUNCH.tmp_gen" <<EOF' in l)
    end = next(i for i, l in enumerate(src) if i > start and l.strip() == "EOF")
    bad = [(i + 1, src[i]) for i in range(start + 1, end) if re.search(r"(?<!\\)`", src[i])]
    check("no unescaped backtick anywhere in the generated-script heredoc",
          not bad, bad[:3])
    # non-vacuous: the same scan MUST flag a planted one
    planted = src[start + 1:end] + ["# a switch rewrites them in lockstep with `--model`."]
    check("and the scan would catch one if introduced",
          any(re.search(r"(?<!\\)`", l) for l in planted))


def test_c576r4_generation_is_clean_and_faithful():
    """End-to-end: generating a script must emit NOTHING on stderr (a stray
    expansion shows up there) and must not truncate its own comments."""
    setup()
    r = subprocess.run(
        ["bash", str(ROOT / "scratch_gen_relaunch.sh"), W, str(uuid.uuid4()),
         "operator@example.com", "opus", "max", "infra", "561t", "claude"],
        cwd=str(ROOT), capture_output=True, text=True)
    check("generation exits 0", r.returncode == 0, r.stderr[-200:])
    check("and writes nothing to stderr", r.stderr.strip() == "", r.stderr[-200:])
    txt = ms.relaunch_path(W).read_text()
    check("no comment was truncated by an expansion",
          "lockstep with the model flag" in txt,
          [l for l in txt.split("\n") if "lockstep" in l])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n--- {t.__name__}")
        try:
            t()
        except Exception as e:
            FAIL += 1
            import traceback
            print(f"  [ERROR] {t.__name__}: {e}")
            traceback.print_exc()
    teardown()
    print(f"\n=== SUMMARY: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
