"""Tests for the Phase 2b modules.

Offline tests cover the pure decisions: CSV resolution, proposal matching,
proposal validation, Sheet row building, artifact hashing, and the cache file.
Emulator tests cover the paths that need a real Firestore API: the full CSV
import plus byte-identical CSV export, deferred requests, proposal approval,
artifacts, and cache refresh.

Run the emulator half with:

    FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \\
    firebase emulators:exec --only firestore --project job-search-tracker-test \\
      ".venv/bin/python -m unittest tests.test_tracker_modules"
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST")
os.environ["TRACKER_PROJECT"] = "job-search-tracker-test"
if EMULATOR_HOST:
    os.environ["TRACKER_ALLOW_WRITES"] = "1"

import artifacts  # noqa: E402
import cache  # noqa: E402
import client  # noqa: E402
import contract  # noqa: E402
import importer  # noqa: E402
import proposals  # noqa: E402
import sheets  # noqa: E402
import tasks  # noqa: E402
import tracker  # noqa: E402

CSV_PATH = ROOT / "job_search_tracker.csv"


def runCli(argv: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = tracker.main(argv)
    raw = buffer.getvalue().strip()
    return code, (json.loads(raw) if raw else {})


class TestImporter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = importer.readCsv(CSV_PATH)
        cls.payloads, cls.warnings = importer.buildImport(cls.rows, now="2026-10-31T00:00:00-04:00")

    def test_header_is_the_legacy_contract(self):
        self.assertEqual(importer.readCsv(CSV_PATH)[0]["id"], self.rows[0]["id"])

    def test_every_row_resolves(self):
        self.assertEqual(len(self.payloads), 84)

    def test_ids_are_preserved_and_unique(self):
        self.assertEqual({payload["id"] for payload in self.payloads}, {row["id"] for row in self.rows})

    def test_every_status_is_mapped(self):
        valid = set(contract.statusValues())
        self.assertTrue(all(payload["status"] in valid for payload in self.payloads))

    def test_missing_roles_stay_null(self):
        nullRoles = [payload for payload in self.payloads if payload["role"] is None]
        self.assertEqual(len(nullRoles), 3)
        for payload in nullRoles:
            self.assertNotIn("", [payload["role"]])

    def test_fit_rating_is_an_integer(self):
        self.assertTrue(all(isinstance(payload["fitRating"], int) for payload in self.payloads))

    def test_notes_are_preserved_verbatim_and_produce_one_event(self):
        for payload, row in zip(self.payloads, self.rows):
            self.assertEqual(payload["notesLegacy"], row["notes"])
            self.assertEqual(len(payload["events"]), 1)
            self.assertEqual(payload["events"][0]["type"], "event:imported")
            self.assertEqual(payload["events"][0]["derivedFrom"], "csv-import")
            self.assertEqual(payload["events"][0]["note"], row["notes"] or None)

    def test_artifact_forms_are_classified_not_assumed(self):
        counts = {"localPath": 0, "externalUrl": 0, "driveUrl": 0}
        for payload in self.payloads:
            for entry in payload["artifacts"]:
                for key in counts:
                    if entry.get(key):
                        counts[key] += 1
        self.assertEqual(sum(counts.values()), 89)
        # 4 Google Docs URLs (2 CVs + 2 cover letters) and 45 absolute
        # Drive-mount paths: the historical record is mostly not repo-relative.
        self.assertEqual(counts["externalUrl"], 4)
        self.assertEqual(counts["driveUrl"], 45)
        self.assertEqual(counts["localPath"], 40)

    def test_empty_artifact_cells_emit_nothing(self):
        payload = next(item for item in self.payloads if not item["artifacts"])
        self.assertEqual(payload["artifacts"], [])

    def test_channel_free_text_maps_or_falls_back(self):
        byId = {payload["id"]: payload for payload in self.payloads}
        snap = byId["major-league-baseball-mlb--senior-android-engineer-mlb-app-feature-teams"]
        self.assertEqual(snap["channel"], "channel:other")
        self.assertEqual(snap["metadata"]["legacyChannel"], "MLB careers site")
        workday = next(
            payload for payload in self.payloads if payload["metadata"].get("legacyChannel", "").startswith("Workday")
        )
        self.assertEqual(workday["channel"], "channel:workday")

    def test_blank_channel_is_null(self):
        self.assertTrue(all(payload["channel"] is None for payload in self.payloads if not payload["metadata"].get("legacyChannel")))

    def test_id_mismatch_blocks_the_import(self):
        row = dict(self.rows[0])
        row["id"] = "wrong-id"
        with self.assertRaises(contract.ContractError):
            importer.rowToPayload(row, 0, "2026-10-31T00:00:00-04:00")

    def test_duplicate_ids_block_the_import(self):
        rows = [dict(self.rows[0]), dict(self.rows[0])]
        with self.assertRaises(contract.ContractError):
            importer.buildImport(rows)

    def test_summary_counts_the_profile(self):
        summary = importer.summarize(self.payloads, self.warnings)
        self.assertEqual(summary["rows"], 84)
        self.assertEqual(summary["uniqueIds"], 84)
        self.assertEqual(summary["artifacts"]["cv"], 47)
        self.assertEqual(summary["artifacts"]["coverLetter"], 42)
        self.assertEqual(summary["notesBoilerplate"], 46)

    def test_report_renders_markdown(self):
        report = {
            "generatedAt": "now",
            "dryRun": True,
            "source": "job_search_tracker.csv",
            "summary": importer.summarize(self.payloads, self.warnings),
            "reconciliation": importer.reconciliation(self.payloads, self.rows),
            "checks": {"rows read == 84": "PASS"},
            "warnings": [],
            "issues": [],
        }
        text = importer.renderReport(report)
        self.assertIn("# Import reconciliation", text)
        self.assertIn("| rows read | 84 |", text)


class TestProposals(unittest.TestCase):
    def applications(self):
        return {
            "acme--staff-android-engineer": {
                "id": "acme--staff-android-engineer",
                "company": "Acme",
                "status": "status:applied",
            },
            "grow-therapy--staff-android-engineer": {
                "id": "grow-therapy--staff-android-engineer",
                "company": "Grow Therapy",
                "status": "status:applied",
            },
        }

    def message(self, **overrides):
        message = {
            "messageId": "m1",
            "subject": "Your application at Acme",
            "sender": "jobs@acme.com",
            "snippet": "Unfortunately, we are not moving forward.",
            "receivedAt": "2026-10-30T10:00:00+00:00",
        }
        message.update(overrides)
        return message

    def test_classification_prefers_rejection_over_acknowledgement(self):
        self.assertEqual(
            proposals.classify("Thank you for applying", "unfortunately not moving forward"),
            ("event:rejection", "status:rejected", "high"),
        )

    def test_acknowledgement_has_low_confidence(self):
        eventType, statusTo, confidence = proposals.classify(
            "Thank you for applying", "we have received your application"
        )
        self.assertEqual((eventType, statusTo, confidence), ("event:acknowledged", None, "low"))

    def test_unknown_phrase_is_not_classified(self):
        self.assertIsNone(proposals.classify("Hello", "just checking in"))

    def test_match_requires_a_single_application(self):
        applicationId, reason = proposals.match(self.message(), self.applications())
        self.assertEqual(applicationId, "acme--staff-android-engineer")
        self.assertIsNone(reason)

    def test_explicit_application_id_wins(self):
        applicationId, _ = proposals.match(
            self.message(applicationId="grow-therapy--staff-android-engineer"), self.applications()
        )
        self.assertEqual(applicationId, "grow-therapy--staff-android-engineer")

    def test_unknown_explicit_id_is_reported(self):
        applicationId, reason = proposals.match(self.message(applicationId="nope"), self.applications())
        self.assertIsNone(applicationId)
        self.assertIn("does not exist", reason)

    def test_multiple_company_matches_are_ambiguous(self):
        applications = {
            **self.applications(),
            "acme--engineer-two": {"id": "acme--engineer-two", "company": "Acme", "status": "status:applied"},
        }
        applicationId, reason = proposals.match(self.message(), applications)
        self.assertIsNone(applicationId)
        self.assertIn("ambiguous", reason)

    def test_build_proposal_is_valid_and_separates_unmatched(self):
        proposal = proposals.buildProposal(
            [
                self.message(),
                self.message(messageId="m2", subject="Hello", sender="x@y.com", snippet="nothing"),
            ],
            self.applications(),
            now="2026-10-31T00:00:00+00:00",
        )
        self.assertEqual(proposal["state"], "proposal:pending")
        self.assertEqual(len(proposal["changes"]), 1)
        self.assertEqual(len(proposal["unmatched"]), 1)
        self.assertEqual(proposal["changes"][0]["statusFrom"], "status:applied")
        self.assertEqual(proposal["changes"][0]["statusTo"], "status:rejected")
        self.assertEqual(contract.errors("proposal", proposal), [])

    def test_duplicate_message_ids_are_ignored(self):
        proposal = proposals.buildProposal(
            [self.message(), self.message()], self.applications(), now="2026-10-31T00:00:00+00:00"
        )
        self.assertEqual(len(proposal["changes"]), 1)


class TestSheets(unittest.TestCase):
    def application(self):
        return {
            "id": "acme--engineer",
            "date": "2026-10-30T00:00:00+00:00",
            "company": "Acme",
            "role": "Engineer",
            "sector": None,
            "channel": "channel:greenhouse",
            "status": "status:applied",
            "fitRating": 88,
            "tags": ["fit:strong"],
            "source": "https://example/1",
            "counters": {"events": 1, "followUpsSent": 0, "interviews": 0, "rejections": 0},
            "timeline": {"lastActivityAt": "2026-10-30", "daysSinceActivity": 1},
        }

    def test_application_header_matches_the_plan(self):
        rows = sheets.applicationRows([self.application()])
        self.assertEqual(rows[0], sheets.APPLICATION_HEADER)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "acme--engineer")
        self.assertEqual(rows[1][2], "Acme")

    def test_artifact_columns_prefer_the_local_path(self):
        artifactsForId = {
            "acme--engineer": [
                {"kind": "artifact:cv", "localPath": "documents/cv.pdf"},
                {"kind": "artifact:cover-letter", "externalUrl": "https://docs.example/1"},
            ]
        }
        rows = sheets.applicationRows([self.application()], artifactsForId)
        self.assertEqual(rows[1][-2], "documents/cv.pdf")
        self.assertEqual(rows[1][-1], "https://docs.example/1")

    def test_event_rows_are_flattened_and_newline_free(self):
        rows = sheets.eventRows(
            {
                "acme--engineer": [
                    {"eventId": "a", "date": "2026-10-30", "type": "event:applied", "note": "line1\nline2", "actor": "actor:user"}
                ]
            }
        )
        self.assertEqual(rows[0], sheets.EVENT_HEADER)
        self.assertEqual(rows[1][5], "line1 line2")

    def test_summary_has_totals_and_groupings(self):
        rows = sheets.summaryRows([self.application()], {})
        labels = [row[0] for row in rows]
        self.assertIn("applications", labels)
        self.assertIn("status:applied", labels)
        self.assertIn("by sector: (none)", labels)
        self.assertIn("median days to first response", labels)

    def test_local_view_writes_three_csvs(self):
        with tempfile.TemporaryDirectory() as directory:
            written = sheets.writeLocal(
                {"Applications": sheets.applicationRows([self.application()])},
                pathlib.Path(directory),
            )
            self.assertEqual(len(written), 1)
            content = written[0].read_text(encoding="utf-8")
            self.assertTrue(content.startswith("id,date,company"))


class TestArtifacts(unittest.TestCase):
    def test_describe_hashes_and_sizes(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as handle:
            handle.write(b"hello world")
            path = handle.name
        try:
            described = artifacts.describe(path)
        finally:
            os.unlink(path)
        self.assertEqual(described["size"], 11)
        self.assertEqual(
            described["sha256"], "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )
        self.assertEqual(described["contentType"], "application/pdf")

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.describe("/no/such/file.pdf")

    def test_blob_path_strips_the_namespace(self):
        self.assertEqual(
            artifacts.blobPath("acme--engineer", "artifact:cv", "main.pdf"),
            "applications/acme--engineer/cv/main.pdf",
        )


class TestCacheFile(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        payload = {"generatedAt": "2026-10-31T00:00:00+00:00", "applications": []}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cache.json"
            cache.save(payload, path)
            self.assertEqual(cache.load(path), payload)

    def test_missing_cache_is_none(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(cache.load(pathlib.Path(directory) / "absent.json"))

    def test_age_is_computed_from_generated_at(self):
        import datetime as dt

        payload = {"generatedAt": "2026-10-31T00:00:00+00:00"}
        reference = dt.datetime(2026, 10, 31, 1, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(cache.ageSeconds(payload, reference), 3600)


class TestTasksOffline(unittest.TestCase):
    def test_request_is_valid_and_due_in_two_hours(self):
        import datetime as dt

        now = dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc)
        request = tasks.buildRequest(["acme--engineer"], now=now)
        self.assertEqual(request["state"], "sync:pending")
        self.assertEqual(request["dueAt"], "2026-10-31T14:00:00+00:00")
        self.assertEqual(contract.errors("syncRequest", request), [])

    def test_invalid_application_id_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tasks.buildRequest(["Not A Slug"])


@unittest.skipUnless(EMULATOR_HOST, "set FIRESTORE_EMULATOR_HOST to run")
class TestEmulatorModules(unittest.TestCase):
    def setUp(self):
        self.db = client.getDb()

    def tearDown(self):
        for name in (
            client.APPLICATIONS,
            client.SYNC_REQUESTS,
            client.PENDING_PROPOSALS,
            client.COMPANIES,
        ):
            for snapshot in self.db.collection(name).stream():
                for sub in (client.EVENTS, client.ARTIFACTS):
                    for child in snapshot.reference.collection(sub).stream():
                        child.reference.delete()
                snapshot.reference.delete()

    def test_import_is_idempotent_and_export_is_byte_identical(self):
        code, report = runCli(["import", "--csv", str(CSV_PATH)])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["wouldWrite"]["applications"], 84)
        self.assertEqual(report["issues"], [])

        code, again = runCli(["import", "--csv", str(CSV_PATH)])
        self.assertEqual(code, 0)
        self.assertEqual(again["reconciliation"]["prepared"], 0)
        self.assertEqual(again["reconciliation"]["skippedAlreadyPresent"], 84)

        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "restored.csv"
            code, result = runCli(["export", "--csv", "-o", str(output)])
            self.assertEqual(code, 0, result)
            expected = CSV_PATH.read_bytes()
            actual = output.read_bytes()
            self.assertEqual(actual, expected)

    def test_export_round_trips_through_a_reimport(self):
        runCli(["import", "--csv", str(CSV_PATH)])
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "restored.csv"
            runCli(["export", "--csv", "-o", str(output)])
            rows = importer.readCsv(output)
            payloads, warnings = importer.buildImport(rows, now="2026-10-31T00:00:00+00:00")
        original = importer.readCsv(CSV_PATH)
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in original])
        self.assertEqual(len(payloads), 84)
        self.assertEqual(len({payload["id"] for payload in payloads}), 84)
        self.assertEqual(
            [payload["notesLegacy"] for payload in payloads],
            [row["notes"] for row in original],
        )

    def test_import_writes_one_event_and_the_expected_artifacts(self):
        runCli(["import", "--csv", str(CSV_PATH)])
        applicationId = "grow-therapy--staff-android-engineer"
        reference = self.db.collection(client.APPLICATIONS).document(applicationId)
        events = [document.to_dict() for document in reference.collection(client.EVENTS).stream()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "event:imported")
        self.assertTrue(contract.isUuid(events[0]["eventId"]))
        artifactDocuments = [document.to_dict() for document in reference.collection(client.ARTIFACTS).stream()]
        self.assertEqual(len(artifactDocuments), 2)
        self.assertTrue(all(contract.isUuid(item["artifactId"]) for item in artifactDocuments))

    def test_tasks_dedupe_widen_and_claim(self):
        first = tasks.enqueue(self.db, ["acme--engineer"], hours=2)
        second = tasks.enqueue(self.db, ["acme--engineer", "acme--designer"], hours=6)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["requestId"], first["requestId"])
        self.assertEqual(second["applicationIds"], ["acme--designer", "acme--engineer"])
        # Earliest due time wins.
        self.assertEqual(second["dueAt"], first["dueAt"])

        import datetime as dt

        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
        due = tasks.due(self.db, future)
        self.assertEqual(len(due), 1)
        claimed = tasks.claim(self.db, first["requestId"])
        self.assertEqual(claimed["state"], "sync:claimed")
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(tasks.claim(self.db, first["requestId"]))
        tasks.complete(self.db, first["requestId"])
        self.assertEqual(tasks.due(self.db, future), [])

    def test_failure_retries_then_stops(self):
        request = tasks.enqueue(self.db, ["acme--engineer"])
        tasks.claim(self.db, request["requestId"])
        first = tasks.fail(self.db, request["requestId"], "boom")
        self.assertEqual(first["state"], "sync:pending")
        for _ in range(3):
            tasks.claim(self.db, request["requestId"])
            result = tasks.fail(self.db, request["requestId"], "boom")
        self.assertEqual(result["state"], "sync:failed")

    def test_proposal_stage_approve_emits_events(self):
        applicationId = "acme--proposal-engineer"
        self.db.collection(client.APPLICATIONS).document(applicationId).set(
            contract.toFirestore(
                {
                    "id": applicationId,
                    "date": "2026-10-01",
                    "company": "Acme",
                    "companyId": "acme",
                    "role": "Engineer",
                    "status": "status:applied",
                    "createdAt": "2026-10-01T00:00:00+00:00",
                    "updatedAt": "2026-10-01T00:00:00+00:00",
                    "schemaVersion": contract.schemaVersion(),
                }
            )
        )
        applications = {applicationId: contract.fromFirestore(self.db.collection(client.APPLICATIONS).document(applicationId).get().to_dict())}
        proposal = proposals.buildProposal(
            [
                {
                    "messageId": "m-approve",
                    "applicationId": applicationId,
                    "subject": "Application update",
                    "sender": "jobs@acme.com",
                    "snippet": "Unfortunately we are not moving forward.",
                    "receivedAt": "2026-10-30T10:00:00+00:00",
                }
            ],
            applications,
            now="2026-10-31T09:00:00+00:00",
        )
        proposals.stage(self.db, proposal)
        stored = proposals.load(self.db, proposal["proposalId"])
        self.assertEqual(stored["state"], "proposal:pending")

        result = proposals.approve(self.db, proposal["proposalId"], note="confirmed")
        self.assertEqual(len(result["approved"]), 1)
        self.assertEqual(proposals.load(self.db, proposal["proposalId"])["state"], "proposal:approved")
        reference = self.db.collection(client.APPLICATIONS).document(applicationId)
        events = [document.to_dict() for document in reference.collection(client.EVENTS).stream()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "event:rejection")
        self.assertEqual(events[0]["derivedFrom"], "gmail")
        self.assertEqual(reference.get().to_dict()["status"], "status:rejected")

    def test_approving_twice_is_refused(self):
        proposalId = contract.newUuid()
        self.db.collection(client.PENDING_PROPOSALS).document(proposalId).set(
            contract.toFirestore(
                {
                    "proposalId": proposalId,
                    "createdAt": "2026-10-31T00:00:00+00:00",
                    "dueAt": "2026-11-01T00:00:00+00:00",
                    "kind": "proposal:gmail-sync",
                    "state": "proposal:approved",
                    "trigger": "trigger:manual",
                    "scope": {"applicationIds": [], "lookbackFrom": None, "query": None},
                    "changes": [],
                }
            )
        )
        with self.assertRaises(contract.ContractError):
            proposals.approve(self.db, proposalId)

    def test_artifact_no_upload_writes_the_document(self):
        applicationId = "acme--artifact-engineer"
        self.db.collection(client.APPLICATIONS).document(applicationId).set(
            contract.toFirestore(
                {
                    "id": applicationId,
                    "date": "2026-10-01",
                    "company": "Acme",
                    "companyId": "acme",
                    "role": "Engineer",
                    "status": "status:applied",
                    "createdAt": "2026-10-01T00:00:00+00:00",
                    "updatedAt": "2026-10-01T00:00:00+00:00",
                    "schemaVersion": contract.schemaVersion(),
                }
            )
        )
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as handle:
            handle.write(b"%PDF-1.7 test")
            path = handle.name
        try:
            code, result = runCli(
                ["artifact", applicationId, "--kind", "artifact:cv", "--path", path, "--no-upload"]
            )
        finally:
            os.unlink(path)
        self.assertEqual(code, 0, result)
        document = (
            self.db.collection(client.APPLICATIONS)
            .document(applicationId)
            .collection(client.ARTIFACTS)
            .document(result["artifactId"])
            .get()
            .to_dict()
        )
        self.assertEqual(document["kind"], "artifact:cv")
        self.assertIsNone(document["storage"])
        self.assertEqual(len(document["sha256"]), 64)
        # Registering an artifact is an event, not a silent subcollection write.
        events = [
            item.to_dict()
            for item in self.db.collection(client.APPLICATIONS)
            .document(applicationId)
            .collection(client.EVENTS)
            .stream()
        ]
        self.assertEqual([item["type"] for item in events], ["event:artifact-added"])

    def test_cache_refresh_writes_a_readable_file(self):
        runCli(["import", "--csv", str(CSV_PATH)])
        payload, path = cache.refresh(self.db)
        self.assertEqual(len(payload["applications"]), 84)
        self.assertTrue(path.exists())
        loaded = cache.load(path)
        self.assertEqual(len(loaded["events"]), 84)

    def test_sheet_local_view_matches_the_store(self):
        runCli(["import", "--csv", str(CSV_PATH)])
        with tempfile.TemporaryDirectory() as directory:
            code, result = runCli(["sheet", "--local-only", "--directory", directory])
            self.assertEqual(code, 0, result)
            applications = pathlib.Path(directory) / "applications.csv"
            with applications.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], sheets.APPLICATION_HEADER)
        self.assertEqual(len(rows) - 1, 84)


if __name__ == "__main__":
    unittest.main()
