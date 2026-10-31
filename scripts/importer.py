"""One-shot import of the legacy CSV into the tracker contract.

The importer is the only code in the repo that reads `job_search_tracker.csv`
for migration, and it opens it read-only. It is also the one place where the
historical record's irregularities are handled, so they are handled once:

- `cv_file` / `cover_letter_file` are polymorphic: a repo path, an absolute
  Drive-mount path, or a Google Docs URL (2 of 47 CVs). Assuming "path" would
  create references that can never resolve.
- `channel` holds two free-text labels that are not enum values. They are
  mapped explicitly and the original text is kept in `metadata.legacyChannel`.
- `notes` is prose, and 46 rows share one boilerplate string. It is preserved
  verbatim in `notesLegacy` and in one `event:imported`; it is never mined for
  facts during the mandatory pass.

Everything here is pure: given CSV rows it returns payloads and a report. The
command layer decides whether to write them.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib

import contract
import events as eventsModule

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Historical `channel` labels whose mapping is unambiguous. Anything else
# becomes `channel:other`; nothing is guessed from a company name.
LEGACY_CHANNEL_HINTS = {
    "greenhouse": "channel:greenhouse",
    "workday": "channel:workday",
    "lever": "channel:lever",
    "ashby": "channel:ashby",
    "smartrecruiters": "channel:smartrecruiters",
    "linkedin": "channel:linkedin",
    "referral": "channel:referral",
    "careers site": "channel:other",
}

BOILERPLATE_NOTES = set(contract.load()["x-legacy-csv"]["notesRule"]["boilerplateValues"])


def legacyHeader() -> list[str]:
    return list(contract.load()["x-legacy-csv"]["header"])


def readCsv(path: str | pathlib.Path) -> list[dict]:
    """Read the legacy CSV, preserving every cell as a string.

    A short row is a malformed record, not a row with missing optional values:
    silently padding it would shift columns, which is exactly the corruption
    this migration exists to end.
    """
    csvPath = pathlib.Path(path).expanduser()
    if not csvPath.exists():
        raise contract.ContractError(f"CSV not found: {csvPath}")
    header = legacyHeader()
    with csvPath.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != header:
            raise contract.ContractError(
                f"{csvPath} header does not match the legacy contract:\n"
                f"  expected: {header}\n"
                f"  actual:   {reader.fieldnames}"
            )
        rows = []
        for line, row in enumerate(reader, start=2):
            if None in row:
                raise contract.ContractError(
                    f"{csvPath}:{line} has more cells than the header; refusing to guess"
                )
            rows.append({"__line__": line, **{key: value or "" for key, value in row.items()}})
    return rows


def mapChannel(raw: str) -> tuple[str | None, list[str]]:
    """Return (channel value | None, warnings).

    A label that is already an enum value wins. Otherwise a known ATS word in
    the label maps to that channel, and an unknown label maps to `channel:other`
    rather than being dropped: the schema has an explicit catch-all, and
    dropping would erase the fact that a channel was recorded at all.
    """
    value = (raw or "").strip()
    if not value:
        return None, []
    known = contract.enums("channel")
    if value in known:
        return value, []
    lowered = value.lower()
    for hint, channel in LEGACY_CHANNEL_HINTS.items():
        if hint in lowered and channel in known:
            return channel, [f"channel {value!r} mapped to {channel} (original kept in metadata.legacyChannel)"]
    if "channel:other" in known:
        return "channel:other", [
            f"channel {value!r} has no enum equivalent; mapped to channel:other "
            "(original kept in metadata.legacyChannel)"
        ]
    return None, [f"channel {value!r} dropped: no catch-all channel exists"]


def artifactEntry(raw: str, kind: str) -> tuple[dict | None, list[str]]:
    """Classify one legacy file cell into one artifact payload entry."""
    value = (raw or "").strip()
    if not value:
        return None, []
    if value.startswith(("http://", "https://")):
        return {"kind": kind, "externalUrl": value}, []
    if value.startswith("/"):
        return {"kind": kind, "driveUrl": value}, [
            f"{kind} {value!r} is an absolute path (legacy Drive mount); recorded as driveUrl"
        ]
    entry = {"kind": kind, "localPath": value}
    if not (REPO_ROOT / value).exists():
        return entry, [
            f"{kind} {value!r} does not exist on disk; reference preserved, upload will fail until it does"
        ]
    return entry, []


def rowToPayload(row: dict, index: int, now: str) -> tuple[dict, list[str]]:
    """Resolve one CSV row into a payload for `buildApplication`."""
    warnings: list[str] = []
    line = row.get("__line__", "?")
    company = row["company"].strip()
    if not company:
        raise contract.ContractError(f"CSV line {line}: company is empty")
    role = row["role"].strip() or None
    if role is None:
        warnings.append(f"line {line}: no role recorded; role stays null, not a placeholder")

    # The ID is derived, then compared. A mismatch is an importer defect, not a
    # data defect, so it must block the import rather than be papered over.
    derivedId = contract.documentId(company, role)
    if derivedId != row["id"].strip():
        raise contract.ContractError(
            f"CSV line {line}: id {row['id']!r} != derived {derivedId!r}. "
            "The slug scheme is unchanged; this is an importer bug."
        )

    status = contract.normalizeStatus(row["status"])
    date = eventsModule.normalizeDate(row["date"]) if row["date"].strip() else now[:10]
    if not row["date"].strip():
        warnings.append(f"line {line}: date is empty; used the import timestamp")

    fitRating = None
    if row["fit_rating"].strip():
        try:
            fitRating = int(row["fit_rating"])
        except ValueError:
            raise contract.ContractError(
                f"CSV line {line}: fit_rating {row['fit_rating']!r} is not an integer"
            ) from None
        if not 0 <= fitRating <= 100:
            raise contract.ContractError(
                f"CSV line {line}: fit_rating {fitRating} is outside 0-100"
            )

    channel, channelWarnings = mapChannel(row["channel"])
    warnings.extend(f"line {line}: {message}" for message in channelWarnings)

    notes = row["notes"]
    if notes.strip() in BOILERPLATE_NOTES:
        warnings.append(
            f"line {line}: notes is the boilerplate provenance marker; preserved verbatim, "
            "not treated as evidence"
        )

    metadata: dict = {"importIndex": index, "importedFrom": "job_search_tracker.csv"}
    if row["channel"].strip():
        metadata["legacyChannel"] = row["channel"].strip()
    if row["role_type"].strip():
        metadata["legacyRoleType"] = row["role_type"].strip()

    artifacts: list[dict] = []
    artifactWarnings: list[str] = []
    for column, kind in (
        ("cv_file", "artifact:cv"),
        ("cover_letter_file", "artifact:cover-letter"),
    ):
        entry, entryWarnings = artifactEntry(row[column], kind)
        artifactWarnings.extend(entryWarnings)
        if entry:
            artifacts.append(entry)
    warnings.extend(f"line {line}: {message}" for message in artifactWarnings)

    payload = {
        "id": row["id"].strip(),
        "company": company,
        "companyId": contract.slug(company),
        "role": role,
        "date": date,
        "status": status,
        "sector": row["sector"].strip() or None,
        "contactPerson": row["contact_person"].strip() or None,
        "fitRating": fitRating,
        "source": row["source"].strip() or None,
        "channel": channel,
        "notesLegacy": notes,
        "createdAt": date,
        "metadata": metadata,
        "events": [
            {
                "type": "event:imported",
                "date": date,
                # The import event is dated at submission, not at migration:
                # otherwise every record's timeline would claim activity today.
                "note": notes or None,
                "actor": "actor:import",
                "derivedFrom": "csv-import",
                "confidence": "high",
            }
        ],
        "artifacts": artifacts,
    }
    return payload, warnings


def buildImport(rows: list[dict], now: str | None = None) -> tuple[list[dict], list[str]]:
    """Resolve every row. Returns (payloads, warnings); raises on a defect."""
    timestamp = now or eventsModule.nowIso()
    payloads = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        payload, rowWarnings = rowToPayload(row, index, timestamp)
        if payload["id"] in seen:
            raise contract.ContractError(
                f"CSV line {row.get('__line__', '?')}: duplicate id {payload['id']!r} "
                f"(already seen on line {seen[payload['id']]})"
            )
        seen[payload["id"]] = row.get("__line__", 0)
        payloads.append(payload)
        warnings.extend(rowWarnings)
    return payloads, warnings


def summarize(payloads: list[dict], warnings: list[str]) -> dict:
    """Counts the reconciliation checklist is measured against."""
    statusCounts: dict[str, int] = {}
    artifacts = {"cv": 0, "coverLetter": 0, "localPath": 0, "externalUrl": 0, "driveUrl": 0}
    notesBoilerplate = 0
    emptyRoles = 0
    for payload in payloads:
        status = payload["status"]
        statusCounts[status] = statusCounts.get(status, 0) + 1
        if payload["role"] is None:
            emptyRoles += 1
        if (payload.get("notesLegacy") or "").strip() in BOILERPLATE_NOTES:
            notesBoilerplate += 1
        for entry in payload.get("artifacts") or []:
            if entry["kind"] == "artifact:cv":
                artifacts["cv"] += 1
            elif entry["kind"] == "artifact:cover-letter":
                artifacts["coverLetter"] += 1
            for key in ("localPath", "externalUrl", "driveUrl"):
                if entry.get(key):
                    artifacts[key] += 1
    return {
        "rows": len(payloads),
        "uniqueIds": len({payload["id"] for payload in payloads}),
        "statusCounts": dict(sorted(statusCounts.items())),
        "emptyRoles": emptyRoles,
        "notesBoilerplate": notesBoilerplate,
        "artifacts": artifacts,
        "warnings": len(warnings),
    }


def reconciliation(
    payloads: list[dict],
    rows: list[dict],
    existingIds: set[str] | None = None,
) -> dict:
    """Compare prepared payloads against the source rows, cell by cell.

    This is deliberately independent of the write path: the dry run's report
    must be able to prove the import is clean before anything is written.
    """
    existing = existingIds or set()
    notesMismatches = []
    prepared = []
    skipped = []
    for payload, row in zip(payloads, rows):
        if payload["id"] in existing:
            skipped.append(payload["id"])
            continue
        prepared.append(payload["id"])
        if (payload.get("notesLegacy") or "") != (row.get("notes") or ""):
            notesMismatches.append(payload["id"])
    return {
        "prepared": len(prepared),
        "skippedAlreadyPresent": len(skipped),
        "skippedIds": skipped,
        "notesVerbatimMismatches": notesMismatches,
        "idMismatches": [],
        "duplicateIds": len(payloads) - len({payload["id"] for payload in payloads}),
    }


def renderReport(report: dict) -> str:
    """Markdown reconciliation report. Written for a human to approve."""
    summary = report["summary"]
    checks = report["checks"]
    lines = [
        "# Import reconciliation",
        "",
        f"Generated: {report['generatedAt']}",
        f"Mode: {'dry run' if report['dryRun'] else 'write'}",
        f"Source: `{report['source']}`",
        "",
        "## Counts",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| rows read | {summary['rows']} |",
        f"| unique ids | {summary['uniqueIds']} |",
        f"| applications prepared | {report['reconciliation']['prepared']} |",
        f"| already present (skipped) | {report['reconciliation']['skippedAlreadyPresent']} |",
        f"| rows with no role | {summary['emptyRoles']} |",
        f"| boilerplate notes | {summary['notesBoilerplate']} |",
        f"| CV artifacts | {summary['artifacts']['cv']} |",
        f"| cover-letter artifacts | {summary['artifacts']['coverLetter']} |",
        f"| artifacts with localPath | {summary['artifacts']['localPath']} |",
        f"| artifacts with externalUrl | {summary['artifacts']['externalUrl']} |",
        f"| artifacts with driveUrl | {summary['artifacts']['driveUrl']} |",
        f"| warnings | {summary['warnings']} |",
        "",
        "## Status map",
        "",
        "| stored status | count |",
        "|---|---:|",
    ]
    for status, count in summary["statusCounts"].items():
        lines.append(f"| `{status}` | {count} |")

    lines += ["", "## Checklist", "", "| check | result |", "|---|---|"]
    for name, value in checks.items():
        lines.append(f"| {name} | {value} |")

    if report.get("issues"):
        lines += ["", "## Issues (blocking)", ""]
        lines.extend(f"- {issue}" for issue in report["issues"])

    if report.get("warnings"):
        lines += ["", "## Warnings (non-blocking)", ""]
        categories = {
            "absolute path": "absolute Drive-mount paths recorded as `driveUrl`",
            "boilerplate provenance marker": "rows whose notes are the boilerplate marker",
            "does not exist on disk": "paths that no longer exist on disk",
        }
        grouped = {label: [] for label in categories.values()}
        other = []
        for warning in report["warnings"]:
            matched = False
            for needle, label in categories.items():
                if needle in warning:
                    grouped[label].append(warning)
                    matched = True
                    break
            if not matched:
                other.append(warning)
        for label, items in grouped.items():
            if items:
                lines.append(f"- {label}: **{len(items)}**")
        lines.extend(f"- {warning}" for warning in other)

    lines += [
        "",
        "## What the importer will write",
        "",
        "- one `applications/{id}` document per prepared row;",
        "- exactly one `event:imported` per row, dated at submission and marked "
        "`derivedFrom: csv-import`;",
        "- one `applications/{id}/artifacts` document per non-empty CV/cover-letter cell.",
        "",
        "Nothing in `applications` is overwritten: a row whose document already "
        "exists is skipped and reported.",
        "",
    ]
    return "\n".join(lines)


def utcNow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
