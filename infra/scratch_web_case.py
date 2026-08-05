#!/usr/bin/env python3
"""Task 377 #4: the web "Create new case" BRIDGE (inbox-handler side).

The dashboard's authed ``POST /precinct/create_case`` (see claude_infra
dashboard/server.py) validates a submission, saves any uploaded files, ALLOCATES a
fresh case number (scratch_case_seq.py), and drops a "web-case" record under
``scratch_full_logs/web_cases/pending/``. The inbox loop calls this bridge each
pass; the bridge CLAIMS each pending record atomically, writes a task spec, SPAWNS
the deputy (precinct + model + parent + deputy all stamped), and emails
the operator an ACK UPON RECEIPT with the submitted form + uploaded
files attached.

ROBUSTNESS (Feng's "a bad upload never wedges the inbox loop"): a single flock
guards the whole pass (concurrent bridges no-op), each record is processed in a
try/except and moved to ``failed/`` on any error (never left to jam the queue), and
nothing here blocks — the spawn is fire-and-forget tmux, the email is best-effort.

Directory contract (shared with dashboard/server.py):
  web_cases/pending/<sid>.json = {id, ts, precinct, model, parent, description,
                                  files:[abs paths], case:<preallocated#>, source:"web",
                                  requester:<email>}
  web_cases/att/<sid>/...       = uploaded files (saved by the server)
  processed  -> web_cases/done/<sid>.json      (record annotated with case/deputy)
  errored    -> web_cases/failed/<sid>.json    (record annotated with the error)

Override the root with ``TSOMP_WEBCASES_ROOT`` (tests). ``--dry`` does everything
EXCEPT the tmux spawn and the real email (prints what it would do) so the flow is
testable offline.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_WEBCASES_ROOT = REPO_ROOT / "scratch_full_logs" / "web_cases"
DEFAULT_INBOX_DIR = REPO_ROOT / "scratch_full_logs" / "inbox"
DEFAULT_REQUESTER = os.environ.get("INFRA_OPERATOR_EMAIL", "")   # operator's address
_MODELS = ("fable", "opus", "sonnet", "haiku")


def _root() -> Path:
    env = os.environ.get("TSOMP_WEBCASES_ROOT")
    return Path(env) if env else DEFAULT_WEBCASES_ROOT


def _inbox_dir() -> Path:
    """Where the deputy task spec is written. Real inbox by default (spawn_worker +
    the dashboard read it there); ``TSOMP_INBOX_ROOT`` overrides it for tests."""
    env = os.environ.get("TSOMP_INBOX_ROOT")
    return Path(env) if env else DEFAULT_INBOX_DIR


def _dirs():
    root = _root()
    d = {k: root / k for k in ("pending", "done", "failed", "att")}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(s: str) -> str:
    """A tmux/worker-safe token: lowercase, [a-z0-9_-] only."""
    return re.sub(r"[^a-z0-9_-]+", "", (s or "").lower().replace(" ", "_")).strip("_-")


def _keywords(precinct: str, case, description: str) -> str:
    """Reply-routing keywords for the spawned deputy."""
    words = re.findall(r"[A-Za-z0-9]+", (description or "").lower())
    kws = [f"webcase", str(case), precinct] + words[:4]
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return ",".join(out)


def _write_spec(spec_path: Path, *, case, precinct, model, parent, deputy,
                description, files):
    """Write the deputy's task spec (parent_task/precinct/deputy stamped at top)."""
    first = (description or "").strip().splitlines()[0] if description.strip() else "web-submitted case"
    title = first[:80]
    lines = [
        f"parent_task: {parent if parent else 'none'}",
        f"precinct: {precinct}",
        f"deputy: {deputy}",
        "",
        f"# Case {case} — {title}",
        "",
        f"This case was submitted through the dashboard **Create new case** form for the "
        f"`{precinct}` precinct (model: {model or 'precinct default'}"
        + (f"; follow-up on task {parent}" if parent else "; new task") + ").",
        "",
        "## Task description (verbatim from the form)",
        "",
        (description or "").strip() or "(no description provided)",
        "",
    ]
    if files:
        lines.append("## Uploaded files (read these directly)")
        lines.append("")
        lines += [f"  - {p}" for p in files]
        lines.append("")
    lines += [
        "## Worker discipline",
        "",
        f"Email {DEFAULT_REQUESTER} a plan + milestone(s) + FINAL; run work synchronously in the "
        "foreground; follow THE WAIT RULES; check your mailbox between steps. On completion CLOSE "
        f"THE CASE: `log append --dept {precinct} --deputy {deputy} --task {case} ...`, `ledger "
        "append`, then touch your done-sentinel.",
        "",
    ]
    spec_path.write_text("\n".join(lines))


def _ack(record, *, case, deputy, spec_path, dry):
    """Email the requester an ACK UPON RECEIPT with the form + uploaded files attached."""
    precinct = record.get("precinct", "")
    model = record.get("model") or "(precinct default)"
    parent = record.get("parent") or "(none — new task)"
    desc = (record.get("description") or "").strip()
    files = [f for f in (record.get("files") or []) if os.path.isfile(f)]
    requester = record.get("requester") or DEFAULT_REQUESTER
    # Task 396: source-aware wording — an emailed precinct request routed DIRECTLY to a
    # deputy (no receptionist) vs a dashboard "Create new case" form submission.
    is_email = record.get("source") == "email"
    origin = ("your precinct-tagged email" if is_email
              else "the dashboard \"Create new case\" form")

    # a small text render of the submission, attached alongside the uploads
    dirs = _dirs()
    form_txt = dirs["att"] / record.get("id", f"case{case}") / "submitted_form.txt"
    form_txt.parent.mkdir(parents=True, exist_ok=True)
    form_txt.write_text(
        ("Emailed precinct request\n" if is_email else "Dashboard 'Create new case' submission\n")
        + f"  case:        {case}\n  precinct:    {precinct}\n  model:       {model}\n"
        f"  follow-up of:{parent}\n  deputy:      {deputy}\n"
        f"  uploads:     {len(files)}\n\n--- description ---\n{desc}\n")

    verb = "assigned" if is_email else "created"
    subject = f"Case {case} {verb} ({precinct}): {desc.splitlines()[0][:70] if desc else 'new case'}"
    body = (
        f"Your [{precinct}] request came in via {origin} and was routed DIRECTLY to the "
        f"{precinct} precinct — a dedicated deputy has been spawned to take it (no receptionist).\n\n"
        if is_email else
        "A new case was submitted through the dashboard \"Create new case\" form and has been "
        "picked up by the inbox handler.\n\n")
    body += (
        f"  case:         {case}\n"
        f"  precinct:     {precinct}\n"
        f"  model:        {model}\n"
        f"  follow-up of: {parent}\n"
        f"  deputy:       {deputy} (spawned in tmux, watchdog-tracked)\n"
        f"  uploads:      {len(files)} file(s)\n\n"
        "Description:\n"
        f"{desc or '(none)'}\n\n"
        "The deputy will email you a plan, milestones, and a FINAL. Your original "
        + ("email text and any attachments" if is_email else "submitted form and any uploaded files")
        + " are attached.")

    attach = [str(form_txt)] + files
    if dry:
        print(f"[dry-ack] to={requester} subject={subject!r} attach={[os.path.basename(a) for a in attach]}")
        return
    env = dict(os.environ)
    env["WORKER_PRECINCT"] = precinct
    env["TSOMP_CASE"] = str(case)
    if record.get("model"):
        env["TSOMP_MODEL"] = record["model"]
    cmd = [sys.executable, "scratch_notify_email.py", subject, body,
           "--agent", deputy, "--to", requester]
    for a in attach:
        cmd += ["--attach", a]
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _spawn(record, *, case, deputy, spec_path, dry):
    """Spawn the deputy for this web case (precinct + model exported)."""
    precinct = record.get("precinct", "")
    requester = record.get("requester") or DEFAULT_REQUESTER
    keywords = _keywords(precinct, case, record.get("description", ""))
    if dry:
        print(f"[dry-spawn] WORKER_PRECINCT={precinct} "
              f"WORKER_MODEL={record.get('model') or '(default)'} "
              f"scratch_spawn_worker.sh {deputy} {spec_path} {requester} {keywords!r}")
        return
    env = dict(os.environ)
    env["WORKER_PRECINCT"] = precinct
    if record.get("model") in _MODELS:
        env["WORKER_MODEL"] = record["model"]
    subprocess.run(["bash", "scratch_spawn_worker.sh", deputy, str(spec_path),
                    requester, keywords],
                   cwd=str(REPO_ROOT), env=env, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def process_one(rec_path: Path, dry=False) -> dict:
    """Process ONE pending web-case record: allocate/confirm case, write spec, spawn
    deputy, ACK. Returns {ok, case, deputy, ...}. Never raises to the caller."""
    dirs = _dirs()
    try:
        record = json.loads(rec_path.read_text())
    except Exception as ex:
        dest = dirs["failed"] / rec_path.name
        with contextlib.suppress(Exception):
            os.replace(rec_path, dest)
        return {"ok": False, "error": f"unreadable record: {ex}", "file": str(rec_path)}

    sid = record.get("id") or rec_path.stem
    try:
        precinct = _safe_name(record.get("precinct", ""))
        if not precinct:
            raise ValueError("missing/invalid precinct")
        model = record.get("model") or None
        if model is not None and model not in _MODELS:
            model = None                      # ignore a bad model, fall to precinct default
            record["model"] = None
        # case number: prefer the server-preallocated one; else allocate now.
        case = record.get("case")
        if not case:
            import scratch_case_seq as cs
            case = cs.allocate()
        record["case"] = case

        # Task 396: a precinct-tagged EMAIL passes a descriptive deputy_hint; a dashboard
        # web submission keeps the web_<case> name. Case suffix keeps it unique either way.
        deputy = _safe_name(record.get("deputy_hint") or f"web_{case}")
        inbox = _inbox_dir()
        spec_path = inbox / f"task_{case}.md"
        inbox.mkdir(parents=True, exist_ok=True)
        _write_spec(spec_path, case=case, precinct=precinct, model=model,
                    parent=record.get("parent"), deputy=deputy,
                    description=record.get("description", ""),
                    files=record.get("files") or [])

        _spawn(record, case=case, deputy=deputy, spec_path=spec_path, dry=dry)
        _ack(record, case=case, deputy=deputy, spec_path=spec_path, dry=dry)

        record.update({"deputy": deputy, "spec": str(spec_path),
                       "processed_ts": time.time()})
        dest = dirs["done"] / rec_path.name
        if not dry:
            dirs["done"].mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, dest)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(rec_path)
        return {"ok": True, "case": case, "deputy": deputy, "precinct": precinct,
                "spec": str(spec_path)}
    except Exception as ex:
        record["error"] = str(ex)
        dest = dirs["failed"] / rec_path.name
        with contextlib.suppress(Exception):
            dest.write_text(json.dumps(record, indent=2))
            with contextlib.suppress(FileNotFoundError):
                os.unlink(rec_path)
        return {"ok": False, "error": str(ex), "id": sid}


def process_all(dry=False) -> list:
    """Process every pending web-case, guarded by a single non-blocking flock so
    concurrent bridge invocations (the loop fires one per pass) never collide."""
    dirs = _dirs()
    lock_path = _root() / "bridge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []                          # another bridge holds it; no-op
        results = []
        for rec in sorted(dirs["pending"].glob("*.json")):
            results.append(process_one(rec, dry=dry))
        return results
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_web_case.py",
                                 description="Bridge dashboard web-case submissions into spawned deputies.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="process all pending web-case submissions")
    p.add_argument("--dry", action="store_true", help="no spawn / no email; print instead")
    sub.add_parser("pending", help="count pending submissions")
    args = ap.parse_args(argv)
    if args.cmd == "process":
        res = process_all(dry=args.dry)
        for r in res:
            print(json.dumps(r))
        print(f"processed {sum(1 for r in res if r.get('ok'))}/{len(res)} web-case(s)",
              file=sys.stderr)
    elif args.cmd == "pending":
        print(len(list((_root() / "pending").glob("*.json"))) if (_root() / "pending").is_dir() else 0)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
