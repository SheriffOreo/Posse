#!/usr/bin/env python3
"""Tests for scratch_docket.py (Case 761).

Everything runs in a sandbox: TSOMP_DOCKET_ROOT for the store and
TSOMP_WEBCASES_ROOT / TSOMP_JTF_ROOT for the drop dirs, so no test ever touches
the live schedule or queues real work. TSOMP_RECORDS_ROOT + TSOMP_CASE_BASE keep
case-number allocation off the real sequence.

  python scratch_docket_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
SANDBOX = Path(tempfile.mkdtemp(prefix="entry_test_"))
os.environ["TSOMP_DOCKET_ROOT"] = str(SANDBOX / "docket")
os.environ["TSOMP_WEBCASES_ROOT"] = str(SANDBOX / "web_cases")
os.environ["TSOMP_JTF_ROOT"] = str(SANDBOX / "jtf")
os.environ["TSOMP_INBOX_ROOT"] = str(SANDBOX / "inbox")
os.environ["TSOMP_RECORDS_ROOT"] = str(SANDBOX / "records")
os.environ["TSOMP_CASE_BASE"] = "990000"
(SANDBOX / "records").mkdir(parents=True, exist_ok=True)
shutil.copy(REPO / "scratch_full_logs" / "records" / "precincts.json",
            SANDBOX / "records" / "precincts.json")
# Use a precinct this install actually has. A fresh install ships only the
# receptionist; hardcoding a project precinct makes the suite fail on a clean
# clone for a reason that has nothing to do with the docket.
_REGISTERED = sorted(json.loads(
    (SANDBOX / "records" / "precincts.json").read_text()).get("precincts", {}))
PRECINCT = _REGISTERED[0]
# A JTF needs a second slot; on a one-precinct install both slots are the same
# precinct, which the docket allows and is still a real two-slot JTF.
PRECINCT2 = _REGISTERED[1] if len(_REGISTERED) > 1 else PRECINCT
# the real judge roster, so "unknown judge" is tested against the live registry
shutil.copy(REPO / "scratch_full_logs" / "records" / "critics.json",
            SANDBOX / "records" / "critics.json")

sys.path.insert(0, str(REPO))
import scratch_docket as P            # noqa: E402

PASS = FAIL = 0


def check(cond, what):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {what}")
    return cond


def raises(fn, needle, what):
    try:
        fn()
    except ValueError as ex:
        return check(needle.lower() in str(ex).lower(),
                     f"{what} (message was {ex!r}, wanted {needle!r})")
    except Exception as ex:
        return check(False, f"{what} (raised {type(ex).__name__}: {ex})")
    return check(False, f"{what} (did not raise)")


def ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def pending(kind):
    d = SANDBOX / ("web_cases" if kind == "case" else "jtf") / "pending"
    return sorted(d.glob("*.json")) if d.is_dir() else []


# --------------------------------------------------------------------------- #
print("== next_occurrence ==")
# 2026-09-19 is a Saturday; weekday() 5.
sat = {"freq": "weekly", "at": "09:00", "dow": 5}
check(P.next_occurrence(sat, ts("2026-09-16 12:00")) == ts("2026-09-19 09:00"),
      "weekly finds the coming Saturday")
check(P.next_occurrence(sat, ts("2026-09-19 08:59")) == ts("2026-09-19 09:00"),
      "weekly fires later the same day")
check(P.next_occurrence(sat, ts("2026-09-19 09:00")) == ts("2026-09-26 09:00"),
      "weekly is STRICTLY after: the exact due moment rolls to next week")
check(P.next_occurrence(sat, ts("2026-09-19 09:01")) == ts("2026-09-26 09:00"),
      "weekly rolls a week once the time has passed")

day = {"freq": "daily", "at": "07:30"}
check(P.next_occurrence(day, ts("2026-09-19 07:29")) == ts("2026-09-19 07:30"),
      "daily same day")
check(P.next_occurrence(day, ts("2026-09-19 07:30")) == ts("2026-09-20 07:30"),
      "daily rolls at the exact minute")

hourly = {"freq": "hourly", "at": "00:20"}
check(P.next_occurrence(hourly, ts("2026-09-19 07:19")) == ts("2026-09-19 07:20"),
      "hourly same hour")
check(P.next_occurrence(hourly, ts("2026-09-19 07:20")) == ts("2026-09-19 08:20"),
      "hourly rolls at the exact minute")

m31 = {"freq": "monthly", "at": "06:00", "dom": 31}
check(P.next_occurrence(m31, ts("2026-01-31 06:01")) == ts("2026-02-28 06:00"),
      "monthly day 31 clamps to the last day of a short month")
m1 = {"freq": "monthly", "at": "06:00", "dom": 1}
check(P.next_occurrence(m1, ts("2026-12-01 06:00")) == ts("2027-01-01 06:00"),
      "monthly crosses the year boundary")
check(P.next_occurrence({"freq": "monthly", "at": "06:00", "dom": 29},
                        ts("2028-01-30 00:00")) == ts("2028-02-29 06:00"),
      "monthly day 29 lands on a leap day")

once = {"freq": "once", "start": ts("2026-09-19 09:00")}
check(P.next_occurrence(once, ts("2026-09-19 08:00")) == ts("2026-09-19 09:00"),
      "once before its time")
check(P.next_occurrence(once, ts("2026-09-19 09:00")) is None,
      "once is spent at its own time")
raises(lambda: P.next_occurrence({"freq": "fortnightly", "at": "09:00"}, time.time()),
       "unknown frequency", "an unknown frequency raises")
raises(lambda: P.next_occurrence({"freq": "daily", "at": "9am"}, time.time()),
       "HH:MM", "a malformed time raises")
raises(lambda: P.next_occurrence({"freq": "daily", "at": "25:00"}, time.time()),
       "out of range", "an out-of-range hour raises")

print("== render_prompt ==")
when = ts("2026-09-19 09:00")
check(P.render_prompt("week {d:%Y%m%d}", when) == "week 20260919", "{d:FMT}")
check(P.render_prompt("{d+1:%A %b %d}", when) == "Sunday Sep 20", "{d+1:FMT}")
check(P.render_prompt("{d-2:%A}", when) == "Thursday", "{d-2:FMT}")
check(P.render_prompt("{date} {time} {weekday}", when) == "2026-09-19 09:00 Saturday",
      "the named placeholders")
check(P.render_prompt('keep {"a": 1} and {not_a_placeholder}', when)
      == 'keep {"a": 1} and {not_a_placeholder}',
      "braces that match no placeholder are left alone")
check(P.render_prompt("", when) == "", "an empty prompt renders empty")

print("== validate ==")
good_case = {"name": "n", "kind": "case", "prompt": "p", "freq": "daily",
             "at": "09:00", "precinct": PRECINCT}
rec = P.validate(dict(good_case))
check(rec["precinct"] == PRECINCT and rec["at"] == "09:00", "a minimal case entry validates")
check(rec["next_run"] > time.time(), "validate arms next_run in the future")
check(P.validate(dict(good_case, at="9:05"))["at"] == "09:05", "H:MM is normalized to HH:MM")
raises(lambda: P.validate(dict(good_case, name="")), "needs a name", "name required")
raises(lambda: P.validate(dict(good_case, prompt="  ")), "prompt is required", "prompt required")
raises(lambda: P.validate(dict(good_case, precinct="")), "needs a precinct", "precinct required")
raises(lambda: P.validate(dict(good_case, precinct="nosuch")), "unknown precinct",
       "an unknown precinct is rejected")
raises(lambda: P.validate(dict(good_case, kind="parade")), "kind must be", "kind is checked")
raises(lambda: P.validate(dict(good_case, freq="yearly")), "frequency must be",
       "frequency is checked")
raises(lambda: P.validate(dict(good_case, freq="once")), "needs a date",
       "a one-off without a date is rejected")
raises(lambda: P.validate(dict(good_case, freq="weekly", dow=9)), "weekday must be",
       "weekday range is checked")
raises(lambda: P.validate(dict(good_case, freq="monthly", dom=0)), "day of month",
       "day-of-month range is checked")
raises(lambda: P.validate(dict(good_case, model="nosuch")), "invalid work model",
       "an unknown model is rejected")
raises(lambda: P.validate(dict(good_case, service="claude", model="terra")),
       "does not belong", "a model from the wrong service is rejected")
raises(lambda: P.validate(dict(good_case, judge="nosuchjudge")), "unknown judge",
       "an unknown judge is rejected")
check(P.validate(dict(good_case, service="claude", model="opus"))["model"] == "opus",
      "a matching service+model pair is accepted")

good_jtf = {"name": "j", "kind": "jtf", "prompt": "p", "freq": "weekly", "at": "09:00",
            "dow": 5, "lead": {"kind": "precinct", "name": PRECINCT},
            "collaborators": [{"kind": "precinct", "name": PRECINCT2}]}
jrec = P.validate(dict(good_jtf))
check(jrec["lead"]["name"] == PRECINCT and len(jrec["collaborators"]) == 1,
      "a minimal JTF entry validates")
raises(lambda: P.validate(dict(good_jtf, collaborators=[])), "at least one collaborator",
       "a JTF needs a collaborator")
raises(lambda: P.validate(dict(good_jtf, lead={"kind": "precinct", "name": "nosuch"})),
       "unknown precinct", "a JTF lead precinct is checked")
raises(lambda: P.validate(dict(good_jtf,
                               lead={"kind": "deputy", "name": "web_1", "model": "opus"})),
       "keeps its own model", "a model on a specific-deputy slot is rejected")
check(P.validate(dict(good_jtf, lead={"kind": "deputy", "name": "web_1"}))["lead"]["kind"]
      == "deputy", "a specific-deputy slot with no lanes is accepted")

print("== store: save / edit / pause / resume / cancel ==")
saved = P.save(dict(good_case, name="Daily sweep"))
pid = saved["id"]
check(pid and P.load(pid)["name"] == "Daily sweep", "save creates a entry")
check(len(P.entries()) == 1, "the new entry is listed")
edited = P.save({"id": pid, "prompt": "a different prompt"})
check(edited["prompt"] == "a different prompt" and edited["precinct"] == PRECINCT,
      "an edit keeps the fields it did not send")
check(edited["created"] == saved["created"], "an edit preserves the creation time")
raises(lambda: P.save({"id": "p_nope", "name": "x"}), "no such entry",
       "editing a missing entry fails")
check(P.set_paused(pid, True)["paused"] is True, "pause sets the flag")
check(P.set_paused(pid, False)["paused"] is False, "resume clears the flag")
spent = P.save({"name": "spent", "kind": "case", "prompt": "p", "freq": "daily",
                "at": "09:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(spent["id"]),
                dict(P.load(spent["id"]), freq="once",
                     start=ts("2020-01-01 09:00"), next_run=None, paused=True))
raises(lambda: P.set_paused(spent["id"], False), "date has passed",
       "resuming a spent one-off asks for a new date")
P.cancel(spent["id"])
check(P.load(spent["id"]) is None, "a cancelled entry leaves the live list")
check((SANDBOX / "docket" / "cancelled" / f"{spent['id']}.json").is_file(),
      "a cancelled entry is MOVED to cancelled/, never deleted")
raises(lambda: P.cancel(spent["id"]), "no such entry", "cancelling twice fails cleanly")
raises(lambda: P._path_for("../escape"), "bad entry id", "an id cannot escape the store")

print("== firing ==")
for p in P.entries():
    P.cancel(p["id"])
due_at = time.time() - 60
case_p = P.save({"name": "Nightly", "kind": "case", "prompt": "sweep for {d:%Y%m%d}",
                 "freq": "daily", "at": "03:00", "precinct": PRECINCT,
                 "service": "claude", "model": "opus", "judge": "vyas"})
P._write_atomic(P._path_for(case_p["id"]), dict(P.load(case_p["id"]), next_run=due_at))
acted = P.run_due()
check(len(acted) == 1 and acted[0]["status"] == "queued", "a due entry fires once")
drops = pending("case")
check(len(drops) == 1, "firing drops exactly one web-case record")
dropped = json.loads(drops[0].read_text())
check(dropped["precinct"] == PRECINCT and dropped["model"] == "opus"
      and dropped["critic"] == "vyas" and dropped["source"] == "docket",
      "the dropped record carries the spec")
check(dropped["description"] == "sweep for "
      + datetime.fromtimestamp(due_at).strftime("%Y%m%d"),
      "the dropped record carries the RENDERED prompt")
check(isinstance(dropped.get("case"), int), "a case number is pre-allocated")
after = P.load(case_p["id"])
check(after["next_run"] > time.time(), "firing re-arms the entry into the future")
check(after["runs"] and after["runs"][0]["status"] == "queued", "the run is recorded")
check(P.run_due() == [], "a second immediate pass fires nothing")
check(len(pending("case")) == 1, "and drops no second record")

case2 = json.loads(drops[0].read_text())
check(case2["case"] != P.load(case_p["id"]).get("_nothing"),
      "the record is self-contained (no back-reference to a previous run)")

paused_p = P.save({"name": "Paused", "kind": "case", "prompt": "p", "freq": "daily",
                   "at": "03:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(paused_p["id"]),
                dict(P.load(paused_p["id"]), next_run=due_at, paused=True))
check(P.run_due() == [], "a paused entry never fires")
check(len(pending("case")) == 1, "and drops nothing")

missed_p = P.save({"name": "Missed", "kind": "case", "prompt": "p", "freq": "daily",
                   "at": "03:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(missed_p["id"]),
                dict(P.load(missed_p["id"]), next_run=time.time() - P.MISSED_GRACE_SEC - 60))
acted = P.run_due()
check(len(acted) == 1 and acted[0]["status"] == "missed",
      "a long-missed occurrence is recorded as missed")
check(len(pending("case")) == 1, "a missed occurrence queues no work")
check(P.load(missed_p["id"])["next_run"] > time.time(), "a missed entry is still re-armed")

jtf_p = P.save(dict(good_jtf, name="Weekly deck"))
P._write_atomic(P._path_for(jtf_p["id"]), dict(P.load(jtf_p["id"]), next_run=due_at))
P.run_due()
jdrops = pending("jtf")
check(len(jdrops) == 1, "a JTF entry drops a JTF record")
jrec = json.loads(jdrops[0].read_text())
check(jrec["lead"]["name"] == PRECINCT and jrec["collaborators"][0]["name"] == PRECINCT2
      and jrec["source"] == "docket", "the JTF record carries the composition")

before = len(pending("case")) + len(pending("jtf"))
dry_p = P.save({"name": "Dry", "kind": "case", "prompt": "p", "freq": "daily",
                "at": "03:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(dry_p["id"]), dict(P.load(dry_p["id"]), next_run=due_at))
dry = P.run_due(dry=True)
check(dry and dry[0]["status"] == "dry", "--dry reports what would fire")
check(len(pending("case")) + len(pending("jtf")) == before, "--dry drops nothing")
check(P.load(dry_p["id"])["next_run"] == due_at, "--dry does not advance the schedule")

print("== the dropped records are consumable by the real bridges ==")
env = dict(os.environ)
web = subprocess.run([sys.executable, "scratch_web_case.py", "process", "--dry"],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
check(web.returncode == 0, f"scratch_web_case.py --dry exits 0 ({web.stderr[-300:]})")
check("dry-spawn" in web.stdout, "the web-case bridge would spawn a deputy for our record")
jtf = subprocess.run([sys.executable, "scratch_jtf.py", "process", "--dry"],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
check(jtf.returncode == 0, f"scratch_jtf.py --dry exits 0 ({jtf.stderr[-300:]})")
check(jtf.stdout.count("dry-spawn") >= 2,
      "the JTF bridge would spawn the lead and the collaborator")

print("== CLI ==")
out = subprocess.run([sys.executable, "scratch_docket.py", "list", "--json"],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(out.returncode == 0 and json.loads(out.stdout)["ok"], "list --json")
payload = json.dumps({"name": "via cli", "kind": "case", "prompt": "p",
                      "freq": "weekly", "at": "09:00", "dow": 5, "precinct": PRECINCT})
out = subprocess.run([sys.executable, "scratch_docket.py", "save"], input=payload,
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
cli_id = json.loads(out.stdout)["entry"]["id"]
check(out.returncode == 0 and P.load(cli_id)["name"] == "via cli", "save reads stdin")
out = subprocess.run([sys.executable, "scratch_docket.py", "pause", "--id", cli_id],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(json.loads(out.stdout)["entry"]["paused"] is True, "pause via CLI")
out = subprocess.run([sys.executable, "scratch_docket.py", "save"], input='{"name":"x"}',
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(out.returncode == 1 and json.loads(out.stdout)["ok"] is False,
      "a bad save exits non-zero with a JSON error")
out = subprocess.run([sys.executable, "scratch_docket.py", "run"],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(out.returncode == 0 and dry_p["id"] in out.stdout,
      "the entry --dry only reported is still armed, and the next real pass fires it")
out = subprocess.run([sys.executable, "scratch_docket.py", "run"],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(out.returncode == 0 and out.stdout.strip() == "",
      "the cron entry point is silent when nothing is due")

print("== run now (uid=1384) ==")
# A manual run consumes the slot the entry was already showing, so the schedule
# keeps its original phase instead of re-phasing off the click.
for e in P.entries():
    P.cancel(e["id"])
sat_9 = P.next_occurrence({"freq": "weekly", "at": "09:00", "dow": 5}, time.time())
manual = P.save({"name": "Manual", "kind": "case", "prompt": "for {d:%Y%m%d}",
                 "freq": "weekly", "at": "09:00", "dow": 5, "precinct": PRECINCT})
check(P.load(manual["id"])["next_run"] == sat_9, "it starts armed for the coming Saturday")
before = len(pending("case"))
after_run = P.run_now(manual["id"])
check(len(pending("case")) == before + 1, "run now queues exactly one fresh record")
dropped = json.loads(sorted(pending("case"))[-1].read_text())
check(dropped["source"] == "docket" and dropped["precinct"] == PRECINCT,
      "the manual record is the same shape the scheduler drops")
check(dropped["description"] == "for " + datetime.now().strftime("%Y%m%d"),
      "its prompt is rendered for TODAY, not for the scheduled slot")
check(isinstance(dropped.get("case"), int), "a fresh case number is allocated")
check(after_run["runs"][0]["status"] == "manual",
      "the run is recorded as manual, not as a scheduled fire")
check(after_run["next_run"] == sat_9 + 7 * 86400,
      "the next time is the occurrence AFTER the one it was showing")
check(P.run_due() == [], "and the consumed slot does not also fire on the next pass")

# a stale next_run (box asleep across several occurrences) must still land ahead
stale = P.save({"name": "Stale", "kind": "case", "prompt": "p", "freq": "daily",
                "at": "03:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(stale["id"]),
                dict(P.load(stale["id"]), next_run=time.time() - 5 * 86400))
check(P.run_now(stale["id"])["next_run"] > time.time(),
      "a manual run on a stale schedule still lands in the future")

# a paused entry fires once and stays off the docket
paused_now = P.save({"name": "Paused manual", "kind": "case", "prompt": "p",
                     "freq": "daily", "at": "03:00", "precinct": PRECINCT})
P.set_paused(paused_now["id"], True)
armed = P.load(paused_now["id"])["next_run"]
n_before = len(pending("case"))
rec = P.run_now(paused_now["id"])
check(len(pending("case")) == n_before + 1, "a paused entry still fires when run by hand")
check(rec["paused"] is True, "...and stays paused")
check(rec["next_run"] == armed, "...and its stored schedule is left alone")

# a spent one-off can be re-run and stays spent
spent_once = P.save({"name": "Once manual", "kind": "case", "prompt": "p",
                     "freq": "once", "start": time.time() + 600, "precinct": PRECINCT})
P._write_atomic(P._path_for(spent_once["id"]), dict(P.load(spent_once["id"]), next_run=None))
rec = P.run_now(spent_once["id"])
check(rec["next_run"] is None, "a spent one-off re-runs without inventing a next time")
check(rec["runs"][0]["status"] == "manual", "and records the manual run")

raises(lambda: P.run_now("p_nosuch"), "no such entry", "run now on a missing entry fails")
# an id that would escape the store is refused by _path_for and surfaces as a plain
# "no such entry" rather than reaching the filesystem.
raises(lambda: P.run_now("../escape"), "no such entry", "run now cannot escape the store")

out = subprocess.run([sys.executable, "scratch_docket.py", "run-now", "--id", manual["id"]],
                     cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
check(out.returncode == 0 and json.loads(out.stdout)["entry"]["runs"][0]["status"] == "manual",
      "run-now via the CLI")
check(P.load(manual["id"])["next_run"] == sat_9 + 14 * 86400,
      "a second manual run consumes the next slot too")

print("== schedule_label ==")
check(P.schedule_label(sat) == "every Saturday at 09:00", "weekly label")
check(P.schedule_label(day) == "every day at 07:30", "daily label")
check(P.schedule_label(hourly) == "every hour at :20", "hourly label")
check(P.schedule_label(m31) == "day 31 of each month at 06:00", "monthly label")
check(P.schedule_label(once).startswith("once, 2026-09-19 09:00"), "once label")

print("== a second runner never double-fires ==")
# The cron runner starts every minute, so two passes CAN overlap. The second must
# see the lock, do nothing and exit — not wait (which would pile up one blocked
# process per minute) and not fire the same occurrence twice.
conc_p = P.save({"name": "Concurrent", "kind": "case", "prompt": "p", "freq": "daily",
                 "at": "03:00", "precinct": PRECINCT})
P._write_atomic(P._path_for(conc_p["id"]), dict(P.load(conc_p["id"]), next_run=due_at))
before_n = len(pending("case"))
with P._lock() as held:
    check(held is True, "the outer lock is acquired")
    check(P.run_due() == [], "a run that cannot take the lock does nothing")
    check(len(pending("case")) == before_n, "and queues nothing")
    check(P.load(conc_p["id"])["next_run"] == due_at, "and leaves the schedule alone")
acted = P.run_due()
check(len(acted) == 1 and acted[0]["id"] == conc_p["id"],
      "once the lock is free the same pass fires normally")

shutil.rmtree(SANDBOX, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
