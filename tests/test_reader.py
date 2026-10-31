"""Tests for the shared read projection.

The readers are the two consumers' only contact with the store, so their job is
narrow and worth pinning: the legacy row shape must be reproduced exactly, the
source must be reported honestly, and a stale snapshot must be labelled.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import reader  # noqa: E402
import render  # noqa: E402


def application(**overrides):
    base = {
        "id": "acme--staff-android-engineer",
        "date": "2026-10-20T00:00:00+00:00",
        "company": "Acme",
        "companyId": "acme",
        "role": "Staff Android Engineer",
        "sector": "Tech",
        "channel": "channel:workday",
        "status": "status:rejected",
        "fitRating": 91,
        "contactPerson": None,
        "source": "https://example.test/1",
        "notesRendered": "history",
        "notesLegacy": "legacy prose",
        "metadata": {"importIndex": 7, "legacyChannel": "Workday (Acme careers)"},
    }
    base.update(overrides)
    return base


class TestRowProjection(unittest.TestCase):
    def test_status_maps_back_to_the_legacy_label(self):
        self.assertEqual(reader.rowFromStore(application())["status"], "rejected")

    def test_fit_rating_is_a_string_for_the_consumers(self):
        self.assertEqual(reader.rowFromStore(application())["fit_rating"], "91")

    def test_missing_fit_rating_is_empty_not_null(self):
        self.assertEqual(reader.rowFromStore(application(fitRating=None))["fit_rating"], "")

    def test_original_channel_label_wins_over_the_enum(self):
        self.assertEqual(
            reader.rowFromStore(application())["channel"], "Workday (Acme careers)"
        )

    def test_channel_falls_back_to_the_enum_without_a_legacy_label(self):
        row = reader.rowFromStore(application(metadata={"importIndex": 7}))
        self.assertEqual(row["channel"], "workday")

    def test_artifact_columns_prefer_the_local_path(self):
        artifacts = [
            {"kind": "artifact:cv", "localPath": "documents/cv.pdf"},
            {"kind": "artifact:cover-letter", "externalUrl": "https://docs.test/1"},
        ]
        row = reader.rowFromStore(application(), artifacts)
        self.assertEqual(row["cv_file"], "documents/cv.pdf")
        self.assertEqual(row["cover_letter_file"], "https://docs.test/1")

    def test_notes_render_then_fall_back_to_the_legacy_blob(self):
        self.assertEqual(reader.rowFromStore(application())["notes"], "history")
        self.assertEqual(
            reader.rowFromStore(application(notesRendered=None))["notes"], "legacy prose"
        )

    def test_row_carries_the_legacy_column_set(self):
        self.assertTrue(set(reader.LEGACY_KEYS) <= set(reader.rowFromStore(application())))

    def test_import_index_preserves_the_pre_migration_order(self):
        early = reader.rowFromStore(application(id="acme--one", metadata={"importIndex": 1}))
        late = reader.rowFromStore(application(id="acme--two", metadata={"importIndex": 2}))
        # Same date: the CSV order must survive, so importIndex 1 comes first.
        rowList = reader._sortRows([late, early])
        self.assertEqual([row["id"] for row in rowList], ["acme--one", "acme--two"])

    def test_newest_date_sorts_first(self):
        older = reader.rowFromStore(application(id="acme--old", date="2026-10-01T00:00:00+00:00"))
        newer = reader.rowFromStore(application(id="acme--new", date="2026-10-30T00:00:00+00:00"))
        rowList = reader._sortRows([older, newer])
        self.assertEqual([row["id"] for row in rowList], ["acme--new", "acme--old"])


class TestCsvSource(unittest.TestCase):
    def test_live_csv_reads_all_rows_in_the_legacy_shape(self):
        rows, meta = reader.rowsFromCsv()
        self.assertEqual(meta["source"], "csv")
        self.assertEqual(len(rows), 84)
        self.assertTrue(set(reader.LEGACY_KEYS) <= set(rows[0]))

    def test_missing_csv_is_a_clear_error(self):
        with self.assertRaises(reader.ReadError):
            reader.rowsFromCsv(pathlib.Path("/no/such/file.csv"))


class TestCacheSource(unittest.TestCase):
    def test_cache_rows_round_trip_with_age(self):
        payload = {
            "generatedAt": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat(
                timespec="seconds"
            ),
            "applications": [application()],
            "artifacts": {"acme--staff-android-engineer": [{"kind": "artifact:cv", "localPath": "documents/cv.pdf"}]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            rows, meta = reader.rowsFromCache(path)
        self.assertEqual(meta["source"], "cache")
        self.assertFalse(meta["stale"])
        self.assertGreaterEqual(meta["ageSeconds"], 300)
        self.assertEqual(rows[0]["cv_file"], "documents/cv.pdf")

    def test_old_cache_is_marked_stale(self):
        payload = {
            "generatedAt": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat(
                timespec="seconds"
            ),
            "applications": [application()],
            "artifacts": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            _rows, meta = reader.rowsFromCache(path)
        self.assertTrue(meta["stale"])

    def test_missing_cache_is_a_clear_error(self):
        with self.assertRaises(reader.ReadError):
            reader.rowsFromCache(pathlib.Path("/no/such/cache.json"))


class TestBadge(unittest.TestCase):
    def test_store_is_live(self):
        self.assertEqual(reader.badge({"source": "store"}), "store (live)")

    def test_cache_shows_its_age_and_staleness(self):
        self.assertEqual(
            reader.badge({"source": "cache", "ageSeconds": 600, "stale": False}),
            "cache (10m old)",
        )
        self.assertEqual(
            reader.badge({"source": "cache", "ageSeconds": 90000, "stale": True}),
            "cache (25h old, stale)",
        )

    def test_csv_is_labelled_pre_migration(self):
        self.assertEqual(reader.badge({"source": "csv"}), "csv (pre-migration)")


class TestSourceSelection(unittest.TestCase):
    def test_csv_preference_skips_the_store(self):
        previous = os.environ.get("TRACKER_SOURCE")
        os.environ["TRACKER_SOURCE"] = "csv"
        try:
            rows, meta = reader.rows()
        finally:
            if previous is None:
                os.environ.pop("TRACKER_SOURCE", None)
            else:
                os.environ["TRACKER_SOURCE"] = previous
        self.assertEqual(meta["source"], "csv")
        self.assertEqual(len(rows), 84)

    def test_falling_back_records_why(self):
        previous = os.environ.get("TRACKER_SOURCE")
        os.environ["TRACKER_SOURCE"] = "auto"
        try:
            _rows, meta = reader.rows("csv")
        finally:
            if previous is None:
                os.environ.pop("TRACKER_SOURCE", None)
            else:
                os.environ["TRACKER_SOURCE"] = previous
        self.assertEqual(meta["source"], "csv")


class TestDivergenceDetection(unittest.TestCase):
    """During the window the CSV is the write target, so the store can lag."""

    def setUp(self):
        self.csvIds = reader._csvIds()
        self.assertIsNotNone(self.csvIds)

    def _stub(self, rows):
        return unittest.mock.patch.object(
            reader, "rowsFromStore", lambda db=None: (rows, {"source": "store", "stale": False})
        )

    def test_store_wins_when_it_covers_every_csv_row(self):
        rows = [reader.rowFromStore(application(id=applicationId)) for applicationId in sorted(self.csvIds)]
        with self._stub(rows):
            got, meta = reader.rows("auto")
        self.assertEqual(meta["source"], "store")
        self.assertEqual(len(got), len(self.csvIds))

    def test_csv_wins_when_the_store_is_missing_a_row(self):
        missing = sorted(self.csvIds)[0]
        rows = [reader.rowFromStore(application(id=applicationId)) for applicationId in sorted(self.csvIds) if applicationId != missing]
        with self._stub(rows), unittest.mock.patch.object(
            reader, "rowsFromCache", lambda path=None: (_ for _ in ()).throw(reader.ReadError("no cache"))
        ):
            got, meta = reader.rows("auto")
        self.assertEqual(meta["source"], "csv")
        self.assertIn(missing, {row["id"] for row in got})
        self.assertEqual(reader.badge(meta), "csv (store behind)")

    def test_store_wins_outright_when_there_is_no_csv(self):
        rows = [reader.rowFromStore(application())]
        with self._stub(rows), unittest.mock.patch.object(
            reader, "LEGACY_CSV", pathlib.Path("/no/such/file.csv")
        ):
            got, meta = reader.rows("auto")
        self.assertEqual(meta["source"], "store")
        self.assertEqual(len(got), 1)


class TestImportedNotesRender(unittest.TestCase):
    def test_imported_event_is_provenance_not_a_dated_line(self):
        event = {"eventId": "x", "type": "event:imported", "date": "2026-10-20", "note": "prose"}
        self.assertEqual(render.renderNote(event), "prose")

    def test_post_import_events_keep_their_date_and_type(self):
        event = {
            "eventId": "y",
            "type": "event:rejection",
            "date": "2026-10-22",
            "note": "closed",
        }
        self.assertEqual(render.renderNote(event), "2026-10-22 · event:rejection — closed")

    def test_import_then_rejection_reads_as_prose_then_history(self):
        events = [
            {"eventId": "a", "type": "event:imported", "date": "2026-10-20", "note": "prose"},
            {"eventId": "b", "type": "event:rejection", "date": "2026-10-22", "note": "closed"},
        ]
        self.assertEqual(
            render.renderNotes(events),
            "prose\n2026-10-22 · event:rejection — closed",
        )

    def test_empty_import_note_does_not_leave_a_blank_line(self):
        events = [
            {"eventId": "a", "type": "event:imported", "date": "2026-10-20", "note": ""},
            {"eventId": "b", "type": "event:applied", "date": "2026-10-20", "note": "sent"},
        ]
        self.assertEqual(render.renderNotes(events), "2026-10-20 · event:applied — sent")


if __name__ == "__main__":
    unittest.main()
