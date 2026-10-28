"""Unit tests for ui/terminal/tracker_tui.py."""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import importlib.util
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
        self.assertGreater(len(rows), 0, "Expected records from job_search_tracker.csv")
        sample = rows[0]
        self.assertIn("company", sample)
        self.assertIn("role", sample)

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
