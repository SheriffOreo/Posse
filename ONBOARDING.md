<p align="center">
  <img src="assets/posse_banner.png" alt="Posse" width="620">
</p>

# Onboarding — set up & run your own Posse

This is the **step-by-step guide for a new operator** standing up a Posse instance on
their own machine. Follow it top to bottom; each step is a few commands and takes ~20
minutes end to end **once you already have a dedicated mailbox and an authenticated
Claude CLI**. Creating the mailbox (a new account, 2-Step Verification, an app password)
and completing the Claude sign-in are the slow parts — do those first, or budget an hour. When you finish you will have: a running fleet manager (the
**Sheriff** + daemons), a login-gated **dashboard** at a URL you choose, and the
ability to **email a task** and watch a **deputy** work it to completion.

> New to the project? Read [`README.md`](README.md) for the what/why and
> [`design/POSSE_DESIGN.pdf`](design/POSSE_DESIGN.pdf) for the architecture. This guide
> is purely operational.

---

## Fastest path — run the setup program

Most operators should just run the **interactive installer**. It does every step in
this guide for you — asks your name + email, the posse's mailbox, Claude auth, and the
web host/port; writes all the config; seeds the front desk; registers your dashboard
login; starts the system; and prints the URL to open:

```bash
python3 setup.py               # walk through setup, then launch the system
python3 setup.py --no-launch   # configure only; start the daemons yourself later
```

It is **re-runnable and safe**: it shows your current values as defaults and only
rewrites what you change. Secrets are typed hidden (never echoed) and land only in
git-ignored files (`infra/operator.json`, `~/.smtp_env`, `~/.anthropic_key`). The
manual, step-by-step walkthrough below is the reference if you prefer to run each piece
yourself, script it, or troubleshoot.

---

## 0. The mental model (read once)

- **You are the operator.** One person runs one Posse instance. Your **email address**
  is your identity everywhere: it is your **dashboard username**, the address the posse
  **emails you** at, and the **allow-listed sender** whose messages command the fleet.
- **Email is the main control plane.** You send a task as an email; a deputy picks it
  up. You reply to steer it. An allow-list gates every sender (fail-closed). The
  dashboard is the other way in: its Precincts, JTF and Docket tabs create work too,
  behind the dashboard login rather than the mail allow-list.
- **The posse needs its own mailbox.** Create a dedicated email account (a Gmail is the
  reference) that the daemons log into to send and receive. That account is separate
  from your personal address.
- **Deputies are `claude` processes.** Each deputy is a headless Claude Code (`claude`)
  run. You need the **Claude CLI** installed and authenticated — either a **Claude Max
  subscription** or an **Anthropic API key**.
- **Where state lives.** In this release the daemons run **from the `infra/` directory**
  and keep their runtime **state** (logs, mailboxes, records, queues) under
  `infra/scratch_full_logs/` (they resolve it next to their own code). You point the
  **dashboard** at that same place with `INFRA_STATE_ROOT` so it reads what the daemons
  write. To keep state somewhere else entirely, set `INFRA_STATE_ROOT` to that
  directory for both the daemons and the dashboard.

You will configure **four** things: a **state directory**, your **identity**, the
**posse mailbox**, and **Claude auth**. Then you start the daemons and register your
dashboard login.

---

## 1. Prerequisites

| Need | Why | Check |
| --- | --- | --- |
| **Linux host** (or WSL) with `bash`, `tmux`, `git` | daemons run as long-lived tmux sessions | `tmux -V && git --version` |
| **Python 3.10** | daemons + dashboard are **pure standard library** — no pip installs, no conda env required | `python3 --version` |
| **Claude CLI** (`claude`, a.k.a. Claude Code) | every deputy is a `claude` process | `claude --version` |
| **A dedicated email account** for the posse | the email control plane (Gmail reference) | you can create an app password |
| **Claude Max subscription _or_ Anthropic API key** | pays for deputy runs | you can `/login` or have a key |

Install the Claude CLI per Anthropic's docs (`npm i -g @anthropic-ai/claude-code`, then
`claude` once to confirm). The dashboard is pure Python standard library — nothing to
pip install.

---

## 2. Get the code

```bash
git clone https://github.com/SheriffOreo/Posse.git
cd Posse
```

Everything below is run from this directory.

---

## 3. Configure your instance (`infra_env.sh`)

`infra_env.sh` is the tracked template holding your instance's non-secret
configuration. The defaults already work for a fresh clone; open it and set your
**identity** and **allow-list**:

> **If you ran `setup.py`, edit `infra_env.local.sh` instead.** The installer writes your
> answers to that file, and `posse_start.sh` loads it *instead of* this template — it does
> not merge the two. So after an installer run, changes to `infra_env.sh` have no effect.
> Edit the template only if you are configuring by hand and never ran the installer.
> `infra_env.local.sh` is git-ignored, so `git pull` never touches your settings.

```bash
# YOUR IDENTITY  (your address is your dashboard username + where the posse mails you)
export INFRA_OPERATOR_EMAIL="you@example.com"
export INFRA_OPERATOR_NAME="Your Name"          # used in the "Hi <name>," greeting

# WHO MAY COMMAND THE FLEET  (comma-separated allow-list)
# Defaults to INFRA_OPERATOR_EMAIL above — so if you leave that blank, this is EMPTY
# and the posse ignores every message it receives. Set both.
export INFRA_MAIL_ALLOWED="you@example.com"

# STATE ROOT — leave the default: the daemons run from infra/ and keep state under
# infra/scratch_full_logs; this points the dashboard at that same place.
# INFRA_CODE_ROOT is set for you at the top of this file (it auto-detects the repo
# directory), so use the line below as-is rather than pasting it into a bare shell.
export INFRA_STATE_ROOT="$INFRA_CODE_ROOT/infra"

# OPTIONAL — run the daemons inside a conda env (unset => system python3 is fine)
export INFRA_CONDA_ENV=""

# DASHBOARD BIND (defaults shown; see Step 6)
export INFRA_DASH_HOST="127.0.0.1"
export INFRA_DASH_PORT="8787"
```

Then **source it in the shell you will start the daemons from** — the tmux daemons
inherit this environment:

```bash
source ./infra_env.sh          # or ./infra_env.local.sh if the installer wrote one
```

> **Why the allow-list matters.** `INFRA_MAIL_ALLOWED` is a **fail-closed** gate: only
> mail from those exact addresses is ever routed or acted on; everything else is left
> untouched. It defaults to `INFRA_OPERATOR_EMAIL`, so setting your identity is usually
> enough — but if you leave your email blank, the allow-list is **empty** and the posse
> ignores every message you send it, with no error anywhere. If you emailed a task and
> nothing happened, check this first. To let a teammate send tasks later, add their
> address here (Step 10).

---

## 4. Set up the posse's mailbox (`~/.smtp_env`)

The daemons need an email account to **send from** and **receive at**. Create a
dedicated account (a Gmail is the reference implementation) and an **app password**
(Gmail → Account → Security → 2-Step Verification → App passwords). Then write the
credentials to `~/.smtp_env`:

```bash
cat > ~/.smtp_env <<'EOF'
SMTP_USER=posse.bot@gmail.com     # the posse's own account (NOT your personal address)
SMTP_PASS=your_gmail_app_password # 16-char app password, no spaces
SMTP_TO=you@example.com           # default recipient = YOU (the operator)
EOF
chmod 600 ~/.smtp_env
```

- `SMTP_USER` / `SMTP_PASS` — the posse account the daemons log into (IMAP to read,
  SMTP to send).
- `SMTP_TO` — where notifications go by default: **your** address.
- **Not on Gmail?** Add `SMTP_HOST` / `SMTP_PORT` (SMTP-over-SSL) and `IMAP_HOST` /
  `IMAP_PORT` (IMAP-over-SSL) to `~/.smtp_env`; they default to Gmail
  (`smtp.gmail.com:465` / `imap.gmail.com:993`).

Send yourself a test message to confirm the credentials work:

```bash
python3 infra/scratch_notify_email.py "Posse test" "hello from my posse" \
    --agent setup --to "$INFRA_OPERATOR_EMAIL"
```

You should receive it within a few seconds. (The mailer keeps a log at
`$INFRA_STATE_ROOT/scratch_full_logs/sent_emails.jsonl`.)

> **Never commit secrets.** `~/.smtp_env` lives outside the repo and is chmod 600. The
> repo's `.gitignore` also blocks it if you ever copy it in.

---

## 5. Authenticate Claude (subscription **or** API key)

Every deputy is a `claude` process, so the CLI must be logged in. Posse supports **both**
billing modes through one switch, `TSOMP_CLAUDE_AUTH` (read by
`infra/scratch_claude_auth.sh`, which every launcher sources):

**Option A — Claude Max subscription (default).** Interactively log the CLI in once:

```bash
claude    # then run  /login  and complete the browser flow
```

This stores OAuth tokens at `~/.claude/.credentials.json` (chmod 600). Deputy usage
bills to your Max subscription and shares its rate limits. Nothing else to set —
`TSOMP_CLAUDE_AUTH` defaults to `subscription`, which clears any API key so the CLI uses
the OAuth login.

**Option B — Anthropic API key (pay-per-token).** Put your key in a file and flip the
switch:

```bash
printf '%s' 'sk-ant-...' > ~/.anthropic_key && chmod 600 ~/.anthropic_key
export TSOMP_CLAUDE_AUTH=apikey        # add this to infra_env.sh to make it permanent
```

Either way the key/tokens live only in those chmod-600 files and are never committed.

### Do deputies need a Claude permission bypass? (yes — it's built in)

A deputy runs **headless**, with no human to approve each tool call, so every launcher
starts `claude` with `--dangerously-skip-permissions`. **You do not add this yourself** —
it ships in the spawn/relaunch scripts. Practically: once you send a task, the deputy runs
shell commands and edits files autonomously within its lane. The only one-time step on your
side is running `claude` once interactively to accept the folder-trust prompt (and finish
`/login`), so the headless runs start cleanly.

---

## 6. Choose the web host & port

The dashboard is a login-gated web UI. It is mostly a **read-only** view of what the
daemons are doing, but it is not read-only: the Precincts, JTF and Docket tabs
**create work** — a new case, a joint task force, or a schedule — and that work runs
with the same autonomous shell access as any other deputy. Protect it accordingly.
Two ways to reach it:

**Localhost + SSH tunnel (default, safest).** It binds `127.0.0.1:8787`; reach it from
your laptop over a tunnel:

```bash
ssh -L 8787:localhost:8787 <your-host>     # then browse http://localhost:8787
```

**Public bind with TLS (no tunnel).** Bind all interfaces and serve HTTPS with a
self-signed cert (generated automatically on first start):

```bash
export INFRA_DASH_PUBLIC=1     # binds 0.0.0.0 AND enables TLS  ->  https://<host>:8787
```

Knobs (all optional; defaults shown): `INFRA_DASH_HOST=127.0.0.1`,
`INFRA_DASH_PORT=8787`, `INFRA_DASH_TLS=0`. `INFRA_DASH_PUBLIC=1` is the convenience
switch that sets host `0.0.0.0` **and** `INFRA_DASH_TLS=1`. If you bind publicly, make
sure your firewall only exposes the port to trusted networks.

---

## 7. Register your dashboard account

The dashboard has one operator account. Your **email is the username**; you set the
**password** through a one-time link (so no password is ever typed on a command line).

```bash
cd dashboard
python3 make_register_link.py --email "$INFRA_OPERATOR_EMAIL" --name "$INFRA_OPERATOR_NAME"
# prints:  http://localhost:8787/register?token=...
cd ..
```

Open that URL (through your SSH tunnel if you're using localhost). The page shows your
email as the account username and asks you to choose a password (min 8 chars). Submit —
the link is single-use and now expires. You will sign in with that password; the nav bar
shows your email as the signed-in account.

- **Lost the password?** Delete `dashboard/instance/auth.json`, then re-run
  `make_register_link.py` for a fresh link.
- The password is stored only as a salted PBKDF2 hash under the git-ignored
  `dashboard/instance/` (chmod 600); the plaintext is never written anywhere.

---

## 8. Start the system

**One command — `bash posse_start.sh`** — starts (or verifies) the whole system: the
daemons (jobmgr, sheriff, docket, inbox, watchdog) **and** the dashboard. It is **idempotent**,
so re-run it any time (after a reboot, after editing config) — every service is guarded
by a tmux session check, so nothing is ever started twice.

```bash
bash posse_start.sh            # start or verify all daemons + the dashboard
bash posse_start.sh --gpu      # also start the optional GPU resource manager
bash posse_start.sh --dry-run  # preview what WOULD start (changes nothing)

bash posse_stop.sh             # stop the daemons + dashboard (deputies + jobs keep running)
```

`setup.py` runs `posse_start.sh` for you at the end of onboarding, so the first launch
and every later restart are identical.

<details><summary><b>What <code>posse_start.sh</code> does under the hood</b> — the equivalent manual steps</summary>

Each starter is **idempotent** and flock-guarded — safe to re-run; each runs from
`infra/` and needs no conda env. Run these from the shell where you sourced
`infra_env.sh`:

```bash
# 1) Job manager — tracks jobs; event-wakes a sleeping deputy when a long job finishes
bash infra/scratch_jobmgr_start.sh

# 2) Resource (GPU) queue — OPTIONAL; only if deputies run contended GPU jobs
bash infra/scratch_gpu_manager_start.sh

# 3) Message router — reads the posse mailbox and routes each message
#    (this one runs in the FOREGROUND loop; run it in its own tmux window/pane)
tmux new-session -d -s inbox "bash -lc 'bash $INFRA_CODE_ROOT/infra/scratch_inbox_loop.sh'"

# 4) Deputy supervisor (the Sheriff's liveness loop) — relaunches crashed/limit-blocked deputies
tmux new-session -d -s watchdog \
  "bash -lc 'source \"$INFRA_CODE_ROOT/infra/_daemon_env.sh\" && python3 scratch_watchdog.py 2>&1 | tee -a scratch_full_logs/watchdog_stdout.log'"

# 5) Sheriff — the system manager: keeps precinct records healthy (zero-API monitoring)
bash infra/scratch_sheriff_start.sh

# 6) Docket runner — fires the scheduled entries on the Docket tab, once a minute
bash infra/scratch_docket_start.sh
```

Then the dashboard:

```bash
cd dashboard
bash start_dashboard.sh                 # localhost:8787  (or INFRA_DASH_PUBLIC=1 for TLS)
cd ..
```

</details>

**Verify** everything is up:

```bash
tmux ls                                 # expect: jobmgr, inbox, watchdog, sheriff, docket, infra_dashboard (+ gpu_manager if started)
curl -sk http://localhost:8787/healthz  # -> {"ok": true}   (use https:// if TLS is on)
```

Sign in at your dashboard URL. You should see the Status page (empty for now).

---

## 9. Create your precincts

Work is organized into **precincts** — the domains you operate in (e.g. `research`,
`development`, `ops`). Posse ships only the built-in **Receptionist** (the front desk);
**you create the rest.** Register one per domain:

```bash
python3 infra/scratch_records.py directory register \
  --name research --description "literature + experiments" --model opus
python3 infra/scratch_records.py directory register \
  --name ops --description "infrastructure + deployments" --model opus
```

`--model` sets the default Claude model for deputies spawned in that precinct (`fable` /
`opus` / `sonnet` / `haiku`). Each precinct gets its own records — a compacted **ledger**
(big-picture digest), an append-only **case log**, and per-case files — all under
`$INFRA_STATE_ROOT/scratch_full_logs/records/<precinct>/`. They appear on the dashboard's
**Precincts** page.

The command confirms with a line like `registered precinct[research] mode=mutable
model=opus`. **Mode** is whether the sheriff may rewrite that precinct's ledger as it
grows: `mutable` (the default, and what you want) lets it compact the ledger; `fixed` is
reserved for the Receptionist, whose ledger is a description of the system rather than a
record of work. A new precinct's directory starts with only `cases/` — the ledger and
case log are written when its first case closes, so an empty-looking directory right
after registering is expected.

---

## 10. Send your first task

Email the **posse account** (`SMTP_USER`) from your **operator address** (an
allow-listed sender), tagging the precinct with `[precinct]` in the subject:

```
To:      posse.bot@gmail.com
Subject: [research] Summarize the three papers in refs/ and email me a one-pager
Body:    Attach any files you need. Send a plan, a milestone, and a final.
```

Within a poll cycle the router spawns a **deputy** in the `research` precinct. It will
email you a short **plan**, then **milestones**, then a **final** result. Watch it live
on the dashboard's **Status** page; the case and its lineage show up under **History**.

**Steer it mid-task** by simply **replying** to any of its emails — your reply is
injected into the deputy's mailbox and it re-reads it without losing its place (its
running jobs survive). To route work without spawning a new deputy, reply on the thread.

That's it — you're operating a Posse.

---

## Onboarding a teammate

The dashboard is a **single operator account**, and the fleet is **single-tenant**. To
bring in a collaborator:

- **Let them send tasks / steer deputies:** add their email to `INFRA_MAIL_ALLOWED`
  (comma-separated) and re-source `infra_env.sh` — the router picks it up on its next
  pass. Their replies now command the fleet like yours.
- **Let them watch the dashboard:** share the dashboard URL + password, or (better) put
  the dashboard behind your own SSO/reverse proxy. Per-user dashboard accounts are not
  part of this single-tenant design.
- **A separate person running their own fleet** should stand up their **own** Posse
  instance (their own state dir, mailbox, and dashboard) by following this guide.

---

## Operating notes

- **The wait discipline (load-bearing).** A deputy waiting on a job either **polls**
  (short jobs, ≤ 4-min intervals — every poll is a cache-warm read) or **submits + sleeps**
  (long jobs — the job manager event-wakes it). This keeps token cost bounded, and it is why a
  deputy waiting on a long job costs almost nothing while it waits.
- **Start / stop the system.** `bash posse_start.sh` starts or verifies everything;
  `bash posse_stop.sh` stops the daemons + dashboard (deputies and jobs in flight keep
  running). Both take `--dry-run` (preview) and `--gpu` (include the GPU manager).
- **Daemons are self-healing.** The starters are idempotent; re-run any of them any time.
  The watchdog relaunches crashed or rate-limited deputies. **After a reboot, just run
  `bash posse_start.sh`.**
- **Deputies launch with the Field Guide.** The **Field Guide** tab holds your standing
  output standards; this release ships **code** and **report**, and every deputy is
  launched with them. The text is sheriff-owned, so the page can ask for a change but
  never makes one — use the section's review button, or the box that opens a
  receptionist case for a new section or new wording.
- **Recurring work goes on the Docket.** The **Docket** tab saves a case or a whole JTF
  plus a schedule; when one comes due the docket daemon launches **fresh** deputies from
  it. Nothing from a previous run is reused.
- **Secrets never touch git.** `~/.smtp_env`, `~/.anthropic_key`,
  `~/.claude/.credentials.json`, and `dashboard/instance/` all live outside the repo and
  are chmod 600. A secret scan should be part of every commit.

---

## Troubleshooting

| Symptom | Likely cause & fix |
| --- | --- |
| Test email never arrives | Wrong `SMTP_USER`/`SMTP_PASS` (use a Gmail **app password**, not the login password), or 2-Step Verification not enabled. Re-run the Step 4 test command and read the error. |
| You emailed a task but nothing happened | Your address isn't in `INFRA_MAIL_ALLOWED` (fail-closed), or the inbox router (`tmux` session `inbox`) isn't running. Check `tmux ls` and `$INFRA_STATE_ROOT/scratch_full_logs/inbox_agent.log`. |
| Deputy spawns then dies immediately | Claude CLI not authenticated. Run `claude` → `/login` (subscription) or set `~/.anthropic_key` + `TSOMP_CLAUDE_AUTH=apikey`. |
| Dashboard shows "Password not set" | Run the Step 7 registration link (or `python3 dashboard/set_password.py`). |
| `/healthz` refused | Dashboard tmux (`infra_dashboard`) not up, or wrong scheme — use `https://` when `INFRA_DASH_TLS=1`. Check `dashboard/instance/` for the cert and the wrapper log. |
| Dashboard is empty although deputies are running | The dashboard's `INFRA_STATE_ROOT` doesn't point at where the daemons write (`infra/scratch_full_logs`). Set `INFRA_STATE_ROOT="$INFRA_CODE_ROOT/infra"` before `start_dashboard.sh`. |

---

## Quick reference — environment

| Variable | Where | Meaning |
| --- | --- | --- |
| `INFRA_STATE_ROOT` | `infra_env.sh` | where the dashboard reads state (= `infra/`, where the daemons write) |
| `INFRA_CONDA_ENV` | `infra_env.sh` | optional conda env for the daemons (unset = system `python3`) |
| `INFRA_OPERATOR_EMAIL` | `infra_env.sh` | your address: dashboard username + default recipient |
| `INFRA_OPERATOR_NAME` | `infra_env.sh` | name used in the "Hi &lt;name&gt;," greeting |
| `INFRA_MAIL_ALLOWED` | `infra_env.sh` | comma-separated allow-list of senders who may command the fleet |
| `INFRA_DASH_HOST` / `INFRA_DASH_PORT` | `infra_env.sh` | dashboard bind (default `127.0.0.1:8787`) |
| `INFRA_DASH_PUBLIC=1` | shell | bind `0.0.0.0` + enable TLS |
| `TSOMP_CLAUDE_AUTH` | shell | `subscription` (default) or `apikey` |
| `SMTP_USER` / `SMTP_PASS` / `SMTP_TO` | `~/.smtp_env` | posse mailbox creds + default recipient |
| `SMTP_HOST` / `IMAP_HOST` (+ `_PORT`) | `~/.smtp_env` | override the default Gmail endpoints |

<sub>Questions the design paper answers: why identity is decoupled from process
liveness, why token cost is scheduled, and how the Sheriff manages the fleet — see
<a href="design/POSSE_DESIGN.pdf">design/POSSE_DESIGN.pdf</a>.</sub>
