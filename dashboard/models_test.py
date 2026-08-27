#!/usr/bin/env python3
"""Case 557: tests for the service-aware model registry (models.py) and the
create-case MODE table (who does the work / who writes the report).

Hermetic — spawns nothing, and reads nothing off disk EXCEPT the one mirror test
that diffs this file against the upstream scratch_models.py registry; that test
SKIPS (rather than fails) when the infra repo is not beside this checkout.
Self-contained (no pytest).  Run:  python models_test.py
"""
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import models  # noqa: E402


def test_aliases_is_still_exactly_the_four_claude_models():
    # COMPAT GUARD: ALIASES is what the precinct-default and sheriff-model
    # selectors iterate. Case 557 must NOT have widened it.
    assert models.ALIASES == ("fable", "opus", "sonnet", "haiku"), models.ALIASES
    assert all(models.service_of(a) == "claude" for a in models.ALIASES)


def test_all_aliases_membership():
    assert models.CHATGPT_ALIASES == ("sol", "terra", "luna", "gpt55")
    assert models.ALL_ALIASES == models.ALIASES + models.CHATGPT_ALIASES
    # every alias is registered, and the registry has no alias outside the list
    assert set(models.ALL_ALIASES) == set(models.MODELS), (
        set(models.ALL_ALIASES) ^ set(models.MODELS))
    assert len(set(models.ALL_ALIASES)) == len(models.ALL_ALIASES), "duplicate alias"
    # aliases are globally unique across services -> the two lists cannot overlap
    assert not set(models.ALIASES) & set(models.CHATGPT_ALIASES)


def test_service_of():
    for a in ("fable", "opus", "sonnet", "haiku"):
        assert models.service_of(a) == "claude", a
    for a in ("sol", "terra", "luna", "gpt55"):
        assert models.service_of(a) == "chatgpt", a
    # accepts an exact id, not just the alias
    assert models.service_of("gpt-5.6-terra") == "chatgpt"
    assert models.service_of("claude-opus-5") == "claude"
    # unknown / empty degrade to the default service, never raise
    assert models.service_of("no-such-model") == models.DEFAULT_SERVICE
    assert models.service_of("") == models.DEFAULT_SERVICE
    assert models.service_of(None) == models.DEFAULT_SERVICE


def test_aliases_for():
    assert models.aliases_for("claude") == models.ALIASES
    assert models.aliases_for("chatgpt") == models.CHATGPT_ALIASES
    # the two partition ALL_ALIASES, in UI order
    got = tuple(a for s in models.SERVICE_IDS for a in models.aliases_for(s))
    assert got == models.ALL_ALIASES, got
    # unknown service degrades to the default rather than returning ()
    assert models.aliases_for("bogus") == models.aliases_for(models.DEFAULT_SERVICE)
    assert models.aliases_for("") == models.aliases_for(models.DEFAULT_SERVICE)


def test_default_model():
    assert models.default_model("claude") == "opus"
    assert models.default_model("chatgpt") == "terra"
    assert models.default_model("") == "opus"
    assert models.default_model("bogus") == "opus"
    # whatever a service defaults to must belong to that service
    for s in models.SERVICE_IDS:
        assert models.service_of(models.default_model(s)) == s, s


def test_services_table_is_consistent():
    assert models.DEFAULT_SERVICE == "claude"
    assert models.SERVICE_IDS == ("claude", "chatgpt")
    assert set(models.SERVICE_IDS) == set(models.SERVICES)
    assert models.service_label("claude") == "Claude"
    assert models.service_label("chatgpt") == "ChatGPT"
    assert models.service_label("bogus") == "Claude"      # degrades, never raises
    # every registered model points at a known service
    for a, (_lbl, _mid, svc) in models.MODELS.items():
        assert svc in models.SERVICES, (a, svc)


def test_existing_lookups_still_work():
    # Case 509 behaviour must survive the 3-tuple widening
    assert models.label("opus") == "Opus 5"
    assert models.label("claude-fable-5") == "Fable 5"
    assert models.label("unknown-x") == "unknown-x"
    assert models.label("") == ""
    assert models.model_id("haiku") == "claude-haiku-4-5-20251001"
    assert models.model_id("unknown-x") == "unknown-x"
    assert models.label_with_id("opus") == "Opus 5 (claude-opus-5)"
    assert models.label_with_id("sol") == "GPT-5.6 Sol (gpt-5.6-sol)"
    assert models.label_with_id("unknown-x") == "unknown-x"
    assert models.alias_of("CLAUDE-OPUS-5") == "opus"
    assert models.alias_of("Terra") == "terra"
    assert models.alias_of("unknown-x") == "unknown-x"
    assert models.alias_of(None) is None
    # ids are unique, so alias -> id -> alias round-trips for every model
    for a in models.ALL_ALIASES:
        assert models.alias_of(models.model_id(a)) == a, a


def test_label_with_service():
    # claude is the default -> untagged (no churn in the existing UI)
    assert models.label_with_service("opus") == "Opus 5"
    assert models.label_with_service("fable") == "Fable 5"
    # non-default services are named
    assert models.label_with_service("terra") == "GPT-5.6 Terra (ChatGPT)"
    assert models.label_with_service("gpt55") == "GPT-5.5 (ChatGPT)"
    assert models.label_with_service("gpt-5.6-luna") == "GPT-5.6 Luna (ChatGPT)"
    # unknown model -> unchanged, untagged
    assert models.label_with_service("unknown-x") == "unknown-x"


# --- MODES: the create-case Service picker (Case 557, Feng uid=670) ---------

def test_modes_table_is_exactly_three_no_empty_entry():
    # The bug this fixes: the picker rendered "Claude (default) / Claude / ChatGPT"
    # (an empty option beside an explicit list) and could not select hybrid at all.
    assert models.MODE_IDS == ("claude", "claude+chatgpt", "chatgpt"), models.MODE_IDS
    assert set(models.MODE_IDS) == set(models.MODES)
    assert len(models.MODE_IDS) == 3
    assert all(m for m in models.MODE_IDS), "an EMPTY mode id would re-create the bug"
    assert models.DEFAULT_MODE == "claude"
    assert models.MODE_IDS[0] == models.DEFAULT_MODE, "claude must be preselected"
    # every mode is renderable: label + blurb present, services known
    for m in models.MODE_IDS:
        s = models.MODES[m]
        assert s["label"] and s["blurb"], m
        assert s["deputy"] in models.SERVICES, m
        assert s["writer"] is None or s["writer"] in models.SERVICES, m


def test_mode_labels():
    assert models.mode_label("claude") == "Claude"
    assert models.mode_label("claude+chatgpt") == "Claude + ChatGPT"
    assert models.mode_label("chatgpt") == "ChatGPT"
    # the two single-agent modes reuse their service label verbatim
    assert models.mode_label("claude") == models.service_label("claude")
    assert models.mode_label("chatgpt") == models.service_label("chatgpt")


def test_normalize_mode():
    for m in models.MODE_IDS:
        assert models.normalize_mode(m) == m, m
    # hand-typed hybrid spellings all land on the one canonical id
    for v in ("hybrid", "mixed", "mix", "both", "claude+gpt", "claude+openai",
              "claude_chatgpt", "claude-chatgpt", "chatgpt+claude",
              "Claude + ChatGPT", "  CLAUDE+CHATGPT  "):
        assert models.normalize_mode(v) == "claude+chatgpt", v
    # service spellings name the single-agent modes
    for v in ("openai", "gpt", "codex", "chat-gpt", "oai", "ChatGPT"):
        assert models.normalize_mode(v) == "chatgpt", v
    for v in ("anthropic", "claude-code", "cc", "Claude"):
        assert models.normalize_mode(v) == "claude", v
    # unknown / empty degrade to the default mode, never raise
    for v in ("", None, "bogus", "gemini", 0):
        assert models.normalize_mode(v) == models.DEFAULT_MODE, v
    assert models.mode_spec("hybrid") is models.MODES["claude+chatgpt"]
    assert models.mode_label("hybrid") == "Claude + ChatGPT"


def test_mode_roles():
    # claude: one agent, no separate writer
    assert models.deputy_service("claude") == "claude"
    assert models.writer_service("claude") is None
    assert models.is_hybrid("claude") is False
    # claude+chatgpt: claude works, chatgpt writes the report
    assert models.deputy_service("claude+chatgpt") == "claude"
    assert models.writer_service("claude+chatgpt") == "chatgpt"
    assert models.is_hybrid("claude+chatgpt") is True
    # chatgpt: one agent, no separate writer
    assert models.deputy_service("chatgpt") == "chatgpt"
    assert models.writer_service("chatgpt") is None
    assert models.is_hybrid("chatgpt") is False
    # exactly one hybrid, and only it names a writer
    assert [m for m in models.MODE_IDS if models.is_hybrid(m)] == ["claude+chatgpt"]
    # unknown degrades to the default mode's roles rather than raising
    assert models.deputy_service("bogus") == "claude"
    assert models.is_hybrid("bogus") is False


def test_aliases_for_mode():
    # a mode may only offer models it can actually run
    assert models.aliases_for_mode("claude") == ("fable", "opus", "sonnet", "haiku")
    assert models.aliases_for_mode("chatgpt") == ("sol", "terra", "luna", "gpt55")
    # the hybrid spans both: claude models = the deputy, chatgpt models = the WRITER
    assert models.aliases_for_mode("claude+chatgpt") == models.ALL_ALIASES
    assert len(models.aliases_for_mode("claude+chatgpt")) == 8
    # deputy's models always come first, and no alias is offered twice
    for m in models.MODE_IDS:
        got = models.aliases_for_mode(m)
        assert len(set(got)) == len(got), (m, got)
        assert set(got) <= set(models.ALL_ALIASES), (m, got)
        assert got[:4] == models.aliases_for(models.deputy_service(m)), m
    assert models.aliases_for_mode("hybrid") == models.aliases_for_mode("claude+chatgpt")
    assert models.aliases_for_mode("bogus") == models.aliases_for_mode(models.DEFAULT_MODE)


def _scratch_models():
    """The upstream registry module, or None when the infra repo is not present
    (a fresh operator's checkout has no sibling scratch_models.py)."""
    cands = []
    for env in ("INFRA_SCRATCH_MODELS", "INFRA_STATE_ROOT"):
        v = (os.environ.get(env) or "").strip()
        if v:
            p = Path(v)
            cands.append(p if p.name == "scratch_models.py" else p / "scratch_models.py")
    cands.append(Path.home() / "Projects" / "time-series-omp" / "scratch_models.py")
    src = next((c for c in cands if c.is_file()), None)
    if not src:
        return None
    sys.path.insert(0, str(src.parent))
    try:
        import scratch_models  # noqa: E402
    finally:
        sys.path.pop(0)
    return scratch_models


def test_mode_mirror_matches_scratch_models_registry():
    """This file is a MIRROR: the create-case form, the email tag and the launcher
    must speak one vocabulary. Diff it against the upstream registry."""
    up = _scratch_models()
    if up is None:
        print("      (skipped: no scratch_models.py found)")
        return
    assert models.MODE_IDS == up.MODE_IDS, (models.MODE_IDS, up.MODE_IDS)
    assert models.DEFAULT_MODE == up.DEFAULT_MODE
    assert set(models.MODES) == set(up.MODES)
    for m in up.MODE_IDS:
        mine, theirs = models.MODES[m], up.MODES[m]
        for k in ("label", "deputy", "writer", "blurb"):
            assert mine[k] == theirs[k], (m, k, mine[k], theirs[k])
        # the derived lookups agree too, not just the raw table
        assert models.mode_label(m) == up.mode_label(m), m
        assert models.deputy_service(m) == up.deputy_service(m), m
        assert models.writer_service(m) == up.writer_service(m), m
        assert models.is_hybrid(m) == up.is_hybrid(m), m
        assert models.aliases_for_mode(m) == up.aliases_for_mode(m), m
    # and the mode vocabulary normalizes identically on both sides
    for v in ("hybrid", "both", "claude+gpt", "openai", "anthropic", "bogus", ""):
        assert models.normalize_mode(v) == up.normalize_mode(v), v


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== model registry test suite  ({len(tests)} tests) ===")
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
