#!/usr/bin/env python3
"""
claude_infra dashboard — pure-stdlib HTTP server (no external deps).

Read-only view over the live tsomp infra state + login protection. Bind to
localhost by default and reach it over an SSH tunnel; auth is a signed session
cookie backed by a PBKDF2 password hash (see auth.py / set_password.py).

Run:   python server.py           (host/port via INFRA_DASH_HOST/PORT)
       ./run.sh                    (sets up instance + starts)
"""
import http.cookies
import json
import mimetypes
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import config
import lineage
import pages
import state

_INLINE_OK = {".txt", ".log", ".md", ".csv", ".json", ".png", ".jpg", ".jpeg",
              ".gif", ".svg", ".pdf", ".output"}


def _api_status():
    return {
        "daemons": state.daemon_status(),
        "workers": state.workers(active_only=True),
        "gpu": {
            "manager_alive": state.gpu_manager_alive(),
            "running": [j for j in state.gpu_jobs_active() if j["bucket"] == "running"],
            "pending": [j for j in state.gpu_jobs_active() if j["bucket"] == "pending"],
        },
        "jobs": state.jobmgr_jobs(active_only=True),
        "limit": state.limit_state(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "InfraDash/1.0"

    # -- helpers ------------------------------------------------------------
    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _authed(self):
        raw = self.headers.get("Cookie", "")
        try:
            c = http.cookies.SimpleCookie(raw)
        except Exception:
            return False
        m = c.get(auth.COOKIE_NAME)
        return bool(m and auth.check_cookie(m.value))

    def _headers(self, code=200, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()

    def _html(self, body, code=200):
        self._headers(code)
        self.wfile.write(body.encode("utf-8"))

    def _json(self, obj, code=200):
        self._headers(code, "application/json; charset=utf-8")
        self.wfile.write(json.dumps(obj).encode("utf-8"))

    def _redirect(self, to, cookie=None):
        extra = []
        if cookie:
            extra.append(("Set-Cookie", cookie))
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)

        if path == "/healthz":
            return self._json({"ok": True})
        if path == "/login":
            hint = "" if auth.is_configured() else \
                "Password not set — run: python set_password.py"
            return self._html(pages.login_page(hint))
        if path == "/logout":
            expired = f"{auth.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            return self._redirect("/login", cookie=expired)
        if path == "/register":
            tok = (q.get("token") or [""])[0]
            if auth.is_configured():
                return self._html(pages.register_page(
                    error="This dashboard is already registered — use the login page.",
                    closed=True), 409)
            if not auth.check_register_token(tok):
                return self._html(pages.register_page(
                    error="Invalid, used, or expired registration link.",
                    closed=True), 403)
            return self._html(pages.register_page(token=tok))

        if not self._authed():
            if path.startswith("/api/"):
                return self._json({"error": "unauthorized"}, 401)
            return self._redirect("/login")

        # authed routes
        if path == "/":
            return self._html(pages.status_page())
        if path == "/history":
            return self._html(pages.history_page())
        if path == "/lineage":
            return self._html(pages.lineage_page())
        if path == "/api/status":
            return self._json(_api_status())
        if path == "/api/history/days":
            return self._json(state.history_day_counts())
        if path == "/api/history/day":
            day = (q.get("date") or [""])[0]
            return self._json(state.history_for_day(day))
        if path == "/api/job":
            jid = (q.get("id") or [""])[0]
            j = state.job_detail(jid)
            return self._json(j or {"error": "not found"}, 200 if j else 404)
        if path == "/api/task":
            try:
                tid = int((q.get("id") or ["0"])[0])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            return self._json({
                "task_id": tid,
                "conversation": lineage.task_conversation(tid),
                "lineage": lineage.lineage_for(tid),
            })
        if path == "/api/lineage":
            return self._json(lineage.build_lineage())
        if path == "/api/forest":
            return self._json(lineage.build_forest())
        if path == "/download":
            return self._download((q.get("path") or [""])[0])
        return self._json({"error": "not found"}, 404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/login", "/register"):
            return self._json({"error": "not found"}, 404)
        ip = self._client_ip()
        n = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = urllib.parse.parse_qs(data)
        if u.path == "/register":
            if auth.is_locked(ip):
                return self._html(pages.register_page(
                    error="Too many attempts — wait a few minutes.", closed=True), 429)
            return self._do_register(form, ip)
        # /login
        if auth.is_locked(ip):
            return self._html(pages.login_page("Too many attempts — wait a few minutes."), 429)
        pw = form.get("password", [""])[0]
        if auth.verify_password(pw):
            auth.record_success(ip)
            cookie = (f"{auth.COOKIE_NAME}={auth.make_cookie()}; Path=/; "
                      f"Max-Age={auth.SESSION_TTL}; HttpOnly; SameSite=Strict")
            return self._redirect("/", cookie=cookie)
        auth.record_fail(ip)
        return self._html(pages.login_page("Incorrect password."), 401)

    def _do_register(self, form, ip):
        if auth.is_configured():
            return self._html(pages.register_page(
                error="This dashboard is already registered.", closed=True), 409)
        tok = form.get("token", [""])[0]
        pw = form.get("password", [""])[0]
        pw2 = form.get("password2", [""])[0]
        if not auth.check_register_token(tok):
            auth.record_fail(ip)
            return self._html(pages.register_page(
                error="Invalid, used, or expired registration link.", closed=True), 403)
        if pw != pw2:
            return self._html(pages.register_page(
                token=tok, error="Passwords did not match."), 400)
        if len(pw) < auth.MIN_PW_LEN:
            return self._html(pages.register_page(
                token=tok, error=f"Password too short (min {auth.MIN_PW_LEN} chars)."), 400)
        if auth.consume_register_token(tok, pw):
            auth.record_success(ip)
            return self._redirect("/login")
        return self._html(pages.register_page(
            error="Registration failed — link invalid or already used.", closed=True), 403)

    # -- guarded file download ---------------------------------------------
    def _download(self, req):
        if not req:
            return self._json({"error": "no path"}, 400)
        try:
            rp = Path(req).resolve()
        except Exception:
            return self._json({"error": "bad path"}, 400)
        low = str(rp).lower()
        if any(frag in low for frag in config.DOWNLOAD_DENY):
            return self._json({"error": "forbidden"}, 403)
        ok = False
        for root in config.DOWNLOAD_ROOTS:
            try:
                rp.relative_to(root)
                ok = True
                break
            except ValueError:
                continue
        if not ok or not rp.is_file():
            return self._json({"error": "forbidden"}, 403)
        try:
            size = rp.stat().st_size
        except OSError:
            return self._json({"error": "unreadable"}, 404)
        if size > config.DOWNLOAD_MAX_BYTES:
            return self._json({"error": "file too large"}, 413)
        ctype = mimetypes.guess_type(rp.name)[0] or "text/plain; charset=utf-8"
        disp = "inline" if rp.suffix.lower() in _INLINE_OK else "attachment"
        self._headers(200, ctype, extra=[
            ("Content-Disposition", f'{disp}; filename="{rp.name}"'),
            ("Content-Length", str(size)),
        ])
        with open(rp, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("%s - %s\n" % (self._client_ip(), fmt % args))


def main():
    if not auth.is_configured():
        sys.stderr.write(
            "\n[!] No dashboard password set. Run:  python set_password.py\n"
            "    (the server will start, but login will reject until you do)\n\n")
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    sys.stderr.write(
        f"[infra-dash] serving on http://{config.HOST}:{config.PORT}  "
        f"(state root: {config.STATE_ROOT})\n"
        f"[infra-dash] tunnel:  ssh -L {config.PORT}:localhost:{config.PORT} <host>\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
