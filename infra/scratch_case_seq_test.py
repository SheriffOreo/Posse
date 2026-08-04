#!/usr/bin/env python3
"""Task 377 #1: tests for the case-number allocator (scratch_case_seq.py).

Covers: base/namespacing, monotonic single + batch allocation, peek is
side-effect-free, persistence across processes, no-collision-with-a-uid-set, and
the concurrency crux (N processes each allocating: every number handed out exactly
once, none lost or duplicated). Self-contained (no pytest); every allocation lives
under a throwaway TSOMP_RECORDS_ROOT.  Run:  python scratch_case_seq_test.py
"""
import contextlib
import json
import multiprocessing as _mp
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

_TEST_ROOT = tempfile.mkdtemp(prefix="tsomp_caseseq_test_")
os.environ["TSOMP_RECORDS_ROOT"] = _TEST_ROOT
os.environ["TSOMP_CASE_BASE"] = "900000"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch_case_seq as cs  # noqa: E402

CTX = _mp.get_context("fork")


def _alloc_worker(root, base, n, q):
    os.environ["TSOMP_RECORDS_ROOT"] = root
    os.environ["TSOMP_CASE_BASE"] = str(base)
    got = [cs.allocate(1) for _ in range(n)]
    q.put(got)


def test_peek_is_base_before_any_allocation():
    # own pristine root so the assertion is order-independent (the module-level
    # root is shared and other tests allocate against it).
    sub = tempfile.mkdtemp(prefix="tsomp_caseseq_peek_", dir=_TEST_ROOT)
    old = os.environ["TSOMP_RECORDS_ROOT"]
    os.environ["TSOMP_RECORDS_ROOT"] = sub
    try:
        assert cs.peek() == 900000, cs.peek()
        assert cs.peek() == 900000        # peek must NOT consume
        assert cs.allocate() == 900000    # first allocation is the base
        assert cs.peek() == 900001
    finally:
        os.environ["TSOMP_RECORDS_ROOT"] = old


def test_allocate_is_monotonic_and_in_band():
    a = cs.allocate()
    b = cs.allocate()
    c = cs.allocate()
    assert a >= 900000 and b == a + 1 and c == b + 1, (a, b, c)
    assert cs.peek() == c + 1, "peek must be the next unallocated number"


def test_allocate_batch():
    start = cs.peek()
    nums = cs.allocate(5)
    assert nums == list(range(start, start + 5)), nums
    assert cs.peek() == start + 5


def test_allocate_never_collides_with_email_uids():
    """Allocated numbers sit in the high band, disjoint from any realistic Gmail
    uid set (the whole point of the namespacing)."""
    email_uids = set(range(1, 2000))          # today's uids are ~[100, 400]
    minted = {cs.allocate() for _ in range(200)}
    assert not (minted & email_uids), "allocated case number collided with an email uid"
    assert min(minted) >= 900000


def test_persistence_across_reads():
    n = cs.allocate()
    # a fresh read of the on-disk counter must reflect the allocation
    assert cs.peek() == n + 1
    assert (Path(_TEST_ROOT) / "case_seq.json").exists()


def test_concurrency_no_duplicates_or_gaps():
    """30 processes each allocating 20 numbers: the 600 numbers handed out must be
    exactly the contiguous range [start, start+600) — every number once, none lost."""
    start = cs.peek()
    n_procs, per = 30, 20
    q = CTX.Queue()
    procs = [CTX.Process(target=_alloc_worker, args=(_TEST_ROOT, 900000, per, q))
             for _ in range(n_procs)]
    for p in procs:
        p.start()
    allnums = []
    for _ in procs:
        allnums.extend(q.get(timeout=60))
    for p in procs:
        p.join(60)
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    total = n_procs * per
    assert len(allnums) == total, f"{len(allnums)} allocations, expected {total}"
    assert len(set(allnums)) == total, "a case number was handed out more than once"
    assert set(allnums) == set(range(start, start + total)), "gap/overlap in allocated range"
    print(f"    [caseseq-stress] {total} concurrent allocations, all unique & contiguous "
          f"[{start}, {start + total})")


# ============================================================================
# Case 391a: ONE continuous sequence (retire the 900000 band). These tests are
# fully ISOLATED — each sets up its own throwaway records+inbox root and
# save/restores TSOMP_RECORDS_ROOT / TSOMP_INBOX_ROOT / TSOMP_CASE_BASE — so they
# never disturb the module-level band-override tests above (which pin base=900000).
# ============================================================================
@contextlib.contextmanager
def _iso_root(case_base=None, seq=None, logs=None, mapkeys=None, specs=None):
    """Isolated throwaway records+inbox root with optional fixtures + env save/restore.
    `logs`={dept:[case#,...]}, `mapkeys`=[case#,...], `specs`=[spec-stem,...],
    `seq`=case_seq.json {"next": seq}. case_base=None UNSETS TSOMP_CASE_BASE."""
    saved = {k: os.environ.get(k) for k in
             ("TSOMP_RECORDS_ROOT", "TSOMP_INBOX_ROOT", "TSOMP_CASE_BASE")}
    r = tempfile.mkdtemp(prefix="tsomp_caseseq_iso_")
    i = tempfile.mkdtemp(prefix="tsomp_caseseq_isoin_")
    try:
        os.environ["TSOMP_RECORDS_ROOT"] = r
        os.environ["TSOMP_INBOX_ROOT"] = i
        if case_base is None:
            os.environ.pop("TSOMP_CASE_BASE", None)
        else:
            os.environ["TSOMP_CASE_BASE"] = str(case_base)
        rp, ip = Path(r), Path(i)
        for dept, rows in (logs or {}).items():
            (rp / dept).mkdir(parents=True, exist_ok=True)
            (rp / dept / "log.tsv").write_text(
                "# case_log dept=%s\n" % dept
                + "".join(f"{t}\tpath\tsummary\n" for t in rows))
        if mapkeys is not None:
            (rp / "task_precinct.json").write_text(
                json.dumps({str(k): {"precinct": "infra"} for k in mapkeys}))
        for s in (specs or []):
            (ip / f"task_{s}.md").write_text("x")
        if seq is not None:
            (rp / "case_seq.json").write_text(json.dumps({"next": seq}))
        yield rp, ip
    finally:
        shutil.rmtree(r, ignore_errors=True)
        shutil.rmtree(i, ignore_errors=True)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# fixtures mirroring today's live shape: logs stop at 384 (only CLOSED cases are
# logged), the map+inbox carry the in-flight 390/391 (+ alphanumeric sub-cases),
# and 900000 (retired band) + 384e/391a (alphanumeric) must all be EXCLUDED.
_FIX_LOGS = {"eval": [300, 347, "# junk"], "infra": [384, "384e", 900000, "papertest"]}
_FIX_MAP = [390, 391, "391a", 900000, "draft_review387"]
_FIX_SPECS = [390, 391, "391a", "391b", 900000, "132_check", "52_rtf_time"]


def test_compute_max_real_case_excludes_band_and_alnum():
    with _iso_root(logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS):
        mx = cs.compute_max_real_case()
        assert mx == 391, f"max real case should be 391 (got {mx}); band/alnum must be excluded"


def test_reseed_retires_900000_band():
    # a legacy 900000-band counter is retired IN PLACE to max_real+1 (continuous).
    with _iso_root(seq=900005, logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS):
        new = cs.reseed()
        assert new == 392, new
        assert json.loads((_iso_seq()).read_text())["next"] == 392
        # and the very next allocation is 392, then 393 — NO 900000 jump.
        assert cs.allocate() == 392
        assert cs.allocate() == 393
        assert cs.peek() == 394


def test_reseed_on_missing_file_seeds_from_scan():
    with _iso_root(logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS):
        assert not _iso_seq().exists()
        assert cs.reseed() == 392
        assert json.loads(_iso_seq().read_text())["next"] == 392


def test_reseed_never_lowers_a_continuous_counter():
    # an already-continuous counter (< 900000) that is AHEAD of the scan must be
    # left untouched, else a reseed would re-hand-out live numbers.
    with _iso_root(seq=450, logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS):
        assert cs.reseed() == 450, "reseed must not lower a live continuous counter"
        assert cs.allocate() == 450
        # --force recomputes from the scan (max_real+1 = 392).
        assert cs.reseed(force=True) == 392


def test_email_and_web_draw_one_continuous_sequence_no_collision():
    # After reseed, an EMAIL case (allocate_for_uid, the inbox handler) and a WEB
    # case (allocate, the dashboard bridge) draw consecutive numbers from the SAME
    # counter — no 900000 jump, no email/web collision.
    with _iso_root(seq=900005, logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS):
        cs.reseed()                                   # -> 392
        email_case = cs.allocate_for_uid("20481")     # gmail uid 20481 -> case 392
        web_case = cs.allocate()                      # dashboard -> case 393
        take_case = cs.allocate_for_uid("20482")      # another email -> case 394
        got = [email_case, web_case, take_case]
        assert got == [392, 393, 394], got
        assert all(c < cs.CASE_CEILING for c in got), "no number may land in the 900000 band"
        assert len(set(got)) == 3, "email and web cases must never collide"


def test_allocate_for_uid_is_idempotent_and_decoupled():
    with _iso_root(seq=500):
        a = cs.allocate_for_uid("205")
        b = cs.allocate_for_uid("205")               # same uid -> SAME case (retry-safe)
        c = cs.allocate_for_uid("206")               # different uid -> next number
        assert a == 500 and b == 500 and c == 501, (a, b, c)
        # the uid->case link is persisted, and the uid is NOT the case number.
        link = json.loads((_iso_records() / "uid_case.json").read_text())
        assert link == {"205": 500, "206": 501}, link


def test_monotonic_and_concurrency_after_reseed():
    # 20 processes each allocate 15 numbers off a freshly reseeded continuous
    # counter: every number handed out exactly once, contiguous, none in the band.
    with _iso_root(seq=900005, logs=_FIX_LOGS, mapkeys=_FIX_MAP, specs=_FIX_SPECS) as (rp, ip):
        start = cs.reseed()                           # -> 392
        assert start == 392
        n_procs, per = 20, 15
        q = CTX.Queue()
        procs = [CTX.Process(target=_alloc_worker_env,
                             args=(str(rp), str(ip), per, q)) for _ in range(n_procs)]
        for p in procs:
            p.start()
        allnums = []
        for _ in procs:
            allnums.extend(q.get(timeout=60))
        for p in procs:
            p.join(60)
        total = n_procs * per
        assert len(allnums) == total and len(set(allnums)) == total, "dup/lost allocation"
        assert set(allnums) == set(range(start, start + total)), "gap/overlap after reseed"
        assert max(allnums) < cs.CASE_CEILING, "an allocation escaped into the 900000 band"
        print(f"    [caseseq-continuity] {total} concurrent allocs contiguous "
              f"[{start}, {start + total}), all < {cs.CASE_CEILING}")


# ============================================================================
# Case 399: collision-safe allocation. allocate() SKIPS any number already present
# in the records (or reserved), so a number minted OUT OF BAND — a legacy uid-as-
# case deputy, a hand-picked/proposed number — is never re-handed to a second case
# (the Case 398/399 clash). Plus the propose/reserve/is_taken primitives for Feng's
# sheriff-authority model. All isolated under a throwaway root.
# ============================================================================
def test_allocate_skips_a_number_already_in_the_records():
    # the exact Case 398/399 shape: the counter sits ON a number that was minted out
    # of band (it is in the map + a case log). allocate MUST skip it, not re-hand it.
    with _iso_root(seq=398, logs={"infra": [398]}, mapkeys=[398]):
        assert cs.is_taken(398) is True
        got = cs.allocate()
        assert got == 399, f"allocate must skip the already-used 398, got {got}"
        assert cs.peek() == 400
        assert cs.allocate() == 400            # continues cleanly past the skip


def test_dedup_is_a_noop_when_the_next_number_is_free():
    # normal case: the counter is ahead of every record -> byte-identical to the plain
    # counter (no skip, no gap).
    with _iso_root(seq=500, logs={"infra": [300, 384]}, mapkeys=[390, 391]):
        assert cs.allocate() == 500 and cs.allocate() == 501


def test_dedup_escape_hatch_TSOMP_CASE_DEDUP_0():
    with _iso_root(seq=398, mapkeys=[398]):
        saved = os.environ.get("TSOMP_CASE_DEDUP")
        os.environ["TSOMP_CASE_DEDUP"] = "0"
        try:
            assert cs.allocate() == 398, "with dedup off the plain counter re-hands 398"
        finally:
            if saved is None:
                os.environ.pop("TSOMP_CASE_DEDUP", None)
            else:
                os.environ["TSOMP_CASE_DEDUP"] = saved


def test_reserve_makes_allocate_skip_and_is_idempotent():
    with _iso_root(seq=405):
        assert cs.reserve(405) == 405
        assert cs.reserve(405) == 405          # idempotent
        assert cs.is_taken(405) is True
        assert cs.allocate() == 406, "allocate must skip a reserved number"


def test_propose_available_vs_taken_vs_out_of_range():
    with _iso_root(seq=400, logs={"infra": [350]}, mapkeys=[360], specs=[370]):
        assert cs.propose(350)["ok"] is False   # in a case log
        assert cs.propose(360)["ok"] is False   # in the map
        assert cs.propose(370)["ok"] is False   # an inbox spec
        assert cs.propose(371)["ok"] is True    # free
        assert cs.propose(0)["ok"] is False     # out of range
        assert cs.propose(900001)["ok"] is False  # the retired band
        assert cs.propose("nope")["ok"] is False  # not an integer
        # once reserved, a proposal for the same number is denied.
        cs.reserve(371)
        assert cs.propose(371)["ok"] is False


def _iso_records():
    return Path(os.environ["TSOMP_RECORDS_ROOT"])


def _iso_seq():
    return _iso_records() / "case_seq.json"


def _alloc_worker_env(rroot, iroot, n, q):
    os.environ["TSOMP_RECORDS_ROOT"] = rroot
    os.environ["TSOMP_INBOX_ROOT"] = iroot
    os.environ.pop("TSOMP_CASE_BASE", None)
    q.put([cs.allocate(1) for _ in range(n)])


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== scratch_case_seq test suite  ({len(tests)} tests) ===")
    print(f"    records root (throwaway): {_TEST_ROOT}")
    passed, failed = 0, []
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
    print(f"=== SUMMARY: {passed} passed, {len(failed)} failed ===")
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = _run()
    finally:
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)
    sys.exit(rc)
