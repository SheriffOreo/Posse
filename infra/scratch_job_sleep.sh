#!/usr/bin/env bash
# Task 216 F1a: park a worker for event-wake sleep — THE way to wait on a
# jobmgr job estimated > 50 min (shorter waits: poll <= 240 s chunks instead).
#
#   Usage: scratch_job_sleep.sh <owner>     # then END YOUR TURN (exit rc=0)
#
# What it does (all reversed automatically by the wake):
#   - touches scratch_full_logs/worker_<owner>.done  -> the launch/relaunch
#     wrapper's sentinel trap sees a "complete" exit and does NOT nudge-resume
#   - sets state="waiting_jobs" for <owner> in watchdog_jobs.json (Task 216
#     uid=217: first-class state) -> the watchdog leaves it alone (no
#     exited_incomplete relaunch loop, no dark alarms) and rosters it honestly
#     as "waiting_jobs (parked; jobmgr wakes it on job completion)"
#   - writes scratch_full_logs/jobs/sleeping/<owner>.json -> tells jobmgr to
#     wake via scratch_interrupt_worker.sh (which rm's the sentinel and sets
#     state back to "running" — the existing reversal, nothing hand-rolled)
#
# REFUSES to park if no wake_on_done job is in flight for <owner> (nothing
# would ever wake it). If a user email interrupts you while parked, handle it,
# then re-park with this script if your job is still running.
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
exec python3 scratch_jobmgr.py --sleep "${1:?usage: scratch_job_sleep.sh <owner>}"
