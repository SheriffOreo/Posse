#!/usr/bin/env python3
"""Task 382 #1: the ACTIVE-DEPUTIES state file.

Source of truth for what CASE (and description) each live deputy is CURRENTLY
working, which the dashboard Status board reads. The problem it fixes: when a
deputy is relaunched on an email reply it first shows under its PREVIOUS case
(correct — it hasn't decided yet); when it then TAKES a new case it updates this
file, so the board flips to the real current case + description (the "still shows
377 while actually on 380" bug).

This is the FOUNDATION for the sheriff-managed active-deputies registry in the
approved redesign. Today the deputy writes its own entry; in the redesign the
deputy REQUESTS a new case number from the sheriff (via the request queue, with
concurrency control + an approval API call) and the SHERIFF writes this file. The
read side (dashboard) is identical either way, so promoting the writer to the
sheriff later needs no UI change.

Store: ``<records_root>/active_deputies.json`` =
    { "<deputy>": {case, description, precinct, updated_ts} }
Mutations are flock + atomic (mirrors scratch_records). ``TSOMP_RECORDS_ROOT``
overrides the root (tests).

CLI:
    scratch_deputy_state.py set --deputy X --case N [--description "..."] [--precinct p]
    scratch_deputy_state.py get [--deputy X]      # one entry, or the whole map (JSON)
    scratch_deputy_state.py clear --deputy X
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS_ROOT = REPO_ROOT / "scratch_full_logs" / "records"
FILE_NAME = "active_deputies.json"


def _records_root() -> Path:
    env = os.environ.get("TSOMP_RECORDS_ROOT")
    return Path(env) if env else DEFAULT_RECORDS_ROOT


def _path() -> Path:
    return _records_root() / FILE_NAME


@contextlib.contextmanager
def _lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lp = target.with_name(target.name + ".lock")
    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read() -> dict:
    try:
        d = json.loads(_path().read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atomic_write(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_actdep_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def set_state(deputy, case, description=None, precinct=None, ts=None):
    """Upsert a deputy's current (case, description, precinct). LOCK + atomic.
    Only non-None fields are updated (so a description-less update keeps the prior
    description). Returns the updated entry."""
    deputy = str(deputy).strip()
    if not deputy:
        raise ValueError("deputy is required")
    target = _path()
    with _lock(target):
        d = _read()
        e = d.get(deputy, {}) if isinstance(d.get(deputy), dict) else {}
        if case is not None and str(case) != "":
            e["case"] = str(case)
        if description is not None:
            e["description"] = str(description)
        if precinct is not None and str(precinct) != "":
            e["precinct"] = str(precinct)
        e["updated_ts"] = ts if ts is not None else time.time()
        d[deputy] = e
        _atomic_write(target, d)
        return e


def get(deputy=None):
    d = _read()
    return d if deputy is None else d.get(str(deputy))


def clear(deputy):
    """Remove a deputy's entry (e.g. when it closes with no successor). LOCK+atomic."""
    target = _path()
    with _lock(target):
        d = _read()
        if str(deputy) in d:
            del d[str(deputy)]
            _atomic_write(target, d)
            return True
        return False


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_deputy_state.py",
                                 description="Active-deputies current-case state (Task 382 #1).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="upsert a deputy's current case/description/precinct")
    s.add_argument("--deputy", required=True)
    s.add_argument("--case", required=True)
    s.add_argument("--description", default=None)
    s.add_argument("--precinct", default=None)
    g = sub.add_parser("get", help="print one entry or the whole map as JSON")
    g.add_argument("--deputy", default=None)
    c = sub.add_parser("clear", help="remove a deputy's entry")
    c.add_argument("--deputy", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "set":
        e = set_state(a.deputy, a.case, description=a.description, precinct=a.precinct)
        print(json.dumps({a.deputy: e}))
    elif a.cmd == "get":
        print(json.dumps(get(a.deputy), indent=2))
    elif a.cmd == "clear":
        print("cleared" if clear(a.deputy) else "no entry")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
