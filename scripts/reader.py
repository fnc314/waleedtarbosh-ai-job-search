"""One read interface for the two existing consumers.

`ui/terminal/tracker_tui.py` and `scripts/generate_report.py` were written against
CSV rows: a flat dict of strings. They are the only readers that matter, and they
are not worth rewriting into a new data model. This module therefore projects the
store back onto the legacy row shape, so porting a consumer is a one-line change
and its rendering code is untouched.

Three sources, tried in this order and each reported honestly:

1. **store** — Firestore, authoritative.
2. **cache** — `generated/tracker-cache.json`, a dated snapshot.
3. **csv** — `job_search_tracker.csv`, the pre-migration record.

The caller always learns which one answered and how old it is. A reader that
silently shows a stale snapshot is worse than one that refuses to start, because
the whole point of the event log is that "what happened" is answerable.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import pathlib
import time

import client
import contract

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY_CSV = REPO_ROOT / "job_search_tracker.csv"

# The order the legacy CSV used. Reversed here because the store is authoritative
# on values, but a consumer diffing columns should still line up.
LEGACY_KEYS = [
    "id",
    "date",
    "company",
    "sector",
    "role",
    "role_type",
    "channel",
    "status",
    "contact_person",
    "fit_rating",
    "notes",
    "cv_file",
    "cover_letter_file",
    "source",
]


class ReadError(RuntimeError):
    """No source could produce rows."""


def legacyStatus(value: str | None) -> str:
    """Namespaced status back to the legacy label the consumers render."""
    for legacy, namespaced in contract.legacyStatusMap().items():
        if namespaced == value:
            return legacy
    return (value or "").split(":", 1)[-1]


def _artifactValue(artifacts: list[dict], kind: str) -> str:
    for artifact in artifacts:
        if artifact.get("kind") != kind:
            continue
        return (
            artifact.get("localPath")
            or artifact.get("driveUrl")
            or artifact.get("externalUrl")
            or ""
        )
    return ""


def rowFromStore(application: dict, artifacts: list[dict] | None = None) -> dict:
    """Project one application document onto the legacy row shape."""
    metadata = application.get("metadata") or {}
    artifacts = artifacts or []
    day = str(application.get("date") or "")
    notes = application.get("notesRendered") or application.get("notesLegacy") or ""
    channel = metadata.get("legacyChannel")
    if channel is None:
        channel = (application.get("channel") or "").split(":", 1)[-1]
    return {
        "id": application.get("id", ""),
        "date": day[:10],
        "company": application.get("company") or "",
        "sector": application.get("sector") or "",
        "role": application.get("role") or "",
        "role_type": metadata.get("legacyRoleType") or application.get("roleType") or "",
        "channel": channel or "",
        "status": legacyStatus(application.get("status")),
        "contact_person": application.get("contactPerson") or "",
        "fit_rating": (
            "" if application.get("fitRating") is None else str(application["fitRating"])
        ),
        "notes": notes,
        "cv_file": _artifactValue(artifacts, "artifact:cv"),
        "cover_letter_file": _artifactValue(artifacts, "artifact:cover-letter"),
        "source": application.get("source") or "",
        # Firestore has no row order. `importIndex` preserves the order the CSV
        # had, so a regenerated dashboard keeps the exact ordering it had before
        # the migration instead of shuffling same-date rows.
        "_order": metadata.get("importIndex", 10**9),
    }


def _sortRows(rows: list[dict]) -> list[dict]:
    """Newest first, then the pre-migration row order, then the ID."""
    rows.sort(key=lambda row: (row["date"], -int(row.get("_order") or 10**9), row["id"]), reverse=True)
    return rows


def rowsFromStore(db=None) -> tuple[list[dict], dict]:
    """Read every application plus its artifacts from Firestore."""
    database = db or client.getDb()
    rows = []
    for snapshot in database.collection(client.APPLICATIONS).stream():
        application = contract.fromFirestore(snapshot.to_dict())
        artifacts = [
            contract.fromFirestore(document.to_dict())
            for document in snapshot.reference.collection(client.ARTIFACTS).stream()
        ]
        rows.append(rowFromStore(application, artifacts))
    _sortRows(rows)
    return rows, {
        "source": "store",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "stale": False,
    }


def rowsFromCache(path: pathlib.Path | None = None) -> tuple[list[dict], dict]:
    """Read rows out of the local cache written by `export --json --refresh-cache`."""
    source = pathlib.Path(path or client.CACHE_PATH)
    if not source.exists():
        raise ReadError(f"no cache at {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts") or {}
    rows = [
        rowFromStore(application, artifacts.get(application.get("id"), []))
        for application in payload.get("applications") or []
    ]
    _sortRows(rows)
    generatedAt = payload.get("generatedAt")
    age = None
    if generatedAt:
        try:
            moment = dt.datetime.fromisoformat(str(generatedAt).replace("Z", "+00:00"))
            age = max(
                0.0,
                (dt.datetime.now(dt.timezone.utc) - moment).total_seconds(),
            )
        except ValueError:
            age = None
    return rows, {
        "source": "cache",
        "generatedAt": generatedAt,
        "ageSeconds": age,
        "stale": age is not None and age > 24 * 3600,
    }


def rowsFromCsv(path: pathlib.Path | None = None) -> tuple[list[dict], dict]:
    """Read the pre-migration CSV. Kept so the migration stays reversible."""
    source = pathlib.Path(path or LEGACY_CSV)
    if not source.exists():
        raise ReadError(f"no CSV at {source}")
    with source.open(newline="", encoding="utf-8") as handle:
        rows = [{**{key: "" for key in LEGACY_KEYS}, **row} for row in csv.DictReader(handle)]
    return rows, {
        "source": "csv",
        "generatedAt": dt.datetime.fromtimestamp(
            source.stat().st_mtime, dt.timezone.utc
        ).isoformat(timespec="seconds"),
        "stale": False,
    }


def _preferredSource() -> str:
    return (os.environ.get("TRACKER_SOURCE") or "auto").strip().lower()


def _csvIds() -> set[str] | None:
    """Application ids in the CSV, or None when there is no CSV."""
    if not LEGACY_CSV.exists():
        return None
    try:
        with LEGACY_CSV.open(newline="", encoding="utf-8") as handle:
            return {row["id"] for row in csv.DictReader(handle) if row.get("id")}
    except (OSError, KeyError):
        return None


def rowsFromStoreWithRetry(db=None, attempts: int = 2):
    """Read the store, retrying once.

    A single gRPC blip must not silently downgrade a UI to a stale cache: the
    badge would say `cache (2m old)` and a reader would reasonably assume the
    store had refused. One immediate retry absorbs the transient case, and a
    real outage still falls through with the reason recorded.
    """
    lastError: Exception | None = None
    for attempt in range(attempts):
        try:
            return rowsFromStore(db)
        except Exception as error:  # noqa: BLE001 - retried, then re-raised
            lastError = error
            if attempt + 1 < attempts:
                time.sleep(0.3)
    raise lastError  # type: ignore[misc]


def rows(preferred: str | None = None) -> tuple[list[dict], dict]:
    """Return (rows, meta). `preferred` overrides `TRACKER_SOURCE`.

    In `auto`, the store wins — but only while it still accounts for every row
    the CSV has. During the migration window the CSV is the write target, so a
    `/apply` can add a row the store has never seen; showing the store silently
    would make the new application vanish from every UI. When that happens the
    reader serves the CSV and says so, and the store takes over by itself once
    the writers move or the row is re-imported.
    """
    choice = (preferred or _preferredSource() or "auto").lower()
    attempts: list[str] = []
    if choice in ("store", "auto"):
        try:
            storeRows, storeMeta = rowsFromStoreWithRetry()
            if choice == "store":
                return storeRows, storeMeta
            csvIds = _csvIds()
            if csvIds is None:
                return storeRows, storeMeta
            missing = csvIds - {row["id"] for row in storeRows}
            if not missing:
                return storeRows, storeMeta
            attempts.append(f"store: missing {len(missing)} row(s) present in the CSV")
        except Exception as error:  # noqa: BLE001 - fall through, report why
            attempts.append(f"store: {error}")
    if choice in ("cache", "auto"):
        try:
            cacheRows, cacheMeta = rowsFromCache()
            if choice == "cache":
                return cacheRows, cacheMeta
            csvIds = _csvIds()
            if csvIds is None or not (csvIds - {row["id"] for row in cacheRows}):
                return cacheRows, cacheMeta
            attempts.append("cache: missing rows present in the CSV")
        except Exception as error:  # noqa: BLE001
            attempts.append(f"cache: {error}")
    if choice in ("csv", "auto"):
        try:
            result = rowsFromCsv()
            result[1]["attempts"] = attempts
            return result
        except Exception as error:  # noqa: BLE001
            attempts.append(f"csv: {error}")
    raise ReadError("no readable source; " + "; ".join(attempts))


def badge(meta: dict) -> str:
    """A one-line provenance string for a UI header."""
    source = meta.get("source", "?")
    if source == "store":
        return "store (live)"
    if source == "cache":
        age = meta.get("ageSeconds")
        if age is None:
            return "cache (age unknown)"
        minutes = int(age // 60)
        label = f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"
        return f"cache ({label} old" + (", stale" if meta.get("stale") else "") + ")"
    if source == "csv":
        if meta.get("attempts"):
            return "csv (store behind)"
        return "csv (pre-migration)"
    return str(source)
