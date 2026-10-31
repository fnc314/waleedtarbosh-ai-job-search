"""Local read cache.

Firestore is authoritative; this file is a convenience for an offline reader
and for a UI that must render before the network answers. It is regenerated at
will and is never a write target: no code may treat it as the record.

`generated/tracker-cache.json` is gitignored, so none of this can leak into a
commit.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

import client
import contract


class CacheError(RuntimeError):
    """The cache cannot be read or written."""


def readAll(db) -> dict:
    """Read every application, event, and artifact into one JSON-safe payload."""
    applications = []
    eventsByApplication = {}
    artifactsByApplication = {}
    for snapshot in db.collection(client.APPLICATIONS).stream():
        application = contract.fromFirestore(snapshot.to_dict())
        applications.append(application)
        reference = db.collection(client.APPLICATIONS).document(application["id"])
        eventsByApplication[application["id"]] = [
            contract.fromFirestore(document.to_dict())
            for document in reference.collection(client.EVENTS).stream()
        ]
        artifactsByApplication[application["id"]] = [
            contract.fromFirestore(document.to_dict())
            for document in reference.collection(client.ARTIFACTS).stream()
        ]
    applications.sort(key=lambda item: (item.get("date") or "", item["id"]), reverse=True)
    # The TUI shows a review badge; the cache must carry it too, or an offline
    # reader would claim there is nothing waiting when there is.
    pendingProposals = [
        {
            "proposalId": document.get("proposalId"),
            "createdAt": document.get("createdAt"),
            "changes": len(document.get("changes") or []),
            "unmatched": len(document.get("unmatched") or []),
        }
        for document in db.collection(client.PENDING_PROPOSALS).stream()
        if document.to_dict().get("state") == "proposal:pending"
    ]
    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "schemaVersion": contract.schemaVersion(),
        "applications": applications,
        "events": eventsByApplication,
        "artifacts": artifactsByApplication,
        "pendingProposals": pendingProposals,
    }


def save(payload: dict, path: pathlib.Path | None = None) -> pathlib.Path:
    """Write atomically: a half-written cache would be worse than none."""
    destination = pathlib.Path(path or client.CACHE_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def load(path: pathlib.Path | None = None) -> dict | None:
    source = pathlib.Path(path or client.CACHE_PATH)
    if not source.exists():
        return None
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CacheError(f"{source} is not valid JSON: {error}") from None


def ageSeconds(payload: dict, now: dt.datetime | None = None) -> float | None:
    generatedAt = payload.get("generatedAt")
    if not generatedAt:
        return None
    moment = dt.datetime.fromisoformat(generatedAt.replace("Z", "+00:00"))
    reference = now or dt.datetime.now(dt.timezone.utc)
    return max(0.0, (reference - moment).total_seconds())


def refresh(db, path: pathlib.Path | None = None) -> tuple[dict, pathlib.Path]:
    payload = readAll(db)
    return payload, save(payload, path)
