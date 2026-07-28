#!/usr/bin/env bash
# Task 216 F1a: submit a job to the jobmgr daemon (event-driven job management).
#
#   Usage: scratch_submit_job.sh <owner_agent> <est_seconds> [--gpu] [--no-wake]
#                                [--timeout S] -- <command...>
#
# Writes scratch_full_logs/jobs/pending/<id>.json and dispatches: CPU via the
# existing scratch_detach.sh (+ rc-capture shim scratch_jobmgr_run.sh), GPU via
# the existing gpu_queue/pending (gpu_manager executes it, untouched). The
# always-on 'jobmgr' tmux daemon monitors completion and, for wake_on_done jobs
# (the default), WAKES the owner via the existing interrupt/mailbox path.
#
# DOCTRINE (Task 216 F1c): estimated runtime > 50 min => submit here, then run
#   bash scratch_job_sleep.sh <owner>   and END YOUR TURN (event-wake).
# Estimated runtime <= 50 min => don't sleep on it: poll in the FOREGROUND with
# sleep chunks <= 240 s (4 min keeps the prompt cache warm; TTL is 5 min).
#
# The command after -- is exec'd verbatim (argv); wrap shell lines as:
#   ... -- bash -c 'cmd1 && cmd2 > out.log'
# All logic lives in scratch_jobmgr.py (--submit); this is a thin wrapper.
cd /home/steven/Projects/time-series-omp
exec python3 scratch_jobmgr.py --submit "$@"
