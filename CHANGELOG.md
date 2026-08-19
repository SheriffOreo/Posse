# Changelog

All notable changes to Posse are recorded here, newest first. **Every push adds a
version note** — the version number, the date, and what changed, with any
**Action required** migration step called out. The current version is also in
[`VERSION`](VERSION), and each release is tagged in git (`v<version>`).

To update an existing install to a new version, follow
**[Updating Posse](README.md#updating-posse)**.

Versioning is [semantic](https://semver.org): `MAJOR.MINOR.PATCH` — MAJOR for a
breaking change, MINOR for a backward-compatible feature, PATCH for a fix.

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
