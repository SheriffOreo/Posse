#!/usr/bin/env python3
"""Offline dashboard tests for Case 761's Docket tab.

Nothing here starts the server or touches the live schedule: the entry reader and
the scratch_docket.py subprocess are stubbed, so the tests cover rendered HTML,
route wiring, and the authenticated delegate-only write handler.
Run: ``python docket_ui_test.py``.
"""
import io
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pages   # noqa: E402
import server  # noqa: E402
import state   # noqa: E402

CASE_DOCKET = {
    "id": "p20260919_aa", "name": "Nightly sweep", "kind": "case",
    "prompt": "Sweep the queue for {date}.", "freq": "daily", "at": "03:00",
    "schedule_label": "every day at 03:00", "next_run": 4102462800.0, "paused": False,
    "precinct": "infra", "service": "claude", "model": "opus",
    "report_service": "", "report_model": "", "judge": "", "judge_service": "",
    "judge_model": "", "runs": [{"ts": 1789000000, "status": "queued", "case": 900123}],
}
JTF_DOCKET = {
    "id": "p20260919_bb", "name": "Weekly slides", "kind": "jtf",
    "prompt": "Build the deck for week {d:%Y%m%d}.", "freq": "weekly", "at": "09:00",
    "dow": 5, "schedule_label": "every Saturday at 09:00", "next_run": None,
    "paused": True, "judge": "vyas", "judge_service": "claude", "judge_model": "opus",
    "lead": {"kind": "precinct", "name": "paper", "model": "fable"},
    "collaborators": [{"kind": "precinct", "name": "omp"},
                      {"kind": "deputy", "name": "web_600"}],
    "runs": [],
}


class FakeHandler(server.Handler):
    def __init__(self, payload=None, path="/api/docket"):
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


def _with_entries(fn, rows=None):
    old = state.docket
    state.docket = lambda: {"docket": rows if rows is not None
                             else [CASE_DOCKET, JTF_DOCKET]}
    try:
        return fn()
    finally:
        state.docket = old


def test_nav_offers_entries_on_every_page():
    nav = pages._nav("docket")
    assert "href='/docket'" in nav and ">Docket<" in nav
    assert "class='cur'>Docket" in nav, "the active tab is not marked"
    assert "href='/docket'" in pages._nav("status"), "the tab is missing elsewhere"


def test_page_shows_both_lists_and_every_requested_control():
    page = _with_entries(pages.docket_page)
    for needle in ["On the docket", "Off the docket", "id='pactive'", "id='poff'",
                   "id='pnew'", "id='composer'", "/api/docket", "/api/docket"]:
        assert needle in page, f"page missing {needle!r}"
    # the composer must collect everything the request named: prompt, judge,
    # work split, time, frequency, and both compositions.
    for needle in ["id='pprompt'", "id='pjudge'", "id='pjudge_service'",
                   "id='pjudge_model'", "id='ptime'", "id='pfreq'", "id='pdow'",
                   "id='pdom'", "id='pdate'", "id='pprecinct'", "id='pcasecfg'",
                   "id='plead-btn'", "id='pcollab-btn'", "id='pcollabs'"]:
        assert needle in page, f"composer missing {needle!r}"
    # the slot picker modal must be present, or the JTF lead button does nothing
    assert "id='pickmodal'" in page and "id='pickresults'" in page
    # per-entry buttons are built in JS; the page must carry their handlers
    for needle in ["'Edit'", "'Resume'", "'Pause'", "'Cancel'", "'Run now'",
                   "op:'save'", "'run-now'"]:
        assert needle in page, f"action missing {needle!r}"


def test_page_offers_every_precinct_judge_and_frequency():
    page = _with_entries(pages.docket_page)
    for precinct in state.precincts():
        assert f">{precinct['name']}<" in page, f"precinct {precinct['name']} not offered"
    for judge in state.critics():
        assert f"value='{judge['id']}'" in page, f"judge {judge['id']} not offered"
    for freq in ("once", "hourly", "daily", "weekly", "monthly"):
        assert f"value='{freq}'" in page, f"frequency {freq} not offered"


def test_page_never_interpolates_entry_text():
    """Names and prompts reach the DOM through textContent, so a entry carrying
    HTML cannot inject it. The rendered shell must contain no entry data at all."""
    hostile = dict(CASE_DOCKET, name="<img src=x onerror=alert(1)>",
                   prompt="<script>alert(2)</script>")
    page = _with_entries(pages.docket_page, rows=[hostile])
    assert "onerror=alert(1)" not in page and "alert(2)" not in page
    assert "Nightly sweep" not in page, "entry rows must not be server-rendered"


def test_get_routes_are_wired():
    def run():
        h = FakeHandler(path="/docket")
        h.do_GET()
        assert h.replied and h.replied[0] == 200 and "On the docket" in h.replied[1]
        api = FakeHandler(path="/api/docket")
        api.do_GET()
        assert api.replied and api.replied[0] == 200
        assert api.replied[1]["docket"][0]["id"] == "p20260919_aa"
    _with_entries(run)


def test_post_is_auth_gated_and_dispatches():
    h = FakeHandler({"op": "save", "entry": {}})
    h._authed = lambda: False
    h.do_POST()
    assert h.replied[0] == 401 and not h.replied[1]["ok"]

    h2 = FakeHandler({"op": "save", "entry": {}})
    h2._docket_write = lambda: setattr(h2, "called", "entry")
    h2.do_POST()
    assert h2.called == "entry", "POST /api/docket did not reach the handler"


def test_save_delegates_the_whole_spec_to_the_cli():
    calls = []
    old_run = server.subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc(json.dumps({"ok": True, "entry": CASE_DOCKET}))

    server.subprocess.run = fake_run
    try:
        spec = {"name": "Nightly sweep", "kind": "case", "prompt": "p",
                "freq": "daily", "at": "03:00", "precinct": "infra"}
        h = FakeHandler({"op": "save", "entry": spec})
        h._docket_write()
        code, body = h.replied
        assert code == 200 and body["ok"] is True
        cmd, kwargs = calls[0]
        assert "scratch_docket.py" in str(cmd[1]) and cmd[2] == "save"
        assert kwargs["cwd"], "the CLI must run in the live state root"
        sent = json.loads(kwargs["input"])
        assert sent["precinct"] == "infra" and sent["prompt"] == "p"
        assert sent["created_by"] == "dashboard"
    finally:
        server.subprocess.run = old_run


def test_pause_resume_cancel_pass_the_id():
    old_run = server.subprocess.run
    try:
        for op in ("pause", "resume", "cancel"):
            calls = []
            server.subprocess.run = lambda cmd, **kw: (
                calls.append(cmd) or FakeProc(json.dumps({"ok": True, "entry": {}})))
            h = FakeHandler({"op": op, "id": "p20260919_aa"})
            h._docket_write()
            assert h.replied[0] == 200, f"{op} failed: {h.replied}"
            assert calls[0][2] == op and calls[0][-1] == "p20260919_aa"
    finally:
        server.subprocess.run = old_run


def test_bad_requests_never_reach_the_cli():
    old_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not run the CLI"))
    try:
        for payload, why in (({"op": "delete", "id": "x"}, "unknown op"),
                             ({"op": "save"}, "save with no entry"),
                             ({"op": "pause"}, "pause with no id"),
                             ({"id": "x"}, "no op at all")):
            h = FakeHandler(payload)
            h._docket_write()
            assert h.replied[0] == 400 and not h.replied[1]["ok"], why
    finally:
        server.subprocess.run = old_run


def test_a_cli_rejection_is_relayed_verbatim():
    """The CLI's message names the field the operator got wrong; flattening it into
    a generic 400 would leave them guessing."""
    old_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: FakeProc(
        json.dumps({"ok": False, "error": "unknown precinct 'nosuch'"}), returncode=1)
    try:
        h = FakeHandler({"op": "save", "entry": {"name": "x"}})
        h._docket_write()
        assert h.replied[0] == 400
        assert h.replied[1]["error"] == "unknown precinct 'nosuch'"
    finally:
        server.subprocess.run = old_run


def test_a_crashed_cli_is_an_error_not_a_silent_success():
    old_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: FakeProc("not json at all", returncode=1)
    try:
        h = FakeHandler({"op": "pause", "id": "p1"})
        h._docket_write()
        assert h.replied[0] == 500 and not h.replied[1]["ok"]
    finally:
        server.subprocess.run = old_run


def test_state_reader_uses_the_live_cli_and_keeps_only_the_display_shape():
    old_run, old_root = state.subprocess.run, state.config.STATE_ROOT
    state._cache.pop("docket", None)
    try:
        state.config.STATE_ROOT = Path(__file__).resolve().parents[2] / "time-series-omp"
        state.subprocess.run = lambda *a, **k: FakeProc(json.dumps(
            {"ok": True, "docket": [dict(CASE_DOCKET, secret="do not render"),
                                     JTF_DOCKET,
                                     {"no": "id"}]}))
        data = state.docket()
        assert len(data["docket"]) == 2, "a row without an id must be dropped"
        first = data["docket"][0]
        assert "secret" not in first, "unknown keys must not reach the page"
        assert first["precinct"] == "infra" and first["model"] == "opus"
        jtf = data["docket"][1]
        assert jtf["lead"]["name"] == "paper" and len(jtf["collaborators"]) == 2
        assert jtf["collaborators"][1]["kind"] == "deputy"
    finally:
        state.subprocess.run, state.config.STATE_ROOT = old_run, old_root
        state._cache.pop("docket", None)


def test_state_reader_degrades_when_the_cli_fails():
    old_run, old_root = state.subprocess.run, state.config.STATE_ROOT
    state._cache.pop("docket", None)
    try:
        state.config.STATE_ROOT = Path(__file__).resolve().parents[2] / "time-series-omp"
        state.subprocess.run = lambda *a, **k: FakeProc("", "boom", returncode=1)
        data = state.docket()
        assert data["docket"] == [] and data.get("error")
    finally:
        state.subprocess.run, state.config.STATE_ROOT = old_run, old_root
        state._cache.pop("docket", None)


def test_a_write_drops_the_cached_reader():
    state._cache["docket"] = (9e12, {"docket": ["stale"]})
    state.invalidate("docket")
    assert "docket" not in state._cache


def test_the_jtf_page_still_works_after_the_shared_slot_split():
    """The slot picker and lane panels moved into _SLOT_JS so Docket could reuse
    them; the JTF form must still carry every function it calls."""
    page = pages.jtf_page()
    for needle in ["function slotConfig", "function slotCard", "function openPick",
                   "function slotPayload", "function wireSlotPicker",
                   "id='pickmodal'", "id='submitjtf'"]:
        assert needle in page, f"JTF page lost {needle!r}"
    entries = _with_entries(pages.docket_page)
    for needle in ["function slotConfig", "function openPick", "function wireSlotPicker"]:
        assert needle in entries, f"Docket page lost {needle!r}"


def test_run_now_confirms_before_it_spends_anything():
    """uid=1384. The button launches real deputies, so it must ask first and must
    tell the operator what the click does to the schedule."""
    page = _with_entries(pages.docket_page)
    i = page.index("var go=el('button','runnow','Run now')")
    block = page[i:i + 700]
    assert "confirm(" in block, "Run now fires without confirming"
    assert "act('run-now', p.id)" in block, "the button does not post the run-now op"
    assert "occurrence after" in block, "the confirm does not say what happens to the schedule"
    # the confirm must gate the call, not follow it
    assert block.index("confirm(") < block.index("act('run-now'"), \
        "the post is not inside the confirm"


def test_run_now_reaches_the_cli_with_the_id():
    old_run = server.subprocess.run
    calls = []
    server.subprocess.run = lambda cmd, **kw: (
        calls.append(cmd) or FakeProc(json.dumps({"ok": True, "entry": CASE_DOCKET})))
    try:
        h = FakeHandler({"op": "run-now", "id": "p20260919_aa"})
        h._docket_write()
        assert h.replied[0] == 200, f"run-now failed: {h.replied}"
        assert calls[0][2] == "run-now" and calls[0][-1] == "p20260919_aa"
    finally:
        server.subprocess.run = old_run


def test_run_now_without_an_id_never_reaches_the_cli():
    old_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not run the CLI"))
    try:
        h = FakeHandler({"op": "run-now"})
        h._docket_write()
        assert h.replied[0] == 400 and not h.replied[1]["ok"]
    finally:
        server.subprocess.run = old_run


def test_pause_confirms_and_cancel_is_gated_behind_pause():
    """uid=1385. Pause asks first; Cancel is only reachable once an entry is OFF the
    docket, so a mis-click on a scheduled entry cannot retire it."""
    page = _with_entries(pages.docket_page)
    i = page.index("var toggle=el('button',null, p.paused ? 'Resume' : 'Pause')")
    pause_block = page[i:i + 500]
    assert "confirm(" in pause_block, "Pause no longer confirms"
    assert "!p.paused && !confirm(" in pause_block, \
        "the confirm must gate PAUSE only — resuming is not a destructive act"
    assert pause_block.index("confirm(") < pause_block.index("act(p.paused"), \
        "the confirm does not gate the post"

    j = page.index("var cancel=el('button','danger','Cancel')")
    guard = page[page.index("if(p.paused || !p.next_run){"):j]
    assert guard, "Cancel is not gated on the entry being off the docket"
    cancel_block = page[j:j + 500]
    assert "confirm(" in cancel_block and "act('cancel', p.id)" in cancel_block
    assert cancel_block.index("confirm(") < cancel_block.index("act('cancel'")
    # red, from the palette's existing red rather than a new hue
    assert ".docket-acts button.danger { color:#f85149" in page
    assert "#f85149" in pages._CSS, "the red is not the palette's existing one"


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
