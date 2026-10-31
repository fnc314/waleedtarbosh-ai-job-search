"""Append-only event history.

Notes are discouraged because prose is unqueryable and lossy. The replacement is
one immutable event per thing that happened. Nothing in this module edits or
deletes an event: a correction is a new event of type `event:status-corrected`.

Two rules live here and nowhere else:

1. An event is built and validated as a whole, so a caller cannot write a
   half-formed history entry.
2. Provenance is mandatory. Migrated or inferred history says so, via
   `derivedFrom` and `confidence`, and never presents as recorded fact.
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable

import client
import contract

# Event types that imply a funnel status. An event carries statusFrom/statusTo
# only when it actually moved the funnel; a follow-up does not.
EVENT_IMPLIED_STATUS = {
    "event:applied": "status:applied",
    "event:recruiter-screen": "status:interview",
    "event:interview-scheduled": "status:interview",
    "event:onsite": "status:interview",
    "event:offer": "status:offer",
    "event:offer-accepted": "status:hired",
    "event:offer-declined": "status:offer-declined",
    "event:rejection": "status:rejected",
    "event:no-response": "status:no-response",
    "event:withdrawn": "status:withdrawn",
}

# Event types that must never silently move the funnel: they describe activity
# inside the current status, or describe the record itself.
NON_STATUS_EVENTS = {
    "event:acknowledged",
    "event:interview-completed",
    "event:assessment",
    "event:follow-up-sent",
    "event:document-tailored",
    "event:fit-recalculated",
    "event:artifact-added",
    "event:note-added",
    "event:imported",
    "event:status-corrected",
}


class EventError(ValueError):
    """An event cannot be recorded as requested."""


def impliedStatus(eventType: str) -> str | None:
    """The status an event type implies, or None if it does not move the funnel."""
    return EVENT_IMPLIED_STATUS.get(eventType)


def statusEventType(status: str) -> str:
    """The canonical event type for entering a status."""
    normalized = contract.normalizeStatus(status)
    for eventType, implied in EVENT_IMPLIED_STATUS.items():
        if implied == normalized:
            return eventType
    # status:drafting has no inbound event type of its own; it is the initial
    # state of a record, so entering it is a correction rather than progress.
    return "event:status-corrected"


def nowIso() -> str:
    """Local-time ISO 8601 with an explicit offset. Never a naive timestamp."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalizeDate(value: str | None) -> str:
    """Accept a bare date or a full timestamp and return a legal contract value."""
    if value is None:
        return nowIso()
    text = value.strip()
    if not text:
        raise EventError("date is empty")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise EventError(f"date is not ISO 8601: {value!r}") from None
    if "T" not in text:
        # A bare date is a calendar fact, not an instant. Keep it naive so no
        # reader west of UTC sees the previous day.
        return parsed.date().isoformat()
    return parsed.isoformat()


def buildEvent(
    eventType: str,
    *,
    date: str | None = None,
    note: str | None = None,
    actor: str = "actor:user",
    derivedFrom: str = "manual",
    confidence: str = "high",
    statusFrom: str | None = None,
    statusTo: str | None = None,
    emailMessageId: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Construct and validate one event document.

    `statusTo` defaults to the status implied by the event type, and `statusFrom`
    to nothing: an event describes what it moved *to*, and callers that know the
    prior state pass it. Guessing the prior state here would invent history.
    """
    if eventType not in contract.enums("eventType"):
        raise EventError(
            f"unknown event type {eventType!r}. Known types: "
            f"{', '.join(contract.enums('eventType'))}"
        )
    if actor not in contract.enums("actor"):
        raise EventError(f"unknown actor {actor!r}")
    if derivedFrom not in contract.enums("derivedFrom"):
        raise EventError(f"unknown derivedFrom {derivedFrom!r}")
    if confidence not in contract.enums("confidence"):
        raise EventError(f"unknown confidence {confidence!r}")

    resolvedDate = normalizeDate(date)
    resolvedType = eventType
    if statusTo is None:
        statusTo = impliedStatus(eventType)
    else:
        statusTo = contract.normalizeStatus(statusTo)
    if statusFrom is not None:
        statusFrom = contract.normalizeStatus(statusFrom)

    event = {
        "eventId": contract.newUuid(),
        "ts": nowIso(),
        "date": resolvedDate,
        "type": resolvedType,
        "actor": actor,
        "derivedFrom": derivedFrom,
        "confidence": confidence,
        "statusFrom": statusFrom,
        "statusTo": statusTo,
        "note": note or None,
        "emailMessageId": emailMessageId or None,
        "metadata": metadata or {},
    }
    return contract.validate("event", event)


def appendEvent(db, applicationId: str, event: dict) -> dict:
    """Write one validated event. Callers must have passed the write gate."""
    validated = contract.validate("event", event)
    reference = (
        db.collection(client.APPLICATIONS)
        .document(applicationId)
        .collection(client.EVENTS)
        .document(validated["eventId"])
    )
    reference.set(contract.toFirestore(validated))
    return validated


def readEvents(db, applicationId: str) -> list[dict]:
    """Events in chronological order. UUIDv7 IDs make a single order_by enough."""
    collection = (
        db.collection(client.APPLICATIONS)
        .document(applicationId)
        .collection(client.EVENTS)
    )
    return [contract.fromFirestore(doc.to_dict()) for doc in collection.stream()]


def chronologically(events: Iterable[dict]) -> list[dict]:
    """Sort by the date each event describes, then by UUIDv7 as a stable tiebreak."""

    def key(event):
        date = event.get("date") or ""
        # A bare date sorts before any timestamp on the same day, which is what
        # we want: the day's first recorded fact comes first.
        return (date, event.get("eventId") or "")

    return sorted(events, key=key)


def latestStatus(events: Iterable[dict]) -> str | None:
    """The status implied by the last status-bearing event, if any."""
    for event in reversed(chronologically(events)):
        if event.get("statusTo"):
            return contract.normalizeStatus(event["statusTo"])
    return None
