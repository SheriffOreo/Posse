#!/usr/bin/env bash
# Task 216 F1a: rc-capture shim for jobmgr CPU jobs. scratch_detach.sh setsids
# an orphan whose exit code is otherwise unobservable post-mortem; running the
# command through this shim drops scratch_full_logs/jobs/rc/<id>.rc when it
# finishes — the unambiguous completion event scratch_jobmgr.py watches for.
# stdout/stderr already stream to the detach .output file; rc goes here too.
#   Usage (via scratch_detach.sh only): scratch_jobmgr_run.sh <job_id> -- <command...>
cd /home/steven/Projects/time-series-omp
ID="${1:?job id}"; shift
[ "${1:-}" = "--" ] && shift
"$@"
RC=$?
mkdir -p scratch_full_logs/jobs/rc
echo "$RC" > "scratch_full_logs/jobs/rc/.${ID}.tmp" \
  && mv "scratch_full_logs/jobs/rc/.${ID}.tmp" "scratch_full_logs/jobs/rc/${ID}.rc"
echo "[jobmgr_run] job ${ID} finished rc=${RC} $(date)"
exit "$RC"
