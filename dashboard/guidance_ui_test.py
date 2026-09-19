#!/usr/bin/env python3
"""Offline dashboard tests for Case 685's Deputies' Field Guide.

The tests never start the server or invoke the live sheriff. They stub the
field-guide reader and request CLI, then exercise rendered HTML, route wiring,
and the authenticated queue-only handler.
Run: ``python guidance_ui_test.py``.
"""
import io
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import pages  # noqa: E402
import server  # noqa: E402
import state  # noqa: E402


def _fixture():
    cats = []
    for cid, label in [
        ("code", "Code"), ("report", "Report"), ("paper", "Paper"),
        ("figure", "Figure"), ("slides", "Slides"),
    ]:
        cats.append({
            "id": cid, "label": label, "guideline": f"Standing {label} standard.",
            "revision": 1, "pending_count": 0, "threshold": 10,
            "pending_lessons": [], "active_rewrite": None, "last_error": "",
        })
    cats[3]["pending_count"] = 1
    cats[3]["pending_lessons"] = [{
        "text": "Use <b>escaped</b> lesson text, never live HTML.", "deputy": "dep", "case": "685",
    }]
    cats[0]["guideline"] = "Use **clear** code."
    return {"title": "Deputies' Field Guide", "categories": cats, "history": []}


class FakeHandler(server.Handler):
    def __init__(self, payload=None, path="/api/field-guide/rewrite"):
        body = json.dumps(payload or {}).encode()
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self.path = path
        self.replied = None
        self.called = None

    def _json(self, obj, code=200):
        self.replied = (code, obj)
        return obj

    def _html(self, obj, code=200):
        self.replied = (code, obj)
        return obj

    def _authed(self):
        return True


class FakeProc:
    def __init__(self, stdout, stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _with_fixture(fn):
    old_guide, old_requests = state.field_guide, state.field_guide_requests
    state.field_guide = lambda: _fixture()
    state.field_guide_requests = lambda limit=30: []
    try:
        return fn()
    finally:
        state.field_guide, state.field_guide_requests = old_guide, old_requests


def test_page_covers_five_categories_and_is_read_only():
    def run():
        page = pages.field_guide_page()
        assert "href='/field-guide'" in page and "Field Guide" in page
        for label in ("Code", "Report", "Paper", "Figure", "Slides"):
            assert f">{label}<" in page, f"missing {label} category"
        assert "Standing Figure standard." in page
        assert "Use clear code." in page and "**clear**" not in page
        assert "&lt;b&gt;escaped&lt;/b&gt;" in page and "<b>escaped</b>" not in page
        assert "Pending lessons are shown here but are never injected" in page
        assert "class='small fgrewrite'" in page and "/api/field-guide/rewrite" in page
        # Case 782 replaced the old "no <textarea> anywhere" assertion. The page now
        # HAS one -- the receptionist request box -- so the rule it was really
        # protecting is asserted directly instead: the dashboard may state a wish
        # about standing policy, but it must never offer a way to EDIT that policy.
        assert page.count("<textarea") == 1, "only the receptionist request box may exist"
        assert "id='fgask'" in page and "contenteditable=" not in page
        # The box must be empty, not an editor pre-loaded with the standing text.
        box = page.split("id='fgask'", 1)[1].split("</textarea>", 1)[0]
        assert "Standing Figure standard." not in box and "Use clear code." not in box
        assert "sheriff owns the" in page.lower() or "sheriff owns" in page.lower()
    _with_fixture(run)


def test_standing_text_is_folded_behind_an_expander():
    """Case 782: each category opens FOLDED. The guidance still renders -- it just
    sits inside its own <details>, which is closed unless it carries `open`."""
    def run():
        page = pages.field_guide_page()
        for label in ("Code", "Report", "Paper", "Figure", "Slides"):
            assert f"Read the standing {label} standard" in page, f"no expander for {label}"
        body = page.split("<h2>Deputies", 1)[1]
        opened = [d for d in body.split("<details")[1:] if d[:60].lstrip().startswith("open")]
        assert not opened, "a Field Guide section is expanded on load"
        # folded != dropped: the text is still on the page, inside the expander.
        assert "Standing Figure standard." in page
        guide_details = [d for d in body.split("<details") if "Read the standing Code" in d]
        assert len(guide_details) == 1 and "Use clear code." in guide_details[0]
        # A long pending lesson rebuilds the same wall, so it folds too.
        assert "Pending lessons (1/10)</summary>" in page
        lesson_block = [d for d in body.split("<details") if "Pending lessons (1/10)" in d]
        assert len(lesson_block) == 1 and "escaped" in lesson_block[0]
    _with_fixture(run)


def test_request_box_targets_new_and_existing_sections():
    def run():
        page = pages.field_guide_page()
        assert "Ask the receptionist to change the Field Guide" in page
        assert "/api/field-guide/message" in page
        assert "value='new'>add a new section" in page
        for cid, label in [("code", "Code"), ("slides", "Slides")]:
            assert f"value='{cid}'>change the {label} section" in page, f"no target for {cid}"
        assert "receptionist" in page.lower()
    _with_fixture(run)


def test_message_opens_a_receptionist_case_and_writes_no_policy(tmp=None):
    import tempfile
    recorded = {}
    old_guide, old_prec = state.field_guide, state.precincts
    old_account, old_cases = auth.account, server.config.WEB_CASES
    state.field_guide = lambda: _fixture()
    state.precincts = lambda: [{"name": "receptionist"}, {"name": "infra"}]
    auth.account = lambda: {"email": "operator@example.com"}
    with tempfile.TemporaryDirectory() as td:
        server.config.WEB_CASES = Path(td)
        try:
            h = FakeHandler({"target": "code", "text": "Say plainly that a deputy may not "
                                                       "leave a commented-out branch behind."},
                            path="/api/field-guide/message")
            h._allocate_case = lambda: 9001
            h._field_guide_message()
            code, body = h.replied
            assert code == 200 and body["ok"] is True and body["case"] == 9001
            files = sorted((Path(td) / "pending").glob("*.json"))
            assert len(files) == 1, "exactly one receptionist case record"
            rec = json.loads(files[0].read_text())
            recorded.update(rec)
        finally:
            state.field_guide, state.precincts = old_guide, old_prec
            auth.account, server.config.WEB_CASES = old_account, old_cases

    assert recorded["precinct"] == "receptionist", "must go to the receptionist"
    assert recorded["source"] == "web_field_guide" and recorded["case"] == 9001
    desc = recorded["description"]
    assert "commented-out branch" in desc, "the operator's own words must survive"
    assert "category id `code`" in desc and "scratch_field_guide.py show --category code" in desc
    # The record must not smuggle a policy write: no standing text, no sheriff role.
    assert "--role sheriff" not in desc and "ledger write" not in desc
    assert recorded["critic"] is None


def test_message_flags_that_a_new_section_needs_a_code_change():
    import tempfile
    old_guide, old_prec, old_account = state.field_guide, state.precincts, auth.account
    old_cases = server.config.WEB_CASES
    state.field_guide = lambda: _fixture()
    state.precincts = lambda: [{"name": "receptionist"}]
    auth.account = lambda: {"email": "operator@example.com"}
    with tempfile.TemporaryDirectory() as td:
        server.config.WEB_CASES = Path(td)
        try:
            h = FakeHandler({"target": "new", "text": "Add a Data section about documenting "
                                                      "provenance and units of a built dataset."},
                            path="/api/field-guide/message")
            h._allocate_case = lambda: 9002
            h._field_guide_message()
            assert h.replied[0] == 200 and h.replied[1]["target"] == "new"
            rec = json.loads(sorted((Path(td) / "pending").glob("*.json"))[0].read_text())
        finally:
            state.field_guide, state.precincts = old_guide, old_prec
            auth.account, server.config.WEB_CASES = old_account, old_cases
    desc = rec["description"]
    assert "CODE change" in desc, "a new section is not a sheriff request alone"
    assert "CATEGORIES" in desc and "scratch_field_guide.py" in desc
    assert "code, figure, paper, report, slides" in desc, "must list what already exists"


def test_message_rejects_junk_without_writing_anything():
    import tempfile
    old_guide, old_prec, old_account = state.field_guide, state.precincts, auth.account
    old_cases = server.config.WEB_CASES
    state.field_guide = lambda: _fixture()
    state.precincts = lambda: [{"name": "receptionist"}]
    auth.account = lambda: {"email": "operator@example.com"}
    with tempfile.TemporaryDirectory() as td:
        server.config.WEB_CASES = Path(td)
        try:
            for payload, why in [
                ({"target": "code", "text": "fix it"}, "too short to act on"),
                ({"target": "nope", "text": "a" * 40}, "unknown section"),
                ({"target": "code", "text": "a" * 6001}, "too long"),
            ]:
                h = FakeHandler(payload, path="/api/field-guide/message")
                h._allocate_case = lambda: 9003
                h._field_guide_message()
                assert h.replied[0] == 400, why
                assert not h.replied[1]["ok"]
            assert not list(Path(td).rglob("*.json")), "a rejected request wrote a case record"
        finally:
            state.field_guide, state.precincts = old_guide, old_prec
            auth.account, server.config.WEB_CASES = old_account, old_cases


def test_message_route_is_auth_gated_and_delegates():
    h = FakeHandler({"target": "code", "text": "x" * 40}, path="/api/field-guide/message")
    h._authed = lambda: False
    h.do_POST()
    assert h.replied[0] == 401 and not h.replied[1]["ok"]

    h2 = FakeHandler({"target": "code", "text": "x" * 40}, path="/api/field-guide/message")
    h2._field_guide_message = lambda: setattr(h2, "called", "message")
    h2.do_POST()
    assert h2.called == "message", "POST route did not dispatch to the receptionist handler"


def test_get_routes_are_wired():
    def run():
        h = FakeHandler(path="/field-guide")
        h.do_GET()
        assert h.replied and h.replied[0] == 200 and "Deputies&rsquo; Field Guide" in h.replied[1]
        api = FakeHandler(path="/api/field-guide")
        api.do_GET()
        assert api.replied and api.replied[0] == 200
        assert api.replied[1]["guide"]["categories"][0]["id"] == "code"
    _with_fixture(run)


def test_post_queues_cli_and_never_writes_a_guide():
    calls = []
    old_guide, old_run, old_account = state.field_guide, server.subprocess.run, auth.account
    state.field_guide = lambda: _fixture()
    auth.account = lambda: {"email": "operator@example.com"}
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc('{"id":"request-1","queued":true,"coalesced":false}')
    server.subprocess.run = fake_run
    try:
        h = FakeHandler({"category": "figure"})
        h._field_guide_rewrite()
        code, body = h.replied
        assert code == 200 and body["ok"] is True and body["request_id"] == "request-1"
        cmd = calls[0][0]
        assert "scratch_field_guide.py" in str(cmd[1]) and "rewrite" in cmd
        assert "scratch_sheriff.py" not in cmd and "standing.json" not in str(cmd)
        assert calls[0][1]["cwd"], "request CLI must run in the live state root"
    finally:
        state.field_guide, server.subprocess.run, auth.account = old_guide, old_run, old_account


def test_post_rejects_unknown_category_without_subprocess():
    old_guide, old_run = state.field_guide, server.subprocess.run
    state.field_guide = lambda: _fixture()
    server.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
    try:
        h = FakeHandler({"category": "registry"})
        h._field_guide_rewrite()
        assert h.replied[0] == 400 and not h.replied[1]["ok"]
    finally:
        state.field_guide, server.subprocess.run = old_guide, old_run


def test_post_route_is_auth_gated_and_delegates():
    h = FakeHandler({"category": "code"})
    h._authed = lambda: False
    h.do_POST()
    assert h.replied[0] == 401 and not h.replied[1]["ok"]

    h2 = FakeHandler({"category": "code"})
    h2._field_guide_rewrite = lambda: setattr(h2, "called", "rewrite")
    h2.do_POST()
    assert h2.called == "rewrite", "POST route did not dispatch to the queue-only handler"


def test_state_reader_uses_the_live_cli_and_validates_shape():
    old_run, old_root = state.subprocess.run, state.config.STATE_ROOT
    state._cache.pop("field_guide", None)
    try:
        # The reader first checks that the state root contains the CLI.  Point
        # it at this checkout; subprocess itself remains fully mocked.
        state.config.STATE_ROOT = Path(__file__).resolve().parents[2] / "time-series-omp"
        state.subprocess.run = lambda *a, **k: FakeProc(json.dumps(_fixture()))
        data = state.field_guide()
        assert len(data["categories"]) == 5 and data["categories"][3]["pending_count"] == 1
    finally:
        state.subprocess.run, state.config.STATE_ROOT = old_run, old_root
        state._cache.pop("field_guide", None)


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed.append(test.__name__)
            traceback.print_exc()
    print(f"SUMMARY: {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
