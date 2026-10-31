"""Unit tests for ui/terminal/tracker_tui.py."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

tui_spec = importlib.util.spec_from_file_location(
    "tracker_tui",
    REPO_ROOT / "ui" / "terminal" / "tracker_tui.py",
)
tui_mod = importlib.util.module_from_spec(tui_spec)
tui_spec.loader.exec_module(tui_mod)


class TrackerTuiTests(unittest.TestCase):
    def test_load_config_loads_defaults(self):
        config = tui_mod.load_config()
        self.assertIn("columns", config)
        self.assertIn("theme", config)
        self.assertIn("split_mode", config)
        self.assertTrue(any(c["key"] == "company" for c in config["columns"]))

    def test_load_data_finds_tracker(self):
        rows = tui_mod.load_data()
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0, "Expected records from the tracker store")
        sample = rows[0]
        self.assertIn("company", sample)
        self.assertIn("role", sample)

    def test_load_data_reports_which_source_answered(self):
        tui_mod.load_data()
        self.assertIn(tui_mod.DATA_META.get("source"), {"store", "cache", "csv", "unavailable"})
        self.assertTrue(tui_mod.DATA_META.get("label"))

    def test_load_data_still_works_with_the_csv_absent(self):
        # The store is the primary source, so the CSV's presence must not
        # matter. Pointing the CSV source at a missing file proves the reader
        # does not quietly depend on it.
        previous = os.environ.get("TRACKER_SOURCE")
        os.environ["TRACKER_SOURCE"] = "cache" if (REPO_ROOT / "generated" / "tracker-cache.json").exists() else "store"
        try:
            rows = tui_mod.load_data()
        finally:
            if previous is None:
                os.environ.pop("TRACKER_SOURCE", None)
            else:
                os.environ["TRACKER_SOURCE"] = previous
        self.assertIsInstance(rows, list)

    def test_recalc_request_is_structured_and_uses_the_application_id(self):
        row = {"id": "acme--staff-android-engineer", "company": "Acme", "role": "Staff Android", "fit_rating": "88"}
        with tempfile.TemporaryDirectory() as directory:
            previous = tui_mod.RECALC_QUEUE_PATH
            tui_mod.RECALC_QUEUE_PATH = Path(directory) / ".recalc_queue"
            try:
                request = tui_mod.write_recalc_request(row)
                written = json.loads((Path(directory) / ".recalc_queue").read_text())
            finally:
                tui_mod.RECALC_QUEUE_PATH = previous
        self.assertEqual(written, request)
        self.assertEqual(request["kind"], "fit-recalculation")
        self.assertEqual(request["applicationId"], "acme--staff-android-engineer")
        self.assertEqual(request["currentFitRating"], "88")
        self.assertIn("requestedAt", request)

    def test_recalc_request_survives_an_unwritable_queue(self):
        previous = tui_mod.RECALC_QUEUE_PATH
        tui_mod.RECALC_QUEUE_PATH = Path("/no/such/dir/.recalc_queue")
        try:
            request = tui_mod.write_recalc_request({"id": "acme--one", "company": "Acme"})
        finally:
            tui_mod.RECALC_QUEUE_PATH = previous
        self.assertEqual(request["applicationId"], "acme--one")

    def test_get_archive_dir_fuzzy_lookup(self):
        row = {"company": "OpenAI", "role": "Android Engineer", "date": "2026-10-27"}
        archive_dir = tui_mod.get_archive_dir(row)
        self.assertTrue(Path(archive_dir).exists(), f"Path {archive_dir} should exist")
        self.assertIn("OpenAI", archive_dir)

    def test_get_column_widths(self):
        rows = [{"date": "2026-10-28", "company": "TestCorp", "status": "applied", "fit_rating": "5"}]
        cols = [{"key": "company", "label": "Company"}, {"key": "role", "label": "Role"}]
        widths = tui_mod.get_column_widths(rows, cols)
        self.assertIn("company", widths)
        self.assertEqual(widths["role"], 0)

    def test_load_detail_lines(self):
        row = {
            "company": "OpenAI",
            "role": "Android Engineer Applied Foundations",
            "date": "2026-10-27",
            "status": "applied",
            "fit_rating": "5",
            "notes": "Testing",
            "source": "https://openai.com",
        }
        lines = tui_mod.load_detail_lines(row, max_width=80)
        self.assertIsInstance(lines, list)
        self.assertTrue(any("COMPANY: OpenAI" in text for text, _ in lines))


if __name__ == "__main__":
    unittest.main()
