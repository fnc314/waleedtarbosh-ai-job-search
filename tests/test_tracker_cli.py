"""Tests for the CLI write path.

Split in two:

- `TestPayloadResolution` is pure. It never touches Firestore, so it runs in
  every environment and covers the decisions the importer depends on: ID
  derivation, enum normalization, merge semantics, artifact polymorphism.
- `TestCliAgainstEmulator` drives the real handlers against the Firestore
  emulator. It is skipped unless FIRESTORE_EMULATOR_HOST is set, which keeps the
  default `unittest discover` run offline and credential-free.

Run the emulator suite with:

    FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \\
    firebase emulators:exec --only firestore --project job-search-tracker-test \\
      "python -m unittest tests.test_tracker_cli -v"
"""

from __future__ import annotations

import contextlib
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

# Pinned before `client` is imported, because client captures it at import time.
# These tests must never be able to reach the production project.
os.environ["TRACKER_PROJECT"] = "job-search-tracker-test"
if EMULATOR_HOST:
    os.environ["TRACKER_ALLOW_WRITES"] = "1"

import client  # noqa: E402
import contract  # noqa: E402
import tracker  # noqa: E402


def runCli(argv: list[str]) -> tuple[int, dict]:
    """Invoke the CLI, returning (exit code, parsed stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = tracker.main(argv)
    raw = buffer.getvalue().strip()
    return code, (json.loads(raw) if raw else {})


def writePayload(payload: dict) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(payload, handle)
    handle.close()
    return handle.name


class TestPayloadResolution(unittest.TestCase):
    def payload(self, **overrides) -> dict:
        payload = {
            "company": "Acme",
            "role": "Staff Android Engineer",
            "date": "2026-10-30",
            "status": "applied",
        }
        payload.update(overrides)
        return payload

    def test_document_id_is_derived_from_company_and_role(self):
        document, _ = tracker.buildApplication(self.payload())
        self.assertEqual(document["id"], "acme--staff-android-engineer")
        self.assertEqual(document["companyId"], "acme")

    def test_explicit_id_is_honoured(self):
        document, _ = tracker.buildApplication(self.payload(id="legacy-id"))
        self.assertEqual(document["id"], "legacy-id")

    def test_illegal_id_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildApplication(self.payload(id="Not A Slug"))

    def test_status_is_normalized_and_derived(self):
        document, _ = tracker.buildApplication(self.payload(status="rejected"))
        self.assertEqual(document["status"], "status:rejected")
        self.assertEqual(document["statusRank"], 5)
        self.assertFalse(document["isOpen"])

    def test_missing_status_defaults_to_drafting(self):
        payload = self.payload()
        del payload["status"]
        document, _ = tracker.buildApplication(payload)
        self.assertEqual(document["status"], "status:drafting")
        self.assertTrue(document["isOpen"])

    def test_missing_role_stays_null_rather_than_empty_string(self):
        document, _ = tracker.buildApplication(self.payload(role="   "))
        self.assertIsNone(document["role"])
        self.assertEqual(document["id"], "acme")

    def test_channel_accepts_bare_and_namespaced_forms(self):
        self.assertEqual(tracker.normalizeChannel("greenhouse"), "channel:greenhouse")
        self.assertEqual(tracker.normalizeChannel("LinkedIn"), "channel:linkedin")
        self.assertEqual(
            tracker.normalizeChannel("channel:other"), "channel:other"
        )
        self.assertIsNone(tracker.normalizeChannel(""))

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.normalizeChannel("carrier-pigeon")

    def test_unregistered_tag_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.normalizeTag("compensation:above-super-commuter:BOSTON")

    def test_structured_source_flattens_to_a_string_and_keeps_the_detail(self):
        document, _ = tracker.buildApplication(
            self.payload(
                source={
                    "url": "https://boards.example/acme/1",
                    "portal": "greenhouse",
                    "reqId": "1",
                }
            )
        )
        self.assertEqual(document["source"], "https://boards.example/acme/1")
        self.assertEqual(document["metadata"]["source"]["portal"], "greenhouse")

    def test_source_without_a_url_falls_back_to_the_portal(self):
        document, _ = tracker.buildApplication(
            self.payload(source={"portal": "workday"})
        )
        self.assertEqual(document["source"], "workday")

    def test_unknown_source_keys_are_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildApplication(self.payload(source={"url": "x", "typo": "y"}))

    def test_payload_events_are_validated_and_counted(self):
        document, produced = tracker.buildApplication(
            self.payload(
                events=[
                    {"type": "event:applied", "date": "2026-10-30"},
                    {"type": "event:follow-up-sent", "date": "2026-10-31"},
                ]
            )
        )
        self.assertEqual(len(produced), 2)
        self.assertEqual(document["counters"]["events"], 2)
        self.assertEqual(document["counters"]["followUpsSent"], 1)
        self.assertTrue(document["notesRendered"].startswith("2026-10-30 · event:applied"))
        for event in produced:
            self.assertTrue(contract.isUuid(event["eventId"]))

    def test_unknown_event_key_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildApplication(
                self.payload(events=[{"type": "event:applied", "typo": 1}])
            )

    def test_missing_company_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildApplication({"role": "Engineer"})

    def test_absent_optional_blocks_are_omitted_not_nulled(self):
        document, _ = tracker.buildApplication(self.payload())
        for key in ("fitEvaluation", "compensation", "location"):
            self.assertNotIn(key, document)

    def test_merge_unions_tags_and_deep_merges_metadata(self):
        existing, _ = tracker.buildApplication(
            self.payload(
                tags=["fit:strong"],
                metadata={"levelExpected": "staff", "recruiterBand": "130-192"},
            )
        )
        merged, _ = tracker.mergeApplication(
            existing,
            {"company": "Acme", "tags": ["process:referral"], "metadata": {"recruiterBand": "140-200"}},
            now=existing["updatedAt"],
        )
        self.assertEqual(merged["tags"], ["fit:strong", "process:referral"])
        self.assertEqual(merged["metadata"]["recruiterBand"], "140-200")
        self.assertEqual(merged["metadata"]["levelExpected"], "staff")
        self.assertEqual(merged["createdAt"], existing["createdAt"])

    def test_merge_preserves_status_when_the_payload_omits_it(self):
        existing, _ = tracker.buildApplication(self.payload(status="interview"))
        merged, _ = tracker.mergeApplication(
            existing, {"company": "Acme", "fitRating": 91}, now=existing["updatedAt"]
        )
        self.assertEqual(merged["status"], "status:interview")
        self.assertEqual(merged["fitRating"], 91)

    def test_merge_does_not_clear_existing_fields(self):
        existing, _ = tracker.buildApplication(
            self.payload(channel="greenhouse", sector="Tech", fitRating=88)
        )
        merged, _ = tracker.mergeApplication(
            existing, {"company": "Acme", "role": "Staff Android Engineer"}, now=existing["updatedAt"]
        )
        self.assertEqual(merged["channel"], "channel:greenhouse")
        self.assertEqual(merged["sector"], "Tech")
        self.assertEqual(merged["fitRating"], 88)

    def test_artifact_ids_are_uuid_v7(self):
        artifacts = tracker.buildArtifacts(
            [{"kind": "artifact:cv", "localPath": "cv/main_Acme.pdf"}],
            source="manual",
            now="2026-10-30T00:00:00-04:00",
        )
        self.assertTrue(contract.isUuid(artifacts[0]["artifactId"]))
        self.assertEqual(artifacts[0]["filename"], "main_Acme.pdf")

    def test_artifact_accepts_a_google_docs_url_as_external(self):
        artifacts = tracker.buildArtifacts(
            [{"kind": "artifact:cv", "externalUrl": "https://docs.google.com/document/d/1"}],
            source="imported",
            now="2026-10-30T00:00:00-04:00",
        )
        self.assertEqual(artifacts[0]["externalUrl"], "https://docs.google.com/document/d/1")
        self.assertIsNone(artifacts[0]["localPath"])

    def test_artifact_without_a_location_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildArtifacts(
                [{"kind": "artifact:cv", "filename": "x.pdf"}],
                source="manual",
                now="2026-10-30T00:00:00-04:00",
            )

    def test_artifact_with_two_locations_is_rejected(self):
        with self.assertRaises(contract.ContractError):
            tracker.buildArtifacts(
                [
                    {
                        "kind": "artifact:cv",
                        "localPath": "cv/x.pdf",
                        "externalUrl": "https://example/x",
                    }
                ],
                source="manual",
                now="2026-10-30T00:00:00-04:00",
            )


class TestLabelInventory(unittest.TestCase):
    """`--label` enumerates against what Gmail actually had last run."""

    def write(self, payload) -> str:
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, handle)
        handle.close()
        return handle.name

    def test_accepts_a_bare_list_and_normalizes_entries(self):
        path = self.write([{"id": "Label_1", "name": "JOBS"}])
        try:
            labels = tracker.loadLabels(path)
        finally:
            os.unlink(path)
        self.assertEqual(labels, [{"id": "Label_1", "name": "JOBS", "type": "user"}])

    def test_accepts_the_wrapped_shape_the_fetcher_writes(self):
        path = self.write({"labels": [{"name": "JOBS/Applications", "type": "user"}]})
        try:
            labels = tracker.loadLabels(path)
        finally:
            os.unlink(path)
        self.assertEqual(labels[0]["name"], "JOBS/Applications")
        self.assertEqual(labels[0]["id"], "")

    def test_a_nameless_label_is_rejected(self):
        path = self.write({"labels": [{"id": "Label_1"}]})
        try:
            with self.assertRaises(contract.ContractError):
                tracker.loadLabels(path)
        finally:
            os.unlink(path)


class TestArtifactPathResolution(unittest.TestCase):
    """Backfill decides which imported locations have bytes worth uploading."""

    root = pathlib.Path("/repo")

    def test_repo_relative_local_path_is_anchored_at_the_root(self):
        resolved = tracker.resolveArtifactPath(
            {"localPath": "documents/a.pdf"}, self.root, includeDrive=True
        )
        self.assertEqual(resolved, self.root / "documents/a.pdf")

    def test_absolute_local_path_is_left_alone(self):
        resolved = tracker.resolveArtifactPath(
            {"localPath": "/tmp/a.pdf"}, self.root, includeDrive=True
        )
        self.assertEqual(resolved, pathlib.Path("/tmp/a.pdf"))

    def test_drive_path_is_used_when_the_drive_scan_is_on(self):
        resolved = tracker.resolveArtifactPath(
            {"driveUrl": "/Users/x/Library/CloudStorage/a.docx"}, self.root, includeDrive=True
        )
        self.assertEqual(
            resolved, pathlib.Path("/Users/x/Library/CloudStorage/a.docx")
        )

    def test_drive_path_is_ignored_when_the_drive_scan_is_off(self):
        self.assertIsNone(
            tracker.resolveArtifactPath(
                {"driveUrl": "/Users/x/a.docx"}, self.root, includeDrive=False
            )
        )

    def test_google_doc_stub_is_never_uploaded(self):
        # A .gdoc is a shortcut to a Google Doc; its bytes are not the document,
        # so uploading it would store a dead pointer.
        self.assertIsNone(
            tracker.resolveArtifactPath({"driveUrl": "/Users/x/a.gdoc"}, self.root, includeDrive=True)
        )

    def test_external_url_has_no_local_file(self):
        self.assertIsNone(
            tracker.resolveArtifactPath(
                {"externalUrl": "https://example.com/a.pdf"}, self.root, includeDrive=True
            )
        )


class TestWriteGate(unittest.TestCase):
    def test_writes_are_refused_without_the_opt_in(self):
        saved = os.environ.pop("TRACKER_ALLOW_WRITES", None)
        try:
            with self.assertRaises(client.WriteNotAllowed):
                client.assertWritesAllowed("an application")
        finally:
            if saved is not None:
                os.environ["TRACKER_ALLOW_WRITES"] = saved

    def test_the_refusal_explains_how_to_proceed(self):
        saved = os.environ.pop("TRACKER_ALLOW_WRITES", None)
        try:
            with self.assertRaises(client.WriteNotAllowed) as caught:
                client.assertWritesAllowed("an application")
            self.assertIn("TRACKER_ALLOW_WRITES=1", str(caught.exception))
        finally:
            if saved is not None:
                os.environ["TRACKER_ALLOW_WRITES"] = saved

    def test_writes_are_allowed_with_the_opt_in(self):
        saved = os.environ.get("TRACKER_ALLOW_WRITES")
        os.environ["TRACKER_ALLOW_WRITES"] = "1"
        try:
            client.assertWritesAllowed("an application")
        finally:
            if saved is None:
                os.environ.pop("TRACKER_ALLOW_WRITES", None)
            else:
                os.environ["TRACKER_ALLOW_WRITES"] = saved


@unittest.skipUnless(EMULATOR_HOST, "set FIRESTORE_EMULATOR_HOST to run")
class TestCliAgainstEmulator(unittest.TestCase):
    """End-to-end coverage of the write path against a real Firestore API."""

    def setUp(self):
        import uuid

        self.db = client.getDb()
        self.suffix = uuid.uuid4().hex[:8]
        self.id = f"acme--engineer-{self.suffix}"
        self.added: list[str] = []

    def tearDown(self):
        for applicationId in self.added:
            reference = self.db.collection(client.APPLICATIONS).document(applicationId)
            for event in reference.collection(client.EVENTS).stream():
                event.reference.delete()
            for artifact in reference.collection(client.ARTIFACTS).stream():
                artifact.reference.delete()
            reference.delete()

    def add(self, **overrides) -> tuple[int, dict]:
        payload = {
            "id": self.id,
            "company": "Acme",
            "role": f"Engineer {self.suffix}",
            "date": "2026-10-30",
            "status": "applied",
            "fitRating": 88,
        }
        payload.update(overrides)
        path = writePayload(payload)
        try:
            code, result = runCli(["add", "--json", path])
        finally:
            os.unlink(path)
        if code == 0:
            self.added.append(result["wrote"])
        return code, result

    def test_add_then_get_returns_the_document_and_its_event(self):
        code, result = self.add(events=[{"type": "event:applied", "date": "2026-10-30"}])
        self.assertEqual(code, 0)
        code, fetched = runCli(["get", self.id])
        self.assertEqual(code, 0)
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["application"]["id"], self.id)
        self.assertEqual(fetched["application"]["schemaVersion"], contract.schemaVersion())
        self.assertEqual(len(fetched["events"]), 1)
        self.assertTrue(contract.isUuid(fetched["events"][0]["eventId"]))
        # Derived from the event, not the payload.
        self.assertEqual(
            fetched["application"]["counters"]["events"], 1
        )

    def test_add_rejects_a_duplicate(self):
        self.assertEqual(self.add()[0], 0)
        code, _ = self.add()
        self.assertEqual(code, 2)

    def test_backfill_dry_run_hashes_files_without_writing(self):
        self.add()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "cv.pdf"
            source.write_bytes(b"%PDF-1.4 test")
            artifactId = contract.newUuid()
            reference = (
                self.db.collection(client.APPLICATIONS)
                .document(self.id)
                .collection(client.ARTIFACTS)
                .document(artifactId)
            )
            reference.set(
                {
                    "artifactId": artifactId,
                    "kind": "artifact:cv",
                    "filename": "cv.pdf",
                    "createdAt": "2026-10-30T00:00:00+00:00",
                    "source": "imported",
                    "localPath": "cv.pdf",
                }
            )
            code, result = runCli(["backfill", "--root", directory])
        self.assertEqual(code, 0)
        self.assertTrue(result["dryRun"])
        self.assertGreaterEqual(result["candidates"], 1)
        self.assertEqual(result["updated"], 0)
        stored = reference.get().to_dict()
        self.assertIsNone(stored.get("storage"))
        self.assertIsNone(stored.get("sha256"))

    def test_dry_run_writes_nothing(self):
        path = writePayload({"id": self.id, "company": "Acme", "role": "Dry", "status": "applied"})
        try:
            code, result = runCli(["add", "--json", path, "--dry-run"])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertTrue(result["dryRun"])
        _, fetched = runCli(["get", self.id])
        self.assertFalse(fetched["found"])

    def test_dry_run_works_with_the_write_gate_off(self):
        """A dry run must not need the write opt-in."""
        saved = os.environ.pop("TRACKER_ALLOW_WRITES", None)
        path = writePayload({"id": self.id, "company": "Acme", "role": "Dry", "status": "applied"})
        try:
            code, result = runCli(["add", "--json", path, "--dry-run"])
            blocked, _ = runCli(["add", "--json", path])
        finally:
            os.unlink(path)
            if saved is not None:
                os.environ["TRACKER_ALLOW_WRITES"] = saved
        self.assertEqual(code, 0)
        self.assertTrue(result["dryRun"])
        self.assertEqual(blocked, 2)
        _, fetched = runCli(["get", self.id])
        self.assertFalse(fetched["found"])

    def test_status_emits_an_event_and_closes_the_application(self):
        self.add()
        code, result = runCli(
            ["status", self.id, "rejected", "--date", "2026-11-05", "--note", "position filled"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["statusFrom"], "status:applied")
        self.assertEqual(result["statusTo"], "status:rejected")
        self.assertFalse(result["isOpen"])
        _, fetched = runCli(["get", self.id])
        application = fetched["application"]
        self.assertEqual(application["status"], "status:rejected")
        self.assertFalse(application["isOpen"])
        self.assertEqual(application["counters"]["rejections"], 1)
        self.assertIsNotNone(application["notesRendered"])
        self.assertIn("event:rejection", application["notesRendered"])

    def test_status_refuses_a_noop_transition(self):
        self.add()
        code, _ = runCli(["status", self.id, "applied"])
        self.assertEqual(code, 2)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(len(fetched["events"]), 0)

    def test_status_correction_is_recorded_as_a_correction(self):
        self.add()
        code, result = runCli(["status", self.id, "applied", "--correction", "--note", "was a draft"])
        self.assertEqual(code, 0)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["events"][0]["type"], "event:status-corrected")

    def test_score_records_the_rating_and_keeps_the_previous_one(self):
        self.add()
        code, result = runCli(["score", self.id, "--fit", "73", "--note", "salary below target"])
        self.assertEqual(code, 0)
        self.assertEqual(result["fitRating"], 73)
        self.assertEqual(result["previousFitRating"], 88)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["application"]["fitRating"], 73)
        scoreEvent = fetched["events"][-1]
        self.assertEqual(scoreEvent["type"], "event:fit-recalculated")
        self.assertEqual(scoreEvent["metadata"]["fitRating"], 73)
        self.assertEqual(scoreEvent["metadata"]["previousFitRating"], 88)
        # A score is not progress: the funnel must not move.
        self.assertEqual(fetched["application"]["status"], "status:applied")

    def test_score_rejects_a_rating_outside_the_range(self):
        self.add()
        self.assertEqual(runCli(["score", self.id, "--fit", "101"])[0], 2)
        self.assertEqual(runCli(["score", self.id, "--fit", "-1"])[0], 2)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["application"]["fitRating"], 88)
        self.assertEqual(len(fetched["events"]), 0)

    def test_score_dry_run_writes_nothing(self):
        self.add()
        code, result = runCli(["score", self.id, "--fit", "60", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(result["dryRun"])
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["application"]["fitRating"], 88)

    def test_score_is_blocked_by_the_write_gate(self):
        self.add()
        saved = os.environ.pop("TRACKER_ALLOW_WRITES", None)
        try:
            code, _ = runCli(["score", self.id, "--fit", "60"])
        finally:
            if saved is not None:
                os.environ["TRACKER_ALLOW_WRITES"] = saved
        self.assertEqual(code, 2)

    def test_event_appends_without_moving_the_funnel(self):
        self.add()
        code, result = runCli(
            ["event", self.id, "--type", "event:follow-up-sent", "--note", "nudged recruiter"]
        )
        self.assertEqual(code, 0)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["application"]["status"], "status:applied")
        self.assertEqual(fetched["application"]["counters"]["followUpsSent"], 1)

    def test_event_can_move_the_funnel_via_status_to(self):
        self.add()
        path = writePayload({"statusTo": "status:interview", "note": "call booked"})
        try:
            code, _ = runCli(
                ["event", self.id, "--type", "event:interview-scheduled", "--json", path]
            )
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        _, fetched = runCli(["get", self.id])
        self.assertEqual(fetched["application"]["status"], "status:interview")
        self.assertTrue(fetched["application"]["isOpen"])

    def test_upsert_merges_into_the_existing_document(self):
        self.add(tags=["fit:strong"], metadata={"levelExpected": "staff"})
        path = writePayload(
            {
                "id": self.id,
                "company": "Acme",
                "tags": ["process:referral"],
                "metadata": {"recruiterBand": "130-192"},
            }
        )
        try:
            code, result = runCli(["upsert", "--json", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertFalse(result["created"])
        _, fetched = runCli(["get", self.id])
        application = fetched["application"]
        self.assertEqual(set(application["tags"]), {"fit:strong", "process:referral"})
        self.assertEqual(application["metadata"]["levelExpected"], "staff")
        self.assertEqual(application["metadata"]["recruiterBand"], "130-192")
        self.assertEqual(application["status"], "status:applied")

    def test_upsert_creates_when_absent(self):
        path = writePayload(
            {"id": self.id, "company": "Acme", "role": "New Role", "status": "drafting"}
        )
        try:
            code, result = runCli(["upsert", "--json", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertTrue(result["created"])
        self.added.append(result["wrote"])

    def test_artifacts_are_written_in_the_same_batch(self):
        code, result = self.add(
            artifacts=[{"kind": "artifact:cv", "localPath": "cv/main_Acme.pdf"}]
        )
        self.assertEqual(code, 0)
        stored = [
            document.to_dict()
            for document in self.db.collection(client.APPLICATIONS)
            .document(self.id)
            .collection(client.ARTIFACTS)
            .stream()
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["kind"], "artifact:cv")

    def test_list_filters_by_status(self):
        self.add()
        code, result = runCli(["list", "--status", "applied"])
        self.assertEqual(code, 0)
        self.assertIn(self.id, [document["id"] for document in result["documents"]])


if __name__ == "__main__":
    unittest.main()
