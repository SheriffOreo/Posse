#!/usr/bin/env bash
# scratch_detach.sh <worker> <job> -- <command…>            (Task 165 F3)
# Detach must-survive compute from the claude process: run the command in its
# own session (setsid -> ppid 1, no controlling tty), stdio on files, pid
# recorded. Survives BOTH an interrupt kill and a NORMAL end-of-turn claude
# exit — unlike the harness run_in_background mechanism, whose process groups
# claude SIGTERMs then SIGKILLs in its shutdown (Task 165 memo §3–4).
# Poll:  kill -0 $(cat scratch_full_logs/<worker>_<job>.pid)  +  tail the .output
# scratch_cpu_snapshot.sh section 3 lists these pid files automatically.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
W="${1:?usage: scratch_detach.sh <worker> <job> -- <command...>}"
J="${2:?usage: scratch_detach.sh <worker> <job> -- <command...>}"
shift 2
[ "${1:-}" = "--" ] && shift
[ $# -ge 1 ] || { echo "ERR: no command given" >&2; exit 2; }
OUT="scratch_full_logs/${W}_${J}.output"
setsid "$@" < /dev/null >> "$OUT" 2>&1 &
echo $! > "scratch_full_logs/${W}_${J}.pid"
echo "detached pid=$(cat "scratch_full_logs/${W}_${J}.pid") out=$OUT"
