"""Contract tests for scripts/export_tracker_json.py.

The tracker CSV is the source of truth for every downstream surface, so these
tests pin the guarantees that keep it exportable: the header matches the schema,
a missing role degrades to a bare company id instead of corrupting the key, and
a gap in the historical record is a warning rather than a hard failure (a hard
failure would push a future writer into inventing the missing value).
"""

import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "export_tracker_json",
    REPO_ROOT / "scripts" / "export_tracker_json.py",
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

SCHEMA = exporter.load_schema(exporter.DEFAULT_SCHEMA)
HEADER = SCHEMA["x-csv-header"]


def make_row(**overrides):
    row = {column: "" for column in HEADER}
    row.update(
        {
            "id": "acme--staff-android-engineer",
            "date": "2026-10-01",
            "company": "Acme",
            "role": "Staff Android Engineer",
            "status": "applied",
            "fit_rating": "80",
        }
    )
    row.update(overrides)
    return row


class ExportTrackerTests(unittest.TestCase):
    def test_slug_normalizes_noise(self):
        self.assertEqual(exporter.slug("Scout Motors Inc."), "scout-motors-inc")
        self.assertEqual(exporter.slug("  Senior Android Engineer, Platform "), "senior-android-engineer-platform")

    def test_live_csv_matches_schema_header(self):
        with exporter.DEFAULT_CSV.open(newline="", encoding="utf-8") as handle:
            actual = next(exporter.csv.reader(handle))
        self.assertEqual(actual, HEADER)

    def test_live_csv_has_no_errors(self):
        rows = exporter.load_rows(exporter.DEFAULT_CSV, HEADER)
        errors, _warnings = exporter.validate(rows, SCHEMA)
        self.assertEqual(errors, [])

    def test_missing_role_requires_bare_company_id(self):
        rows = [make_row(id="acme--", role="")]
        errors, warnings = exporter.validate(rows, SCHEMA)
        self.assertTrue(any("bare company slug" in e for e in errors))

        rows = [make_row(id="acme", role="")]
        errors, warnings = exporter.validate(rows, SCHEMA)
        self.assertEqual(errors, [])
        self.assertTrue(any("role unknown" in w for w in warnings))

    def test_undated_terminal_status_is_a_warning(self):
        rows = [make_row(id="acme--x", role="X", status="rejected", notes="gmail-sync: Rejection")]
        errors, warnings = exporter.validate(rows, SCHEMA)
        self.assertEqual(errors, [])
        self.assertTrue(any("no dated resolution" in w for w in warnings))

    def test_status_outside_closed_set_is_an_error(self):
        rows = [make_row(status="in_progress")]
        errors, _warnings = exporter.validate(rows, SCHEMA)
        self.assertTrue(any("outside the closed set" in e for e in errors))

    def test_duplicate_ids_are_an_error(self):
        rows = [make_row(), make_row()]
        errors, _warnings = exporter.validate(rows, SCHEMA)
        self.assertTrue(any("duplicate id" in e for e in errors))

    def test_export_document_derives_fields(self):
        row = make_row(
            id="acme--staff-android-engineer",
            status="applied",
            notes="Applied 2026-10-01; 2026-10-05 followed up 2026-10-05; followed up 2026-10-12",
        )
        rank = SCHEMA["properties"]["status"]["x-status-rank"]
        terminal = set(SCHEMA["properties"]["status"]["x-terminal"])
        doc = exporter.export_document(row, rank, terminal, exporter.dt.date(2026, 10, 30))

        self.assertEqual(doc["company_id"], "acme")
        self.assertEqual(doc["status_rank"], rank["applied"])
        self.assertTrue(doc["is_open"])
        self.assertEqual(doc["last_activity_date"], "2026-10-12")
        self.assertEqual(doc["days_since_activity"], 18)
        self.assertEqual(doc["follow_ups_sent"], 2)
        self.assertEqual(doc["event_dates"], ["2026-10-01", "2026-10-05", "2026-10-12"])
        self.assertEqual(doc["fit_rating"], 80)

    def test_empty_cells_export_as_null(self):
        rank = SCHEMA["properties"]["status"]["x-status-rank"]
        terminal = set(SCHEMA["properties"]["status"]["x-terminal"])
        doc = exporter.export_document(make_row(), rank, terminal, exporter.dt.date(2026, 10, 30))
        for field in ("sector", "role_type", "channel", "contact_person", "source", "cv_file"):
            self.assertIsNone(doc[field], field)


if __name__ == "__main__":
    unittest.main()
