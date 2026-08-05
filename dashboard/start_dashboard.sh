#!/usr/bin/env bash
# Launch (or verify) the claude_infra dashboard in its OWN detached tmux session,
# wrapped in a while-true auto-restart loop so it survives a crash / process exit.
# Idempotent: safe to run repeatedly (mirrors scratch_gpu_manager_start.sh).
#
# NOTE: this is NOT one of the always-on infra daemons (watchdog / jobmgr / inbox /
# gpu_manager) and it does not touch them — it is a separate, read-only viewer
# service on its own session name. The server is pure Python stdlib.
#
#   Usage:
#     bash start_dashboard.sh                    # localhost:8787 (reach via SSH tunnel)
#     INFRA_DASH_PUBLIC=1 bash start_dashboard.sh # bind 0.0.0.0 + TLS (no tunnel needed)
#     INFRA_DASH_HOST=0.0.0.0 INFRA_DASH_TLS=0 bash start_dashboard.sh  # public plain HTTP
#
# INFRA_DASH_PUBLIC=1 is the convenience switch: it binds all interfaces AND turns
# on TLS (self-signed cert generated once into instance/) so the login password is
# never sent cleartext on the LAN. Set INFRA_DASH_TLS=0 to force plain HTTP.
set -euo pipefail
SESSION="infra_dashboard"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Per-machine config (git-ignored; setup.py writes it, or edit by hand). Sourced BEFORE
# the defaults below so a persisted INFRA_STATE_ROOT / bind survives a bare restart —
# without this, a restart that forgot to export INFRA_STATE_ROOT falls back to the empty
# release dir and the dashboard renders blank (Case 431).
if [ -f "$HERE/../infra_env.local.sh" ]; then . "$HERE/../infra_env.local.sh"; fi

# Default state root: the infra/ dir shipped beside this dashboard (override with INFRA_STATE_ROOT).
export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-$(cd "$HERE/.." && pwd)/infra}"
# Public convenience switch: all interfaces + TLS on (encrypted login over the LAN).
if [ "${INFRA_DASH_PUBLIC:-0}" = "1" ]; then
  export INFRA_DASH_HOST="0.0.0.0"
  export INFRA_DASH_TLS="${INFRA_DASH_TLS:-1}"
fi
export INFRA_DASH_HOST="${INFRA_DASH_HOST:-127.0.0.1}"
export INFRA_DASH_PORT="${INFRA_DASH_PORT:-8787}"
export INFRA_DASH_TLS="${INFRA_DASH_TLS:-0}"
PY="${PYTHON:-python3}"
LOG="$HERE/instance/dashboard.log"
CERT="$HERE/instance/cert.pem"
KEY="$HERE/instance/key.pem"

mkdir -p "$HERE/instance"

# ---- TLS: generate a self-signed cert ONCE, at setup time only ----------------
# The server itself never shells out — it only reads the cert/key via stdlib ssl.
# openssl runs here, exactly once, and only if a cert isn't already present.
gen_cert() {
  command -v openssl >/dev/null 2>&1 || { echo "ERR: openssl not found; cannot make TLS cert" >&2; return 1; }
  local CN IP SAN
  CN="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo localhost)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  SAN="DNS:${CN},DNS:localhost,IP:127.0.0.1"
  [ -n "$IP" ] && SAN="${SAN},IP:${IP}"
  echo "generating self-signed TLS cert (CN=$CN; SAN=$SAN) into instance/ ..."
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$KEY" -out "$CERT" \
    -days 825 -subj "/CN=${CN}" -addext "subjectAltName=${SAN}" >/dev/null 2>&1
  chmod 600 "$KEY" 2>/dev/null || true
  chmod 644 "$CERT" 2>/dev/null || true
}
if [ "$INFRA_DASH_TLS" = "1" ] && { [ ! -s "$CERT" ] || [ ! -s "$KEY" ]; }; then
  # Task 350: prefer a CA-signed leaf (a browser trusts it once instance/rootCA.pem
  # is installed on the viewing device — no more "Not Secure") over a bare
  # self-signed cert. gen_ca_cert.sh is idempotent and only fires HERE, when no cert
  # is present; a cert already on disk (the CA-signed leaf) is left untouched, so a
  # normal restart keeps serving it. gen_cert (self-signed) stays as the fallback.
  if [ -f "$HERE/gen_ca_cert.sh" ]; then
    bash "$HERE/gen_ca_cert.sh" || { echo "ERR: CA-signed cert generation failed" >&2; exit 1; }
  else
    gen_cert || { echo "ERR: TLS requested but cert generation failed" >&2; exit 1; }
  fi
fi
export INFRA_DASH_CERT="${INFRA_DASH_CERT:-$CERT}"
export INFRA_DASH_KEY="${INFRA_DASH_KEY:-$KEY}"

SCHEME="http"; [ "$INFRA_DASH_TLS" = "1" ] && SCHEME="https"
HOSTDISP="$INFRA_DASH_HOST"
[ "$INFRA_DASH_HOST" = "0.0.0.0" ] && HOSTDISP="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo localhost)"

if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "dashboard tmux session '$SESSION' already running:"
  tmux list-panes -t "=$SESSION" -F '  pane #{pane_pid} #{pane_current_command}'
  echo "  URL: $SCHEME://$HOSTDISP:$INFRA_DASH_PORT"
  exit 0
fi

tmux new-session -d -s "$SESSION" \
  "bash -lc 'cd \"$HERE\" && export INFRA_STATE_ROOT=\"$INFRA_STATE_ROOT\" INFRA_DASH_HOST=\"$INFRA_DASH_HOST\" INFRA_DASH_PORT=\"$INFRA_DASH_PORT\" INFRA_DASH_TLS=\"$INFRA_DASH_TLS\" INFRA_DASH_CERT=\"$INFRA_DASH_CERT\" INFRA_DASH_KEY=\"$INFRA_DASH_KEY\"; while true; do \"$PY\" server.py 2>&1 | tee -a \"$LOG\"; echo \"[dashboard-wrapper] server exited rc=\$? \$(date) — restarting in 5s\" | tee -a \"$LOG\"; sleep 5; done'"
sleep 2
if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "started dashboard tmux session '$SESSION' (log: $LOG)"
  echo "  URL:    $SCHEME://$HOSTDISP:$INFRA_DASH_PORT"
  if [ "$INFRA_DASH_HOST" = "127.0.0.1" ]; then
    echo "  tunnel: ssh -L $INFRA_DASH_PORT:localhost:$INFRA_DASH_PORT <this-host>"
  elif [ "$INFRA_DASH_TLS" != "1" ]; then
    echo "  WARNING: bound to a public interface over PLAIN HTTP — the login password"
    echo "           travels cleartext on the LAN. Prefer INFRA_DASH_PUBLIC=1 (adds TLS)."
  fi
else
  echo "ERR: dashboard session '$SESSION' did not come up" >&2
  exit 1
fi
