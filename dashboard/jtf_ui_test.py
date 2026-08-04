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
