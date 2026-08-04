#!/usr/bin/env python3
"""Case 391a: ONE continuous, monotonic, concurrency-safe CASE-NUMBER allocator.

A "case number" (a.k.a. task number) identifies one unit of work and is what the
user sees (email subjects, the dashboard, case files, the ``[precinct | case | ...]``
header). Case 391a retires the old 900000 "web band": there is now a SINGLE
continuous sequence for EVERY origin — an emailed task, a dashboard "Create new
case" web form, a deputy taking a follow-up, a JTF, or a sheriff-allocated case all
draw the next number from this one counter (e.g. ... 391 -> 392 -> 393 -> ...).

THE Gmail uid IS NOT THE CASE NUMBER (this reverses the Task 377 "namespaced"
note). Email cases used to reuse the inbound Gmail IMAP **uid** as their case
number, which forced allocated (web / take / sheriff / JTF) numbers into a high
900000 band so they could never collide with a uid. That split is gone: the uid is
now used ONLY for routing / dedup (IMAP seen-marking) / receipts / mailbox origin
headers, and every VISIBLE case number — email OR web — is allocated here. A uid and
a case number may now share a numeric range harmlessly: they live in different
namespaces used in different places (the uid never keys a case file, the map, or
``TSOMP_CASE``). ``allocate_for_uid(uid)`` keeps a small uid->case link so an inbox
handler retry (Task 362 defers) reuses the same reserved number instead of minting a
second case for one email.

SEEDING / RESEEDING. The live counter lives at ``<records_root>/case_seq.json`` =
``{"next": <int>}`` and is authoritative once present (never clamped). When it is
absent, the next number is chosen so it can never collide with an existing case:

  * an explicit ``TSOMP_CASE_BASE`` (tests / manual) pins the seed;
  * in a TEST sandbox (``TSOMP_RECORDS_ROOT`` set) the seed is ``DEFAULT_CASE_BASE``
    (a high failsafe — deterministic, and disjoint from any low fixture);
  * in real production the counter SELF-HEALS: it scans the live records (every
    precinct's case log + the task_precinct map + inbox/task_*.md), takes the max
    REAL case number (pure-integer, excluding the retired 900000+ band and
    alphanumeric sub-cases like 384e / 391a), and seeds at max+1.

``reseed()`` writes that self-healed value into the file, and — because it is safe
by construction — retires a legacy 900000-band counter in place (see its docstring).
Run once at deploy: ``python scratch_case_seq.py reseed`` (today: 900005 -> 392).

STORAGE / CONCURRENCY. Mutations take the same exclusive-advisory-lock +
atomic-replace discipline as scratch_records.py, so concurrent allocations from the
dashboard POST handler, the inbox loop, the sheriff and deputies never lose an
update. Reads are lock-free. ``TSOMP_RECORDS_ROOT`` overrides the records root
(tests); ``TSOMP_INBOX_ROOT`` overrides the inbox dir (tests); ``TSOMP_CASE_BASE``
overrides the no-file seed (tests).

CLI:
    python scratch_case_seq.py allocate [--n K]        # print K fresh numbers
    python scratch_case_seq.py allocate-for-uid --uid N  # case # for an email uid (idempotent)
    python scratch_case_seq.py peek                    # next number, without taking it
    python scratch_case_seq.py reseed [--force]        # retire the 900000 band -> continuous
    python scratch_case_seq.py max-real                # max REAL case # across the records
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS_ROOT = REPO_ROOT / "scratch_full_logs" / "records"
DEFAULT_INBOX_DIR = REPO_ROOT / "scratch_full_logs" / "inbox"
SEQ_NAME = "case_seq.json"
UID_LINK_NAME = "uid_case.json"
# Sheriff-approved PROPOSED numbers that a deputy asked for but that are not yet
# written into the records. allocate() treats these as taken (union with the records
# scan) so an approved proposal is never handed to a different case in the window
# between approval and the case being filed. Append-only; small.
RESERVED_NAME = "case_reserved.json"

# The retired web-band boundary. Numbers >= this are the LEGACY allocated band
# (web_900000 and the few sheriff/JTF test allocations that rode it); they are
# EXCLUDED from the max-real-case scan and a counter file sitting at/above this is
# treated as a legacy band to retire on reseed. NOT a place new numbers come from.
CASE_CEILING = 900000

# Failsafe seed when there is NO counter file and nothing else to go on: a high,
# collision-proof value for an empty TEST sandbox or a catastrophically empty
# production tree. Real production never reaches it (the self-heal scan finds the
# live max first); the reseeded file drives the continuous low sequence. Override
# with TSOMP_CASE_BASE (tests pin it for determinism).
DEFAULT_CASE_BASE = 900000


def _records_root() -> Path:
    env = os.environ.get("TSOMP_RECORDS_ROOT")
    return Path(env) if env else DEFAULT_RECORDS_ROOT


def _inbox_dir() -> Path:
    env = os.environ.get("TSOMP_INBOX_ROOT")
    return Path(env) if env else DEFAULT_INBOX_DIR


def _seq_path() -> Path:
    return _records_root() / SEQ_NAME


def _uid_link_path() -> Path:
    return _records_root() / UID_LINK_NAME


def _reserved_path() -> Path:
    return _records_root() / RESERVED_NAME


def _read_reserved() -> set:
    """The set of sheriff-reserved (approved-but-not-yet-filed) case numbers. Lock-free."""
    try:
        d = json.loads(_reserved_path().read_text())
        return set(int(x) for x in d) if isinstance(d, list) else set()
    except Exception:
        return set()


@contextlib.contextmanager
def _exclusive_lock(target: Path):
    """Exclusive advisory lock on a stable sidecar (mirrors scratch_records)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_caseseq_", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------------------
# max-real-case scan (self-heal seed + reseed source)
# ----------------------------------------------------------------------------
def _numeric_case(token: str):
    """A pure-integer, in-range REAL case number, or None. Rejects alphanumeric
    sub-cases (384e / 391a), '_'-suffixed spec stems (52_rtf_time), header/comment
    junk, and the retired 900000+ band."""
    t = (token or "").strip()
    if not t.isdigit():
        return None
    n = int(t)
    return n if 0 < n < CASE_CEILING else None


def scan_numbers(records_root=None, inbox_dir=None) -> set:
    """The SET of every REAL case number present in the live records: every
    precinct's case log (``<records>/*/log.tsv`` col-0), the task_precinct map keys,
    and the inbox spec filenames (``<inbox>/task_*.md``). Pure-integer only, excluding
    the 900000+ band and alphanumeric sub-cases. Empty set if none found. NEVER raises
    (each source is guarded) — a missing/garbled source contributes nothing.

    This is the authoritative "taken" set. ``allocate()`` skips anything in it (union
    the sheriff-approved ``reserved`` set), so a number minted OUT OF BAND — e.g. a
    legacy deputy that reused a Gmail uid AS its case number, bypassing this counter —
    is never re-handed to a second case (the Case 398/399 collision class)."""
    rroot = Path(records_root) if records_root else _records_root()
    idir = Path(inbox_dir) if inbox_dir else _inbox_dir()
    nums = set()

    # 1) every precinct's case log — first tab-separated column of each row.
    with contextlib.suppress(Exception):
        for logf in sorted(rroot.glob("*/log.tsv")):
            with contextlib.suppress(Exception):
                for line in logf.read_text(errors="replace").splitlines():
                    n = _numeric_case(line.split("\t", 1)[0])
                    if n:
                        nums.add(n)

    # 2) the task_precinct map keys.
    with contextlib.suppress(Exception):
        d = json.loads((rroot / "task_precinct.json").read_text())
        if isinstance(d, dict):
            for k in d:
                n = _numeric_case(str(k))
                if n:
                    nums.add(n)

    # 3) inbox spec filenames: task_<id>.md -> <id> up to the first '_' or '.'.
    with contextlib.suppress(Exception):
        for spec in idir.glob("task_*.md"):
            m = re.match(r"task_([0-9]+)(?:[._]|$)", spec.name)
            if m:
                n = _numeric_case(m.group(1))
                if n:
                    nums.add(n)
    return nums


def compute_max_real_case(records_root=None, inbox_dir=None) -> int:
    """The max REAL case number across the live records (0 if none). Thin wrapper over
    ``scan_numbers`` — kept for the self-heal seed + ``reseed`` callers."""
    nums = scan_numbers(records_root, inbox_dir)
    return max(nums) if nums else 0


def _seed_when_no_file() -> int:
    """The next number to hand out when there is NO counter file. Priority:
    explicit TSOMP_CASE_BASE -> (test sandbox) DEFAULT_CASE_BASE -> (production)
    self-heal from the live records -> DEFAULT_CASE_BASE."""
    env = os.environ.get("TSOMP_CASE_BASE")
    if env not in (None, ""):
        with contextlib.suppress(TypeError, ValueError):
            return int(env)
    # A throwaway records root means a TEST sandbox: stay deterministic (do not
    # scan real records) and hand back the high failsafe, matching legacy behavior.
    if os.environ.get("TSOMP_RECORDS_ROOT"):
        return DEFAULT_CASE_BASE
    # Real production, no file: self-heal to (max real case + 1).
    mx = compute_max_real_case()
    return mx + 1 if mx > 0 else DEFAULT_CASE_BASE


def _read_next() -> int:
    """The next number to hand out. The on-disk counter is AUTHORITATIVE and is
    never clamped (it only ever moves up); when absent, see _seed_when_no_file.
    Lock-free."""
    try:
        d = json.loads(_seq_path().read_text())
        return int(d["next"])
    except Exception:
        return _seed_when_no_file()


def peek() -> int:
    """Return the next case number WITHOUT allocating it. NON-BLOCKING."""
    return _read_next()


def taken_numbers() -> set:
    """Every case number that is currently unavailable: the records scan
    (``scan_numbers``) UNION the sheriff-reserved set. This is what ``allocate`` skips
    and ``propose`` denies against. Lock-free (a caller that must act on it takes the
    lock and re-reads inside)."""
    return scan_numbers() | _read_reserved()


def is_taken(n) -> bool:
    """True iff case number ``n`` is already used (in the records) or reserved."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False
    return n in taken_numbers()


def propose(n) -> dict:
    """Availability check for a deputy-PROPOSED case number (Feng's model: the sheriff
    approves-if-available / denies-if-taken). Returns
    ``{"ok": bool, "number": int|None, "reason": str}``. PURE — does NOT claim the
    number; the sheriff calls ``reserve(n)`` on approve to make the claim stick."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return {"ok": False, "number": None, "reason": f"not an integer: {n!r}"}
    if not (0 < n < CASE_CEILING):
        return {"ok": False, "number": n,
                "reason": f"out of range (a real case number is 0 < n < {CASE_CEILING})"}
    if n in taken_numbers():
        return {"ok": False, "number": n, "reason": "already taken (in the records or reserved)"}
    return {"ok": True, "number": n, "reason": "available"}


def reserve(n) -> int:
    """Claim an approved proposed number by adding it to the reserved set. LOCK + atomic,
    IDEMPOTENT. Does NOT bump the monotonic counter — ``allocate`` simply SKIPS reserved
    numbers when it reaches them, so no in-between numbers are lost to a gap. Once the
    case is filed into the records, ``scan_numbers`` also covers it (belt + suspenders)."""
    n = int(n)
    if not (0 < n < CASE_CEILING):
        raise ValueError(f"cannot reserve {n} (a real case number is 0 < n < {CASE_CEILING})")
    rp = _reserved_path()
    with _exclusive_lock(rp):
        cur = _read_reserved()
        cur.add(n)
        _atomic_write(rp, json.dumps(sorted(cur)) + "\n")
    return n


def allocate(n: int = 1):
    """Allocate ``n`` fresh, monotonic case numbers. LOCK-PROTECTED + atomic.

    Returns a single int when ``n == 1`` (the common case), else a list of ints.
    Never repeats a number across concurrent callers (the exclusive lock serializes
    the read-modify-write).

    COLLISION-SAFE (Case 399): before handing out a number the allocator SKIPS any
    value already present in the records or reserved (``taken_numbers``), so a number
    minted out of band — a legacy uid-as-case deputy, a hand-picked/proposed number, a
    counter that self-healed onto a used value — can never be re-handed to a second
    case. In the normal case (the next counter value is free) this is a no-op and the
    behavior is byte-identical to the plain counter. Set ``TSOMP_CASE_DEDUP=0`` to
    disable the skip (escape hatch); the plain monotonic counter is then used."""
    if n < 1:
        raise ValueError("n must be >= 1")
    dedup = os.environ.get("TSOMP_CASE_DEDUP", "1") != "0"
    target = _seq_path()
    with _exclusive_lock(target):
        cur = _read_next()
        if dedup:
            taken = scan_numbers() | _read_reserved()
            guard = 0
            # advance the start past any already-taken number (bounded safety loop)
            while cur in taken and guard < 1_000_000:
                cur += 1
                guard += 1
        nums = list(range(cur, cur + n))
        _atomic_write(target, json.dumps({"next": cur + n}) + "\n")
    return nums[0] if n == 1 else nums


def allocate_for_uid(uid, kind: str = "email"):
    """Return the case number reserved for an inbound Gmail ``uid``, allocating (and
    persisting a uid->case link) exactly once. IDEMPOTENT: repeated calls for the
    same uid return the same number, so an inbox-handler retry (Task 362 defers)
    reuses the stable spec ``task_<case>.md`` instead of minting a second case for
    one email. The uid stays a routing/dedup/receipt key only — this is just the
    link "where the pipeline needs it". LOCK-PROTECTED + atomic."""
    uid = str(uid)
    link = _uid_link_path()
    with _exclusive_lock(link):
        try:
            m = json.loads(link.read_text())
            if not isinstance(m, dict):
                m = {}
        except Exception:
            m = {}
        if uid in m:
            with contextlib.suppress(TypeError, ValueError):
                return int(m[uid])
        case = allocate()                     # own lock on case_seq (distinct file)
        m[uid] = case
        _atomic_write(link, json.dumps(m, indent=2) + "\n")
        return case


def reseed(force: bool = False) -> int:
    """Retire the 900000 band / seed the live counter to the continuous sequence.

    Writes ``case_seq.json`` = max(real case #)+1 when the counter is MISSING, sits
    in the legacy 900000+ band, or ``force`` is set. When the counter is already a
    continuous (< CEILING) value it is LEFT ALONE (returned as-is) so a reseed never
    lowers an advanced counter and re-hands-out live numbers. Safe by construction:
    all of 392..(CEILING-1) are unused today (real cases stop at 391; the only
    >=CEILING numbers are the already-assigned web/sheriff/JTF band). LOCK + atomic."""
    target = _seq_path()
    with _exclusive_lock(target):
        cur = None
        with contextlib.suppress(Exception):
            cur = int(json.loads(target.read_text())["next"])
        seed = compute_max_real_case() + 1
        if force or cur is None or cur >= CASE_CEILING:
            new = seed
        else:
            new = cur                          # already continuous -> never lower
        _atomic_write(target, json.dumps({"next": new}) + "\n")
        return new


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scratch_case_seq.py",
                                 description="One continuous, monotonic case-number allocator.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("allocate", help="allocate fresh case number(s)")
    a.add_argument("--n", type=int, default=1, help="how many to allocate (default 1)")
    au = sub.add_parser("allocate-for-uid", help="case # for an email uid (idempotent link)")
    au.add_argument("--uid", required=True)
    sub.add_parser("peek", help="print the next number without allocating it")
    rs = sub.add_parser("reseed", help="retire the 900000 band -> continuous (safe, idempotent)")
    rs.add_argument("--force", action="store_true", help="reseed even a continuous counter")
    sub.add_parser("max-real", help="print the max REAL case number across the records")
    pr = sub.add_parser("propose", help="availability check for a proposed number (approve/deny JSON)")
    pr.add_argument("--n", type=int, required=True)
    rv = sub.add_parser("reserve", help="claim an approved proposed number (sheriff, on approve)")
    rv.add_argument("--n", type=int, required=True)
    it = sub.add_parser("is-taken", help="print yes|no for whether a number is used/reserved")
    it.add_argument("--n", type=int, required=True)
    sub.add_parser("taken", help="print the count of taken (used+reserved) case numbers")
    args = ap.parse_args(argv)
    if args.cmd == "allocate":
        res = allocate(args.n)
        for x in ([res] if isinstance(res, int) else res):
            print(x)
    elif args.cmd == "allocate-for-uid":
        print(allocate_for_uid(args.uid))
    elif args.cmd == "peek":
        print(peek())
    elif args.cmd == "reseed":
        print(reseed(force=args.force))
    elif args.cmd == "max-real":
        print(compute_max_real_case())
    elif args.cmd == "propose":
        res = propose(args.n)
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    elif args.cmd == "reserve":
        print(reserve(args.n))
    elif args.cmd == "is-taken":
        print("yes" if is_taken(args.n) else "no")
    elif args.cmd == "taken":
        print(len(taken_numbers()))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
