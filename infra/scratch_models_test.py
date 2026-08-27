#!/usr/bin/env python3
"""Case 509 — unit tests for the canonical model registry (scratch_models.py).
Run:  python scratch_models_test.py   (exit 0 = PASS)."""
import subprocess
import sys

import scratch_models as M

_fails = []


def check(cond, msg):
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        _fails.append(msg)


def test_resolve_and_label():
    print("resolve_id / label / label_with_id")
    check(M.resolve_id("opus") == "claude-opus-5", "opus -> claude-opus-5")
    check(M.resolve_id("fable") == "claude-fable-5", "fable -> claude-fable-5")
    check(M.label("opus") == "Opus 5", "opus -> 'Opus 5'")
    check(M.label("fable") == "Fable 5", "fable -> 'Fable 5'")
    check(M.label_with_id("opus") == "Opus 5 (claude-opus-5)", "label_with_id opus")


def test_idempotent_and_reverse():
    print("alias_of accepts alias OR id, case-insensitive; ids resolve idempotently")
    check(M.alias_of("claude-opus-5") == "opus", "id -> alias opus")
    check(M.alias_of("OPUS") == "opus", "uppercase alias -> opus")
    check(M.resolve_id("claude-opus-5") == "claude-opus-5", "id passed through unchanged")
    check(M.label("claude-fable-5") == "Fable 5", "label from an id")


def test_unknown_passthrough():
    print("unknown model degrades gracefully (never raises / never blank)")
    check(M.resolve_id("gpt-5") == "gpt-5", "unknown id passes through")
    check(M.label("gpt-5") == "gpt-5", "unknown label = the input")
    check(M.alias_of(None) is None and M.label(None) == "", "None safe")


def test_weekly_fallback():
    print("weekly fallback: fable -> opus; opus has none")
    check(M.weekly_fallback("fable") == "opus", "fable -> opus")
    check(M.weekly_fallback("claude-fable-5") == "opus", "fable id -> opus")
    check(M.weekly_fallback("opus") is None, "opus -> None (no separate premium tier)")
    check(M.is_weekly_limited("fable") and M.is_weekly_limited("opus"),
          "both premium models are weekly-limited")
    check(not M.is_weekly_limited("sonnet"), "sonnet is not weekly-limited")


def test_cli():
    print("CLI: id / label / weekly-fallback / list")
    r = subprocess.run([sys.executable, "scratch_models.py", "id", "opus"],
                       capture_output=True, text=True)
    check(r.stdout.strip() == "claude-opus-5", "`id opus` -> claude-opus-5")
    r = subprocess.run([sys.executable, "scratch_models.py", "weekly-fallback", "fable"],
                       capture_output=True, text=True)
    check(r.stdout.strip() == "opus" and r.returncode == 0, "`weekly-fallback fable` -> opus rc0")
    r = subprocess.run([sys.executable, "scratch_models.py", "weekly-fallback", "opus"],
                       capture_output=True, text=True)
    check(r.stdout.strip() == "" and r.returncode == 1, "`weekly-fallback opus` -> empty rc1")


def test_modes():
    """Case 557 (Feng uid=670): the case-level choice is a MODE, not a bare vendor."""
    check(M.MODE_IDS == ("claude", "claude+chatgpt", "chatgpt"),
          "exactly three modes, claude first")
    check(M.DEFAULT_MODE == "claude", "claude is the default mode")
    check(M.mode_label("claude+chatgpt") == "Claude + ChatGPT", "hybrid label")
    # who runs / who writes
    check(M.deputy_service("claude") == "claude" and M.writer_service("claude") is None,
          "claude mode: claude runs and writes")
    check(M.deputy_service("chatgpt") == "chatgpt" and M.writer_service("chatgpt") is None,
          "chatgpt mode: chatgpt runs and writes")
    check(M.deputy_service("claude+chatgpt") == "claude"
          and M.writer_service("claude+chatgpt") == "chatgpt",
          "hybrid: claude runs, chatgpt writes")
    check(M.is_hybrid("claude+chatgpt") and not M.is_hybrid("claude")
          and not M.is_hybrid("chatgpt"), "only claude+chatgpt is hybrid")


def test_mode_normalization():
    for raw in ("hybrid", "Claude + ChatGPT", "claude+gpt", "CLAUDE+CHATGPT",
                "both", "mixed", "chatgpt+claude"):
        check(M.normalize_mode(raw) == "claude+chatgpt", f"{raw!r} -> claude+chatgpt")
    check(M.normalize_mode("openai") == "chatgpt", "'openai' -> chatgpt mode")
    check(M.normalize_mode("anthropic") == "claude", "'anthropic' -> claude mode")
    # a typo must land on the safe default, never on the other vendor
    check(M.normalize_mode("chatgtp") == "claude", "typo -> claude default")
    check(M.normalize_mode("") == "claude" and M.normalize_mode(None) == "claude",
          "empty/None -> claude default")


def test_aliases_for_mode():
    """The Model dropdown must never offer a model the chosen mode cannot run."""
    check(M.aliases_for_mode("claude") == ("fable", "opus", "sonnet", "haiku"),
          "claude mode offers only claude models")
    check(M.aliases_for_mode("chatgpt") == ("sol", "terra", "luna", "gpt55"),
          "chatgpt mode offers only chatgpt models")
    hy = M.aliases_for_mode("claude+chatgpt")
    check(set(hy) == set(M.ALL_ALIASES) and len(hy) == 8,
          "hybrid offers both sides (deputy + writer)")
    check(hy[:4] == M.ALIASES, "hybrid lists the deputy's models first")


def test_modes_cli():
    r = subprocess.run([sys.executable, "scratch_models.py", "mode", "hybrid"],
                       capture_output=True, text=True)
    check(r.stdout.split() == ["claude+chatgpt", "claude", "chatgpt"],
          "`mode hybrid` -> 3 lines: mode, deputy, writer")
    r = subprocess.run([sys.executable, "scratch_models.py", "mode", "chatgpt"],
                       capture_output=True, text=True)
    check(r.stdout.split() == ["chatgpt", "chatgpt"],
          "`mode chatgpt` -> writer line is empty")
    r = subprocess.run([sys.executable, "scratch_models.py", "modes"],
                       capture_output=True, text=True)
    check(all(m in r.stdout for m in M.MODE_IDS), "`modes` lists all three")


def main():
    for fn in (test_resolve_and_label, test_idempotent_and_reverse,
               test_unknown_passthrough, test_weekly_fallback, test_cli,
               test_modes, test_mode_normalization, test_aliases_for_mode,
               test_modes_cli):
        fn()
    print(f"\n=== SUMMARY: {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED'} ===")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
