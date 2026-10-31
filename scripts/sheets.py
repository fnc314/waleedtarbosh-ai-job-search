"""The Google Sheets view.

The Sheet is a projection, never a source. Every sync replaces all three tabs
wholesale: a full replace is the only way to guarantee that a deleted or
corrected record disappears from the view. Hand edits are therefore impossible
to preserve, which is the point.

Row builders are pure so they can be tested offline. Only `sync` talks to the
Sheets API, and it accepts an already-built service, which keeps it testable and
keeps credentials in `client.py`.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib

import client
import contract

APPLICATIONS_TAB = "Applications"
EVENTS_TAB = "Events"
SUMMARY_TAB = "Summary"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

APPLICATION_HEADER = [
    "id",
    "date",
    "company",
    "role",
    "sector",
    "channel",
    "status",
    "fitRating",
    "workMode",
    "compMin",
    "compMax",
    "tags",
    "lastActivityAt",
    "daysSinceActivity",
    "followUpsSent",
    "source",
    "cv",
    "coverLetter",
]

EVENT_HEADER = [
    "applicationId",
    "date",
    "type",
    "statusFrom",
    "statusTo",
    "note",
    "actor",
]


def _day(value) -> str:
    return str(value or "")[:10]


def _artifactLabel(artifacts: list[dict] | None, kind: str) -> str:
    for artifact in artifacts or []:
        if artifact.get("kind") != kind:
            continue
        return (
            artifact.get("localPath")
            or artifact.get("driveUrl")
            or artifact.get("externalUrl")
            or artifact.get("filename")
            or ""
        )
    return ""


def applicationRows(applications: list[dict], artifacts: dict[str, list[dict]] | None = None) -> list[list]:
    """Header plus one row per application, newest first."""
    artifacts = artifacts or {}
    rows = [list(APPLICATION_HEADER)]
    ordered = sorted(
        applications, key=lambda item: (item.get("date") or "", item.get("id") or ""), reverse=True
    )
    for application in ordered:
        timeline = application.get("timeline") or {}
        counters = application.get("counters") or {}
        compensation = application.get("compensation") or {}
        location = application.get("location") or {}
        appArtifacts = artifacts.get(application.get("id"), [])
        rows.append(
            [
                application.get("id", ""),
                _day(application.get("date")),
                application.get("company", ""),
                application.get("role") or "",
                application.get("sector") or "",
                application.get("channel") or "",
                application.get("status") or "",
                application.get("fitRating") if application.get("fitRating") is not None else "",
                location.get("workMode") or "",
                compensation.get("min") if compensation.get("min") is not None else "",
                compensation.get("max") if compensation.get("max") is not None else "",
                ", ".join(application.get("tags") or []),
                _day(timeline.get("lastActivityAt")),
                timeline.get("daysSinceActivity") if timeline.get("daysSinceActivity") is not None else "",
                counters.get("followUpsSent", 0),
                application.get("source") or "",
                _artifactLabel(appArtifacts, "artifact:cv"),
                _artifactLabel(appArtifacts, "artifact:cover-letter"),
            ]
        )
    return rows


def eventRows(eventsByApplication: dict[str, list[dict]]) -> list[list]:
    """Header plus every event, oldest first within an application."""
    rows = [list(EVENT_HEADER)]
    for applicationId in sorted(eventsByApplication):
        for event in sorted(
            eventsByApplication[applicationId], key=lambda item: (item.get("date") or "", item.get("eventId") or "")
        ):
            rows.append(
                [
                    applicationId,
                    _day(event.get("date")),
                    event.get("type", ""),
                    event.get("statusFrom") or "",
                    event.get("statusTo") or "",
                    (event.get("note") or "").replace("\n", " "),
                    event.get("actor", ""),
                ]
            )
    return rows


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def summaryRows(applications: list[dict], eventsByApplication: dict[str, list[dict]]) -> list[list]:
    """A small key/value table: totals, funnel, groupings, and response time."""
    rows = [["metric", "value"]]
    total = len(applications)
    rows.append(["applications", total])

    funnel: dict[str, int] = {}
    for application in applications:
        status = application.get("status") or "unknown"
        funnel[status] = funnel.get(status, 0) + 1
    for status in sorted(funnel, key=lambda item: contract.statusRank().get(item, 99)):
        share = (funnel[status] / total * 100) if total else 0
        rows.append([status, f"{funnel[status]} ({share:.1f}%)"])

    for label, field in (("by sector", "sector"), ("by channel", "channel")):
        groups: dict[str, int] = {}
        for application in applications:
            key = application.get(field) or "(none)"
            groups[key] = groups.get(key, 0) + 1
        for key in sorted(groups):
            rows.append([f"{label}: {key}", groups[key]])

    # Days from submission to the first event that is not the import marker.
    latencies = []
    for application in applications:
        submitted = application.get("date")
        if not submitted:
            continue
        moments = [
            event.get("date")
            for event in eventsByApplication.get(application["id"], [])
            if event.get("type") != "event:imported" and event.get("date")
        ]
        if not moments:
            continue
        first = min(moments)
        try:
            start = dt.date.fromisoformat(str(submitted)[:10])
            end = dt.date.fromisoformat(str(first)[:10])
        except ValueError:
            continue
        latencies.append((end - start).days)
    median = _median([float(value) for value in latencies])
    rows.append(["median days to first response", "n/a" if median is None else median])
    rows.append(["responses measured", len(latencies)])
    return rows


def tabs(db, *, artifacts: dict[str, list[dict]] | None = None) -> dict[str, list[list]]:
    """Read the store and build all three tabs. Pure projection, no writes."""
    applications = []
    eventsByApplication: dict[str, list[dict]] = {}
    artifactMap: dict[str, list[dict]] = dict(artifacts or {})
    for snapshot in db.collection(client.APPLICATIONS).stream():
        application = contract.fromFirestore(snapshot.to_dict())
        applications.append(application)
        reference = db.collection(client.APPLICATIONS).document(application["id"])
        eventsByApplication[application["id"]] = [
            contract.fromFirestore(document.to_dict())
            for document in reference.collection(client.EVENTS).stream()
        ]
        if application["id"] not in artifactMap:
            artifactMap[application["id"]] = [
                contract.fromFirestore(document.to_dict())
                for document in reference.collection(client.ARTIFACTS).stream()
            ]
    return {
        APPLICATIONS_TAB: applicationRows(applications, artifactMap),
        EVENTS_TAB: eventRows(eventsByApplication),
        SUMMARY_TAB: summaryRows(applications, eventsByApplication),
    }


def writeLocal(tableViews: dict[str, list[list]], directory: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Write the view as CSV files, for review before a spreadsheet exists."""
    target = pathlib.Path(directory or (client.REPO_ROOT / "generated" / "sheets-view"))
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rows in tableViews.items():
        path = target / f"{name.lower()}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerows(rows)
        written.append(path)
    return written


def sync(db, sheetId: str, service=None, *, tabsOverride: dict[str, list[list]] | None = None) -> dict:
    """Replace all three tabs. Never appends and never reads cell values back."""
    if service is None:
        from googleapiclient.discovery import build

        service = build("sheets", "v4", credentials=client.getCredentials(), cache_discovery=False)

    tableViews = tabsOverride or tabs(db)

    metadata = service.spreadsheets().get(spreadsheetId=sheetId).execute()
    existing = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
    missing = [name for name in tableViews if name not in existing]
    if missing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheetId,
            body={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]},
        ).execute()

    updates = [
        {
            "range": f"'{name}'!A1",
            "values": rows,
        }
        for name, rows in tableViews.items()
    ]
    # Clear first: a shrinking view must not leave the previous run's tail rows
    # behind, which is exactly what "full replace" means.
    for name in tableViews:
        service.spreadsheets().values().clear(
            spreadsheetId=sheetId, range=f"'{name}'!A1:ZZ100000"
        ).execute()
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheetId,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()
    return {
        "sheetId": sheetId,
        "tabs": {name: len(rows) - 1 for name, rows in tableViews.items()},
        "addedTabs": missing,
    }
