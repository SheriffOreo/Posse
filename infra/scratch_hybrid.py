#!/usr/bin/env python3
"""HYBRID MODE — claude does the work, chatgpt writes the report, they iterate.

Case 557 (Feng): "claude does all the work, code writing, etc. and pass the draft
of report to chatgpt, and chatgpt is responsible for writing the report to human,
and they can iterate together."

WHY THIS SHAPE
A one-shot "polish this text" hand-off would not be iteration, and it would waste
the thing that makes an agent better than an API call: the writer can READ THE
REPO. So a hybrid round is a two-way exchange.

  round r:  deputy  --(draft + artifacts + answers to r-1's queries)-->  writer
            writer  --(report.md + response.json{status, queries, changes})-->  deputy

The writer owns the prose and the structure. It may not invent facts: anything it
cannot verify from the draft or the artifacts it must raise as a QUERY rather than
write around, and the deputy answers it in the next round. The loop converges when
the writer returns status=READY (no blocking queries), or when the round budget is
spent — whichever comes first.

RELATIONSHIP TO THE JUDGE (Case 551)
Deliberately the same machinery, because it is the same problem: hand an artifact
to an independent agent, block for a structured response, archive every round so
the outcome is a file rather than a claim in an email. scratch_critic.py proved
that loop; this reuses its shape (compose -> spawn anonymous worker -> poll for a
structured result -> archive under case_<n>/round_<r>/) rather than inventing a
second orchestration. The difference is the direction of authority: a judge RULES
on the deputy's artifact, a writer OWNS the artifact and the deputy supplies truth.

FAIL-CLOSED
A hybrid hand-off must never cost the deputy its work. If the writer dies, times
out, or returns garbage, run() keeps the last good report — falling back to the
deputy's own draft — and says so in the result. The report is never left empty and
the draft is never overwritten.

USAGE (from a deputy, blocking)
    python scratch_hybrid.py handoff --case 557 --draft reports/task557/DRAFT.md \\
        --artifact scratch_hybrid.py --artifact scratch_models.py \\
        --request "the original ask, verbatim" --rounds 3
  -> writes scratch_full_logs/hybrid/case_557/round_*/report.md and prints the
     final report path + status. Between rounds it prints the writer's queries;
     answer them with `answer --case 557 --round R --text/--file` (or let
     --auto-answer hand them straight back for the next round).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Poll cadence while blocking on a writer round. Kept at the Task 216 discipline:
# these are cheap file stats in a foreground loop, never a model call.
_POLL_CHUNK = 15.0
DEFAULT_TIMEOUT = 2400.0          # 40 min per round — under the 50-min wait-rule line
DEFAULT_ROUNDS = 3
# The writer model. A hybrid case that picked a chatgpt model on the create-case
# form has it exported as TSOMP_HYBRID_MODEL by scratch_spawn_worker.sh; absent
# that, terra (the balanced chatgpt tier; sol for a hard report).
DEFAULT_MODEL = os.environ.get("TSOMP_HYBRID_MODEL") or "terra"

STATUSES = ("READY", "NEEDS_INPUT", "BLOCKED")

# ---------------------------------------------------------------------------
# WHEN the hand-off is worth it (Case 557, Feng uid=669)
#
# "only pdf/latex should be written by chatgpt (if I ask to write a pdf report),
#  md you can just do it yourself."
#
# So the writer is for the POLISHED, human-facing artifact — a PDF report or the
# LaTeX behind it. A plain .md (a case file, a findings note, a README) the
# deputy writes itself: it is working material, the round trip costs a worker,
# and Steven reads it as text either way. This is a real GATE, not advice in the
# charter — should_handoff() is checked by run(), so a deputy that calls hybrid
# on a .md gets its own draft straight back with the reason.
# ---------------------------------------------------------------------------
WRITER_SUFFIXES = (".pdf", ".tex", ".latex")


def should_handoff(target):
    """(bool, reason) — is `target` a deliverable the ChatGPT writer should own?

    `target` is the INTENDED final deliverable path (what Steven will open), not
    the draft. No target at all -> True: an explicit hybrid call with nothing to
    check is taken at its word, since the caller may be producing a PDF by a
    route we cannot see."""
    if not target:
        return True, "no --target given; taking the hybrid request at face value"
    suf = Path(str(target)).suffix.lower()
    if suf in WRITER_SUFFIXES:
        return True, f"target is {suf} — a polished deliverable, the writer owns it"
    return False, (f"target is {suf or 'extensionless'}, not PDF/LaTeX — per the "
                   f"Case-557 rule the deputy writes this one itself")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def hybrid_root(create: bool = False) -> Path:
    root = Path(os.environ.get("TSOMP_HYBRID_ROOT",
                               REPO_ROOT / "scratch_full_logs" / "hybrid"))
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def case_dir(case, create: bool = False) -> Path:
    d = hybrid_root(create=create) / f"case_{case}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def round_dir(case, round_no, create: bool = False) -> Path:
    d = case_dir(case, create=create) / f"round_{int(round_no)}"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s):
    return re.sub(r"[^a-z0-9_-]", "", str(s).lower())[:32] or "case"


def _read(p, limit=None):
    try:
        t = Path(p).read_text(errors="replace")
    except Exception:
        return ""
    if limit and len(t) > limit:
        return t[:limit] + f"\n\n[... truncated at {limit} chars ...]"
    return t


# ---------------------------------------------------------------------------
# the writer charter — the fixed standard, identical for every hybrid case
# ---------------------------------------------------------------------------

CHARTER = """\
# THE HYBRID WRITER CHARTER  (v1.0, Case 557)

You are the WRITER on a hybrid case. Another agent — a Claude deputy — did the
work: it ran the experiments, wrote the code, read the sources, and produced a
DRAFT plus the artifacts behind it. You own exactly one thing, and you own it
completely: **the document the human actually reads.**

The human is Steven. He is technical, busy, and reads on a phone as often as a
laptop. He has said he finds ChatGPT's writing better than Claude's — that is why
you are in this loop. Earn it.

## 1. Your authority, and its limit

YOURS: structure, emphasis, order, length, headings, what gets a table, what gets
cut, every sentence of the prose. You are not a copy-editor applying a polish pass
to someone else's outline. If the draft buries the finding in section 4, move it
to the top. If two sections say the same thing, merge them. Rewrite freely.

NOT YOURS: the facts. You may not invent, extrapolate, round, or soften a number,
a file path, a command, or a result. Every factual claim in your report must trace
to the draft or to an artifact you actually read.

When you need a fact you do not have — a number the draft omits, a claim you
cannot verify, an ambiguity you cannot resolve — you do NOT write around it, and
you do NOT guess. You raise it as a QUERY and the deputy answers it next round.
Writing around a gap is the one failure mode that makes this whole loop worthless.

## 2. Read the artifacts. Do not just rewrite the draft.

You are an agent in the repository, not a text box. The artifacts listed in your
assignment are real files: open them. Check that the draft's numbers match what
the code and outputs actually say. If the draft claims a test passes, look. A
mismatch between the draft and its own evidence is the single most valuable thing
you can find — report it as a query, flagged as a CONTRADICTION.

## 3. What a good report to Steven looks like

- **Lead with the answer.** First paragraph states what was asked and what the
  outcome was. Not "background", not method. If he reads only the first five
  lines, he should have the result.
- **One message.** You should be able to write, in one sentence, what this
  document is for. If you cannot, the structure is wrong — fix it before writing.
- **Numbers, always.** "Much faster" is not a finding; "4.05x on small k, 0.24x at
  k=1007" is. Every claim as large as its evidence and no larger.
- **Ground it.** file:line, exact commands, exact paths. He will check.
- **Say what you did NOT do.** Untested paths, skipped cases, known gaps, things
  held for his decision. A report that hides its own limits is worse than useless
  because it will be believed.
- **Terse.** Cut every sentence that does not carry a fact or a decision. No
  throat-clearing openers, no "it is worth noting that", no restating the question
  back at him, no summary paragraph that repeats the top. Density is not the same
  as compression: shorter is only better when nothing load-bearing is lost.
- **No AI slop.** No "delve", no "robust solution", no triads of adjectives, no
  concluding paragraph about how this "empowers" anything.

## 4. Structure, by default

Use your judgement, but absent a reason to differ:
  1. What was asked, and the answer (short).
  2. What was done, and what it produced — the numbers.
  3. What to look at (deliverable paths).
  4. What is NOT done / open decisions for him.
Headings that state findings ("Resume works; the session id is stable") beat
headings that state topics ("Session handling").

## 5. Your output — TWO files, both required

Write BOTH into the OUTPUT DIRECTORY named in your assignment.

**report.md** — the document itself. Markdown. No preamble about being an AI, no
meta-commentary about the drafting process. Just the report.

  FORMAT EXCEPTION: if your assignment names a TARGET ending in `.tex`, write
  **report.tex** INSTEAD of report.md — a complete, standalone LaTeX document
  that compiles as-is with `pdflatex` (its own documentclass and preamble, no
  external .sty). Prefer `article`, `booktabs` for tables, `graphicx` only if you
  actually include a figure. Escape LaTeX specials in prose (`_`, `%`, `&`, `#`,
  `$`). Keep every line under ~100 characters so the source stays diffable, and
  do not let a table or figure run off the page — an oversized float is a silent
  failure that logs "Float too large for page", not an Overfull warning.
  You are producing the artifact a human will read as a PDF; that is exactly why
  this mode exists. You still write response.json either way.

**response.json** — strict JSON, exactly this shape:

    {
      "status": "READY" | "NEEDS_INPUT" | "BLOCKED",
      "one_line": "<one sentence: the state of the report>",
      "queries": [
        {"id": "q1",
         "kind": "missing_fact" | "unverifiable" | "contradiction" | "ambiguity",
         "question": "<what you need, precisely>",
         "why": "<what in the report depends on it>",
         "blocking": true | false}
      ],
      "changes": ["<what you changed this round and why>"],
      "cut": ["<what you removed, and why it was not load-bearing>"],
      "notes": "<anything the deputy should know; may be empty>"
    }

STATUS, honestly:
  READY       the report is finished and every claim in it is supported. Use this
              only when you have NO blocking queries.
  NEEDS_INPUT the report is written but depends on answers you do not have.
  BLOCKED     you could not produce a usable report at all; say why in one_line.

A READY carrying a blocking query is a contradiction and will be downgraded to
NEEDS_INPUT automatically — so do not use READY to look finished. There is no
credit for converging fast, and none for finding fault either. The only thing
being measured is whether the document is true and worth his time.

## 6. Iterating

On later rounds you are given your own previous report and the deputy's ANSWERS to
your queries. Fold the answers in, re-check anything they change, and drop queries
they resolve. Do not re-litigate a settled point. Do not re-ask a question that was
answered. Carry forward what was already good — a later round should be strictly
better, never a rewrite for its own sake.
"""


def protocol_block(case):
    """The deputy-facing HYBRID PROTOCOL text for a claude+chatgpt case.

    ONE source of truth (Case 551's pattern): the web form, a precinct-tagged
    email and a direct spawn all render this, so the instructions can never drift
    between the three intake paths."""
    return f"""\
This case runs in **Claude + ChatGPT** mode. You (Claude) do ALL the work — the
investigation, the code, the runs, the verification. You do NOT write the final
human-facing report: a ChatGPT writer does, and the two of you iterate.

WHEN THIS APPLIES — only for a POLISHED deliverable, i.e. a PDF or the LaTeX
behind it. If what Steven asked for is a plain .md (a findings note, a case file,
a README), WRITE IT YOURSELF and skip the hand-off entirely; the gate below
enforces this, so calling it on a .md just returns your draft.

HOW TO RUN IT (blocking, foreground, at the END once the work is done and verified):

    python scratch_hybrid.py handoff --case {case} \\
        --draft <your draft>.md \\
        --target reports/task{case}/REPORT.tex \\
        --artifact <a file the writer should verify against> \\
        --artifact <another> \\
        --request-file <the verbatim request> \\
        --rounds 3

  * --draft is YOUR raw material: every fact, number, path and caveat, structured
    however you like. Do not polish it — that is the writer's job. Do not omit a
    limitation to make it read better; the writer cannot invent what you leave out.
  * --target is the deliverable Steven will open. A `.tex` target makes the writer
    emit LaTeX that compiles with `~/.TinyTeX/bin/x86_64-linux/pdflatex`.
  * --artifact each real file backing a claim. The writer OPENS these and checks
    your numbers against them; a mismatch comes back as a CONTRADICTION query.

THE ITERATION — this is the part that matters. The writer returns a status:
  READY        the report is done and every claim in it is supported.
  NEEDS_INPUT  it wrote the report but raised QUERIES it cannot answer itself.
  BLOCKED      it could not produce a usable report; the reason is in one_line.
On NEEDS_INPUT, READ THE QUERIES (`python scratch_hybrid.py queries --case {case}`)
and ANSWER THEM — with facts and file paths, not reassurance. Then continue:

    python scratch_hybrid.py handoff --case {case} --draft <draft> --target <target> \\
        --answers-file <your answers>.md --start-round <next> --rounds 2

A query is the writer telling you the report cannot honestly be finished without
something only you know. Do not talk it out of a query and do not answer one by
softening the claim — fix the gap or say plainly that it cannot be closed.

WHAT YOU DELIVER: the writer's document is the report. Copy it to your deliverable
path, build the PDF if the target is .tex, and ATTACH THAT to your FINAL email —
not your draft. Keep the draft alongside it so the two can be compared.
Every round is archived under scratch_full_logs/hybrid/case_{case}/round_<r>/
(prompt, the document, response.json, your answers), so the hand-off is an
artifact rather than a claim in an email.

IF THE WRITER FAILS (dies, times out, BLOCKED on every round) the tool falls back
to YOUR draft and says so. That is a degraded outcome, not a silent one: say in
your FINAL that the hybrid hand-off failed and that the report is your own.
"""


def charter_text():
    """The writer charter. An operator can override the file to retune it."""
    p = hybrid_root() / "WRITER_CHARTER.md"
    if p.is_file():
        t = _read(p).strip()
        if t:
            return t
    return CHARTER


# ---------------------------------------------------------------------------
# prompt composition
# ---------------------------------------------------------------------------

def compose(case, *, draft, artifacts, outdir, round_no=1, request_text="",
            previous=None, answers="", deputy_note="", target=""):
    """CHARTER + this round's assignment. `previous` is the prior round's dir."""
    tex = str(target).lower().endswith((".tex", ".latex"))
    outname = "report.tex" if tex else "report.md"
    parts = [charter_text().rstrip(), "", "=" * 74,
             "# THIS ASSIGNMENT", "",
             f"CASE: {case}",
             f"ROUND: {round_no}",
             f"OUTPUT DIRECTORY: {outdir}",
             f"REPOSITORY: {REPO_ROOT}"]
    if target:
        parts.append(f"TARGET DELIVERABLE: {target}")
    parts += [f"WRITE YOUR DOCUMENT AS: {outdir}/{outname}"
              + ("   (LaTeX — see the FORMAT EXCEPTION in the charter)" if tex else ""),
              ""]

    if request_text.strip():
        parts += ["## The request the deputy was given", "",
                  request_text.strip(), ""]

    if deputy_note.strip():
        parts += ["## Note from the deputy", "", deputy_note.strip(), ""]

    parts += ["## The deputy's DRAFT", "",
              "This is the raw material, not a template. Restructure it freely.",
              "", "```markdown", _read(draft, 120_000).rstrip(), "```", ""]

    if artifacts:
        parts += ["## Artifacts — READ THESE, do not take the draft's word for them",
                  ""]
        parts += [f"  - {a}" for a in artifacts]
        parts += ["",
                  "Open them. Verify the draft's numbers against them. A mismatch is a",
                  "CONTRADICTION query, and it is the most valuable thing you can find.",
                  ""]

    if previous is not None:
        prev_report = report_path(previous)
        prev_resp = _parse_response(Path(previous))
        parts += ["=" * 74, "# YOUR PREVIOUS ROUND", ""]
        if prev_report:                      # report_path -> Path or None
            fence = "latex" if prev_report.suffix.lower() in (".tex", ".latex") else "markdown"
            parts += [f"## The document you wrote last round ({prev_report.name})", "",
                      f"```{fence}", _read(prev_report, 120_000).rstrip(), "```", ""]
        if prev_resp.get("queries"):
            parts += ["## The queries you raised", ""]
            for q in prev_resp["queries"]:
                if isinstance(q, dict):
                    parts.append(f"  [{q.get('id','?')}] ({q.get('kind','?')}, "
                                 f"blocking={q.get('blocking')}) {q.get('question','')}")
                else:
                    parts.append(f"  - {q}")
            parts.append("")
        if answers.strip():
            parts += ["## THE DEPUTY'S ANSWERS — fold these in", "",
                      answers.strip(), ""]
        else:
            parts += ["## The deputy's answers", "",
                      "(none supplied this round — if a query is still unanswered and",
                      "still blocking, keep it open rather than guessing)", ""]

    parts += ["=" * 74, "",
              "Write BOTH files now:",
              f"  {outdir}/{outname}"
              + (" " * max(1, 16 - len(outname))) + "(the document)",
              f"  {outdir}/response.json   (strict JSON, schema per the charter)",
              "",
              "Write the files directly. Do not print the report to stdout instead of",
              "writing it, and do not ask for confirmation before writing.", ""]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------

def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


# The document a round produced. Case 557: a LaTeX target makes the writer emit
# report.tex instead of report.md, so every "did it write the document" check
# goes through here rather than hardcoding one name. .tex wins when both exist
# (a round that was asked for LaTeX and also left a stray .md).
_REPORT_NAMES = ("report.tex", "report.md")


def report_path(d):
    """Path to round-dir `d`'s document, or None if it wrote neither name."""
    for n in _REPORT_NAMES:
        p = Path(d) / n
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _parse_response(d: Path) -> dict:
    """Parse round dir `d`'s response.json into a normalized dict. {} if absent.

    Normalization is FAIL-CLOSED in the same spirit as the judge's verdict parser:
    an unknown status becomes NEEDS_INPUT, and a READY that still carries a
    blocking query is downgraded — the status can never disagree with the queries
    listed underneath it."""
    j = _read_json(d / "response.json")
    if not isinstance(j, dict):
        # A writer that produced a report but no/!broken json is NOT a failure —
        # treat it as NEEDS_INPUT so the deputy still gets the prose.
        if report_path(d):
            return {"status": "NEEDS_INPUT", "one_line": "response.json missing or unparseable",
                    "queries": [], "changes": [], "cut": [], "notes": "",
                    "has_report": True, "malformed": True}
        return {}
    out = dict(j)
    st = str(out.get("status", "")).strip().upper().replace(" ", "_")
    out["status"] = st if st in STATUSES else "NEEDS_INPUT"
    for k in ("queries", "changes", "cut"):
        v = out.get(k)
        out[k] = v if isinstance(v, list) else []
    out.setdefault("one_line", "")
    out.setdefault("notes", "")
    blocking = [q for q in out["queries"]
                if isinstance(q, dict) and q.get("blocking")]
    out["blocking"] = blocking
    if out["status"] == "READY" and blocking:
        out["status"] = "NEEDS_INPUT"
        out["downgraded"] = True
    out["has_report"] = report_path(d) is not None
    out["ready"] = out["status"] == "READY" and out["has_report"]
    return out


# ---------------------------------------------------------------------------
# running a round
# ---------------------------------------------------------------------------

def _spawn_writer(anon, prompt_file, model):
    subprocess.run(["bash", "scratch_spawn_anon.sh", anon, str(prompt_file),
                    "--model", model],
                   cwd=str(REPO_ROOT), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _anon_alive(anon):
    r = subprocess.run(["tmux", "has-session", "-t", f"=anon_{anon}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def write_round(case, *, draft, artifacts=(), round_no=1, request_text="",
                answers="", deputy_note="", model=DEFAULT_MODEL,
                timeout=DEFAULT_TIMEOUT, poll=_POLL_CHUNK, spawn=None, now=None,
                target=""):
    """Run ONE writer round to completion (BLOCKING) and return the parsed response.

    The returned dict always carries 'dir' and 'round'. On timeout/death it carries
    status BLOCKED and 'timed_out'/'died' — never an exception, because a hybrid
    failure must degrade to "keep the draft", not take the deputy down with it."""
    d = round_dir(case, round_no, create=True)
    prev = round_dir(case, round_no - 1) if round_no > 1 else None
    if prev is not None and not prev.is_dir():
        prev = None

    prompt = compose(case, draft=draft, artifacts=list(artifacts), outdir=d,
                     round_no=round_no, request_text=request_text,
                     previous=prev, answers=answers, deputy_note=deputy_note,
                     target=target)
    (d / "prompt.md").write_text(prompt)

    import scratch_models as sm
    ts = float(now or time.time())
    (d / "meta.json").write_text(json.dumps({
        "case": str(case), "round": int(round_no), "ts": ts,
        "model": model, "model_id": sm.resolve_id(model),
        "service": sm.service_of(model),
        "draft": str(draft), "artifacts": [str(a) for a in artifacts],
        "target": str(target or ""),
    }, indent=2))

    anon = f"hybrid_{_slug(case)}_r{round_no}"
    (spawn or _spawn_writer)(anon, d / "prompt.md", model)

    deadline = ts + float(timeout)
    grace = None
    while True:
        r = _parse_response(d)
        if r:
            # If the json landed before report.md, give the writer a moment to
            # finish the second file rather than reporting a half-round.
            if not r.get("has_report") and _anon_alive(anon):
                if grace is None:
                    grace = time.time() + 45.0
                if time.time() < grace:
                    time.sleep(min(poll, 5.0))
                    continue
            r["dir"] = str(d)
            r["round"] = int(round_no)
            return r
        if not _anon_alive(anon) and time.time() > ts + 60:
            # died without producing anything parseable
            return {"status": "BLOCKED", "one_line": "writer exited without a report",
                    "queries": [], "changes": [], "cut": [], "notes": "",
                    "blocking": [], "has_report": report_path(d) is not None,
                    "ready": False, "died": True, "dir": str(d), "round": int(round_no)}
        if time.time() >= deadline:
            return {"status": "BLOCKED", "one_line": f"writer timed out after {timeout:.0f}s",
                    "queries": [], "changes": [], "cut": [], "notes": "",
                    "blocking": [], "has_report": report_path(d) is not None,
                    "ready": False, "timed_out": True, "dir": str(d), "round": int(round_no)}
        time.sleep(poll)


def run(case, *, draft, artifacts=(), rounds=DEFAULT_ROUNDS, request_text="",
        model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT, answer_fn=None,
        deputy_note="", verbose=True, start_round=1, answers="", target=""):
    """The full hybrid loop. Returns a result dict:
        {status, rounds_run, report (path), report_text, history, fell_back}

    `answer_fn(queries, round_no) -> str` supplies the deputy's answers between
    rounds. With no answer_fn the loop still runs (the writer keeps unanswered
    queries open) but will usually stop at NEEDS_INPUT, which is the honest
    outcome: nobody answered.

    `start_round` + `answers` CONTINUE an existing hand-off: a deputy that came
    back to answer a blocking query out-of-band restarts at the next round with
    those answers, instead of paying for round 1 again. (This gap was found by
    dogfooding: the writer held a blocking query, the deputy answered it, and
    there was no way to resume — every path re-ran from round 1.)"""
    # Case 557 (uid=669): the PDF/LaTeX gate. Checked HERE rather than left to the
    # deputy's judgement, so "md I write myself" is enforced by the tool and not by
    # remembering. Skipping is a success, not a failure: the draft IS the deliverable.
    ok, why = should_handoff(target)
    if not ok:
        if verbose:
            print(f"[hybrid] SKIPPED — {why}", flush=True)
        return {"status": "SKIPPED", "rounds_run": 0, "report": str(draft),
                "report_text": _read(draft), "fell_back": False, "skipped": True,
                "reason": why, "history": [], "last": {}}

    history, last_good = [], None
    r = {}
    start = max(1, int(start_round))
    for n in range(start, start + int(rounds)):
        if verbose:
            print(f"[hybrid] case {case} round {n}/{rounds} — writer running "
                  f"({model})…", flush=True)
        r = write_round(case, draft=draft, artifacts=artifacts, round_no=n,
                        request_text=request_text, answers=answers,
                        deputy_note=deputy_note, model=model, timeout=timeout,
                        target=target)
        history.append({k: r.get(k) for k in
                        ("round", "status", "one_line", "dir", "ready")})
        rep = report_path(r.get("dir", "")) if r.get("dir") else None
        if rep:
            last_good = rep
        if verbose:
            print(f"[hybrid]   -> {r.get('status')} : {r.get('one_line','')}", flush=True)
            for q in r.get("blocking") or []:
                print(f"[hybrid]      BLOCKING {q.get('id','?')}: {q.get('question','')}",
                      flush=True)
        if r.get("ready"):
            break
        if n < start + int(rounds) - 1:
            qs = r.get("queries") or []
            answers = answer_fn(qs, n) if (answer_fn and qs) else ""
            if answers:
                (Path(r["dir"]) / "answers.md").write_text(answers)
            elif not qs:
                # nothing to answer and not READY -> another round would be
                # identical input; stop rather than burn a writer on it.
                break

    fell_back = last_good is None
    return {"status": r.get("status", "BLOCKED"),
            "rounds_run": len(history),
            "report": str(last_good) if last_good else str(draft),
            "report_text": _read(last_good) if last_good else _read(draft),
            "fell_back": fell_back,
            "history": history,
            "last": r}


def history(case) -> list:
    """Every recorded round for a case, oldest first."""
    d = case_dir(case)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir(), key=lambda q: (len(q.name), q.name)):
        if not p.is_dir() or not p.name.startswith("round_"):
            continue
        r = _parse_response(p)
        meta = _read_json(p / "meta.json") or {}
        out.append({"round": int(p.name.split("_")[1]), "dir": str(p),
                    "status": r.get("status"), "one_line": r.get("one_line", ""),
                    "ready": bool(r.get("ready")), "ts": meta.get("ts"),
                    "model": meta.get("model"), "service": meta.get("service"),
                    "queries": len(r.get("queries") or []),
                    "blocking": len(r.get("blocking") or [])})
    return out


def final_report(case):
    """Path to the newest non-empty document for a case (.tex or .md), or None."""
    for h in reversed(history(case)):
        p = report_path(h["dir"])
        if p:
            return p
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_queries(qs):
    lines = []
    for q in qs:
        if isinstance(q, dict):
            lines.append(f"  [{q.get('id','?')}] {q.get('kind','?')}"
                         f"{' BLOCKING' if q.get('blocking') else ''}\n"
                         f"      Q: {q.get('question','')}\n"
                         f"      why: {q.get('why','')}")
        else:
            lines.append(f"  - {q}")
    return "\n".join(lines) or "  (none)"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hybrid mode: claude works, chatgpt writes.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("handoff", help="run the hybrid loop (BLOCKING)")
    h.add_argument("--case", required=True)
    h.add_argument("--draft", required=True, help="the deputy's draft report")
    h.add_argument("--artifact", action="append", default=[],
                   help="a file the writer should verify against (repeatable)")
    h.add_argument("--request", default="", help="the original request, verbatim")
    h.add_argument("--request-file", default="")
    h.add_argument("--note", default="", help="a note from the deputy to the writer")
    h.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    h.add_argument("--model", default=DEFAULT_MODEL)
    h.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    h.add_argument("--answers-file", default="",
                   help="answers to feed to EVERY later round (simple non-interactive mode)")
    h.add_argument("--start-round", type=int, default=1,
                   help="CONTINUE an existing hand-off at this round (with --answers-file "
                        "carrying your answers to the previous round's queries)")
    h.add_argument("--target", default="",
                   help="the INTENDED final deliverable path. The writer only runs for "
                        "a PDF/LaTeX target (.pdf/.tex); anything else returns your draft "
                        "untouched -- a plain .md the deputy writes itself (Case 557).")

    s = sub.add_parser("status", help="the latest round's state")
    s.add_argument("--case", required=True)
    s.add_argument("--json", action="store_true")

    hi = sub.add_parser("history", help="every round for a case")
    hi.add_argument("--case", required=True)
    hi.add_argument("--json", action="store_true")

    q = sub.add_parser("queries", help="the latest round's open queries")
    q.add_argument("--case", required=True)

    a = sub.add_parser("answer", help="record answers for a round")
    a.add_argument("--case", required=True)
    a.add_argument("--round", type=int, required=True)
    grp = a.add_mutually_exclusive_group(required=True)
    grp.add_argument("--text")
    grp.add_argument("--file")

    r = sub.add_parser("report", help="print the path of the final report")
    r.add_argument("--case", required=True)

    sub.add_parser("charter", help="print the writer charter")

    args = ap.parse_args(argv)

    if args.cmd == "charter":
        print(charter_text())
        return 0

    if args.cmd == "handoff":
        req = args.request
        if args.request_file:
            req = _read(args.request_file)
        canned = _read(args.answers_file) if args.answers_file else ""
        fn = (lambda qs, n: canned) if canned else None
        # On a CONTINUE, the canned answers are the input to the very first round
        # we run, not just to the rounds after it.
        seed = canned if (canned and args.start_round > 1) else ""
        res = run(args.case, draft=args.draft, artifacts=args.artifact,
                  rounds=args.rounds, request_text=req, model=args.model,
                  timeout=args.timeout, answer_fn=fn, deputy_note=args.note,
                  start_round=args.start_round, answers=seed, target=args.target)
        print()
        if res.get("skipped"):
            print(f"STATUS      : SKIPPED ({res['reason']})")
            print(f"REPORT      : {res['report']}   (your draft, unchanged)")
            return 0
        print(f"STATUS      : {res['status']}")
        print(f"ROUNDS RUN  : {res['rounds_run']}")
        print(f"REPORT      : {res['report']}"
              + ("   (FELL BACK to the draft — the writer produced nothing)"
                 if res["fell_back"] else ""))
        last = res.get("last") or {}
        if last.get("queries"):
            print("OPEN QUERIES:")
            print(_fmt_queries(last["queries"]))
        return 0 if not res["fell_back"] else 1

    if args.cmd == "status":
        hs = history(args.case)
        if not hs:
            print(f"no hybrid rounds recorded for case {args.case}")
            return 1
        last = hs[-1]
        if args.json:
            print(json.dumps(last, indent=2))
        else:
            print(f"case {args.case} round {last['round']}: {last['status']} "
                  f"({last['blocking']} blocking / {last['queries']} queries)")
            print(f"  {last['one_line']}")
            print(f"  {last['dir']}")
        return 0

    if args.cmd == "history":
        hs = history(args.case)
        if args.json:
            print(json.dumps(hs, indent=2))
        else:
            for h_ in hs:
                print(f"round {h_['round']}: {h_['status']:12s} "
                      f"q={h_['queries']} blocking={h_['blocking']}  "
                      f"{h_['model']}  {h_['one_line'][:60]}")
        return 0

    if args.cmd == "queries":
        hs = history(args.case)
        if not hs:
            print(f"no hybrid rounds for case {args.case}")
            return 1
        print(_fmt_queries(_parse_response(Path(hs[-1]["dir"])).get("queries") or []))
        return 0

    if args.cmd == "answer":
        d = round_dir(args.case, args.round)
        if not d.is_dir():
            print(f"no such round dir: {d}", file=sys.stderr)
            return 1
        (d / "answers.md").write_text(args.text if args.text else _read(args.file))
        print(f"recorded answers -> {d/'answers.md'}")
        return 0

    if args.cmd == "report":
        p = final_report(args.case)
        if not p:
            print(f"no report for case {args.case}", file=sys.stderr)
            return 1
        print(p)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
