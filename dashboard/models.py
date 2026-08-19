"""Model registry for the Posse dashboard (Case 509).

Mirror of the scratch_* infra registry (scratch_models.py) so the dashboard —
a SEPARATE repo that cannot import the operator's scratch_* files — shows the
SPECIFIC Claude model (e.g. "Opus 4.8"), not just the bare alias "opus", wherever
a model appears: the Precincts table, the Sheriff block, the active-deputies
Status rows, and the model selectors on the create-case / precinct-model / sheriff
forms. Keep the ids/labels in sync with scratch_models.py when a model changes.

Display-only (no launch/weekly-limit logic lives here — that is the watchdog's,
in scratch_models.py). Stdlib-free, matching the rest of the dashboard.
"""

# alias -> (label, exact id). Order = how selectors should list them.
MODELS = {
    "fable":  ("Fable 5",    "claude-fable-5"),
    "opus":   ("Opus 4.8",   "claude-opus-4-8"),
    "sonnet": ("Sonnet 4.6", "claude-sonnet-4-6"),
    "haiku":  ("Haiku 4.5",  "claude-haiku-4-5-20251001"),
}
ALIASES = ("fable", "opus", "sonnet", "haiku")

_ID_TO_ALIAS = {mid: a for a, (_, mid) in MODELS.items()}


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
    """'opus' -> 'Opus 4.8'; unknown -> the input unchanged (never blank)."""
    a = alias_of(model)
    return MODELS[a][0] if a in MODELS else (model or "")


def model_id(model):
    """'opus' -> 'claude-opus-4-8'; unknown -> the input unchanged."""
    a = alias_of(model)
    return MODELS[a][1] if a in MODELS else (model or "")


def label_with_id(model):
    """'Opus 4.8 (claude-opus-4-8)'; unknown -> the input unchanged."""
    a = alias_of(model)
    if a in MODELS:
        lbl, mid = MODELS[a]
        return f"{lbl} ({mid})"
    return model or ""
