#!/usr/bin/env python3
"""Case 384e / Phase E: tests for the dashboard JTF (Joint Task Force) surface.

Covers the page render (Cowork->JTF rename complete, precinct-or-deputy slot UI wired,
the right endpoints referenced) and the server-side precinct-vs-deputy SLOT VALIDATION
(server.Handler._valid_slot). Pure/offline — no server is started.  Run:
  python jtf_ui_test.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pages    # noqa: E402
import server   # noqa: E402


def test_page_renders_jtf_not_cowork():
    p = pages.jtf_page()
    for needle in ["New JTF", "Joint Task Force", "jtf-form", "specific deputy",
                   "id='submitjtf'", "id='leadslot'", "id='collabchips'"]:
        assert needle in p, f"page missing {needle!r}"
    # user-facing rename is complete
    assert "Cowork" not in p and "cowork" not in p, "stale Cowork text in the page"


def test_nav_links_jtf():
    nav = pages._nav("jtf")
    assert ">JTF<" in nav and "href='/jtf'" in nav, nav
    assert "Cowork" not in nav and "/cowork" not in nav, "stale Cowork nav link"


def test_js_uses_new_endpoints_and_slot_objects():
    js = pages._JTF_JS
    # the slot picker offers BOTH precincts and specific deputies
    assert "/api/precincts" in js and "/api/agents" in js, "picker sources missing"
    assert "kind:'precinct'" in js and "kind:'deputy'" in js, "slot objects missing"
    # submit posts to the new endpoint
    assert "/api/jtf" in js and "submitJTF" in js, "submit not wired to /api/jtf"
    assert "/api/cowork" not in js, "stale /api/cowork POST"


def test_valid_slot_precinct_vs_deputy():
    V = server.Handler._valid_slot
    known = {"infra", "omp", "eval"}
    # precinct slot must be a LIVE precinct
    assert V({"kind": "precinct", "name": "infra"}, known) == {"kind": "precinct", "name": "infra"}
    assert V({"kind": "precinct", "name": "ghost"}, known) is None
    # deputy slot just needs a non-empty name (membership not required)
    assert V({"kind": "deputy", "name": "case_ui"}, known) == {"kind": "deputy", "name": "case_ui"}


def test_valid_slot_rejects_malformed():
    V = server.Handler._valid_slot
    known = {"infra"}
    for bad in [None, "nope", 42, {"kind": "team", "name": "x"},
                {"kind": "deputy", "name": ""}, {"name": "x"}, {"kind": "precinct"}]:
        assert V(bad, known) is None, f"accepted malformed slot: {bad!r}"


# ----- Case 599: per-slot work split, C1/C2 numbering, repeated precincts -----
def test_valid_slot_carries_a_precinct_work_split():
    V = server.Handler._valid_slot
    known = {"infra"}
    got = V({"kind": "precinct", "name": "infra", "service": "claude", "model": "opus",
             "report_service": "chatgpt", "report_model": "terra"}, known)
    assert got == {"kind": "precinct", "name": "infra", "service": "claude",
                   "model": "opus", "report_service": "chatgpt",
                   "report_model": "terra"}, got
    # a slot that chose nothing keeps the pre-599 shape exactly
    assert V({"kind": "precinct", "name": "infra"}, known) == {
        "kind": "precinct", "name": "infra"}


def test_valid_slot_rejects_unusable_lane_values():
    V = server.Handler._valid_slot
    known = {"infra"}
    for bad in [{"model": "banana"}, {"service": "banana"},
                {"report_service": "banana"}, {"report_model": "banana"},
                {"report_service": "claude+chatgpt"},        # a MODE is not a service
                {"model": "terra", "service": "claude"},     # model vs service
                {"report_model": "opus", "report_service": "chatgpt"}]:
        slot = dict({"kind": "precinct", "name": "infra"}, **bad)
        assert V(slot, known) is None, f"accepted an unusable lane: {bad!r}"


def test_valid_slot_refuses_a_model_on_a_specific_deputy():
    # a live agent keeps its own model; a control that did nothing would be a
    # promise the backend drops, so the value is REJECTED rather than ignored.
    V = server.Handler._valid_slot
    known = {"infra"}
    assert V({"kind": "deputy", "name": "web_591"}, known) == {
        "kind": "deputy", "name": "web_591"}
    for bad in ("service", "model", "report_service", "report_model"):
        assert V({"kind": "deputy", "name": "web_591", bad: "opus"}, known) is None, bad


def test_page_offers_per_slot_split_and_numbers_collaborators():
    p = pages.jtf_page()
    js = pages._JTF_JS
    # the registry data the per-slot pickers are built from must be on the page
    for needle in ["CC_SVC_ALIASES", "CC_MODEL_LABELS", "CC_SERVICE_IDS", "CC_WORK_TYPES"]:
        assert needle in js, f"JTF page cannot build model lists: {needle} missing"
    # every slot renders a card with its tag + config panel
    assert "slotCard" in js and "slotConfig" in js and "jtf-slot" in p
    # collaborators are numbered C1..Cn from their position
    assert "'C'+(i+1)" in js, "collaborators are not numbered"
    assert "C1" in p and "C2" in p, "the numbering is not explained on the page"
    # the four work-split fields are what gets posted, for precinct slots only
    assert "report_service" in js and "report_model" in js
    assert "if(s.kind==='precinct')" in js, "a deputy slot's lanes are not stripped"


def test_page_allows_the_same_precinct_more_than_once():
    # the bridge always allowed repeated precincts (each is a distinct fresh
    # deputy); it was pick() that refused them via a kind+name comparison.
    js = pages._JTF_JS
    assert "slot.kind==='deputy' &&" in js, \
        "pick() still blocks a duplicate regardless of kind"
    # and the page says so, so the behaviour is discoverable
    assert "more than once" in pages.jtf_page()


# ----- Case 599 uid=770: judge box styling + its own service/model -----------
def test_jtf_form_styles_select_like_every_other_box():
    # the Judge dropdown was the ONLY control falling back to the browser default
    # (white on a dark card) because this rule listed textarea and input but not
    # select. Assert the SELECTOR, and that it shares the others' declaration.
    css = pages._CSS if hasattr(pages, "_CSS") else pages._shell("x", "", active="jtf")
    rule = [ln for ln in css.splitlines() if ".jtf-form textarea" in ln]
    assert rule, "the jtf-form box rule is gone"
    block = css.split(".jtf-form textarea", 1)[1].split("}", 1)[0]
    assert ".jtf-form select" in rule[0] or ".jtf-form select" in block, \
        f"select is not styled with the other boxes: {rule[0]}"
    assert "background:var(--surface2)" in block, block


def test_judge_service_and_model_boxes_exist_and_are_gated():
    p = pages.jtf_page()
    js = pages._JTF_JS
    assert "id='judge_service'" in p and "id='judge_model'" in p, \
        "the judge service/model boxes are missing"
    assert "id='judge-models'" in p and "display:none" in p, \
        "the judge pair is not hidden until a judge is picked"
    assert "syncJudge" in js, "nothing reveals/populates the judge pair"
    # the model list is REBUILT per service (never optgroup.hidden — Case 557)
    assert "modelPairs(js" in js, "judge model list is not rebuilt for the service"
    # and both are posted
    assert "judge_service:" in js and "judge_model:" in js, "the pair is not posted"


def test_judge_pair_validated_server_side():
    # a bad value must 400 rather than be silently dropped (the Case 551 rule).
    src = server.Handler._create_jtf.__doc__ or ""
    import inspect
    body = inspect.getsource(server.Handler._create_jtf)
    assert "judge_model" in body and "judge_service" in body, \
        "the JTF endpoint ignores the judge pair"
    for needle in ["invalid judge_model", "invalid judge_service",
                   "does not belong to"]:
        assert needle in body, f"missing rejection path: {needle}"



# --------------------------------------------------------------------------- #
# Case 760: the Status board's JTF boxes + add-collaborator
# --------------------------------------------------------------------------- #
def test_status_page_boxes_jtfs_and_offers_add_collaborator():
    h = pages.status_page()
    for needle in ["id='jtfs'", "id='addmodal'", "id='addbody'",
                   "renderJtfs", "openAddCollab", "sendAddCollab",
                   "/api/jtf/collaborator"]:
        assert needle in h, f"status page missing {needle!r}"
    assert "Active JTF (" in h, "the JTF section has no heading"
    # uid=1381 + uid=1383: two PEER sections, Active Deputies FIRST then Active JTF.
    # Each heading must sit directly above the table it describes — heading the whole
    # page with "Active Deputies" is what left it describing nothing.
    assert h.index(">Active Deputies ") < h.index("id='workers'") < h.index("id='jtfs'"), \
        "order must be: Active Deputies heading, its table, then the JTF section"
    assert "Other deputies" not in h, "the demoted sub-heading is gone"


def test_one_row_builder_serves_the_box_and_the_rest():
    """The controls must not be re-implemented for boxed rows -- two builders would
    drift the day a control is added, and a JTF member would silently lose one."""
    js = pages._STATUS_JS
    assert js.count("function workerRow(") == 1, "more than one row builder"
    assert js.count("function workerTable(") == 1
    # every control glyph appears exactly once: in the single row builder
    for glyph in ("⇆", "▶", "⏹", "✉"):
        assert js.count(f"'{glyph}'") == 1, f"glyph {glyph} built in {js.count(chr(39)+glyph+chr(39))} places"
    # the box calls the shared builder rather than assembling rows itself
    assert "workerTable(rows, tags)" in js and "workerTable(rest,null)" in js


def test_the_deputy_count_measures_its_own_section():
    """uid=1381: the count under Active Deputies is the table below it, not the
    fleet — the JTF heading counts JTFs and each box names its own members, so a
    fleet total would count JTF members under a heading they are not in."""
    js = pages._STATUS_JS
    i = js.index("getElementById('wc')")
    window = js[i - 400:i + 200]
    assert "rest.filter" in window, "the failed count is taken from the fleet, not the section"
    assert "rest.length-nfail" in window, "the active count is taken from the fleet"
    assert js.index("var rest=s.workers.filter") < i, \
        "the count is computed before the section it counts is known"


def test_the_add_dialog_explains_the_chain_before_anything_is_spawned():
    js = pages._STATUS_JS
    for needle in ["Send interrupts the LEAD",
                   "ONLY after the last one is the new deputy launched",
                   "message the lead that it is ready",
                   "already has an onboarding chain in "]:
        assert needle in js, f"the dialog never says: {needle!r}"


def test_collab_request_validation():
    V = server.Handler._valid_collab_request
    known = {"infra", "eval"}
    ok, err = V({"jtf": "900", "precinct": "infra", "message": "hi"}, known)
    assert err is None and ok["lanes"] == {} and ok["jtf"] == "900", (ok, err)
    assert V({"jtf": "9 0", "precinct": "infra", "message": "h"}, known)[1] == "bad JTF id"
    assert "unknown precinct" in V({"jtf": "9", "precinct": "ghost", "message": "h"}, known)[1]
    assert "message for the lead" in V({"jtf": "9", "precinct": "infra", "message": " "}, known)[1]
    # an unusable work split is REFUSED, not dropped
    assert "invalid model" in V({"jtf": "9", "precinct": "infra", "message": "h",
                                 "model": "banana"}, known)[1]
    assert "does not belong" in V({"jtf": "9", "precinct": "infra", "message": "h",
                                   "model": "opus", "service": "chatgpt"}, known)[1]
    good, err = V({"jtf": "9", "precinct": "infra", "message": "h",
                   "model": "terra", "service": "chatgpt"}, known)
    assert err is None and good["lanes"] == {"service": "chatgpt", "model": "terra"}


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print(f"=== jtf_ui test suite  ({len(tests)} tests) ===")
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
    sys.exit(_run())
