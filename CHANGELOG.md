# Changelog

All notable changes to Posse are recorded here, newest first. **Every push adds a
version note** — the version number, the date, and what changed, with any
**Action required** migration step called out. The current version is also in
[`VERSION`](VERSION), and each release is tagged in git (`v<version>`).

To update an existing install to a new version, follow
**[Update an existing install](README.md#update-an-existing-install)**.

Versioning is [semantic](https://semver.org): `MAJOR.MINOR.PATCH` — MAJOR for a
breaking change, MINOR for a backward-compatible feature, PATCH for a fix.

## [1.6.1] — 2026-09-19

### Added

- **A license.** Posse is now released under the Apache License 2.0; the full text is
  in [`LICENSE`](LICENSE). Until now the repository carried no license, which meant it
  granted no permission to use, modify or redistribute the code. It does now.

## [1.6.0] — 2026-09-19

### Added — the Field Guide: standing output standards every deputy launches with

A **Field Guide** tab holds the standards your deputies are held to. This release ships
two categories, **code** and **report**, with short starter text; each is injected into
every deputy's launch prompt, so a deputy knows the bar before it writes anything.

The text is **sheriff-owned** and the dashboard never edits it. Each section can ask the
sheriff for a review, and a new box asks the **receptionist** for a change — a new
section, or different wording in one that exists. Both open work; neither writes policy.
Deputies may file optional **lessons** from the CLI; they are shown on the tab, never
injected into a launch prompt, and a category is reviewed automatically at ten pending.

Sections open **folded**, so the tab is a list you can scan rather than a wall of text.

Growing it is a code change on purpose: extend `CATEGORIES` and `DEFAULT_GUIDELINES` in
`infra/scratch_field_guide.py` together.

### Added — the Docket: work that runs on a schedule

A **Docket** tab saves a spec plus a time: a single **case** in one precinct, or a whole
**JTF** (lead plus numbered collaborators, each with its own model and work split). When
an entry comes due, the runner drops a brand-new submission into the same queues the
Create-case and New-JTF forms write, so **fresh** deputies are spawned — nothing from a
previous run is reused. Repeats are once / hourly / daily / weekly / monthly, and a
prompt may carry date placeholders filled in at launch.

**Action required:** the docket needs its runner. `posse_start.sh` now starts a `docket`
daemon alongside the others — re-run it after updating. Without that daemon entries still
save and edit, but nothing ever fires.

### Changed

- Controls are offered only when the program that carries them out is installed. A
  button that writes an order nobody executes looks like it worked and silently does
  nothing, so the switch/relaunch/kill, reply, and add-collaborator controls now appear
  only when their backend is present.
- `posse_stop.sh` stops the new `docket` session with the rest.

### Removed

- The **Lineage** tab. The forest view was not useful. Reconstruction itself stays: it
  still builds History's day trees, a case's ancestors strip, and the agent search on the
  JTF and Docket forms.
- `MIGRATION.md` and `proposed_patches/` — notes on moving this code out of its original
  home, and a patch proposal for the removed Lineage tab that was applied in July 2026.
- One project's dataset names and download roots, which had leaked into the deputy-spec
  prompt and the download allow-list where no other operator could use them.

### Fixed

- The dashboard README claimed follow-up parent links were "not persisted" and the fix
  was "not applied". Both were wrong: `scratch_inbox_handle.sh` has stamped `parent_task:`
  since Task 323, so those edges are exact.
- A test reached into one developer's absolute path and, failing everywhere else, skipped
  two assertions in silence. It now reads this repo's own `infra/` and says when it skips.
- Removed a real person's email address from a tracked test fixture.

## [1.5.0] — 2026-08-28

### Added — the work split: one case, one context, several models (Case 583)

**1.4.0 shipped a create-case form offering a work split that this release's backend
silently dropped.** The form collected `report_model`/`report_service`; the bridge and
the launcher referenced them zero times. That is the same defect 1.4.0 itself fixed for
the Judges tab, reintroduced in the same commit. It is fixed properly here.

A case now belongs to ONE deputy with ONE context, and what changes over the case's life
is the **model generating it**. The unit of choice is a **lane**:

- **work** — thinking, planning, search, code, tests, records, and *all* correspondence
- **report** — a deliverable document a human reads (PDF/LaTeX, README, design doc, deck)

The report lane defaults to "same as work", so a case that ignores the split behaves
exactly as before. Set them differently and the deputy switches its **own** model when it
starts the document: it writes a request and exits, and the system manager confirms it is
really gone, carries the context across, and relaunches it on the other lane.

- **`infra/scratch_model_switch.py`** (new, + 149-test suite): the switch protocol, the
  lanes record, the cross-service hand-off seed, and the deputy-facing protocol text.
- Same-service switches are **lossless** (same session, one flag); cross-service ones are
  **reconstructed**, because the two CLIs keep transcripts in stores neither can read.
- **E-mail is not report writing**: the switch CLI refuses an email-shaped reason, so a
  case cannot switch models a dozen times to write five-line notes.
- `scratch_models.py` gained the lane vocabulary (`WORK_TYPES`, `lanes()`, `lane_mode()`)
  and a `lanes` CLI; the system manager gained switch classification and application; the
  bridge and launcher now carry the report lane and persist the lanes record.

### Fixed — a ChatGPT deputy could not survive a relaunch (Case 583)

The launcher was service-aware and started `codex` for a ChatGPT case, but it called the
relaunch generator **without the service**, and the generator had no notion of one. So a
ChatGPT deputy that was interrupted, crashed, or hit a limit came back as **`claude
--resume`** — a silent vendor switch mid-case. The generator now takes the service
(inferring it from the model when omitted), resumes codex with `codex exec resume`,
captures codex's self-minted session id from *this* run's log region, and handles the
cold-start path a cross-service switch requires.

Also fixed: a follow-up to a finished deputy resolved its relaunch script from the
non-durable manager roster alone, dropping most ended deputies onto the one-shot handler
that restores none of their case settings; the loop now falls back to the script on disk
and restores the case's own lane.

### Added — the Code judge

A third packaged judge, `code`, for reviewing source rather than prose: correctness first
(paths traced, callers followed, something actually run), then naming, readability, a
comment bar of concise-or-none, and human, non-slop style.

### Changed — README

The "Two vendors" feature bullet and the whole **Using ChatGPT** section described the
two-agent hybrid as *the* way to say "Claude works, ChatGPT writes". They now describe the
work split. The hybrid still ships and is documented as what it is: the older mechanism,
worth reaching for only when you specifically want an independent second reader.

### Known gaps

Per-vendor limit *detection* and auth-expiry recovery remain out of the release. The
create-case **receipt** (`submitted_form.txt` rendering) is not ported, and its five
upstream tests were removed rather than left failing. The records manager still gates
ledger/log ops on the older role-string check; only the judge registry uses process
authorization.

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
