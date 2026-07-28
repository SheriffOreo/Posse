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
HOST = os.environ.get("INFRA_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("INFRA_DASH_PORT", "8787"))

# --- dashboard-private secret store (password hash + cookie key) ------------
INSTANCE_DIR = Path(
    os.environ.get("INFRA_DASH_INSTANCE", Path(__file__).resolve().parent / "instance")
)
SECRET_FILE = INSTANCE_DIR / "auth.json"
# One-time registration token (hash + used flag). Also under the git-ignored instance/.
REGISTER_FILE = INSTANCE_DIR / "register.json"

# --- state file locations (all under STATE_ROOT) ----------------------------
SCRATCH = STATE_ROOT / "scratch_full_logs"
INBOX = SCRATCH / "inbox"
JOBS = SCRATCH / "jobs"
GPU_QUEUE = STATE_ROOT / "gpu_queue"

WATCHDOG_JOBS = SCRATCH / "watchdog_jobs.json"
REGISTRY = STATE_ROOT / "scratch_agents_registry.json"
SENT_EMAILS = SCRATCH / "sent_emails.jsonl"
LIMIT_STATE = JOBS / "limit_state.json"

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
]
# Never serve these, even inside a whitelisted root.
DOWNLOAD_DENY = [
    "credentials.json", ".smtp_env", ".anthropic_key", ".credentials",
    "auth.json", "register.json", ".env", "id_rsa", "private", ".pem", ".key",
]

# Max bytes served by the download endpoint (guard against dumping huge files).
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
