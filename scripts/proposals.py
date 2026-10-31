"""The proposal queue: collector output that a human must approve.

The split exists because a cron job has no approver. `collect` may write this
collection and `meta/sync` and nothing else. Only `approve` reads a proposal and
emits events into `applications`, and it does so in one batch, so a partially
approved batch cannot exist.

Matching and classification here are deterministic and conservative. A message
that does not match exactly one application, or does not match a known phrase,
lands in `unmatched` with a reason. Guessing would put invented history in an
append-only log, where it can never be quietly corrected.
"""

from __future__ import annotations

import datetime as dt
import re

import client
import contract
import events as eventsModule
import render

# Ordered by specificity. The first rule that matches wins, so a rejection that
# also says "thank you for applying" is filed as a rejection.
CLASSIFIERS = [
    (
        "event:rejection",
        "status:rejected",
        "high",
        [
            r"\bunfortunately\b",
            r"not moving forward",
            r"regret to inform",
            r"no longer under consideration",
            r"\bnot selected\b",
            r"won't be moving forward",
            r"will not be moving forward",
            r"decided to (?:move forward|proceed) with other",
            r"application status",
            r"position (?:has been|was) filled",
        ],
    ),
    (
        "event:offer",
        "status:offer",
        "high",
        [
            r"\bjob offer\b",
            r"pleased to offer",
            r"offer letter",
            r"we would like to offer you",
        ],
    ),
    (
        "event:interview-scheduled",
        "status:interview",
        "medium",
        [
            r"\binterview\b",
            r"schedule (?:a )?(?:call|chat|conversation)",
            r"phone screen",
            r"hiring manager (?:call|conversation)",
        ],
    ),
    (
        "event:assessment",
        None,
        "medium",
        [
            r"\bcoding (?:challenge|assessment)\b",
            r"\btake[- ]home\b",
            r"\bonline assessment\b",
            r"\btechnical assessment\b",
        ],
    ),
    (
        "event:acknowledged",
        None,
        "low",
        [
            r"thank you for applying",
            r"application (?:has been |was )?received",
            r"we(?:'ve| have) received your application",
            r"thank you for your interest",
        ],
    ),
]

PENDING = "proposal:pending"


def nowIso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _normalized(text: str | None) -> str:
    return (text or "").lower()


def classify(subject: str | None, snippet: str | None) -> tuple[str, str | None, str] | None:
    """Return (eventType, statusTo, confidence) or None."""
    haystack = _normalized(f"{subject or ''}\n{snippet or ''}")
    for eventType, statusTo, confidence, patterns in CLASSIFIERS:
        for pattern in patterns:
            if re.search(pattern, haystack):
                return eventType, statusTo, confidence
    return None


def _companyMatches(text: str, application: dict) -> bool:
    """A conservative company match: the full company name as a word phrase."""
    company = _normalized(application.get("company"))
    if not company:
        return False
    escaped = re.escape(company)
    if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text):
        return True
    slugWords = [word for word in contract.slug(company).split("-") if len(word) >= 5]
    if not slugWords:
        return False
    # Every significant word of a multi-word company must appear; "Grow Therapy"
    # matching on "therapy" alone would be wrong.
    return all(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) for word in slugWords)


def match(message: dict, applications: dict[str, dict]) -> tuple[str | None, str | None]:
    """Return (applicationId, reason). Exactly one of them is set."""
    explicit = message.get("applicationId")
    if explicit:
        if explicit in applications:
            return explicit, None
        return None, f"explicit applicationId {explicit!r} does not exist"
    haystack = _normalized(f"{message.get('subject') or ''}\n{message.get('sender') or ''}\n{message.get('snippet') or ''}")
    matches = [applicationId for applicationId, application in applications.items() if _companyMatches(haystack, application)]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "no application matched"
    return None, f"ambiguous: matched {len(matches)} applications ({', '.join(sorted(matches)[:4])})"


def buildProposal(
    messages: list[dict],
    applications: dict[str, dict],
    *,
    trigger: str = "trigger:manual",
    lookbackFrom: str | None = None,
    query: str | None = None,
    now: str | None = None,
) -> dict:
    """Turn fetched messages into one reviewable proposal. Writes nothing."""
    timestamp = now or nowIso()
    changes = []
    unmatched = []
    seen = set()
    for message in messages:
        if not isinstance(message, dict):
            raise contract.ContractError("each message must be an object")
        messageId = str(message.get("messageId") or "").strip()
        if not messageId:
            raise contract.ContractError("each message needs a messageId")
        if messageId in seen:
            continue
        seen.add(messageId)

        applicationId, reason = match(message, applications)
        classification = classify(message.get("subject"), message.get("snippet"))
        if applicationId is None or classification is None:
            unmatched.append(
                {
                    "sender": message.get("sender"),
                    "subject": message.get("subject"),
                    "receivedAt": message.get("receivedAt"),
                    "reason": reason or "no classification pattern matched",
                }
            )
            continue

        eventType, statusTo, confidence = classification
        if message.get("applicationId"):
            confidence = "high"
        current = applications[applicationId].get("status")
        changes.append(
            {
                "applicationId": applicationId,
                "eventType": eventType,
                "statusFrom": current,
                "statusTo": statusTo,
                "confidence": confidence,
                "sourceEmail": {
                    "messageId": messageId,
                    "subject": message.get("subject"),
                    "sender": message.get("sender"),
                    "receivedAt": message.get("receivedAt"),
                },
            }
        )

    scopeIds = sorted({change["applicationId"] for change in changes})
    proposal = {
        "proposalId": contract.newUuid(),
        "createdAt": timestamp,
        "dueAt": (dt.datetime.fromisoformat(timestamp) + dt.timedelta(days=1)).isoformat(timespec="seconds"),
        "collectedAt": timestamp,
        "kind": "proposal:gmail-sync",
        "state": PENDING,
        "trigger": trigger,
        "scope": {
            "applicationIds": scopeIds,
            "lookbackFrom": lookbackFrom,
            "query": query,
        },
        "changes": changes,
        "unmatched": unmatched,
    }
    return contract.validate("proposal", proposal)


def stage(db, proposal: dict, *, dryRun: bool = False) -> dict:
    if not dryRun:
        db.collection(client.PENDING_PROPOSALS).document(proposal["proposalId"]).set(
            contract.toFirestore(proposal)
        )
    return {
        "proposalId": proposal["proposalId"],
        "changes": len(proposal["changes"]),
        "unmatched": len(proposal["unmatched"]),
        "state": proposal["state"],
        "applications": proposal["scope"]["applicationIds"],
    }


def listing(db, state: str | None = None) -> list[dict]:
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = db.collection(client.PENDING_PROPOSALS)
    if state:
        query = query.where(filter=FieldFilter("state", "==", state))
    documents = [contract.fromFirestore(snapshot.to_dict()) for snapshot in query.stream()]
    documents.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return documents


def load(db, proposalId: str) -> dict:
    snapshot = db.collection(client.PENDING_PROPOSALS).document(proposalId).get()
    if not snapshot.exists:
        raise contract.ContractError(f"no such proposal: {proposalId}")
    return contract.fromFirestore(snapshot.to_dict())


def approve(db, proposalId: str, *, reviewedBy: str = "actor:user", note: str | None = None,
            only: set[int] | None = None) -> dict:
    """Apply the selected changes, one event per change, in bounded batches.

    A change whose target status equals the application's current status is
    skipped, so approving a batch staged before an intervening write cannot
    duplicate a transition. Skips are reported, never silent. `only` holds
    1-based change indexes: review is allowed to be selective, and a change the
    reviewer left out is reported as skipped rather than silently applied.
    """
    proposal = load(db, proposalId)
    if proposal["state"] != PENDING:
        raise contract.ContractError(
            f"proposal {proposalId} is {proposal['state']}, not {PENDING}"
        )

    applications: dict[str, dict] = {}
    storedEvents: dict[str, list[dict]] = {}
    prepared: list[tuple[str, dict]] = []
    skipped = []

    for index, change in enumerate(proposal["changes"], start=1):
        applicationId = change["applicationId"]
        if only is not None and index not in only:
            skipped.append({"index": index, "applicationId": applicationId, "reason": "not selected"})
            continue
        if applicationId not in applications:
            snapshot = db.collection(client.APPLICATIONS).document(applicationId).get()
            if not snapshot.exists:
                skipped.append({"applicationId": applicationId, "reason": "application no longer exists"})
                continue
            applications[applicationId] = contract.fromFirestore(snapshot.to_dict())
            storedEvents[applicationId] = eventsModule.readEvents(db, applicationId)

        application = applications[applicationId]
        current = application.get("status")
        if change.get("statusTo") and contract.normalizeStatus(change["statusTo"]) == contract.normalizeStatus(current):
            skipped.append(
                {
                    "applicationId": applicationId,
                    "reason": f"already {current}; {change['eventType']} would duplicate it",
                }
            )
            continue

        event = eventsModule.buildEvent(
            change["eventType"],
            date=(change.get("sourceEmail") or {}).get("receivedAt"),
            note=note,
            actor="actor:gmail-sync",
            derivedFrom="gmail",
            confidence=change.get("confidence") or "medium",
            statusFrom=current,
            statusTo=change.get("statusTo"),
            emailMessageId=(change.get("sourceEmail") or {}).get("messageId"),
        )
        prepared.append((applicationId, event))
        # Keep the in-memory view current so two changes for one application in
        # the same proposal see each other's result.
        if event.get("statusTo"):
            applications[applicationId] = {**application, "status": event["statusTo"]}

    batch = db.batch()
    operations = 0
    written = []
    for applicationId, event in prepared:
        reference = db.collection(client.APPLICATIONS).document(applicationId)
        storedEvents[applicationId].append(event)
        projected = render.project(applications[applicationId], storedEvents[applicationId])
        projected["updatedAt"] = nowIso()
        batch.set(reference, contract.toFirestore(projected), merge=True)
        batch.set(
            reference.collection(client.EVENTS).document(event["eventId"]),
            contract.toFirestore(event),
        )
        operations += 2
        written.append(
            {"applicationId": applicationId, "eventId": event["eventId"], "type": event["type"]}
        )
        if operations >= 400:
            batch.commit()
            batch = db.batch()
            operations = 0

    resolved = {
        "state": "proposal:approved",
        "reviewedAt": nowIso(),
        "reviewedBy": reviewedBy,
        "resolution": note or f"approved {len(written)} change(s)",
    }
    batch.update(
        db.collection(client.PENDING_PROPOSALS).document(proposalId),
        contract.toFirestore(resolved),
    )
    batch.commit()
    return {"proposalId": proposalId, "approved": written, "skipped": skipped, **resolved}


def reject(db, proposalId: str, *, reviewedBy: str = "actor:user", reason: str | None = None) -> dict:
    proposal = load(db, proposalId)
    if proposal["state"] != PENDING:
        raise contract.ContractError(
            f"proposal {proposalId} is {proposal['state']}, not {PENDING}"
        )
    resolved = {
        "state": "proposal:rejected",
        "reviewedAt": nowIso(),
        "reviewedBy": reviewedBy,
        "resolution": reason or "rejected without changes",
    }
    db.collection(client.PENDING_PROPOSALS).document(proposalId).update(
        contract.toFirestore(resolved)
    )
    return {"proposalId": proposalId, **resolved}


def expire(db, now: str | None = None) -> list[str]:
    """Mark overdue pending proposals expired. Returns the ids changed."""
    moment = now or nowIso()
    expired = []
    for proposal in listing(db, PENDING):
        if (proposal.get("dueAt") or "") <= moment:
            db.collection(client.PENDING_PROPOSALS).document(proposal["proposalId"]).update(
                {"state": "proposal:expired", "reviewedAt": moment, "resolution": "expired un-reviewed"}
            )
            expired.append(proposal["proposalId"])
    return expired
