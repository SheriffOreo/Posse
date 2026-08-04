# infra_env.sh — your Posse instance's configuration (non-secret).
#
# Source this in the shell you start the daemons + dashboard from; the tmux
# daemons inherit this environment. See ONBOARDING.md for the full walkthrough.
# Secrets do NOT go here — mailbox creds live in ~/.smtp_env and Claude auth in
# ~/.claude/.credentials.json or ~/.anthropic_key (all chmod 600, never committed).

# --- code vs state ----------------------------------------------------------
# INFRA_CODE_ROOT  — where this repo's code lives (auto-detected).
# INFRA_STATE_ROOT — where runtime state lives. In this release the daemons run
#                    FROM the infra/ dir and keep state under infra/scratch_full_logs
#                    (they resolve it relative to their own file). Point the DASHBOARD
#                    at that same place so it reads what the daemons write. (Relocating
#                    state to an arbitrary dir is the deferred cutover — MIGRATION.md.)
export INFRA_CODE_ROOT="${INFRA_CODE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"
export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-$INFRA_CODE_ROOT/infra}"

# Optional: run the daemons inside a conda env (e.g. on a dev host). Unset => use
# the python3 already on PATH — the daemons are pure-stdlib, so no env is required.
export INFRA_CONDA_ENV="${INFRA_CONDA_ENV:-}"

# --- your identity ----------------------------------------------------------
# Your email is your dashboard username, the address the posse emails you at, and
# (via the allow-list below) a sender that may command the fleet.
export INFRA_OPERATOR_EMAIL="${INFRA_OPERATOR_EMAIL:-}"          # you@example.com
export INFRA_OPERATOR_NAME="${INFRA_OPERATOR_NAME:-}"           # used in "Hi <name>,"

# --- who may command the fleet ---------------------------------------------
# FAIL-CLOSED allow-list of senders (comma-separated). EMPTY = ignore all mail.
# Start with just your own address; add teammates to let them send tasks too.
export INFRA_MAIL_ALLOWED="${INFRA_MAIL_ALLOWED:-$INFRA_OPERATOR_EMAIL}"

# --- Claude auth ------------------------------------------------------------
# subscription (default; uses ~/.claude/.credentials.json) or apikey (~/.anthropic_key).
export TSOMP_CLAUDE_AUTH="${TSOMP_CLAUDE_AUTH:-subscription}"

# --- dashboard bind ---------------------------------------------------------
# Default localhost (reach via SSH tunnel). Set INFRA_DASH_PUBLIC=1 when starting
# the dashboard to bind 0.0.0.0 + enable TLS instead.
export INFRA_DASH_HOST="${INFRA_DASH_HOST:-127.0.0.1}"
export INFRA_DASH_PORT="${INFRA_DASH_PORT:-8787}"
