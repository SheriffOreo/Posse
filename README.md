<p align="center">
  <img src="assets/posse_banner.png" alt="Posse" width="760">
</p>

<p align="center">
  <em>A cost-aware operating system for long-horizon, human-directed multi-agent work.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/runtime-Python%203.10-6cb6ff">
  <img src="https://img.shields.io/badge/dashboard-pure%20stdlib%20%C2%B7%20no%20deps-3fb950">
  <img src="https://img.shields.io/badge/control%20plane-email-e3b341">
  <img src="https://img.shields.io/badge/tokens-cache--aware%20scheduling-c58bff">
</p>

---

**Posse** is the infrastructure that runs a group of autonomous **Claude Code** agents
for the `time-series-omp` / OMPGen research project. You email a task; a **deputy**
(a worker agent) picks it up, works it to completion — possibly over hours of GPU
time — and emails you back its plan, milestones, and result. Reply mid-task and it
incorporates your feedback without losing its place. If it crashes or hits a usage
limit, it comes back. If a job will take an hour, the deputy hands the wait to a job
manager and is woken the moment it finishes. Overseeing it all is the **Sheriff**, a
single always-on system manager; a read-only web dashboard lets you watch everything.

> **The metaphor.** Work is organized into **Precincts** (domains: `eval`, `omp`,
> `query`, `paper`, `infra`, and a `receptionist` front desk). Each precinct's work
> is done by ephemeral **Deputies**, one per **case** (a numbered thread of work). A
> single **Sheriff** — Oreo, in the badge above — manages the whole operation:
> supervising deputies and keeping each precinct's records healthy. The deputies are
> a *posse*.

> **📄 Design document.** The architecture, the problems it solves, a comparison with
> existing agent frameworks, and the rationale for every module are written up in
> **[`design/POSSE_DESIGN.pdf`](design/POSSE_DESIGN.pdf)** — start there for the full
> picture.

> **Code vs. state.** This repo is the **versioned home for the code**. The **runtime
> state** (logs, mailboxes, job records, the GPU queue, registries, precinct records)
> is large and machine-local; it lives in the working directory the daemons operate on
> (`INFRA_STATE_ROOT`, default `/home/steven/Projects/time-series-omp`) and is
> git-ignored. See [`MIGRATION.md`](MIGRATION.md) for the split and the deferred cutover.

## Why this exists

Most agent frameworks are built for a **single, supervised task**: a human starts a
session, watches it run, and reads the result. The regime Posse targets is different —
one human asynchronously directs **dozens** of independent threads by email, over
weeks, each running unattended. That "outside the task" layer raises four problems
session-based frameworks don't address:

1. **Continuity of a thread** — one question and its follow-ups must keep their context
   and logic across interruptions, crashes, and restarts, *even after the process that
   ran them has died and been replaced*.
2. **Managing many threads** — dozens run at once; each needs a stable identity, an
   owner, isolation, and a structure a human can navigate.
3. **Continuous, autonomous operation** — the system routes work, keeps deputies alive,
   recovers from crashes and rate limits, serializes the one GPU, and wakes a deputy
   when a long job finishes — with nobody babysitting a terminal.
4. **Token efficiency in long threads** — a days-old thread carries a big context, and
   the provider's cache economics make touching it cheap or ruinous (a ~12.5× swing).
   Long-horizon operation is only viable if cost is *engineered*.

Posse was hardened by a real, expensive lesson: undisciplined polling once cost
~$10k/month in cache rewrites, so the wait discipline below is load-bearing, not
decoration.

## Three design principles

- **Identity is decoupled from process liveness.** A thread's identity is a durable
  *record* (a case file + a persisted lineage map), not a running process. A thread
  can be resumed — or rebuilt into a fresh process under the *same* identity — long
  after its original process died. Continuity survives death.
- **Token cost is a first-class scheduling resource.** Every wait (poll vs. sleep),
  memory (keep vs. compact), and model (cheap vs. capable) decision is taken against an
  explicit cost model derived from the provider's cache economics.
- **Zero-cost mechanical control is separated from paid cognition.** Routing, liveness,
  recovery, waking, GPU serialization, and monitoring make **no** LLM calls; the model
  is invoked only for real work and a handful of manager decisions. That is what makes
  always-on operation affordable.

## Features

- 📨 **Email is the control plane.** A `[precinct]`-tagged email spawns a deputy in
  that precinct; a reply steers the deputy that sent it. Routing is deterministic and
  **zero-LLM**; an allow-list gates senders.
- ⚡ **Email interrupts a running deputy.** A mid-task reply is injected into the
  deputy's mailbox and the deputy is *surgically* relaunched to read it — only the
  claude process is killed; its CPU/GPU jobs keep running.
- 🤠 **The Sheriff manages the operation.** One always-on system manager supervises
  deputy liveness (crash / usage-limit relaunch) and precinct-records health (ledger
  compaction, a change-request queue, precinct lifecycle). **Its monitoring is
  zero-API.**
- 🗂️ **Precincts give durable, bounded, auditable memory.** Each domain has a compacted
  *ledger* (big-picture digest), an append-only *case log*, and per-case files — plain
  text, managed through one concurrency-safe records manager.
- ⏳ **Event-wake for long jobs.** A deputy submits a >50-min job and *sleeps*; the job
  manager wakes it the instant it finishes. Short jobs use ≤4-min foreground polls so
  every poll is a **cache-warm read** instead of a cold context rewrite.
- 🎛️ **One GPU, queued fairly.** A GPU manager runs a single GPU job at a time from a
  file-backed queue; jobs survive the owner being interrupted.
- 👥 **Multi-agent coordination.** Tracked *sub-deputies* (numbered `400a`, `400b`, …),
  *sheriff-wake* on subtask completion, *Joint Task Forces* (a lead + collaborators),
  and deputy-owned *anonymous critics*.
- 👁️ **Read-only dashboard.** Login-gated, pure-stdlib web UI: live Status, active
  deputies, case lineage, a job-history calendar, per-precinct records — day/night
  themed (the western "Posse" skin).
- 🔐 **Secrets never touch git.** SMTP creds, API keys, OAuth tokens, and the login
  hash all live outside the repo; a download sandbox + denylist guard the dashboard.

## Architecture

<p align="center">
  <img src="assets/system_design.png" alt="Posse system architecture" width="960">
</p>

Work flows down the center — a **human** emails a task; the always-on **inbox router**
maps it (mechanically, no LLM) to a live deputy's mailbox, a new deputy in a tagged
**precinct**, or the **receptionist**. Deputies work **cases** to completion, emailing
**plan / milestone / FINAL**, and — for a long job — **submit + sleep** so the **job
manager** can event-wake them (GPU work rides the **GPU manager**). The **Sheriff**
(left) supervises deputies and precinct records with **zero-API monitoring**, spending
tokens only on ledger compaction and change-request decisions. A read-only
**dashboard** (right) observes it all. See
**[`design/POSSE_DESIGN.pdf`](design/POSSE_DESIGN.pdf)** for the full walkthrough.

| Component | File (`infra/`) | Role |
| --- | --- | --- |
| **Email notifier** | `scratch_notify_email.py` | Outbound email (greeting/signature + `[precinct│case│deputy│model]` header auto-added); logs every send. |
| **Inbox reader** | `scratch_inbox.py` | Fetch + route replies (allow-list only); two mechanical signals — reply-thread, `[precinct]` tag — else the receptionist. |
| **Inbox router** | `scratch_inbox_loop.sh` | Always-on (`tmux inbox`). Delivers each reply to the live deputy's mailbox, or spawns a deputy for a tagged precinct. |
| **Worker spawner** | `scratch_spawn_worker.sh` | Launch a persistent `tmux` deputy (+ watchdog registration, relaunch script, done-sentinel, precinct/model/case stamps). |
| **Interrupt entrypoint** | `scratch_interrupt_worker.sh` | One call: inject → guard-stamp → surgical-kill → relaunch → verify. |
| **Mailbox** | `scratch_mailbox_append.sh` · `scratch_read_mailbox.sh` | Flock-safe, origin-headered channel for live mid-task feedback. |
| **Sheriff** | `scratch_sheriff.py` · `scratch_sheriff_start.sh` | Always-on (`tmux sheriff`). System manager: zero-API monitoring; LLM only for ledger compaction + change-request decisions. |
| **Records manager** | `scratch_records.py` | The one concurrency-safe way to touch precinct records: deputies append-only, sheriff-only compaction/retraction/lifecycle. |
| **Request queue** | `scratch_sheriff_request.py` | Deputy → sheriff requests (retract, edit ledger, case number, precinct create/delete) with anti-impersonation. |
| **Case allocator** | `scratch_case_seq.py` | One continuous monotonic case number for email + web; the Gmail uid is a routing key only. |
| **Watchdog** | `scratch_watchdog.py` | Always-on (`tmux watchdog`). Detect killed / limit-blocked deputies, email, auto-relaunch (the Sheriff's deputy-supervision loop). |
| **Job manager** | `scratch_jobmgr.py` | Always-on (`tmux jobmgr`). Track all CPU+GPU jobs; event-wake the owner on completion. |
| **Job submit / sleep** | `scratch_submit_job.sh` · `scratch_job_sleep.sh` | Launch a >50-min job, then park the owner for an event-wake. |
| **Subtask wake** | `scratch_subtask_wake.py` | Register + park a parent deputy; the Sheriff wakes it when its sub-deputies finish. |
| **JTF bridge** | `scratch_jtf.py` | Joint Task Force: a lead + collaborators; each slot a precinct (new deputy) or a specific deputy (must-take). |
| **GPU manager** | `gpu_manager.py` · `submit_gpu.py` | Always-on (`tmux gpu_manager`). One GPU job at a time from `gpu_queue/`. |
| **Registry** | `scratch_agents_registry.json` | Deputy → session-id + reply-routing keywords + description. |
| **Auth switch** | `scratch_claude_auth.sh` | Sourced by every launcher; Claude Max OAuth (default) vs. API-key billing. |
| **Dashboard** | `dashboard/` | Login-gated, read-only, stdlib HTTPS UI over the live state. |

## Quickstart

Everything is Python 3.10 + bash; the dashboard needs **no pip installs**. Point the
daemons at the state directory with `INFRA_STATE_ROOT` (default shown).

```bash
export INFRA_STATE_ROOT=/home/steven/Projects/time-series-omp

# Always-on daemons (each idempotent / flock-guarded):
bash infra/scratch_jobmgr_start.sh          # tmux: jobmgr       — job manager
bash infra/scratch_gpu_manager_start.sh     # tmux: gpu_manager  — GPU queue
bash infra/scratch_inbox_loop.sh            # tmux: inbox        — email router
bash infra/scratch_sheriff_start.sh         # tmux: sheriff      — system manager
#   (watchdog runs in tmux: watchdog — see MIGRATION.md for a manual bounce)

# Read-only dashboard:
cd dashboard
python3 set_password.py                      # one-time: set the login password
bash start_dashboard.sh                      # tmux: infra_dashboard → http://127.0.0.1:8787
#   public host, no tunnel (binds 0.0.0.0 + self-signed TLS):
#   INFRA_DASH_PUBLIC=1 bash start_dashboard.sh   → https://<host>:8787
```

From your laptop, reach the localhost dashboard over an SSH tunnel:

```bash
ssh -L 8787:localhost:8787 <host>     # then browse http://localhost:8787
```

Key env vars: `INFRA_STATE_ROOT` (state dir the daemons read/write), `INFRA_DASH_PORT`
(default `8787`), `INFRA_DASH_PUBLIC=1` (bind `0.0.0.0` **and** enable TLS). Full list
in [`dashboard/README.md`](dashboard/README.md). Today the daemons run from the tsomp
checkout; running them *from this repo* is a one-time cutover — see
[`MIGRATION.md`](MIGRATION.md).

## The wait discipline (why it's load-bearing)

The prompt cache has a ~5-minute TTL: a touch within it is a warm **read**; a touch
after is a cold **rewrite** of the whole context, at ~12.5× the read. So:

- **≤ 50 min → foreground-poll at ≤ 4-min intervals** — every poll lands inside the
  cache TTL as a warm read.
- **> 50 min → submit + sleep** — hand the wait to the job manager and end the turn;
  one event-wake costs a single rewrite instead of an hour of polls.

Fifty minutes is the break-even: warm polling costs ~15 reads/hr of context; an
event-wake costs one rewrite (~12.5 reads); 12.5 / 15 h ≈ 50 min. Undisciplined
(>5-min) polling is exactly what caused the ~$10k/month incident. This is the single
most important operational rule in the system.

## Repository layout

```
claude_infra/
├── assets/               # brand: Posse banner + logo (SVG/PNG) + the architecture figure
├── design/               # POSSE_DESIGN.pdf — the design & experience report
├── infra/                # the core daemon + helper scripts (byte-identical copies)
├── dashboard/            # login-protected, read-only status/history dashboard (stdlib)
├── proposed_patches/     # design notes for changes NOT yet applied to live infra
├── infra_env.sh          # INFRA_CODE_ROOT / INFRA_STATE_ROOT config (cutover + dashboard)
├── MIGRATION.md          # code/state split · path strategy · cutover · rollback
└── README.md             # this file
```

## Operating the daemons (sanctioned restarts)

These are the **only** approved ways to bounce a daemon (all idempotent):

```bash
bash infra/scratch_jobmgr_start.sh                                   # jobmgr
bash infra/scratch_gpu_manager_start.sh                              # gpu_manager
bash infra/scratch_sheriff_start.sh                                  # sheriff
touch "$INFRA_STATE_ROOT/scratch_full_logs/inbox/RESTART_LOOP"       # inbox (graceful reload)
# watchdog: manual bounce only — see MIGRATION.md
```

## Secrets — never committed

`~/.smtp_env`, `~/.anthropic_key`, `~/.claude/.credentials.json`, the dashboard
`instance/` (password hash + cookie secret + TLS cert), and the GitHub PAT in the git
remote are all **outside the repo** and covered by [`.gitignore`](.gitignore). The
code only ever *reads* them from those chmod-600 files; a secret scan guards every
commit.

---

<sub><b>Posse</b> is the project name for this repo (`claude_infra` on disk); the
mascot is Oreo the sheriff. Internal research infrastructure — no license granted;
not for redistribution.</sub>
