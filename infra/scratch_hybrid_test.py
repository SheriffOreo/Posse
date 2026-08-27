#!/usr/bin/env python3
"""Tests for scratch_hybrid.py (Case 557) — hermetic: no agents, no API calls.

Every test points TSOMP_HYBRID_ROOT at a temp dir and injects a FAKE writer via
the `spawn` hook, so the whole round loop (compose -> spawn -> poll -> parse ->
archive) is exercised without spending a token.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

_ROOT = tempfile.mkdtemp(prefix="hybrid_test_")
os.environ["TSOMP_HYBRID_ROOT"] = _ROOT

import scratch_hybrid as H  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def fake_writer(status="READY", queries=None, report="# Report\n\nBody.",
                write_report=True, write_json=True, delay_report=False):
    """Build a spawn() stand-in that immediately writes the writer's outputs."""
    def _spawn(anon, prompt_file, model):
        d = Path(prompt_file).parent
        if write_json:
            (d / "response.json").write_text(json.dumps({
                "status": status, "one_line": "fake writer",
                "queries": queries or [], "changes": ["c1"], "cut": [], "notes": ""}))
        if write_report and not delay_report:
            (d / "report.md").write_text(report)
    return _spawn


# --------------------------------------------------------------------------
print("paths + charter")
check("hybrid_root honours TSOMP_HYBRID_ROOT", str(H.hybrid_root()) == _ROOT)
check("round_dir nests case/round",
      H.round_dir("900", 2).parts[-2:] == ("case_900", "round_2"))
ch = H.charter_text()
check("charter is non-trivial", len(ch) > 3000, f"len={len(ch)}")
for must in ["QUERY", "response.json", "report.md", "READY", "NEEDS_INPUT", "BLOCKED"]:
    check(f"charter names {must}", must in ch)
check("charter forbids inventing facts",
      "may not invent" in ch or "not invent" in ch)

# --------------------------------------------------------------------------
print("compose()")
d = tempfile.mkdtemp()
draft = Path(d) / "draft.md"
draft.write_text("# Draft\n\nThe number is 42.")
art = Path(d) / "eg.py"
art.write_text("x = 42\n")
p = H.compose("901", draft=draft, artifacts=[str(art)], outdir="/out",
              round_no=1, request_text="do the thing")
check("prompt embeds the charter", "HYBRID WRITER CHARTER" in p)
check("prompt embeds the draft", "The number is 42." in p)
check("prompt lists artifacts", str(art) in p)
check("prompt carries the request", "do the thing" in p)
check("prompt names both output files",
      "/out/report.md" in p and "/out/response.json" in p)
check("round 1 has no previous-round section", "YOUR PREVIOUS ROUND" not in p)

# a second round must carry the previous report + answers
r1 = H.round_dir("902", 1, create=True)
(r1 / "report.md").write_text("# Prev report\n\nold text")
(r1 / "response.json").write_text(json.dumps(
    {"status": "NEEDS_INPUT", "queries": [
        {"id": "q1", "kind": "missing_fact", "question": "what is k?", "blocking": True}]}))
# compose() is pure: it takes the previous round's dir rather than deriving it
# (write_round is what resolves and passes it).
p2 = H.compose("902", draft=draft, artifacts=[], outdir="/out", round_no=2,
               previous=r1, answers="k is 7")
check("round 2 embeds previous report", "old text" in p2)
check("round 2 embeds previous queries", "what is k?" in p2)
check("round 2 embeds the answers", "k is 7" in p2)

# --------------------------------------------------------------------------
print("response parsing is fail-closed")
rd = Path(tempfile.mkdtemp())


def parse(resp, report=True):
    (rd / "response.json").write_text(json.dumps(resp))
    rp = rd / "report.md"
    if report:
        rp.write_text("# r")
    elif rp.exists():
        rp.unlink()
    return H._parse_response(rd)


r = parse({"status": "READY", "queries": [{"id": "q1", "blocking": True}]})
check("READY + blocking query downgrades to NEEDS_INPUT", r["status"] == "NEEDS_INPUT")
check("downgrade is flagged", r.get("downgraded") is True)
check("downgraded round is not ready", r["ready"] is False)
r = parse({"status": "READY", "queries": [{"id": "q1", "blocking": False}]})
check("READY + non-blocking query stays READY", r["status"] == "READY" and r["ready"])
r = parse({"status": "ready", "queries": []})
check("status is case-insensitive", r["status"] == "READY")
r = parse({"status": "NONSENSE"})
check("unknown status -> NEEDS_INPUT", r["status"] == "NEEDS_INPUT")
r = parse({"status": "READY"}, report=False)
check("READY without report.md is not ready", r["ready"] is False)
r = parse({"status": "READY", "queries": "not-a-list"})
check("non-list queries coerced to []", r["queries"] == [])
(rd / "response.json").write_text("{ broken")
(rd / "report.md").write_text("# r")
r = H._parse_response(rd)
check("broken json + a report -> NEEDS_INPUT not loss", r["status"] == "NEEDS_INPUT")
check("broken json is flagged malformed", r.get("malformed") is True)
(rd / "response.json").unlink()
(rd / "report.md").unlink()
check("no files at all -> empty dict", H._parse_response(rd) == {})

# --------------------------------------------------------------------------
print("write_round() end to end (fake writer)")
res = H.write_round("910", draft=draft, artifacts=[], round_no=1,
                    spawn=fake_writer(status="READY"), poll=0.01, timeout=5)
check("round returns READY", res["status"] == "READY")
check("round is marked ready", res["ready"] is True)
check("round dir recorded", Path(res["dir"]).is_dir())
check("prompt.md archived", (Path(res["dir"]) / "prompt.md").is_file())
check("meta.json archived", (Path(res["dir"]) / "meta.json").is_file())
meta = json.loads((Path(res["dir"]) / "meta.json").read_text())
check("meta records the service", meta.get("service") == "chatgpt", meta.get("service"))
check("meta records the pinned model id",
      meta.get("model_id") == "gpt-5.6-terra", meta.get("model_id"))

# a writer that never writes anything -> BLOCKED, no exception
res = H.write_round("911", draft=draft, round_no=1,
                    spawn=lambda *a: None, poll=0.01, timeout=1.0)
check("silent writer -> BLOCKED", res["status"] == "BLOCKED")
check("silent writer flagged timed_out/died",
      res.get("timed_out") or res.get("died"))
check("silent writer never raises", res.get("ready") is False)

# --------------------------------------------------------------------------
print("run() loop + fallback")
# HERMETIC: every run() test must patch _spawn_writer. run() has no spawn hook of
# its own (only write_round does), so leaving it unpatched launches a REAL agent —
# which is exactly what happened the first time this suite was run, spending ~11k
# tokens on a live codex writer and then failing the assertion because the writer
# SUCCEEDED and so never fell back.
_orig = H._spawn_writer

H._spawn_writer = lambda a, p, m: None          # a writer that produces nothing
try:
    res = H.run("920", draft=draft, rounds=3, verbose=False, timeout=1.0,
                answer_fn=lambda qs, n: "answered")
finally:
    H._spawn_writer = _orig
check("run() with a dead writer falls back to the draft", res["fell_back"] is True)
check("fallback report path IS the draft", res["report"] == str(draft))
check("fallback still returns the draft text", "The number is 42." in res["report_text"])
check("fallback status is BLOCKED", res["status"] == "BLOCKED")

# converges on the first READY round and stops early
calls = []


def counting_spawn(anon, prompt_file, model):
    calls.append(anon)
    fake_writer(status="READY")(anon, prompt_file, model)


H._spawn_writer = counting_spawn
try:
    res = H.run("921", draft=draft, rounds=5, verbose=False)
finally:
    H._spawn_writer = _orig
check("run() stops at the first READY", res["rounds_run"] == 1, res["rounds_run"])
check("only one writer was spawned", len(calls) == 1, len(calls))
check("run() reports READY", res["status"] == "READY")
check("run() did NOT fall back", res["fell_back"] is False)
check("run() report is the round's report.md",
      res["report"].endswith("round_1/report.md"), res["report"])

# a NEEDS_INPUT round with queries keeps iterating and feeds answers forward
seen_prompts = []


def qspawn(anon, prompt_file, model):
    seen_prompts.append(Path(prompt_file).read_text())
    n = len(seen_prompts)
    fake_writer(status="READY" if n >= 2 else "NEEDS_INPUT",
                queries=[] if n >= 2 else
                [{"id": "q1", "kind": "missing_fact", "question": "k?", "blocking": True}]
                )(anon, prompt_file, model)


H._spawn_writer = qspawn
try:
    res = H.run("922", draft=draft, rounds=3, verbose=False,
                answer_fn=lambda qs, n: "k is 7")
finally:
    H._spawn_writer = _orig
check("iterates past NEEDS_INPUT", res["rounds_run"] == 2, res["rounds_run"])
check("converges to READY", res["status"] == "READY")
check("round 2 prompt carried the answers", "k is 7" in seen_prompts[1])
check("answers.md archived", (H.round_dir("922", 1) / "answers.md").is_file())

# no answer_fn + no queries -> stop rather than re-run identical input
H._spawn_writer = lambda a, p, m: fake_writer(status="NEEDS_INPUT", queries=[])(a, p, m)
try:
    res = H.run("923", draft=draft, rounds=4, verbose=False)
finally:
    H._spawn_writer = _orig
check("stops when another round would be identical", res["rounds_run"] == 1,
      res["rounds_run"])

# --------------------------------------------------------------------------
print("history() / final_report()")
hs = H.history("922")
check("history has both rounds", len(hs) == 2, len(hs))
check("history is ordered", [h["round"] for h in hs] == [1, 2])
check("history records status", hs[1]["status"] == "READY")
check("final_report returns the last non-empty report",
      str(H.final_report("922")).endswith("round_2/report.md"))
check("final_report on an unknown case is None", H.final_report("nope") is None)
check("history on an unknown case is []", H.history("nope") == [])

# round_10 must sort after round_9, not between round_1 and round_2
for i in (1, 2, 9, 10):
    rr = H.round_dir("930", i, create=True)
    (rr / "report.md").write_text(f"r{i}")
    (rr / "response.json").write_text(json.dumps({"status": "READY"}))
check("history sorts round_10 last",
      [h["round"] for h in H.history("930")] == [1, 2, 9, 10],
      [h["round"] for h in H.history("930")])

# --------------------------------------------------------------------------
print("registry integration")
import scratch_models as sm  # noqa: E402
check("default hybrid model is a chatgpt model",
      sm.service_of(H.DEFAULT_MODEL) == "chatgpt", H.DEFAULT_MODEL)
check("default hybrid model is registered", sm.known(H.DEFAULT_MODEL))
check("per-round timeout stays under the 50-min wait-rule line",
      H.DEFAULT_TIMEOUT <= 50 * 60, H.DEFAULT_TIMEOUT)

# --------------------------------------------------------------------------
print("PDF/LaTeX gate (Case 557, uid=669)")
for t in ("reports/x/R.pdf", "reports/x/R.tex", "a.LaTeX", "b.PDF"):
    ok, _ = H.should_handoff(t)
    check(f"{t} -> writer runs", ok is True)
for t in ("notes.md", "findings.MD", "r.txt", "Makefile"):
    ok, _ = H.should_handoff(t)
    check(f"{t} -> deputy writes it itself", ok is False)
ok, why = H.should_handoff("")
check("no target -> taken at face value", ok is True and "face value" in why)

# the gate is enforced by run(), not merely advertised
_o = H._spawn_writer
H._spawn_writer = lambda a, p_, m: (_ for _ in ()).throw(
    AssertionError("writer must NOT be spawned for a .md target"))
try:
    res = H.run("940", draft=draft, rounds=3, verbose=False, target="out/NOTES.md")
finally:
    H._spawn_writer = _o
check("run() SKIPS a .md target", res["status"] == "SKIPPED")
check("skip returns the deputy's own draft", res["report"] == str(draft))
check("skip is not a fallback/failure", res["fell_back"] is False and res["rounds_run"] == 0)
check("skip explains why", "PDF/LaTeX" in res["reason"])

print("LaTeX target routing")
p_tex = H.compose("941", draft=draft, artifacts=[], outdir="/o", round_no=1,
                  target="reports/x/R.tex")
p_md = H.compose("941", draft=draft, artifacts=[], outdir="/o", round_no=1,
                 target="reports/x/R.md")
check("tex target asks for report.tex", "/o/report.tex" in p_tex)
check("tex target names the FORMAT EXCEPTION", "FORMAT EXCEPTION" in p_tex)
# Assert on the DIRECTIVE line, not the whole prompt: the charter's FORMAT
# EXCEPTION paragraph mentions report.tex in every prompt, tex target or not.
_dir_md = [l for l in p_md.splitlines() if "WRITE YOUR DOCUMENT AS" in l][0]
_dir_tex = [l for l in p_tex.splitlines() if "WRITE YOUR DOCUMENT AS" in l][0]
check("md target directs to report.md",
      "/o/report.md" in _dir_md and "report.tex" not in _dir_md)
check("tex target directs to report.tex",
      "/o/report.tex" in _dir_tex and "report.md" not in _dir_tex)
check("target is echoed into the assignment", "reports/x/R.tex" in p_tex)
# report_path finds either name, .tex winning
rd = H.round_dir("942", 1, create=True)
(rd / "report.md").write_text("md")
check("report_path finds report.md", H.report_path(rd).name == "report.md")
(rd / "report.tex").write_text("tex")
check("report_path prefers report.tex", H.report_path(rd).name == "report.tex")
check("report_path on an empty dir is None",
      H.report_path(H.round_dir("943", 1, create=True)) is None)

print("deputy-facing protocol block")
pb = H.protocol_block(557)
for must in ("Claude + ChatGPT", "scratch_hybrid.py handoff", "--target", "--artifact",
             "NEEDS_INPUT", "READY", "BLOCKED", "queries", "--start-round",
             "falls back", "case_557"):
    check(f"protocol names {must}", must in pb)
check("protocol states the md rule", ".md" in pb and "WRITE IT YOURSELF" in pb)

shutil.rmtree(_ROOT, ignore_errors=True)
print()
print(f"=== SUMMARY: {PASS} passed, {FAIL} failed ===")
raise SystemExit(1 if FAIL else 0)
