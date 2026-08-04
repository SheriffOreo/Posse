#!/usr/bin/env python3
"""Tests for scratch_notify_email.context_header — the [precinct|case|deputy|model]
stamp. Focus: Case 394 board-first case/precinct resolution (a deputy that TOOK a
new case must stamp the CURRENT case from the active-deputies board, not the stale
TSOMP_CASE literal baked into its relaunch script), with a safe env fallback.

Runs fully offline: imports the module and calls context_header directly (no SMTP),
and points TSOMP_RECORDS_ROOT at a throwaway dir so the live board is never touched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import scratch_notify_email as ne


class ContextHeaderBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ne_test_")
        self._saved = {k: os.environ.get(k) for k in
                       ("TSOMP_RECORDS_ROOT", "WORKER_PRECINCT", "TSOMP_CASE", "TSOMP_MODEL")}
        os.environ["TSOMP_RECORDS_ROOT"] = self.tmp

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_board(self, mapping):
        p = Path(self.tmp) / "active_deputies.json"
        p.write_text(json.dumps(mapping))

    # --- the exact Case 394 bug ---------------------------------------------
    def test_board_overrides_stale_env_case(self):
        """case_ui took 391 (board) but its relaunch script still exports 377 (env).
        The stamp must show 391 — the bug was it showed 377."""
        os.environ["WORKER_PRECINCT"] = "infra"
        os.environ["TSOMP_CASE"] = "377"      # stale literal from the relaunch script
        os.environ["TSOMP_MODEL"] = "opus"
        self._write_board({"case_ui": {"case": "391", "precinct": "infra"}})
        h = ne.context_header("case_ui")
        self.assertIn("case: 391", h)
        self.assertNotIn("377", h)
        self.assertEqual(h, "[precinct: infra | case: 391 | deputy: case_ui | model: opus]")

    def test_board_precinct_overrides_env(self):
        os.environ["WORKER_PRECINCT"] = "infra"
        os.environ["TSOMP_CASE"] = "377"
        self._write_board({"d": {"case": "500", "precinct": "query"}})
        h = ne.context_header("d")
        self.assertIn("precinct: query", h)
        self.assertIn("case: 500", h)

    # --- safe fallback (old behavior preserved) -----------------------------
    def test_no_board_file_falls_back_to_env(self):
        os.environ["WORKER_PRECINCT"] = "infra"
        os.environ["TSOMP_CASE"] = "377"
        os.environ["TSOMP_MODEL"] = "opus"
        # no active_deputies.json written
        h = ne.context_header("case_ui")
        self.assertEqual(h, "[precinct: infra | case: 377 | deputy: case_ui | model: opus]")

    def test_agent_absent_from_board_falls_back_to_env(self):
        os.environ["WORKER_PRECINCT"] = "infra"
        os.environ["TSOMP_CASE"] = "377"
        self._write_board({"someone_else": {"case": "999", "precinct": "eval"}})
        h = ne.context_header("case_ui")
        self.assertIn("case: 377", h)
        self.assertIn("precinct: infra", h)
        self.assertNotIn("999", h)

    def test_corrupt_board_falls_back_to_env(self):
        os.environ["TSOMP_CASE"] = "377"
        (Path(self.tmp) / "active_deputies.json").write_text("{not json")
        h = ne.context_header("case_ui")
        self.assertIn("case: 377", h)

    def test_board_entry_missing_case_uses_env_case(self):
        os.environ["TSOMP_CASE"] = "377"
        os.environ["WORKER_PRECINCT"] = "infra"
        self._write_board({"case_ui": {"precinct": "infra"}})  # no case key
        h = ne.context_header("case_ui")
        self.assertIn("case: 377", h)          # env case survives
        self.assertIn("precinct: infra", h)

    # --- field omission ------------------------------------------------------
    def test_no_case_anywhere_omits_case(self):
        os.environ.pop("TSOMP_CASE", None)
        os.environ["WORKER_PRECINCT"] = "infra"
        os.environ["TSOMP_MODEL"] = "opus"
        self._write_board({"case_ui": {"precinct": "infra"}})
        h = ne.context_header("case_ui")
        self.assertNotIn("case:", h)
        self.assertEqual(h, "[precinct: infra | deputy: case_ui | model: opus]")

    def test_no_agent_no_fields_when_env_empty(self):
        for k in ("WORKER_PRECINCT", "TSOMP_CASE", "TSOMP_MODEL"):
            os.environ.pop(k, None)
        self.assertEqual(ne.context_header(""), "")
        self.assertEqual(ne.context_header(None), "")

    def test_empty_agent_never_reads_board(self):
        # a non-deputy manual send with only env set still stamps from env
        os.environ["TSOMP_CASE"] = "377"
        self._write_board({"": {"case": "111"}})
        h = ne.context_header("")
        self.assertNotIn("111", h)             # empty agent → no board lookup


if __name__ == "__main__":
    unittest.main(verbosity=2)
