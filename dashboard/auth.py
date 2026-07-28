"""
Login + session handling. Pure stdlib.

- Password is stored ONLY as a salted PBKDF2-HMAC-SHA256 hash in instance/auth.json
  (git-ignored, chmod 600). The plaintext is never written anywhere.
- Sessions are stateless signed cookies: base64("ok:<expiry>") + "." +
  HMAC-SHA256(cookie_secret, payload). Verified in constant time; expiry enforced.
- A tiny in-memory backoff slows password brute force.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import config

_ITER = 240_000
COOKIE_NAME = "infra_dash_session"
SESSION_TTL = 12 * 3600  # seconds

# ip -> (fail_count, window_start_ts)   (module-level, per-process)
_login_fails = {}
_LOCK_AFTER = 6
_LOCK_WINDOW = 300


def _load():
    try:
        return json.loads(config.SECRET_FILE.read_text())
    except Exception:
        return None


def is_configured() -> bool:
    d = _load()
    return bool(d and d.get("pw_hash") and d.get("cookie_secret"))


def set_password(pw: str):
    config.INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    d = _load() or {}
    d["salt"] = salt.hex()
    d["pw_hash"] = h.hex()
    d["iter"] = _ITER
    if not d.get("cookie_secret"):
        d["cookie_secret"] = secrets.token_hex(32)
    config.SECRET_FILE.write_text(json.dumps(d))
    try:
        os.chmod(config.SECRET_FILE, 0o600)
    except OSError:
        pass


def verify_password(pw: str) -> bool:
    d = _load()
    if not d:
        return False
    try:
        salt = bytes.fromhex(d["salt"])
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(d.get("iter", _ITER)))
        return hmac.compare_digest(h.hex(), d["pw_hash"])
    except Exception:
        return False


def _secret() -> bytes:
    d = _load() or {}
    return (d.get("cookie_secret") or "").encode()


def make_cookie() -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = base64.urlsafe_b64encode(f"ok:{exp}".encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_cookie(val: str) -> bool:
    if not val or "." not in val:
        return False
    payload, sig = val.rsplit(".", 1)
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        raw = base64.urlsafe_b64decode(payload.encode()).decode()
        _, exp = raw.split(":")
        return int(exp) > int(time.time())
    except Exception:
        return False


# --- brute-force backoff ----------------------------------------------------
def is_locked(ip: str) -> bool:
    n, start = _login_fails.get(ip, (0, 0))
    if n >= _LOCK_AFTER and (time.time() - start) < _LOCK_WINDOW:
        return True
    return False


def record_fail(ip: str):
    n, start = _login_fails.get(ip, (0, 0))
    if (time.time() - start) >= _LOCK_WINDOW:
        n, start = 0, time.time()
    _login_fails[ip] = (n + 1, start or time.time())


def record_success(ip: str):
    _login_fails.pop(ip, None)
