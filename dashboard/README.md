# Claude Infra Dashboard

A small, **login-protected, read-only** web dashboard over the live infra state
(workers, GPU, job manager, usage limits, job history + task conversations &
lineage). Pure Python **standard library** — no pip installs, no build step.

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
- On History (and Lineage) the task **conversation/detail panel is sticky** — it
  follows the scroll and updates in place, so clicking a job at the bottom of a long
  day-list shows the detail where you are rather than jumping to the top.

**Appearance** — a **day/night toggle** (moon/sun button in the nav) switches between
the default dark palette and a light theme; the choice is saved in `localStorage`
(`infra-theme`) and applied before first paint (no flash). The palette is a set of
CSS custom properties, so night mode is unchanged from before.

## Run it

```bash
cd dashboard
# 1) set a login password (stored only as a salted PBKDF2 hash under instance/)
python3 set_password.py                 # or: INFRA_DASH_PASSWORD=... python3 set_password.py --env
#    (first-time bootstrap without the CLI: python3 make_register_link.py → open the URL)
# 2a) localhost only (reach via SSH tunnel):
bash start_dashboard.sh                  # http://127.0.0.1:8787
# 2b) public host, no tunnel — binds 0.0.0.0 AND enables TLS (encrypted login):
INFRA_DASH_PUBLIC=1 bash start_dashboard.sh
#     → https://<this-host>:8787   (self-signed cert generated once into instance/)
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
directly on the LAN. The cert is self-signed, so the browser shows a one-time
"not private" warning → **Advanced → Proceed**. To bind publicly over plain HTTP
instead (password travels **cleartext** on the LAN — not recommended), use
`INFRA_DASH_HOST=0.0.0.0 INFRA_DASH_TLS=0 bash start_dashboard.sh`.

## Configuration (env vars)

| var | default | meaning |
|-----|---------|---------|
| `INFRA_STATE_ROOT` | `/home/steven/Projects/time-series-omp` | dir the daemons read/write (read-only here) |
| `INFRA_DASH_HOST` | `127.0.0.1` | bind address; `0.0.0.0` = all interfaces (public) |
| `INFRA_DASH_PORT` | `8787` | port |
| `INFRA_DASH_PUBLIC` | `0` | `1` = shortcut for host `0.0.0.0` + TLS on (public, encrypted) |
| `INFRA_DASH_TLS` | `0` | `1` = wrap the socket with stdlib `ssl`; adds `Secure` to the cookie |
| `INFRA_DASH_CERT` / `INFRA_DASH_KEY` | `instance/cert.pem` / `instance/key.pem` | TLS cert + key (self-signed, generated once by `start_dashboard.sh`) |
| `INFRA_DASH_INSTANCE` | `dashboard/instance` | where the password hash + cookie secret (+ TLS cert/key) live (git-ignored) |
| `INFRA_DASH_PASSWORD` | — | one-shot, only read by `set_password.py --env` |

## Security notes

- **Read-only.** No endpoint mutates infra state. (No "manage/kill" actions ship in v1 —
  those were left for explicit sign-off; see repo `README.md`.)
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
  turns on TLS (stdlib `ssl`, self-signed cert in `instance/`), so the login password
  is encrypted in transit and the session cookie gets the `Secure` flag. Plain-HTTP
  public bind is possible (`INFRA_DASH_TLS=0`) but sends the password **cleartext** on
  the wire — keep this on the CMU LAN only; never internet-expose or port-forward it.
- Security headers on every response: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a restrictive CSP (`img-src 'self' data:`).

### Fully automated — no Claude/Anthropic API

The served dashboard is **pure Python standard library**. It reads the live state
files **read-only**, serves HTTP(S), and does nothing else: it makes **no
Claude/Anthropic API calls, spawns no `claude` process, and issues no external
network requests**. The only subprocess it ever runs is `tmux ls` (to show daemon
health). Verify:

```bash
grep -rInE 'anthropic|claude|ANTHROPIC_API|api\.anthropic|subprocess|os\.system|requests\.|urllib\.request|http\.client' dashboard/*.py
```

The only hits are `.anthropic_key` in the download **denylist**, the string
"claude_infra" in docstrings, and the `subprocess.run(["tmux","ls"])` health check —
no LLM calls. (`lineage_figure.py` is a **standalone, offline** figure generator that
imports matplotlib; it is **not** imported by `server.py` and is never part of the
running service.) So the dashboard runs unattended with zero token cost.

## Files

| file | role |
|------|------|
| `server.py` | stdlib HTTP server: routing, auth gate, JSON APIs, guarded `/download` |
| `state.py` | read-only readers over watchdog roster / jobs / gpu_queue / limit state |
| `lineage.py` | task-lineage reconstruction + per-task email-thread assembly |
| `auth.py` | password hashing + signed-cookie sessions + login backoff |
| `pages.py` | HTML/CSS/JS (status + history are JS-driven off the JSON APIs) |
| `config.py` | paths + bind + download-safety config (all env-overridable) |
| `set_password.py` | set/reset the login password |
| `make_register_link.py` | mint the one-time registration URL (first-password bootstrap) |
| `start_dashboard.sh` | launch/verify the auto-restarting `infra_dashboard` tmux session (+ public/TLS) |
| `run.sh` | minimal start script (ensures a password, exports env, launches) |

## Task lineage — how reliable is it?

Follow-ups are launched as **new** task numbers, but the parent link is **not
persisted** today (see repo `MIGRATION.md` and `../proposed_patches/`). Lineage is
reconstructed best-effort, per edge, from: explicit `task_A->B->C` chains in specs
(high confidence), "follow-up of Task N" phrasing, then normalized-subject email
threading (fallback). Each edge is labelled with its basis in the UI. The permanent
fix is a one-line stamp in `scratch_inbox_handle.sh` (patch provided, not applied).
