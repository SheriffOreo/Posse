#!/usr/bin/env bash
# scratch_codex_auth.sh — THE single switch for how headless CHATGPT workers
# authenticate, and the one place that knows where the `codex` binary lives.
# Case 557. This is the exact sibling of scratch_claude_auth.sh; read that file
# first if you are changing either.
#
# SOURCE this file (never execute it) after any env activation and before any
# `codex` invocation. Launchers source it the same relative way as
# scratch_claude_auth.sh:
#     source "$(dirname "${BASH_SOURCE[0]:-$0}")/scratch_codex_auth.sh"
#
# Modes (env var TSOMP_CODEX_AUTH, read at source time):
#   subscription  (DEFAULT) — leave ~/.codex/auth.json in charge, i.e. the
#                 ChatGPT plan OAuth login Steven created with `codex login`.
#                 Usage bills to that plan and shares its limits with his own
#                 Codex sessions, exactly as claude subscription mode shares the
#                 Max limits.
#   apikey        — export CODEX_API_KEY from ~/.openai_key; billing moves to the
#                 OpenAI API account (pay-per-token). OpenAI's own guidance is
#                 that this is the right mode for CI/automation.
#
# To flip a single launch to API billing:
#     TSOMP_CODEX_AUTH=apikey bash scratch_spawn_worker.sh ...
#
# WHAT THIS EXPORTS
#   TSOMP_CODEX_BIN   absolute path to the resolved `codex` executable, or empty
#                     if none was found. Launchers MUST use this rather than a
#                     bare `codex` (see BINARY RESOLUTION below).
#   TSOMP_CODEX_AUTH_MODE  the mode actually taken ('subscription'|'apikey').
#   CODEX_API_KEY / OPENAI_API_KEY   set in apikey mode, CLEARED in subscription
#                     mode so a stray key in the environment can never silently
#                     redirect a subscription-billed case onto paid API tokens.
#
# BINARY RESOLUTION (why this is not just `command -v codex`)
#   On this box codex is NOT on PATH: it ships inside the VS Code ChatGPT
#   extension, at a VERSIONED path that changes on every extension update
#   (~/.vscode-server/extensions/openai.chatgpt-<version>-linux-x64/bin/...).
#   We therefore search a list of candidates and pick the newest matching
#   extension directory. If Steven later installs codex properly (npm -g), that
#   copy is found first and this glob never matters again.
#
# NOTE: the ChatGPT OAuth tokens live only in ~/.codex/auth.json (chmod 600) and
# any API key only in ~/.openai_key (chmod 600, git-ignored). Never print or
# commit either.

# --- binary resolution -----------------------------------------------------
_tsomp_codex_resolve() {
  # 1. an explicit override always wins
  if [ -n "${TSOMP_CODEX_BIN:-}" ] && [ -x "${TSOMP_CODEX_BIN}" ]; then
    printf '%s' "$TSOMP_CODEX_BIN"; return 0
  fi
  # 2. a properly installed codex on PATH
  local p
  p="$(command -v codex 2>/dev/null)"
  if [ -n "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  # 3. common manual install locations
  for p in "$HOME/.npm-global/bin/codex" "$HOME/.local/bin/codex" \
           "/usr/local/bin/codex" "$HOME/.cargo/bin/codex"; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  # 4. the VS Code ChatGPT extension bundle — newest version wins. `sort -V`
  #    orders 26.9.x above 26.10.x wrongly under a plain sort, hence -V.
  p="$(ls -d "$HOME"/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex \
        2>/dev/null | sort -V | tail -1)"
  if [ -n "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  # 5. the same bundle under a local (non-remote) VS Code install
  p="$(ls -d "$HOME"/.vscode/extensions/openai.chatgpt-*/bin/*/codex \
        2>/dev/null | sort -V | tail -1)"
  if [ -n "$p" ] && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  return 1
}

TSOMP_CODEX_BIN="$(_tsomp_codex_resolve || true)"
export TSOMP_CODEX_BIN
unset -f _tsomp_codex_resolve

if [ -z "${TSOMP_CODEX_BIN:-}" ]; then
  echo "[codex-auth] WARN: no \`codex\` executable found — chatgpt deputies cannot launch." >&2
  echo "[codex-auth]       install it (npm i -g @openai/codex) or set TSOMP_CODEX_BIN." >&2
fi

# --- auth mode -------------------------------------------------------------
case "${TSOMP_CODEX_AUTH:-subscription}" in
  apikey|api|key)
    export TSOMP_CODEX_AUTH_MODE=apikey
    if [ -r "$HOME/.openai_key" ]; then
      CODEX_API_KEY="$(tr -d ' \t\r\n' < "$HOME/.openai_key")"
      export CODEX_API_KEY
      export OPENAI_API_KEY="$CODEX_API_KEY"
    else
      echo "[codex-auth] WARN: TSOMP_CODEX_AUTH=apikey but ~/.openai_key is unreadable —" >&2
      echo "[codex-auth]       create it (chmod 600) or drop back to subscription mode." >&2
    fi
    ;;
  *)
    export TSOMP_CODEX_AUTH_MODE=subscription
    # Clear any inherited key so subscription mode really is subscription mode.
    # Without this a key exported elsewhere in the shell would silently move the
    # case onto paid API billing with no signal anywhere in the logs.
    unset CODEX_API_KEY OPENAI_API_KEY
    if [ ! -r "$HOME/.codex/auth.json" ]; then
      echo "[codex-auth] WARN: subscription mode but ~/.codex/auth.json is missing —" >&2
      echo "[codex-auth]       run \`codex login\` once, or chatgpt workers will fail to auth." >&2
    else
      # Pre-flight the OAuth access token, in the spirit of Case 520 but much
      # lighter: the codex access token has a TEN-DAY life (vs hours for
      # Claude's) and the CLI refreshes it itself on use, so there is nothing to
      # single-flight here. All we owe the operator is a warning BEFORE a
      # multi-day-old token strands a headless run with no human at the terminal.
      _tsomp_codex_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
      if command -v python3 >/dev/null 2>&1; then
        python3 - "$HOME/.codex/auth.json" <<'PYEOF' >&2 || true
import base64, json, sys, time
try:
    tok = json.load(open(sys.argv[1]))["tokens"]["access_token"]
    payload = tok.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    if exp:
        left = exp - time.time()
        if left <= 0:
            print("[codex-auth] WARN: the ChatGPT access token EXPIRED "
                  f"{-left/3600:.1f}h ago; codex will try to refresh it, and if "
                  "that fails run `codex login` again.")
        elif left < 48 * 3600:
            print(f"[codex-auth] NOTE: ChatGPT access token expires in {left/3600:.1f}h "
                  "— re-run `codex login` soon to avoid stranding headless runs.")
except Exception:
    pass
PYEOF
      fi
      unset _tsomp_codex_dir
    fi
    ;;
esac
