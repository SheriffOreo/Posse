# The Posse dashboard

> The operator's view of **[Posse](../README.md)**. This page documents the dashboard
> itself; see the [project README](../README.md) for the whole system.

A small, **login-protected** web dashboard over the live system state (deputies, GPU,
job manager, usage limits, job history, case conversations). Most of it is a read-only
view, but it is **not** a read-only application: the Precincts, JTF and Docket tabs
create work, and that work runs with the same autonomous shell access as any other
deputy. Pure Python **standard library** — no pip installs, no build step.

## What it shows

**Status** (`/`, auto-refreshes every 8 s)
- Daemon health pills (watchdog / jobmgr / inbox / gpu_manager, via `tmux ls`).
- **Active workers** (state ≠ done/failed): name, state (running / parked-awaiting-job /
  parked-usage-limit / …), brief description, requester, a ● when the worker has
  unread mailbox feedback, relaunch count.
- **GPU**: gpu_manager alive?, running job, pending queue.
- **Job manager**: active jobs (pending / running / sleeping / wakes) + owning agent.
- **Usage-limit banner** when a Claude limit is hit (kind + reset time + source worker).

**History** (`/history`)
- A **calendar**; days with activity show a count. Click a day →
- that day's tasks as **lineage mini-trees** (grouped by root, not a flat list): each
  task nested under its parent, edges colour-labelled by inference confidence, and
  parents/children from *other* days shown muted as **context** links. Plus the day's
  **jobs** (status, owner, command); click a job → detail + **deliverable download**.
- Click any task → its **full, untruncated conversation** (inbound email bodies in a
  scrollable panel, **attached images rendered inline**), a **lineage** view (ancestors →
  this task → follow-up children), and a **DELIVERABLES** section linking that task's
  job artifacts (output / stdout / stderr) and matching `reports/` files — every link
  served through the guarded `/download` endpoint.
- On History the task **conversation/detail panel is sticky** — it
  follows the scroll and updates in place, so clicking a job at the bottom of a long
  day-list shows the detail where you are rather than jumping to the top. A
  conversation taller than the viewport **scrolls inside the panel** (the panel is
  capped at `100vh − 80px`), so the last messages and deliverables stay reachable.

**Docket** (`/entries`) — standing work that runs on a schedule (Case 761)
- A **entry** is a saved spec plus a time: either a single **case** in one precinct
  (with its work split and judge) or a whole **JTF** (lead + numbered collaborators,
  each with its own model and split). It also holds the prompt the run is launched with.
- Two lists: **On the docket** (scheduled) and **Stood down** (paused, or a spent
  one-off) with a **Resume** button. Every entry carries **Edit** (prompt, precinct or
  composition, models, work split, judge, time, repeat), **Pause/Resume** and **Cancel**;
  a cancelled entry is moved to `docket/cancelled/`, never deleted.
- Repeats: once / hourly / daily / weekly / monthly. The prompt may carry date
  placeholders that are filled in at launch — `{date}`, `{time}`, `{datetime}`,
  `{weekday}`, and `{d:FMT}` / `{d+N:FMT}` / `{d-N:FMT}` (strftime of the fire date,
  shifted N days).
- When a entry comes due, the runner drops a **brand-new** submission into
  `web_cases/pending/` or `jtf/pending/` — the same queues this dashboard's Create-case
  and New-JTF forms write — and the inbox loop spawns **fresh** deputies from it. No
  worker from a previous run is ever resumed or reused.
- The schedule store, the arithmetic and every validation rule live in
  `infra/scratch_docket.py`; the dashboard reads through that CLI and delegates every
  write to it. The **docket** daemon (started by `posse_start.sh`) runs it once a
  minute — without that daemon entries still save, but nothing ever fires.

**Field Guide** (`/field-guide`) — the standing output standards every deputy is launched with
- One section per category. This release ships **code** and **report**; each opens
  folded, with a button to read the standing text.
- The text is **sheriff-owned**: nothing on this page edits it. Each section has a
  button asking the sheriff to review that category, and a box for asking the
  **receptionist** for a change — a new section, or different wording in one that
  exists. Both open work; neither writes policy.
- Deputies may file optional **lessons** from the CLI. They are shown here, never
  injected into a launch prompt, and a category is reviewed automatically once ten
  are pending.

**Appearance** — a **day/night toggle** (moon/sun button in the nav) switches between
the default dark palette and a light theme; the choice is saved in `localStorage`
(`infra-theme`) and applied before first paint (no flash). The palette is a set of
CSS custom properties, so night mode is unchanged from before.

## Run it

> Full first-time setup is in the repo-root [`../ONBOARDING.md`](../ONBOARDING.md).

```bash
cd dashboard
# 1) register the operator account — your EMAIL is the username; you set the password
#    via a one-time link (no password typed on the CLI):
python3 make_register_link.py --email you@example.com --name "Your Name"   # open the printed URL
#    (or set/reset a password directly: python3 set_password.py [--email you@example.com])
# 2a) localhost only (reach via SSH tunnel):
bash start_dashboard.sh                  # http://127.0.0.1:8787
# 2b) public host, no tunnel — binds 0.0.0.0 AND enables TLS (encrypted login):
INFRA_DASH_PUBLIC=1 bash start_dashboard.sh
#     → https://<this-host>:8787   (CA-signed leaf from gen_ca_cert.sh, generated once
#       into instance/; trust instance/rootCA.pem once on the browsing device — see below)
```

`start_dashboard.sh` runs the server in its own auto-restarting `infra_dashboard`
tmux session (idempotent; it is **not** one of the always-on daemons and never
touches them).

**Localhost mode** — tunnel from your laptop:

```bash
ssh -L 8787:localhost:8787 <this-host>
# then browse http://localhost:8787
```

**Public mode** (`INFRA_DASH_PUBLIC=1`) — browse `https://<this-host>:8787`
directly on the LAN. The cert is a **CA-signed leaf** minted once by
`gen_ca_cert.sh` (a small local Root CA, `instance/rootCA.pem`, signs the host's
leaf `instance/cert.pem`). To make the browser show a normal padlock, install
`instance/rootCA.pem` **once** on the viewing device (macOS: double-click →
Keychain Access → *Always Trust*); after that no warning appears. Until the root is
trusted, the connection is still encrypted but the browser shows a one-time "not
private" warning → **Advanced → Proceed**. To bind publicly over plain HTTP instead
(password travels **cleartext** on the LAN — not recommended), use
`INFRA_DASH_HOST=0.0.0.0 INFRA_DASH_TLS=0 bash start_dashboard.sh`.

> Rotating the cert: delete `instance/cert.pem` + `instance/key.pem` and re-run
> `bash gen_ca_cert.sh` (keeps the same root, so already-trusting devices need no
> action). Delete `instance/rootCA.*` too to rotate the root (devices must re-trust).
> `instance/*.key` are private — never share them; only the `*.pem` certs are public.

## Configuration (env vars)

| var | default | meaning |
|-----|---------|---------|
| `INFRA_STATE_ROOT` | `../infra` (shipped beside the dashboard) | dir the daemons read/write; the dashboard reads it, and writes only the queue records its forms create |
| `INFRA_DASH_HOST` | `127.0.0.1` | bind address; `0.0.0.0` = all interfaces (public) |
| `INFRA_DASH_PORT` | `8787` | port |
| `INFRA_DASH_PUBLIC` | `0` | `1` = shortcut for host `0.0.0.0` + TLS on (public, encrypted) |
| `INFRA_DASH_TLS` | `0` | `1` = wrap the socket with stdlib `ssl`; adds `Secure` to the cookie |
| `INFRA_DASH_CERT` / `INFRA_DASH_KEY` | `instance/cert.pem` / `instance/key.pem` | TLS cert + key (CA-signed leaf from `gen_ca_cert.sh`; falls back to self-signed if that script is absent) |
| `INFRA_DASH_INSTANCE` | `dashboard/instance` | where the password hash + cookie secret (+ TLS cert/key) live (git-ignored) |
| `INFRA_DASH_PASSWORD` | — | one-shot, only read by `set_password.py --env` |

## Security notes

- **Controlled actions.** Read pages never mutate state. Authenticated action endpoints
  validate a narrow request and hand it to the owning subsystem (for example, a watchdog
  control order or a sheriff request); the Field Guide page can queue a review but cannot
  edit standing guidance or run the sheriff model from the web request.
- **Auth.** Login form → PBKDF2-SHA256 password check (constant-time) → signed,
  expiring, `HttpOnly` `SameSite=Strict` session cookie (HMAC-SHA256). In-memory
  backoff after repeated failures. The plaintext password is never stored.
- **Downloads are sandboxed**: a requested file is served only if its *realpath* is
  under a whitelisted root (`reports/`, `outputs_*`, `scratch_full_logs/`,
  `gpu_queue/logs/`, `eval/`) and matches none of the secret denylist
  (`credentials.json`, `.smtp_env`, `.anthropic_key`, keys, `auth.json`, …). Blocks
  `../` traversal and secret leakage; 50 MB cap. Relative paths (e.g. a job's
  `output_path`) resolve against `INFRA_STATE_ROOT`, never the server's cwd.
- **Public bind + TLS.** Default bind is localhost (reach via SSH tunnel). To serve
  on the LAN without a tunnel, use `INFRA_DASH_PUBLIC=1` — it binds `0.0.0.0` **and**
  turns on TLS (stdlib `ssl`, CA-signed leaf in `instance/`; trust `instance/rootCA.pem`
  once per device for a clean padlock), so the login password is encrypted in transit
  and the session cookie gets the `Secure` flag. Plain-HTTP
  public bind is possible (`INFRA_DASH_TLS=0`) but sends the password **cleartext** on
  the wire — keep this on the CMU LAN only; never internet-expose or port-forward it.
- Security headers on every response: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a restrictive CSP (`img-src 'self' data:`).

### Fully automated — no Claude/Anthropic API

The served dashboard is **pure Python standard library**. It makes **no
Claude/Anthropic API calls, starts no agent itself, and issues no external network
requests** — so the page cannot spend your usage limit by being open or refreshed.

What it does do: it reads the live state files, and on an authenticated write it runs
one of the repo's own CLIs (`scratch_records.py`, `scratch_case_seq.py`,
`scratch_docket.py`, `scratch_field_guide.py`) or drops a record into a queue. That is
how a new case, a JTF, a docket entry or a policy request is created. The daemons, not
this server, then launch the agent — which is why creating work here costs exactly what
creating it by email costs. `tmux ls` is used to show daemon health. Verify:

```bash
grep -rInE 'anthropic|claude|ANTHROPIC_API|api\.anthropic|subprocess|os\.system|requests\.|urllib\.request|http\.client' dashboard/*.py
```

The only hits are `.anthropic_key` in the download **denylist**, the string
"claude_infra" in docstrings, and the `subprocess.run(["tmux","ls"])` health check —
no LLM calls. So the dashboard runs unattended with zero token cost.

## Files

| file | role |
|------|------|
| `server.py` | stdlib HTTP server: routing, auth gate, JSON APIs, guarded `/download` |
| `state.py` | read-only readers over watchdog roster / jobs / gpu_queue / limit state |
| `lineage.py` | task-lineage reconstruction + per-task email-thread assembly (History day trees, each case's conversation panel, JTF/Docket agent search) |
| `auth.py` | password hashing + signed-cookie sessions + login backoff |
| `pages.py` | HTML/CSS/JS (status + history are JS-driven off the JSON APIs) |
| `config.py` | paths + bind + download-safety config (all env-overridable) |
| `docket_ui_test.py` | offline tests for the Docket tab (render, routes, delegate-only writes) |
| `guidance_ui_test.py` | offline tests for the Field Guide tab (folding, the receptionist box, delegate-only writes) |
| `state_test.py` | offline tests for the readers, including which controls an install may offer |
| `set_password.py` | set/reset the login password |
| `make_register_link.py` | mint the one-time registration URL (first-password bootstrap) |
| `start_dashboard.sh` | launch/verify the auto-restarting `infra_dashboard` tmux session (+ public/TLS) |
| `run.sh` | minimal start script (ensures a password, exports env, launches) |

## Task lineage — how reliable is it?

The standalone Lineage **tab** was removed in Case 761 (Feng: the forest view was not
useful). The reconstruction below still runs: it builds History's day mini-trees, the
ancestors/children strip on a case's conversation panel, and the task titles the
JTF/Docket agent picker searches.

A follow-up is launched as a **new** case, and its parent link **is** persisted:
`scratch_inbox_handle.sh` stamps `parent_task:` onto every new spec via
`scratch_task_parent.py`, so those edges are exact. Older cases, and any spec that
predates the stamp, are reconstructed best-effort per edge from explicit
`task_A->B->C` chains, "follow-up of Task N" phrasing, then normalized-subject email
threading. Each edge is labelled with its basis in the UI.
