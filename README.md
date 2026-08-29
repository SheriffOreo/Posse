<p align="center">
  <img src="assets/posse_banner.png" alt="Posse" width="760">
</p>

<p align="center">
  <em>A cost-aware operating system for long-horizon, human-steered fleets of LLM agents.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/runtime-Python%203.10-6cb6ff">
  <img src="https://img.shields.io/badge/dashboard-pure%20stdlib%20%C2%B7%20no%20deps-3fb950">
  <img src="https://img.shields.io/badge/control%20plane-async%20messaging-e3b341">
  <img src="https://img.shields.io/badge/tokens-cache--aware%20scheduling-c58bff">
</p>

---

**Posse** runs and manages a **fleet of autonomous agents** over long horizons. You send
a task as a message; a **deputy** (a worker agent) picks it up, works it to completion —
possibly over hours of compute — and reports back a plan, milestones, and result. Reply
mid-task and it incorporates your feedback without losing its place. If it crashes or
hits a usage limit, it comes back. If a job will take an hour, the deputy hands the wait
to a job manager and is woken the moment it finishes. Overseeing it all is the
**Sheriff**, a single always-on system manager; a read-only web dashboard lets you watch
everything.

Posse is a **general framework**: the repo ships only the **Sheriff** and a
**Receptionist** front desk, and **you create the precincts** that fit your work.

> **The metaphor.** Work is organized into **Precincts** — user-defined domains
> (e.g. `evaluation`, `development`, `research`, `ops`). Each precinct's work is done by
> ephemeral **Deputies**, one per **case** (a numbered thread of work). A single
> **Sheriff** — Oreo, in the badge above — manages the whole operation: supervising
> deputies and keeping each precinct's records healthy. The deputies are a *posse*.

> **🚀 New operator? Start here.** Run **`python3 setup.py`** — one interactive program
> that asks a handful of questions (your identity, the posse mailbox, Claude auth, the web
> host/port), writes every config file, seeds the front desk, registers your dashboard
> login, starts the system, and prints the URL to open. [`ONBOARDING.md`](ONBOARDING.md)
> is the same steps by hand, plus the mental model and troubleshooting.

> **♻️ Already set up? Relaunch after a reboot.** `setup.py` is a **one-time installer** —
> you do **not** re-run it to restart. To bring the whole system back up after a machine
> power cycle (or to stop it), run these two idempotent scripts from the repo root:
>
> ```bash
> bash posse_start.sh    # start or verify every daemon + the dashboard (safe to re-run)
> bash posse_stop.sh     # stop the daemons + dashboard (deputies + jobs keep running)
> ```

> **📄 Design paper.** The architecture, the requirements it targets, a comparison with
> existing agent frameworks, and the rationale for every module are written up in
> **[`design/POSSE_DESIGN.pdf`](design/POSSE_DESIGN.pdf)** — start there for the full
> picture.

> **Code vs. state.** This repo is the **versioned home for the code**. The **runtime
> state** (logs, mailboxes, job records, the queue, registries, precinct records) is
> large and machine-local; it lives in a working directory the daemons operate on
> (`INFRA_STATE_ROOT`) and is git-ignored. See [`MIGRATION.md`](MIGRATION.md) for the split.

## Why this exists

Most agent frameworks are built for a **single, supervised task**: a human starts a
session, watches it run, and reads the result. The regime Posse targets is different —
a human asynchronously directs **many** agents over long horizons, each running
unattended. That "outside the task" layer raises requirements session-based frameworks
don't address:

1. **Continuity of a thread** — one line of work and its follow-ups must keep their
   context and logic across interruptions, crashes, and restarts, *even after the
   process that ran them has died and been replaced*.
2. **Managing many threads** — a fleet runs at once; each needs a stable identity, an
   owner, isolation, and a structure a human can navigate.
3. **Continuous, autonomous operation** — the system routes work, keeps deputies alive,
   recovers from crashes and rate limits, serializes contended resources, and wakes a
   deputy when a long job finishes — with nobody babysitting a terminal.
4. **Token efficiency in long threads** — a days-old thread carries a big context, and
   the provider's cache economics make touching it cheap or ruinous (a ~12.5× swing).
   Long-horizon operation is only viable if cost is *engineered*.

## Three design principles

- **Identity is decoupled from process liveness.** A thread's identity is a durable
  *record* (a case file + a persisted lineage map), not a running process. A thread can
  be resumed — or rebuilt into a fresh process under the *same* identity — long after
  its original process died. Continuity survives death.
- **Token cost is a first-class scheduling resource.** Every wait (poll vs. sleep),
  memory (keep vs. compact), and model (cheap vs. capable) decision is taken against an
  explicit cost model derived from the provider's cache economics.
- **Zero-cost mechanical control is separated from paid cognition.** Routing, liveness,
  recovery, waking, resource serialization, and monitoring make **no** LLM calls; the
  model is invoked only for real work and a handful of manager decisions.

## Features

- 🤝 **Two vendors, and a case can change model mid-flight.** A case runs on Claude or
  on ChatGPT (OpenAI's `codex`) — and it is **one case, one context, several models**:
  the deputy switches *its own* model between **lanes** (the work lane vs. the
  human-report lane), rather than handing off to a second agent. So "Claude does the
  thinking, ChatGPT writes the report" is one deputy crossing a jurisdiction line, and
  nothing is lost in a hand-off. Subscription or API-key auth for either; usage limits
  are tracked per vendor, so hitting one does not block the other.
  See [Using ChatGPT](#using-chatgpt-optional).
- 📨 **Async messaging is the control plane.** A `[precinct]`-tagged message spawns a
  deputy in that precinct; a reply steers the deputy that sent it. Routing is
  deterministic and **zero-LLM**; an allow-list gates senders. (The reference
  implementation uses email.)
- ⚡ **Messages interrupt a running deputy.** A mid-task reply is injected into the
  deputy's mailbox and the deputy is *surgically* relaunched to read it — only the agent
  process is killed; its compute jobs keep running.
- 🤠 **The Sheriff manages the fleet.** One always-on system manager supervises deputy
  liveness (crash / usage-limit relaunch) and precinct-records health (ledger compaction,
  a change-request queue, precinct lifecycle). **Its monitoring is zero-API.**
- 🗂️ **Precincts give durable, bounded, auditable memory.** Each user-defined domain has
  a compacted *ledger* (big-picture digest), an append-only *case log*, and per-case
  files — plain text, managed through one concurrency-safe records manager.
- ⏳ **Event-wake for long jobs.** A deputy submits a >50-min job and *sleeps*; the job
  manager wakes it the instant it finishes. Short jobs use ≤4-min foreground polls so
  every poll is a **cache-warm read** instead of a cold context rewrite.
- 🎛️ **Serialized resources.** A resource manager runs contended jobs (e.g. one GPU) one
  at a time from a file-backed queue; jobs survive the owner being interrupted.
- 👥 **Multi-agent coordination.** Tracked *sub-deputies* (numbered `Na`, `Nb`, …),
  *manager-wake* on subtask completion, *joint task forces* (a lead + collaborators),
  and deputy-owned *anonymous helpers*.
- ⚖️ **Judges review the work.** An independent agent reads a deliverable it did not
  produce and returns **SIGN-OFF / REVISE / REJECT**; every round is archived, so a
  sign-off is an artifact rather than a claim in a status mail. Assign one per case and
  the deputy cannot close until it signs off. See [Judges](#judges-independent-review).
- 👁️ **Read-only dashboard.** Login-gated (your **email is the username**, set via a
  one-time registration link), pure-stdlib web UI: live Status, active deputies, case
  lineage, a job-history calendar, per-precinct records.
- 🔐 **Secrets never touch git.** Messaging creds, API keys, OAuth tokens, and the login
  hash all live outside the repo; a download sandbox + denylist guard the dashboard.

## Architecture

<p align="center">
  <img src="assets/system_design.png" alt="Posse system architecture" width="960">
</p>

Work flows down the center — a **human** sends a task; the always-on **router** maps it
(mechanically, no LLM) to a live deputy's mailbox, a new deputy in a tagged
**precinct**, or the **Receptionist**. Deputies work **cases** to completion, reporting
**plan / milestone / result**, and — for a long job — **submit + sleep** so the **job
manager** can event-wake them. The **Sheriff** (left) supervises deputies and precinct
records with **zero-API monitoring**, spending tokens only on ledger compaction and
change-request decisions. A read-only **dashboard** (right) observes it all. The
framework ships only the Sheriff and Receptionist; every other precinct is
user-created. See **[`design/POSSE_DESIGN.pdf`](design/POSSE_DESIGN.pdf)** for the full
walkthrough.

| Component | File (`infra/`) | Role |
| --- | --- | --- |
| **Message notifier** | `scratch_notify_email.py` | Outbound message to the requester (context header auto-added); logs every send. |
| **Inbox reader** | `scratch_inbox.py` | Fetch + route replies (allow-list only); two mechanical signals — reply-thread, `[precinct]` tag — else the Receptionist. |
| **Router** | `scratch_inbox_loop.sh` | Always-on. Delivers each reply to the live deputy's mailbox, or spawns a deputy for a tagged precinct. |
| **Deputy spawner** | `scratch_spawn_worker.sh` | Launch a persistent deputy (+ watchdog registration, relaunch script, done-marker, precinct/model/case stamps). |
| **Interrupt entrypoint** | `scratch_interrupt_worker.sh` | One call: inject → guard-stamp → surgical-kill → relaunch → verify. |
| **Mailbox** | `scratch_mailbox_append.sh` · `scratch_read_mailbox.sh` | Flock-safe, origin-headered channel for live mid-task feedback. |
| **Sheriff** | `scratch_sheriff.py` · `scratch_sheriff_start.sh` | Always-on system manager: zero-API monitoring; LLM only for ledger compaction + change-request decisions. |
| **Records manager** | `scratch_records.py` | The one concurrency-safe way to touch precinct records: deputies append-only, sheriff-only compaction/retraction/lifecycle. |
| **Request queue** | `scratch_sheriff_request.py` | Deputy → sheriff requests (retract, edit ledger, allocate id, precinct create/delete) with anti-impersonation. |
| **Case allocator** | `scratch_case_seq.py` | One continuous monotonic case number for every channel. |
| **Watchdog** | `scratch_watchdog.py` | Always-on. Detect killed / limit-blocked deputies, notify, auto-relaunch (the Sheriff's deputy-supervision loop). |
| **Job manager** | `scratch_jobmgr.py` | Always-on. Track all jobs; event-wake the owner on completion. |
| **Job submit / sleep** | `scratch_submit_job.sh` · `scratch_job_sleep.sh` | Launch a >50-min job, then park the owner for an event-wake. |
| **JTF bridge** | `scratch_jtf.py` | Joint task force: a lead + collaborators; each slot a precinct (new deputy) or a specific deputy (must-take). |
| **Resource manager** | `gpu_manager.py` · `submit_gpu.py` | Always-on. One contended job at a time from a file-backed queue. |
| **Registry** | `scratch_agents_registry.json` | Deputy → session-id + reply-routing keywords + description. |
| **Dashboard** | `dashboard/` | Login-gated, read-only, stdlib HTTPS UI over the live state. |

## Quickstart

Everything is Python 3.10 + bash; the dashboard needs **no pip installs**. The one-command
path does all of the below for you:

```bash
python3 setup.py            # FIRST-TIME setup only: config + seed + launch, then prints the dashboard URL
```

Once you're set up, **you never re-run `setup.py`.** To start or stop the system any time —
including **relaunching after a machine reboot** — use these two idempotent scripts from the
repo root (`setup.py` calls the first for you on the initial install):

```bash
bash posse_start.sh        # start or verify all daemons + the dashboard (re-runnable; --gpu / --dry-run)
bash posse_stop.sh         # stop the daemons + dashboard (deputies + jobs keep running)
```

**[`ONBOARDING.md`](ONBOARDING.md) is the full walkthrough** (mailbox, Claude auth,
troubleshooting); the condensed manual path, if you'd rather run each piece yourself:

```bash
# 1) Configure your instance: identity in infra/operator.json (name + email + allow-list),
#    plus state dir / dashboard bind. `python3 setup.py` writes these; or by hand:
cp infra/operator.json.example infra/operator.json && $EDITOR infra/operator.json
cp infra_env.sh my_env.sh && $EDITOR my_env.sh    # optional: state dir, conda env, dashboard bind
source ./infra_env.sh

# 2) Posse mailbox creds (dedicated account; Gmail app password) — chmod 600.
#    ~/.smtp_env:  SMTP_USER=posse.bot@gmail.com / SMTP_PASS=... / SMTP_TO=you@example.com

# 3) Claude auth — subscription (claude → /login) OR apikey (~/.anthropic_key + TSOMP_CLAUDE_AUTH=apikey)

# 3b) ChatGPT/Codex auth — OPTIONAL, only if you want ChatGPT deputies. See
#     "Using ChatGPT" below: codex login (subscription) OR ~/.openai_key (API key).

# 4) Always-on daemons (each idempotent / flock-guarded):
bash infra/scratch_jobmgr_start.sh          # job manager
bash infra/scratch_gpu_manager_start.sh     # resource (GPU) queue — optional
bash infra/scratch_inbox_loop.sh            # message router
bash infra/scratch_sheriff_start.sh         # Sheriff — system manager (+ watchdog; see ONBOARDING.md)

# 4b) Judges: install the two that ship with Posse (idempotent; setup.py does this too).
python3 infra/scratch_critic.py seed

# 5) Dashboard: register your login (email = username), then serve it.
cd dashboard
python make_register_link.py --email you@example.com --name "Your Name"   # open the printed link, set a password
bash start_dashboard.sh                      # → http://127.0.0.1:8787
#   public host (binds 0.0.0.0 + self-signed TLS):
#   INFRA_DASH_PUBLIC=1 bash start_dashboard.sh   → https://<host>:8787
```

Reach the localhost dashboard over an SSH tunnel: `ssh -L 8787:localhost:8787 <host>`.

Key env vars: `INFRA_STATE_ROOT` (state dir), `INFRA_OPERATOR_EMAIL` / `INFRA_MAIL_ALLOWED`
(your identity + the sender allow-list), `INFRA_DASH_PORT` (default `8787`),
`INFRA_DASH_PUBLIC=1` (bind `0.0.0.0` **and** enable TLS). Full list in
[`ONBOARDING.md`](ONBOARDING.md) and [`dashboard/README.md`](dashboard/README.md).

## Using ChatGPT (optional)

A case can run on Anthropic's `claude` **or** OpenAI's `codex`, chosen per case. Claude
is the default and nothing here is required for a Claude-only install.

**1. Install the Codex CLI** so `codex` is on `PATH`:

```bash
npm install -g @openai/codex
codex --version
```

Posse also finds a `codex` shipped inside the VS Code ChatGPT extension, or one named by
`TSOMP_CODEX_BIN`, so an explicit install is not strictly required — but it is the
simplest thing that keeps working across editor updates.

**2. Authenticate**, either way:

```bash
# subscription — uses your ChatGPT plan, no per-token billing (default)
codex login            # once, interactively; writes ~/.codex/auth.json

# OR API key — bills the OpenAI API account; OpenAI recommends this for automation
printf '%s' "sk-..." > ~/.openai_key && chmod 600 ~/.openai_key
export TSOMP_CODEX_AUTH=apikey
```

`infra/scratch_codex_auth.sh` is the single switch, loaded by every launcher via
`_daemon_env.sh` exactly like the Claude one. In subscription mode it *clears* any
inherited `OPENAI_API_KEY`/`CODEX_API_KEY`, so a stray key can never silently move a
subscription case onto paid tokens. It also warns when the ChatGPT access token is
within 48 h of expiry.

**3. Choose the service — and, if you want, a per-lane split.** A case belongs to ONE
deputy with ONE context. What can change over that case's life is the **model generating
it**, and the unit of choice is a **work type**, or *lane*:

| Lane | What it covers |
| --- | --- |
| **work** | thinking, planning, search, code, experiments, tests, the case file, records, messages to other agents — and **all** e-mail correspondence with you |
| **report** | a deliverable **document** a human sits down and read: a PDF and the LaTeX behind it, a README, a design doc, a slide deck |

The create-case form asks for a service+model for each lane; the report lane defaults to
**"same as work"**, so a case that ignores the split behaves exactly like a single-model
case. Set the two lanes differently — say work on `claude:opus`, report on
`chatgpt:terra` — and the deputy **switches its own model** when it starts the document:
it writes a switch request and exits, and the system manager confirms it is really gone,
carries the context across, and relaunches it on the other lane.

> **E-mail is not report writing.** The tempting rule — "anything a human reads" — would
> sweep in every ACK, milestone and FINAL, and the case would switch models a dozen times
> to write five-line notes. Correspondence stays in whatever model is running; only a
> deliverable document crosses the line, and the switch CLI **refuses** an email-shaped
> reason.

Two fidelity classes, and the difference is worth knowing:

- **Same service, different model** (e.g. `opus` → `fable`): *lossless*. Same session, one
  flag; nothing is re-read or re-summarised.
- **Across services** (`claude` ↔ `chatgpt`): *reconstructed*. The two CLIs keep
  transcripts in stores neither can read, so the outgoing transcript is rendered into a
  budgeted hand-off that seeds a fresh session on the other side.

```bash
python3 infra/scratch_model_switch.py lanes  --worker <name>   # this case's split
python3 infra/scratch_model_switch.py request --to report --reason "write the PDF report"
python3 infra/scratch_model_switch.py status --worker <name>
```

By e-mail, tag the request:

```
[infra][service:chatgpt]   in the subject
service: chatgpt           on its own line in the body — the deputy's service
model: opus                optional — the work lane's model
report_service: chatgpt    optional — the report lane's service
report_model: terra        optional — the report lane's model
```
Naming a ChatGPT model alone is enough: `model: luna` implies the `chatgpt` service. An
unrecognised value falls back rather than guessing. `python3 infra/scratch_inbox.py
email-protocol` prints the whole tag vocabulary, generated from the live registries so it
cannot go stale.

**Models** (`python3 infra/scratch_models.py list`): Claude `fable` / `opus` / `sonnet` /
`haiku`; ChatGPT `sol` (GPT-5.6 Sol) / `terra` (GPT-5.6 Terra, the default) / `luna`
(GPT-5.6 Luna) / `gpt55` (GPT-5.5). Update ids in that one file when a vendor ships a new
version — everything else reads them from there.

### The older two-agent hybrid

Before the work split there was a `claude+chatgpt` **hybrid mode**: a Claude deputy plus a
*separate* ChatGPT writer that read the artifacts and asked the deputy questions between
rounds. It still ships (`infra/scratch_hybrid.py`, archived per round under
`scratch_full_logs/hybrid/case_<n>/round_<r>/`, falling back to the deputy's own draft if
the writer dies), and it is still the only way to get a genuine second reader.

But it is **not** how a case expresses "one model works, another writes" any more. Two
agents means two contexts for one case, and everything the writer knows has to survive a
hand-off; the work split gives you the same division of labour inside a single context.
Prefer the split; reach for hybrid only when you specifically want an independent second
model reading your artifacts.

```bash
python3 infra/scratch_hybrid.py handoff --case 42 \
    --draft draft.md --target reports/case42/REPORT.tex \
    --artifact somefile.py --rounds 3
```

**Limits are per-vendor.** ChatGPT's 5-hour and weekly caps are tracked separately from
Claude's, so hitting one does not block cases on the other.

## Judges (independent review)

A **judge** is an agent that reviews a deliverable it did not produce and returns one of
three verdicts — **SIGN-OFF**, **REVISE**, **REJECT**. Each round is archived under
`infra/scratch_full_logs/critic_reviews/case_<n>/round_<r>/` (the prompt, the verdict
JSON, the full written ruling), so a sign-off is an artifact you can open rather than a
claim in a status mail. Assign one to a case and its deputy **cannot close** until the
judge signs off.

Every judge is composed from two parts: a **charter** shared by all of them (the rules,
the verdict vocabulary, the output contract, and a mandatory page-inspection pass for any
PDF) plus a per-judge **persona** that supplies taste and priority order. Two ship with
Posse:

| Judge | Best for | Leads with |
| --- | --- | --- |
| `anonymous` (default) | anything — the general-purpose reviewer | Is it TRUE, and is it READABLE by someone who was not in the room? |
| `vyas` | papers, talks, decks, posters, proposals | Clarity of communication: what is the message, is the story line clear, is the claim clear, is it visualized? |

```bash
python3 infra/scratch_critic.py list                     # the roster
python3 infra/scratch_critic.py show --critic vyas --full # the composed system prompt
python3 infra/scratch_critic.py status --case 42          # every round so far
```

`setup.py` installs both on first run. They are **not** run automatically: a deputy runs
a judge only when its case was assigned one, or when you ask — an unrequested review
costs a whole extra agent.

**Assign a judge** on the create-case form's *Judge* selector, or by tag:

```
[infra][judge:vyas]     in the subject
judge: vyas             on its own line in the body
```

**The registry is sheriff-owned.** Nobody edits it directly — adding, changing or
retiring a judge is a request the sheriff decides and journals:

```bash
python3 infra/scratch_critic.py propose --critic myjudge --op critic_add \
    --display-name "The My Judge" --description "one line" \
    --prompt-file myjudge.md --reason "why the fleet needs it" --wait 180
```

The dashboard's **Judges** tab shows the roster, each judge's full prompt, recent
rulings, and a one-question form that opens a case whose deputy *writes* the new judge's
prompt for you. The two shipped prompts live in `infra/judges/`; the seeder only ever
adds a judge that is missing, so once you edit or retire one, that decision sticks.

## Updating Posse

Posse is a git repo. Updating means **pulling the new code and restarting the
long-lived services** so they run it. Your configuration and state —
`infra/operator.json`, `~/.smtp_env`, `infra_env.local.sh`, and everything under
`infra/scratch_full_logs/` — live **outside** version control, so a pull never touches
them. `setup.py` is a **one-time installer**; you do *not* re-run it to update.

1. **See what's new.** Skim [`CHANGELOG.md`](CHANGELOG.md) — the top entry is the latest
   version. Note any entry marked **Action required** (a migration step you must run).

2. **Pull the new version.**
   ```bash
   cd <your posse checkout>
   git stash            # only if you have local edits to keep
   git pull --ff-only
   git stash pop        # only if you stashed
   ```
   If `git pull` reports a conflict in a file you edited locally, resolve it, or take
   the repo's version with `git checkout -- <file>`.

3. **Run any migration** the changelog calls out for the versions you crossed. Most
   updates need none.

4. **Restart the services so they run the new code.** The inbox router re-executes its
   Python per poll, so it picks up new code on its own; the long-lived daemons and the
   dashboard are persistent processes and must be bounced:
   ```bash
   bash posse_stop.sh    # stop the dashboard + inbox/watchdog/jobmgr/sheriff
   bash posse_start.sh   # idempotent restart of all of them  (add --gpu if you use GPUs)
   ```
   Running deputies and detached jobs are **left alone** — they finish (or get
   relaunched) on their current code, which is expected and safe.

5. **Verify.**
   ```bash
   cat VERSION                    # the version you're now on
   ```
   Open the dashboard (`https://<host>:8787`) and confirm it loads. Reply to any
   deputy's email as usual; new work runs on the new code.

## The wait discipline (why it's load-bearing)

The prompt cache has a ~5-minute TTL: a touch within it is a warm **read**; a touch
after is a cold **rewrite** of the whole context, at ~12.5× the read. So:

- **≤ 50 min → foreground-poll at ≤ 4-min intervals** — every poll lands inside the
  cache TTL as a warm read.
- **> 50 min → submit + sleep** — hand the wait to the job manager and end the turn;
  one event-wake costs a single rewrite instead of an hour of polls.

Fifty minutes is the break-even: warm polling costs ~15 reads/hr of context; an
event-wake costs one rewrite (~12.5 reads); 12.5 / 15 h ≈ 50 min. This is the single
most important operational rule in the system.

## Repository layout

```
posse/
├── assets/               # brand: Posse banner + logo (SVG/PNG) + the architecture figure
├── design/               # POSSE_DESIGN.pdf — the design paper (+ sources)
├── infra/                # the core daemon + helper scripts (+ their tests)
│   └── judges/           # the judge prompts Posse ships with (installed on first run)
├── dashboard/            # login-protected, read-only status/history dashboard (stdlib)
├── infra_env.sh          # your instance config (state dir, identity, allow-list, bind)
├── ONBOARDING.md         # step-by-step new-operator setup guide  ← start here
├── CHANGELOG.md          # version notes — one entry per push (see "Updating Posse")
├── VERSION               # the current version string
├── MIGRATION.md          # code/state split · path strategy
└── README.md             # this file
```

## Secrets — never committed

Messaging creds, API keys, OAuth tokens, and the dashboard `instance/` (password hash +
cookie secret + TLS cert) all live **outside the repo** — `~/.smtp_env`,
`~/.anthropic_key`, `~/.claude/.credentials.json`, and for ChatGPT `~/.openai_key` /
`~/.codex/auth.json` — and are covered by
[`.gitignore`](.gitignore). The code only ever *reads* them from those chmod-600 files;
a secret scan guards every commit.

---

<sub><b>Posse</b> — an operating system for long-horizon LLM-agent fleets; the mascot is
Oreo the sheriff. Internal research infrastructure — no license granted yet.</sub>
