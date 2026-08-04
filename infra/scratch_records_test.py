#!/usr/bin/env python3
"""Task 371 phase 1: tests for the records-manager (scratch_records.py).

Covers the full contract -- ledger {read/append/write, sheriff-only write},
log {read/append, no write}, the length helper, init-on-missing, field
sanitization -- plus the concurrency crux:

  * a 50-PROCESS append stress test: every appended line must land exactly once,
    none corrupted or interleaved (ledger + log);
  * concurrent READS during a burst of sheriff WRITES: every read must parse a
    COMPLETE file (never a torn/partial one);
  * a direct NON-BLOCKING + MUTUAL-EXCLUSION proof: a read returns immediately
    while a writer holds the exclusive lock, and a concurrent appender is made to
    wait for that same lock.

Self-contained runner (no pytest needed):  python scratch_records_test.py
All records live under a throwaway temp root (TSOMP_RECORDS_ROOT) so no real
department data is ever touched.
"""
import multiprocessing as _mp
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Isolate every test on a throwaway records root BEFORE importing the module.
_TMPDIR = os.environ.get("TSOMP_TEST_TMPDIR") or None
_TEST_ROOT = tempfile.mkdtemp(prefix="tsomp_records_test_", dir=_TMPDIR)
os.environ["TSOMP_RECORDS_ROOT"] = _TEST_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_records as rec  # noqa: E402

CTX = _mp.get_context("fork")  # true separate processes; real cross-process flock


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _data_lines(log_text):
    """Non-comment, non-empty lines of a case log."""
    return [ln for ln in log_text.splitlines() if ln and not ln.startswith("#")]


def _make_version(k, n_rows=5000):
    """A large, self-describing ledger doc: header + body + footer all tagged k."""
    rows = "\n".join(f"payload-{k}" for _ in range(n_rows))
    return f"VERSION={k}\n{rows}\nEND={k}\n"


def _validate_version(content):
    """True iff `content` is a COMPLETE single-version doc (no torn/partial read)."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) < 3:
        return False, f"too few lines ({len(lines)})"
    m = re.match(r"^VERSION=(\d+)$", lines[0])
    if not m:
        return False, f"bad header {lines[0]!r}"
    k = m.group(1)
    if lines[-1] != f"END={k}":
        return False, f"header/footer mismatch: {lines[0]!r} vs {lines[-1]!r} (torn read)"
    for ln in lines[1:-1]:
        if ln != f"payload-{k}":
            return False, f"torn body line {ln!r} (expected payload-{k})"
    return True, f"v{k} ({len(lines) - 2} rows)"


# ---- module-level workers (picklable; used by multiprocessing) -------------
def _append_worker(dept, proc_id, n, root):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    for i in range(n):
        rec.ledger_append(dept, f"LEDGER|proc={proc_id}|item={i}", role="deputy")
        rec.log_append(dept, task=f"{proc_id}-{i}", summary=f"LOG|proc={proc_id}|item={i}")


def _writer_worker(dept, seconds, root):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    end = time.time() + seconds
    k = 0
    while time.time() < end:
        rec.ledger_write(dept, _make_version(k), role="sheriff")
        k += 1


def _reader_worker(dept, seconds, root, q):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    reads, end = 0, time.time() + seconds
    while time.time() < end:
        content = rec.ledger_read(dept)          # NON-BLOCKING, no lock
        if content == "":
            continue
        ok, detail = _validate_version(content)
        if not ok:
            q.put(("FAIL", detail))
            return
        reads += 1
    q.put(("OK", reads))


def _lock_holder(dept, hold_s, root, ready_evt):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    target = rec._ledger_path(dept, create=True)
    with rec._exclusive_lock(target):
        ready_evt.set()
        time.sleep(hold_s)


def _append_once(dept, payload, root, done_q):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    rec.ledger_append(dept, payload, role="deputy")
    done_q.put(time.time())


def _register_worker(name, root):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    rec.directory_register(name, description=f"desc for {name}", ledger_mode="mutable")


# ---------------------------------------------------------------------------
# unit tests
# ---------------------------------------------------------------------------
def test_read_missing_returns_empty():
    assert rec.ledger_read("_test_missing") == ""
    assert rec.log_read("_test_missing") == ""


def test_ledger_append_and_read():
    d = "_test_led_ar"
    rec.ledger_append(d, "first case paragraph.", role="deputy")
    rec.ledger_append(d, "second case paragraph.", role="deputy")
    txt = rec.ledger_read(d)
    lines = [ln for ln in txt.splitlines() if ln]
    assert lines == ["first case paragraph.", "second case paragraph."], lines


def test_ledger_write_sheriff_ok():
    d = "_test_led_w"
    rec.ledger_append(d, "stale entry", role="deputy")
    rec.ledger_write(d, "COMPACTED LEDGER\nline2\n", role="sheriff")
    assert rec.ledger_read(d) == "COMPACTED LEDGER\nline2\n"


def test_ledger_write_deputy_refused():
    d = "_test_led_perm"
    rec.ledger_append(d, "keep me", role="deputy")
    before = rec.ledger_read(d)
    for bad_role in ("deputy", "receptionist", ""):
        try:
            rec.ledger_write(d, "HIJACK", role=bad_role)
        except PermissionError:
            pass
        else:
            raise AssertionError(f"ledger_write allowed role={bad_role!r}")
    assert rec.ledger_read(d) == before, "refused write must not change content"


def test_ledger_length():
    d = "_test_len"
    rec.ledger_write(d, "abcd" * 25, role="sheriff")   # 100 chars
    info = rec.ledger_length(d)
    assert info["chars"] == 100, info
    assert info["approx_tokens"] == 25, info


def test_log_append_and_format():
    d = "_test_log_fmt"
    rec.log_append(d, task=371, summary="built the records manager",
                   case_file_path="scratch_full_logs/inbox/task_371.md")
    rec.log_append(d, task=372, summary="second case")
    txt = rec.log_read(d)
    assert txt.startswith("# case_log dept=_test_log_fmt"), txt[:60]
    assert "case_file_folder=" in txt
    data = _data_lines(txt)
    assert len(data) == 2, data
    f0 = data[0].split("\t")
    assert f0 == ["371", "scratch_full_logs/inbox/task_371.md",
                  "built the records manager"], f0
    # default case-file path is derived from the folder + task number
    assert data[1].split("\t")[1] == "scratch_full_logs/inbox/task_372.md"


def test_log_has_no_write():
    assert not hasattr(rec, "log_write"), "the case log must have NO write method"


def test_log_append_with_deputy_four_fields():
    """Task 377 #1: a deputy-carrying append is a 4-field row with the deputy in
    field 1 (right after the task number)."""
    d = "_t377_dep"
    rec.log_append(d, task=377, deputy="case_ui",
                   summary="added the deputy column",
                   case_file_path="scratch_full_logs/records/infra/cases/task_377.md")
    data = _data_lines(rec.log_read(d))
    assert len(data) == 1, data
    f = data[0].split("\t")
    assert len(f) == 4, f"expected 4 fields, got {len(f)}: {f}"
    assert f == ["377", "case_ui",
                 "scratch_full_logs/records/infra/cases/task_377.md",
                 "added the deputy column"], f


def test_log_append_without_deputy_is_backward_compatible_three_fields():
    """Task 377 #1: an append with NO deputy stays the ORIGINAL 3-field row
    (byte-identical to pre-377) so old readers + old data keep working."""
    d = "_t377_nodep"
    rec.log_append(d, task=100, summary="no deputy here",
                   case_file_path="scratch_full_logs/inbox/task_100.md")
    data = _data_lines(rec.log_read(d))
    assert len(data) == 1, data
    f = data[0].split("\t")
    assert len(f) == 3, f"deputy-less row must stay 3 fields, got {f}"
    assert f == ["100", "scratch_full_logs/inbox/task_100.md", "no deputy here"], f


def test_log_deputy_is_sanitized():
    """A deputy with whitespace/tabs cannot break the single-line 4-field shape."""
    d = "_t377_depsan"
    rec.log_append(d, task=5, deputy="weird\tname\nhere", summary="s")
    data = _data_lines(rec.log_read(d))
    assert len(data) == 1
    assert data[0].count("\t") == 3, f"exactly 4 fields expected: {data[0]!r}"
    assert data[0].split("\t")[1] == "weird name here"


def test_log_remove_sheriff_only():
    """Task 377 (Feng uid=380): log_remove is sheriff-only; every other role is
    refused and the log is unchanged."""
    d = "_t377_rm_perm"
    rec.log_append(d, task=900000, deputy="web_900000", summary="test entry")
    before = rec.log_read(d)
    for bad in ("deputy", "receptionist", ""):
        try:
            rec.log_remove(d, 900000, role=bad)
        except PermissionError:
            pass
        else:
            raise AssertionError(f"log_remove allowed role={bad!r}")
    assert rec.log_read(d) == before, "refused remove must not change the log"


def test_log_remove_surgical_and_preserves_others():
    """The sheriff can retract exactly one task's line(s); all other rows + the
    header survive verbatim."""
    d = "_t377_rm"
    rec.log_append(d, task=377, deputy="case_ui", summary="real case")
    rec.log_append(d, task=900000, deputy="web_900000", summary="self-test to retract")
    rec.log_append(d, task=381, summary="another real case")   # 3-field (no deputy)
    n = rec.log_remove(d, 900000, role="sheriff")
    assert n == 1, n
    data = _data_lines(rec.log_read(d))
    tasks = [ln.split("\t")[0] for ln in data]
    assert tasks == ["377", "381"], tasks
    assert "900000" not in rec.log_read(d)
    # header intact + remaining rows unchanged (377 still 4-field, 381 still 3-field)
    assert rec.log_read(d).startswith("# case_log dept=" + d)
    assert data[0].split("\t") == ["377", "case_ui", "scratch_full_logs/inbox/task_377.md", "real case"]
    assert data[1].split("\t") == ["381", "scratch_full_logs/inbox/task_381.md", "another real case"]


def test_log_remove_absent_task_is_noop():
    d = "_t377_rm_absent"
    rec.log_append(d, task=1, summary="keep")
    assert rec.log_remove(d, 999, role="sheriff") == 0
    assert len(_data_lines(rec.log_read(d))) == 1


def test_alphanumeric_subtask_case_ids():
    """Task 384: sub-deputy subtask ids like 200a / 200b (a subtask of case 200) are
    first-class case numbers in the log — append, exact per-id remove, and the parent
    id is untouched."""
    d = "_t384_sub"
    rec.log_append(d, task="200", deputy="parent", summary="the big case")
    rec.log_append(d, task="200a", deputy="sub_a", summary="subtask a")
    rec.log_append(d, task="200b", deputy="sub_b", summary="subtask b")
    ids = [ln.split("\t")[0] for ln in _data_lines(rec.log_read(d))]
    assert ids == ["200", "200a", "200b"], ids
    # removing 200a must NOT touch 200 or 200b (exact first-field match, not prefix)
    assert rec.log_remove(d, "200a", role="sheriff") == 1
    ids = [ln.split("\t")[0] for ln in _data_lines(rec.log_read(d))]
    assert ids == ["200", "200b"], ids


def test_log_mixed_old_and_new_rows_coexist():
    """Old 3-field rows and new 4-field rows can sit in the same log and both are
    well-formed lines (the reader disambiguates by field count)."""
    d = "_t377_mixed"
    rec.log_append(d, task=1, summary="old style")                       # 3 fields
    rec.log_append(d, task=2, deputy="bob", summary="new style")         # 4 fields
    data = _data_lines(rec.log_read(d))
    assert [ln.count("\t") for ln in data] == [2, 3], data
    assert data[0].split("\t") == ["1", "scratch_full_logs/inbox/task_1.md", "old style"]
    assert data[1].split("\t") == ["2", "bob", "scratch_full_logs/inbox/task_2.md", "new style"]


def test_log_sanitizes_tabs_and_newlines():
    d = "_test_log_san"
    rec.log_append(d, task="9\t9", summary="line one\nline two\twith tab")
    data = _data_lines(rec.log_read(d))
    assert len(data) == 1
    assert data[0].count("\t") == 2, f"exactly 3 fields expected: {data[0]!r}"
    assert data[0].split("\t")[2] == "line one line two with tab"


def test_init_on_missing_dept():
    d = "_test_init"
    assert not (Path(_TEST_ROOT) / d).exists()
    rec.log_append(d, task=1, summary="creates dir + header on first use")
    assert (Path(_TEST_ROOT) / d / "log.tsv").exists()
    rec.ledger_append(d, "creates ledger too")
    assert (Path(_TEST_ROOT) / d / "ledger.md").exists()


def test_bad_dept_name_rejected():
    for bad in ("../escape", "a/b", ".", "..", ""):
        try:
            rec.ledger_read(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"dept {bad!r} should be rejected")


# ---------------------------------------------------------------------------
# concurrency stress: 50 processes appending; every line lands exactly once
# ---------------------------------------------------------------------------
def test_concurrency_append_stress():
    d = "_test_stress"
    n_procs, n_items = 50, 20
    procs = [CTX.Process(target=_append_worker, args=(d, p, n_items, _TEST_ROOT))
             for p in range(n_procs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]

    total = n_procs * n_items

    # ---- ledger: every payload present exactly once, each line well-formed ----
    led_lines = [ln for ln in rec.ledger_read(d).splitlines() if ln]
    expected = {f"LEDGER|proc={p}|item={i}" for p in range(n_procs) for i in range(n_items)}
    assert all(re.fullmatch(r"LEDGER\|proc=\d+\|item=\d+", ln) for ln in led_lines), \
        "a ledger line was corrupted/interleaved"
    assert len(led_lines) == total, f"ledger: {len(led_lines)} lines, expected {total}"
    assert set(led_lines) == expected, "ledger: missing/duplicate payloads"

    # ---- log: every line has exactly 3 fields; every payload present once ----
    log_data = _data_lines(rec.log_read(d))
    assert len(log_data) == total, f"log: {len(log_data)} lines, expected {total}"
    for ln in log_data:
        assert ln.count("\t") == 2, f"log line not 3 fields (interleaved?): {ln!r}"
    got = {tuple(ln.split("\t")[i] for i in (0, 2)) for ln in log_data}
    want = {(f"{p}-{i}", f"LOG|proc={p}|item={i}")
            for p in range(n_procs) for i in range(n_items)}
    assert got == want, "log: missing/duplicate payloads"
    print(f"    [stress] {len(led_lines)}/{total} ledger + {len(log_data)}/{total} log "
          f"lines from {n_procs} procs, all unique & well-formed")


# ---------------------------------------------------------------------------
# concurrent reads during a burst of sheriff writes: never a torn read
# ---------------------------------------------------------------------------
def test_concurrent_reads_during_writes():
    d = "_test_rw"
    rec.ledger_write(d, _make_version(0), role="sheriff")  # seed a complete file
    dur = 1.5
    q = CTX.Queue()
    writers = [CTX.Process(target=_writer_worker, args=(d, dur, _TEST_ROOT)) for _ in range(2)]
    readers = [CTX.Process(target=_reader_worker, args=(d, dur, _TEST_ROOT, q)) for _ in range(4)]
    for p in writers + readers:
        p.start()
    for p in writers + readers:
        p.join(60)

    results = [q.get(timeout=5) for _ in readers]
    fails = [detail for status, detail in results if status != "OK"]
    assert not fails, f"torn/partial read(s): {fails}"
    total_reads = sum(n for status, n in results if status == "OK")
    assert total_reads > 0, "readers observed nothing"
    assert all(p.exitcode == 0 for p in writers + readers)
    print(f"    [read/write] {total_reads} concurrent reads during writer burst, "
          f"all complete (0 torn)")


# ---------------------------------------------------------------------------
# direct proof: reads DON'T block on the writer lock; appenders DO
# ---------------------------------------------------------------------------
def test_read_nonblocking_append_blocks_under_held_lock():
    d = "_test_nb"
    known = "KNOWN-LEDGER-CONTENT\n"
    rec.ledger_write(d, known, role="sheriff")

    hold_s = 2.0
    ready = CTX.Event()
    holder = CTX.Process(target=_lock_holder, args=(d, hold_s, _TEST_ROOT, ready))
    holder.start()
    assert ready.wait(10), "lock holder never acquired"
    t_lock = time.time()

    # (1) a READ returns the complete file immediately, without waiting for the lock
    t0 = time.time()
    content = rec.ledger_read(d)
    dt = time.time() - t0
    assert content == known, f"read got {content!r}"
    assert dt < 0.5, f"read blocked on the writer lock ({dt:.2f}s)"

    # (2) an APPEND must wait for the held lock, i.e. not finish while it is held
    done_q = CTX.Queue()
    appender = CTX.Process(target=_append_once, args=(d, "AFTER-LOCK", _TEST_ROOT, done_q))
    appender.start()
    time.sleep(0.7)  # well before the 2.0s release
    assert appender.is_alive(), "appender did NOT block on the held exclusive lock"

    holder.join(10)
    appender.join(10)
    finished = done_q.get(timeout=5)
    assert finished - t_lock >= hold_s - 0.4, "append finished before the lock was released"
    assert "AFTER-LOCK" in rec.ledger_read(d)
    print(f"    [non-block] read returned in {dt*1000:.0f}ms under a held lock; "
          f"append serialized behind it")


# ---------------------------------------------------------------------------
# Task 372: fixed-ledger mode, ledger_author, the global precinct directory,
# new-precinct registration, and the receptionist's log.
# ---------------------------------------------------------------------------
def test_directory_register_and_read():
    rec.directory_register("_t372_eval", description="eval + baselines + datasets",
                           ledger_mode="mutable")
    d = rec.directory_read()
    assert "_t372_eval" in d["precincts"], d
    e = d["precincts"]["_t372_eval"]
    assert e["ledger_mode"] == "mutable", e
    assert e["ledger"] == "_t372_eval/ledger.md", e
    assert e["log"] == "_t372_eval/log.tsv", e
    assert e["cases"] == "_t372_eval/cases", e
    # the precinct dir + its cases/ folder are created on register
    assert (Path(_TEST_ROOT) / "_t372_eval" / "cases").is_dir()
    # machine directory + human render both exist and mention the precinct
    assert (Path(_TEST_ROOT) / "precincts.json").exists()
    md = rec._read_text(rec._directory_md_path())
    assert "Precinct directory" in md and "_t372_eval" in md, md[:120]


def test_precinct_mode_default_and_fixed():
    # UNREGISTERED precinct is mutable -> phase-1 behaviour is unchanged
    assert rec.precinct_mode("_t372_never_registered") == "mutable"
    rec.directory_register("_t372_fixmode", ledger_mode="fixed")
    assert rec.precinct_mode("_t372_fixmode") == "fixed"


def test_fixed_ledger_refuses_append_and_write():
    d = "_t372_recept"
    rec.directory_register(d, ledger_mode="fixed", description="front desk")
    rec.ledger_author(d, "FIXED receptionist ledger: job + system.\n")
    for bad in ("append", "sheriff-write"):
        try:
            if bad == "append":
                rec.ledger_append(d, "should not land")
            else:
                rec.ledger_write(d, "hijack", role="sheriff")
        except PermissionError:
            pass
        else:
            raise AssertionError(f"fixed ledger {bad} was NOT refused")
    # reads always work; the fixed content is untouched by the refused writes
    assert rec.ledger_read(d) == "FIXED receptionist ledger: job + system.\n"


def test_ledger_author_write_once():
    d = "_t372_author"
    rec.directory_register(d, ledger_mode="fixed")
    rec.ledger_author(d, "v1\n")
    try:
        rec.ledger_author(d, "v2\n")            # already authored, no force
    except PermissionError:
        pass
    else:
        raise AssertionError("re-author without force should be refused")
    assert rec.ledger_read(d) == "v1\n", "content changed by a refused re-author"
    rec.ledger_author(d, "v2\n", force=True)    # explicit overwrite allowed
    assert rec.ledger_read(d) == "v2\n"


def test_fixed_precinct_log_still_works():
    # the fixed-ledger rule constrains ONLY the ledger; the log is normal.
    d = "_t372_recept_log"
    rec.directory_register(d, ledger_mode="fixed")
    rec.log_append(d, task=1, summary="receptionist opened a case")
    rec.log_append(d, task=2, summary="another case")
    data = _data_lines(rec.log_read(d))
    assert len(data) == 2, data
    assert data[0].split("\t")[2] == "receptionist opened a case"


def test_directory_register_idempotent_preserves_records():
    d = "_t372_idem"
    rec.directory_register(d, description="first", ledger_mode="mutable")
    rec.ledger_append(d, "important case paragraph")
    rec.log_append(d, task=7, summary="a case")
    # re-register updates description + mode but must not disturb the records
    rec.directory_register(d, description="second", ledger_mode="fixed")
    e = rec.directory_read()["precincts"][d]
    assert e["description"] == "second" and e["ledger_mode"] == "fixed", e
    assert "important case paragraph" in rec.ledger_read(d), "ledger clobbered by re-register"
    assert len(_data_lines(rec.log_read(d))) == 1, "log clobbered by re-register"


# ---------------------------------------------------------------------------
# Task 376: per-precinct DEFAULT MODEL (directory `model` field + precinct_model).
# ---------------------------------------------------------------------------
def test_precinct_model_default_and_set():
    # unregistered precinct -> DEFAULT_MODEL (opus); phase-1 behaviour unchanged
    assert rec.precinct_model("_t376_never") == "opus"
    assert rec.precinct_model("_t376_never") == rec.DEFAULT_MODEL
    # registered WITHOUT a model -> still the default, and no stray key stored
    rec.directory_register("_t376_nomodel", description="d")
    assert rec.precinct_model("_t376_nomodel") == "opus"
    assert "model" not in rec.directory_read()["precincts"]["_t376_nomodel"]
    # registered WITH a model -> that model, stored in the entry + rendered md
    rec.directory_register("_t376_paper", description="d", model="fable")
    assert rec.precinct_model("_t376_paper") == "fable"
    assert rec.directory_read()["precincts"]["_t376_paper"]["model"] == "fable"
    md = rec._read_text(rec._directory_md_path())
    assert "Model" in md and "fable" in md, md[:200]


def test_directory_register_model_validated():
    for bad in ("gpt4", "opus4", "", "Fable"):     # exact lowercase names only
        try:
            rec.directory_register("_t376_badmodel", model=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"model {bad!r} should be rejected")
    # a rejected model must not have created the precinct
    assert "_t376_badmodel" not in rec.directory_read()["precincts"]
    rec.directory_register("_t376_okmodel", model="sonnet")
    assert rec.precinct_model("_t376_okmodel") == "sonnet"


def test_directory_register_model_preserved_and_updatable():
    d = "_t376_upd"
    rec.directory_register(d, description="first", model="opus")
    rec.ledger_append(d, "a case paragraph")
    # re-register WITHOUT a model preserves the stored model AND the records
    rec.directory_register(d, description="second")
    e = rec.directory_read()["precincts"][d]
    assert e["model"] == "opus", "model dropped by a model-less re-register"
    assert e["description"] == "second"
    assert "a case paragraph" in rec.ledger_read(d), "records clobbered"
    # re-register WITH a new model updates it
    rec.directory_register(d, model="haiku")
    assert rec.precinct_model(d) == "haiku"


def test_sheriff_model_default_and_set():
    # Task 384b / Phase C: the GLOBAL sheriff model. Unset config -> DEFAULT_SHERIFF_MODEL
    # (fable, distinct from the per-precinct DEFAULT_MODEL=opus).
    assert rec.sheriff_config_read() == {}
    assert rec.sheriff_model_get() == "fable"
    assert rec.sheriff_model_get() == rec.DEFAULT_SHERIFF_MODEL
    # set -> persisted in sheriff_config.json + read back
    ret = rec.sheriff_model_set("opus", role="sheriff")
    assert ret["model"] == "opus"
    assert rec.sheriff_model_get() == "opus"
    assert rec.sheriff_config_read()["model"] == "opus"
    assert '"model": "opus"' in rec._read_text(rec._sheriff_config_path())
    # update to another valid model
    rec.sheriff_model_set("sonnet", role="sheriff")
    assert rec.sheriff_model_get() == "sonnet"


def test_sheriff_model_sheriff_only_and_validated():
    # SHERIFF-ONLY: a non-sheriff role is refused (like ledger_write / log_remove)
    for role in ("deputy", "receptionist", ""):
        try:
            rec.sheriff_model_set("opus", role=role)
        except PermissionError:
            pass
        else:
            raise AssertionError(f"role {role!r} should be refused")
    # VALIDATED against _MODELS (exact lowercase names only)
    for bad in ("gpt4", "Opus", "", "fable5"):
        try:
            rec.sheriff_model_set(bad, role="sheriff")
        except ValueError:
            pass
        else:
            raise AssertionError(f"model {bad!r} should be rejected")


def test_sheriff_config_read_robust_to_garbage():
    # a corrupt config file -> read returns {} and get falls back to fable (never raises)
    rec._sheriff_config_path().parent.mkdir(parents=True, exist_ok=True)
    rec._sheriff_config_path().write_text("{ not json ]")
    assert rec.sheriff_config_read() == {}
    assert rec.sheriff_model_get() == "fable"
    rec._sheriff_config_path().unlink(missing_ok=True)      # restore clean state


def test_concurrency_directory_register():
    """N processes registering DISTINCT precincts concurrently: every entry must
    land exactly once and precincts.json must stay valid JSON (no lost update)."""
    n = 30
    names = [f"_t372_conc{i}" for i in range(n)]
    procs = [CTX.Process(target=_register_worker, args=(nm, _TEST_ROOT)) for nm in names]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    got = set(rec.directory_read()["precincts"].keys())
    missing = [nm for nm in names if nm not in got]
    assert not missing, f"lost concurrent registrations: {missing}"
    # the file on disk still parses as JSON (no torn/interleaved write)
    import json as _json
    parsed = _json.loads(rec._read_text(rec._directory_json_path()))
    assert len(parsed["precincts"]) >= n
    print(f"    [dir-stress] {len(names)} concurrent registers all landed; "
          f"precincts.json valid ({len(parsed['precincts'])} total)")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase D (Case 384d): precinct soft-delete / restore / purge + directory filter
# ---------------------------------------------------------------------------
def test_soft_delete_moves_to_trash_and_hides_from_active():
    d = "_384d_del"
    rec.directory_register(d, description="to be deleted", ledger_mode="mutable")
    rec.ledger_append(d, "a real case paragraph", role="deputy")
    rec.log_append(d, "10", "closed a case", deputy="depX")
    assert d in rec.directory_read()["precincts"]
    entry = rec.precinct_soft_delete(d, role="sheriff")
    assert entry["status"] == "deleted" and entry["trash"].startswith(".trash/")
    assert entry["purge_after"] > entry["deleted_ts"]
    # dropped from the active directory + render, present with include_deleted
    assert d not in rec.directory_read()["precincts"], "deleted precinct hidden from active list"
    assert d in rec.directory_read(include_deleted=True)["precincts"]
    assert d not in rec._read_text(rec._directory_md_path()), "human render omits deleted precinct"
    assert rec.precinct_status(d) == "deleted"
    # records physically moved to trash (not lost): the ledger travels along
    trash = Path(_TEST_ROOT) / rec.TRASH_DIRNAME
    hits = [p for p in trash.iterdir() if p.name.startswith(d + "@")]
    assert hits and (hits[0] / "ledger.md").exists()
    assert not (Path(_TEST_ROOT) / d).exists(), "the live precinct dir is gone (moved to trash)"


def test_soft_delete_is_sheriff_only_and_idempotent():
    d = "_384d_perm"
    rec.directory_register(d, ledger_mode="mutable")
    try:
        rec.precinct_soft_delete(d, role="deputy")
        raise AssertionError("soft-delete must be sheriff-only")
    except PermissionError:
        pass
    rec.precinct_soft_delete(d, role="sheriff")
    # idempotent: deleting an already-deleted precinct is a no-op that returns the entry
    again = rec.precinct_soft_delete(d, role="sheriff")
    assert again["status"] == "deleted"


def test_soft_delete_unknown_and_fixed_refused():
    try:
        rec.precinct_soft_delete("_384d_nope", role="sheriff")
        raise AssertionError("deleting an unknown precinct must raise")
    except ValueError:
        pass
    f = "_384d_fixed"
    rec.directory_register(f, ledger_mode="fixed", description="front desk")
    try:
        rec.precinct_soft_delete(f, role="sheriff")
        raise AssertionError("a fixed-ledger (receptionist) precinct must never be deletable")
    except ValueError:
        pass


def test_restore_brings_precinct_back():
    d = "_384d_restore"
    rec.directory_register(d, description="restore me", ledger_mode="mutable")
    rec.ledger_append(d, "case content to preserve", role="deputy")
    rec.precinct_soft_delete(d, role="sheriff")
    assert rec.precinct_status(d) == "deleted"
    entry = rec.precinct_restore(d, role="sheriff")
    assert "status" not in entry and "trash" not in entry
    assert rec.precinct_status(d) == "active"
    assert d in rec.directory_read()["precincts"]
    assert rec.ledger_read(d) == "case content to preserve\n", "records intact after restore"
    # restoring a non-deleted precinct is refused
    try:
        rec.precinct_restore(d, role="sheriff")
        raise AssertionError("restoring a non-deleted precinct must raise")
    except ValueError:
        pass


def test_purge_hard_deletes_only_after_soft_delete():
    d = "_384d_purge"
    rec.directory_register(d, ledger_mode="mutable")
    # purge of an ACTIVE precinct is refused (never a shortcut around soft-delete)
    try:
        rec.precinct_purge(d, role="sheriff")
        raise AssertionError("purging a non-deleted precinct must raise")
    except ValueError:
        pass
    entry = rec.precinct_soft_delete(d, role="sheriff")
    trash_dir = Path(_TEST_ROOT) / entry["trash"]
    assert trash_dir.exists()
    res = rec.precinct_purge(d, role="sheriff")
    assert res["purged"] == d
    assert not trash_dir.exists(), "trash hard-deleted on purge"
    assert rec.precinct_status(d) is None, "purge removes the directory entry entirely"
    assert d not in rec.directory_read(include_deleted=True)["precincts"]


def test_pending_purge_respects_purge_after():
    d = "_384d_pp"
    rec.directory_register(d, ledger_mode="mutable")
    rec.precinct_soft_delete(d, role="sheriff", retention_days=14)   # purge_after ~ +14d
    names_now = [n for n, _ in rec.precincts_pending_purge()]
    assert d not in names_now, "a fresh 14-day trash is NOT yet due for purge"
    # a far-future 'now' makes it due
    future = __import__("time").time() + 15 * 86400
    names_future = [n for n, _ in rec.precincts_pending_purge(now=future)]
    assert d in names_future, "past its purge_after it is due"
    # retention_days=0 is immediately due
    d2 = "_384d_pp0"
    rec.directory_register(d2, ledger_mode="mutable")
    rec.precinct_soft_delete(d2, role="sheriff", retention_days=0)
    assert d2 in [n for n, _ in rec.precincts_pending_purge()]


def test_register_refuses_to_reactivate_a_soft_deleted_precinct():
    # re-registering a soft-deleted precinct is refused (would orphan trashed records);
    # the caller must restore or purge first. After restore, register works again.
    d = "_384d_reactivate"
    rec.directory_register(d, ledger_mode="mutable")
    rec.precinct_soft_delete(d, role="sheriff")
    assert rec.precinct_status(d) == "deleted"
    try:
        rec.directory_register(d, description="back again", role="sheriff")
        raise AssertionError("re-registering a soft-deleted precinct must be refused")
    except ValueError:
        pass
    # no empty active dir was created behind the refusal
    assert not (Path(_TEST_ROOT) / d).exists(), "refused register must not create a dir"
    rec.precinct_restore(d, role="sheriff")
    rec.directory_register(d, description="back again", role="sheriff")   # now OK
    assert d in rec.directory_read()["precincts"]


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_records test suite  ({len(tests)} tests) ===")
    print(f"    records root (throwaway): {_TEST_ROOT}")
    passed, failed = 0, []
    t0 = time.time()
    for t in tests:
        try:
            t()
        except Exception:
            failed.append(t.__name__)
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"[PASS] {t.__name__}")
    dt = time.time() - t0
    print(f"=== SUMMARY: {passed} passed, {len(failed)} failed in {dt:.1f}s ===")
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = _run()
    finally:
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)  # never touches real dept data
    sys.exit(rc)
