# MIGRATION — moving the infra into `claude_infra`

This documents how the live tsomp orchestration infra maps into this repo, the path
strategy, and the (deferred, opt-in) cutover to run the daemons from here. The
guiding constraint: **the live daemons must keep working throughout.** Deliverable
#1 is a populated + committed repo with the daemons **still running unchanged** from
tsomp; any cutover is a separate, explicitly-approved step.

---

## 1. Code vs state split

| | What | Where it lives | In git? |
| --- | --- | --- | --- |
| **CODE** | the ~26 daemon + helper files (`scratch_watchdog.py`, `scratch_jobmgr.py`, `gpu_manager.py`, `scratch_inbox*.{py,sh}`, spawn/relaunch/interrupt/detach/snapshot/auth/submit/sleep helpers) + 2 companions (`scratch_spawn_paper_worker.sh`, `submit_gpu.py`) | `claude_infra/infra/` | **yes** |
| **DOCS + DASHBOARD** | this file, READMEs, `.gitignore`, `infra_env.sh`, `dashboard/`, `proposed_patches/` | `claude_infra/` | **yes** |
| **RUNTIME STATE** | `scratch_full_logs/` (2.3 GB: logs, per-worker prompts + relaunch scripts, mailboxes, receipts, `sent_emails.jsonl`, `watchdog_jobs.json`, `jobs/`), `gpu_queue/` (93 MB), `scratch_agents_registry.json`, `inbox_blocked.json` | tsomp working dir (`INFRA_STATE_ROOT`) | **no** — git-ignored |
| **SECRETS** | `~/.smtp_env`, `~/.anthropic_key`, `~/.claude/.credentials.json`, the PAT in the git remote URL | outside any repo (chmod 600) | **no** — never |

Rationale: the state is large, machine-local, and rewritten continuously by the live
daemons; versioning it is pointless and would bloat the repo. The repo is the
**code** home. The daemons keep reading/writing state in place.

---

## 2. Path strategy

### The key finding
The core daemons are **path-clean**: each resolves its root from
`Path(__file__).resolve().parent` (`scratch_jobmgr.py:103`, `scratch_watchdog.py:73`,
`gpu_manager.py:21`, …), **not** a hardcoded tsomp path. The "459 files hardcode
`/home/steven/Projects/time-series-omp`" are the *generated* per-worker prompts and
relaunch scripts under `scratch_full_logs/` (state, not code) plus a couple of `cd`
lines in `scratch_spawn_worker.sh`. So:

- **A byte-identical copy of the code needs zero edits** to be a faithful versioned
  mirror, and copying it does **not** touch the running daemons. ✅ (done — deliverable #1)
- But because `ROOT = __file__.parent` is used for **both** locating sibling helper
  scripts **and** locating state, simply *running* a daemon from `claude_infra/infra/`
  would make it look for `scratch_full_logs/` next to the code (wrong). Running from
  here therefore requires teaching the daemons a **state root** separate from their
  **code root**.

### Options considered

| | Option | Pro | Con |
| --- | --- | --- | --- |
| **(a) ✅ recommended** | **Parameterize** `INFRA_STATE_ROOT` (default = `__file__.parent`, so byte-equivalent when unset). Code lives here; state resolves to tsomp. | Clean single home; backward-compatible (unset ⇒ identical behavior); the dashboard already works this way. | A one-time, careful diff separating "state paths" from "code/helper paths" in the daemons. Applied only at cutover. |
| (b) | Keep code physically in tsomp; make this repo the versioned home via **symlink/sync**. | Daemons stay byte-identical, zero cutover risk. | Two sources of truth; a sync/symlink farm to maintain; edits-in-repo don't take effect until synced. |
| (c) | **Full relocation** with a compatibility shim. | Clean end state. | Biggest blast radius on a live system; most cutover risk. |

**Recommendation: (a).** It yields a single clean home, is backward-compatible
(daemons behave identically until `INFRA_STATE_ROOT` is set), and matches the knob
the dashboard already honors. Until cutover, the committed copies are **byte-identical
mirrors** and the daemons keep running from tsomp — so we get deliverable #1 with
**zero** operational risk, and (a) is a small, reviewable diff applied later on your OK.

### The diff (a) entails at cutover — enumerated, NOT yet applied
In `infra/`, replace state-directory references with an `INFRA_STATE_ROOT`-derived
`STATE_ROOT` while leaving helper-script references on the code root:
- `scratch_jobmgr.py`, `scratch_watchdog.py`, `gpu_manager.py`: introduce
  `STATE_ROOT = Path(os.environ.get("INFRA_STATE_ROOT", ROOT))` and use it for
  `scratch_full_logs`, `gpu_queue`, `scratch_agents_registry.json`, `jobs/`.
- `scratch_notify_email.py:26`: `sent_emails.jsonl` path → under `STATE_ROOT`.
- `scratch_spawn_worker.sh` (lines ~30/187/191) and `scratch_gen_relaunch.sh`: the
  worker prompt's `cd /home/steven/Projects/time-series-omp` and
  `source .../scratch_claude_auth.sh` must keep pointing workers at the **tsomp working
  dir** (that is correctly `INFRA_STATE_ROOT`), while sourcing helpers from
  `INFRA_CODE_ROOT`. Parameterize both via `infra_env.sh`.
Each change defaults to today's behavior when the env vars are unset.

---

## 3. Cutover plan (⚠ deferred — run only after explicit OK)

Do **one daemon at a time**, verify, then proceed. Each has a sanctioned,
flock-guarded restart; mail/jobs survive because state is untouched and the queues
are on disk.

```bash
source /home/steven/Projects/claude_infra/infra_env.sh   # sets INFRA_STATE_ROOT

# 1) jobmgr  — idempotent starter picks up the same jobs/ dir
bash /home/steven/Projects/claude_infra/infra/scratch_jobmgr_start.sh
#    verify: tmux has-session -t jobmgr; jobs/{running,done} advancing

# 2) gpu_manager
bash /home/steven/Projects/claude_infra/infra/scratch_gpu_manager_start.sh
#    verify: gpu_queue/pending drains, done/ grows

# 3) inbox router — graceful reload of the loop
touch "$INFRA_STATE_ROOT/scratch_full_logs/inbox/RESTART_LOOP"
#    verify: send yourself a test reply; confirm routing into a mailbox

# 4) watchdog — manual bounce last (it supervises the others)
#    kill the tmux 'watchdog' window and relaunch from infra/ with INFRA_STATE_ROOT set
```

**Prereq:** apply the §2(a) diff first (otherwise a daemon started from `infra/`
looks for state beside the code). **Rollback:** restart each daemon from the tsomp
checkout exactly as today (`cd /home/steven/Projects/time-series-omp && bash
scratch_jobmgr_start.sh`, etc.) — since state never moved, rollback is instant and
lossless.

**Until cutover:** nothing changes. The daemons run from tsomp; this repo is a
mirror + the dashboard (which reads tsomp state read-only).

---

## 4. Secret hygiene (every commit)

Before the first `git add`, a strict `.gitignore` was written and a scan run over
everything staged:

```bash
# hard secret VALUES must be ZERO:
grep -rnE 'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|-----BEGIN[A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}' .
```

Result: **clean.** The only matches for `ANTHROPIC` / `SMTP_PASS` / `credentials.json`
/ `.smtp_env` are env-var **names** and file **paths** — code that *reads* secrets
from external chmod-600 files, never embedding a value. The GitHub PAT lives only in
`.git/config`'s remote URL (which git never commits) and is git-ignored everywhere
else. **Recommendation stands: rotate that PAT**, since it was exposed in a triage
session.

---

## 5. What is intentionally deferred (needs your OK)

- **`git push`** — local commits only so far. Push after you confirm the scan is
  clean *and* whether the repo should be public or private.
- **Daemon cutover** (§3) — the daemons still run from tsomp, untouched.
- **Exact task lineage** — see `proposed_patches/01_task_lineage.md` (edits live
  `scratch_inbox.py`; the dashboard reconstructs lineage best-effort meanwhile).
- **State-changing dashboard actions** ("manage the tasks") — v1 is read/monitor +
  guarded downloads only; any kill/relaunch/cancel button is proposed separately.
