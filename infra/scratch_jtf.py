#!/usr/bin/env python3
"""Case 384e / Phase E: the JOINT TASK FORCE (JTF) backend BRIDGE.

A JTF (formerly "Cowork", Task 353) is one **lead** + one or more **collaborators**
(+ an optional **critic**) working together on one task. Each slot (the lead and each
collaborator) is EITHER a **precinct** or a **specific deputy**:

  * PRECINCT slot   -> spawn a NEW deputy in that precinct (fresh worker, precinct
                       default model) via scratch_spawn_worker.sh.
  * DEPUTY slot     -> that EXACT deputy takes the role and MUST take it (never hands
                       it off). We reach the deputy with the SAME kill+relaunch-inject
                       machinery as an email reply (scratch_interrupt_worker.sh): the
                       must-take JTF assignment is injected into its mailbox and it is
                       relaunched to read it first. The deputy prompt's JTF MUST-TAKE
                       PROTOCOL (scratch_spawn_worker.sh preamble) tells a busy deputy
                       to stop -> hand over -> notify the sheriff -> take.

The optional **critic** is NOT spawned here: per the Task 384 collaboration model it
is an ANONYMOUS worker (scratch_spawn_anon.sh) that the LEAD owns (deputy-owned, no
paperwork). The lead is instructed (in its role spec / assignment) to spawn it.

FLOW (mirrors the robust Task 377 web-case bridge):
  dashboard authed POST /api/jtf  ->  drops a record under
    scratch_full_logs/jtf/pending/<sid>.json
  the inbox loop calls `scratch_jtf.py process` each pass  ->  this bridge CLAIMS each
  record (flock), materializes every slot, registers the group, and emails
  the operator an ACK.

Directory contract (shared with the dashboard server):
  jtf/pending/<sid>.json = {id, ts, lead:{kind,name}, collaborators:[{kind,name}...],
                            critic:bool, description, source:"web", requester}
  processed -> jtf/done/<sid>.json      (record annotated with jtf_id + assignments)
  errored   -> jtf/failed/<sid>.json    (record annotated with the error)

ROBUSTNESS (Feng's "a bad submission never wedges anything"): a single flock guards
the whole pass, each record is processed in a try/except and moved to failed/ on any
error, each PARTICIPANT is materialized in its own try/except (one bad slot never
aborts the group), and nothing here blocks (spawns/interrupts are fire-and-forget,
the email is best-effort).

Override the root with TSOMP_JTF_ROOT (tests). `--dry` does everything EXCEPT the
real spawn / interrupt / email (prints instead) so the flow is testable offline.
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
DEFAULT_JTF_ROOT = REPO_ROOT / "scratch_full_logs" / "jtf"
DEFAULT_INBOX_DIR = REPO_ROOT / "scratch_full_logs" / "inbox"
DEFAULT_REQUESTER = os.environ.get("INFRA_OPERATOR_EMAIL", "")   # operator's address
_KINDS = ("precinct", "deputy")


def _root() -> Path:
    env = os.environ.get("TSOMP_JTF_ROOT")
    return Path(env) if env else DEFAULT_JTF_ROOT


def _inbox_dir() -> Path:
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


def known_precincts() -> set:
    """The live ACTIVE precinct set (soft-deleted ones excluded), from the records
    manager (honours TSOMP_RECORDS_ROOT). Empty set if unavailable — a precinct slot
    is then rejected, never silently mis-spawned."""
    try:
        import scratch_records as sr
        return set((sr.directory_read().get("precincts") or {}).keys())
    except Exception:
        return set()


def norm_slot(slot) -> dict:
    """Normalize ONE slot to {"kind": "precinct"|"deputy", "name": <safe>}.

    This is the precinct-vs-deputy slot-resolution primitive. It validates SHAPE
    only (kind in the allowed set; a non-empty safe name); membership of a precinct
    in the live directory is checked later, where the known set is available. Raises
    ValueError on a malformed slot so the caller can quarantine the whole record."""
    if not isinstance(slot, dict):
        raise ValueError(f"slot must be an object, got {type(slot).__name__}")
    kind = str(slot.get("kind", "")).strip().lower()
    if kind not in _KINDS:
        raise ValueError(f"slot kind must be one of {_KINDS}, got {kind!r}")
    name = _safe_name(str(slot.get("name", "")))
    if not name:
        raise ValueError("slot name is empty/invalid")
    return {"kind": kind, "name": name}


def resolve_slots(record) -> tuple:
    """Return (lead_slot, [collaborator_slots]) normalized + DEDUPED.

    A specific DEPUTY can hold only one role, so a deputy that appears more than once
    (as lead and/or several collaborators) is kept only in its FIRST occurrence, lead
    first. PRECINCT slots are never deduped — each is a distinct fresh deputy, so the
    same precinct may legitimately appear several times. Raises ValueError if the lead
    is missing or any slot is malformed."""
    lead_raw = record.get("lead")
    if not lead_raw:
        raise ValueError("a JTF needs a lead slot")
    lead = norm_slot(lead_raw)
    seen_deputies = {lead["name"]} if lead["kind"] == "deputy" else set()
    collabs = []
    for c in (record.get("collaborators") or []):
        s = norm_slot(c)
        if s["kind"] == "deputy":
            if s["name"] in seen_deputies:
                continue                      # a deputy can hold only one role
            seen_deputies.add(s["name"])
        collabs.append(s)
    if not collabs:
        raise ValueError("a JTF needs at least one collaborator")
    return lead, collabs


def _role_label(role, idx):
    return "lead" if role == "lead" else f"c{idx}"


def _roster_text(jtf_id, lead_final, collab_finals):
    """A one-block human roster every participant is given, so the lead knows who to
    coordinate and each collaborator knows who leads."""
    def desc(p):
        if p["kind"] == "precinct":
            return f"{p['deputy']} (new deputy, precinct {p['name']})"
        return f"{p['name']} (specific deputy)"
    lines = [f"JTF {jtf_id} members:",
             f"  LEAD: {desc(lead_final)}"]
    for p in collab_finals:
        lines.append(f"  COLLABORATOR: {desc(p)}")
    return "\n".join(lines)


def _critic_instruction(jtf_id, critic):
    if not critic:
        return ("No critic was requested for this JTF.")
    return (
        "A CRITIC was requested. As the lead, once you have a draft of the deliverables, "
        "spawn an INDEPENDENT critic as an ANONYMOUS worker (Task 384 collaboration model — "
        "deputy-owned, no paperwork) to audit them before you finalize:\n"
        f"    bash scratch_spawn_anon.sh jtf{_safe_name(str(jtf_id))}_critic <prompt_file>\n"
        "Write the critic a prompt describing exactly what to review and where the "
        "deliverables are; poll scratch_full_logs/anon_jtf" + _safe_name(str(jtf_id)) +
        "_critic.log for its verdict; you OWN it (monitor + relaunch if it dies). Fold "
        "its findings in before the FINAL email.")


def _participant_spec(*, jtf_id, role, case, precinct, deputy, description,
                      roster, critic, requester):
    """The task spec for a PRECINCT-slot participant (a fresh deputy). Stamped with
    parent_task/precinct/deputy at the top exactly like the web-case spec, so the
    normal spawn plumbing (case symlink, precinct stamp, active-deputies) works."""
    is_lead = (role == "lead")
    title = f"JTF {jtf_id} — {'LEAD' if is_lead else 'collaborator'}"
    lines = [
        f"parent_task: {jtf_id}",
        f"precinct: {precinct}",
        f"deputy: {deputy}",
        "",
        f"# Case {case} — {title}",
        "",
        f"You are part of **Joint Task Force (JTF) {jtf_id}**, a group of agents working "
        f"together on ONE task. You are the **{'LEAD' if is_lead else 'COLLABORATOR'}** for "
        f"this JTF, spawned fresh in the `{precinct}` precinct (you carry this precinct's "
        "knowledge and ledger).",
        "",
        "## The task (verbatim from the request)",
        "",
        (description or "").strip() or "(no description provided)",
        "",
        "## Your JTF",
        "",
        "```",
        roster,
        "```",
        "",
    ]
    if is_lead:
        lines += [
            "## Your role: LEAD",
            "",
            "You OWN the deliverables: plan the work, delegate to your collaborators, "
            "review + iterate, and drive the JTF to completion. Coordinate the "
            "collaborators BY EMAIL to the requester and, when you need to task one "
            "directly, RELAY to it (bash scratch_mailbox_append.sh <collab> \"relay from "
            f"agent {deputy}\" <file>, or scratch_interrupt_worker.sh <collab> --relay-from "
            f"{deputy} --message-file <file> to interrupt+relay). Every collaborator is "
            "watchdog-monitored, exactly like regular work.",
            "",
            _critic_instruction(jtf_id, critic),
            "",
        ]
    else:
        lines += [
            "## Your role: COLLABORATOR",
            "",
            "Carry your precinct's knowledge to the task and do your part. The LEAD "
            "coordinates the JTF and owns the deliverables; attend to the lead's relayed "
            "instructions, email the requester your results, and keep the lead in the "
            "loop. If you get no instruction from the lead promptly, email the lead's "
            "requester with what you can contribute and ask how to proceed.",
            "",
        ]
    lines += [
        "## Worker discipline",
        "",
        f"Email {requester} a plan + milestone(s) + FINAL; run work synchronously in the "
        "foreground; follow THE WAIT RULES; check your mailbox between steps. On completion "
        f"CLOSE THE CASE: `log append --dept {precinct} --deputy {deputy} --task {case} ...`, "
        "`ledger append`, then touch your done-sentinel.",
        "",
    ]
    return "\n".join(lines)


def _assignment_message(*, jtf_id, role, case, deputy, description, roster, critic,
                        requester):
    """The must-take JTF assignment injected into a SPECIFIC deputy's mailbox. The
    'JTF ASSIGNMENT (must-take)' header is what the deputy's JTF MUST-TAKE PROTOCOL
    (its prompt preamble) keys on."""
    is_lead = (role == "lead")
    parts = [
        "JTF ASSIGNMENT (must-take)",
        "",
        f"You have been assigned to **Joint Task Force (JTF) {jtf_id}** as its "
        f"**{'LEAD' if is_lead else 'COLLABORATOR'}**. This is a MUST-TAKE assignment: "
        "follow the JTF MUST-TAKE PROTOCOL in your prompt preamble (you may NOT delegate "
        "or decline this JTF role — if you are mid-task, STOP and hand your CURRENT case "
        "over to a new deputy, NOTIFY the sheriff of the swap, THEN take this JTF).",
        "",
        f"Take this JTF as case {case} (in your own precinct). Title your JTF emails "
        f"\"Case {case}: JTF {jtf_id} — {'lead' if is_lead else 'collaborator'}\".",
        "",
        "The task (verbatim from the request):",
        (description or "").strip() or "(no description provided)",
        "",
        "Your JTF:",
        roster,
        "",
    ]
    if is_lead:
        parts += [
            "As the LEAD you own the deliverables: plan, delegate to your collaborators "
            "(coordinate by email + relay), review and iterate, and drive the JTF to "
            "completion.",
            _critic_instruction(jtf_id, critic),
        ]
    else:
        parts += [
            "As a COLLABORATOR, do your part and coordinate with the lead (attend to the "
            "lead's relayed instructions; email the requester your results).",
        ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# side-effecting steps (stubbed in tests; printed under --dry)
# --------------------------------------------------------------------------- #
def _spawn_precinct(*, jtf_id, role, case, precinct, deputy, description, roster,
                    critic, requester, dry):
    """Materialize a PRECINCT slot: write the spec + spawn a fresh deputy in the
    precinct (precinct default model — WORKER_MODEL left unset)."""
    inbox = _inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    spec_path = inbox / f"task_{case}.md"
    spec_path.write_text(_participant_spec(
        jtf_id=jtf_id, role=role, case=case, precinct=precinct, deputy=deputy,
        description=description, roster=roster, critic=critic, requester=requester))
    kws = ",".join(["jtf", str(jtf_id), precinct, role])
    if dry:
        print(f"[dry-spawn] WORKER_PRECINCT={precinct} scratch_spawn_worker.sh "
              f"{deputy} {spec_path} {requester} {kws!r}")
        return
    env = dict(os.environ)
    env["WORKER_PRECINCT"] = precinct
    env.pop("WORKER_MODEL", None)            # let the precinct default model apply
    subprocess.run(["bash", "scratch_spawn_worker.sh", deputy, str(spec_path),
                    requester, kws],
                   cwd=str(REPO_ROOT), env=env, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _assign_deputy(*, jtf_id, role, case, deputy, description, roster, critic,
                   requester, dry):
    """Materialize a specific-DEPUTY slot (must-take): inject the assignment into the
    deputy's mailbox and interrupt+relaunch it (the same machinery as an email reply),
    so it reads the JTF assignment as its first action. Returns True iff reached."""
    dirs = _dirs()
    msg_dir = dirs["att"] / str(jtf_id)
    msg_dir.mkdir(parents=True, exist_ok=True)
    msg_path = msg_dir / f"assign_{deputy}.md"
    msg_path.write_text(_assignment_message(
        jtf_id=jtf_id, role=role, case=case, deputy=deputy, description=description,
        roster=roster, critic=critic, requester=requester))
    if dry:
        print(f"[dry-assign] scratch_interrupt_worker.sh {deputy} --relay-from jtf "
              f"--message-file {msg_path}")
        return True
    try:
        r = subprocess.run(["bash", "scratch_interrupt_worker.sh", deputy,
                            "--relay-from", "jtf", "--message-file", str(msg_path)],
                           cwd=str(REPO_ROOT), timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return r.returncode == 0
    except Exception:
        return False


def _ack(record, *, jtf_id, lead_final, collab_finals, critic, spawned, dry):
    """Email fenghaod an ACK once the JTF is materialized (best-effort)."""
    requester = record.get("requester") or DEFAULT_REQUESTER
    desc = (record.get("description") or "").strip()

    def line(p):
        if p["kind"] == "precinct":
            tag = "spawned" if p.get("ok", True) else "FAILED to spawn"
            return f"    {p['role']:12s} {p['deputy']} — new deputy in precinct {p['name']} ({tag}, case {p['case']})"
        tag = "assigned (must-take)" if p.get("ok", True) else "COULD NOT REACH"
        return f"    {p['role']:12s} {p['name']} — specific deputy, {tag} (case {p['case']})"

    roster_lines = [line({**lead_final, "role": "LEAD"})]
    roster_lines += [line({**p, "role": "collab"}) for p in collab_finals]
    subject = f"JTF {jtf_id} created: {desc.splitlines()[0][:60] if desc else 'new joint task force'}"
    src = {"web": "through the dashboard",
           "cron-weekly": "by the weekly-slides cron"}.get(
        record.get("source") or "web", f"via {record.get('source')}")
    body = (
        f"A new Joint Task Force (JTF) was submitted {src} and has been "
        "materialized by the inbox handler.\n\n"
        f"  JTF id:    {jtf_id}\n"
        f"  critic:    {'yes (the lead will spawn an anonymous critic)' if critic else 'no'}\n\n"
        "Members:\n" + "\n".join(roster_lines) + "\n\n"
        "Description:\n" + (desc or "(none)") + "\n\n"
        "Precinct slots were spawned as fresh deputies (precinct default model); specific-"
        "deputy slots were sent a must-take assignment (they stop + hand over any current "
        "case, notify the sheriff, then take the JTF). The lead owns the deliverables and "
        "will email you a plan, milestones, and a FINAL.")
    if dry:
        print(f"[dry-ack] to={requester} subject={subject!r}")
        return
    env = dict(os.environ)
    env["WORKER_PRECINCT"] = "infra"
    env["TSOMP_CASE"] = str(jtf_id)
    cmd = [sys.executable, "scratch_notify_email.py", subject, body,
           "--agent", "jtf", "--to", requester]
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _allocate(n=1):
    import scratch_case_seq as cs
    return cs.allocate(n)


def process_one(rec_path: Path, dry=False) -> dict:
    """Process ONE pending JTF record: resolve slots, allocate the group id + a case
    per participant, materialize the lead then the collaborators, ACK. Never raises to
    the caller; a malformed record is quarantined to failed/."""
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
        lead_slot, collab_slots = resolve_slots(record)
        # validate precinct membership up front (deputy slots are validated by the
        # interrupt machinery, which no-ops safely if the deputy can't be revived).
        known = known_precincts()
        for s in [lead_slot] + collab_slots:
            if s["kind"] == "precinct" and s["name"] not in known:
                raise ValueError(f"unknown precinct '{s['name']}'")
        critic = bool(record.get("critic"))
        requester = record.get("requester") or DEFAULT_REQUESTER

        jtf_id = _allocate()                       # the group id (shown to the user)

        # PASS 1 — resolve every participant's FINAL identity (deputy name + case)
        # without spawning, so each spec/assignment can carry the full roster.
        def finalize(slot, role, idx):
            case = _allocate()
            if slot["kind"] == "precinct":
                deputy = _safe_name(f"jtf{jtf_id}_{_role_label(role, idx)}")
                return {"kind": "precinct", "name": slot["name"], "deputy": deputy,
                        "case": case, "role": role}
            return {"kind": "deputy", "name": slot["name"], "deputy": slot["name"],
                    "case": case, "role": role}

        lead_final = finalize(lead_slot, "lead", 0)
        collab_finals = [finalize(s, "collab", i + 1) for i, s in enumerate(collab_slots)]
        roster = _roster_text(jtf_id, lead_final, collab_finals)

        # PASS 2 — materialize each slot (robust: one bad slot never aborts the group).
        def materialize(p):
            try:
                if p["kind"] == "precinct":
                    _spawn_precinct(jtf_id=jtf_id, role=p["role"], case=p["case"],
                                    precinct=p["name"], deputy=p["deputy"],
                                    description=record.get("description", ""),
                                    roster=roster, critic=(critic and p["role"] == "lead"),
                                    requester=requester, dry=dry)
                    p["ok"] = True
                else:
                    p["ok"] = _assign_deputy(
                        jtf_id=jtf_id, role=p["role"], case=p["case"], deputy=p["name"],
                        description=record.get("description", ""), roster=roster,
                        critic=(critic and p["role"] == "lead"), requester=requester, dry=dry)
            except Exception as ex:
                p["ok"] = False
                p["error"] = str(ex)
            return p

        materialize(lead_final)                    # lead first (it coordinates)
        for p in collab_finals:
            materialize(p)

        _ack(record, jtf_id=jtf_id, lead_final=lead_final, collab_finals=collab_finals,
             critic=critic, spawned=None, dry=dry)

        record.update({"jtf_id": jtf_id, "critic": critic,
                       "lead": lead_final, "collaborators": collab_finals,
                       "processed_ts": time.time()})
        if not dry:
            dirs["done"].mkdir(parents=True, exist_ok=True)
            dest = dirs["done"] / rec_path.name
            tmp = dest.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, dest)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(rec_path)
        return {"ok": True, "jtf_id": jtf_id, "lead": lead_final,
                "collaborators": collab_finals}
    except Exception as ex:
        record["error"] = str(ex)
        dest = dirs["failed"] / rec_path.name
        with contextlib.suppress(Exception):
            dest.write_text(json.dumps(record, indent=2))
            with contextlib.suppress(FileNotFoundError):
                os.unlink(rec_path)
        return {"ok": False, "error": str(ex), "id": sid}


def process_all(dry=False) -> list:
    """Process every pending JTF record, guarded by a single non-blocking flock so
    concurrent bridge invocations (the loop fires one per pass) never collide."""
    dirs = _dirs()
    lock_path = _root() / "bridge.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []                              # another bridge holds it; no-op
        results = []
        for rec in sorted(dirs["pending"].glob("*.json")):
            results.append(process_one(rec, dry=dry))
        return results
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_jtf.py",
                                 description="Materialize dashboard JTF submissions into spawned/assigned deputies.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="materialize all pending JTF submissions")
    p.add_argument("--dry", action="store_true", help="no spawn / interrupt / email; print instead")
    sub.add_parser("pending", help="count pending submissions")
    args = ap.parse_args(argv)
    if args.cmd == "process":
        res = process_all(dry=args.dry)
        for r in res:
            print(json.dumps(r))
        print(f"materialized {sum(1 for r in res if r.get('ok'))}/{len(res)} JTF(s)",
              file=sys.stderr)
    elif args.cmd == "pending":
        pend = _root() / "pending"
        print(len(list(pend.glob("*.json"))) if pend.is_dir() else 0)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
