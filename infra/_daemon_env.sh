#!/usr/bin/env bash
# Shared runtime setup, sourced by every daemon starter + the deputy launcher.
# Makes the infra portable: no hardcoded host path, no mandatory conda env.
#
#   - cd into THIS directory (infra/), which is both the code root and — because the
#     daemons resolve state as Path(__file__).parent/scratch_full_logs — the state
#     root. Point the dashboard at it with INFRA_STATE_ROOT=<repo>/infra.
#   - Optionally activate a conda env: export INFRA_CONDA_ENV=<name> (e.g. on a dev
#     host). Unset => use the python3 already on PATH. The daemons are pure-stdlib,
#     so NO conda env is required for a fresh install.
#   - Load Claude auth (subscription vs API key) via scratch_claude_auth.sh.
#   - Make sure the `claude` CLI is reachable (npm global bin on PATH).
_INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$_INFRA_DIR" || return 1 2>/dev/null || exit 1
if [ -n "${INFRA_CONDA_ENV:-}" ]; then
  for _c in "$HOME/anaconda3/etc/profile.d/conda.sh" "$HOME/miniconda3/etc/profile.d/conda.sh"; do
    [ -f "$_c" ] && { . "$_c"; conda activate "$INFRA_CONDA_ENV"; break; }
  done
fi
[ -f "$_INFRA_DIR/scratch_claude_auth.sh" ] && . "$_INFRA_DIR/scratch_claude_auth.sh"
export PATH="$HOME/.npm-global/bin:$PATH"
