# infra_env.sh — shared location config for the claude_infra code.
#
# Source this before running a daemon FROM this repo (post-cutover), and it is
# what dashboard/run.sh reads too. It defines the split between:
#   INFRA_CODE_ROOT  — where the daemon code + helper scripts live (this repo)
#   INFRA_STATE_ROOT — where the LIVE runtime state lives (the tsomp working dir:
#                      scratch_full_logs/, gpu_queue/, registry, mailboxes, jobs).
#
# WHY A SPLIT: today every daemon resolves ROOT = Path(__file__).parent and uses
# it for BOTH code and state. When the code moves here, state must still resolve
# to the tsomp dir. The dashboard already honors INFRA_STATE_ROOT. Wiring the
# daemons to honor it is the one-time CUTOVER edit (see MIGRATION.md §3); until
# then the committed daemon copies are byte-identical and run from tsomp.

export INFRA_CODE_ROOT="${INFRA_CODE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"
export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-/home/steven/Projects/time-series-omp}"

# Convenience for the dashboard.
export INFRA_DASH_HOST="${INFRA_DASH_HOST:-127.0.0.1}"
export INFRA_DASH_PORT="${INFRA_DASH_PORT:-8787}"
