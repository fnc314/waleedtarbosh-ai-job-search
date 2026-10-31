"""Contract tests for tracker.schema.json.

These guard the two things that are expensive to fix later: identifier
conventions, and drift between the enum registry and the fields it constrains.
Everything here runs without Firestore credentials — it validates the JSON
boundary only, which is exactly what scripts/tracker.py enforces on write.
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import unittest

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "tracker.schema.json"
CSV_PATH = ROOT / "job_search_tracker.csv"

LOWER_PASCAL = re.compile(r"^[a-z][a-zA-Z0-9]*$")

# namespace:kebab-value, then an optional UPPERCASE qualifier segment.
# The qualifier form is what makes compensation:above-super-commuter:NYC legal
# while still rejecting compensation:above-super-commuter:boston.
NAMESPACED_ENUM = re.compile(
    r"^[a-z][a-z0-9-]*(:[a-z][a-z0-9-]*)+(:[A-Z][A-Z0-9-]*)?$"
)

# Enum definitions whose values are single tokens rather than namespaced
# identifiers. Keeping this list explicit means a new namespaced enum cannot
# silently opt out of the convention check.
UNNAMESPACED_ENUMS = {
    "confidenceValue",   # high/medium/low: already unambiguous
    "derivedFromValue",  # manual/notes-regex/gmail/csv-import: provenance, not identity
    # regionValue is not an enum of values; it is the qualifier vocabulary that
    # tags draw their trailing segment from, and qualifiers are UPPERCASE by
    # design. Checked properly by test_every_tag_qualifier_is_a_registered_region.
    "regionValue",
}

# Document-shaped definitions: these are what land in Firestore and therefore
# must obey the key convention.
DOCUMENT_DEFS = (
    "application",
    "event",
    "artifact",
    "proposal",
    "syncRequest",
    "company",
    "contact",
)


def loadSchema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def iterPropertyBlocks(node, path="$"):
    """Yield (path, property-name-set) for every `properties` block, recursively."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            yield path, set(props)
        for key, child in node.items():
            yield from iterPropertyBlocks(child, f"{path}.{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from iterPropertyBlocks(child, f"{path}[{index}]")


def enumValues(schema: dict, defName: str) -> list:
    return schema["$defs"][defName]["enum"]


class TestSchemaIntegrity(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()

    def test_declares_draft_2020_12(self):
        self.assertEqual(
            self.schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )

    def test_schema_version_is_four(self):
        self.assertEqual(self.schema["schemaVersion"], 4)

    def test_revision_log_is_appended_and_ordered(self):
        revisions = self.schema["x-revision-log"]
        versions = [entry["version"] for entry in revisions]
        self.assertEqual(versions, sorted(versions))
        self.assertIn(self.schema["schemaVersion"], versions)
        for entry in revisions:
            self.assertTrue(entry["note"].strip())

    def test_every_generated_id_field_references_the_uuid_definition(self):
        for documentName, fieldName in (
            ("event", "eventId"),
            ("artifact", "artifactId"),
            ("proposal", "proposalId"),
            ("syncRequest", "requestId"),
        ):
            field = self.schema["$defs"][documentName]["properties"][fieldName]
            self.assertEqual(
                field.get("$ref"), "#/$defs/uuidV7", f"{documentName}.{fieldName}"
            )

    def test_schema_is_itself_a_valid_metaschema_instance(self):
        jsonschema.Draft202012Validator.check_schema(self.schema)

    def test_application_schema_pins_the_declared_version(self):
        app = self.schema["$defs"]["application"]["properties"]["schemaVersion"]
        self.assertEqual(app["const"], self.schema["schemaVersion"])


class TestNamingConventions(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()

    def test_every_document_key_is_lower_pascal_case(self):
        violations = []
        for defName in DOCUMENT_DEFS:
            node = self.schema["$defs"][defName]
            for path, props in iterPropertyBlocks(node, f"$defs.{defName}"):
                for prop in props:
                    if not LOWER_PASCAL.match(prop):
                        violations.append(f"{path}.{prop}")
        self.assertEqual(
            violations, [], f"keys violate lowerPascalCase: {violations}"
        )

    def test_no_snake_case_keys_anywhere_in_defs(self):
        violations = []
        for path, props in iterPropertyBlocks(self.schema["$defs"], "$defs"):
            for prop in props:
                if "_" in prop:
                    violations.append(f"{path}.{prop}")
        self.assertEqual(violations, [], f"snake_case keys forbidden: {violations}")

    def test_no_leading_capital_keys_anywhere_in_defs(self):
        violations = []
        for path, props in iterPropertyBlocks(self.schema["$defs"], "$defs"):
            for prop in props:
                if prop[0].isupper():
                    violations.append(f"{path}.{prop}")
        self.assertEqual(violations, [], f"leading-capital keys forbidden: {violations}")

    def test_naming_block_documents_the_forbidden_list(self):
        forbidden = " ".join(self.schema["x-naming"]["forbidden"])
        self.assertIn("lower_snake_case", forbidden)
        self.assertIn("leading-capital", forbidden)


class TestEnumRegistry(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()

    def test_namespaced_enums_follow_namespace_kebab(self):
        violations = []
        for defName, node in self.schema["$defs"].items():
            if not defName.endswith("Value") or "enum" not in node:
                continue
            if defName in UNNAMESPACED_ENUMS:
                continue
            for value in node["enum"]:
                if not NAMESPACED_ENUM.match(value):
                    violations.append(f"{defName}: {value!r}")
        self.assertEqual(violations, [], f"enum values not namespaced: {violations}")

    def test_enum_values_are_unique(self):
        for defName, node in self.schema["$defs"].items():
            if defName.endswith("Value") and "enum" in node:
                values = node["enum"]
                self.assertEqual(
                    len(values), len(set(values)), f"{defName} has duplicate values"
                )

    def test_status_rank_covers_exactly_the_status_enum(self):
        node = self.schema["$defs"]["statusValue"]
        enum = set(node["enum"])
        rank = node["x-status-rank"]
        self.assertEqual(set(rank), enum)
        # A rank map that is not a permutation hides a real ordering bug.
        self.assertEqual(sorted(rank.values()), list(range(len(enum))))

    def test_terminal_statuses_are_a_subset_of_the_status_enum(self):
        node = self.schema["$defs"]["statusValue"]
        extra = set(node["x-terminal"]) - set(node["enum"])
        self.assertEqual(extra, set(), f"terminal statuses outside enum: {extra}")

    def test_every_tag_qualifier_is_a_registered_region(self):
        regions = set(self.schema["$defs"]["regionValue"]["enum"])
        violations = []
        for tag in self.schema["$defs"]["tagValue"]["enum"]:
            segments = tag.split(":")
            if len(segments) == 3:
                qualifier = segments[2]
                if qualifier not in regions:
                    violations.append(tag)
        self.assertEqual(
            violations, [], f"tag qualifiers not in regionValue: {violations}"
        )


class TestLegacyImportMap(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()
        self.legacy = self.schema["x-legacy-csv"]

    def test_field_map_covers_every_csv_column(self):
        self.assertEqual(set(self.legacy["fieldMap"]), set(self.legacy["header"]))

    def test_artifact_path_rule_accounts_for_every_form_in_the_file(self):
        """The profile is the evidence; the rule must not omit a form it found."""
        rule = self.legacy["artifactPathRule"]
        for form in ("httpUrl", "absolutePath", "repoRelativePath", "empty", "missingOnDisk"):
            self.assertIn(form, rule, f"artifactPathRule missing {form!r}")

    def test_notes_rule_forbids_treating_boilerplate_as_evidence(self):
        rule = self.legacy["notesRule"]
        self.assertIn("must not treat that boilerplate as evidence", rule["description"])
        self.assertIn("Added via historical gmail sync", rule["boilerplateValues"])

    def test_profile_row_count_matches_the_file(self):
        if not CSV_PATH.exists():
            self.skipTest("CSV already retired")
        with CSV_PATH.open(newline="", encoding="utf-8") as handle:
            actual = list(csv.DictReader(handle))
        self.assertEqual(self.legacy["profile"]["rows"], len(actual))

    def test_status_map_targets_are_real_status_values(self):
        enum = set(enumValues(self.schema, "statusValue"))
        for source, target in self.legacy["statusMap"].items():
            self.assertIn(target, enum, f"statusMap[{source!r}] -> {target!r}")

    @unittest.skipUnless(CSV_PATH.exists(), "CSV already retired")
    def test_csv_header_matches_declared_legacy_header(self):
        with CSV_PATH.open(newline="", encoding="utf-8") as handle:
            actual = next(csv.reader(handle))
        self.assertEqual(actual, self.legacy["header"])

    @unittest.skipUnless(CSV_PATH.exists(), "CSV already retired")
    def test_every_status_in_the_csv_is_importable(self):
        statusMap = self.legacy["statusMap"]
        with CSV_PATH.open(newline="", encoding="utf-8") as handle:
            seen = {row["status"].strip() for row in csv.DictReader(handle)}
        unmapped = {s for s in seen if s and s not in statusMap}
        self.assertEqual(unmapped, set(), f"statuses with no import mapping: {unmapped}")


EXAMPLE_APPLICATION = {
    "id": "doordash--software-engineer-devex-android",
    "date": "2026-10-30T00:00:00-04:00",
    "company": "DoorDash",
    "companyId": "doordash",
    "role": "Software Engineer, Developer Experience (Android)",
    "roleType": "Mobile (Android)",
    "channel": "channel:greenhouse",
    "sector": "Tech",
    "status": "status:applied",
    "statusRank": 1,
    "isOpen": True,
    "contactPerson": None,
    "fitRating": 88,
    "source": "https://job-boards.greenhouse.io/doordashusa/jobs/8197854",
    "tags": [
        "relocation:accepted",
        "compensation:above-super-commuter:NYC",
        "compensation:below-nyc-floor",
        "fit:strong",
    ],
    "metadata": {"recruiterBand": "130600-192000", "levelExpected": "staff"},
    "fitEvaluation": {"rubricVersion": 2, "evaluatedAt": "2026-10-30T00:00:00-04:00", "gaps": []},
    "compensation": {"min": 130600, "max": 192000, "currency": "USD", "period": "year", "vsTarget": None},
    "location": {
        "city": "New York",
        "region": "NY",
        "country": "US",
        "workMode": "work-mode:hybrid",
        "relocation": True,
        "superCommuter": True,
    },
    "counters": {"events": 2, "followUpsSent": 0, "interviews": 0, "rejections": 0},
    "timeline": {
        "firstActivityAt": "2026-10-30T00:00:00-04:00",
        "lastActivityAt": "2026-10-30T00:00:00-04:00",
        "daysSinceActivity": 1,
        "nextFollowUpAt": None,
    },
    "notesRendered": "2026-10-30 · event:applied — Applied via Greenhouse.",
    "notesLegacy": "Applied 2026-10-30 via Greenhouse.",
    "contactIds": [],
    "createdAt": "2026-10-30T00:00:00-04:00",
    "updatedAt": "2026-10-30T14:22:00-04:00",
    "schemaVersion": 4,
}


class TestApplicationValidation(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()
        self.validator = jsonschema.Draft202012Validator(
            {"$ref": "#/$defs/application", "$defs": self.schema["$defs"]}
        )

    def errorsFor(self, document):
        return [error.message for error in self.validator.iter_errors(document)]

    def test_worked_example_is_valid(self):
        self.assertEqual(self.errorsFor(EXAMPLE_APPLICATION), [])

    def test_unknown_top_level_key_is_rejected(self):
        bad = dict(EXAMPLE_APPLICATION, surpriseField=1)
        self.assertTrue(self.errorsFor(bad))

    def test_snake_case_key_is_rejected(self):
        bad = dict(EXAMPLE_APPLICATION)
        bad["fit_rating"] = 88
        self.assertTrue(self.errorsFor(bad))

    def test_unregistered_tag_is_rejected(self):
        bad = dict(EXAMPLE_APPLICATION, tags=["compensation:above-super-commuter:BOSTON"])
        self.assertTrue(self.errorsFor(bad))

    def test_fit_rating_must_be_an_integer_in_range(self):
        self.assertTrue(self.errorsFor(dict(EXAMPLE_APPLICATION, fitRating="88")))
        self.assertTrue(self.errorsFor(dict(EXAMPLE_APPLICATION, fitRating=101)))

    def test_status_must_be_namespaced(self):
        self.assertTrue(self.errorsFor(dict(EXAMPLE_APPLICATION, status="applied")))

    def test_metadata_accepts_arbitrary_keys(self):
        document = dict(EXAMPLE_APPLICATION, metadata={"anythingAtAll": [1, 2, 3]})
        self.assertEqual(self.errorsFor(document), [])


class TestEventAndProposalValidation(unittest.TestCase):
    def setUp(self):
        self.schema = loadSchema()
        self.validators = {
            name: jsonschema.Draft202012Validator(
                {"$ref": f"#/$defs/{name}", "$defs": self.schema["$defs"]}
            )
            for name in ("event", "proposal", "syncRequest")
        }

    def errorsFor(self, name, document):
        return [error.message for error in self.validators[name].iter_errors(document)]

    def test_valid_event_passes(self):
        event = {
            "eventId": "018f3c1a-4b2e-7d3f-9a41-2c6b8e5d0f17",
            "ts": "2026-10-30T14:22:00-04:00",
            "date": "2026-10-30T00:00:00-04:00",
            "type": "event:applied",
            "actor": "actor:user",
            "derivedFrom": "manual",
            "confidence": "high",
            "statusTo": "status:applied",
            "note": "Applied via Greenhouse.",
        }
        self.assertEqual(self.errorsFor("event", event), [])

    def test_event_id_must_be_a_uuid_v7(self):
        """A v4 UUID is unsortable and must be rejected, not silently accepted."""
        event = {
            "eventId": "018f3c1a-4b2e-4d3f-9a41-2c6b8e5d0f17",
            "ts": "2026-10-30T14:22:00-04:00",
            "date": "2026-10-30",
            "type": "event:applied",
            "actor": "actor:user",
            "derivedFrom": "manual",
            "confidence": "high",
        }
        self.assertTrue(self.errorsFor("event", event))

    def test_event_id_must_be_canonical_lowercase(self):
        """Firestore document IDs are case-sensitive, so both cases would create
        two records for one event. Only the canonical form is accepted."""
        event = {
            "eventId": "018F3C1A-4B2E-7D3F-9A41-2C6B8E5D0F17",
            "ts": "2026-10-30T14:22:00-04:00",
            "date": "2026-10-30",
            "type": "event:applied",
            "actor": "actor:user",
            "derivedFrom": "manual",
            "confidence": "high",
        }
        self.assertTrue(self.errorsFor("event", event))

    def test_migrated_event_must_declare_low_confidence_provenance(self):
        """A regex-recovered date must be able to admit it is a guess."""
        event = {
            "eventId": "018f3c1a-4b2e-7d3f-9a41-2c6b8e5d0f17",
            "ts": "2026-10-30T14:22:00-04:00",
            "date": "2026-10-22",
            "type": "event:rejection",
            "actor": "actor:import",
            "derivedFrom": "notes-regex",
            "confidence": "low",
        }
        self.assertEqual(self.errorsFor("event", event), [])

    def test_valid_sync_request_passes(self):
        request = {
            "requestId": "018f3c1a-8e77-7b21-a3c9-51d6f0e2b844",
            "createdAt": "2026-10-30T14:22:00-04:00",
            "dueAt": "2026-10-30T16:22:00-04:00",
            "kind": "sync:gmail-collect",
            "state": "sync:pending",
            "applicationIds": ["doordash--software-engineer-devex-android"],
            "attempts": 0,
        }
        self.assertEqual(self.errorsFor("syncRequest", request), [])


if __name__ == "__main__":
    unittest.main()
