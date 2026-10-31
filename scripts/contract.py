"""The contract: one loader, one validator, one set of derived-field rules.

`tracker.schema.json` is the single source of truth for field shapes and enum
values. This module is the only place that reads it, so an enum can never be
half-updated: change the JSON and every validator, renderer, and Sheet header
moves with it.
"""

from __future__ import annotations

import json
import pathlib
import re
import unicodedata
import uuid

import jsonschema

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "tracker.schema.json"

# Crockford base32 is gone; identifiers are UUIDv7. This is the canonical
# textual form, and it pins the two bits that matter: the version nibble is 7
# and the variant nibble is RFC 4122. A v4 UUID is a hard error rather than a
# silently unsortable record.
UUID_V7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

_CACHE: dict | None = None
_VALIDATORS: dict[str, jsonschema.Draft202012Validator] = {}


class ContractError(ValueError):
    """A document does not satisfy tracker.schema.json."""


def load() -> dict:
    """Load and memoize the schema document."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _CACHE


def reload() -> dict:
    """Drop the memoized schema. Used by tests that mutate the file."""
    global _CACHE
    _CACHE = None
    _VALIDATORS.clear()
    return load()


def schemaVersion() -> int:
    return load()["schemaVersion"]


def definitions() -> dict:
    return load()["$defs"]


def definition(name: str) -> dict:
    try:
        return definitions()[name]
    except KeyError:
        raise ContractError(f"unknown schema definition {name!r}") from None


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


def enums(name: str) -> list[str]:
    """Return the values of a named enum, e.g. enums("eventType")."""
    return list(definition(f"{name}Value")["enum"])


def statusValues() -> list[str]:
    return enums("status")


def statusRank() -> dict[str, int]:
    return dict(definition("statusValue")["x-status-rank"])


def terminalStatuses() -> set[str]:
    return set(definition("statusValue")["x-terminal"])


def tags() -> list[str]:
    return enums("tag")


def regions() -> list[str]:
    return enums("region")


def legacyStatusMap() -> dict[str, str]:
    return dict(load()["x-legacy-csv"]["statusMap"])


def legacyFieldMap() -> dict[str, str]:
    return dict(load()["x-legacy-csv"]["fieldMap"])


def normalizeStatus(raw: str) -> str:
    """Map a legacy or human status onto a namespaced status value.

    Accepts 'applied', 'APPLIED', 'no response', and already-namespaced values,
    so callers do not have to know which era a value came from.
    """
    if not raw:
        raise ContractError("status is empty")
    value = raw.strip()
    values = statusValues()
    if value in values:
        return value
    key = value.lower().replace("_", " ").strip()
    mapping = legacyStatusMap()
    if value.lower() in mapping:
        return mapping[value.lower()]
    if key in mapping:
        return mapping[key]
    underscored = key.replace(" ", "_")
    if underscored in mapping:
        return mapping[underscored]
    raise ContractError(
        f"status {raw!r} is not recognized. Known values: {sorted(values)}"
    )


def isTerminal(status: str) -> bool:
    return normalizeStatus(status) in terminalStatuses()


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


def slug(value: str, fallback: str = "") -> str:
    """ASCII kebab slug, used for companyId and document IDs."""
    if not value:
        return fallback
    normalized = unicodedata.normalize("NFKD", value)
    asciiOnly = normalized.encode("ascii", "ignore").decode("ascii")
    return _SLUG_STRIP.sub("-", asciiOnly.lower().strip()).strip("-") or fallback


def documentId(company: str, role: str | None) -> str:
    """Stable application ID: slug(company) + '--' + slug(role), or bare company.

    Byte-identical to the scheme already present in the CSV, which is what lets
    the Phase 3 reconciliation assert an exact match on all 84 rows.
    """
    companySlug = slug(company)
    if not companySlug:
        raise ContractError("cannot derive a document ID without a company name")
    roleSlug = slug(role or "")
    return f"{companySlug}--{roleSlug}" if roleSlug else companySlug


def newUuid() -> str:
    """Generate a local UUIDv7.

    Generated here rather than delegated to a service, because an identifier
    must never require a network round trip: the record has to be writable
    offline, and a failed ID allocation must not become a failed application.

    UUIDv7 carries a 48-bit millisecond timestamp in its high bits, so the
    textual form sorts in creation order. CPython's `uuid.uuid7()` is monotonic
    within a millisecond (it increments a counter rather than redrawing random
    bits), which is what lets `events` be ordered by document ID alone.
    """
    return str(uuid.uuid7())


def isUuid(value: str) -> bool:
    """True for the canonical lowercase UUIDv7 form."""
    return bool(UUID_V7.match(value or ""))


def uuidTimestamp(value: str) -> int:
    """Recover the millisecond timestamp encoded in a UUIDv7's high 48 bits."""
    if not isUuid(value):
        raise ContractError(f"not a UUIDv7: {value!r}")
    return uuid.UUID(value).int >> 80


# --------------------------------------------------------------------------
# Derived fields
# --------------------------------------------------------------------------


def deriveStatusFields(status: str) -> dict:
    """Compute statusRank and isOpen from a status value. Never hand-maintain these."""
    normalized = normalizeStatus(status)
    return {
        "status": normalized,
        "statusRank": statusRank()[normalized],
        "isOpen": normalized not in terminalStatuses(),
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validator(name: str) -> jsonschema.Draft202012Validator:
    """Return a memoized validator for a named definition."""
    if name not in _VALIDATORS:
        schema = load()
        _VALIDATORS[name] = jsonschema.Draft202012Validator(
            {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}
        )
    return _VALIDATORS[name]


def errors(name: str, document: dict) -> list[str]:
    """Return human-readable validation errors for a document. Empty means valid."""
    messages = []
    for error in validator(name).iter_errors(document):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"{location}: {error.message}")
    return messages


def validate(name: str, document: dict) -> dict:
    """Validate or raise. Returns the document for convenient chaining."""
    problems = errors(name, document)
    if problems:
        joined = "\n  - ".join(problems)
        raise ContractError(f"{name} does not satisfy the contract:\n  - {joined}")
    return document


# --------------------------------------------------------------------------
# Round-tripping Firestore values
# --------------------------------------------------------------------------


def toFirestore(document: dict):
    """Convert ISO date strings into Firestore Timestamps, recursively.

    The contract validates the JSON boundary (ISO strings) because that is what
    a user or an agent actually writes. Firestore needs Timestamps for range
    queries and ordering, so conversion happens once, here.
    """

    def convert(value):
        if isinstance(value, dict):
            return {key: convert(child) for key, child in value.items()}
        if isinstance(value, list):
            return [convert(child) for child in value]
        if isinstance(value, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2}))?",
            value,
        ):
            from datetime import datetime

            if "T" not in value:
                # Date-only. A naive UTC midnight renders as the same calendar
                # day everywhere, so a submission date never drifts backwards
                # for a reader west of UTC.
                parsed = datetime.fromisoformat(value)
            else:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed
        return value

    return convert(document)


def fromFirestore(value):
    """Convert Firestore Timestamps back into ISO strings for the JSON boundary."""
    from datetime import datetime

    if isinstance(value, dict):
        return {key: fromFirestore(child) for key, child in value.items()}
    if isinstance(value, list):
        return [fromFirestore(child) for child in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
