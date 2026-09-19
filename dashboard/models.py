"""Model registry for the Posse dashboard (Case 509; multi-service Case 557).

Mirror of the scratch_* infra registry (scratch_models.py) so the dashboard —
a SEPARATE repo that cannot import the operator's scratch_* files — shows the
SPECIFIC model (e.g. "Opus 5"), not just the bare alias "opus", wherever
a model appears: the Precincts table, the Sheriff block, the active-deputies
Status rows, and the model selectors on the create-case / precinct-model / sheriff
forms. Keep the ids/labels in sync with scratch_models.py when a model changes.

MULTI-SERVICE (Case 557). A deputy can run on Anthropic's `claude` CLI or on
OpenAI's `codex` CLI, so every alias carries the SERVICE that runs it. Aliases are
GLOBALLY UNIQUE across services, because the alias is what gets stored bare in
precincts.json / a web-case record / the watchdog roster, and those stores have no
service column — `service_of(alias)` recovers it.

Display-only (no launch/weekly-limit logic lives here — that is the watchdog's,
in scratch_models.py). The per-service CLI + auth-helper contract therefore stays
in scratch_models.py too: the dashboard never spawns anything, so it mirrors only
what it renders. Stdlib-free, matching the rest of the dashboard.
"""

# alias -> (label, exact id, service). Order = how selectors should list them.
MODELS = {
    # ---- Anthropic / claude ----
    "fable":  ("Fable 5.1",     "claude-fable-5-1",          "claude"),
    "opus":   ("Opus 5",        "claude-opus-5",             "claude"),
    "sonnet": ("Sonnet 5",      "claude-sonnet-5",           "claude"),
    "haiku":  ("Haiku 4.5",     "claude-haiku-4-5-20251001", "claude"),
    # ---- OpenAI / chatgpt (codex CLI) ----
    "astra":  ("GPT-6 Astra",   "gpt-6-astra",              "chatgpt"),
    "sol":    ("GPT-5.6 Sol",   "gpt-5.6-sol",               "chatgpt"),
    "terra":  ("GPT-5.6 Terra", "gpt-5.6-terra",             "chatgpt"),
    "luna":   ("GPT-5.6 Luna",  "gpt-5.6-luna",              "chatgpt"),
    "gpt55":  ("GPT-5.5",       "gpt-5.5",                   "chatgpt"),
}

# service id -> display label. Ordered as the UI should offer them: claude is
# FIRST and is the default everywhere (Case 557 added chatgpt as a per-case
# opt-in; it did not change what any existing precinct or deputy runs on).
SERVICES = {
    "claude":  {"label": "Claude",  "vendor": "Anthropic"},
    "chatgpt": {"label": "ChatGPT", "vendor": "OpenAI"},
}
SERVICE_IDS = ("claude", "chatgpt")
DEFAULT_SERVICE = "claude"

# The model a case gets when it picks the service but not the model.
SERVICE_DEFAULT_MODEL = {"claude": "opus", "chatgpt": "terra"}

# ---------------------------------------------------------------------------
# MODES — what a CASE picks (Case 557). Mirror of scratch_models.py MODES.
#
# The user-facing choice is NOT "which vendor" but "who does the work and who
# writes it up". Exactly three, and there is deliberately NO empty "default"
# entry: the first pass exposed the raw SERVICE ids plus an empty option, so the
# create-case dropdown read "Claude (default) / Claude / ChatGPT" — the first two
# being the same thing — and hybrid mode, the whole point of the feature, was not
# selectable at all.
#
# `deputy` is the service that RUNS the case; `writer` owns the human-facing
# report, or None when the deputy writes it itself (only the hybrid has one).
# The two single-agent mode ids are deliberately IDENTICAL to their service ids,
# so a pre-mode "service: chatgpt" value is still a valid mode.
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

# In the order the UI offers them; claude is PRESELECTED (no empty entry).
MODE_IDS = ("claude", "claude+chatgpt", "chatgpt")
DEFAULT_MODE = "claude"

# ---------------------------------------------------------------------------
# WORK TYPES — the LANES a case splits into (Case 561). MIRROR of
# scratch_models.WORK_TYPES; models_test.py diffs the two.
#
# A case picks a service+model per lane. The deputy launches on the WORK lane and
# switches ITSELF into the report lane when it starts the deliverable document —
# one case, one context, several models. This REPLACES the Case 557 two-agent
# hybrid as the way to mix vendors on a case.
#
# EMAIL IS NOT REPORT WRITING (Feng uid=674): the report lane is a deliverable
# DOCUMENT, not correspondence. An ACK/milestone/FINAL email is written in
# whatever model is running.
# ---------------------------------------------------------------------------
WORK_TYPES = {
    "work": {
        "label": "Work",
        "blurb": ("thinking, planning, web search, code, experiments, tests, the "
                  "case file, records entries, messages to other agents, and ALL "
                  "email correspondence with Steven"),
    },
    "report": {
        "label": "Human report writing",
        "blurb": ("a deliverable DOCUMENT written for a human to read — a PDF "
                  "report and the LaTeX behind it, a README, a design doc, a "
                  "slide deck; NOT code, NOT the case file, and NOT email "
                  "(an ACK/milestone/FINAL email is never a reason to switch)"),
    },
}
WORK_TYPE_IDS = ("work", "report")
DEFAULT_WORK_TYPE = "work"

# Spellings accepted from a human (email tag / form post) for the hybrid mode —
# generous, because this is the value most likely to be typed by hand.
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

# Service spellings that name a single-agent mode. scratch_models.normalize_mode
# reaches these by falling through to its normalize_service(); mirrored explicitly
# here because the dashboard's _norm_service is deliberately strict (widening it
# would change what service_label()/aliases_for() do on the render paths).
_MODE_SERVICE_ALIASES = {
    "openai": "chatgpt", "gpt": "chatgpt", "codex": "chatgpt",
    "chat-gpt": "chatgpt", "oai": "chatgpt",
    "anthropic": "claude", "claude-code": "claude", "cc": "claude",
}

# COMPAT: ALIASES stays CLAUDE-ONLY for legacy callers that deliberately mean
# Claude. Service-neutral controls (precinct defaults, Sheriff model, create
# case, and deputy switching) use aliases_for(service) or ALL_ALIASES.
ALIASES = ("fable", "opus", "sonnet", "haiku")
CHATGPT_ALIASES = ("astra", "sol", "terra", "luna", "gpt55")
ALL_ALIASES = ALIASES + CHATGPT_ALIASES

# Ids an alias used to resolve to, mirroring scratch_models._LEGACY_IDS. A Status
# row or roster entry written before the bump still carries the old id, and without
# this it would render as the raw string instead of the model's label (Case 759).
_LEGACY_IDS = {
    "claude-fable-5": "fable",
}

_ID_TO_ALIAS = dict(_LEGACY_IDS)
_ID_TO_ALIAS.update({mid: a for a, (_, mid, _svc) in MODELS.items()})


def alias_of(model):
    """Canonical alias for `model` (an alias or a full id); unknown -> unchanged."""
    if not model:
        return model
    m = str(model).strip()
    low = m.lower()
    if low in MODELS:
        return low
    return _ID_TO_ALIAS.get(m) or _ID_TO_ALIAS.get(low) or m


def label(model):
    """'opus' -> 'Opus 5'; unknown -> the input unchanged (never blank)."""
    a = alias_of(model)
    return MODELS[a][0] if a in MODELS else (model or "")


def model_id(model):
    """'opus' -> 'claude-opus-5'; unknown -> the input unchanged."""
    a = alias_of(model)
    return MODELS[a][1] if a in MODELS else (model or "")


def label_with_id(model):
    """'Opus 5 (claude-opus-5)'; unknown -> the input unchanged."""
    a = alias_of(model)
    if a in MODELS:
        lbl, mid, _svc = MODELS[a]
        return f"{lbl} ({mid})"
    return model or ""


# -- service lookups (Case 557) ---------------------------------------------

def _norm_service(service):
    """Canonical service id; unknown/empty -> DEFAULT_SERVICE, so a stale or
    hand-typed value degrades to claude (what every pre-557 caller meant) instead
    of raising in a render path."""
    s = str(service or "").strip().lower()
    return s if s in SERVICES else DEFAULT_SERVICE


def service_of(model):
    """Which service runs `model` (alias or id); unknown -> DEFAULT_SERVICE."""
    a = alias_of(model)
    return MODELS[a][2] if a in MODELS else DEFAULT_SERVICE


def service_label(service):
    """'chatgpt' -> 'ChatGPT'; unknown -> the default service's label."""
    return SERVICES[_norm_service(service)]["label"]


def aliases_for(service):
    """The registered aliases for `service`, in UI order."""
    svc = _norm_service(service)
    return tuple(a for a in ALL_ALIASES if MODELS[a][2] == svc)


def default_model(service):
    """The model to use when a case picks `service` but no explicit model."""
    return SERVICE_DEFAULT_MODEL.get(_norm_service(service), "opus")


def label_with_service(model):
    """Label, tagged with the service when it is NOT claude: 'terra' ->
    'GPT-5.6 Terra (ChatGPT)', 'opus' -> 'Opus 5'. Lets a table cell name the
    backend without a second column (the claude case stays untouched)."""
    svc = service_of(model)
    lbl = label(model)
    return lbl if svc == DEFAULT_SERVICE else f"{lbl} ({service_label(svc)})"


# -- mode lookups (Case 557) -------------------------------------------------

def normalize_mode(mode):
    """Canonical mode id for `mode`, accepting the service spellings and the
    obvious hand-typed hybrid variants ('hybrid', 'claude+gpt', 'Claude + ChatGPT').
    Unknown or empty -> DEFAULT_MODE, so a stale value degrades to plain Claude
    rather than blanking a render path or naming a vendor nobody asked for."""
    if not mode:
        return DEFAULT_MODE
    m = str(mode).strip().lower().replace(" ", "")
    if m in MODES:
        return m
    if m in _MODE_ALIASES:
        return _MODE_ALIASES[m]
    return _MODE_SERVICE_ALIASES.get(m, DEFAULT_MODE)


def mode_spec(mode):
    """The MODES dict for `mode` (normalized). Never None."""
    return MODES[normalize_mode(mode)]


def mode_label(mode):
    """'claude+chatgpt' -> 'Claude + ChatGPT'."""
    return mode_spec(mode)["label"]


def deputy_service(mode):
    """Which service RUNS the case for `mode`."""
    return mode_spec(mode)["deputy"]


def writer_service(mode):
    """Which service owns the human-facing report, or None when the deputy writes
    it itself. Only the hybrid mode has one."""
    return mode_spec(mode)["writer"]


def is_hybrid(mode):
    """True if `mode` hands the report to a different service than the deputy."""
    return writer_service(mode) is not None


def aliases_for_mode(mode):
    """Every model alias this mode could legitimately use — the deputy's models,
    plus the writer's when the two differ. Drives the create-case Model list, so
    picking a mode never offers a model that mode cannot run. In the hybrid, a
    chatgpt pick is the REPORT WRITER's model, not the deputy's."""
    s = mode_spec(mode)
    out = list(aliases_for(s["deputy"]))
    if s["writer"] and s["writer"] != s["deputy"]:
        out += [a for a in aliases_for(s["writer"]) if a not in out]
    return tuple(out)
