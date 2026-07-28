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
- everything **launched** that day: **tasks** and **jobs** (status, owner, command). Click a
  job → detail + **deliverable download** (output / stdout / stderr). Click a task →
- the **conversation** (email thread, reconstructed) + a **lineage** view (ancestors →
  this task → follow-up children), each edge labelled by how it was inferred.

## Run it

```bash
cd dashboard
# 1) set a login password (stored only as a salted PBKDF2 hash under instance/)
python3 set_password.py                 # or: INFRA_DASH_PASSWORD=... python3 set_password.py --env
# 2) start (localhost:8787 by default)
./run.sh
```

From your laptop, tunnel and open a browser:

```bash
ssh -L 8787:localhost:8787 <this-host>
# then browse http://localhost:8787
```

## Configuration (env vars)

| var | default | meaning |
|-----|---------|---------|
| `INFRA_STATE_ROOT` | `/home/steven/Projects/time-series-omp` | dir the daemons read/write (read-only here) |
| `INFRA_DASH_HOST` | `127.0.0.1` | bind address — keep localhost unless fronted by TLS + auth |
| `INFRA_DASH_PORT` | `8787` | port |
| `INFRA_DASH_INSTANCE` | `dashboard/instance` | where the password hash + cookie secret live (git-ignored) |
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
  `../` traversal and secret leakage; 50 MB cap.
- **Bind localhost.** Reach it via SSH tunnel. If you must bind wider, put it behind a
  TLS reverse proxy and add the `Secure` cookie flag (see `auth.py`).
- Security headers on every response: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a restrictive CSP.

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
| `run.sh` | start script (ensures a password, exports env, launches) |

## Task lineage — how reliable is it?

Follow-ups are launched as **new** task numbers, but the parent link is **not
persisted** today (see repo `MIGRATION.md` and `../proposed_patches/`). Lineage is
reconstructed best-effort, per edge, from: explicit `task_A->B->C` chains in specs
(high confidence), "follow-up of Task N" phrasing, then normalized-subject email
threading (fallback). Each edge is labelled with its basis in the UI. The permanent
fix is a one-line stamp in `scratch_inbox_handle.sh` (patch provided, not applied).
