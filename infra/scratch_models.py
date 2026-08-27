#!/usr/bin/env python3
"""Canonical model registry for the Posse system (Case 509; multi-service Case 557).

ONE source of truth mapping the short model ALIAS used throughout the system
(precincts.json, WORKER_MODEL / TSOMP_MODEL, the dashboard selectors, the sheriff
config) to (a) the SERVICE that runs it, (b) the EXACT model id passed to that
service's CLI, and (c) the human-readable label shown in the dashboard UI and
email context stamps.

Why this exists (Case 509). `claude --model opus` binds to "the latest opus" at
run time, which can silently drift between versions; the dashboard/emails showed
only the bare alias "opus"/"fable", so Steven could not tell WHICH model a
precinct or deputy actually ran. Pinning the id + label here makes the system
specific: launches pass the exact id, the UI shows e.g. "Opus 5", and updating
to a newer model is a one-line edit in this single file.

It also records which premium aliases carry a separate, model-specific WEEKLY
usage limit, and the model to fall back to when a worker hits it (fable -> opus).
The watchdog uses this to relaunch a fable deputy on opus when the fable weekly
cap is hit (Case 509), since opus has separate capacity.

MULTI-SERVICE (Case 557). A deputy can now run on Anthropic's `claude` CLI or on
OpenAI's `codex` CLI. Every alias therefore carries a `service` ("claude" or
"chatgpt"); ALIASES ARE GLOBALLY UNIQUE across services, because the alias is
what gets stored bare in precincts.json / TSOMP_MODEL / the watchdog roster, and
those stores have no service column. `service_of(alias)` is the single lookup
that tells a launcher WHICH CLI to invoke — see SERVICES below for the per-service
CLI + auth-helper contract.

Kept dependency-free (stdlib only) so both the scratch_* infra AND a
`python3 scratch_models.py <cmd>` shell call from bash launchers can use it.
Model ids/labels are current as of 2026-08; when a vendor ships a new version,
update the id/label/generation below.
"""
import sys

# ---------------------------------------------------------------------------
# SERVICES — the agent backends a deputy can run on (Case 557).
#
# `cli` is the executable a launcher invokes; `auth_helper` is the bash file it
# must SOURCE first (each one exports/clears that vendor's credentials and is the
# single switch between subscription and API-key billing); `auth_env` is the env
# var that flips that switch. `bin_env` names the env var an auth helper sets to
# the RESOLVED absolute path of the CLI, for services whose binary is not
# guaranteed to be on PATH (codex ships inside the VS Code ChatGPT extension).
# ---------------------------------------------------------------------------
SERVICES = {
    "claude": {
        "label": "Claude",
        "vendor": "Anthropic",
        "cli": "claude",
        "auth_helper": "scratch_claude_auth.sh",
        "auth_env": "TSOMP_CLAUDE_AUTH",
        "bin_env": "TSOMP_CLAUDE_BIN",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "vendor": "OpenAI",
        "cli": "codex",
        "auth_helper": "scratch_codex_auth.sh",
        "auth_env": "TSOMP_CODEX_AUTH",
        "bin_env": "TSOMP_CODEX_BIN",
    },
}

# Service ids in the order the UI should offer them. claude is FIRST and is the
# default everywhere: Case 557 added chatgpt as a per-case opt-in, it did not
# change what any existing precinct or deputy runs on.
SERVICE_IDS = ("claude", "chatgpt")
DEFAULT_SERVICE = "claude"

# ---------------------------------------------------------------------------
# MODES — what a CASE actually picks (Case 557, Feng uid=670).
#
# The user-facing choice is NOT "which vendor" but "who does the work and who
# writes it up". Three modes, and only three:
#
#   claude           one agent: Claude works AND writes its own report
#   claude+chatgpt   two agents: Claude works, ChatGPT writes the report, they
#                    iterate (this is hybrid mode — scratch_hybrid.py)
#   chatgpt          one agent: ChatGPT works AND writes its own report
#
# The first pass of this case exposed a raw SERVICE picker (claude|chatgpt) with
# a separate empty "precinct default" entry, so the dropdown read
# "Claude (default) / Claude / ChatGPT" — the first two being the same thing —
# and hybrid mode, the whole point of the request, was not selectable at all
# (it existed only as a CLI tool). MODES is the fix: it is the single vocabulary
# the form, the email tag, and the spec all speak.
#
# `deputy` is the service that RUNS the case; `writer` is the service that owns
# the human-facing report, or None when the deputy writes it itself. Note the
# mode ids for the two single-agent modes are deliberately IDENTICAL to their
# service ids, so an older "service: chatgpt" tag is still a valid mode.
# ---------------------------------------------------------------------------
MODES = {
    "claude": {
        "label": "Claude",
        "deputy": "claude",
        "writer": None,
        "blurb": "Claude does the work and writes the report.",
    },
    "claude+chatgpt": {
        "label": "Claude + ChatGPT",
        "deputy": "claude",
        "writer": "chatgpt",
        "blurb": "Claude does all the work; ChatGPT writes the report; they iterate.",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "deputy": "chatgpt",
        "writer": None,
        "blurb": "ChatGPT does the work and writes the report.",
    },
}

# In the order the UI offers them. claude is preselected — there is NO separate
# "default" entry, because with three concrete choices an empty one is noise.
MODE_IDS = ("claude", "claude+chatgpt", "chatgpt")
DEFAULT_MODE = "claude"

# Spellings accepted from a human (email tag / form post) for the hybrid mode.
# Kept generous because this is the value most likely to be typed by hand.
_MODE_ALIASES = {
    "hybrid": "claude+chatgpt",
    "mixed": "claude+chatgpt",
    "mix": "claude+chatgpt",
    "claude+gpt": "claude+chatgpt",
    "claude+openai": "claude+chatgpt",
    "claude_chatgpt": "claude+chatgpt",
    "claude-chatgpt": "claude+chatgpt",
    "chatgpt+claude": "claude+chatgpt",
    "both": "claude+chatgpt",
}

# The model each service falls back to when a case picks the service but not the
# model (e.g. "service: chatgpt" with no "model:" tag).
SERVICE_DEFAULT_MODEL = {
    "claude": "opus",
    "chatgpt": "terra",
}

# alias -> canonical spec. `service` selects the backend CLI; `id` is the exact
# string handed to that CLI's --model flag; `label` is what the UI/emails show;
# `weekly_limited` marks the premium models that have a separate per-model weekly
# cap on the subscription plan.
#
# The chatgpt entries were taken from the LOCAL authoritative catalog rather than
# from documentation: `codex debug models` renders the exact set this binary +
# this account can actually reach (Case 557). gpt-5.4 / gpt-5.4-mini are in that
# catalog but are deliberately NOT registered here — they retire from Codex on
# 2026-08-31. gpt-5.3-codex is likewise absent: it is already gone from the
# catalog for ChatGPT sign-in, despite still being named in third-party docs.
MODELS = {
    # ---- Anthropic / claude ------------------------------------------------
    "opus": {
        "service": "claude",
        "id": "claude-opus-5",
        "label": "Opus 5",
        "generation": "5",
        "premium": True,
        "weekly_limited": True,
    },
    "fable": {
        "service": "claude",
        "id": "claude-fable-5",
        "label": "Fable 5",
        "generation": "5",
        "premium": True,
        "weekly_limited": True,
    },
    "sonnet": {
        "service": "claude",
        "id": "claude-sonnet-5",
        "label": "Sonnet 5",
        "generation": "5",
        "premium": False,
        "weekly_limited": False,
    },
    "haiku": {
        "service": "claude",
        "id": "claude-haiku-4-5-20251001",
        "label": "Haiku 4.5",
        "generation": "4.5",
        "premium": False,
        "weekly_limited": False,
    },
    # ---- OpenAI / chatgpt (codex CLI) --------------------------------------
    "sol": {
        "service": "chatgpt",
        "id": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "generation": "5.6",
        "premium": True,
        "weekly_limited": True,
        "effort": "high",
        "note": "Latest frontier agentic coding model.",
    },
    "terra": {
        "service": "chatgpt",
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "generation": "5.6",
        "premium": True,
        "weekly_limited": True,
        "effort": "high",
        "note": "Balanced agentic coding model for everyday work.",
    },
    "luna": {
        "service": "chatgpt",
        "id": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "generation": "5.6",
        "premium": False,
        "weekly_limited": False,
        "effort": "medium",
        "note": "Fast and affordable agentic coding model.",
    },
    "gpt55": {
        "service": "chatgpt",
        "id": "gpt-5.5",
        "label": "GPT-5.5",
        "generation": "5.5",
        "premium": True,
        "weekly_limited": True,
        "effort": "high",
        "note": "Frontier model for complex coding, research, and real-world work.",
    },
}

# The canonical short aliases, in the order the UI should offer them.
#
# COMPAT: ALIASES stays CLAUDE-ONLY. It is what the dashboard's precinct-default
# selector and the sheriff-model selector iterate, and Case 557 deliberately did
# not move any precinct onto chatgpt — the new service is a per-case opt-in. Use
# aliases_for(service) or ALL_ALIASES to reach the full set.
ALIASES = ("fable", "opus", "sonnet", "haiku")
CHATGPT_ALIASES = ("sol", "terra", "luna", "gpt55")
ALL_ALIASES = ALIASES + CHATGPT_ALIASES

# When a premium model hits its model-specific WEEKLY cap, relaunch the worker on
# this model instead (it has separate capacity). Case 509: a fable deputy that
# hits the fable weekly limit is moved to opus. opus has no configured fallback
# (if opus is weekly-capped there is no cheaper-and-separate premium tier to move
# to), so it stays on the wait-for-reset path.
#
# Case 557: the chatgpt tiers fall back DOWN their own ladder (sol -> terra ->
# luna). A fallback must never cross services — the two vendors' limits are
# unrelated, and switching service mid-case would change the CLI, the auth, and
# the resume-session format all at once.
WEEKLY_FALLBACK = {
    "fable": "opus",
    "sol": "terra",
    "terra": "luna",
    "gpt55": "terra",
}

# Reverse index: exact id -> alias, so a value read from a relaunch script or a
# `--model claude-opus-5` invocation normalizes back to the canonical alias.
_ID_TO_ALIAS = {spec["id"]: alias for alias, spec in MODELS.items()}


def alias_of(model):
    """Canonical alias for `model`, which may be an alias ('opus') OR a full id
    ('claude-opus-5'). Case-insensitive. Unknown values pass through unchanged
    (never raises) so callers degrade gracefully to today's alias-only behavior."""
    if not model:
        return model
    m = str(model).strip()
    low = m.lower()
    if low in MODELS:
        return low
    if m in _ID_TO_ALIAS:
        return _ID_TO_ALIAS[m]
    if low in _ID_TO_ALIAS:
        return _ID_TO_ALIAS[low]
    return m


def spec(model):
    """The registry dict for `model` (alias or id), or None if unknown."""
    return MODELS.get(alias_of(model))


def resolve_id(model):
    """Exact `claude --model` id for `model` (alias or id). Unknown -> unchanged
    (so a not-yet-registered model still launches as whatever string was given)."""
    s = spec(model)
    return s["id"] if s else model


def label(model):
    """Human label for `model` (alias or id), e.g. 'opus' -> 'Opus 5'. Unknown
    -> the input unchanged, so the UI never shows a blank."""
    s = spec(model)
    return s["label"] if s else (model or "")


def label_with_id(model):
    """'Opus 5 (claude-opus-5)' for tooltips / verbose UI. Unknown -> input."""
    s = spec(model)
    return f"{s['label']} ({s['id']})" if s else (model or "")


def is_weekly_limited(model):
    """True if `model` carries a separate per-model weekly cap (the premium tier)."""
    s = spec(model)
    return bool(s and s.get("weekly_limited"))


def weekly_fallback(model):
    """The alias to relaunch on when `model` hits its weekly cap, or None."""
    return WEEKLY_FALLBACK.get(alias_of(model))


# ---------------------------------------------------------------------------
# SERVICE lookups (Case 557)
# ---------------------------------------------------------------------------

def known(model):
    """True if `model` (alias or exact id) is in the registry."""
    return alias_of(model) in MODELS


def service_of(model):
    """Which service runs `model` (alias or id). Unknown -> DEFAULT_SERVICE, so a
    stale or hand-typed model never routes a launch to the wrong CLI — it lands on
    claude, which is what every pre-Case-557 caller meant."""
    s = spec(model)
    return s.get("service", DEFAULT_SERVICE) if s else DEFAULT_SERVICE


def is_service(model, service):
    """True if `model` runs on `service`."""
    return service_of(model) == service


def normalize_service(service):
    """Canonical service id for `service`, accepting the obvious human spellings
    ('openai', 'gpt', 'codex' -> chatgpt; 'anthropic' -> claude). Unknown or empty
    -> DEFAULT_SERVICE, so a typo can never wedge a launch."""
    if not service:
        return DEFAULT_SERVICE
    s = str(service).strip().lower()
    if s in SERVICES:
        return s
    if s in ("openai", "gpt", "codex", "chat-gpt", "oai"):
        return "chatgpt"
    if s in ("anthropic", "claude-code", "cc"):
        return "claude"
    return DEFAULT_SERVICE


def service_spec(service):
    """The SERVICES dict for `service` (normalized). Never None."""
    return SERVICES[normalize_service(service)]


def service_label(service):
    """'chatgpt' -> 'ChatGPT'."""
    return service_spec(service)["label"]


def aliases_for(service):
    """The registered aliases for `service`, in UI order."""
    svc = normalize_service(service)
    return tuple(a for a in ALL_ALIASES if MODELS[a].get("service", DEFAULT_SERVICE) == svc)


def default_model(service):
    """The model to use when a case picks `service` but no explicit model."""
    return SERVICE_DEFAULT_MODEL.get(normalize_service(service), "opus")


def normalize_mode(mode):
    """Canonical mode id for `mode`, accepting the service spellings and the
    obvious hand-typed hybrid variants ('hybrid', 'claude+gpt', ...). Unknown or
    empty -> DEFAULT_MODE, so a typo degrades to plain Claude rather than
    silently routing a case to a vendor nobody asked for."""
    if not mode:
        return DEFAULT_MODE
    m = str(mode).strip().lower().replace(" ", "")
    if m in MODES:
        return m
    if m in _MODE_ALIASES:
        return _MODE_ALIASES[m]
    # fall back to the service vocabulary ('openai' -> chatgpt -> the chatgpt mode)
    svc = normalize_service(m)
    return svc if svc in MODES else DEFAULT_MODE


def mode_spec(mode):
    """The MODES dict for `mode` (normalized). Never None."""
    return MODES[normalize_mode(mode)]


def mode_label(mode):
    """'claude+chatgpt' -> 'Claude + ChatGPT'."""
    return mode_spec(mode)["label"]


def deputy_service(mode):
    """Which service RUNS the case for `mode` — what the launcher must invoke."""
    return mode_spec(mode)["deputy"]


def writer_service(mode):
    """Which service owns the human-facing report, or None when the deputy writes
    it itself. Only the hybrid mode has one."""
    return mode_spec(mode)["writer"]


def is_hybrid(mode):
    """True if `mode` hands the report to a different service than the deputy."""
    return writer_service(mode) is not None


def modes_for_service(service):
    """The modes whose DEPUTY runs on `service`. Used to keep the model dropdown
    honest: a model is only offerable if some selected mode actually runs it."""
    svc = normalize_service(service)
    return tuple(m for m in MODE_IDS if MODES[m]["deputy"] == svc)


def aliases_for_mode(mode):
    """Every model alias this mode could legitimately use — the deputy's models,
    plus the writer's when the two differ. Drives the create-case Model list, so
    picking a mode never offers a model that mode cannot run."""
    s = mode_spec(mode)
    out = list(aliases_for(s["deputy"]))
    if s["writer"] and s["writer"] != s["deputy"]:
        out += [a for a in aliases_for(s["writer"]) if a not in out]
    return tuple(out)


def resolve(model=None, service=None):
    """Resolve a (model, service) request into the concrete (alias, id, service)
    a launcher needs. This is THE entry point for the launch path.

    Rules, in order:
      * an explicit KNOWN model wins outright and carries its own service — asking
        for 'luna' gets you chatgpt whether or not a service was named;
      * a named service with no model (or an unknown one) falls to that service's
        default model;
      * nothing at all -> the default service's default model.

    Returns (alias, exact_id, service). Never raises.
    """
    if model and known(model):
        a = alias_of(model)
        return a, MODELS[a]["id"], MODELS[a].get("service", DEFAULT_SERVICE)
    svc = normalize_service(service)
    a = default_model(svc)
    return a, MODELS[a]["id"], svc


def effort(model):
    """The reasoning-effort level to run `model` at. Claude models use the
    system-wide 'max'; the chatgpt models carry a per-model level because the
    codex catalog's supported_reasoning_levels differ per model (luna has no
    'ultra', gpt-5.5 stops at 'xhigh')."""
    s = spec(model)
    if not s:
        return "max"
    return s.get("effort", "max")


def _usage():
    print("usage: scratch_models.py {id|label|labelid|alias|service|effort|"
          "weekly-fallback|known|resolve|list|services} [model]", file=sys.stderr)
    return 2


def main(argv):
    if not argv:
        return _usage()
    cmd = argv[0]
    if cmd == "list":
        # `list` with no argument lists EVERY registered model (grouped by
        # service); `list <service>` narrows to one. Pre-Case-557 callers passed
        # no argument and got the 4 claude models — they now also see the chatgpt
        # ones, which is the point of the command.
        want = normalize_service(argv[1]) if len(argv) > 1 else None
        for svc in SERVICE_IDS:
            if want and svc != want:
                continue
            for a in aliases_for(svc):
                s = MODELS[a]
                wk = " weekly-capped" if s.get("weekly_limited") else ""
                fb = WEEKLY_FALLBACK.get(a)
                print(f"{svc:8s} {a:7s} {s['id']:28s} {s['label']}{wk}"
                      + (f"  (weekly-> {fb})" if fb else ""))
        return 0
    if cmd == "services":
        for sid in SERVICE_IDS:
            s = SERVICES[sid]
            dflt = default_model(sid)
            print(f"{sid:8s} {s['label']:8s} cli={s['cli']:8s} auth={s['auth_helper']:24s} "
                  f"default={dflt} ({MODELS[dflt]['id']})")
        return 0
    if cmd == "modes":
        for mid in MODE_IDS:
            m = MODES[mid]
            print(f"{mid:15s} {m['label']:18s} deputy={m['deputy']:8s} "
                  f"writer={m['writer'] or '-':8s} {m['blurb']}")
        return 0
    if len(argv) < 2:
        return _usage()
    model = argv[1]
    if cmd == "id":
        print(resolve_id(model)); return 0
    if cmd == "label":
        print(label(model)); return 0
    if cmd == "labelid":
        print(label_with_id(model)); return 0
    if cmd == "alias":
        print(alias_of(model)); return 0
    if cmd == "service":
        print(service_of(model)); return 0
    if cmd == "mode":
        # `mode <value>` -> three lines a bash launcher reads in one shot:
        # the canonical mode, the DEPUTY service, and the writer service ('' if none).
        mm = normalize_mode(model)
        print(mm); print(MODES[mm]["deputy"]); print(MODES[mm]["writer"] or "")
        return 0
    if cmd == "effort":
        print(effort(model)); return 0
    if cmd == "known":
        # rc-only predicate for bash: `if python3 scratch_models.py known "$M"`
        ok = known(model)
        print("yes" if ok else "no")
        return 0 if ok else 1
    if cmd == "resolve":
        # `resolve <model> [service]` -> three whitespace-free lines a bash
        # launcher can read in one shot: alias, exact id, service.
        a, mid, svc = resolve(model if model != "-" else None,
                              argv[2] if len(argv) > 2 else None)
        print(a); print(mid); print(svc)
        return 0
    if cmd == "weekly-fallback":
        fb = weekly_fallback(model)
        print(fb or "")
        return 0 if fb else 1
    return _usage()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
