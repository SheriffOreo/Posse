#!/usr/bin/env bash
# Task 274 G3: hand a worker's self-monitored scratch_detach.sh jobs to jobmgr at
# limit-kill time (the watchdog's limit path calls this). Thin wrapper; all logic
# lives in scratch_jobmgr_adopt.py. Idempotent — safe to call repeatedly.
#   Usage: scratch_jobmgr_adopt.sh <owner> [--all | --pidfile <path>]
cd /home/steven/Projects/time-series-omp
exec python3 scratch_jobmgr_adopt.py "$@"
