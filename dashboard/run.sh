#!/usr/bin/env bash
# Start the claude_infra dashboard. Localhost-only by default; reach via SSH tunnel.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Where the LIVE infra state lives (read-only). Default: the infra/ dir shipped
# beside this dashboard (the daemons keep state under infra/scratch_full_logs).
# Same knob as the daemon cutover — override with INFRA_STATE_ROOT.
export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-$(cd "$HERE/.." && pwd)/infra}"
export INFRA_DASH_HOST="${INFRA_DASH_HOST:-127.0.0.1}"
export INFRA_DASH_PORT="${INFRA_DASH_PORT:-8787}"

PY="${PYTHON:-python3}"

# First run: make sure a login password exists (stored hashed under instance/).
if [ ! -f instance/auth.json ]; then
  echo "[run] No dashboard password set yet."
  if [ -n "${INFRA_DASH_PASSWORD:-}" ]; then
    "$PY" set_password.py --env
  else
    "$PY" set_password.py
  fi
fi

echo "[run] state root : $INFRA_STATE_ROOT"
echo "[run] listening   : http://$INFRA_DASH_HOST:$INFRA_DASH_PORT"
echo "[run] SSH tunnel  : ssh -L $INFRA_DASH_PORT:localhost:$INFRA_DASH_PORT <this-host>"
exec "$PY" server.py
