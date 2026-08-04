#!/usr/bin/env python3
"""Task 384 (Feng uid=384): SHERIFF wake-on-subtask-completion.

A launching deputy that spawns SUB-DEPUTIES (registered sub-cases) and needs to WAIT
for them before continuing can register a WAKE with the sheriff and then PARK (end its
turn safely). When the named subtasks close, the sheriff wakes it — the deputy resumes
automatically with a "SUBTASKS DONE" message injected. This mirrors the jobmgr
event-wake (scratch_job_sleep.sh), but the waker is the SHERIFF and the trigger is
sub-deputy completion (their done-sentinels), not a jobmgr job.

A subtask (sub-deputy) is COMPLETE when its done-sentinel
``scratch_full_logs/worker_<subdeputy>.done`` exists.

Registry: ``<records_root>/wakes/<parent>.json`` =
    {parent, wait_for: ["<subdeputy>", ...], mode: "all"|"any", note, ts}

The sheriff daemon calls ``process_all()`` each pass (ZERO API — file checks + a
mailbox append + a tmux relaunch; never a claude call). CLI mirrors the API.

Env: TSOMP_RECORDS_ROOT (registry root; tests), TSOMP_SCRATCH_ROOT (scratch_full_logs
root, for the sentinels/relaunch scripts; tests).
"""
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
_MODES = ("all", "any")


def _records_root() -> Path:
    env = os.environ.get("TSOMP_RECORDS_ROOT")
    return Path(env) if env else (REPO_ROOT / "scratch_full_logs" / "records")


def _scratch_root() -> Path:
    env = os.environ.get("TSOMP_SCRATCH_ROOT")
    return Path(env) if env else (REPO_ROOT / "scratch_full_logs")


def _wakes_dir() -> Path:
    return _records_root() / "wakes"


def _wake_path(parent) -> Path:
    return _wakes_dir() / f"{parent}.json"


def _sentinel(name) -> Path:
    return _scratch_root() / f"worker_{name}.done"


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


def _atomic_write(target: Path, data) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_wake_", suffix=".json")
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


def register(parent, wait_for, mode="all", note="", ts=None):
    """Register a wake: wake `parent` when its `wait_for` sub-deputies complete."""
    parent = str(parent).strip()
    if isinstance(wait_for, str):
        wait_for = [w.strip() for w in wait_for.split(",") if w.strip()]
    wait_for = [str(w).strip() for w in wait_for if str(w).strip()]
    if not parent or not wait_for:
        raise ValueError("parent and a non-empty wait_for are required")
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}")
    rec = {"parent": parent, "wait_for": wait_for, "mode": mode,
           "note": note or "", "ts": ts if ts is not None else time.time()}
    target = _wake_path(parent)
    with _lock(target):
        _atomic_write(target, rec)
    return rec


def get(parent):
    try:
        return json.loads(_wake_path(parent).read_text())
    except Exception:
        return None


def clear(parent):
    p = _wake_path(parent)
    if p.exists():
        with contextlib.suppress(OSError):
            p.unlink()
        return True
    return False


def list_wakes():
    d = _wakes_dir()
    out = []
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                continue
    return out


def done_subtasks(wait_for):
    """Which of `wait_for` have completed (their done-sentinel exists)."""
    return [w for w in wait_for if _sentinel(w).exists()]


def is_ready(rec):
    """True if `rec`'s wake condition is satisfied (all/any subtasks complete)."""
    if not isinstance(rec, dict):
        return False
    wait_for = rec.get("wait_for") or []
    done = done_subtasks(wait_for)
    if rec.get("mode") == "any":
        return len(done) >= 1
    return len(done) == len(wait_for) and len(wait_for) > 0


def _set_watchdog_state(name, state):
    """Best-effort watchdog_jobs.json state flip (park/unpark), mirroring job_sleep."""
    p = _scratch_root() / "watchdog_jobs.json"
    try:
        with _lock(p):
            jobs = json.loads(p.read_text()) if p.exists() else []
            for j in jobs:
                if j.get("name") == name:
                    j["state"] = state
            _atomic_write(p, jobs)
    except Exception:
        pass


def park(parent):
    """Park `parent` for a sheriff wake: touch its done-sentinel (so the sentinel
    trap treats the turn-end as complete) + set watchdog state=waiting_jobs (so the
    watchdog leaves it alone). REFUSES if no wake is registered (nothing would ever
    wake it). The deputy calls this, then ENDS ITS TURN. The wake reverses both."""
    if get(parent) is None:
        raise SystemExit(f"refusing to park {parent}: no wake registered "
                         f"(register one first with `register --parent {parent} --wait ...`)")
    _sentinel(parent).parent.mkdir(parents=True, exist_ok=True)
    _sentinel(parent).touch()
    _set_watchdog_state(parent, "waiting_jobs")
    return True


def wake_parent(parent, done_list, dry=False):
    """Wake a parked parent: clear its stale done-sentinel (so it is tracked as
    RUNNING again while it resumes), flip watchdog state back to running, inject a
    'SUBTASKS DONE' message into its mailbox, and relaunch it via its registered
    relaunch script. ZERO API here (the relaunch script starts claude in a separate
    tmux; this call shells only mailbox-append + tmux)."""
    msg = ("=== SUBTASKS DONE (sheriff wake) ===\n"
           f"The subtask(s) you were waiting on have completed: {', '.join(done_list)}.\n"
           "Resume your case: reconcile their deliverables/case files and continue.\n"
           "=== END ===\n")
    if dry:
        return {"parent": parent, "done": done_list, "dry": True}
    # relaunch script for the parent
    rel = _scratch_root() / f"scratch_worker_{parent}_relaunch.sh"
    rel_repo = REPO_ROOT / f"scratch_worker_{parent}_relaunch.sh"
    relaunch = rel if rel.exists() else rel_repo
    # 1) clear the stale park sentinel so the resumed worker is tracked as running
    with contextlib.suppress(OSError):
        _sentinel(parent).unlink()
    _set_watchdog_state(parent, "running")
    # 2) inject the wake message into the parent's mailbox (origin: sheriff relay)
    with contextlib.suppress(Exception):
        mf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt")
        mf.write(f"FROM: sheriff\nSUBJECT: subtasks done\n\n{msg}")
        mf.close()
        subprocess.run(["bash", str(REPO_ROOT / "scratch_mailbox_append.sh"),
                        parent, "relay from sheriff", mf.name],
                       cwd=str(REPO_ROOT), timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        with contextlib.suppress(OSError):
            os.unlink(mf.name)
    # 3) relaunch the parent (tmux-gone parked worker) via its relaunch script
    if relaunch.exists():
        with contextlib.suppress(Exception):
            subprocess.run(["tmux", "new-session", "-d", "-s", parent,
                            f"bash {relaunch}"], cwd=str(REPO_ROOT), timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return {"parent": parent, "done": done_list, "relaunched": relaunch.exists()}


def process_all(dry=False):
    """Sheriff pass: wake every parent whose wake condition is satisfied, then clear
    its wake. Returns the list of woken parents. ZERO API; robust per-record."""
    woken = []
    for rec in list_wakes():
        try:
            if is_ready(rec):
                parent = rec["parent"]
                done = done_subtasks(rec.get("wait_for") or [])
                res = wake_parent(parent, done, dry=dry)
                if not dry:
                    clear(parent)
                woken.append(res)
        except Exception:
            continue
    return woken


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_subtask_wake.py",
                                 description="Sheriff wake-on-subtask-completion (Task 384).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="register a wake for a parent deputy")
    r.add_argument("--parent", required=True)
    r.add_argument("--wait", required=True, help="comma-separated sub-deputy names")
    r.add_argument("--mode", default="all", choices=list(_MODES))
    r.add_argument("--note", default="")
    p = sub.add_parser("park", help="park the parent for the sheriff wake (then END YOUR TURN)")
    p.add_argument("--parent", required=True)
    g = sub.add_parser("get", help="print a parent's wake as JSON")
    g.add_argument("--parent", required=True)
    c = sub.add_parser("clear", help="remove a parent's wake")
    c.add_argument("--parent", required=True)
    sub.add_parser("list", help="list all registered wakes")
    pr = sub.add_parser("process", help="sheriff pass: wake all ready parents")
    pr.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "register":
        print(json.dumps(register(a.parent, a.wait, mode=a.mode, note=a.note)))
    elif a.cmd == "park":
        park(a.parent)
        print(f"parked {a.parent} (touch sentinel + watchdog waiting_jobs); END YOUR TURN — "
              f"the sheriff wakes you when the subtasks finish")
    elif a.cmd == "get":
        print(json.dumps(get(a.parent), indent=2))
    elif a.cmd == "clear":
        print("cleared" if clear(a.parent) else "no wake")
    elif a.cmd == "list":
        print(json.dumps(list_wakes(), indent=2))
    elif a.cmd == "process":
        print(json.dumps(process_all(dry=a.dry), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
