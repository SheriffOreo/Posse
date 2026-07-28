#!/usr/bin/env python3
"""Task 274 G3 — hand a worker's SELF-monitored jobs to jobmgr at limit-kill time.

When a worker is killed by a usage limit its claude process dies but every long job
it launched via scratch_detach.sh keeps running (setsid — see DESIGN.md G2). Those
self-monitored jobs are NOT tracked by jobmgr, so nothing would hold/notify/hand-over
their completion during the limit. This helper enumerates the dying worker's detach
jobs (each leaves scratch_full_logs/<owner>_<job>.pid + .output) and REGISTERS each as
a jobmgr running/ record (owner_agent=<owner>, wake_on_done=true, output_path known,
adopted=true) so jobmgr now monitors it exactly like a submitted job — waking, or
holding+notifying under a limit, on completion.

  Usage:  scratch_jobmgr_adopt.py <owner> [--all | --pidfile <path>]
          (default --all; the watchdog calls it with just <owner>)

Idempotent three ways so repeated limit events never double-adopt:
  * skip a pid file already referenced by a jobmgr running/ or pending/ record;
  * skip jobmgr's OWN detach jobs (named <owner>_job_<id>.pid);
  * skip anything with an existing scratch_full_logs/jobs/adopted/<basename>.marker
    (written on adopt).
An adopted job's exit code is unobservable (no jobmgr_run rc shim), so jobmgr finishes
it as done/rc-unknown on process exit; identity is (pid, /proc starttime), pid-reuse
safe. A pid file that is unreadable/malformed is reported (never silently dropped) —
the watchdog surfaces the summary line in its limit email.

PURE PYTHON. Honors TSOMP_JOBS_DIR (test isolation), same as scratch_jobmgr.py.
Prints ONE summary line (last line) for the caller's log/email.
"""
import glob
import json
import os
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS = Path(os.environ.get("TSOMP_JOBS_DIR") or (ROOT / "scratch_full_logs" / "jobs"))
RUNNING, PENDING, DONE = JOBS / "running", JOBS / "pending", JOBS / "done"
ADOPTED = JOBS / "adopted"
LOGDIR = ROOT / "scratch_full_logs"


def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.rename(path)                       # atomic publish


def _starttime_of(pid):
    """Field 22 of /proc/<pid>/stat — (pid, starttime) is a pid-reuse-safe identity."""
    try:
        stat = open(f"/proc/{pid}/stat").read()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except Exception:
        return None


def _cmd_of(pid):
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        return " ".join(a.decode(errors="replace") for a in raw.split(b"\0") if a)[:300]
    except OSError:
        return ""


def _tracked_pidfiles():
    """pid_file paths (as jobmgr stores them, relative to ROOT) already tracked in
    running/ or pending/ — these are jobmgr-managed, never self-monitored."""
    tracked = set()
    for d in (RUNNING, PENDING):
        for p in glob.glob(str(d / "*.json")):
            r = _read_json(p)
            if r and r.get("pid_file"):
                tracked.add(str(r["pid_file"]).lstrip("./"))
    return tracked


def _new_id():
    return f"jadopt{int(time.time() * 1000)}_{secrets.token_hex(3)}"


def adopt_one(owner, pidfile, tracked):
    """Adopt a single pid file. Returns ('adopted', jid) | ('skip', reason) |
    ('error', reason)."""
    pf = Path(pidfile)
    base = pf.name                                   # <owner>_<job>.pid
    stem = base[:-4] if base.endswith(".pid") else base
    rel = f"scratch_full_logs/{base}"

    # job part after the "<owner>_" prefix; jobmgr's own detach jobs are "job_<id>"
    jobpart = stem[len(owner) + 1:] if stem.startswith(owner + "_") else stem
    if jobpart.startswith("job_"):
        return ("skip", f"{base}: jobmgr-managed (job_* detach name)")
    if rel in tracked:
        return ("skip", f"{base}: already a jobmgr running/pending record")
    marker = ADOPTED / f"{stem}.marker"
    if marker.exists():
        return ("skip", f"{base}: already adopted (marker present)")

    try:
        pid = int(Path(pf).read_text().split()[0])
    except Exception as e:
        return ("error", f"{base}: unreadable pid file ({e})")

    starttime = _starttime_of(pid)
    dead = starttime is None
    # dead-at-adopt: use a sentinel starttime that can never match a live (reused)
    # pid, so jobmgr treats it as gone -> finishes -> hold+notify (never dropped).
    rec_starttime = -1 if dead else starttime
    output = f"scratch_full_logs/{stem}.output"
    jid = _new_id()
    now = time.time()
    try:
        start_ts = pf.stat().st_mtime
    except OSError:
        start_ts = now
    rec = {
        "job_id": jid, "owner_agent": owner, "type": "cpu",
        "command": _cmd_of(pid) or "(adopted self-monitored job; command unknown)",
        "submit_ts": now, "start_ts": start_ts, "est_seconds": 0,
        "status": "running", "wake_on_done": True,
        "output_path": output, "pid_file": rel,
        "result_path": f"scratch_full_logs/jobs/done/{jid}.json",
        "adopted": True, "adopted_ts": now,
        "adopted_reason": "limit-kill handoff of self-monitored scratch_detach.sh job",
        "pid": pid, "starttime": rec_starttime, "dead_at_adopt": dead,
    }
    RUNNING.mkdir(parents=True, exist_ok=True)
    _write_json(RUNNING / f"{jid}.json", rec)
    ADOPTED.mkdir(parents=True, exist_ok=True)
    _write_json(marker, {"stem": stem, "job_id": jid, "owner": owner,
                         "pid": pid, "adopted_ts": now, "dead_at_adopt": dead})
    return ("adopted", f"{jid}({base}{'' if not dead else ',dead@adopt'})")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("usage: scratch_jobmgr_adopt.py <owner> [--all | --pidfile <path>]", file=sys.stderr)
        return 2
    owner = sys.argv[1]
    if not owner or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in owner):
        print(f"ERR: bad owner name '{owner}' (lowercase [a-z0-9_-])", file=sys.stderr)
        return 2
    rest = sys.argv[2:]
    if rest and rest[0] == "--pidfile":
        if len(rest) < 2:
            print("ERR: --pidfile needs a path", file=sys.stderr)
            return 2
        pidfiles = [rest[1]]
    else:                                            # --all (default)
        pidfiles = sorted(glob.glob(str(LOGDIR / f"{owner}_*.pid")))

    ADOPTED.mkdir(parents=True, exist_ok=True)
    tracked = _tracked_pidfiles()
    adopted, skipped, errors = [], [], []
    for pf in pidfiles:
        kind, detail = adopt_one(owner, pf, tracked)
        (adopted if kind == "adopted" else errors if kind == "error" else skipped).append(detail)

    for d in adopted:
        print(f"  adopted: {d}")
    for d in skipped:
        print(f"  skip:    {d}")
    for d in errors:
        print(f"  ERROR:   {d}")
    summary = (f"adopted {len(adopted)} self-monitored job(s) for {owner}"
               + (": " + ", ".join(adopted) if adopted else "")
               + (f"; skipped {len(skipped)} already-tracked" if skipped else "")
               + (f"; {len(errors)} UNREADABLE pid file(s) — see log" if errors else ""))
    print(summary)                                   # last line = the caller's summary
    return 0


if __name__ == "__main__":
    sys.exit(main())
