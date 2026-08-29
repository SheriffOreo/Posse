#!/usr/bin/env python3
"""Case 561 — MODEL SWITCH: one case, one context, several models.

THE PROBLEM THIS REPLACES
-------------------------
Case 557's `claude+chatgpt` mode answered "who works and who writes" with TWO
AGENTS: a claude deputy, plus a separate chatgpt writer spawned by
scratch_hybrid.py that reads the deputy's artifacts and asks it questions. Feng's
objection (the Case 561 request) is that this gives ONE case TWO contexts, which
is not the design. A case has ONE context. What should change over the life of
that context is the MODEL generating it.

THE MECHANISM
-------------
Modelled directly on the done-sentinel, which is the thing the fleet already
trusts to carry a fact from a dying worker to the watchdog:

  1. the deputy writes a SWITCH REQUEST   -> worker_<name>.switch
  2. the deputy EXITS (ends its turn)
  3. the watchdog sees the request, CONFIRMS the process is really gone, applies
     the switch, and relaunches the same case on the new service+model.

Step 3 is `apply()` below; the watchdog calls it. Nothing here relaunches
anything — this module prepares the ground and reports what it did.

WHAT "CARRY THE CONTEXT" ACTUALLY MEANS (the honest part)
---------------------------------------------------------
There are two cases and they are NOT equally faithful. Saying so is the point:

  SAME SERVICE  (opus -> fable, or terra -> luna)
      LOSSLESS. The session belongs to the CLI, not to the model: we keep the
      session id and change one flag. `claude --resume <sid> --model <new>`
      already runs in production — Case 509 moves a weekly-capped fable deputy
      to opus exactly this way. Nothing is rendered, nothing is dropped.

  CROSS SERVICE (claude <-> chatgpt)
      RECONSTRUCTED, and necessarily lossy. The two CLIs keep their transcripts
      in different stores and formats, and neither can load the other's. So the
      outgoing transcript is RENDERED into a handoff document that seeds a fresh
      session on the target service. What survives: the launch prompt verbatim
      (preamble + case spec), every user/assistant message, and tool calls with
      their results truncated per call. What does NOT survive: the model's own
      reasoning blocks (claude's thinking signatures and codex's encrypted
      reasoning are vendor-private and worthless to the other vendor), and the
      middle of a long transcript once the budget is hit — which is dropped
      explicitly, with a marker saying how much went and where the full file is.

  The seed is deliberately built on worker_<name>_prompt.md — the ORIGINAL launch
  prompt — so the incoming model gets the full worker preamble (discipline, wait
  rules, close protocol) and not just a conversation it cannot act on.

EMAIL IS NOT A REASON TO SWITCH (Feng uid=674)
----------------------------------------------
The tempting definition of the report lane — "anything a human reads" — sweeps in
every ACK, milestone and FINAL email, and a case would then burn a process
restart to write five lines. Correspondence stays in whatever model is running.
`request()` refuses an email-shaped reason; see _EMAIL_REASON_RE.

CLI
---
  request --to <lane|service:model|model> --reason "..."   (deputy; then EXIT)
  status [--worker N] [--json]                             (anyone)
  cancel [--worker N]                                      (deputy)
  lanes  [--worker N]                                      (deputy)
  apply  --worker N [--json]                               (watchdog)
  handoff --worker N [--out F]                             (debug: render only)
  write-lanes --worker N ...                               (spawn-time)
  settings --worker N [--json|--env]                       (Case 576: relaunch)
  restore-lane --worker N [--json]                         (Case 576: follow-up)

CASE 576 — RELAUNCHING AN ENDED CASE ON ITS OWN SETTINGS
--------------------------------------------------------
A follow-up e-mail to a case whose deputy already closed must bring that deputy
back on the settings the case was CREATED with, not on whatever the fleet
defaults to. The lanes record written at spawn is where those settings durably
live, so this module owns the answer:

  settings()      the complete set — both lanes, the mode, the judge (its id AND
                  its service+model), the case number, precinct, requester. The
                  CASE comes from the active-deputies board first, so a deputy
                  that TOOK a follow-up case is not revived stamping the case it
                  closed (17 relaunch scripts had drifted that way).
  restore_lane()  a case that ended in the REPORT lane — the normal end of a
                  split case: write the PDF, send FINAL, close — is sitting on
                  the report model. Follow-up e-mail is WORK, so the deputy goes
                  back to its work lane before the relaunch, through the same
                  apply() the watchdog uses rather than a second copy of it.
"""
import argparse
import json
import os
import re

import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LOGS = REPO_ROOT / "scratch_full_logs"

sys.path.insert(0, str(REPO_ROOT))
import scratch_models  # noqa: E402


def _operator_email() -> str:
    """This instance's operator address, resolved the same way the mailer does
    (daemon env, then infra/operator.json). Empty if the instance has no identity
    configured yet -- callers treat that as 'no requester', never as a default."""
    try:
        import scratch_notify_email
        return scratch_notify_email.OPERATOR_EMAIL or ""
    except Exception:
        return os.environ.get("INFRA_OPERATOR_EMAIL", "").strip()

# Rendered-transcript budget for a CROSS-SERVICE handoff, in characters.
# ~140k chars is roughly 35k tokens: large enough to carry a full working session
# nobody would call "summarised", small enough to leave the incoming model most
# of its window for the work it still has to do.
TRANSCRIPT_BUDGET = int(os.environ.get("TSOMP_SWITCH_BUDGET", "140000"))
# Per tool call / tool result. Tool output is the bulk of any transcript and the
# least valuable per character — the head of a result carries the finding, the
# tail is usually a directory listing.
TOOL_CHARS = 700

_VALID_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# An email-shaped switch reason. Matched against the reason ONLY when the reason
# names no deliverable document (see _DOC_RE) — "write the PDF report I will
# email to Steven" is a legitimate report-lane switch that happens to say
# "email", and refusing it would be the same over-literal reading that made the
# lane definition wrong in the first place.
_EMAIL_REASON_RE = re.compile(
    r"\b(e-?mail|ack(nowledge)?|milestone|final\s+(email|note|update)|reply|"
    r"respond|notify|update\s+steven|write\s+steven|message\s+steven)\b", re.I)
_DOC_RE = re.compile(
    r"\b(pdf|latex|\.tex|tex\b|readme|report\.|design\s+doc|whitepaper|"
    r"paper|deck|slides?|manual|documentation|writeup|write-up)\b", re.I)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def _check_name(name):
    if not name or not _VALID_NAME.match(name):
        raise ValueError(f"bad worker name: {name!r}")
    return name


def switch_path(name):
    return LOGS / f"worker_{_check_name(name)}.switch"


def lanes_path(name):
    return LOGS / f"worker_{_check_name(name)}.lanes.json"


def seed_path(name):
    return LOGS / f"worker_{_check_name(name)}.seed_prompt.md"


def history_path(name):
    return LOGS / f"worker_{_check_name(name)}.switches.jsonl"


def prompt_path(name):
    return LOGS / f"worker_{_check_name(name)}_prompt.md"


def codex_session_path(name):
    return LOGS / f"worker_{_check_name(name)}.codex_session"


def relaunch_path(name):
    return REPO_ROOT / f"scratch_worker_{_check_name(name)}_relaunch.sh"


def claude_transcript_path(session_id):
    """Where the claude CLI keeps a session transcript. The project directory is
    the absolute cwd with every '/' turned into '-'."""
    slug = str(REPO_ROOT).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def codex_transcript_path(session_id):
    """Codex names its rollouts rollout-<ts>-<session_id>.jsonl under a
    year/month/day tree, so the id has to be searched for rather than computed."""
    root = Path.home() / ".codex" / "sessions"
    if not session_id or not root.is_dir():
        return None
    hits = sorted(root.glob(f"**/rollout-*-{session_id}.jsonl"))
    return hits[-1] if hits else None


def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _atomic_write(path, text):
    """tmp + fsync + replace, so a reader never sees a half-written record.
    Same discipline as the Case 541 watchdog roster save."""
    path = Path(path)
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def default_worker():
    """The worker this process belongs to. spawn/relaunch export TSOMP_WORKER;
    the older env names are accepted so a script regenerated from an earlier
    template still resolves."""
    for k in ("TSOMP_WORKER", "WORKER_NAME", "GPU_JOB_OWNER"):
        v = (os.environ.get(k) or "").strip()
        if v and _VALID_NAME.match(v):
            return v
    return None


# --------------------------------------------------------------------------
# lanes
# --------------------------------------------------------------------------
def write_lanes(name, work_model=None, work_service=None,
                report_model=None, report_service=None,
                judge_model=None, judge_service=None,
                case=None, precinct=None, requester=None,
                judge=None, mode=None):
    """Persist a case's work split next to its worker files. Written once at
    spawn; read by the deputy (`lanes`), by `request --to <lane>`, by the
    watchdog when it applies a switch, and (Case 576) by the relaunch of an
    ended case that has to come back on these same settings.

    `judge` is the judge's ID — WHICH judge must sign off. Case 561 stored only
    the judge's service+model, so the id survived nowhere outside the deputy's
    own conversation: for case 567 'vyas' existed only inside a prose paragraph
    of the spec, recoverable by no tool. `mode` is likewise recorded so a revival
    can re-export it (the relaunch used to drop TSOMP_MODE entirely)."""
    ln = scratch_models.lanes(work_model, work_service, report_model, report_service)
    rec = {
        "worker": _check_name(name),
        "case": str(case or ""),
        "precinct": precinct or "",
        "requester": requester or "",
        "mode": scratch_models.normalize_mode(mode) if mode else "",
        "lanes": {wt: ln[wt] for wt in scratch_models.WORK_TYPE_IDS},
        "split": ln["split"],
        "current": "work",          # every case starts in the work lane
        "written_ts": time.time(),
    }
    if judge or judge_model or judge_service:
        j = {"critic": (judge or "").strip()}
        # Only record a service+model when the case actually chose one. Filling in
        # the platform default here would look identical on disk but behave
        # differently: scratch_critic treats a caller-supplied model as an
        # override of the judge's OWN registry model, so a defaulted value would
        # silently demote every judge to the platform default.
        if judge_model or judge_service:
            ja, jid, jsvc = scratch_models.resolve(judge_model, judge_service)
            j.update({"service": jsvc, "model": ja, "id": jid,
                      "effort": scratch_models.effort(ja)})
        rec["judge"] = j
    _atomic_write(lanes_path(name), json.dumps(rec, indent=2))
    return rec


def read_lanes(name):
    rec = _read_json(lanes_path(name))
    if isinstance(rec, dict) and rec.get("lanes"):
        return rec
    return None


def lanes_or_default(name):
    """A worker with no lanes file (every worker spawned before Case 561) is a
    single-lane case: both lanes are whatever it is running now. This is what
    makes the feature backward-compatible without a migration."""
    rec = read_lanes(name)
    if rec:
        return rec
    cur = current_target(name)
    lane = {"service": cur["service"], "model": cur["model"], "id": cur["id"],
            "effort": cur["effort"]}
    return {"worker": name, "case": "", "precinct": "", "requester": "",
            "lanes": {wt: dict(lane, work_type=wt,
                               label=scratch_models.WORK_TYPES[wt]["label"])
                      for wt in scratch_models.WORK_TYPE_IDS},
            "split": False, "current": "work", "synthesized": True}


def current_target(name):
    """What the worker is running RIGHT NOW, read from its relaunch script — the
    one artifact that is always present and always current (the watchdog rewrites
    it on every model change, Case 509)."""
    svc, model = "claude", None
    try:
        s = relaunch_path(name).read_text()
        m = re.search(r'AGENT_SERVICE="([^"]*)"', s)
        if m and m.group(1):
            svc = m.group(1)
        m = re.search(r'export\s+TSOMP_MODEL="([^"]*)"', s)
        if m and m.group(1):
            model = m.group(1)
    except Exception:
        pass
    a, i, s2 = scratch_models.resolve(model, svc)
    return {"service": s2, "model": a, "id": i, "effort": scratch_models.effort(a)}


# --------------------------------------------------------------------------
# request / status / cancel  (deputy side)
# --------------------------------------------------------------------------
def parse_target(spec, name=None):
    """Resolve a --to value into (service, model_alias). Accepts, in order:
         a LANE id        'report'        -> that lane's service+model
         service:model    'chatgpt:terra'
         a bare service   'chatgpt'       -> that service's default model
         a bare model     'terra'         -> carries its own service
    """
    spec = (spec or "").strip().lower()
    if not spec:
        raise ValueError("a --to target is required")
    if spec in scratch_models.WORK_TYPES:
        rec = lanes_or_default(name)
        L = rec["lanes"][spec]
        return L["service"], L["model"], spec
    svc = model = None
    if ":" in spec:
        a, b = spec.split(":", 1)
        svc, model = a.strip(), b.strip()
    elif spec in scratch_models.SERVICE_IDS:
        svc = spec
    else:
        model = spec
    if model and not scratch_models.known(model):
        raise ValueError(
            f"unknown model {model!r}; known: {', '.join(scratch_models.ALL_ALIASES)}")
    if svc and scratch_models.normalize_service(svc) != svc:
        raise ValueError(f"unknown service {svc!r}; known: "
                         f"{', '.join(scratch_models.SERVICE_IDS)}")
    a, _i, s = scratch_models.resolve(model, svc)
    return s, a, None


def _reason_is_email_shaped(reason):
    r = reason or ""
    return bool(_EMAIL_REASON_RE.search(r)) and not _DOC_RE.search(r)


def request(name, to, reason, lane=None):
    """Record a switch request. The deputy must EXIT after this returns ok."""
    _check_name(name)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("--reason is required: it is what the watchdog emails "
                         "Steven and what the incoming model is told it is for")
    if _reason_is_email_shaped(reason):
        raise ValueError(
            "REFUSED — that reason is email correspondence, which is NOT a "
            "reason to switch models (Feng uid=674). ACKs, milestone updates, "
            "the FINAL email and messages to other agents are written in "
            "WHATEVER MODEL YOU ARE RUNNING NOW. Only a deliverable document "
            "(PDF/LaTeX report, README, design doc, deck) crosses into the "
            "report lane. Write the email and carry on.")
    svc, model, lane_id = parse_target(to, name)
    lane = lane or lane_id
    cur = current_target(name)
    if (cur["service"], cur["model"]) == (svc, model):
        raise ValueError(f"already running {svc}:{model} — nothing to switch")
    rec = {
        "worker": name,
        "case": (read_lanes(name) or {}).get("case", os.environ.get("TSOMP_CASE", "")),
        "from_service": cur["service"], "from_model": cur["model"],
        "to_service": svc, "to_model": model,
        "to_id": scratch_models.resolve_id(model),
        "to_effort": scratch_models.effort(model),
        "lane": lane or "",
        "reason": reason,
        "cross_service": cur["service"] != svc,
        "state": "requested",
        "requested_ts": time.time(),
    }
    _atomic_write(switch_path(name), json.dumps(rec, indent=2))
    return rec


def status(name):
    return _read_json(switch_path(name))


def cancel(name):
    p = switch_path(name)
    if p.exists():
        p.unlink()
        return True
    return False


# --------------------------------------------------------------------------
# transcript rendering  (cross-service handoff)
# --------------------------------------------------------------------------
def _clip(s, n):
    s = "" if s is None else str(s)
    s = s.replace("\r", "")
    if len(s) <= n:
        return s
    return s[:n] + f"\n… [+{len(s) - n} chars truncated]"


def _render_claude(path):
    """claude transcript -> list of rendered blocks, oldest first.

    Thinking blocks are dropped on purpose: they are vendor-private (a signature
    blob plus text the other vendor cannot verify or continue) and they are the
    single largest thing in a claude transcript."""
    out = []
    try:
        fh = open(path)
    except Exception:
        return out
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") not in ("user", "assistant"):
                continue
            msg = d.get("message") or {}
            role = msg.get("role") or d.get("type")
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    out.append(f"[{role}]\n{content.strip()}")
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                t = part.get("type")
                if t == "text":
                    if (part.get("text") or "").strip():
                        out.append(f"[{role}]\n{part['text'].strip()}")
                elif t == "tool_use":
                    inp = part.get("input")
                    inp = (inp if isinstance(inp, str)
                           else json.dumps(inp, ensure_ascii=False))
                    out.append(f"[tool: {part.get('name')}]\n{_clip(inp, TOOL_CHARS)}")
                elif t == "tool_result":
                    c = part.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c
                                      if isinstance(x, dict))
                    out.append(f"[tool result]\n{_clip(c, TOOL_CHARS)}")
    return out


def _render_codex(path):
    """codex rollout -> list of rendered blocks, oldest first.

    `reasoning` items carry `encrypted_content` and are skipped for the same
    reason claude's thinking is. `developer` role messages are the harness's own
    injected instructions, not the conversation, so they are skipped too."""
    out = []
    try:
        fh = open(path)
    except Exception:
        return out
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            t = p.get("type")
            if t == "message":
                role = p.get("role") or "?"
                if role == "developer":
                    continue
                txt = "\n".join(
                    x.get("text", "") for x in (p.get("content") or [])
                    if isinstance(x, dict) and x.get("text"))
                if txt.strip():
                    out.append(f"[{role}]\n{txt.strip()}")
            elif t in ("custom_tool_call", "function_call"):
                out.append(f"[tool: {p.get('name')}]\n"
                           f"{_clip(p.get('input') or p.get('arguments'), TOOL_CHARS)}")
            elif t in ("custom_tool_call_output", "function_call_output"):
                o = p.get("output")
                if isinstance(o, list):
                    o = "\n".join(x.get("text", "") for x in o
                                  if isinstance(x, dict))
                out.append(f"[tool result]\n{_clip(o, TOOL_CHARS)}")
    return out


def _is_relaunch_boilerplate(block, launch_prompt):
    """True for a block that is the worker PREAMBLE rather than conversation.

    Every launch and every relaunch feeds the deputy a multi-tens-of-kB prompt:
    the discipline rules, the wait rules, the precinct ledger, the case spec. The
    seed already carries that once, verbatim, as PART 1. Replaying it again for
    each relaunch would spend most of the budget re-stating what the reader is
    already holding — measured on this very case, the first block alone was 60k
    of a 140k budget, which is what left the head empty on the first attempt.

    Detected by content, not position: a long block whose opening lines appear in
    the launch prompt is the same preamble text.
    """
    if not launch_prompt or len(block) < 4000:
        return False
    body = block.split("\n", 1)[-1]
    probe = body[100:500].strip()
    return len(probe) > 100 and probe in launch_prompt


def render_transcript(service, session_id, budget=None, launch_prompt=None):
    """Render a session to text within a character budget.

    When the transcript exceeds the budget the MIDDLE is dropped, not the tail:
    the head holds the task as originally posed and the tail holds the live
    working state, and both matter more than the middle. The drop is announced
    inline — a silently truncated handoff would let the incoming model believe it
    has the whole story.

    Returns (text, meta).
    """
    budget = budget or TRANSCRIPT_BUDGET
    if service == "claude":
        src = claude_transcript_path(session_id)
        blocks = _render_claude(src) if src and src.exists() else []
    else:
        src = codex_transcript_path(session_id)
        blocks = _render_codex(src) if src else []
    raw_n = len(blocks)
    if launch_prompt:
        blocks = [b for b in blocks if not _is_relaunch_boilerplate(b, launch_prompt)]
    meta = {"source": str(src) if src else None, "blocks": len(blocks),
            "preamble_blocks_dropped": raw_n - len(blocks),
            "dropped_blocks": 0, "budget": budget}
    if not blocks:
        meta["empty"] = True
        return "", meta
    joined = "\n\n".join(blocks)
    meta["chars_total"] = len(joined)
    if len(joined) <= budget:
        meta["chars_kept"] = len(joined)
        return joined, meta
    # Keep 35% head / 65% tail: the tail is where the work actually is.
    head_budget = int(budget * 0.35)
    tail_budget = budget - head_budget
    head, hlen, hi = [], 0, 0
    for i, b in enumerate(blocks):
        if hlen + len(b) > head_budget:
            hi = i
            break
        head.append(b)
        hlen += len(b) + 2
    tail, tlen, ti = [], 0, len(blocks)
    for i in range(len(blocks) - 1, hi - 1, -1):
        if tlen + len(blocks[i]) > tail_budget:
            ti = i + 1
            break
        tail.append(blocks[i])
        tlen += len(blocks[i]) + 2
        ti = i
    tail.reverse()
    dropped = max(0, ti - hi)
    meta["dropped_blocks"] = dropped
    meta["chars_kept"] = hlen + tlen
    where = ("This is the MIDDLE of the session; the earliest exchanges are above "
             "and the most recent work is below, both in full."
             if head else
             "This is EVERYTHING BEFORE the most recent work, which follows below "
             "in full. The earlier part of the session is NOT included here — the "
             "case spec and worker preamble are in PART 1, but the early "
             "investigation is not.")
    gap = (f"\n\n===== [{dropped} message/tool blocks "
           f"({len(joined) - hlen - tlen} chars) OMITTED HERE to fit the handoff "
           f"budget of {budget} chars] =====\n"
           f"{where}\nThe complete untruncated transcript is on disk at:\n  {src}\n"
           f"Read it directly if you need something from the omitted span.\n"
           f"=====\n\n")
    return ("\n\n".join(head) + gap + "\n\n".join(tail)) if head else \
           (gap.lstrip("\n") + "\n\n".join(tail)), meta


def build_seed(name, rec):
    """The handoff document that seeds a fresh session on the TARGET service.

    Layered deliberately:
      1. the switch header — who you are now and what you may/may not do;
      2. the ORIGINAL launch prompt verbatim (worker preamble + case spec), so
         the incoming model has the discipline rules, the wait rules, the close
         protocol and the precinct ledger, not just a conversation;
      3. the rendered conversation so far;
      4. the marching order — the deputy's own stated reason for switching.
    """
    lanes_rec = lanes_or_default(name)
    from_svc, from_model = rec["from_service"], rec["from_model"]
    to_svc, to_model = rec["to_service"], rec["to_model"]
    sess = _session_id_for(name, from_svc)
    launch_prompt = ""
    p = prompt_path(name)
    if p.exists():
        try:
            launch_prompt = p.read_text()
        except Exception:
            launch_prompt = ""
    body, meta = render_transcript(from_svc, sess, launch_prompt=launch_prompt)

    lane_lines = []
    for wt in scratch_models.WORK_TYPE_IDS:
        L = lanes_rec["lanes"][wt]
        mark = "  <-- YOU ARE HERE" if (rec.get("lane") == wt) else ""
        lane_lines.append(
            f"  {L.get('label', wt):22s} {L['service']}:{L['model']}"
            f" ({scratch_models.label(L['model'])}){mark}\n"
            f"      {scratch_models.WORK_TYPES[wt]['blurb']}")
    lanes_block = "\n".join(lane_lines)

    if meta.get("empty"):
        provenance = (
            "!! The previous session's transcript could NOT be read"
            f" (looked for: {meta.get('source')}). You are starting from the case"
            " spec and the artifacts on disk ALONE. Say so if you are asked what"
            " happened earlier — do not invent a history you were not given.")
    else:
        provenance = (
            f"Rendered from the {from_svc} session transcript "
            f"({meta['blocks']} blocks, {meta.get('chars_kept', 0)} of "
            f"{meta.get('chars_total', 0)} chars"
            + (f", {meta['dropped_blocks']} blocks omitted from the middle"
               if meta["dropped_blocks"] else ", complete")
            + f").\nFull transcript on disk: {meta.get('source')}\n"
            "The previous model's private reasoning blocks are NOT included "
            "(they are vendor-specific and cannot be replayed here)"
            + (f"; {meta['preamble_blocks_dropped']} repeat(s) of the worker "
               "preamble were folded into PART 1 rather than repeated"
               if meta.get("preamble_blocks_dropped") else "") + ".")

    return f"""\
================== MODEL SWITCH — CONTINUE AN EXISTING CASE ==================
You are the deputy **{name}** on case {rec.get('case') or '(see spec below)'}.
THIS IS NOT A NEW TASK. This case has been running; you are taking over its
context on a different model, at the previous model's own request.

  was running : {from_svc}:{from_model} ({scratch_models.label(from_model)})
  now running : {to_svc}:{to_model} ({scratch_models.label(to_model)})   <- you
  reason given by the previous model:
      {rec.get('reason', '')}

THE WORK SPLIT FOR THIS CASE — each model has a jurisdiction:
{lanes_block}

RULES OF THE SPLIT (these are why you were switched):
  * Stay in your lane. When the work moves OUT of your lane, do not push
    through it — request the switch back and exit:
        python scratch_model_switch.py request --to <work|report> --reason "..."
  * If you are in the REPORT lane and you find you need to change code or a
    design, or run an experiment, switch BACK to the work lane first.
  * NEVER switch models to write an email. ACKs, milestone updates, the FINAL
    email and messages to other agents are written in whatever model is running.
    A switch costs a restart; correspondence is not worth one and is not report
    writing.
  * Everything else about the case is unchanged: same deliverables, same close
    protocol, same done-sentinel.

CONTEXT PROVENANCE — read this before you rely on anything below:
{provenance}
==============================================================================


########## PART 1 of 3 — YOUR ORIGINAL LAUNCH PROMPT (verbatim) ##########
This is exactly what the deputy on this case was given at launch: the worker
discipline, the wait rules, the close protocol, the precinct ledger, and the
case spec. It all still applies to you.

{launch_prompt if launch_prompt else '(the original launch prompt could not be read; work from the transcript below)'}

########## PART 2 of 3 — THE SESSION SO FAR ##########
Below is the conversation that has already happened on this case, oldest first,
rendered from the previous model's transcript. Treat it as YOUR OWN history:
these are the things you already did, found, and decided.

{body if body else '(no transcript available — see the provenance note above)'}

########## PART 3 of 3 — WHAT TO DO NOW ##########
Resume the case from exactly where PART 2 stops. Your first move is the reason
you were switched in:

    {rec.get('reason', '')}

Do NOT restart the case, re-do finished work, or re-send emails already sent.
Do NOT re-introduce yourself to Steven — from his side nothing happened except
that the work continued.

Work synchronously in the foreground, follow THE WAIT RULES, check your mailbox
between steps, and close the case exactly as PART 1 specifies. If the work moves
out of your lane, request a switch back and exit.
"""


def _session_id_for(name, service):
    if service == "chatgpt":
        try:
            return codex_session_path(name).read_text().strip()
        except Exception:
            return ""
    try:
        s = relaunch_path(name).read_text()
        m = re.search(r'claude --resume "([0-9a-fA-F-]{36})"', s)
        if m:
            return m.group(1)
        m = re.search(r'resume=([0-9a-fA-F-]{36})', s)
        if m:
            return m.group(1)
    except Exception:
        pass
    reg = _read_json(REPO_ROOT / "scratch_agents_registry.json", {}) or {}
    return ((reg.get("workers") or {}).get(name) or {}).get("session", "")


# --------------------------------------------------------------------------
# apply  (watchdog side)
# --------------------------------------------------------------------------
def _rewrite_same_service(name, rec):
    """Repoint the relaunch script at a new model of the SAME service.

    Generalises Case 509's switch_relaunch_model: the effort flag has to move
    too, because the chatgpt tiers do not all support the same reasoning levels
    (luna has no 'ultra', gpt-5.5 stops at 'xhigh') — leaving a stale effort
    behind would make the relaunch fail outright, not merely run oddly."""
    p = relaunch_path(name)
    s = p.read_text()
    s = re.sub(r'(export\s+TSOMP_MODEL=")[^"]*(")',
               lambda m: m.group(1) + rec["to_model"] + m.group(2), s)
    s = re.sub(r'(--model\s+)\S+', lambda m: m.group(1) + rec["to_id"], s)
    s = re.sub(r'(--effort\s+)\S+', lambda m: m.group(1) + rec["to_effort"], s)
    s = re.sub(r'(model_reasoning_effort=")[^"]*(")',
               lambda m: m.group(1) + rec["to_effort"] + m.group(2), s)
    _atomic_write(p, s)
    os.chmod(p, 0o775)


def _regen_cross_service(name, rec, lanes_rec):
    """Point the worker at the OTHER service: write the seed, mint/clear the
    target session, and regenerate the relaunch script from the canonical
    template (never hand-patched — scratch_gen_relaunch.sh is the one generator,
    per the Case 170 rule)."""
    seed = build_seed(name, rec)
    _atomic_write(seed_path(name), seed)

    # Last resort is the OPERATOR's own address (identity file / daemon env), never a
    # baked-in one: the relaunch script needs a requester and a wrong address would
    # silently send this instance's mail to whoever the release was built by.
    requester = lanes_rec.get("requester") or _roster_field(name, "requester") \
        or _operator_email()
    precinct = lanes_rec.get("precinct") or os.environ.get("WORKER_PRECINCT", "infra")
    case = lanes_rec.get("case") or rec.get("case") or ""

    # Retire the codex session on ANY cross-service hop, in BOTH directions.
    #   leaving chatgpt : that transcript is now stale — everything done on the
    #                     other service after this point is not in it. Resuming
    #                     it on a later hop back would silently rewind the case
    #                     to the moment we left, losing all the work in between.
    #                     (Caught by test_switch_into_claude_mints_a_new_session.)
    #   entering chatgpt: there must be no id, so the next run cold-starts with
    #                     the seed and captures a fresh one.
    # Renamed rather than deleted: a dead session id costs nothing to keep and is
    # the only breadcrumb back to the old rollout file.
    cp = codex_session_path(name)
    if cp.exists():
        cp.rename(cp.with_name(cp.name + ".prev"))

    if rec["to_service"] == "chatgpt":
        sid = _session_id_for(name, "claude") or str(uuid.uuid4())
    else:
        # claude lets us choose the id, so mint one now and register it.
        sid = str(uuid.uuid4())
        _update_registry_session(name, sid)

    cmd = ["bash", str(REPO_ROOT / "scratch_gen_relaunch.sh"), name, sid,
           requester, rec["to_model"], rec["to_effort"], precinct, str(case),
           rec["to_service"]]
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"scratch_gen_relaunch.sh failed rc={r.returncode}: "
                           f"{(r.stderr or r.stdout)[-500:]}")
    return sid


def _roster_field(name, field):
    jobs = _read_json(LOGS / "watchdog_jobs.json", []) or []
    for j in jobs:
        if isinstance(j, dict) and j.get("name") == name:
            return j.get(field)
    return None


def _update_registry_session(name, sid):
    p = REPO_ROOT / "scratch_agents_registry.json"
    reg = _read_json(p)
    if not isinstance(reg, dict) or "workers" not in reg:
        return False
    w = reg["workers"].get(name)
    if not isinstance(w, dict):
        return False
    w["session"] = sid
    _atomic_write(p, json.dumps(reg, indent=2))
    return True


def apply(name, rec=None):
    """Perform a pending switch. Called by the watchdog AFTER it has confirmed
    the worker process is gone. Does NOT relaunch — the caller does that, so the
    relaunch stays on the watchdog's single well-tested path.

    Returns the completed record (with `state`, `applied_ts`, `session`).
    """
    _check_name(name)
    rec = rec or status(name)
    if not rec:
        raise ValueError(f"no pending switch for {name}")
    lanes_rec = lanes_or_default(name)
    rec["state"] = "applying"
    _atomic_write(switch_path(name), json.dumps(rec, indent=2))

    if rec["from_service"] == rec["to_service"]:
        _rewrite_same_service(name, rec)
        rec["session"] = _session_id_for(name, rec["to_service"])
        rec["context"] = "resumed"          # lossless: same session, new model
    else:
        rec["session"] = _regen_cross_service(name, rec, lanes_rec)
        rec["context"] = "reseeded"         # rendered handoff into a new session
        rec["seed_chars"] = seed_path(name).stat().st_size

    # The lane the case is now in — what the Status page shows.
    if rec.get("lane"):
        lanes_rec["current"] = rec["lane"]
        lanes_rec.setdefault("lanes", {})
        _atomic_write(lanes_path(name), json.dumps(lanes_rec, indent=2))

    rec["state"] = "applied"
    rec["applied_ts"] = time.time()
    with open(history_path(name), "a") as f:
        f.write(json.dumps(rec) + "\n")
    switch_path(name).unlink(missing_ok=True)
    return rec


# --------------------------------------------------------------------------
# Case 576 — the settings an ENDED case must be revived on
# --------------------------------------------------------------------------
def _board_case(name):
    """The case this deputy is CURRENTLY on, per the active-deputies board.

    The board is the Task-382 source of truth precisely because a deputy that
    TAKES a follow-up case updates it, while the relaunch script keeps the case
    it was spawned with. Reading the script instead is what put a wrong case in
    29 already-sent e-mail stamps."""
    try:
        import scratch_deputy_state
        return str((scratch_deputy_state.get(name) or {}).get("case") or "")
    except Exception:
        return ""


def _relaunch_case(name):
    """The case baked into the relaunch script — the last-resort fallback."""
    try:
        m = re.search(r'export\s+TSOMP_CASE="([^"]*)"', relaunch_path(name).read_text())
        return m.group(1) if m else ""
    except Exception:
        return ""


def _judge_from_prompt(name):
    """Recover the judge ID of a case created before Case 576 persisted it.

    Every judged case still HAS the id — inside the JUDGE PROTOCOL header of its
    launch prompt — so this makes the fix retroactive instead of applying only to
    cases spawned from now on. Parsed by scratch_critic, which also writes it."""
    try:
        import scratch_critic
        p = LOGS / f"worker_{name}_prompt.md"
        return scratch_critic.judge_of_prompt(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def settings(name):
    """Everything a revival of this case has to be given back.

    Merged from the durable lanes record (Case 561, extended by Case 576 with the
    judge id and the mode), with the case number resolved board-first. Works for
    a worker that predates the lanes file too: lanes_or_default() synthesises the
    split from whatever the relaunch script is running now."""
    _check_name(name)
    rec = lanes_or_default(name)
    work = rec["lanes"]["work"]
    report = rec["lanes"]["report"]
    judge = rec.get("judge") or {}
    critic = judge.get("critic") or _judge_from_prompt(name)
    return {
        "worker": name,
        "case": _board_case(name) or str(rec.get("case") or "") or _relaunch_case(name),
        "precinct": rec.get("precinct") or "",
        "requester": rec.get("requester") or "",
        "mode": rec.get("mode") or "",
        "split": bool(rec.get("split")),
        "current_lane": rec.get("current") or "work",
        "work": work,
        "report": report,
        "judge": critic,
        "judge_service": judge.get("service") or "",
        "judge_model": judge.get("model") or "",
        "synthesized": bool(rec.get("synthesized")),
    }


# The env a relaunch must carry. Kept beside settings() so the two cannot drift.
# Read TODAY by scratch_notify_email (TSOMP_CASE, WORKER_PRECINCT -> the
# [precinct|case|deputy|model] stamp) and scratch_hybrid (TSOMP_HYBRID_MODEL).
# The judge and report-lane names have no reader yet: they are recorded for the
# deputy and for tooling that does not consume them so far. In particular the
# JUDGE is NOT restored through the environment — it survives because the JUDGE
# PROTOCOL block is part of the launch prompt the relaunch replays.
#
# TSOMP_MODEL and TSOMP_SERVICE are deliberately ABSENT. They must describe the
# model the script is about to RUN, and this block is evaluated after the baked
# literals, so sourcing them from the lanes record would overwrite the truth with
# a configuration: web_575 runs chatgpt:sol in its report lane, and a work-lane
# value here would make it sign every e-mail as claude:fable. It would also undo
# Case 509 — switch_relaunch_model rewrites the baked TSOMP_MODEL to move a
# weekly-capped worker, and this eval would reset it on the next relaunch. The
# baked literal is correct by construction: _rewrite_same_service and
# _regen_cross_service both move it in lockstep with `--model`.
_SETTINGS_ENV = (
    ("TSOMP_CASE",         lambda s: s["case"]),
    ("WORKER_PRECINCT",    lambda s: s["precinct"]),
    ("TSOMP_MODE",         lambda s: s["mode"]),
    ("TSOMP_REPORT_MODEL", lambda s: s["report"]["model"]),
    ("TSOMP_REPORT_SERVICE", lambda s: s["report"]["service"]),
    ("TSOMP_HYBRID_MODEL", lambda s: s["report"]["model"] if s["split"] else ""),
    ("TSOMP_JUDGE",        lambda s: s["judge"]),
    ("TSOMP_JUDGE_MODEL",  lambda s: s["judge_model"]),
    ("TSOMP_JUDGE_SERVICE", lambda s: s["judge_service"]),
)


def settings_env(name, settings_rec=None):
    """Shell `export` lines restoring those settings. Emitted for eval by the
    relaunch path; only non-empty values are exported so this never blanks a
    variable the caller legitimately set."""
    s = settings_rec or settings(name)
    out = []
    for var, get in _SETTINGS_ENV:
        v = str(get(s) or "").strip()
        if v:
            out.append("export %s=%s" % (var, _shq(v)))
    return "\n".join(out)


def _shq(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


# Block injected into an already-generated relaunch script. Text, not a template:
# this is written into the FINAL script, so nothing here is re-expanded.
# The presence test is the ASSIGNMENT, not a comment: scratch_gen_relaunch.sh
# emits the same mechanism under its own wording, and matching on prose would
# make this inject a second, redundant copy into every freshly generated script.
_ENV_MARKER = "_SETTINGS_ENV="
_ENV_BLOCK = """# Case 576: restore this case's settings at RUN time (scratch_model_switch.settings).
_SETTINGS_ENV="$(python3 scratch_model_switch.py settings --worker '{name}' --env 2>/dev/null)"
[ -n "$_SETTINGS_ENV" ] && eval "$_SETTINGS_ENV"
unset _SETTINGS_ENV
"""


def refresh_relaunch_env(name):
    """Teach an ALREADY-GENERATED relaunch script to restore its case's settings.

    scratch_gen_relaunch.sh now emits this block, but 401 relaunch scripts were
    generated before it did, and regenerating them wholesale would risk the one
    thing that must never be wrong — the session id each resumes. So the block is
    inserted in place, after the last baked export, using the same targeted
    rewrite `_rewrite_same_service` has used in production since Case 561.

    Because the block is evaluated at run time and comes LAST, it also corrects
    the stale literals above it (chiefly TSOMP_CASE, which is frozen at spawn and
    wrong for any deputy that took a follow-up case).

    Idempotent — returns True only when the file was actually changed.
    """
    _check_name(name)
    p = relaunch_path(name)
    if not p.exists():
        return False
    s = p.read_text(encoding="utf-8", errors="replace")
    if _ENV_MARKER in s:
        return False
    anchor = f'export TSOMP_WORKER="{name}"\n'
    if anchor not in s:
        anchor = next((a for a in (f'export TSOMP_MODEL=', f'export TSOMP_CASE=')
                       if a in s), None)
        if not anchor:
            return False                     # unrecognised script: leave it alone
        line = s[s.index(anchor):]
        anchor = line[:line.index("\n") + 1]
    block = _ENV_BLOCK.format(name=name)
    _atomic_write(p, s.replace(anchor, anchor + block, 1))
    os.chmod(p, 0o775)
    return True


def restore_lane_reason(name, lane="work", require_closed=True):
    """Why restore_lane() would decline — its five None paths are NOT the same
    thing, and two of them are deliberate REFUSALS, not no-ops.

    The checks below MIRROR restore_lane's order exactly, and take the same
    require_closed: a reason that tests different things in a different order
    from the decision it explains is worse than no reason at all."""
    if require_closed and not (LOGS / f"worker_{name}.done").exists():
        return "refused: no done-sentinel (not a closed case — a crashed deputy keeps its lane)"
    rec = read_lanes(name)
    if not rec or not rec.get("split"):
        return "nothing to restore: one model for everything (no split)"
    if (rec.get("current") or "work") == lane:
        return f"nothing to restore: already on the {lane} lane"
    if switch_path(name).exists():
        return "refused: a switch is already pending (never raced)"
    # restore_lane's fifth path: the lanes record disagrees with the script, which
    # already runs the target model (e.g. a Case-509 weekly-cap move).
    return f"nothing to restore: the relaunch script already runs the {lane} lane"


def restore_lane(name, lane="work", require_closed=True):
    """Put an ENDED deputy back on the given lane before a follow-up relaunch.

    Why this exists: `current` is only ever advanced by a switch and is never
    reset, so a split case that ended in the REPORT lane — which is the NORMAL
    end of one (write the PDF, send FINAL, close) — has its relaunch script
    pointed at the report model. A follow-up e-mail is WORK, so reviving it there
    would hand the work to the report writer, inverting the split the case was
    configured with.

    Scoped to a CLOSED case, and enforced here rather than trusted to call sites:
    `require_closed` demands the done-sentinel, because a deputy that CRASHED
    mid-report has no sentinel and must be resumed exactly where it was. That
    distinction is not cosmetic — for a split cross-service case this is not a
    flag flip: apply() takes _regen_cross_service, which retires the codex
    session, mints a new one and rebuilds the script from a lossy rendered
    handoff. Demoting a crashed mid-report deputy that way would destroy work.

    Returns the applied record, or None when nothing needed to change (the common
    case: an unsplit case, or one that ended in the work lane).
    """
    _check_name(name)
    if require_closed and not (LOGS / f"worker_{name}.done").exists():
        return None                      # not a closed case: leave the lane alone
    rec = read_lanes(name)
    if not rec or not rec.get("split"):
        return None                      # one model for everything: nothing to restore
    if (rec.get("current") or "work") == lane:
        return None                      # already there
    if switch_path(name).exists():
        return None                      # a real switch is pending — never race it
    L = rec["lanes"][lane]
    cur = current_target(name)
    if (cur["service"], cur["model"]) == (L["service"], L["model"]):
        return None                      # script already points at the lane
    # Built here rather than through request(): request() refuses an e-mail-shaped
    # reason (Feng uid=674), and "a follow-up email arrived" is exactly that shape
    # — yet this is not a deputy choosing to switch, it is the system restoring a
    # configured setting. apply() does the actual work, so the same-service and
    # cross-service paths stay the watchdog's single tested implementation.
    switch = {
        "worker": name,
        "case": str(rec.get("case") or ""),
        "from_service": cur["service"], "from_model": cur["model"],
        "to_service": L["service"], "to_model": L["model"],
        "to_id": scratch_models.resolve_id(L["model"]),
        "to_effort": scratch_models.effort(L["model"]),
        "lane": lane,
        "reason": f"follow-up on a closed case: restoring the {lane} lane",
        "cross_service": cur["service"] != L["service"],
        "state": "requested",
        "requested_ts": time.time(),
        "origin": "case576_followup",
    }
    _atomic_write(switch_path(name), json.dumps(switch, indent=2))
    try:
        return apply(name, switch)
    except Exception:
        # apply() stamps state="applying" onto the record BEFORE doing the work, so
        # a failure mid-way leaves it on disk — and because switch_pending() now
        # ignores this origin, the watchdog can no longer pick it up as the backstop
        # it is for a deputy-initiated switch. Clearing it here restores that
        # invariant: a failed restore leaves NOTHING pending. The caller relaunches
        # on the un-restored lane, which is exactly the pre-576 behaviour, rather
        # than stranding the case half-switched with nothing able to finish it.
        switch_path(name).unlink(missing_ok=True)
        raise


def describe(rec):
    """One line for an email / a log."""
    arrow = (f"{rec['from_service']}:{rec['from_model']} -> "
             f"{rec['to_service']}:{rec['to_model']}")
    how = {"resumed": "same session, model swapped (lossless)",
           "reseeded": "new session seeded with the rendered transcript"}.get(
        rec.get("context"), rec.get("context", "?"))
    lane = f" [lane: {rec['lane']}]" if rec.get("lane") else ""
    return f"{arrow}{lane} — {how}"


# --------------------------------------------------------------------------
# deputy-facing prompt text (single source of truth, Case 551's pattern)
# --------------------------------------------------------------------------
def protocol_block(case, lanes_rec):
    """The '## Work split' section injected into a deputy's task spec.

    One source of truth so the web form, an email-tagged case and a direct spawn
    all describe the split identically."""
    rows = []
    for wt in scratch_models.WORK_TYPE_IDS:
        L = lanes_rec["lanes"][wt]
        rows.append(f"  {L.get('label', wt):22s} -> {L['service']}:{L['model']} "
                    f"({scratch_models.label(L['model'])})\n"
                    f"      {scratch_models.WORK_TYPES[wt]['blurb']}")
    table = "\n".join(rows)
    if not lanes_rec.get("split"):
        return f"""\
This case runs on ONE model for everything:

{table}

Both lanes are the same model, so you will never need to switch. (The system
supports switching mid-case — `python scratch_model_switch.py request` — but this
case was not configured to split, so just do the work.)
"""
    work = lanes_rec["lanes"]["work"]
    rep = lanes_rec["lanes"]["report"]
    return f"""\
This case is SPLIT ACROSS TWO MODELS. Each has a jurisdiction and you move
between them by switching YOUR OWN model — there is no second agent, no
hand-off to anyone else. It is one case, one context, and you are all of it.

{table}

You are running the WORK model right now.

HOW TO SWITCH (this is the whole mechanism):

    python scratch_model_switch.py request --to report --reason "<why, one line>"
    # then STOP — end your turn immediately, exit the process.

You write the request; you exit; the watchdog confirms you are really gone,
carries your context to the other model, and starts you again there. You will
come back with the conversation so far in front of you and a header telling you
which lane you are in. Switch back the same way: `--to work`.

WHEN TO SWITCH — the jurisdiction rule:
  * You have finished the investigation/code/experiments and are about to start
    writing the deliverable DOCUMENT ({rep['service']}:{rep['model']} owns that)
        -> switch --to report
  * You are mid-report and discover you must change code or a design, or run
    another experiment ({work['service']}:{work['model']} owns that)
        -> switch --to work, do it, then switch --to report again
  * The point is that each model stays inside its own jurisdiction. Do not
    "just quickly" write the report in the work model, and do not "just quickly"
    patch code in the report model.

WHEN **NOT** TO SWITCH — read this twice:
  * NEVER switch to write an email. Your ACK, your milestone updates, your FINAL
    email to Steven, and any message to another agent are written in WHATEVER
    MODEL YOU ARE RUNNING AT THE TIME. Email is correspondence, not a
    deliverable document. `request` will refuse an email-shaped reason.
  * The case file, the ledger/log entries, code comments and commit-style notes
    are WORK, not report writing — they are read by agents, not by a human
    sitting down with a document.
  * A switch costs a process restart and a context rehydration. Two or three in
    a case is normal. Ten means you are using it as a mood ring.

CROSS-VENDOR HONESTY: switching between claude and chatgpt cannot resume a
session (the two CLIs have separate transcript stores), so your context is
RENDERED into a handoff document and replayed into a fresh session. Your
messages and tool calls survive; the previous model's private reasoning does
not, and a very long session has its middle dropped with an explicit marker
naming the file that still holds it in full. Before you switch, make sure
anything that must survive is ON DISK (notes, drafts, the case file) rather than
only in your head. Switching between two models of the SAME vendor is lossless.
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _resolve_name(args):
    n = getattr(args, "worker", None) or default_worker()
    if not n:
        print("ERR: could not determine the worker name — pass --worker NAME "
              "(or export TSOMP_WORKER)", file=sys.stderr)
        raise SystemExit(2)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scratch_model_switch.py",
                                 description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("request", help="ask the watchdog to switch your model, then EXIT")
    p.add_argument("--worker")
    p.add_argument("--to", required=True,
                   help="lane (work|report), service:model, service, or model")
    p.add_argument("--reason", required=True)
    p.add_argument("--lane", help="override the lane this switch enters")

    for c, h in (("status", "show the pending switch"),
                 ("cancel", "delete the pending switch"),
                 ("lanes", "show this case's work split")):
        p = sub.add_parser(c, help=h)
        p.add_argument("--worker")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("apply", help="(watchdog) perform the pending switch")
    p.add_argument("--worker", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("handoff", help="(debug) render the handoff seed only")
    p.add_argument("--worker", required=True)
    p.add_argument("--out")

    p = sub.add_parser("protocol", help="(spawn) the deputy-facing work-split text")
    p.add_argument("--worker")
    p.add_argument("--case", default="")
    p.add_argument("--work-model"); p.add_argument("--work-service")
    p.add_argument("--report-model"); p.add_argument("--report-service")

    p = sub.add_parser("write-lanes", help="(spawn) persist a case's work split")
    p.add_argument("--worker", required=True)
    p.add_argument("--work-model"); p.add_argument("--work-service")
    p.add_argument("--report-model"); p.add_argument("--report-service")
    p.add_argument("--judge-model"); p.add_argument("--judge-service")
    p.add_argument("--judge"); p.add_argument("--mode")
    p.add_argument("--case"); p.add_argument("--precinct"); p.add_argument("--requester")

    # Case 576: what a revival of this case must be given back.
    p = sub.add_parser("settings", help="(relaunch) this case's durable settings")
    p.add_argument("--worker", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--env", action="store_true",
                   help="shell export lines, for eval by a relaunch script")

    p = sub.add_parser("restore-lane",
                       help="(follow-up relaunch) put an ended deputy back on a lane")
    p.add_argument("--worker", required=True)
    p.add_argument("--lane", default="work", choices=list(scratch_models.WORK_TYPE_IDS))
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "request":
        name = _resolve_name(a)
        try:
            rec = request(name, a.to, a.reason, a.lane)
        except ValueError as e:
            print(f"{e}", file=sys.stderr)
            return 2
        print(f"SWITCH REQUESTED: {describe(dict(rec, context='pending'))}")
        print(f"  signal: {switch_path(name)}")
        print("\nNOW EXIT. End your turn immediately — do not start new work, do "
              "not send an email about this.\nThe watchdog will confirm you are "
              "gone, carry your context across, and start you again on "
              f"{rec['to_service']}:{rec['to_model']}.")
        return 0

    if a.cmd == "status":
        name = _resolve_name(a)
        rec = status(name)
        if a.json:
            print(json.dumps(rec or {}, indent=2)); return 0
        if not rec:
            cur = current_target(name)
            print(f"{name}: no pending switch (running "
                  f"{cur['service']}:{cur['model']})")
            return 0
        print(f"{name}: {rec['state']} — {describe(rec)}\n  reason: {rec['reason']}")
        return 0

    if a.cmd == "cancel":
        name = _resolve_name(a)
        print("cancelled" if cancel(name) else "no pending switch")
        return 0

    if a.cmd == "lanes":
        name = _resolve_name(a)
        rec = lanes_or_default(name)
        if a.json:
            print(json.dumps(rec, indent=2)); return 0
        cur = current_target(name)
        print(f"{name}  case={rec.get('case') or '?'}  "
              f"running={cur['service']}:{cur['model']}  "
              f"split={'yes' if rec.get('split') else 'no'}")
        for wt in scratch_models.WORK_TYPE_IDS:
            L = rec["lanes"][wt]
            mark = " *" if (L["service"], L["model"]) == (cur["service"], cur["model"]) else "  "
            print(f" {mark} {wt:8s} {L['service']}:{L['model']:6s} "
                  f"{scratch_models.label(L['model'])}")
        if rec.get("judge"):
            j = rec["judge"]
            # Case 576: a case may name a judge without choosing its model, so
            # service/model are optional keys now — .get(), not [].
            who = j.get("critic") or "(judge)"
            how = (f"{j['service']}:{j['model']}" if j.get("model")
                   else "its own registered model")
            print(f"    judge    {who} on {how}")
        return 0

    if a.cmd == "apply":
        try:
            rec = apply(a.worker)
        except Exception as e:
            print(f"ERR: {e}", file=sys.stderr)
            return 1
        print(json.dumps(rec, indent=2) if a.json else describe(rec))
        return 0

    if a.cmd == "handoff":
        rec = status(a.worker)
        if not rec:
            cur = current_target(a.worker)
            other = "chatgpt" if cur["service"] == "claude" else "claude"
            rec = {"worker": a.worker, "case": "", "reason": "(dry run)",
                   "from_service": cur["service"], "from_model": cur["model"],
                   "to_service": other,
                   "to_model": scratch_models.default_model(other), "lane": "report"}
        text = build_seed(a.worker, rec)
        if a.out:
            Path(a.out).write_text(text)
            print(f"{a.out} ({len(text)} chars)")
        else:
            print(text)
        return 0

    if a.cmd == "protocol":
        # Explicit lane args win (spawn time, before any lanes file exists);
        # otherwise fall back to the worker's persisted split.
        if a.work_model or a.work_service or a.report_model or a.report_service:
            ln = scratch_models.lanes(a.work_model, a.work_service,
                                      a.report_model, a.report_service)
            rec = {"lanes": {wt: ln[wt] for wt in scratch_models.WORK_TYPE_IDS},
                   "split": ln["split"]}
        else:
            rec = lanes_or_default(_resolve_name(a))
        print(protocol_block(a.case, rec))
        return 0

    if a.cmd == "write-lanes":
        rec = write_lanes(a.worker, a.work_model, a.work_service,
                          a.report_model, a.report_service,
                          a.judge_model, a.judge_service,
                          a.case, a.precinct, a.requester,
                          judge=a.judge, mode=a.mode)
        print(json.dumps(rec, indent=2))
        return 0

    if a.cmd == "settings":
        s = settings(a.worker)
        if a.env:
            print(settings_env(a.worker, s)); return 0
        if a.json:
            print(json.dumps(s, indent=2)); return 0
        print(f"{a.worker}  case={s['case'] or '?'}  precinct={s['precinct'] or '?'}  "
              f"mode={s['mode'] or '(default)'}  split={'yes' if s['split'] else 'no'}"
              f"{'  [synthesized: no lanes file]' if s['synthesized'] else ''}")
        for wt in scratch_models.WORK_TYPE_IDS:
            L = s[wt]
            mark = " *" if wt == s["current_lane"] else "  "
            print(f" {mark} {wt:8s} {L['service']}:{L['model']}")
        print(f"    judge    {s['judge'] or '(none)'}"
              + (f" on {s['judge_service']}:{s['judge_model']}" if s["judge_model"] else ""))
        return 0

    if a.cmd == "restore-lane":
        changed = refresh_relaunch_env(a.worker)
        rec = restore_lane(a.worker, a.lane)
        if a.json:
            print(json.dumps({"lane": rec or {}, "env_block_added": changed,
                              "reason": restore_lane_reason(a.worker, a.lane)}, indent=2))
            return 0
        if changed:
            print(f"{a.worker}: relaunch script taught to restore its settings")
        # A bare "nothing to restore" hid five different outcomes, two of which
        # (no done-sentinel; a switch already pending) are deliberate REFUSALS
        # rather than no-ops.
        print(f"restored: {describe(rec)}" if rec
              else f"{a.worker}: {restore_lane_reason(a.worker, a.lane)}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
