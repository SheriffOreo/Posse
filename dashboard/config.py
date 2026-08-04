"""
Central configuration for the claude_infra dashboard.

Everything is READ-ONLY against the live tsomp working directory. The one knob
that matters is INFRA_STATE_ROOT: it points at the directory the daemons use for
runtime state (default: the tsomp checkout). This is the SAME env var the daemon
"cutover" uses, so the dashboard and a relocated daemon agree on where state lives.

No secrets live here. The dashboard's own login secret lives under instance/
(git-ignored), created by set_password.py.
"""
import os
from pathlib import Path

# --- where the LIVE runtime state lives (read-only) -------------------------
STATE_ROOT = Path(
    os.environ.get("INFRA_STATE_ROOT", "/home/steven/Projects/time-series-omp")
).resolve()

# --- network bind (default localhost; reach via SSH tunnel) ------------------
# HOST defaults to localhost (safe). To reach it without a tunnel, bind wider via
# INFRA_DASH_HOST=0.0.0.0 (or the INFRA_DASH_PUBLIC=1 path in start_dashboard.sh).
HOST = os.environ.get("INFRA_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("INFRA_DASH_PORT", "8787"))


def _flag(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- optional TLS (stdlib ssl; cert generated at setup time, see start_dashboard.sh)
# When on, the server wraps its socket with a self-signed cert and the session
# cookie gets the `Secure` flag so the password is never sent cleartext on the LAN.
TLS = _flag("INFRA_DASH_TLS")

# --- dashboard-private secret store (password hash + cookie key) ------------
INSTANCE_DIR = Path(
    os.environ.get("INFRA_DASH_INSTANCE", Path(__file__).resolve().parent / "instance")
)
SECRET_FILE = INSTANCE_DIR / "auth.json"
# One-time registration token (hash + used flag). Also under the git-ignored instance/.
REGISTER_FILE = INSTANCE_DIR / "register.json"
# TLS cert + key (self-signed, generated at setup time). Under the git-ignored
# instance/ so they never land in git. Only read when TLS is on.
CERT_FILE = Path(os.environ.get("INFRA_DASH_CERT", INSTANCE_DIR / "cert.pem"))
KEY_FILE = Path(os.environ.get("INFRA_DASH_KEY", INSTANCE_DIR / "key.pem"))

# --- state file locations (all under STATE_ROOT) ----------------------------
SCRATCH = STATE_ROOT / "scratch_full_logs"
INBOX = SCRATCH / "inbox"
JOBS = SCRATCH / "jobs"
GPU_QUEUE = STATE_ROOT / "gpu_queue"

WATCHDOG_JOBS = SCRATCH / "watchdog_jobs.json"
REGISTRY = STATE_ROOT / "scratch_agents_registry.json"
SENT_EMAILS = SCRATCH / "sent_emails.jsonl"
LIMIT_STATE = JOBS / "limit_state.json"

# Task 372 (Sheriff & Deputies precincts): the records room.
RECORDS = SCRATCH / "records"
PRECINCTS_JSON = RECORDS / "precincts.json"
PRECINCTS_MD = RECORDS / "PRECINCTS.md"
TASK_PRECINCT = RECORDS / "task_precinct.json"
SHERIFF_LOG = SCRATCH / "sheriff" / "sheriff.log"
# Task 384b / Phase C: the GLOBAL sheriff config (one value system-wide) the sheriff
# daemon reads + the dashboard shows/sets. Today it holds the global SHERIFF MODEL
# ({"model": "fable"}); an authed POST /sheriff/model writes it via the records manager.
SHERIFF_CONFIG = RECORDS / "sheriff_config.json"
# Task 382 #1: the active-deputies state file (deputy -> current case/description/
# precinct) the Status board trusts over the launch-script defaults, so a relaunched
# deputy that TAKES a new case shows the real current case (not its previous one).
ACTIVE_DEPUTIES = RECORDS / "active_deputies.json"

# Task 377 #4: the web "Create new case" drop dir the inbox-handler bridge
# (scratch_web_case.py) consumes. The POST handler writes pending/<sid>.json +
# att/<sid>/<uploads> here. Caps for the authed multipart upload.
WEB_CASES = SCRATCH / "web_cases"
WEB_CASE_MAX_BODY = 30 * 1024 * 1024   # total multipart body cap (bytes)
WEB_CASE_MAX_FILES = 10                # max uploaded files per submission
WEB_CASE_MAX_FILE_BYTES = 20 * 1024 * 1024   # per-file cap (bytes)

# Case 384e / Phase E: the JTF (Joint Task Force) drop dir the inbox-side bridge
# (tsomp scratch_jtf.py) materializes. The authed POST /api/jtf writes
# pending/<sid>.json here; a small JSON body cap guards the endpoint.
JTF = SCRATCH / "jtf"
JTF_MAX_BODY = 256 * 1024               # JTF submission is small JSON (no uploads)

# jobmgr job buckets
JOB_BUCKETS = ["pending", "running", "wakes", "sleeping", "done"]
GPU_BUCKETS = ["pending", "running", "done"]

# --- download endpoint safety ----------------------------------------------
# A requested file is served ONLY if its realpath is under one of these roots
# AND does not match a DENY fragment. Prevents path traversal + secret leaks.
DOWNLOAD_ROOTS = [
    (STATE_ROOT / "reports").resolve(),
    (STATE_ROOT / "outputs_sweep").resolve(),
    (STATE_ROOT / "outputs_tuning").resolve(),
    (SCRATCH).resolve(),
    (GPU_QUEUE / "logs").resolve(),
    (STATE_ROOT / "eval").resolve(),
    # Task 327 (Steven-approved, option b): the paper's notes dir, so deliverables a
    # persistent worker emails from the paper repo (e.g. residual_models_survey.pdf,
    # the Task 324 FINAL attachment) resolve through the guarded /download. Narrowest
    # path that covers them; DOWNLOAD_DENY below still blocks any secret inside it.
    Path("/home/steven/Papers/Time_series_OMP/notes").resolve(),
]
# Never serve these, even inside a whitelisted root.
DOWNLOAD_DENY = [
    "credentials.json", ".smtp_env", ".anthropic_key", ".credentials",
    "auth.json", "register.json", ".env", "id_rsa", "private", ".pem", ".key",
]

# Max bytes served by the download endpoint (guard against dumping huge files).
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024


def _under(rp, root) -> bool:
    try:
        rp.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_download(path):
    """Resolve a download request to a concrete file Path IFF it is allowed, else
    None. Single source of truth shared by the /download handler and the
    deliverables-link assembler (so we never surface a link the server would 403).

    RELATIVE paths resolve against STATE_ROOT — job JSONs store artifact paths
    relative to the state root, not the server's cwd. The realpath must then sit
    under a DOWNLOAD_ROOT, match no DENY fragment, and be a real file. Size is NOT
    checked here (the handler enforces the cap)."""
    if not path:
        return None
    try:
        p = Path(path)
        if not p.is_absolute():
            p = STATE_ROOT / p
        rp = p.resolve()
    except Exception:
        return None
    low = str(rp).lower()
    if any(frag in low for frag in DOWNLOAD_DENY):
        return None
    if not any(_under(rp, root) for root in DOWNLOAD_ROOTS):
        return None
    try:
        return rp if rp.is_file() else None
    except OSError:
        return None


def download_allowed(path) -> bool:
    """True iff resolve_download() would serve `path`. See it for the rules."""
    return resolve_download(path) is not None
