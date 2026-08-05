#!/usr/bin/env bash
# scratch_claude_auth.sh — THE single switch for how headless workers authenticate.
#
# SOURCE this file (never execute it) after any env activation and before any
# `claude` invocation. Every launcher sources it via _daemon_env.sh, i.e.:
#     source "$(dirname "${BASH_SOURCE[0]:-$0}")/scratch_claude_auth.sh"
#
# Modes (env var TSOMP_CLAUDE_AUTH, read at source time):
#   subscription  (DEFAULT, since 2026-07-22) — clear ANTHROPIC_API_KEY so the
#                 CLI falls back to the Claude Max OAuth login stored in
#                 ~/.claude/.credentials.json. Usage bills to the operator's Max
#                 subscription and shares its 5-hour / weekly limits with his
#                 interactive sessions. The watchdog already knows how to defer a
#                 limit-blocked worker until the reset.
#   apikey        — export ANTHROPIC_API_KEY from ~/.anthropic_key (the old
#                 behaviour; bills to the Console API account, pay-per-token).
#
# To flip a single launch back to API billing:
#     TSOMP_CLAUDE_AUTH=apikey bash scratch_spawn_worker.sh ...
# To flip everything back, change the default in the `case` below (and any env
# hook you use to export the key, e.g. a conda activate.d script).
#
# NOTE: the raw key still lives only in ~/.anthropic_key (chmod 600, git-ignored)
# and the OAuth tokens only in ~/.claude/.credentials.json (chmod 600). Never
# print or commit either.

case "${TSOMP_CLAUDE_AUTH:-subscription}" in
  apikey|api|key)
    if [ -r "$HOME/.anthropic_key" ]; then
      export ANTHROPIC_API_KEY="$(tr -d ' \t\r\n' < "$HOME/.anthropic_key")"
    else
      echo "[claude-auth] WARN: TSOMP_CLAUDE_AUTH=apikey but ~/.anthropic_key is unreadable" >&2
    fi
    ;;
  *)
    unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
    if [ ! -r "$HOME/.claude/.credentials.json" ]; then
      echo "[claude-auth] WARN: subscription mode but ~/.claude/.credentials.json is missing —" >&2
      echo "[claude-auth]       run \`claude\` interactively once and /login, or workers will fail to auth." >&2
    fi
    ;;
esac
