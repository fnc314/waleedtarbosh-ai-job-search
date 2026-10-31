"""Deferred collection requests: a local stand-in for Cloud Tasks.

Writes enqueue "collect Gmail in a couple of hours" instead of running a fetch
inline. The reasons are practical: a rejection confirmation does not arrive in
the same second you record the rejection, and an interactive write must not wait
on a network call or on an LLM.

Dedupe is deliberately application-scoped. Five writes about two applications in
ten minutes must produce one request, not five, so a pending request whose
application set intersects the new one is reused and widened. The earliest due
time wins: a later terminal write must never push an already-scheduled run back.
"""

from __future__ import annotations

import datetime as dt

import client
import contract

DEFAULT_KIND = "sync:gmail-collect"
DEFAULT_HOURS = 2
TERMINAL_HOURS = 6


def nowIso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def dueAtIso(hours: float, now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return (moment + dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def buildRequest(
    applicationIds: list[str],
    *,
    hours: float = DEFAULT_HOURS,
    kind: str = DEFAULT_KIND,
    now: dt.datetime | None = None,
) -> dict:
    """A complete, valid sync-request document (not yet written)."""
    if kind not in contract.enums("syncKind"):
        raise contract.ContractError(f"unknown sync kind {kind!r}")
    ids = sorted({applicationId for applicationId in applicationIds if applicationId})
    for applicationId in ids:
        if not contract.validator("documentId").is_valid(applicationId):
            raise contract.ContractError(f"not a legal application id: {applicationId!r}")
    document = {
        "requestId": contract.newUuid(),
        "createdAt": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
        "dueAt": dueAtIso(hours, now),
        "kind": kind,
        "state": "sync:pending",
        "applicationIds": ids,
        "attempts": 0,
    }
    return contract.validate("syncRequest", document)


def pending(db, kind: str = DEFAULT_KIND) -> list[dict]:
    """Every pending request of one kind.

    Filtered by `state` in the query and by `kind` in Python, so no extra
    composite index is required for a collection that holds a handful of rows.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(client.SYNC_REQUESTS).where(
        filter=FieldFilter("state", "==", "sync:pending")
    )
    return [
        contract.fromFirestore(snapshot.to_dict())
        for snapshot in query.stream()
        if snapshot.to_dict().get("kind", kind) == kind
    ]


def enqueue(
    db,
    applicationIds: list[str],
    *,
    hours: float = DEFAULT_HOURS,
    kind: str = DEFAULT_KIND,
    now: dt.datetime | None = None,
    dryRun: bool = False,
) -> dict:
    """Create or widen a pending request. Returns {requestId, created, dueAt}."""
    candidate = buildRequest(applicationIds, hours=hours, kind=kind, now=now)
    wanted = set(candidate["applicationIds"])
    reusable = None
    for existing in pending(db, kind):
        if wanted & set(existing.get("applicationIds") or []):
            reusable = existing
            break

    if reusable is None:
        if not dryRun:
            db.collection(client.SYNC_REQUESTS).document(candidate["requestId"]).set(
                contract.toFirestore(candidate)
            )
        return {
            "requestId": candidate["requestId"],
            "created": True,
            "dueAt": candidate["dueAt"],
            "applicationIds": candidate["applicationIds"],
        }

    merged = sorted(set(reusable.get("applicationIds") or []) | wanted)
    dueAt = min(reusable["dueAt"], candidate["dueAt"])
    if not dryRun:
        db.collection(client.SYNC_REQUESTS).document(reusable["requestId"]).update(
            contract.toFirestore({"applicationIds": merged, "dueAt": dueAt})
        )
    return {
        "requestId": reusable["requestId"],
        "created": False,
        "dueAt": dueAt,
        "applicationIds": merged,
    }


def due(db, now: dt.datetime | None = None) -> list[dict]:
    """Pending requests whose due time has arrived, oldest first."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    moment = now or dt.datetime.now(dt.timezone.utc)
    query = (
        db.collection(client.SYNC_REQUESTS)
        .where(filter=FieldFilter("state", "==", "sync:pending"))
        .where(filter=FieldFilter("dueAt", "<=", moment))
        .order_by("dueAt")
    )
    return [contract.fromFirestore(snapshot.to_dict()) for snapshot in query.stream()]


def claim(db, requestId: str) -> dict | None:
    """Atomically move pending → claimed. None when someone else won the race."""
    from google.cloud import firestore

    reference = db.collection(client.SYNC_REQUESTS).document(requestId)
    transaction = db.transaction()

    @firestore.transactional
    def _claim(transaction):
        snapshot = reference.get(transaction=transaction)
        if not snapshot.exists:
            return None
        document = snapshot.to_dict()
        if document.get("state") != "sync:pending":
            return None
        document["state"] = "sync:claimed"
        document["attempts"] = int(document.get("attempts") or 0) + 1
        transaction.update(reference, contract.toFirestore(document))
        return contract.fromFirestore(document)

    return _claim(transaction)


def complete(db, requestId: str) -> None:
    db.collection(client.SYNC_REQUESTS).document(requestId).update({"state": "sync:done"})


def fail(db, requestId: str, error: str, *, maxAttempts: int = 3) -> dict:
    """Record a failure. Retries twice, then stops rather than looping forever."""
    reference = db.collection(client.SYNC_REQUESTS).document(requestId)
    snapshot = reference.get()
    document = contract.fromFirestore(snapshot.to_dict()) if snapshot.exists else {}
    attempts = int(document.get("attempts") or 0)
    if attempts >= maxAttempts:
        reference.update({"state": "sync:failed", "lastError": error[:1000]})
        return {"state": "sync:failed", "attempts": attempts}
    retryAt = dueAtIso(0.5)
    reference.update(
        {"state": "sync:pending", "dueAt": retryAt, "lastError": error[:1000]}
    )
    return {"state": "sync:pending", "attempts": attempts, "dueAt": retryAt}
