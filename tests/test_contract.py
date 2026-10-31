"""Tests for scripts/contract.py — the single implementation of the contract.

Everything here is pure: no Firestore, no credentials, no network. The point is
to lock down the behaviours the importer and every writer depend on, so a
refactor cannot quietly change how an ID is derived or how a status normalizes.
"""

from __future__ import annotations

import datetime
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import contract  # noqa: E402


class TestEnums(unittest.TestCase):
    def test_enums_resolve_from_the_schema(self):
        self.assertIn("status:applied", contract.enums("status"))
        self.assertIn("event:applied", contract.enums("eventType"))
        self.assertEqual(len(contract.regions()), 6)

    def test_unknown_enum_name_is_a_contract_error(self):
        with self.assertRaises(contract.ContractError):
            contract.enums("nonsense")

    def test_status_rank_is_a_partial_order_over_the_enum(self):
        rank = contract.statusRank()
        self.assertEqual(set(rank), set(contract.statusValues()))
        self.assertLess(rank["status:drafting"], rank["status:applied"])
        self.assertLess(rank["status:offer"], rank["status:hired"])

    def test_terminal_statuses_exclude_every_open_status(self):
        terminal = contract.terminalStatuses()
        self.assertNotIn("status:applied", terminal)
        self.assertNotIn("status:interview", terminal)
        self.assertNotIn("status:offer", terminal)
        self.assertIn("status:rejected", terminal)


class TestNormalizeStatus(unittest.TestCase):
    def test_legacy_bare_values_normalize(self):
        self.assertEqual(contract.normalizeStatus("applied"), "status:applied")
        self.assertEqual(contract.normalizeStatus("rejected"), "status:rejected")
        self.assertEqual(contract.normalizeStatus("drafting"), "status:drafting")

    def test_case_and_whitespace_do_not_matter(self):
        self.assertEqual(contract.normalizeStatus("  APPLIED "), "status:applied")
        self.assertEqual(contract.normalizeStatus("Interview"), "status:interview")

    def test_both_legacy_spellings_of_no_response_work(self):
        self.assertEqual(contract.normalizeStatus("no_response"), "status:no-response")
        self.assertEqual(contract.normalizeStatus("no response"), "status:no-response")

    def test_already_namespaced_values_pass_through(self):
        self.assertEqual(
            contract.normalizeStatus("status:offer-declined"), "status:offer-declined"
        )

    def test_unrecognized_status_raises_rather_than_guessing(self):
        with self.assertRaises(contract.ContractError):
            contract.normalizeStatus("ghosted")
        with self.assertRaises(contract.ContractError):
            contract.normalizeStatus("")

    def test_is_terminal_agrees_with_the_enum(self):
        self.assertTrue(contract.isTerminal("rejected"))
        self.assertTrue(contract.isTerminal("status:hired"))
        self.assertFalse(contract.isTerminal("interview"))


class TestSlugAndDocumentId(unittest.TestCase):
    def test_slug_is_ascii_kebab(self):
        self.assertEqual(contract.slug("DoorDash"), "doordash")
        self.assertEqual(contract.slug("PPG Industries"), "ppg-industries")
        self.assertEqual(contract.slug("AT&T"), "at-t")

    def test_slug_does_not_emit_leading_or_trailing_hyphens(self):
        self.assertEqual(contract.slug("  Acme  "), "acme")
        self.assertEqual(contract.slug("!!!Acme!!!"), "acme")
        for value in ("Acme", "  Acme  ", "-(Acme)-", "A  B"):
            produced = contract.slug(value)
            self.assertFalse(produced.startswith("-"), produced)
            self.assertFalse(produced.endswith("-"), produced)

    def test_slug_strips_accents_rather_than_dropping_letters(self):
        self.assertEqual(contract.slug("Zürich"), "zurich")

    def test_slug_falls_back_when_everything_is_stripped(self):
        self.assertEqual(contract.slug("!!!", fallback="unknown"), "unknown")
        self.assertEqual(contract.slug(""), "")

    def test_document_id_matches_the_historical_scheme(self):
        self.assertEqual(
            contract.documentId("DoorDash", "Software Engineer, DevEx"),
            "doordash--software-engineer-devex",
        )

    def test_document_id_without_a_role_is_the_bare_company(self):
        self.assertEqual(contract.documentId("Acme", None), "acme")
        self.assertEqual(contract.documentId("Acme", ""), "acme")

    def test_document_id_requires_a_company(self):
        with self.assertRaises(contract.ContractError):
            contract.documentId("", "Engineer")

    def test_document_id_satisfies_the_schema_pattern(self):
        import re

        pattern = re.compile(contract.definition("documentId")["pattern"])
        for company, role in (
            ("DoorDash", "Software Engineer, DevEx"),
            ("AT&T", "Principal"),
            ("Acme", None),
            ("Zürich Insurance", "Data (Platform) Engineer"),
        ):
            produced = contract.documentId(company, role)
            self.assertRegex(produced, pattern, f"{company!r}/{role!r} -> {produced!r}")
            self.assertLessEqual(len(produced), 191)


class TestUuid(unittest.TestCase):
    def test_new_uuid_is_a_canonical_uuid_v7(self):
        value = contract.newUuid()
        self.assertRegex(
            value,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(len(value), 36)
        self.assertTrue(contract.isUuid(value))

    def test_generation_is_local(self):
        """No network, no service: generating IDs must work offline."""
        import uuid

        parsed = uuid.UUID(contract.newUuid())
        self.assertEqual(parsed.version, 7)
        self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_uuids_are_unique(self):
        values = {contract.newUuid() for _ in range(500)}
        self.assertEqual(len(values), 500)

    def test_uuids_are_strictly_increasing_in_creation_order(self):
        """Two events written back-to-back must not order randomly."""
        values = [contract.newUuid() for _ in range(500)]
        self.assertEqual(values, sorted(values))

    def test_uuid_timestamp_is_close_to_now(self):
        value = contract.newUuid()
        millis = contract.uuidTimestamp(value)
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        self.assertLess(abs(now - millis), 2_000)

    def test_non_uuid_v7_is_rejected(self):
        v4 = "018f3c1a-4b2e-4d3f-9a41-2c6b8e5d0f17"          # wrong version nibble
        badVariant = "018f3c1a-4b2e-7d3f-1a41-2c6b8e5d0f17"   # wrong variant nibble
        for bad in ("", "not-a-uuid", v4, badVariant, "01JA5Z3M8Q9V0W1X2Y3Z4A5B6C"):
            self.assertFalse(contract.isUuid(bad), bad)
            with self.assertRaises(contract.ContractError, msg=bad):
                contract.uuidTimestamp(bad)

    def test_uuid_pattern_matches_the_schema_definition(self):
        import re

        pattern = re.compile(contract.definition("uuidV7")["pattern"])
        for _ in range(50):
            self.assertRegex(contract.newUuid(), pattern)


class TestDerivedFields(unittest.TestCase):
    def test_derived_fields_cover_every_status(self):
        for status in contract.statusValues():
            derived = contract.deriveStatusFields(status)
            self.assertEqual(derived["status"], status)
            self.assertEqual(derived["statusRank"], contract.statusRank()[status])
            self.assertEqual(
                derived["isOpen"], status not in contract.terminalStatuses()
            )
            self.assertIsInstance(derived["isOpen"], bool)

    def test_derived_fields_accept_legacy_input(self):
        derived = contract.deriveStatusFields("REJECTED")
        self.assertEqual(derived["status"], "status:rejected")
        self.assertFalse(derived["isOpen"])


class TestValidation(unittest.TestCase):
    def validApplication(self) -> dict:
        return {
            "id": "acme--engineer",
            "date": "2026-10-30",
            "company": "Acme",
            "companyId": "acme",
            "status": "status:applied",
            "schemaVersion": contract.schemaVersion(),
            "createdAt": "2026-10-30T00:00:00-04:00",
            "updatedAt": "2026-10-30T00:00:00-04:00",
        }

    def test_minimal_document_is_valid(self):
        self.assertEqual(contract.errors("application", self.validApplication()), [])

    def test_validate_returns_the_document(self):
        document = self.validApplication()
        self.assertIs(contract.validate("application", document), document)

    def test_validate_raises_with_a_located_message(self):
        bad = dict(self.validApplication(), fitRating=900)
        with self.assertRaises(contract.ContractError) as caught:
            contract.validate("application", bad)
        self.assertIn("fitRating", str(caught.exception))

    def test_errors_report_the_path_not_just_the_message(self):
        bad = dict(self.validApplication(), status="applied")
        problems = contract.errors("application", bad)
        self.assertTrue(problems)
        self.assertTrue(any(problem.startswith("status:") for problem in problems))

    def test_unknown_definition_is_a_contract_error(self):
        with self.assertRaises(contract.ContractError):
            contract.definition("doesNotExist")


class TestFirestoreRoundTrip(unittest.TestCase):
    def test_date_only_becomes_a_datetime_at_midnight(self):
        converted = contract.toFirestore({"date": "2026-10-30"})
        self.assertIsInstance(converted["date"], datetime.datetime)
        self.assertEqual(converted["date"].year, 2026)
        self.assertEqual(converted["date"].month, 10)
        self.assertEqual(converted["date"].day, 30)
        self.assertEqual(converted["date"].hour, 0)

    def test_offset_timestamp_keeps_its_instant(self):
        converted = contract.toFirestore({"ts": "2026-10-30T14:22:00-04:00"})
        self.assertEqual(
            converted["ts"], datetime.datetime(2026, 10, 30, 18, 22, tzinfo=datetime.timezone.utc)
        )

    def test_conversion_is_recursive_over_dicts_and_lists(self):
        converted = contract.toFirestore(
            {"timeline": {"lastActivityAt": "2026-10-30"}, "dates": ["2026-10-31"]}
        )
        self.assertIsInstance(converted["timeline"]["lastActivityAt"], datetime.datetime)
        self.assertIsInstance(converted["dates"][0], datetime.datetime)

    def test_non_date_strings_are_left_alone(self):
        converted = contract.toFirestore(
            {"status": "status:applied", "note": "a 2026-10-30 mention", "n": 3}
        )
        self.assertEqual(converted["status"], "status:applied")
        self.assertEqual(converted["note"], "a 2026-10-30 mention")
        self.assertEqual(converted["n"], 3)

    def test_from_firestore_returns_iso_strings(self):
        moment = datetime.datetime(2026, 10, 30, 0, 0)
        self.assertEqual(contract.fromFirestore({"d": moment})["d"], "2026-10-30T00:00:00")

    def test_round_trip_is_stable(self):
        original = {"date": "2026-10-30", "nested": {"ts": "2026-10-30T14:22:00-04:00"}}
        once = contract.fromFirestore(contract.toFirestore(original))
        twice = contract.fromFirestore(contract.toFirestore(once))
        self.assertEqual(once, twice)

    def test_round_trip_preserves_the_original_offset(self):
        """A local-time timestamp comes back local, not silently re-signed as UTC."""
        once = contract.fromFirestore(
            contract.toFirestore({"ts": "2026-10-30T14:22:00-04:00"})
        )
        self.assertEqual(once["ts"], "2026-10-30T14:22:00-04:00")
        parsed = datetime.datetime.fromisoformat(once["ts"])
        self.assertEqual(
            parsed.astimezone(datetime.timezone.utc),
            datetime.datetime(2026, 10, 30, 18, 22, tzinfo=datetime.timezone.utc),
        )

    def test_round_trip_preserves_a_date_only_calendar_day(self):
        once = contract.fromFirestore(contract.toFirestore({"date": "2026-10-30"}))
        self.assertEqual(once["date"], "2026-10-30T00:00:00")
        # Naive midnight means no reader west of UTC sees the previous day.
        self.assertTrue(once["date"].startswith("2026-10-30"))


class TestLegacyMaps(unittest.TestCase):
    def test_status_map_targets_are_real_status_values(self):
        values = set(contract.statusValues())
        for target in contract.legacyStatusMap().values():
            self.assertIn(target, values)

    def test_field_map_covers_the_declared_header(self):
        header = contract.load()["x-legacy-csv"]["header"]
        self.assertEqual(set(contract.legacyFieldMap()), set(header))

    def test_artifact_path_rule_declares_the_http_url_case(self):
        rule = contract.load()["x-legacy-csv"]["artifactPathRule"]
        self.assertEqual(rule["httpUrl"], "artifacts[].externalUrl")


if __name__ == "__main__":
    unittest.main()
