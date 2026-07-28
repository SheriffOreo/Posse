# claude_infra

The orchestration infrastructure that runs headless **Claude Code** worker agents
for the `time-series-omp` project: an email inbox/router, a watchdog that keeps
workers alive across crashes and usage limits, a job manager for long CPU/GPU jobs,
a GPU queue, and the launch/relaunch/interrupt machinery that ties them together —
plus a login-protected **dashboard** to watch and browse it all.

> This repo is the **versioned home for the CODE**. The **runtime state** (logs,
> mailboxes, job records, registries, the GPU queue) is large and machine-local; it
> stays in the working directory the daemons operate on (`INFRA_STATE_ROOT`, default
> `/home/steven/Projects/time-series-omp`) and is **git-ignored**. See
> [`MIGRATION.md`](MIGRATION.md) for the code/state split and the (deferred) cutover.

## Layout

```
claude_infra/
├── infra/                 # the ~26 core daemon + helper files (byte-identical copies)
├── dashboard/             # login-protected read-only status/history dashboard (stdlib)
├── proposed_patches/      # design notes for changes NOT yet applied to live infra
├── infra_env.sh           # INFRA_CODE_ROOT / INFRA_STATE_ROOT config (for cutover + dashboard)
├── MIGRATION.md           # code/state split · path strategy · cutover · rollback
├── README.md              # this file
└── .gitignore             # excludes all runtime state + secrets
```

## Architecture (what each daemon does)

Three always-on daemons run in `tmux` sessions (plus the GPU manager), all launched
from and operating on the tsomp working dir:

| Component | File (`infra/`) | Role |
| --- | --- | --- |
| **Email notifier** | `scratch_notify_email.py` | Send email to the requester (greeting/signature auto-added). Logs every send to `sent_emails.jsonl`. |
| **Inbox reader** | `scratch_inbox.py` | Fetch/route replies (allow-list only), capture attachments, defer-on-usage-limit, route by `In-Reply-To` → agent. |
| **Inbox router** | `scratch_inbox_loop.sh` | Always-on (tmux `inbox`). Routes each reply to its worker's live **mailbox**, or dispatches a handler. |
| **Reply handler** | `scratch_inbox_handle.sh` | Triages a reply: question → answer inline; task → write a `task_<uid>.md` spec and spawn a persistent worker. |
| **Mailbox r/w** | `scratch_read_mailbox.sh`, `scratch_mailbox_append.sh` | Drain / append live mid-task feedback (flock-safe; origin-headered). |
| **Interrupt** | `scratch_interrupt_worker.sh` | One-call inject → guard-stamp → surgical-kill → relaunch → verify. The canonical way to poke a running worker. |
| **Relaunch template** | `scratch_gen_relaunch.sh` | Generates each worker's `--resume` relaunch script (origin-aware ack, death forensics). |
| **Worker spawner** | `scratch_spawn_worker.sh` | Launch a persistent tmux worker (+ relaunch script, watchdog registration, done-sentinel). |
| **Watchdog** | `scratch_watchdog.py` | Always-on (tmux `watchdog`). Detects killed workers (crash or usage/weekly limit), emails Steven, auto-relaunches — now with limit-cooperation (Task 274). State in `watchdog_jobs.json`. |
| **Job manager** | `scratch_jobmgr.py` | Always-on (tmux `jobmgr`). Tracks all CPU+GPU jobs in `scratch_full_logs/jobs/{pending,running,done,…}`; wakes a job's owner on completion. |
| **Job submit / sleep** | `scratch_submit_job.sh`, `scratch_job_sleep.sh` | Launch a >50-min job then park the owner for an event-wake (the cost-saving path). |
| **GPU manager** | `gpu_manager.py`, `submit_gpu.py` | Always-on (tmux `gpu_manager`). Runs one GPU job at a time from `gpu_queue/pending` → `gpu_queue/done`. |
| **Snapshots** | `scratch_cpu_snapshot.sh`, `scratch_gpu_snapshot.sh`, `scratch_subagent_snapshot.sh` | Reprint a worker's in-flight CPU/GPU/sub-agent jobs on resume. |
| **Detach** | `scratch_detach.sh` | Run >2-min compute in its own session + pid file (survives interrupts). |
| **Auth switch** | `scratch_claude_auth.sh` | Sourced by every launcher; selects Claude Max OAuth (default) vs API-key billing. |
| **Registry** | (`scratch_agents_registry.json`, state) | Worker → session-id + reply-routing keywords + description. |

**Design conventions** (why it is shaped this way) live in the tsomp `CLAUDE.md` §2–3
and in `scratch_full_logs/inbox/backup_task274/DESIGN.md` (limit cooperation). Two
load-bearing rules: workers run **synchronously** (never background a job and end the
turn), and long waits go through the **jobmgr event-wake** (>50 min) or **≤4-min
foreground polls** (≤50 min) to keep the prompt cache warm.

## Operating the daemons (sanctioned restarts)

These are the **only** approved ways to bounce a daemon (all idempotent / flock-guarded):

```bash
# job manager        (tmux: jobmgr)
bash infra/scratch_jobmgr_start.sh
# gpu manager        (tmux: gpu_manager)
bash infra/scratch_gpu_manager_start.sh
# inbox router       (tmux: inbox)  — graceful reload:
touch "$INFRA_STATE_ROOT/scratch_full_logs/inbox/RESTART_LOOP"
# watchdog           (tmux: watchdog) — manual bounce only, see MIGRATION.md
```

Today the daemons run from the tsomp checkout. Running them **from this repo** is a
one-time cutover (state must still resolve to `INFRA_STATE_ROOT`) — see
[`MIGRATION.md`](MIGRATION.md). It is **not** done automatically.

## Dashboard

Login-protected, read-only, pure-stdlib. Real-time status (workers / GPU / jobmgr /
usage-limit) + job-history calendar → day → job/task → deliverable download +
per-task conversation & lineage. See [`dashboard/README.md`](dashboard/README.md).

```bash
cd dashboard && python3 set_password.py && ./run.sh
# then, from your laptop:  ssh -L 8787:localhost:8787 <host>  →  http://localhost:8787
```

## Secrets — never committed

`~/.smtp_env`, `~/.anthropic_key`, `~/.claude/.credentials.json`, and the GitHub PAT
embedded in this repo's git remote URL are all **out of the repo** and covered by
`.gitignore`. The code only ever *reads* them from those chmod-600 files. A pre-commit
secret scan (see `MIGRATION.md`) guards every commit.
