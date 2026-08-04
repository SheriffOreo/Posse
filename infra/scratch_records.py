#!/usr/bin/env python3
"""Task 371 (Sheriff & Deputies, phase 1): the records-manager program.

The "Sheriff & Deputies" agent/task model (design report:
reports/task359/AGENT_TASK_MODEL.tex) gives each *department* (precinct) two
durable records:

  * a **ledger** -- the department's big-picture context (prior case summaries +
    pointers). MUTABLE: the sheriff compacts it (A->B) and applies approved edits;
    deputies only ever *append* a one-paragraph case report when a case closes.
  * a **case log** -- an append-only INDEX. A header line points at the
    department's case-file folder, then ONE LINE PER CLOSED CASE:
        <task#>\\t<case_file_path>\\t<one-sentence summary>
    NOBODY edits it -- not even the sheriff. Deputies only *append* (one line per
    finished case) and anyone may *read*. It is the permanent table-of-contents
    that lets a future deputy find past cases by description without opening each
    case file, and it is the recall backstop behind ledger compaction.

the operator's rule (2026-07-29): *all* access to these files goes through THIS
program, never directly to the file on disk, and it must be concurrency-safe:

  ledger : read (NON-BLOCKING) | append (LOCK) | write (LOCK, sheriff-only)
  log    : read (NON-BLOCKING) | append (LOCK)                 [no write, ever]

Concurrency / robustness design (the crux):

  * MUTATIONS are serialized by an EXCLUSIVE advisory lock (fcntl.flock LOCK_EX)
    on a per-target SIDECAR lock file (``<file>.lock``). The sidecar -- not the
    data file -- is locked so the lock inode stays stable across the atomic
    replace below.
  * BOTH append and write commit via an ATOMIC os.replace() of a same-directory
    temp file onto the target. A reader therefore always opens either the old or
    the new *complete* file -- never a partially written one. (append is a
    read-modify-write under the lock: read current -> concatenate -> atomic
    replace; the lock makes the RMW race-free, atomicity makes it crash-safe.)
  * READS take NO lock and never block on a writer; they just open-and-read the
    current committed file. A killed writer leaves only an orphan temp file, never
    a half-written ledger/log.

Usage (CLI mirrors the Python API 1:1):

    python scratch_records.py ledger read   --dept paper
    python scratch_records.py ledger append --dept paper --role deputy  --text "..."
    python scratch_records.py ledger write  --dept paper --role sheriff --file new.md
    python scratch_records.py ledger length --dept paper
    python scratch_records.py log read      --dept paper
    python scratch_records.py log append    --dept paper --role deputy \\
        --task 371 --summary "..." [--case-file scratch_full_logs/inbox/task_371.md]

Data root: ``scratch_full_logs/records/<dept>/`` with ``ledger.md`` (mutable) and
``log.tsv`` (append-only). Override the root with ``TSOMP_RECORDS_ROOT`` (used by
the test suite to stay off any real data).

Task 372 extensions (additive; phase-1 behaviour unchanged for any precinct that
is not registered fixed):

  * A precinct's ledger mode is ``mutable`` (default) or ``fixed``. A FIXED ledger
    (the receptionist) refuses ``ledger append``/``ledger write``; it is set once
    with ``ledger author`` and is read-only through the API thereafter:

        python scratch_records.py ledger author --dept receptionist --file recept.md
        python scratch_records.py directory register --name receptionist --mode fixed \\
            --description "front desk: routing + system description"

  * A GLOBAL precinct directory (``precincts.json`` machine + ``PRECINCTS.md``
    human, both at the records root) lists every precinct so everyone knows every
    one; register/update a precinct through it (creates its dir + ``cases/``):

        python scratch_records.py directory read           # precincts.json (JSON)
        python scratch_records.py directory read --md      # PRECINCTS.md (human)
        python scratch_records.py directory register --name eval --description "..."
        python scratch_records.py directory mode --dept receptionist   # -> fixed

This was phase 1 (standalone, no live daemon). Task 372 wires it into the live
system (sheriff daemon + deputy hooks + receptionist routing).
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS_ROOT = REPO_ROOT / "scratch_full_logs" / "records"

# Default case-file folder recorded in a fresh log header (today's inbox layout;
# a log's per-line path can point straight at scratch_full_logs/inbox/task_<N>.md).
DEFAULT_CASE_FOLDER = "scratch_full_logs/inbox"

LEDGER_NAME = "ledger.md"
LOG_NAME = "log.tsv"
CASES_DIRNAME = "cases"          # per-precinct case-file folder (Task 372)

# The GLOBAL precinct directory (Task 372): a single machine file + human render
# at the records ROOT (not under any one precinct) so "everyone knows about
# everyone". precincts.json is the source of truth; PRECINCTS.md is rendered from it.
DIRECTORY_JSON = "precincts.json"
DIRECTORY_MD = "PRECINCTS.md"

# A precinct's ledger is either MUTABLE (the sheriff compacts it, deputies append)
# or FIXED (authored once, then read-only through the API -- the receptionist).
LEDGER_MODE_MUTABLE = "mutable"
LEDGER_MODE_FIXED = "fixed"
_LEDGER_MODES = (LEDGER_MODE_MUTABLE, LEDGER_MODE_FIXED)

# A precinct's lifecycle STATUS (Phase D, Case 384d). An entry with no ``status``
# field is ACTIVE (backward-compatible default). A SHERIFF soft-delete moves the
# precinct's records into ``.trash`` and flips the entry to ``deleted`` (recoverable
# for 14 days), which ``directory_read`` filters out of the active list by default.
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"
# Soft-deleted precinct records are quarantined here (a hidden dir at the records
# ROOT; ``_safe_dept`` bars a leading '.', so no precinct can ever be named
# ``.trash`` and collide). Kept for TRASH_RETENTION_DAYS, then hard-purged by the
# sheriff's mechanical purge pass.
TRASH_DIRNAME = ".trash"
TRASH_RETENTION_DAYS = 14

# A precinct's DEFAULT MODEL (Task 376): the model a deputy spawned for this
# precinct runs on UNLESS overridden (an explicit WORKER_MODEL env, or a
# 'model:' tag in the routing email). Stored per-precinct in the directory;
# an unset/unknown value falls back to DEFAULT_MODEL. the operator's policy: paper
# stays fable 5; omp/query/eval/infra use opus 4.8 (deputies are ephemeral with
# bounded context, which fits opus -- the old fable pin was for the monolithic
# standing sessions).
_MODELS = ("fable", "opus", "sonnet", "haiku")
DEFAULT_MODEL = "opus"

# The GLOBAL SHERIFF MODEL (Task 384b / Phase C): ONE model for the WHOLE system
# (NOT per-precinct -- Feng was explicit). It is the model the sheriff daemon runs
# its two kinds of API call on -- ledger COMPACTION and change-request DECISIONS.
# Persisted at the records ROOT in ``sheriff_config.json`` ({"model": "fable"}),
# read by the sheriff daemon and shown/settable on the dashboard. Default is fable
# (cheap, large context -- the sheriff's calls are bounded, reflective rewrites).
DEFAULT_SHERIFF_MODEL = "fable"
SHERIFF_CONFIG_JSON = "sheriff_config.json"

_DEPT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")  # leading '.' (./..) barred
_CHARS_PER_TOKEN = 4  # rough heuristic for the approximate-token count


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _records_root() -> Path:
    """Records root; ``TSOMP_RECORDS_ROOT`` overrides (read live so tests can set it)."""
    env = os.environ.get("TSOMP_RECORDS_ROOT")
    return Path(env) if env else DEFAULT_RECORDS_ROOT


def _safe_dept(dept: str) -> str:
    """Validate a department name so it can never escape the records root."""
    if not isinstance(dept, str) or not _DEPT_RE.match(dept) or dept in (".", ".."):
        raise ValueError(
            f"invalid department name {dept!r} "
            "(allowed: letters, digits, '_', '.', '-'; no path separators)"
        )
    return dept


def _dept_dir(dept: str, *, create: bool = False) -> Path:
    d = _records_root() / _safe_dept(dept)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path(dept: str, *, create: bool = False) -> Path:
    return _dept_dir(dept, create=create) / LEDGER_NAME


def _log_path(dept: str, *, create: bool = False) -> Path:
    return _dept_dir(dept, create=create) / LOG_NAME


def _cases_dir(dept: str, *, create: bool = False) -> Path:
    d = _dept_dir(dept, create=create) / CASES_DIRNAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _trash_root(*, create: bool = False) -> Path:
    """The soft-delete quarantine dir at the records root (Phase D). A deleted
    precinct's whole records dir is moved under here as ``<name>@<ISO>``."""
    d = _records_root() / TRASH_DIRNAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _directory_json_path() -> Path:
    return _records_root() / DIRECTORY_JSON


def _directory_md_path() -> Path:
    return _records_root() / DIRECTORY_MD


def _sheriff_config_path() -> Path:
    return _records_root() / SHERIFF_CONFIG_JSON


# ---------------------------------------------------------------------------
# locking + atomic write primitives
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _exclusive_lock(target: Path):
    """Block until we hold an exclusive advisory lock for ``target``.

    Locks a persistent sidecar ``<target>.lock`` (never unlinked) so every
    process flocks the same stable inode, and so the atomic replace of the data
    file below cannot swap the lock inode out from under a holder.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocks other appenders/writers, not readers
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(target: Path, content: str) -> None:
    """Atomically (re)place ``target`` with ``content`` via a same-dir temp + replace.

    A concurrent reader sees either the old complete file or the new complete
    file, never a partial one. A crash leaves at most an orphan temp file.
    Call only while holding the target's exclusive lock.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_records_", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))  # atomic on POSIX (same filesystem)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    # Best-effort: persist the rename itself so a crash can't lose it.
    with contextlib.suppress(OSError):
        dfd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _read_text(path: Path) -> str:
    """Lock-free read of a committed file; '' if it does not exist yet."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _sanitize_field(value: str) -> str:
    """Collapse tabs/newlines to spaces so one record stays exactly one TSV line."""
    return re.sub(r"\s+", " ", str(value).replace("\t", " ")).strip()


# ---------------------------------------------------------------------------
# LEDGER  --  read (non-blocking) | append (lock) | write (lock, sheriff-only)
# ---------------------------------------------------------------------------
def ledger_read(dept: str) -> str:
    """Return the department ledger. NON-BLOCKING; '' if none yet."""
    return _read_text(_ledger_path(dept))


def ledger_append(dept: str, text: str, role: str = "deputy") -> None:
    """Append a one-paragraph case report to the ledger. LOCK-PROTECTED.

    Any role may append (deputies close cases this way). Existing content is
    never rewritten -- append is the only write a deputy performs on the ledger.
    Refused for a FIXED-ledger precinct (the receptionist): its ledger is
    authored once via ``ledger_author`` and read-only thereafter.
    """
    if precinct_mode(dept) == LEDGER_MODE_FIXED:
        raise PermissionError(
            f"ledger_append refused: precinct {dept!r} has a FIXED ledger "
            "(authored once via ledger_author; read-only through the API)"
        )
    entry = text.rstrip("\n")
    target = _ledger_path(dept, create=True)
    with _exclusive_lock(target):
        current = _read_text(target)
        if current.strip():
            # blank-line-separate case reports so each is a distinct paragraph
            # (this is what lets the sheriff compact by paragraph); readers that
            # filter blank lines are unaffected.
            current = current.rstrip("\n") + "\n\n"
        else:
            current = ""
        _atomic_write(target, current + entry + "\n")


def ledger_write(dept: str, new_content: str, role: str) -> None:
    """Replace the ledger wholesale (compaction / approved edit). LOCK-PROTECTED.

    SHERIFF-ONLY: this is the ONLY way existing ledger content ever changes.
    Raises PermissionError for any other role.
    """
    if role != "sheriff":
        raise PermissionError(
            f"ledger_write is sheriff-only; role={role!r} refused "
            "(deputies may only ledger_append)"
        )
    if precinct_mode(dept) == LEDGER_MODE_FIXED:
        raise PermissionError(
            f"ledger_write refused: precinct {dept!r} has a FIXED ledger "
            "(the sheriff never edits it; authored once via ledger_author)"
        )
    target = _ledger_path(dept, create=True)
    with _exclusive_lock(target):
        _atomic_write(target, new_content)


def ledger_author(dept: str, content: str, *, force: bool = False) -> None:
    """Author a ledger's content once (write-once bootstrap). LOCK-PROTECTED.

    This is the sanctioned way to set a FIXED-ledger precinct's content (the
    receptionist's fixed description of its job + the system): ``ledger_append``
    and ``ledger_write`` both refuse a fixed ledger, so this is its *only* writer,
    and it writes only ONCE -- it refuses if the ledger already has content unless
    ``force=True``. It works for a mutable precinct too (a convenient one-time
    seed), but there the sheriff's ``ledger_write`` remains the normal editor.
    """
    target = _ledger_path(dept, create=True)
    with _exclusive_lock(target):
        current = _read_text(target)
        if current.strip() and not force:
            raise PermissionError(
                f"ledger_author refused: precinct {dept!r} ledger already authored "
                f"({len(current)} chars); pass force=True to overwrite"
            )
        _atomic_write(target, content)


def ledger_length(dept: str) -> dict:
    """Return {'chars', 'approx_tokens'} for the ledger (read-only helper)."""
    chars = len(ledger_read(dept))
    return {"chars": chars, "approx_tokens": math.ceil(chars / _CHARS_PER_TOKEN)}


# ---------------------------------------------------------------------------
# LOG  --  read (non-blocking) | append (lock).  There is deliberately NO write.
# ---------------------------------------------------------------------------
def _log_header(dept: str, case_folder: str) -> str:
    folder = _sanitize_field(case_folder) or DEFAULT_CASE_FOLDER
    return (
        f"# case_log dept={dept}\n"
        f"# case_file_folder={folder}\n"
        # Task 377 #1: the deputy (who did the work) is the 2nd field, right after
        # the task number. Old 3-field rows (<task>\t<path>\t<summary>) still parse
        # and render with an empty deputy; new rows carry all four.
        "# format: <task>\\t<deputy>\\t<case_file_path>\\t<one_sentence_summary>\n"
    )


def log_read(dept: str) -> str:
    """Return the department case log (header + one line per closed case). NON-BLOCKING."""
    return _read_text(_log_path(dept))


def log_append(
    dept: str,
    task,
    summary: str,
    case_file_path: str = None,
    role: str = "deputy",
    case_folder: str = DEFAULT_CASE_FOLDER,
    deputy: str = None,
) -> None:
    """Append ONE index line for a closed case. LOCK-PROTECTED (append-only, no write).

    Line format (Task 377 #1): when a ``deputy`` is given the row is FOUR fields
    ``<task>\\t<deputy>\\t<case_file_path>\\t<one-sentence summary>`` -- the working
    deputy sits right after the task number. When ``deputy`` is omitted the row is
    the ORIGINAL three fields ``<task>\\t<case_file_path>\\t<summary>`` (byte-identical
    to pre-377), so deputy-less appends stay fully backward-compatible. Readers
    disambiguate purely by field count. The header (with the case-file folder path)
    is written once, when the log is created. Any role may append; the log is
    immutable except for append.
    """
    task_s = _sanitize_field(task)
    summ_s = _sanitize_field(summary)
    dep_s = _sanitize_field(deputy) if deputy else ""
    if case_file_path is None:
        folder = (case_folder or DEFAULT_CASE_FOLDER).rstrip("/")
        case_file_path = f"{folder}/task_{task_s}.md"
    path_s = _sanitize_field(case_file_path)
    # 4-field row iff a deputy is present; else the original 3-field row (backward
    # compatible — the pre-377 append path is byte-for-byte unchanged).
    line = (f"{task_s}\t{dep_s}\t{path_s}\t{summ_s}\n" if dep_s
            else f"{task_s}\t{path_s}\t{summ_s}\n")

    target = _log_path(dept, create=True)
    with _exclusive_lock(target):
        current = _read_text(target)
        if not current:
            current = _log_header(dept, case_folder)
        elif not current.endswith("\n"):
            current += "\n"
        _atomic_write(target, current + line)


def log_remove(dept: str, task, role: str) -> int:
    """Remove ALL data lines for ``task`` from the case log. LOCK-PROTECTED.

    The case log is otherwise strictly APPEND-ONLY; this is the single sanctioned
    exception, granted to the SHERIFF only (Task 377 / Feng uid=380) so it can
    retract a test/erroneous entry (e.g. a self-test case) without hand-editing the
    file. Non-sheriff callers are refused (PermissionError), exactly like
    ``ledger_write``. Header/comment lines and every non-matching data line are
    preserved verbatim; only lines whose FIRST tab-field equals ``task`` are dropped.
    Returns the number of lines removed. Atomic replace (a reader never sees a torn
    file). This keeps deputy attribution honest: deputies can only append, and the
    sole editor of the index is the system manager."""
    if role != "sheriff":
        raise PermissionError(
            f"log_remove is sheriff-only; role={role!r} refused "
            "(the case log is append-only for everyone else)"
        )
    task_s = _sanitize_field(task)
    target = _log_path(dept)
    with _exclusive_lock(target):
        current = _read_text(target)
        if not current:
            return 0
        kept, removed = [], 0
        for ln in current.split("\n"):
            if ln.startswith("#") or not ln.strip():
                kept.append(ln)
                continue
            if ln.split("\t", 1)[0] == task_s:
                removed += 1
            else:
                kept.append(ln)
        if removed:
            new = "\n".join(kept)
            if new and not new.endswith("\n"):
                new += "\n"
            _atomic_write(target, new)
        return removed


# ---------------------------------------------------------------------------
# GLOBAL PRECINCT DIRECTORY  --  one artifact everyone is seeded with, so
# "everyone knows about everyone" (Task 372). precincts.json is the machine
# source of truth; PRECINCTS.md is rendered from it. Both live at the records
# ROOT. read = non-blocking; register/update = LOCK-protected + atomic.
# ---------------------------------------------------------------------------
def _directory_read_raw() -> dict:
    """The FULL directory on disk, INCLUDING soft-deleted precincts (status ==
    ``deleted``). NON-BLOCKING. This is the read every MUTATOR must use for its
    read-modify-write so a re-register / delete / restore never silently drops a
    soft-deleted entry (whose trash + purge_after metadata must survive). Callers
    that want the ACTIVE view use ``directory_read`` (which filters below)."""
    txt = _read_text(_directory_json_path())
    if not txt.strip():
        return {"precincts": {}}
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return {"precincts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("precincts"), dict):
        return {"precincts": {}}
    return data


def directory_read(include_deleted: bool = False) -> dict:
    """Return the global precinct directory as a dict. NON-BLOCKING.

    Shape: ``{"precincts": {<name>: {description, ledger_mode, ledger, log,
    cases}}}``. Returns an empty directory if none exists yet or is unparseable
    (so callers -- and every phase-1 test that never registers a precinct -- see
    a well-formed empty directory, and unregistered depts default to mutable).

    Phase D (Case 384d): a SOFT-DELETED precinct carries ``status: "deleted"`` in
    its entry (its records live in ``.trash`` for 14 days). By DEFAULT such entries
    are FILTERED OUT here, so the ACTIVE precinct list -- and every consumer
    (``mutable_precincts``, the dashboard, the receptionist directory) -- drops a
    deleted precinct automatically. Pass ``include_deleted=True`` to get the full
    on-disk directory (used by restore / purge / the raw mutators). This default is
    backward-compatible: no existing entry carries a ``status`` field, so nothing is
    filtered until a precinct is actually soft-deleted.
    """
    data = _directory_read_raw()
    if include_deleted:
        return data
    precincts = {n: e for n, e in data.get("precincts", {}).items()
                 if not (isinstance(e, dict) and e.get("status") == STATUS_DELETED)}
    return {**data, "precincts": precincts}


def precinct_mode(dept: str) -> str:
    """Return the ledger mode of ``dept``: 'fixed' iff registered fixed, else
    'mutable'. An UNREGISTERED precinct is 'mutable' -- so all phase-1 behaviour
    (and its tests) is unchanged, and only an explicitly fixed precinct refuses."""
    _safe_dept(dept)
    entry = directory_read().get("precincts", {}).get(dept)
    if isinstance(entry, dict) and entry.get("ledger_mode") == LEDGER_MODE_FIXED:
        return LEDGER_MODE_FIXED
    return LEDGER_MODE_MUTABLE


def precinct_model(dept: str) -> str:
    """Return ``dept``'s registered DEFAULT MODEL (Task 376), or ``DEFAULT_MODEL``
    (opus) when the precinct is unregistered or carries no/unknown model. A deputy
    spawned for the precinct runs on this unless a higher-priority override (an
    explicit WORKER_MODEL env or an email 'model:' tag) supplies one."""
    _safe_dept(dept)
    entry = directory_read().get("precincts", {}).get(dept)
    if isinstance(entry, dict):
        m = entry.get("model")
        if m in _MODELS:
            return m
    return DEFAULT_MODEL


def _render_precincts_md(data: dict) -> str:
    """Render the human-readable global directory from the machine directory.
    Phase D: soft-deleted precincts (``status == deleted``) are omitted -- the
    human directory shows only the ACTIVE precincts, matching ``directory_read``."""
    precincts = {n: e for n, e in data.get("precincts", {}).items()
                 if not (isinstance(e, dict) and e.get("status") == STATUS_DELETED)}
    out = [
        "# Precinct directory (global)",
        "",
        "The Sheriff & Deputies system organizes work into **precincts**. Every",
        "deputy and the sheriff are seeded with this directory so everyone knows",
        "every precinct. Source of truth: `precincts.json` (write via",
        "`scratch_records.py directory register`; never hand-edit).",
        "",
        f"Precincts: **{len(precincts)}**",
        "",
        "| Precinct | Ledger mode | Model | Description | Ledger | Case log | Cases |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(precincts):
        e = precincts[name] if isinstance(precincts[name], dict) else {}
        out.append(
            f"| `{name}` | {e.get('ledger_mode', LEDGER_MODE_MUTABLE)} "
            f"| {e.get('model', DEFAULT_MODEL)} "
            f"| {e.get('description', '')} "
            f"| `{e.get('ledger', name + '/' + LEDGER_NAME)}` "
            f"| `{e.get('log', name + '/' + LOG_NAME)}` "
            f"| `{e.get('cases', name + '/' + CASES_DIRNAME)}` |"
        )
    out.append("")
    return "\n".join(out)


def directory_register(
    name: str,
    description: str = "",
    ledger_mode: str = LEDGER_MODE_MUTABLE,
    case_folder: str = None,
    model: str = None,
    role: str = "sheriff",
) -> dict:
    """Register (or update) a precinct in the global directory. LOCK-PROTECTED.

    Creates the precinct's records dir + ``cases/`` folder, writes/updates its
    entry in precincts.json, and re-renders PRECINCTS.md -- all under the
    directory's exclusive lock, each file committed by atomic replace. Idempotent:
    re-registering an existing precinct updates its description/mode/model/paths
    but never disturbs its ledger, log, or case files. Used by the migration and
    by the receptionist when it creates a NEW precinct. Returns the entry.

    ``model`` (Task 376) is the precinct's DEFAULT MODEL for spawned deputies; it
    is validated against ``_MODELS`` and only overwrites the stored value when
    given, so passing ``model=None`` leaves any existing default untouched.

    Phase D: re-registering a SOFT-DELETED precinct is refused (ValueError) -- its
    records live in ``.trash``, so silently recreating an empty active dir would
    orphan them; the caller must ``precinct_restore`` (recover) or ``precinct_purge``
    (finalize) first.
    """
    name = _safe_dept(name)
    if ledger_mode not in _LEDGER_MODES:
        raise ValueError(f"invalid ledger_mode {ledger_mode!r} (allowed: {_LEDGER_MODES})")
    if model is not None and model not in _MODELS:
        raise ValueError(f"invalid model {model!r} (allowed: {_MODELS})")
    jpath = _directory_json_path()
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(jpath):
        data = _directory_read_raw()          # raw: never drop a soft-deleted entry
        precincts = data.setdefault("precincts", {})
        existing = precincts.get(name) if isinstance(precincts.get(name), dict) else {}
        if existing.get("status") == STATUS_DELETED:
            raise ValueError(
                f"precinct {name!r} is soft-deleted (in .trash); restore or purge it "
                "before re-registering (refusing to orphan its trashed records)")
        # create the records dir + cases/ only for a genuinely active precinct
        _dept_dir(name, create=True)
        _cases_dir(name, create=True)
        entry = dict(existing)
        desc = _sanitize_field(description)
        entry.update({
            "description": desc or entry.get("description", ""),
            "ledger_mode": ledger_mode,
            "ledger": f"{name}/{LEDGER_NAME}",
            "log": f"{name}/{LOG_NAME}",
            "cases": _sanitize_field(case_folder) if case_folder else f"{name}/{CASES_DIRNAME}",
        })
        if model is not None:            # only set when given; else keep any existing default
            entry["model"] = model
        precincts[name] = entry
        _atomic_write(jpath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(_directory_md_path(), _render_precincts_md(data))
    return entry


# ---------------------------------------------------------------------------
# PRECINCT LIFECYCLE (Phase D, Case 384d) -- soft-delete / restore / purge, all
# SHERIFF-ONLY (like ledger_write / log_remove). DELETE IS RECOVERABLE: it never
# hard-removes anything except the explicit >retention purge. The destructive
# perform is guarded UPSTREAM by the sheriff (active-deputy interlock + an emailed
# YES confirmation); these functions are the mechanical records primitives.
# ---------------------------------------------------------------------------
def precinct_status(name: str) -> str:
    """Return ``active`` | ``deleted`` for a registered precinct, or ``None`` if it
    is not in the directory at all. Reads the RAW directory (sees deleted entries)."""
    _safe_dept(name)
    e = _directory_read_raw().get("precincts", {}).get(name)
    if not isinstance(e, dict):
        return None
    return STATUS_DELETED if e.get("status") == STATUS_DELETED else STATUS_ACTIVE


def precinct_soft_delete(name: str, role: str = "sheriff", *, ts: float = None,
                         retention_days: int = TRASH_RETENTION_DAYS) -> dict:
    """SOFT-DELETE a precinct (SHERIFF-ONLY). Move its whole records dir to
    ``.trash/<name>@<ISO>/`` and flip its directory entry to
    ``status=deleted`` with a ``purge_after`` of now + ``retention_days``. The
    records stay ON DISK (recoverable via ``precinct_restore``); NOTHING is hard-
    deleted here. Idempotent: soft-deleting an already-deleted precinct is a no-op
    that returns the existing entry. Returns the updated entry.

    Raises PermissionError for a non-sheriff role, ValueError for an unknown
    precinct or the receptionist (the front desk is never deletable)."""
    if role != "sheriff":
        raise PermissionError(
            f"precinct_soft_delete is sheriff-only; role={role!r} refused")
    name = _safe_dept(name)
    ts = time.time() if ts is None else ts
    jpath = _directory_json_path()
    with _exclusive_lock(jpath):
        data = _directory_read_raw()
        precincts = data.get("precincts", {})
        entry = precincts.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"unknown precinct {name!r} (not in the directory)")
        if entry.get("ledger_mode") == LEDGER_MODE_FIXED:
            raise ValueError(f"precinct {name!r} has a FIXED ledger (the receptionist "
                             "front desk is never deletable)")
        if entry.get("status") == STATUS_DELETED:
            return entry                       # already soft-deleted: no-op
        iso = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))
        trash_rel = f"{TRASH_DIRNAME}/{name}@{iso}"
        trash_dir = _records_root() / trash_rel
        src = _dept_dir(name)
        _trash_root(create=True)
        if src.exists():
            if trash_dir.exists():             # unique iso should prevent this
                raise ValueError(f"trash target already exists: {trash_dir}")
            os.replace(str(src), str(trash_dir))
        entry.update({
            "status": STATUS_DELETED,
            "deleted_ts": ts,
            "deleted_iso": iso,
            "purge_after": ts + retention_days * 86400,
            "trash": trash_rel,
        })
        precincts[name] = entry
        _atomic_write(jpath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(_directory_md_path(), _render_precincts_md(data))
    return entry


def precinct_restore(name: str, role: str = "sheriff") -> dict:
    """RESTORE a soft-deleted precinct (SHERIFF-ONLY) within its retention window:
    move its records back out of ``.trash`` and clear the deleted status so it
    re-appears in the active directory. Returns the restored entry.

    Raises PermissionError for a non-sheriff role; ValueError if the precinct is
    unknown, not deleted, its trash is missing, or a live dir already occupies its
    slot (never clobbers existing records)."""
    if role != "sheriff":
        raise PermissionError(
            f"precinct_restore is sheriff-only; role={role!r} refused")
    name = _safe_dept(name)
    jpath = _directory_json_path()
    with _exclusive_lock(jpath):
        data = _directory_read_raw()
        precincts = data.get("precincts", {})
        entry = precincts.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"unknown precinct {name!r} (not in the directory)")
        if entry.get("status") != STATUS_DELETED:
            raise ValueError(f"precinct {name!r} is not deleted; nothing to restore")
        trash_rel = entry.get("trash") or f"{TRASH_DIRNAME}/{name}@{entry.get('deleted_iso', '')}"
        trash_dir = _records_root() / trash_rel
        dst = _dept_dir(name)
        if trash_dir.exists():
            if dst.exists():
                raise ValueError(f"cannot restore {name!r}: a live records dir already "
                                 f"exists at {dst} (refusing to clobber)")
            os.replace(str(trash_dir), str(dst))
        for k in ("status", "deleted_ts", "deleted_iso", "purge_after", "trash"):
            entry.pop(k, None)
        precincts[name] = entry
        _atomic_write(jpath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(_directory_md_path(), _render_precincts_md(data))
    return entry


def precincts_pending_purge(now: float = None) -> list:
    """RAW scan for soft-deleted precincts whose ``purge_after`` has passed. Returns
    a list of ``(name, entry)`` the sheriff's purge pass should hard-delete. NON-
    BLOCKING (a read-only scan)."""
    now = time.time() if now is None else now
    out = []
    for name, e in _directory_read_raw().get("precincts", {}).items():
        if (isinstance(e, dict) and e.get("status") == STATUS_DELETED
                and isinstance(e.get("purge_after"), (int, float))
                and e["purge_after"] <= now):
            out.append((name, e))
    return out


def precinct_purge(name: str, role: str = "sheriff") -> dict:
    """HARD-DELETE a soft-deleted precinct's trash + drop its directory entry
    (SHERIFF-ONLY). This is the ONLY hard delete in the records manager; it is
    meant for the sheriff's mechanical >retention purge (a precinct whose
    ``purge_after`` has passed). Raises PermissionError for a non-sheriff role,
    ValueError if the precinct is unknown or not soft-deleted (purge is never a
    shortcut around soft-delete)."""
    if role != "sheriff":
        raise PermissionError(
            f"precinct_purge is sheriff-only; role={role!r} refused")
    name = _safe_dept(name)
    jpath = _directory_json_path()
    with _exclusive_lock(jpath):
        data = _directory_read_raw()
        precincts = data.get("precincts", {})
        entry = precincts.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"unknown precinct {name!r} (not in the directory)")
        if entry.get("status") != STATUS_DELETED:
            raise ValueError(f"precinct {name!r} is not soft-deleted; refusing to "
                             "hard-purge (soft-delete first)")
        trash_rel = entry.get("trash") or f"{TRASH_DIRNAME}/{name}@{entry.get('deleted_iso', '')}"
        trash_dir = _records_root() / trash_rel
        if trash_dir.exists():
            shutil.rmtree(str(trash_dir), ignore_errors=True)
        del precincts[name]
        _atomic_write(jpath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(_directory_md_path(), _render_precincts_md(data))
    return {"purged": name, "trash": trash_rel}


# ---------------------------------------------------------------------------
# GLOBAL SHERIFF CONFIG (Task 384b / Phase C) -- ONE system-wide config object for
# the sheriff daemon, living alongside the precinct directory at the records ROOT.
# Today it holds exactly the global SHERIFF MODEL (the model the sheriff runs its
# ledger-compaction and change-request-decision API calls on -- one value for the
# whole system, not per-precinct). read = non-blocking; set = LOCK-protected +
# atomic and, like ``ledger_write`` / ``log_remove``, SHERIFF-ONLY (the sheriff owns
# system config; a deputy cannot self-authorize a change).
# ---------------------------------------------------------------------------
def sheriff_config_read() -> dict:
    """Return the global sheriff config as a dict. NON-BLOCKING. Returns an empty
    dict if none exists yet or it is unparseable, so callers always see a
    well-formed map (and an unset config falls back to the defaults below)."""
    txt = _read_text(_sheriff_config_path())
    if not txt.strip():
        return {}
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def sheriff_model_get() -> str:
    """Return the persisted GLOBAL sheriff model, or ``DEFAULT_SHERIFF_MODEL``
    (fable) when the config is unset or carries no/unknown model. This is the
    system-wide model the sheriff uses for BOTH its ledger-compaction and
    change-request-decision API calls."""
    m = sheriff_config_read().get("model")
    return m if m in _MODELS else DEFAULT_SHERIFF_MODEL


def sheriff_model_set(model: str, role: str = "sheriff") -> dict:
    """Set the persisted GLOBAL sheriff model. LOCK-PROTECTED + atomic. SHERIFF-ONLY
    (like ``ledger_write`` / ``log_remove``): the sheriff owns system config, so any
    other role is refused. ``model`` is validated against ``_MODELS``. Returns the
    new config dict."""
    if role != "sheriff":
        raise PermissionError(
            f"sheriff_model_set is sheriff-only; role={role!r} refused "
            "(the global sheriff model is system config only the sheriff sets)"
        )
    if model not in _MODELS:
        raise ValueError(f"invalid model {model!r} (allowed: {_MODELS})")
    jpath = _sheriff_config_path()
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(jpath):
        data = sheriff_config_read()
        data["model"] = model
        _atomic_write(jpath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _read_input(text: str, file: str) -> str:
    """Resolve --text / --file / '-' (stdin) into a content string for a write."""
    if file is not None:
        if file == "-":
            return sys.stdin.read()
        return Path(file).read_text(encoding="utf-8")
    if text is not None:
        return text
    return sys.stdin.read()


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="scratch_records.py",
        description="Concurrency-safe records manager for department ledger + case log.",
    )
    rec = ap.add_subparsers(dest="record", required=True)

    # ---- ledger ----
    led = rec.add_parser("ledger", help="the mutable, sheriff-writable department ledger")
    ledop = led.add_subparsers(dest="op", required=True)

    p = ledop.add_parser("read", help="print the ledger (non-blocking)")
    p.add_argument("--dept", required=True)

    p = ledop.add_parser("append", help="append a case paragraph (lock; any role)")
    p.add_argument("--dept", required=True)
    p.add_argument("--role", default="deputy")
    p.add_argument("--text", default=None, help="paragraph text (else --file / stdin)")
    p.add_argument("--file", default=None, help="read paragraph from file ('-' = stdin)")

    p = ledop.add_parser("write", help="replace the ledger (lock; SHERIFF ONLY)")
    p.add_argument("--dept", required=True)
    p.add_argument("--role", required=True, help="must be 'sheriff'")
    p.add_argument("--text", default=None, help="new content (else --file / stdin)")
    p.add_argument("--file", default=None, help="read new content from file ('-' = stdin)")

    p = ledop.add_parser("author",
                         help="author a ledger ONCE (lock; the only writer a FIXED ledger accepts)")
    p.add_argument("--dept", required=True)
    p.add_argument("--text", default=None, help="content (else --file / stdin)")
    p.add_argument("--file", default=None, help="read content from file ('-' = stdin)")
    p.add_argument("--force", action="store_true", help="overwrite an already-authored ledger")

    p = ledop.add_parser("length", help="print {chars, approx_tokens} as JSON")
    p.add_argument("--dept", required=True)

    # ---- log ----
    log = rec.add_parser("log", help="the append-only case-log index (no write, ever)")
    logop = log.add_subparsers(dest="op", required=True)

    p = logop.add_parser("read", help="print the case log (non-blocking)")
    p.add_argument("--dept", required=True)

    p = logop.add_parser("remove",
                         help="remove all lines for a task (lock; SHERIFF ONLY — the one append-only exception)")
    p.add_argument("--dept", required=True)
    p.add_argument("--task", required=True, help="task/case number to retract")
    p.add_argument("--role", required=True, help="must be 'sheriff'")

    p = logop.add_parser("append", help="append one closed-case index line (lock)")
    p.add_argument("--dept", required=True)
    p.add_argument("--role", default="deputy")
    p.add_argument("--task", required=True, help="task number")
    p.add_argument("--summary", required=True, help="one-sentence case summary")
    p.add_argument("--deputy", default=None,
                   help="Task 377: the working deputy (2nd field, after task)")
    p.add_argument("--case-file", dest="case_file", default=None,
                   help="path to the case file (default: <folder>/task_<task>.md)")
    p.add_argument("--case-folder", dest="case_folder", default=DEFAULT_CASE_FOLDER,
                   help="case-file folder recorded in the header on first append")

    # ---- directory (global precinct directory) ----
    dr = rec.add_parser("directory", help="the global precinct directory (PRECINCTS.md + precincts.json)")
    drop = dr.add_subparsers(dest="op", required=True)

    p = drop.add_parser("read", help="print the machine directory (precincts.json) as JSON")
    p.add_argument("--md", action="store_true", help="print the human render (PRECINCTS.md) instead")
    p.add_argument("--include-deleted", dest="include_deleted", action="store_true",
                   help="include soft-deleted precincts (Phase D; default hides them)")

    p = drop.add_parser("register", help="register/update a precinct (lock; creates dir + cases/)")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--mode", default=LEDGER_MODE_MUTABLE, choices=list(_LEDGER_MODES),
                   help="ledger mode: mutable (default) or fixed")
    p.add_argument("--case-folder", dest="case_folder", default=None)
    p.add_argument("--model", default=None, choices=list(_MODELS),
                   help="default model for spawned deputies (Task 376); unset keeps existing")
    p.add_argument("--role", default="sheriff")

    p = drop.add_parser("mode", help="print a precinct's ledger mode (mutable|fixed)")
    p.add_argument("--dept", required=True)

    p = drop.add_parser("model", help="print a precinct's default model (Task 376; opus if unset)")
    p.add_argument("--dept", required=True)

    p = drop.add_parser("status", help="print a precinct's lifecycle status (active|deleted; Phase D)")
    p.add_argument("--dept", required=True)

    p = drop.add_parser("delete",
                        help="SOFT-DELETE a precinct -> .trash + status=deleted (lock; SHERIFF ONLY)")
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="sheriff", help="must be 'sheriff'")
    p.add_argument("--retention-days", dest="retention_days", type=int,
                   default=TRASH_RETENTION_DAYS, help=f"days before purge (default {TRASH_RETENTION_DAYS})")

    p = drop.add_parser("restore",
                        help="RESTORE a soft-deleted precinct from .trash (lock; SHERIFF ONLY)")
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="sheriff", help="must be 'sheriff'")

    p = drop.add_parser("purge",
                        help="HARD-DELETE a soft-deleted precinct's trash + entry (lock; SHERIFF ONLY)")
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="sheriff", help="must be 'sheriff'")

    # ---- sheriff (global sheriff config: the ONE system-wide sheriff model) ----
    shf = rec.add_parser("sheriff", help="global sheriff config (the system-wide sheriff model, Task 384b)")
    shfop = shf.add_subparsers(dest="op", required=True)

    p = shfop.add_parser("config", help="print the global sheriff config (sheriff_config.json) as JSON")

    p = shfop.add_parser("model", help="print (or --set) the GLOBAL sheriff model (fable if unset)")
    p.add_argument("--set", dest="set_model", default=None, choices=list(_MODELS),
                   help="set the global sheriff model (SHERIFF-ONLY)")
    p.add_argument("--role", default="sheriff", help="must be 'sheriff' to --set")

    return ap


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.record == "ledger":
            if args.op == "read":
                sys.stdout.write(ledger_read(args.dept))
            elif args.op == "append":
                ledger_append(args.dept, _read_input(args.text, args.file), role=args.role)
                info = ledger_length(args.dept)
                print(f"appended to ledger[{args.dept}] (now {info['chars']} chars, "
                      f"~{info['approx_tokens']} tokens)", file=sys.stderr)
            elif args.op == "write":
                ledger_write(args.dept, _read_input(args.text, args.file), role=args.role)
                info = ledger_length(args.dept)
                print(f"wrote ledger[{args.dept}] (now {info['chars']} chars, "
                      f"~{info['approx_tokens']} tokens)", file=sys.stderr)
            elif args.op == "author":
                ledger_author(args.dept, _read_input(args.text, args.file), force=args.force)
                info = ledger_length(args.dept)
                print(f"authored ledger[{args.dept}] (now {info['chars']} chars, "
                      f"~{info['approx_tokens']} tokens)", file=sys.stderr)
            elif args.op == "length":
                print(json.dumps(ledger_length(args.dept)))
        elif args.record == "log":
            if args.op == "read":
                sys.stdout.write(log_read(args.dept))
            elif args.op == "append":
                log_append(args.dept, args.task, args.summary,
                           case_file_path=args.case_file, role=args.role,
                           case_folder=args.case_folder, deputy=args.deputy)
                print(f"appended to log[{args.dept}]: task {args.task}", file=sys.stderr)
            elif args.op == "remove":
                nrm = log_remove(args.dept, args.task, role=args.role)
                print(f"removed {nrm} line(s) for task {args.task} from log[{args.dept}]",
                      file=sys.stderr)
        elif args.record == "directory":
            if args.op == "read":
                if args.md:
                    sys.stdout.write(_read_text(_directory_md_path()))
                else:
                    print(json.dumps(directory_read(include_deleted=args.include_deleted),
                                     indent=2, ensure_ascii=False))
            elif args.op == "register":
                entry = directory_register(args.name, description=args.description,
                                           ledger_mode=args.mode,
                                           case_folder=args.case_folder,
                                           model=args.model, role=args.role)
                print(f"registered precinct[{args.name}] mode={entry['ledger_mode']} "
                      f"model={entry.get('model', DEFAULT_MODEL)}", file=sys.stderr)
            elif args.op == "mode":
                print(precinct_mode(args.dept))
            elif args.op == "model":
                print(precinct_model(args.dept))
            elif args.op == "status":
                st = precinct_status(args.dept)
                print(st if st is not None else "unregistered")
            elif args.op == "delete":
                entry = precinct_soft_delete(args.name, role=args.role,
                                             retention_days=args.retention_days)
                print(f"soft-deleted precinct[{args.name}] -> {entry.get('trash')} "
                      f"(purge_after epoch {entry.get('purge_after'):.0f})", file=sys.stderr)
            elif args.op == "restore":
                precinct_restore(args.name, role=args.role)
                print(f"restored precinct[{args.name}] from trash", file=sys.stderr)
            elif args.op == "purge":
                res = precinct_purge(args.name, role=args.role)
                print(f"hard-purged precinct[{args.name}] (removed {res['trash']})", file=sys.stderr)
        elif args.record == "sheriff":
            if args.op == "config":
                print(json.dumps(sheriff_config_read(), indent=2, ensure_ascii=False))
            elif args.op == "model":
                if args.set_model is not None:
                    sheriff_model_set(args.set_model, role=args.role)
                    print(f"set global sheriff model = {args.set_model}", file=sys.stderr)
                else:
                    print(sheriff_model_get())
    except PermissionError as e:
        print(f"PERMISSION DENIED: {e}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
