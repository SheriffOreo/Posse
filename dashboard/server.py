#!/usr/bin/env python3
"""
claude_infra dashboard — pure-stdlib HTTP server (no external deps).

Read-only view over the live tsomp infra state + login protection. Bind to
localhost by default and reach it over an SSH tunnel; auth is a signed session
cookie backed by a PBKDF2 password hash (see auth.py / set_password.py).

Run:   python server.py           (host/port via INFRA_DASH_HOST/PORT)
       ./run.sh                    (sets up instance + starts)
"""
import contextlib
import http.cookies
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import config
import jtf
import lineage
import models
import multipart
import pages
import state

_INLINE_OK = {".txt", ".log", ".md", ".csv", ".json", ".png", ".jpg", ".jpeg",
              ".gif", ".svg", ".pdf", ".output"}
# Case 591: a deputy name reaches an argv, so it is checked here as well as in
# the CLI. Same shape scratch_model_switch._VALID_NAME enforces.
_VALID_WORKER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _session_cookie(value, max_age):
    """Build the session Set-Cookie header. Adds the `Secure` flag under TLS so the
    cookie is never sent back over plain HTTP on the LAN."""
    secure = "; Secure" if config.TLS else ""
    return (f"{auth.COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Strict{secure}")


def _api_status():
    return {
        "daemons": state.daemon_status(),
        # Case 591: the Status board lists failed deputies so they can be relaunched.
        "workers": state.workers(active_only=True, include_failed=True),
        "gpu": {
            "manager_alive": state.gpu_manager_alive(),
            "running": [j for j in state.gpu_jobs_active() if j["bucket"] == "running"],
            "pending": [j for j in state.gpu_jobs_active() if j["bucket"] == "pending"],
        },
        "jobs": state.jobmgr_jobs(active_only=True),
        # Case 760: the JTF groups whose deputies are on this board, so the table
        # can be split into one box per joint task force plus the ungrouped rest.
        # Derived from the same workers() call above, so a box can never name a
        # deputy the table below has no row (and no controls) for.
        "jtfs": state.jtf_board(),
        "limit": state.limit_state(),
        # Case 582: ALL live limits (both vendors' account walls + per-model caps).
        # "limit" stays so nothing already reading it breaks; the banner uses this,
        # because a ChatGPT wall is invisible in "limit" by construction.
        "limits": state.limit_states(),
        # Case 681: auth expiry is account-local too.  This intentionally exposes
        # only the account display label + blocked state, never an auth home, key,
        # token, or credential timestamp.
        "auths": state.auth_states(),
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
        if path == "/judges":                       # Case 551
            return self._html(pages.judges_page())
        if path == "/field-guide":                  # Case 685
            return self._html(pages.field_guide_page())
        if path == "/api/judge":                    # one judge's custom prompt
            cid = (q.get("id") or [""])[0]
            return self._json({"id": cid, "prompt": state.critic_prompt(cid)})
        if path == "/api/judges":
            return self._json({"critics": state.critics(include_retired=True),
                               "charter": state.critic_charter(),
                               "requests": state.critic_requests(),
                               "reviews": state.critic_reviews()})
        if path == "/api/field-guide":
            return self._json({"guide": state.field_guide(),
                               "requests": state.field_guide_requests()})
        if path == "/api/judge_review":             # Case 555: one case's FULL rulings
            case = (q.get("case") or [""])[0]
            if (q.get("part") or [""])[0] == "prompt":
                rnd = (q.get("round") or [""])[0]
                return self._json({"case": case, "round": rnd,
                                   "prompt": state.critic_review_prompt(case, rnd)})
            return self._json(state.critic_review_detail(case))
        if path == "/jtf":
            return self._html(pages.jtf_page())
        if path == "/docket":                      # Case 761
            return self._html(pages.docket_page())
        if path == "/api/docket":
            return self._json(state.docket())
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
        if path == "/api/deputy/settings":
            # Case 591: the live settings the switch / relaunch modal prefills
            # with. On demand only — the modal opens far less often than the 8 s
            # status poll, and this shells out per deputy.
            return self._deputy_settings((q.get("name") or [""])[0])
        if path == "/api/deputy/message":
            # Case 616: what the reply / follow-up dialog prefills with — is this
            # deputy live (so the message interrupts it) or ended (so it is
            # revived), and is it still on the case the button was pressed for.
            # Read-only; the CLI's `show` writes nothing.
            return self._deputy_message_settings(
                (q.get("name") or [""])[0], (q.get("case") or [""])[0])
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
        if path == "/api/agents":
            qs = (q.get("q") or [""])[0]
            return self._json(jtf.agent_index(qs))
        if path == "/download":
            return self._download((q.get("path") or [""])[0])
        return self._json({"error": "not found"}, 404)

    # -- POST ---------------------------------------------------------------
    # Whitelisted set for persistent model writes.  The sheriff resolves the
    # service from the selected alias, so it can run either registered backend.
    _MODELS = tuple(models.ALL_ALIASES)
    # Case 561 (Feng uid=676): a PRECINCT's default may be any registered model, so
    # that "each precinct has a default service and model" is actually expressible —
    # the alias is globally unique, so naming the model names the service. The
    _PRECINCT_MODELS = tuple(models.ALL_ALIASES)

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
        # Case 551: PROPOSE a new judge. Deliberately does NOT write the critic
        # registry -- it files a critic_add request on the sheriff queue (the
        # registry is sheriff-owned), so the dashboard stays a proposer.
        if u.path == "/judges/propose":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._propose_critic()
        # Case 685: an authenticated operator can ask the sheriff to review one
        # Field Guide category.  This queues a request only; it never writes the
        # sheriff-owned standing text or launches a model on the web request.
        if u.path == "/api/field-guide/rewrite":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._field_guide_rewrite()
        # Case 782: the Field Guide's request box. Opens a RECEPTIONIST case saying
        # what the operator wants changed; it writes no policy of its own.
        if u.path == "/api/field-guide/message":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._field_guide_message()
        # Case 591: the Status board's per-deputy buttons -- switch a live deputy's
        # model, or relaunch one the watchdog has given up on. Writes an order file
        # and returns; the WATCHDOG carries it out on its next tick, so nothing on
        # this path calls Claude or spawns anything.
        if u.path == "/api/deputy/control":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._deputy_control()
        # Case 616: the Status board's reply button and the precinct case log's
        # follow-up button. Unlike the control orders above this one does NOT go
        # through the watchdog: it is the e-mail interrupt path, so it appends to
        # the deputy's mailbox and relaunches it there and then, and the request
        # blocks until the deputy is confirmed alive again (a few seconds).
        if u.path == "/api/deputy/message":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._deputy_message()
        # Case 760: add a collaborator to a LIVE JTF. Like the reply button this is
        # the message path, not a watchdog order -- it hands the LEAD an onboarding
        # step and blocks until the lead has it. The new deputy is NOT spawned here:
        # the note travels through every collaborator first (scratch_jtf_onboard.py).
        if u.path == "/api/jtf/collaborator":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._jtf_collaborator()
        # Case 761: create / edit / pause / resume / cancel a DOCKET. The dashboard
        # neither schedules nor fires anything -- it hands the submission to the
        # tsomp scratch_docket.py CLI, which owns the store and every validation
        # rule (precinct, service, model, judge, schedule), and relays its answer.
        if u.path == "/api/docket":
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            return self._docket_write()
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
        if name in known and model in self._PRECINCT_MODELS:
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
            # An authenticated dashboard is the explicit operator control plane for
            # this one global setting.  Pass the sheriff capability to its short
            # lived records-manager child rather than weakening deputy authorization.
            try:
                env["TSOMP_SHERIFF_TOKEN"] = (config.RECORDS / ".sheriff_token").read_text().strip()
            except OSError:
                return self._redirect("/precincts")
            try:
                subprocess.run(
                    [sys.executable, str(config.STATE_ROOT / "scratch_records.py"),
                     "sheriff", "model", "--set", model, "--role", "sheriff"],
                    cwd=str(config.STATE_ROOT), env=env, timeout=30,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            except Exception as ex:
                sys.stderr.write(f"[infra-dash] set sheriff model failed: {ex}\n")
        return self._redirect("/precincts")

    # -- Case 591: operator orders on a deputy -------------------------------
    _CONTROL_CLI = "scratch_watchdog_control.py"
    _CONTROL_OPS = ("switch", "relaunch", "terminate", "cancel")

    def _control(self, op, name, to=None, report_to=None):
        """Run one order through the tsomp control CLI and return its JSON.

        Shelling out rather than importing is the choice `/precinct/model` already
        made: the CLI owns the validation, the roster lookup and the record
        format, so the dashboard can only ask for what it is allowed to ask for
        and there is one implementation to keep correct.
        """
        who = (auth.account().get("name") or auth.account().get("email") or "").strip()
        cmd = [sys.executable, str(config.STATE_ROOT / self._CONTROL_CLI), op,
               "--worker", name, "--json",
               "--by", f"dashboard ({who})" if who else "dashboard"]
        if to:
            cmd += ["--to", to]
        if report_to:
            cmd += ["--report-to", report_to]
        # Force the LIVE state by dropping any TSOMP_* override the server
        # inherited — a test root here would file the order where nothing reads it.
        env = {k: v for k, v in os.environ.items()
               if k not in ("TSOMP_RECORDS_ROOT", "TSOMP_WATCHDOG_JOBS",
                            "TSOMP_JOBS_DIR")}
        try:
            p = subprocess.run(cmd, cwd=str(config.STATE_ROOT), env=env,
                               timeout=60, capture_output=True, text=True)
            return json.loads(p.stdout)
        except Exception as ex:
            sys.stderr.write(f"[infra-dash] {self._CONTROL_CLI} {op} failed: {ex}\n")
            return {"ok": False, "error": f"could not run {self._CONTROL_CLI}: {ex}"}

    def _deputy_settings(self, name):
        """GET side: the live settings the modal prefills with. Read-only — the
        CLI's `show` writes nothing."""
        if not _VALID_WORKER.match(name or ""):
            return self._json({"ok": False, "error": "bad worker name"}, 400)
        r = self._control("show", name)
        return self._json(r, 200 if r.get("ok") else 400)

    def _deputy_control(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 8192:
            return self._json({"ok": False, "error": "body too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
        except Exception:
            return self._json({"ok": False, "error": "expected a JSON body"}, 400)
        op = str(body.get("op") or "").strip()
        name = str(body.get("name") or "").strip()
        if op not in self._CONTROL_OPS:
            return self._json({"ok": False, "error": f"unknown op {op!r}"}, 400)
        if not _VALID_WORKER.match(name):
            return self._json({"ok": False, "error": "bad worker name"}, 400)
        r = self._control(op, name,
                          to=str(body.get("to") or "").strip() or None,
                          report_to=str(body.get("report_to") or "").strip() or None)
        return self._json(r, 200 if r.get("ok") else 400)

    # -- Case 616: an operator message to a deputy ---------------------------
    _MESSAGE_CLI = "scratch_deputy_message.py"
    # Long enough for a real instruction, short enough that nobody pastes a
    # design document into a mailbox. The CLI enforces its own limit too.
    _MAX_MESSAGE = 20000

    def _message_cli(self, op, name, case="", message=None, source=""):
        """Run one delivery through the tsomp CLI and return its JSON.

        The message goes in through a FILE, not argv: it is free text the operator
        typed, and argv is visible in `ps` — which is also how a worker once killed
        itself, by pattern-matching its own command line (rc=143, Task 176).
        """
        who = (auth.account().get("name") or auth.account().get("email") or "").strip()
        cmd = [sys.executable, str(config.STATE_ROOT / self._MESSAGE_CLI), op,
               "--worker", name, "--json", "--by", who or "the operator"]
        if case:
            cmd += ["--case", str(case)]
        if source:
            cmd += ["--source", source]
        env = {k: v for k, v in os.environ.items()
               if k not in ("TSOMP_RECORDS_ROOT", "TSOMP_WATCHDOG_JOBS",
                            "TSOMP_JOBS_DIR")}
        tmp = None
        try:
            if message is not None:
                fd, tmp = tempfile.mkstemp(prefix="dashmsg_", suffix=".txt")
                with os.fdopen(fd, "w") as f:
                    f.write(message)
                cmd += ["--message-file", tmp]
            p = subprocess.run(cmd, cwd=str(config.STATE_ROOT), env=env,
                               timeout=180, capture_output=True, text=True)
            return json.loads(p.stdout)
        except Exception as ex:
            sys.stderr.write(f"[infra-dash] {self._MESSAGE_CLI} {op} failed: {ex}\n")
            return {"ok": False, "error": f"could not run {self._MESSAGE_CLI}: {ex}"}
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- Case 760: add a collaborator to a running JTF -----------------------
    _ONBOARD_CLI = "scratch_jtf_onboard.py"

    @staticmethod
    def _valid_collab_request(body, known):
        """Validate an add-collaborator submission -> (request, error).

        A pure function for the same reason _valid_slot is one: the refusals are
        the interesting behaviour and a test should be able to drive them without
        standing up a request. An unusable work-split value is REFUSED rather than
        dropped (the Case-551 rule) -- a silently ignored model choice is the
        failure mode this precinct keeps re-learning.
        """
        jtf_id = str(body.get("jtf") or "").strip()
        precinct = str(body.get("precinct") or "").strip()
        msg = str(body.get("message") or "").strip()
        if not re.fullmatch(r"[0-9a-zA-Z]{1,16}", jtf_id):
            return None, "bad JTF id"
        if precinct not in known:
            return None, f"unknown precinct '{precinct}'"
        if not msg:
            return None, ("a message for the lead is required -- it is what tells "
                          "the lead why this collaborator is joining")
        lanes = {}
        for f in ("service", "model", "report_service", "report_model"):
            v = str(body.get(f) or "").strip().lower()
            if not v:
                continue
            ok = (v in (models.MODES if f == "service" else models.SERVICE_IDS)
                  if f.endswith("service") else v in models.ALL_ALIASES)
            if not ok:
                return None, f"invalid {f} '{v}'"
            lanes[f] = v
        if (lanes.get("model") and lanes.get("service")
                and models.service_of(lanes["model"]) != lanes["service"]):
            return None, (f"model '{lanes['model']}' does not belong to service "
                          f"'{lanes['service']}'")
        return {"jtf": jtf_id, "precinct": precinct, "message": msg,
                "lanes": lanes}, None

    def _jtf_collaborator(self):
        """Start an onboarding chain: the LEAD is asked to write the note, every
        collaborator adds to it, and only then is the new deputy spawned.

        The same shell-out contract as the two buttons above -- the CLI owns the
        validation, the chain state and the delivery, so a malformed request is
        refused by the one implementation rather than by a copy living here.
        """
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > self._MAX_MESSAGE + 4096:
            return self._json({"ok": False, "error": "message too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
        except Exception:
            return self._json({"ok": False, "error": "expected a JSON body"}, 400)
        ask, err = self._valid_collab_request(body, {p["name"] for p in state.precincts()})
        if err:
            return self._json({"ok": False, "error": err}, 400)
        jtf_id, precinct, lanes = ask["jtf"], ask["precinct"], ask["lanes"]
        msg = ask["message"]
        who = (auth.account().get("name") or auth.account().get("email") or "").strip()
        cmd = [sys.executable, str(config.STATE_ROOT / self._ONBOARD_CLI), "request",
               "--jtf", jtf_id, "--precinct", precinct,
               "--by", who or "the operator"]
        for f, v in lanes.items():
            cmd += [f"--{f.replace('_', '-')}", v]
        env = {k: v for k, v in os.environ.items()
               if k not in ("TSOMP_RECORDS_ROOT", "TSOMP_WATCHDOG_JOBS",
                            "TSOMP_JOBS_DIR", "TSOMP_JTF_ROOT")}
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="jtfonboard_", suffix=".txt")
            with os.fdopen(fd, "w") as f:
                f.write(msg)
            cmd += ["--message-file", tmp]
            p = subprocess.run(cmd, cwd=str(config.STATE_ROOT), env=env,
                               timeout=180, capture_output=True, text=True)
            r = json.loads(p.stdout)
        except Exception as ex:
            sys.stderr.write(f"[infra-dash] {self._ONBOARD_CLI} request failed: {ex}\n")
            r = {"ok": False, "error": f"could not run {self._ONBOARD_CLI}: {ex}"}
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return self._json(r, 200 if r.get("ok") else 400)

    def _deputy_message_settings(self, name, case=""):
        if not _VALID_WORKER.match(name or ""):
            return self._json({"ok": False, "error": "bad worker name"}, 400)
        r = self._message_cli("show", name, case=case)
        return self._json(r, 200 if r.get("ok") else 400)

    def _deputy_message(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > self._MAX_MESSAGE + 4096:
            return self._json({"ok": False, "error": "message too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
        except Exception:
            return self._json({"ok": False, "error": "expected a JSON body"}, 400)
        name = str(body.get("name") or "").strip()
        msg = str(body.get("message") or "").strip()
        case = str(body.get("case") or "").strip()
        source = str(body.get("source") or "").strip()
        if not _VALID_WORKER.match(name):
            return self._json({"ok": False, "error": "bad worker name"}, 400)
        if not msg:
            return self._json({"ok": False, "error": "the message is empty"}, 400)
        if len(msg) > self._MAX_MESSAGE:
            return self._json({"ok": False,
                               "error": f"message is {len(msg)} characters; the "
                                        f"limit is {self._MAX_MESSAGE}"}, 400)
        if case and not re.match(r"^[A-Za-z0-9_-]{1,24}$", case):
            return self._json({"ok": False, "error": "bad case id"}, 400)
        if source not in ("", "status", "precinct"):
            return self._json({"ok": False, "error": "bad source"}, 400)
        r = self._message_cli("send", name, case=case, message=msg, source=source)
        return self._json(r, 200 if r.get("ok") else 400)

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
        the live set, and the model/service against the registry. The dashboard never
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
        service = (fields.get("service") or "").strip().lower()   # Case 557
        writer_model = (fields.get("writer_model") or "").strip().lower()  # Case 557 uid=671
        parent = (fields.get("parent") or "").strip()
        description = (fields.get("description") or "").strip()
        critic = (fields.get("critic") or "").strip().lower()      # Case 551
        # Case 561: the work split's REPORT lane + the judge's own service/model.
        report_model = (fields.get("report_model") or "").strip().lower()
        report_service = (fields.get("report_service") or "").strip().lower()
        judge_model = (fields.get("judge_model") or "").strip().lower()
        judge_service = (fields.get("judge_service") or "").strip().lower()

        known = {p["name"] for p in state.precincts()}
        if precinct not in known:
            return self._json({"ok": False, "error": f"unknown precinct '{precinct}'"}, 400)
        # Case 557: a CASE may pick either service, so this path validates against
        # the full cross-service registry, shared by all persisted model paths.
        if model and model not in models.ALL_ALIASES:
            return self._json({"ok": False, "error": f"invalid model '{model}'"}, 400)
        # Case 557: the "service" field carries a MODE ("claude" / "claude+chatgpt" /
        # "chatgpt") -- who does the work and who writes the report -- so it validates
        # against MODE_IDS, not the raw SERVICE_IDS. The two single-agent mode ids are
        # identical to their service ids, so a pre-mode value still passes.
        if service and service not in models.MODE_IDS:
            return self._json({"ok": False, "error": f"invalid service '{service}'"}, 400)
        # Case 557 (uid=671): the hybrid WRITER's model. Validated against the
        # full registry here; the bridge additionally checks it belongs to the
        # mode's writer service and drops it otherwise.
        if writer_model and writer_model not in models.ALL_ALIASES:
            return self._json({"ok": False, "error": f"invalid writer_model '{writer_model}'"}, 400)
        if parent and not parent.isdigit():
            return self._json({"ok": False, "error": "follow-up must be a task/case number"}, 400)
        if not description:
            return self._json({"ok": False, "error": "a task description is required"}, 400)
        # Case 551: only a LIVE judge may be selected. Reject an unknown id here
        # rather than letting the bridge silently degrade it to "no critic" -- the
        # user picked a judge and must be told if it did not take.
        if critic and critic not in {c["id"] for c in state.critics()}:
            return self._json({"ok": False, "error": f"unknown judge '{critic}'"}, 400)
        # Case 561: the report lane and the judge each get their own service+model.
        # Rejected here rather than silently dropped by the bridge -- the user made
        # a choice and must be told if it did not take (the Case 551 rule).
        # An EMPTY report_service means "same as work", which is the default and is
        # what makes an unsplit case behave exactly as it did before.
        for _lbl, _m, _s in (("report", report_model, report_service),
                             ("judge", judge_model, judge_service)):
            if _m and _m not in models.ALL_ALIASES:
                return self._json({"ok": False, "error": f"invalid {_lbl}_model '{_m}'"}, 400)
            if _s and _s not in models.SERVICE_IDS:
                return self._json({"ok": False, "error": f"invalid {_lbl}_service '{_s}'"}, 400)
            if _m and _s and models.service_of(_m) != _s:
                return self._json({"ok": False, "error":
                                   f"{_lbl} model '{_m}' does not belong to service '{_s}'"}, 400)

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
                  "model": model or None, "service": service or None,
                  "writer_model": writer_model or None,   # Case 557 uid=671
                  "parent": parent or None,
                  "description": description, "files": saved, "case": case,
                  "critic": critic or None,
                  # Case 561: the work split (work lane = model/service above) and
                  # the judge's own service+model. None = inherit / registry default.
                  "report_model": report_model or None,
                  "report_service": report_service or None,
                  "judge_model": judge_model or None,
                  "judge_service": judge_service or None,
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
        return self._json({"ok": True, "case": case, "sid": sid, "critic": critic or "",
                           "files": len(saved), "note": note})

    # -- authed write: PROPOSE a critic ("judge") (Case 551; rewritten Case 569) --
    _JUDGE_RESERVED = ("charter", "none", "no", "off", "false", "0", "default")

    @staticmethod
    def _judge_id_from_name(name):
        """Derive a judge id from the NAME the user typed (Case 569: one field, not
        an id AND a display name -- the two were the same answer asked twice).

        Drops a leading article ("The Vyas Judge" -> vyas) and the trailing noun
        ("... Judge"/"Critic"/"Reviewer"), because those words are in every name
        and carry no distinguishing information. Whatever survives is slugged."""
        words = re.findall(r"[A-Za-z0-9]+", str(name or ""))
        words = [w.lower() for w in words]
        while words and words[0] in ("the", "a", "an"):
            words.pop(0)
        while len(words) > 1 and words[-1] in ("judge", "critic", "reviewer", "review"):
            words.pop()
        cid = "_".join(words)[:32].strip("_-")
        if cid and not cid[0].isalpha():        # the id must start with a letter
            cid = "j_" + cid[:30]
        return cid

    def _propose_critic(self):
        """Open a RECEPTIONIST CASE to write a new judge's prompt (Case 569).

        This used to file the critic_add itself, from a prompt the user typed into
        the form. It no longer does: the form now collects a name and a
        plain-language description of what the judge should care about, and this
        handler turns that into a case. The deputy reads the charter and the live
        personas, writes the prompt, writes the roster description and the sheriff
        reason, and files the critic_add. The sheriff still decides every one.

        The dashboard still spawns nothing -- it drops a web-case record for the
        inbox-side bridge, exactly like the 'Create new case' form."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > config.WEB_CASE_MAX_BODY:
            return self._json({"ok": False, "error": "bad request size"}, 400)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "invalid payload"}, 400)

        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        model = str(payload.get("model") or "").strip().lower()
        if len(name) < 2:
            return self._json({"ok": False, "error": "give the judge a name"}, 400)
        if len(description) < 40:
            return self._json({"ok": False, "error":
                               "describe what this judge should care about — a sentence or "
                               "two at minimum, or the deputy has nothing to write from"}, 400)
        # Case 561: a judge may run on either service, so validate against the full
        # cross-service registry shared by the precinct and sheriff write paths.
        if model and model not in models.ALL_ALIASES:
            return self._json({"ok": False, "error": f"invalid model '{model}'"}, 400)

        cid = self._judge_id_from_name(name)
        if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", cid):
            return self._json({"ok": False, "error":
                               "could not make an id from that name — use letters and "
                               "digits (e.g. 'The Reproducibility Judge')"}, 400)
        if cid in self._JUDGE_RESERVED:
            return self._json({"ok": False, "error": f"'{cid}' is a reserved id"}, 400)
        # Taken ids are rejected HERE rather than left for the deputy to discover:
        # the user would otherwise wait for a whole case to be told the name clashes.
        # Retired judges count as taken (the registry keeps them, so the id is not free).
        taken = set()
        with contextlib.suppress(Exception):
            taken = {c["id"] for c in state.critics(include_retired=True)}
        if cid in taken:
            return self._json({"ok": False, "error":
                               f"a judge with id '{cid}' already exists — pick another name"}, 400)

        precinct = "receptionist"
        if precinct not in {p["name"] for p in state.precincts()}:
            return self._json({"ok": False, "error":
                               "the receptionist precinct is not registered"}, 500)

        sid = time.strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
        case = self._allocate_case()
        record = {"id": sid, "ts": time.time(), "precinct": precinct,
                  "model": None, "service": None, "parent": None,
                  "description": description, "files": [], "case": case,
                  "critic": None,
                  "judge_proposal": {"id": cid, "name": name, "model": model or ""},
                  "deputy_hint": f"judge_{case}",
                  "source": "web_judge", "requester": config.OPERATOR.get("email") or ""}
        try:
            pend = config.WEB_CASES / "pending"
            pend.mkdir(parents=True, exist_ok=True)
            tmp = pend / (sid + ".json.tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, pend / (sid + ".json"))
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not queue submission: {ex}"}, 500)
        return self._json({"ok": True, "case": case, "id": cid, "sid": sid,
                           "note": "opened as a receptionist case; the deputy writes the "
                                   "prompt and files it with the sheriff."})

    # -- Case 685: ask the sheriff to review a Field Guide category ---------
    _FIELD_GUIDE_MAX_BODY = 4096

    def _field_guide_rewrite(self):
        """Queue, but never perform, one operator-triggered Field Guide review.

        The field-guide CLI owns category validation, request coalescing, and the
        sheriff-request record format. This thin authenticated wrapper keeps the
        dashboard from becoming a second writer for standing policy or a web
        endpoint that directly invokes the sheriff model.
        """
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > self._FIELD_GUIDE_MAX_BODY:
            return self._json({"ok": False, "error": "bad request size"}, 400)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "invalid payload"}, 400)
        category = str(payload.get("category") or "").strip().lower()
        guide = state.field_guide()
        known = {str(row.get("id")) for row in guide.get("categories") or []
                 if isinstance(row, dict)}
        if category not in known:
            return self._json({"ok": False, "error": "unknown Field Guide category"}, 400)
        requester = (auth.account().get("email") or config.OPERATOR.get("email") or "").strip()
        if not requester:
            return self._json({"ok": False,
                               "error": "the dashboard has no operator email for a sheriff request"}, 500)
        cmd = [sys.executable, str(config.STATE_ROOT / "scratch_field_guide.py"), "rewrite",
               "--category", category, "--origin", "receptionist", "--requester", requester,
               "--precinct", "infra"]
        env = {k: v for k, v in os.environ.items() if k != "TSOMP_RECORDS_ROOT"}
        try:
            proc = subprocess.run(cmd, cwd=str(config.STATE_ROOT), env=env, timeout=30,
                                  capture_output=True, text=True)
            data = json.loads(proc.stdout or "{}")
            if proc.returncode or not isinstance(data, dict):
                raise ValueError((proc.stderr or "invalid Field Guide response").strip())
        except Exception as exc:
            sys.stderr.write(f"[infra-dash] Field Guide request failed: {exc}\n")
            return self._json({"ok": False, "error": "could not queue sheriff review"}, 500)
        return self._json({"ok": True, "category": category, "request_id": data.get("id"),
                           "coalesced": bool(data.get("coalesced")),
                           "note": "queued for sheriff review"})

    # -- Case 782: ask the receptionist for a Field Guide change -----------
    _FIELD_GUIDE_MSG_MAX_BODY = 16384
    _FIELD_GUIDE_MSG_MAX_TEXT = 6000

    def _field_guide_message(self):
        """Open one receptionist case describing a Field Guide change the operator wants.

        This is the Case-569 'propose a judge' path applied to standing policy: the
        dashboard states a wish and lets a deputy do the work, rather than becoming a
        second writer for text the sheriff owns. A brand-new section is deliberately
        allowed here even though no CLI can create one -- the category list is a closed
        tuple in scratch_field_guide.py, so a new section is a code change, and the
        spec below says so instead of the request failing with nowhere to go.
        """
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > self._FIELD_GUIDE_MSG_MAX_BODY:
            return self._json({"ok": False, "error": "bad request size"}, 400)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "invalid payload"}, 400)

        text = str(payload.get("text") or "").strip()
        if len(text) < 20:
            return self._json({"ok": False, "error":
                               "describe the change in a sentence or two \u2014 a deputy has "
                               "to work from it"}, 400)
        if len(text) > self._FIELD_GUIDE_MSG_MAX_TEXT:
            return self._json({"ok": False, "error": "that request is too long"}, 400)

        target = str(payload.get("target") or "new").strip().lower()
        known = {}
        with contextlib.suppress(Exception):
            known = {str(row.get("id")): str(row.get("label") or row.get("id"))
                     for row in (state.field_guide().get("categories") or [])
                     if isinstance(row, dict)}
        if target != "new" and target not in known:
            return self._json({"ok": False, "error": "unknown Field Guide section"}, 400)

        if target == "new":
            scope = ("Steven wants a **new Field Guide section**. Note before you plan: the "
                     "category list is a closed tuple (`CATEGORIES`) in "
                     "`scratch_field_guide.py`, so a new section is a CODE change as well as "
                     "a standing-text change \u2014 it cannot be added by a sheriff request "
                     "alone. " + ("Existing sections are: " + ", ".join(sorted(known)) + "."
                                  if known else
                                  "The Field Guide could not be read just now, so check "
                                  "which sections already exist before you plan."))
        else:
            scope = (f"Steven wants the **{known[target]}** section of the Field Guide changed "
                     f"(category id `{target}`). Its standing text is sheriff-owned: read the "
                     f"current wording with `python scratch_field_guide.py show --category "
                     f"{target}` before you propose anything.")

        description = (
            "Field Guide change request, submitted from the dashboard's **Field Guide** tab.\n\n"
            + scope
            + "\n\n## What Steven asked for (verbatim from the form)\n\n"
            + text
            + "\n\n## Your job\n\n"
              "Work out what this change actually requires, then take it there. The sheriff "
              "owns every word of standing policy, so the standing-text part of it is a "
              "sheriff request (`python scratch_field_guide.py request --category <c> "
              "--reason ...`), quoting the wording you want clause by clause \u2014 that "
              "command takes only `--reason`, so wording you do not quote is wording the "
              "sheriff will invent for you. If the request needs anything the Field Guide "
              "cannot express, say so to Steven rather than approximating it. Check whether "
              "the live **vyas** judge enforces the same rule you are changing "
              "(`scratch_full_logs/records/critics/vyas.md`): a guide edit the judge "
              "contradicts is an inert policy."
        )

        precinct = "receptionist"
        if precinct not in {p["name"] for p in state.precincts()}:
            return self._json({"ok": False, "error":
                               "the receptionist precinct is not registered"}, 500)

        sid = time.strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
        case = self._allocate_case()
        record = {"id": sid, "ts": time.time(), "precinct": precinct,
                  "model": None, "service": None, "parent": None,
                  "description": description, "files": [], "case": case,
                  "critic": None,
                  "deputy_hint": f"guide_{case}" if case else "",
                  "source": "web_field_guide",
                  "requester": (auth.account().get("email")
                                or config.OPERATOR.get("email") or "")}
        try:
            pend = config.WEB_CASES / "pending"
            pend.mkdir(parents=True, exist_ok=True)
            tmp = pend / (sid + ".json.tmp")
            tmp.write_text(json.dumps(record, indent=2))
            os.replace(tmp, pend / (sid + ".json"))
        except Exception as ex:
            return self._json({"ok": False, "error": f"could not queue submission: {ex}"}, 500)
        return self._json({"ok": True, "case": case, "sid": sid, "target": target,
                           "note": "opened as a receptionist case"})

    # -- authed write: create a JTF (Joint Task Force) (Case 384e) -----------
    @staticmethod
    def _valid_slot(slot, known):
        """Normalize + validate ONE slot to {"kind","name",...} or None. A precinct
        slot's name must be a live precinct; a deputy slot just needs a non-empty name
        (the must-take machinery no-ops safely if the deputy can't be revived).

        Case 599: a PRECINCT slot may also carry its own WORK SPLIT — service/model for
        the work lane and report_service/report_model for the report lane — exactly the
        four fields the create-case form collects, because a precinct slot IS a fresh
        case in that precinct. Validated here rather than silently dropped by the
        bridge: the user made a choice and must be told if it did not take (the Case 551
        rule). All four are optional; empty = the precinct default / "same as work",
        which is the pre-599 behaviour byte for byte.

        A DEPUTY slot carries none of them — it is a live agent that keeps its own
        configured model, so accepting a model there would be a promise nothing
        fulfils. An explicit model on a deputy slot is REJECTED, not ignored."""
        if not isinstance(slot, dict):
            return None
        kind = str(slot.get("kind", "")).strip().lower()
        name = str(slot.get("name", "")).strip()
        if kind not in ("precinct", "deputy") or not name:
            return None
        if kind == "precinct" and name not in known:
            return None
        out = {"kind": kind, "name": name}
        lanes = {k: str(slot.get(k) or "").strip().lower()
                 for k in ("service", "model", "report_service", "report_model")}
        if kind == "deputy":
            return None if any(lanes.values()) else out
        # work lane: `service` is a MODE id here for the same reason the create-case
        # form posts one (the two single-agent mode ids ARE the service ids).
        if lanes["service"] and lanes["service"] not in models.MODE_IDS:
            return None
        if lanes["report_service"] and lanes["report_service"] not in models.SERVICE_IDS:
            return None
        for mk, sk in (("model", "service"), ("report_model", "report_service")):
            m, s = lanes[mk], lanes[sk]
            if m and m not in models.ALL_ALIASES:
                return None
            # a model must belong to the service it is paired with. The work lane's
            # `service` is a mode, so compare against the mode's DEPUTY service.
            svc = models.deputy_service(s) if (s and sk == "service") else s
            if m and svc and models.service_of(m) != svc:
                return None
        out.update({k: v for k, v in lanes.items() if v})
        return out

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
        # Case 599 (uid=770): the judge's own service+model, as on the create-case
        # form. Rejected rather than silently dropped (the Case 551 rule); empty =
        # the judge's registered default, which is the pre-599 behaviour.
        judge_model = str(payload.get("judge_model") or "").strip().lower()
        judge_service = str(payload.get("judge_service") or "").strip().lower()
        if judge_model and judge_model not in models.ALL_ALIASES:
            return self._json({"ok": False, "error": f"invalid judge_model '{judge_model}'"}, 400)
        if judge_service and judge_service not in models.SERVICE_IDS:
            return self._json({"ok": False,
                               "error": f"invalid judge_service '{judge_service}'"}, 400)
        if judge_model and judge_service and models.service_of(judge_model) != judge_service:
            return self._json({"ok": False, "error":
                               f"judge model '{judge_model}' does not belong to "
                               f"service '{judge_service}'"}, 400)

        sid = time.strftime("%Y%m%d%H%M%S") + "_" + os.urandom(4).hex()
        record = {"id": sid, "ts": time.time(), "lead": lead, "collaborators": collabs,
                  "judge_model": judge_model or None,
                  "judge_service": judge_service or None,
                  # Case 551: a JTF names WHICH judge signs its deliverables off.
                  # Legacy bools still work (the bridge maps true -> default critic).
                  "critic": (str(payload.get("critic") or "").strip().lower()
                             if not isinstance(payload.get("critic"), bool)
                             else bool(payload.get("critic"))),
                  "description": description,
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

    # -- authed write: DOCKET (Case 761) -----------------------------------
    _DOCKET_OPS = ("save", "pause", "resume", "cancel", "run-now")

    def _docket_write(self):
        """Run one entry operation through the owning CLI and relay its verdict.

        Deliberately thin: duplicating scratch_docket.py's validation here would
        give the operator two answers to the same question and let them drift. This
        checks only what the subprocess cannot be asked to check cheaply -- the body
        is small, the op is one we offer, and an id-op names an id."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return self._json({"ok": False, "error": "empty submission"}, 400)
        if n > config.DOCKET_MAX_BODY:
            return self._json({"ok": False, "error": "submission too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "expected a JSON object"}, 400)
        op = str(payload.get("op") or "").strip().lower()
        if op not in self._DOCKET_OPS:
            return self._json({"ok": False, "error": f"unknown op '{op}'"}, 400)

        cli = config.STATE_ROOT / "scratch_docket.py"
        if not cli.is_file():
            return self._json({"ok": False, "error": "the docket is unavailable"}, 503)
        env = {k: v for k, v in os.environ.items() if k != "TSOMP_RECORDS_ROOT"}
        if op == "save":
            entry = payload.get("entry")
            if not isinstance(entry, dict):
                return self._json({"ok": False, "error": "missing entry"}, 400)
            entry.setdefault("requester", config.OPERATOR.get("email") or "")
            entry["created_by"] = "dashboard"
            cmd, stdin = [sys.executable, str(cli), "save"], json.dumps(entry)
        else:
            pid = str(payload.get("id") or "").strip()
            if not pid:
                return self._json({"ok": False, "error": f"'{op}' needs a entry id"}, 400)
            cmd, stdin = [sys.executable, str(cli), op, "--id", pid], None
        try:
            proc = subprocess.run(cmd, input=stdin, cwd=str(config.STATE_ROOT), env=env,
                                  timeout=30, capture_output=True, text=True)
            answer = json.loads(proc.stdout or "{}")
        except Exception as ex:
            return self._json({"ok": False, "error": f"entry command failed: {ex}"}, 500)
        if not isinstance(answer, dict) or not answer.get("ok"):
            # the CLI's own message names the field the operator got wrong, so it is
            # relayed verbatim rather than flattened into a generic 400.
            error = (answer or {}).get("error") if isinstance(answer, dict) else ""
            return self._json({"ok": False, "error": error or "entry command failed"}, 400)
        state.invalidate("docket")
        return self._json(answer)

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
