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
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import config
import jtf
import lineage
import multipart
import pages
import state

_INLINE_OK = {".txt", ".log", ".md", ".csv", ".json", ".png", ".jpg", ".jpeg",
              ".gif", ".svg", ".pdf", ".output"}


def _session_cookie(value, max_age):
    """Build the session Set-Cookie header. Adds the `Secure` flag under TLS so the
    cookie is never sent back over plain HTTP on the LAN."""
    secure = "; Secure" if config.TLS else ""
    return (f"{auth.COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Strict{secure}")


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
                         # Case 421: blob: lets the create-case form preview pasted /
                         # dropped images via URL.createObjectURL (object URLs).
                         "script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                         "font-src 'self' data:")
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
            return self._redirect("/login", cookie=_session_cookie("", 0))
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
            info = auth.register_info()
            return self._html(pages.register_page(
                token=tok, email=info.get("email"), name=info.get("name")))

        if not self._authed():
            if path.startswith("/api/"):
                return self._json({"error": "unauthorized"}, 401)
            return self._redirect("/login")

        # authed routes
        if path == "/":
            return self._html(pages.status_page())
        if path == "/history":
            return self._html(pages.history_page())
        if path == "/precincts":
            return self._html(pages.precincts_page())
        if path == "/precinct":
            return self._html(pages.precinct_detail_page((q.get("name") or [""])[0]))
        if path == "/lineage":
            return self._html(pages.lineage_page())
        if path == "/jtf":
            return self._html(pages.jtf_page())
        if path == "/cowork":
            # Phase E: "Cowork" was renamed "JTF" — keep the old link working
            # (redirect, never 500) for any bookmark / in-flight page.
            return self._redirect("/jtf")
        if path == "/api/status":
            return self._json(_api_status())
        if path == "/api/gpu_stats":
            # Task 377 #3: on-demand per-GPU stats (nvidia-smi run per request).
            # Deliberately NOT part of _api_status — only the Status page Refresh
            # button hits this, so nvidia-smi never runs on the 8s auto-poll.
            return self._json(state.gpu_stats())
        if path == "/api/precincts":
            return self._json({"sheriff": state.sheriff_status(),
                               "precincts": state.precincts()})
        if path == "/api/precinct":
            name = (q.get("name") or [""])[0]
            d = state.precinct_detail(name)
            return self._json(d or {"error": "not found"}, 200 if d else 404)
        if path == "/api/history/days":
            return self._json(state.history_day_counts())
        if path == "/api/history/day":
            day = (q.get("date") or [""])[0]
            return self._json(state.history_for_day(day))
        if path == "/api/history/day_lineage":
            day = (q.get("date") or [""])[0]
            return self._json(lineage.history_day_lineage(day))
        if path == "/api/job":
            jid = (q.get("id") or [""])[0]
            j = state.job_detail(jid)
            return self._json(j or {"error": "not found"}, 200 if j else 404)
        if path == "/api/task":
            try:
                tid = int((q.get("id") or ["0"])[0])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            conv = lineage.task_conversation(tid)
            return self._json({
                "task_id": tid,
                "conversation": conv,
                "lineage": lineage.lineage_for(tid),
                "deliverables": state.deliverables_for(tid, (conv or {}).get("agent")),
            })
        if path == "/api/lineage":
            return self._json(lineage.build_lineage())
        if path == "/api/forest":
            return self._json(lineage.build_forest())
        if path == "/api/agents":
            qs = (q.get("q") or [""])[0]
            return self._json(jtf.agent_index(qs))
        if path == "/download":
            return self._download((q.get("path") or [""])[0])
        return self._json({"error": "not found"}, 404)

    # -- POST ---------------------------------------------------------------
    # Whitelisted set for the model write path (mirrors scratch_records._MODELS).
    _MODELS = ("fable", "opus", "sonnet", "haiku")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # Task 376: the dashboard's FIRST write — set a precinct's default model.
        # Auth-gated, model whitelisted, only edits an EXISTING precinct, and
        # delegates to the tsomp records manager (the single source of truth).
        if u.path == "/precinct/model":
            if not self._authed():
                return self._redirect("/login")
            return self._set_precinct_model()
        # Task 384b / Phase C: set the ONE global sheriff model (system-wide, not
        # per-precinct). Same auth-gated, whitelisted, records-manager-delegating
        # pattern as the precinct model above.
        if u.path == "/sheriff/model":
            if not self._authed():
                return self._redirect("/login")
            return self._set_sheriff_model()
        # Task 377 #4: the authed per-precinct "Create new case" upload. Submitted
        # via fetch() with FormData, so it answers JSON (401 on unauth, not a
        # redirect). Parses multipart itself, then returns before the urlencoded
        # body read below.
        if u.path == "/precinct/create_case":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._create_web_case()
        # Case 384e / Phase E: the authed JTF (Joint Task Force) submission. Submitted
        # via fetch() with a JSON body, so it answers JSON (401 on unauth, not a
        # redirect). Validates each slot (precinct-or-deputy) then drops a record the
        # inbox-side scratch_jtf.py bridge materializes.
        if u.path == "/api/jtf":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._create_jtf()
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
            cookie = _session_cookie(auth.make_cookie(), auth.SESSION_TTL)
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
        name = form.get("name", [""])[0]
        if not auth.check_register_token(tok):
            auth.record_fail(ip)
            return self._html(pages.register_page(
                error="Invalid, used, or expired registration link.", closed=True), 403)
        info = auth.register_info()
        if pw != pw2:
            return self._html(pages.register_page(
                token=tok, email=info.get("email"), name=name or info.get("name"),
                error="Passwords did not match."), 400)
        if len(pw) < auth.MIN_PW_LEN:
            return self._html(pages.register_page(
                token=tok, email=info.get("email"), name=name or info.get("name"),
                error=f"Password too short (min {auth.MIN_PW_LEN} chars)."), 400)
        if auth.consume_register_token(tok, pw, name=name):
            auth.record_success(ip)
            return self._redirect("/login")
        return self._html(pages.register_page(
            error="Registration failed — link invalid or already used.", closed=True), 403)

    # -- authed write: set a precinct's default model (Task 376) -------------
    def _set_precinct_model(self):
        """Validate + persist a precinct's default model by delegating to the tsomp
        records manager. Only edits an EXISTING precinct with a whitelisted model;
        anything else is a no-op redirect. Never creates a precinct or writes
        anything else — the dashboard stays otherwise read-only."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = urllib.parse.parse_qs(data)
        name = (form.get("name") or [""])[0].strip()
        model = (form.get("model") or [""])[0].strip().lower()
        back = "/precinct?name=" + urllib.parse.quote(name)
        known = {p["name"] for p in state.precincts()}
        if name in known and model in self._MODELS:
            # Delegate to the records manager (its precincts.json is the source of
            # truth the dashboard reads). Force the LIVE records root by dropping any
            # TSOMP_RECORDS_ROOT override the server might inherit.
            env = {k: v for k, v in os.environ.items() if k != "TSOMP_RECORDS_ROOT"}
            try:
                subprocess.run(
                    [sys.executable, str(config.STATE_ROOT / "scratch_records.py"),
                     "directory", "register", "--name", name, "--model", model],
                    cwd=str(config.STATE_ROOT), env=env, timeout=30,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except Exception as ex:
                sys.stderr.write(f"[infra-dash] set model failed: {ex}\n")
        return self._redirect(back)

    # -- authed write: set the GLOBAL sheriff model (Task 384b / Phase C) -----
    def _set_sheriff_model(self):
        """Validate + persist the ONE system-wide sheriff model by delegating to the
        tsomp records manager (`scratch_records.py sheriff model --set`). Whitelisted
        model only; anything else is a no-op redirect. The dashboard writes nothing
        else — the records manager's sheriff_config.json is the single source of truth
        the daemon reads."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        form = urllib.parse.parse_qs(data)
        model = (form.get("model") or [""])[0].strip().lower()
        if model in self._MODELS:
            # Force the LIVE records root by dropping any TSOMP_RECORDS_ROOT override.
            env = {k: v for k, v in os.environ.items() if k != "TSOMP_RECORDS_ROOT"}
            try:
                subprocess.run(
                    [sys.executable, str(config.STATE_ROOT / "scratch_records.py"),
                     "sheriff", "model", "--set", model, "--role", "sheriff"],
                    cwd=str(config.STATE_ROOT), env=env, timeout=30,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except Exception as ex:
                sys.stderr.write(f"[infra-dash] set sheriff model failed: {ex}\n")
        return self._redirect("/precincts")

    # -- authed write: create a web case (Task 377 #4) -----------------------
    def _allocate_case(self):
        """Allocate a fresh case number from the tsomp allocator (the single source
        of truth). Returns an int, or None on failure (the bridge then allocates its
        own, so a failure here never blocks the submission)."""
        env = {k: v for k, v in os.environ.items() if k != "TSOMP_RECORDS_ROOT"}
        try:
            out = subprocess.run(
                [sys.executable, str(config.STATE_ROOT / "scratch_case_seq.py"), "allocate"],
                cwd=str(config.STATE_ROOT), env=env, timeout=30,
                capture_output=True, text=True, check=False)
            lines = (out.stdout or "").strip().splitlines()
            return int(lines[0]) if lines else None
        except Exception as ex:
            sys.stderr.write(f"[infra-dash] case allocate failed: {ex}\n")
            return None

    def _create_web_case(self):
        """Validate + persist a per-precinct 'Create new case' submission as a
        web-case record the inbox-handler bridge (scratch_web_case.py) consumes.
        Multipart is parsed here; sizes are capped; the precinct is validated against
        the live set and the model against the whitelist. The dashboard itself never
        spawns anything — it only drops the record + files for the inbox pipeline."""
        ct = self.headers.get("Content-Type", "")
        boundary = multipart.boundary_of(ct)
        if not boundary:
            return self._json({"ok": False, "error": "expected multipart/form-data"}, 400)
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._json({"ok": False, "error": "empty submission"}, 400)
        if n > config.WEB_CASE_MAX_BODY:
            return self._json({"ok": False, "error": "submission too large"}, 413)
        try:
            body = self.rfile.read(n)
            fields, files = multipart.parse(body, boundary)
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not parse form: {ex}"}, 400)

        precinct = (fields.get("precinct") or "").strip()
        model = (fields.get("model") or "").strip().lower()
        parent = (fields.get("parent") or "").strip()
        description = (fields.get("description") or "").strip()

        known = {p["name"] for p in state.precincts()}
        if precinct not in known:
            return self._json({"ok": False, "error": f"unknown precinct '{precinct}'"}, 400)
        if model and model not in self._MODELS:
            return self._json({"ok": False, "error": f"invalid model '{model}'"}, 400)
        if parent and not parent.isdigit():
            return self._json({"ok": False, "error": "follow-up must be a task/case number"}, 400)
        if not description:
            return self._json({"ok": False, "error": "a task description is required"}, 400)

        sid = time.strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
        saved, skipped = [], 0
        try:
            att_dir = config.WEB_CASES / "att" / sid
            att_dir.mkdir(parents=True, exist_ok=True)
            for f in files[:config.WEB_CASE_MAX_FILES]:
                content = f.get("content") or b""
                if len(content) > config.WEB_CASE_MAX_FILE_BYTES:
                    skipped += 1
                    continue
                safe = re.sub(r"[^\w.\-]+", "_", (f.get("filename") or "file")).strip("_") or "file"
                dest = att_dir / safe
                i = 1
                while dest.exists():
                    dest = att_dir / f"{i}_{safe}"
                    i += 1
                with open(dest, "wb") as fh:
                    fh.write(content)
                saved.append(str(dest))
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not save uploads: {ex}"}, 500)

        case = self._allocate_case()
        record = {"id": sid, "ts": time.time(), "precinct": precinct,
                  "model": model or None, "parent": parent or None,
                  "description": description, "files": saved, "case": case,
                  "source": "web", "requester": config.OPERATOR.get("email") or ""}
        try:
            pend = config.WEB_CASES / "pending"
            pend.mkdir(parents=True, exist_ok=True)
            tmp = pend / (sid + ".json.tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, pend / (sid + ".json"))
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not queue submission: {ex}"}, 500)
        note = ("submitted — the inbox handler will spawn the deputy and email an ACK "
                "with your form + uploads attached.")
        if skipped:
            note += f" ({skipped} oversized file(s) skipped.)"
        return self._json({"ok": True, "case": case, "sid": sid,
                           "files": len(saved), "note": note})

    # -- authed write: create a JTF (Joint Task Force) (Case 384e) -----------
    @staticmethod
    def _valid_slot(slot, known):
        """Normalize + validate ONE slot to {"kind","name"} or None. A precinct slot's
        name must be a live precinct; a deputy slot just needs a non-empty name (the
        must-take machinery no-ops safely if the deputy can't be revived)."""
        if not isinstance(slot, dict):
            return None
        kind = str(slot.get("kind", "")).strip().lower()
        name = str(slot.get("name", "")).strip()
        if kind not in ("precinct", "deputy") or not name:
            return None
        if kind == "precinct" and name not in known:
            return None
        return {"kind": kind, "name": name}

    def _create_jtf(self):
        """Validate a JTF submission (a lead + >=1 collaborator, each slot a precinct or
        a specific deputy) and drop a record the inbox-side scratch_jtf.py bridge
        materializes. The dashboard itself never spawns anything — it only queues the
        record, exactly like the web-case path."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._json({"ok": False, "error": "empty submission"}, 400)
        if n > config.JTF_MAX_BODY:
            return self._json({"ok": False, "error": "submission too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "expected a JSON object"}, 400)

        known = {p["name"] for p in state.precincts()}
        lead = self._valid_slot(payload.get("lead"), known)
        if not lead:
            return self._json({"ok": False,
                               "error": "lead must be a valid precinct or a specific deputy"}, 400)
        collabs_in = payload.get("collaborators")
        if not isinstance(collabs_in, list) or not collabs_in:
            return self._json({"ok": False, "error": "add at least one collaborator"}, 400)
        collabs = []
        for c in collabs_in:
            v = self._valid_slot(c, known)
            if not v:
                return self._json({"ok": False,
                                   "error": "each collaborator must be a valid precinct or a specific deputy"}, 400)
            collabs.append(v)
        description = str(payload.get("description") or "").strip()
        if not description:
            return self._json({"ok": False, "error": "a task description is required"}, 400)

        sid = time.strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
        record = {"id": sid, "ts": time.time(), "lead": lead, "collaborators": collabs,
                  "critic": bool(payload.get("critic")), "description": description,
                  "source": "web", "requester": config.OPERATOR.get("email") or ""}
        try:
            pend = config.JTF / "pending"
            pend.mkdir(parents=True, exist_ok=True)
            tmp = pend / (sid + ".json.tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, pend / (sid + ".json"))
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not queue submission: {ex}"}, 500)
        return self._json({"ok": True, "sid": sid,
                           "note": "the inbox handler will materialize the JTF and email an ACK."})

    # -- guarded file download ---------------------------------------------
    def _download(self, req):
        if not req:
            return self._json({"error": "no path"}, 400)
        # realpath under a whitelisted root, no DENY fragment, is a real file;
        # relative paths resolve against STATE_ROOT (shared guard).
        rp = config.resolve_download(req)
        if rp is None:
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
    scheme = "http"
    if config.TLS:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(config.CERT_FILE), str(config.KEY_FILE))
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            scheme = "https"
        except Exception as e:
            sys.stderr.write(
                f"[infra-dash] TLS requested but could not load cert/key "
                f"({config.CERT_FILE} / {config.KEY_FILE}): {e}\n"
                f"[infra-dash] REFUSING to fall back to cleartext with TLS on. "
                f"Generate a cert (start_dashboard.sh does this) or unset INFRA_DASH_TLS.\n")
            srv.server_close()
            raise SystemExit(1)
    sys.stderr.write(
        f"[infra-dash] serving on {scheme}://{config.HOST}:{config.PORT}  "
        f"(state root: {config.STATE_ROOT})\n")
    if config.HOST in ("127.0.0.1", "localhost"):
        sys.stderr.write(
            f"[infra-dash] tunnel:  ssh -L {config.PORT}:localhost:{config.PORT} <host>\n")
    elif not config.TLS:
        sys.stderr.write(
            "[infra-dash] NOTE: bound to a public interface over plain HTTP — the "
            "login password travels cleartext on the LAN. Prefer INFRA_DASH_TLS=1.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
