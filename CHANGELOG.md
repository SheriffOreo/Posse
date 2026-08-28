# Changelog

All notable changes to Posse are recorded here, newest first. **Every push adds a
version note** — the version number, the date, and what changed, with any
**Action required** migration step called out. The current version is also in
[`VERSION`](VERSION), and each release is tagged in git (`v<version>`).

To update an existing install to a new version, follow
**[Updating Posse](README.md#updating-posse)**.

Versioning is [semantic](https://semver.org): `MAJOR.MINOR.PATCH` — MAJOR for a
breaking change, MINOR for a backward-compatible feature, PATCH for a fix.

## [1.4.0] — 2026-08-28

### Added — Judges: independent review, in the release (Case 583)

v1.3.0 shipped a dashboard **Judges** tab reading a registry that nothing in the
release ever created — the UI was real, the backend was not. It is now real.

- **`infra/scratch_critic.py`** (new): the judge harness — registry, prompt
  composition, review rounds, verdict parsing, and the deputy-facing prompt blocks.
  A judge = a shared **charter** (rules, the SIGN-OFF/REVISE/REJECT vocabulary, the
  `verdict.json` + `verdict.md` output contract, and a mandatory page-inspection pass
  for any PDF deliverable) + a per-judge **persona** supplying taste and priority order.
- **Two judges ship**: `anonymous` (the default — is it true, is it readable) and
  `vyas` (papers/talks/decks — clarity of communication first). Their prompts are
  tracked files in **`infra/judges/`**, installed into the sheriff-owned records root by
  `python3 infra/scratch_critic.py seed`, which `setup.py` now runs. Seeding only ever
  ADDS a judge that is missing, so editing or retiring one survives every later upgrade.
- **Assign per case** via the create-case form's Judge selector or a `judge: <id>` body
  line / `[judge:<id>]` subject tag. An assigned case gets a JUDGE PROTOCOL section and
  **cannot close** until the judge signs off; an unknown id degrades to no judge rather
  than wedging the launch.
- **Sheriff-owned registry**: new `critic_add` / `critic_update` / `critic_remove`
  request ops. Nobody writes the roster directly — the sheriff decides each request and
  journals it. Registry writes are gated on **process** authorization (a token only the
  sheriff daemon holds), not on a role string any caller can simply claim.
- **Every deputy is told judges exist** — and told **not** to run one unless asked. An
  unrequested review costs a whole extra agent, so the default is no judge.

### Added — ChatGPT in the setup program (Case 583)

`setup.py` gained **step 5, "ChatGPT (Codex) — optional second service"**. v1.3.0 ported
the ChatGPT backend and documented it in the README, but the installer never mentioned
it, so a fresh operator could select ChatGPT on the create-case form with no `codex`
installed, no auth and nothing configured.

- Detects the `codex` binary using the **same resolution order** as
  `scratch_codex_auth.sh` — including the copy inside the VS Code ChatGPT extension,
  which never lands on `PATH` — and records `TSOMP_CODEX_BIN` when it is not on `PATH`.
- Offers both billing modes (`codex login` subscription, or `~/.openai_key` +
  `TSOMP_CODEX_AUTH=apikey`), writes the key `chmod 600`, and persists the choice to
  `infra_env.local.sh`.
- Entirely optional: decline it and the install is Claude-only, with no ChatGPT
  variables written.

### Fixed

- The spawn preamble advertised "a CRITIC that reviews your draft" as the archetypal
  anonymous helper — an open invitation for deputies to run reviews nobody asked for.
  Replaced with neutral examples plus an explicit "not for reviewing your own
  deliverables".

### Known gaps

Per-vendor limit detection, auth-expiry recovery, the HTML-only-mail fallback and
mid-case model switching remain out of the release (unchanged from 1.3.0). The mirror's
records manager still gates its *ledger/log* ops on the older role-string check; only
the judge registry uses the process-authorization gate added here.

## [1.3.0] — 2026-08-27

### Added — ChatGPT / Codex support (Case 557)

A case can now run on **Claude**, on **ChatGPT** (OpenAI's `codex` CLI), or in
**hybrid**, chosen per case. Claude remains the default; nothing changes for a
Claude-only install.

- **Three modes**, on the create-case form (`Service`) and as an e-mail tag
  (`service:` / `mode:`, subject `[service:claude+chatgpt]`):
  `claude` · `claude+chatgpt` · `chatgpt`. Naming a ChatGPT model alone
  (`model: luna`) implies the ChatGPT mode; an unrecognised value falls back to Claude.
- **Hybrid mode** (`claude+chatgpt`): Claude does the work, a ChatGPT writer owns the
  human-facing report, and the two iterate — the writer may not invent facts, so
  anything it cannot verify comes back as a query for the deputy to answer. Runs only
  for a PDF/LaTeX deliverable; falls back to the deputy's draft if the writer fails.
  New `infra/scratch_hybrid.py`.
- **Both auth modes**: `codex login` (ChatGPT subscription, default) or `~/.openai_key`
  with `TSOMP_CODEX_AUTH=apikey`. New `infra/scratch_codex_auth.sh`, loaded by
  `_daemon_env.sh` alongside the Claude switch; it resolves the `codex` binary (which is
  often not on `PATH`) and clears any inherited `OPENAI_API_KEY` in subscription mode.
- **Model registry** `infra/scratch_models.py` (new to the release): one file mapping
  each alias to its service, exact model id and label. Claude `fable`/`opus`/`sonnet`/
  `haiku`; ChatGPT `sol`/`terra`/`luna`/`gpt55` (GPT-5.6 Sol/Terra/Luna, GPT-5.5), taken
  from the live `codex debug models` catalog. `gpt-5.4*` is deliberately absent — it
  retires from Codex on 2026-08-31.
- **Dashboard**: Service selector with the three modes; the Model list is rebuilt to the
  chosen mode (options that don't apply are removed from the DOM, not merely hidden —
  WebKit ignores `hidden` on `<optgroup>`), and hybrid shows two model pickers.
- **Surgical kill** now recognises `codex` as an agent process. Previously it matched the
  literal string `claude`, so interrupting a ChatGPT deputy fell back to
  `tmux kill-session`, which also kills the deputy's CPU children.

**Action required:** none. ChatGPT is opt-in per case — see
[Using ChatGPT](README.md#using-chatgpt-optional) to enable it.

**Known gap:** the release `infra/` mirror still predates several live-system changes
(cases 466, 509, 520, 532, 551, 552), so the per-vendor **usage-limit detection** and the
**judge** subsystem are not in this release. ChatGPT case creation, launch, resume and
hybrid mode are complete and tested; limit handling for ChatGPT deputies is not. Tracked
for a follow-up mirror resync.

## [1.2.0] — 2026-08-19

_Action required: none — dashboard-only; pull + restart per "Updating Posse"._

Persists the accumulated dashboard improvements that had been running live but were not
yet committed to the repo (cases 440, 447, 471, 509, 512).

### Added
- Per-case **conversation** and **deliverables** views on each precinct's Case log,
  opening a centered modal (the same case-centric reconstruction as the History tab).
- A **model registry** (`dashboard/models.py`) surfaced across the Precincts table, the
  Sheriff block, and the precinct / create-case model selectors (exact model ids +
  display labels).

### Changed
- **History page**: lineage is direct-reply-only (no inferred trees), each case shows a
  one-sentence summary of its *request*, and the day view is grouped by precinct.
- **Status page**: the case description is the same one-sentence request summary.

### Fixed
- **Deliverables panel** no longer shows "(none found)" when a FINAL emailed a file
  outside the download roots — such files are now shown (with a downloadable flag), and
  the configured project dirs are download roots so cross-precinct files download.

### Security
- The download endpoint never serves VCS internals (`/.git/` added to the deny list) —
  a repo remote URL can embed a token, and this matters once project dirs are roots.

## [1.1.0] — 2026-08-19

_Action required: none — a normal update (pull + restart) is enough._

### Fixed
- **Fresh install: precinct-tagged emails got a "deputy spawned" ack but no deputy
  ever ran** ([issue #1]). A clean checkout has no `scratch_agents_registry.json`
  yet, and the worker-registration step of `scratch_spawn_worker.sh` read it with no
  guard under `set -euo pipefail` — so on the first spawn it crashed with
  `FileNotFoundError` *before* launching the deputy's tmux session, while the
  web-case bridge had already emailed the ack. The registration step now tolerates a
  missing / empty / corrupt registry and starts from `{"workers": {}}`, so the deputy
  always launches. (The collision-guard earlier in the same script and
  `scratch_inbox.py` already tolerated a missing registry; now the registration step
  does too.)

### Added
- `setup.py` seeds an empty `scratch_agents_registry.json` on a fresh install, so the
  router and dashboard see a consistent registry from first boot (the spawn script
  also creates it on demand as a safety net).
- **This changelog** and a top-level [`VERSION`](VERSION) file — every push now
  carries a version note.
- README **"Updating Posse"** section: a step-by-step guide to update an existing
  install to a new repo version.

## [1.0.0] — 2026-08-05

Initial public release.

- The Sheriff & Deputies system: precinct-scoped records (ledger / case log / case
  files), an always-on Sheriff (deputy supervision + records health), the inbox
  router + email control plane, the job manager with the cache-aware wait discipline,
  and surgical async interruption.
- A login-protected, read-only status/history web dashboard (pure stdlib, no deps).
- The `setup.py` interactive installer, `posse_start.sh` / `posse_stop.sh`, and the
  [`ONBOARDING.md`](ONBOARDING.md) new-operator guide.

[issue #1]: https://github.com/SheriffOreo/Posse/issues/1
[1.2.0]: https://github.com/SheriffOreo/Posse/releases/tag/v1.2.0
[1.1.0]: https://github.com/SheriffOreo/Posse/releases/tag/v1.1.0
[1.0.0]: https://github.com/SheriffOreo/Posse/tree/5ab4e20
