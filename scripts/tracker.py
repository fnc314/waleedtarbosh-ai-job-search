#!/usr/bin/env python3
"""The single write path for the job search tracker.

Every mutation of the record goes through this command. TUI, dashboard, reports,
and the Gmail collector all call into the same modules, so there is exactly one
place where validation, derived fields, and event emission can be wrong.

Read-only subcommands work during the migration window. Anything that writes
application data requires TRACKER_ALLOW_WRITES=1, because the CSV stays
authoritative until Phase 10 (see docs/tracker-migration.md).

Usage:
    python3 scripts/tracker.py check
    python3 scripts/tracker.py seed
    python3 scripts/tracker.py list --open
    python3 scripts/tracker.py get <id>
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import client  # noqa: E402
import contract  # noqa: E402
import events as eventsModule  # noqa: E402
import render  # noqa: E402
import artifacts as artifactsModule  # noqa: E402
import cache  # noqa: E402
import importer  # noqa: E402
import proposals as proposalsModule  # noqa: E402
import sheets  # noqa: E402
import tasks  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def emit(payload) -> None:
    """Print JSON to stdout. Diagnostics always go to stderr."""
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")


def warn(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def commandCheck(args) -> int:
    """Verify the schema contract and, unless asked not to, the connection."""
    schema = contract.load()
    summary = {
        "schemaPath": str(client.SCHEMA_PATH),
        "schemaVersion": contract.schemaVersion(),
        "project": client.PROJECT_ID,
        "database": client.DATABASE,
        "bucket": client.ARTIFACT_BUCKET,
        "credential": (
            str(client.resolveCredentialsPath())
            if client.resolveCredentialsPath()
            else "application default credentials"
        ),
        "enums": {
            name: len(contract.enums(name))
            for name in (
                "status",
                "eventType",
                "channel",
                "artifactKind",
                "workMode",
                "actor",
                "proposalState",
                "tag",
            )
        },
        "definitions": sorted(schema["$defs"]),
        "checks": [],
    }

    failures = 0

    # Derived-field rules must agree with the enum they derive from.
    try:
        for status in contract.statusValues():
            derived = contract.deriveStatusFields(status)
            if derived["status"] != status:
                failures += 1
                summary["checks"].append(
                    f"FAIL derived status mismatch for {status!r}"
                )
        summary["checks"].append(
            f"OK derived fields resolve for all {len(contract.statusValues())} statuses"
        )
    except Exception as error:  # noqa: BLE001 - report, never crash the check
        failures += 1
        summary["checks"].append(f"FAIL derived fields: {error}")

    # A document ID must round-trip through the slug scheme.
    try:
        sample = contract.documentId("DoorDash", "Software Engineer, DevEx")
        expected = "doordash--software-engineer-devex"
        if sample != expected:
            failures += 1
            summary["checks"].append(
                f"FAIL documentId derivation: {sample!r} != {expected!r}"
            )
        else:
            summary["checks"].append("OK documentId derivation is stable")
    except Exception as error:  # noqa: BLE001
        failures += 1
        summary["checks"].append(f"FAIL documentId: {error}")

    if not args.offline:
        try:
            db = client.getDb()
            collections = sorted(collection.id for collection in db.collections())
            summary["live"] = {
                "reachable": True,
                "collections": collections,
            }
            registry = db.collection(client.META).document(client.META_SCHEMA).get()
            summary["live"]["schemaRegistrySeeded"] = registry.exists
            summary["checks"].append("OK Firestore reachable")
            if not registry.exists:
                summary["checks"].append(
                    "NOTE meta/schema is not seeded yet - run: tracker.py seed"
                )
        except Exception as error:  # noqa: BLE001
            failures += 1
            summary["live"] = {"reachable": False, "error": str(error)}
            summary["checks"].append(f"FAIL Firestore: {error}")

    summary["ok"] = failures == 0
    emit(summary)
    return EXIT_OK if failures == 0 else EXIT_ERROR


def commandSeed(args) -> int:
    """Write meta/schema so the registry is queryable from the database itself.

    Infrastructure, not application data: this is exempt from the write gate
    because seeding it is how the connection is proven before anything else
    exists.
    """
    schema = contract.load()
    document = {
        "schemaVersion": contract.schemaVersion(),
        "enums": {
            name: contract.enums(name)
            for name in (
                "status",
                "eventType",
                "channel",
                "artifactKind",
                "workMode",
                "actor",
                "proposalState",
                "proposalKind",
                "trigger",
                "syncKind",
                "syncState",
                "confidence",
                "derivedFrom",
                "region",
                "tag",
            )
        },
        "statusRank": contract.statusRank(),
        "terminalStatuses": sorted(contract.terminalStatuses()),
        "naming": schema["x-naming"],
        "legacyCsv": {
            "header": schema["x-legacy-csv"]["header"],
            "statusMap": schema["x-legacy-csv"]["statusMap"],
            "profile": schema["x-legacy-csv"]["profile"],
        },
        "source": "tracker.schema.json",
    }

    db = client.getDb()
    db.collection(client.META).document(client.META_SCHEMA).set(document, merge=True)
    emit(
        {
            "ok": True,
            "wrote": f"{client.META}/{client.META_SCHEMA}",
            "schemaVersion": document["schemaVersion"],
            "enumCount": len(document["enums"]),
        }
    )
    return EXIT_OK


def commandList(args) -> int:
    db = client.getDb()
    query = db.collection(client.APPLICATIONS)
    if args.status:
        query = query.where("status", "==", contract.normalizeStatus(args.status))
    if args.open:
        query = query.where("isOpen", "==", True)
    if args.tag:
        query = query.where("tags", "array_contains", args.tag)
    query = query.order_by("date", direction="DESCENDING")
    if args.limit:
        query = query.limit(args.limit)
    documents = [contract.fromFirestore(doc.to_dict()) for doc in query.stream()]
    emit({"count": len(documents), "documents": documents})
    return EXIT_OK


def commandGet(args) -> int:
    db = client.getDb()
    reference = db.collection(client.APPLICATIONS).document(args.id)
    snapshot = reference.get()
    if not snapshot.exists:
        warn(f"not found: {args.id}")
        emit({"found": False, "id": args.id})
        return EXIT_ERROR
    events = [
        contract.fromFirestore(doc.to_dict())
        for doc in reference.collection(client.EVENTS).order_by("ts").stream()
    ]
    artifactDocuments = [
        contract.fromFirestore(doc.to_dict())
        for doc in reference.collection(client.ARTIFACTS).stream()
    ]
    emit(
        {
            "found": True,
            "application": contract.fromFirestore(snapshot.to_dict()),
            "events": events,
            "artifacts": artifactDocuments,
        }
    )
    return EXIT_OK


def readPayload(path: str) -> dict:
    """Read a JSON payload from a file, or from stdin when path is '-'."""
    if path == "-":
        raw = sys.stdin.read()
        origin = "<stdin>"
    else:
        payloadPath = pathlib.Path(path).expanduser()
        if not payloadPath.exists():
            raise contract.ContractError(f"payload file not found: {payloadPath}")
        raw = payloadPath.read_text(encoding="utf-8")
        origin = str(payloadPath)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise contract.ContractError(f"{origin} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise contract.ContractError(f"{origin} must contain a JSON object")
    return payload


def normalizeChannel(raw: str | None) -> str | None:
    """Accept 'greenhouse', 'LinkedIn', or 'channel:greenhouse'."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    known = contract.enums("channel")
    if value in known:
        return value
    candidate = "channel:" + contract.slug(value)
    if candidate in known:
        return candidate
    raise contract.ContractError(
        f"unknown channel {raw!r}. Known values: {', '.join(known)}"
    )


def normalizeTag(raw: str) -> str:
    value = str(raw).strip()
    known = contract.tags()
    if value in known:
        return value
    raise contract.ContractError(
        f"unknown tag {raw!r}. Adding a tag is a schemaVersion bump, deliberately. "
        f"Known values: {', '.join(known)}"
    )


def flattenSource(value) -> tuple[str | None, dict]:
    """`source` is a string in the document; a structured form is kept in metadata.

    The payload accepts {url, portal, reqId} because that is what a posting
    actually is, but the record's `source` field stays a plain string so it
    remains sortable, searchable, and CSV-round-trippable.
    """
    if value is None:
        return None, {}
    if isinstance(value, str):
        return (value.strip() or None), {}
    if not isinstance(value, dict):
        raise contract.ContractError("source must be a string or an object")
    unknown = set(value) - {"url", "portal", "reqId", "note"}
    if unknown:
        raise contract.ContractError(f"source has unknown keys: {sorted(unknown)}")
    rendered = value.get("url") or value.get("portal")
    return (str(rendered).strip() if rendered else None), {"source": value}


def artifactFilename(entry: dict) -> str:
    explicit = entry.get("filename")
    if explicit:
        return str(explicit)
    for key in ("localPath", "externalUrl", "driveUrl"):
        value = entry.get(key)
        if value:
            return pathlib.PurePosixPath(str(value)).name or str(value)
    raise contract.ContractError("artifact needs a filename or a path/URL")


def buildArtifacts(entries, *, source: str, now: str) -> list[dict]:
    """Resolve payload artifacts into contract artifact documents.

    Exactly one of localPath / externalUrl / driveUrl should be set. The legacy
    import proves why: 2 CVs in the historical record are Google Docs URLs, and
    recording those as local paths would create a reference that never resolves.
    """
    produced = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            raise contract.ContractError("each artifact must be an object")
        kind = entry.get("kind")
        if kind is None:
            raise contract.ContractError("artifact is missing `kind`")
        if kind not in contract.enums("artifactKind"):
            raise contract.ContractError(
                f"unknown artifact kind {kind!r}. Known: "
                f"{', '.join(contract.enums('artifactKind'))}"
            )
        filename = artifactFilename(entry)
        locations = [
            key for key in ("localPath", "externalUrl", "driveUrl") if entry.get(key)
        ]
        if len(locations) != 1:
            raise contract.ContractError(
                f"artifact {filename!r} must set exactly one of "
                f"localPath, externalUrl, driveUrl (got {locations})"
            )
        document = {
            "artifactId": contract.newUuid(),
            "kind": kind,
            "filename": filename,
            "contentType": entry.get("contentType"),
            "size": entry.get("size"),
            "sha256": entry.get("sha256"),
            "storage": entry.get("storage"),
            "localPath": entry.get("localPath"),
            "driveUrl": entry.get("driveUrl"),
            "externalUrl": entry.get("externalUrl"),
            "createdAt": now,
            "source": source,
        }
        produced.append(contract.validate("artifact", document))
    return produced


def buildEvents(entries, *, actor: str, derivedFrom: str, confidence: str) -> list[dict]:
    produced = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            raise contract.ContractError("each event must be an object")
        if "type" not in entry:
            raise contract.ContractError("event is missing `type`")
        unknown = set(entry) - {
            "type",
            "date",
            "note",
            "actor",
            "derivedFrom",
            "confidence",
            "statusFrom",
            "statusTo",
            "emailMessageId",
            "metadata",
        }
        if unknown:
            raise contract.ContractError(f"event has unknown keys: {sorted(unknown)}")
        produced.append(
            eventsModule.buildEvent(
                entry["type"],
                date=entry.get("date"),
                note=entry.get("note"),
                actor=entry.get("actor", actor),
                derivedFrom=entry.get("derivedFrom", derivedFrom),
                confidence=entry.get("confidence", confidence),
                statusFrom=entry.get("statusFrom"),
                statusTo=entry.get("statusTo"),
                emailMessageId=entry.get("emailMessageId"),
                metadata=entry.get("metadata"),
            )
        )
    return produced


def buildApplication(payload: dict, *, now: str | None = None) -> tuple[dict, list[dict]]:
    """Resolve a JSON payload into a complete, valid application document.

    Everything derivable is derived. A caller supplies facts; this function
    decides IDs, ranks, openness, and any read-only projections.
    """
    timestamp = now or eventsModule.nowIso()
    company = str(payload.get("company") or "").strip()
    if not company:
        raise contract.ContractError("`company` is required")
    role = payload.get("role")
    role = str(role).strip() if role is not None and str(role).strip() else None

    applicationId = payload.get("id") or contract.documentId(company, role)
    if not contract.validator("documentId").is_valid(applicationId):
        raise contract.ContractError(
            f"id {applicationId!r} is not a legal document ID "
            "(kebab-case, optionally one '--' separator)"
        )

    status = contract.normalizeStatus(payload.get("status") or "status:drafting")
    date = payload.get("date") or timestamp[:10]

    tags = []
    for raw in payload.get("tags") or []:
        tag = normalizeTag(raw)
        if tag not in tags:
            tags.append(tag)

    source, sourceMetadata = flattenSource(payload.get("source"))
    metadata = dict(payload.get("metadata") or {})
    metadata.update(sourceMetadata)

    document = {
        "id": applicationId,
        "date": eventsModule.normalizeDate(str(date)),
        "company": company,
        "companyId": payload.get("companyId") or contract.slug(company),
        "sector": payload.get("sector") or None,
        "role": role,
        "roleType": payload.get("roleType") or None,
        "channel": normalizeChannel(payload.get("channel")),
        "contactPerson": payload.get("contactPerson") or None,
        "fitRating": payload.get("fitRating"),
        "source": source,
        "tags": tags,
        "metadata": metadata,
        "fitEvaluation": payload.get("fitEvaluation"),
        "compensation": payload.get("compensation"),
        "location": payload.get("location"),
        "notesLegacy": payload.get("notesLegacy"),
        "contactIds": list(payload.get("contactIds") or []),
        "createdAt": payload.get("createdAt") or timestamp,
        "updatedAt": timestamp,
        "schemaVersion": contract.schemaVersion(),
    }
    document.update(contract.deriveStatusFields(status))

    # Absent optional objects are omitted rather than stored as nulls: a missing
    # compensation block means "not evaluated", which is different from an
    # evaluated block with unknown numbers.
    for key in ("fitEvaluation", "compensation", "location"):
        if document[key] is None:
            del document[key]

    created = buildEvents(
        payload.get("events") or [],
        actor="actor:user",
        derivedFrom="manual",
        confidence="high",
    )
    document.update(render.project(document, created))
    return contract.validate("application", document), created


def mergeApplication(existing: dict, payload: dict, *, now: str) -> dict:
    """Create-or-merge semantics for `upsert`.

    `metadata` merges key-by-key and `tags` unions, because both are additive
    vocabularies: silently dropping a key because a caller only knew about two of
    five fields is exactly the data loss this migration exists to prevent.
    """
    mergedPayload = dict(payload)
    mergedPayload.setdefault("id", existing.get("id"))
    mergedPayload.setdefault("company", existing.get("company"))
    mergedPayload.setdefault("date", existing.get("date"))
    if not mergedPayload.get("status"):
        mergedPayload["status"] = existing.get("status")

    mergedMetadata = dict(existing.get("metadata") or {})
    mergedMetadata.update(payload.get("metadata") or {})
    mergedPayload["metadata"] = mergedMetadata

    mergedTags = list(existing.get("tags") or [])
    for raw in payload.get("tags") or []:
        tag = normalizeTag(raw)
        if tag not in mergedTags:
            mergedTags.append(tag)
    mergedPayload["tags"] = mergedTags

    for key in ("companyId", "sector", "roleType", "channel", "contactPerson", "source"):
        if mergedPayload.get(key) in (None, "") and existing.get(key) is not None:
            mergedPayload[key] = existing[key]
    if mergedPayload.get("fitRating") is None:
        mergedPayload["fitRating"] = existing.get("fitRating")
    for key in ("fitEvaluation", "compensation", "location"):
        if mergedPayload.get(key) is None and existing.get(key) is not None:
            mergedPayload[key] = existing[key]

    mergedPayload["createdAt"] = existing.get("createdAt") or now
    return buildApplication(mergedPayload, now=now)


def writeApplication(db, document: dict, eventsToWrite: list[dict], artifactsToWrite: list[dict]):
    """Write a document plus its first events/artifacts in one atomic commit.

    A batch is used deliberately: a record that exists without its
    `event:imported`, or an artifact written for a document that failed to save,
    would break the reconciliation invariants the whole migration is measured on.
    """
    batch = db.batch()
    reference = db.collection(client.APPLICATIONS).document(document["id"])
    batch.set(reference, contract.toFirestore(document))
    for event in eventsToWrite:
        batch.set(
            reference.collection(client.EVENTS).document(event["eventId"]),
            contract.toFirestore(event),
        )
    for artifact in artifactsToWrite:
        batch.set(
            reference.collection(client.ARTIFACTS).document(artifact["artifactId"]),
            contract.toFirestore(artifact),
        )
    batch.commit()


def commandAdd(args) -> int:
    payload = readPayload(args.json)
    document, pendingEvents = buildApplication(payload)
    artifacts = buildArtifacts(
        payload.get("artifacts"), source="manual", now=eventsModule.nowIso()
    )

    if args.dry_run:
        emit(
            {
                "dryRun": True,
                "wouldWrite": document["id"],
                "application": document,
                "events": pendingEvents,
                "artifacts": artifacts,
            }
        )
        return EXIT_OK

    db = client.getDb()
    if db.collection(client.APPLICATIONS).document(document["id"]).get().exists:
        raise contract.ContractError(
            f"{document['id']} already exists; use `upsert` to merge or `event` to append"
        )
    client.assertWritesAllowed("a new application")
    writeApplication(db, document, pendingEvents, artifacts)
    task = enqueueCollectionQuietly(db, [document["id"]], hours=tasks.DEFAULT_HOURS)
    emit(
        {
            "ok": True,
            "wrote": document["id"],
            "events": [event["eventId"] for event in pendingEvents],
            "artifacts": [artifact["artifactId"] for artifact in artifacts],
            "syncRequest": task,
        }
    )
    return EXIT_OK


def commandUpsert(args) -> int:
    payload = readPayload(args.json)
    now = eventsModule.nowIso()
    db = client.getDb()
    requestedId = payload.get("id")
    if not requestedId:
        company = str(payload.get("company") or "").strip()
        if not company:
            raise contract.ContractError("`company` (or `id`) is required")
        role = payload.get("role")
        requestedId = contract.documentId(company, role)

    snapshot = db.collection(client.APPLICATIONS).document(requestedId).get()
    if snapshot.exists:
        document, pendingEvents = mergeApplication(
            contract.fromFirestore(snapshot.to_dict()), payload, now=now
        )
        created = False
    else:
        document, pendingEvents = buildApplication(payload, now=now)
        created = True

    artifacts = buildArtifacts(payload.get("artifacts"), source="manual", now=now)

    if args.dry_run:
        emit(
            {
                "dryRun": True,
                "wouldCreate": created,
                "wouldWrite": document["id"],
                "application": document,
                "events": pendingEvents,
                "artifacts": artifacts,
            }
        )
        return EXIT_OK

    client.assertWritesAllowed("an application")
    writeApplication(db, document, pendingEvents, artifacts)
    emit(
        {
            "ok": True,
            "created": created,
            "wrote": document["id"],
            "eventCount": len(pendingEvents),
            "artifactCount": len(artifacts),
        }
    )
    return EXIT_OK


def commandStatus(args) -> int:
    db = client.getDb()
    reference = db.collection(client.APPLICATIONS).document(args.id)
    snapshot = reference.get()
    if not snapshot.exists:
        raise contract.ContractError(f"no such application: {args.id}")

    application = contract.fromFirestore(snapshot.to_dict())
    current = contract.normalizeStatus(application["status"])
    target = contract.normalizeStatus(args.status)
    if target == current and not args.correction:
        raise contract.ContractError(
            f"{args.id} is already {target}. A no-op transition would fabricate history.\n"
            "  If the stored status is wrong, pass --correction to emit "
            "event:status-corrected."
        )

    eventType = (
        "event:status-corrected" if args.correction else eventsModule.statusEventType(target)
    )
    event = eventsModule.buildEvent(
        eventType,
        date=args.date,
        note=args.note,
        actor="actor:user",
        statusFrom=current,
        statusTo=target,
    )

    if args.dry_run:
        emit({"dryRun": True, "event": event, "statusFrom": current, "statusTo": target})
        return EXIT_OK

    client.assertWritesAllowed("a status change")
    stored = eventsModule.readEvents(db, args.id)
    stored.append(event)
    projected = render.project(application, stored)
    projected["updatedAt"] = eventsModule.nowIso()
    projected["schemaVersion"] = contract.schemaVersion()

    batch = db.batch()
    batch.set(reference, contract.toFirestore(projected), merge=True)
    batch.set(
        reference.collection(client.EVENTS).document(event["eventId"]),
        contract.toFirestore(event),
    )
    batch.commit()
    hours = tasks.TERMINAL_HOURS if not projected["isOpen"] else tasks.DEFAULT_HOURS
    task = enqueueCollectionQuietly(db, [args.id], hours=hours)
    emit(
        {
            "ok": True,
            "id": args.id,
            "eventId": event["eventId"],
            "statusFrom": current,
            "statusTo": target,
            "isOpen": projected["isOpen"],
            "syncRequest": task,
        }
    )
    return EXIT_OK


def commandEvent(args) -> int:
    payload = readPayload(args.json) if args.json else {}
    db = client.getDb()
    reference = db.collection(client.APPLICATIONS).document(args.id)
    snapshot = reference.get()
    if not snapshot.exists:
        raise contract.ContractError(f"no such application: {args.id}")
    application = contract.fromFirestore(snapshot.to_dict())

    event = eventsModule.buildEvent(
        args.type,
        date=args.date or payload.get("date"),
        note=args.note if args.note is not None else payload.get("note"),
        actor=payload.get("actor", "actor:user"),
        derivedFrom=payload.get("derivedFrom", "manual"),
        confidence=payload.get("confidence", "high"),
        statusFrom=payload.get("statusFrom"),
        statusTo=payload.get("statusTo"),
        emailMessageId=payload.get("emailMessageId"),
        metadata=payload.get("metadata"),
    )

    if args.dry_run:
        emit({"dryRun": True, "event": event})
        return EXIT_OK

    client.assertWritesAllowed("an event")
    stored = eventsModule.readEvents(db, args.id)
    stored.append(event)
    projected = render.project(application, stored)
    projected["updatedAt"] = eventsModule.nowIso()

    batch = db.batch()
    batch.set(reference, contract.toFirestore(projected), merge=True)
    batch.set(
        reference.collection(client.EVENTS).document(event["eventId"]),
        contract.toFirestore(event),
    )
    batch.commit()
    emit(
        {
            "ok": True,
            "id": args.id,
            "eventId": event["eventId"],
            "type": event["type"],
            "status": projected.get("status"),
        }
    )
    return EXIT_OK


def enqueueCollectionQuietly(db, applicationIds, *, hours: float):
    """A deferred Gmail run must never turn a successful write into a failure."""
    try:
        return tasks.enqueue(db, applicationIds, hours=hours)
    except Exception as error:  # noqa: BLE001 - reported, never raised
        warn(f"warning: could not enqueue a Gmail collection request: {error}")
        return None


def readJsonValue(path: str):
    """Read a JSON file or stdin, allowing an array as well as an object."""
    if path == "-":
        raw = sys.stdin.read()
        origin = "<stdin>"
    else:
        payloadPath = pathlib.Path(path).expanduser()
        if not payloadPath.exists():
            raise contract.ContractError(f"payload file not found: {payloadPath}")
        raw = payloadPath.read_text(encoding="utf-8")
        origin = str(payloadPath)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise contract.ContractError(f"{origin} is not valid JSON: {error}") from None


def requireApplication(db, applicationId: str) -> dict:
    snapshot = db.collection(client.APPLICATIONS).document(applicationId).get()
    if not snapshot.exists:
        raise contract.ContractError(f"no such application: {applicationId}")
    return contract.fromFirestore(snapshot.to_dict())


def commandArtifact(args) -> int:
    db = client.getDb()
    application = requireApplication(db, args.id)
    if args.kind not in contract.enums("artifactKind"):
        raise contract.ContractError(
            f"unknown artifact kind {args.kind!r}. Known: {', '.join(contract.enums('artifactKind'))}"
        )

    described = artifactsModule.describe(args.path)
    storage = None
    if not args.no_upload:
        bucket = client.getBucket()
        storage = artifactsModule.upload(
            bucket, applicationId=args.id, kind=args.kind, path=args.path
        )

    entry = {
        "kind": args.kind,
        "filename": args.filename or described["filename"],
        "contentType": described["contentType"],
        "size": described["size"],
        "sha256": described["sha256"],
        "storage": storage,
        "localPath": str(args.path),
    }
    now = eventsModule.nowIso()
    document = artifactsModule.artifactDocument(entry, source="manual", now=now)
    event = eventsModule.buildEvent(
        "event:artifact-added",
        note=document["filename"],
        actor="actor:user",
        derivedFrom="manual",
        confidence="high",
    )

    if args.dry_run:
        emit(
            {
                "dryRun": True,
                "applicationId": args.id,
                "artifact": document,
                "event": event,
                "wouldUpload": storage is not None,
            }
        )
        return EXIT_OK

    client.assertWritesAllowed("an artifact")
    reference = db.collection(client.APPLICATIONS).document(args.id)
    stored = eventsModule.readEvents(db, args.id)
    stored.append(event)
    projected = render.project(application, stored)
    projected["updatedAt"] = now

    batch = db.batch()
    batch.set(
        reference.collection(client.ARTIFACTS).document(document["artifactId"]),
        contract.toFirestore(document),
    )
    batch.set(reference, contract.toFirestore(projected), merge=True)
    batch.set(
        reference.collection(client.EVENTS).document(event["eventId"]),
        contract.toFirestore(event),
    )
    batch.commit()
    emit(
        {
            "ok": True,
            "applicationId": args.id,
            "artifactId": document["artifactId"],
            "eventId": event["eventId"],
            "storage": storage,
        }
    )
    return EXIT_OK


def commandBulk(args) -> int:
    value = readJsonValue(args.json)
    if isinstance(value, dict):
        entries = value.get("applications")
        if entries is None:
            raise contract.ContractError("bulk payload must be a list or {applications: [...]}")
    else:
        entries = value
    if not isinstance(entries, list):
        raise contract.ContractError("bulk payload must contain a list of applications")

    now = eventsModule.nowIso()
    accepted = []
    failures = []
    for index, payload in enumerate(entries):
        try:
            if not isinstance(payload, dict):
                raise contract.ContractError("payload is not an object")
            document, pendingEvents = buildApplication(payload, now=now)
            artifacts = buildArtifacts(payload.get("artifacts"), source="manual", now=now)
            accepted.append((document, pendingEvents, artifacts))
        except contract.ContractError as error:
            failures.append({"index": index, "company": (payload or {}).get("company"), "error": str(error)})

    if failures and not args.allow_partial:
        emit(
            {
                "ok": False,
                "accepted": len(accepted),
                "failed": failures,
                "note": "nothing was written; fix the rows or pass --allow-partial",
            }
        )
        return EXIT_USAGE

    if args.dry_run:
        emit(
            {
                "dryRun": True,
                "accepted": [(document["id"], len(events), len(artifacts)) for document, events, artifacts in accepted],
                "failed": failures,
            }
        )
        return EXIT_OK

    client.assertWritesAllowed("a bulk import")
    db = client.getDb()
    written = []
    for document, pendingEvents, artifacts in accepted:
        if db.collection(client.APPLICATIONS).document(document["id"]).get().exists:
            failures.append({"id": document["id"], "error": "already exists"})
            continue
        writeApplication(db, document, pendingEvents, artifacts)
        written.append(document["id"])
    task = enqueueCollectionQuietly(db, written, hours=tasks.DEFAULT_HOURS)
    emit(
        {
            "ok": not failures,
            "written": written,
            "failed": failures,
            "syncRequest": task,
        }
    )
    return EXIT_OK if not failures else EXIT_USAGE


def importedApplicationIds(db, applicationIds: set[str]) -> set[str]:
    """Applications that already carry an `event:imported` marker.

    Idempotency is keyed on the event, not on the document merely existing: a
    hand-created application with the same ID must never be mistaken for an
    imported one and silently skipped. Only candidate IDs are probed, and the
    query stays inside one subcollection so no collection-group index is needed.
    """
    imported = set()
    for applicationId in applicationIds:
        query = (
            db.collection(client.APPLICATIONS)
            .document(applicationId)
            .collection(client.EVENTS)
            .where("type", "==", "event:imported")
            .limit(1)
        )
        if any(True for _snapshot in query.stream()):
            imported.add(applicationId)
    return imported


def commandImport(args) -> int:
    rows = importer.readCsv(args.csv)
    payloads, warnings = importer.buildImport(rows)
    summary = importer.summarize(payloads, warnings)
    profile = contract.load()["x-legacy-csv"]["profile"]

    db = client.getDb()
    existingIds = {snapshot.id for snapshot in db.collection(client.APPLICATIONS).stream()}
    alreadyImported = importedApplicationIds(db, {payload["id"] for payload in payloads} & existingIds)
    importable = [payload for payload in payloads if payload["id"] not in alreadyImported]
    skipped = sorted(alreadyImported)

    # Resolve every payload before writing anything: the dry run must exercise
    # exactly the same construction path as the write.
    now = eventsModule.nowIso()
    resolved = []
    for payload in importable:
        document, pendingEvents = buildApplication(payload, now=now)
        artifacts = buildArtifacts(payload.get("artifacts"), source="imported", now=now)
        resolved.append((document, pendingEvents, artifacts))

    reconciliation = importer.reconciliation(payloads, rows, existingIds=alreadyImported)
    checks = {
        f"rows read == {profile['rows']}": "PASS" if summary["rows"] == profile["rows"] else f"FAIL ({summary['rows']})",
        f"unique ids == {profile['distinctIds']}": (
            "PASS" if summary["uniqueIds"] == profile["distinctIds"] else f"FAIL ({summary['uniqueIds']})"
        ),
        "duplicate ids == 0": "PASS" if reconciliation["duplicateIds"] == 0 else f"FAIL ({reconciliation['duplicateIds']})",
        "every id derives exactly": "PASS" if not reconciliation["idMismatches"] else "FAIL",
        "notes preserved verbatim": (
            "PASS" if not reconciliation["notesVerbatimMismatches"] else f"FAIL ({len(reconciliation['notesVerbatimMismatches'])})"
        ),
        f"rows with no role == {profile['rows'] - profile['populated']['role']}": (
            "PASS"
            if summary["emptyRoles"] == profile["rows"] - profile["populated"]["role"]
            else f"FAIL ({summary['emptyRoles']})"
        ),
        f"CV artifacts == {profile['populated']['cv_file']}": (
            "PASS" if summary["artifacts"]["cv"] == profile["populated"]["cv_file"] else f"FAIL ({summary['artifacts']['cv']})"
        ),
        f"cover-letter artifacts == {profile['populated']['cover_letter_file']}": (
            "PASS"
            if summary["artifacts"]["coverLetter"] == profile["populated"]["cover_letter_file"]
            else f"FAIL ({summary['artifacts']['coverLetter']})"
        ),
        "one event:imported per row": (
            "PASS" if all(len(events) == 1 for _document, events, _artifacts in resolved) else "FAIL"
        ),
        f"fitRating within {profile['fitRatingRange'][0]}-{profile['fitRatingRange'][1]}": (
            "PASS"
            if all(
                isinstance(document.get("fitRating"), int)
                and profile["fitRatingRange"][0] <= document["fitRating"] <= profile["fitRatingRange"][1]
                for document, _events, _artifacts in resolved
            )
            else "FAIL"
        ),
    }
    issues = [f"{name}: {value}" for name, value in checks.items() if value.startswith("FAIL")]

    report = {
        "generatedAt": importer.utcNow(),
        "dryRun": bool(args.dry_run),
        "source": str(pathlib.Path(args.csv)),
        "summary": summary,
        "reconciliation": reconciliation,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
        "skippedAlreadyImported": skipped,
        "wouldWrite": {
            "applications": len(resolved),
            "events": sum(len(events) for _document, events, _artifacts in resolved),
            "artifacts": sum(len(artifacts) for _document, _events, artifacts in resolved),
        },
    }

    if args.dry_run:
        if args.report:
            path = pathlib.Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(importer.renderReport(report), encoding="utf-8")
            report["reportPath"] = str(path)
        emit(report)
        return EXIT_OK if not issues else EXIT_USAGE

    client.assertWritesAllowed("the CSV import")
    batch = db.batch()
    operations = 0
    for document, pendingEvents, artifacts in resolved:
        reference = db.collection(client.APPLICATIONS).document(document["id"])
        batch.set(reference, contract.toFirestore(document))
        for event in pendingEvents:
            batch.set(
                reference.collection(client.EVENTS).document(event["eventId"]),
                contract.toFirestore(event),
            )
            operations += 1
        for artifact in artifacts:
            batch.set(
                reference.collection(client.ARTIFACTS).document(artifact["artifactId"]),
                contract.toFirestore(artifact),
            )
            operations += 1
        operations += 1
        if operations >= 400:
            batch.commit()
            batch = db.batch()
            operations = 0
    batch.commit()

    if args.report:
        path = pathlib.Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(importer.renderReport(report), encoding="utf-8")
        report["reportPath"] = str(path)
    emit(report)
    return EXIT_OK if not issues else EXIT_USAGE


def legacyStatus(value: str) -> str:
    for legacy, namespaced in contract.legacyStatusMap().items():
        if namespaced == value:
            return legacy
    return value.split(":", 1)[-1]


def legacyArtifact(artifacts: list[dict], kind: str) -> str:
    for artifact in artifacts:
        if artifact.get("kind") != kind:
            continue
        return (
            artifact.get("localPath")
            or artifact.get("externalUrl")
            or artifact.get("driveUrl")
            or ""
        )
    return ""


def applicationsToCsv(applications: list[dict], artifacts: dict[str, list[dict]]) -> str:
    """Regenerate the legacy CSV shape. This is the rollback path."""
    header = importer.legacyHeader()
    ordered = sorted(
        applications,
        key=lambda item: (
            (item.get("metadata") or {}).get("importIndex", 10**9),
            "" if item.get("date") is None else str(item["date"]),
        ),
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for application in ordered:
        metadata = application.get("metadata") or {}
        channel = metadata.get("legacyChannel")
        if channel is None:
            channel = application.get("channel") or ""
            channel = channel.split(":", 1)[-1] if channel else ""
        writer.writerow(
            [
                application.get("id", ""),
                str(application.get("date") or "")[:10],
                application.get("company", ""),
                application.get("sector") or "",
                application.get("role") or "",
                metadata.get("legacyRoleType") or application.get("roleType") or "",
                channel,
                legacyStatus(application.get("status", "")),
                application.get("contactPerson") or "",
                application.get("fitRating") if application.get("fitRating") is not None else "",
                application.get("notesLegacy") or "",
                legacyArtifact(artifacts.get(application["id"], []), "artifact:cv"),
                legacyArtifact(artifacts.get(application["id"], []), "artifact:cover-letter"),
                application.get("source") or "",
            ]
        )
    return buffer.getvalue()


def commandExport(args) -> int:
    db = client.getDb()
    payload = cache.readAll(db)
    if args.refresh_cache:
        cache.save(payload)
    if args.csv:
        text = applicationsToCsv(payload["applications"], payload["artifacts"])
    elif args.ndjson:
        text = "\n".join(
            json.dumps(application, ensure_ascii=False) for application in payload["applications"]
        ) + "\n"
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        path = pathlib.Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        emit({"ok": True, "wrote": str(path), "applications": len(payload["applications"])})
    else:
        sys.stdout.write(text)
    return EXIT_OK


def commandSheet(args) -> int:
    db = client.getDb()
    tableViews = sheets.tabs(db)
    sheetId = args.sheet_id or client.getSheetId()
    if args.local_only or not sheetId:
        written = sheets.writeLocal(tableViews, pathlib.Path(args.directory) if args.directory else None)
        emit(
            {
                "ok": True,
                "mode": "local",
                "written": [str(path) for path in written],
                "tabs": {name: len(rows) - 1 for name, rows in tableViews.items()},
                "note": "set TRACKER_SHEET_ID or pass --sheet-id to sync a real spreadsheet",
            }
        )
        return EXIT_OK
    result = sheets.sync(db, sheetId, tabsOverride=tableViews)
    emit({"ok": True, "mode": "sheets", **result})
    return EXIT_OK


def commandProposals(args) -> int:
    db = client.getDb()
    if args.proposalsCommand == "list":
        documents = proposalsModule.listing(db, args.state)
        emit({"count": len(documents), "proposals": documents})
        return EXIT_OK
    if args.proposalsCommand == "show":
        emit({"proposal": proposalsModule.load(db, args.id)})
        return EXIT_OK
    if args.proposalsCommand == "approve":
        client.assertWritesAllowed("approved proposal changes")
        emit({"ok": True, **proposalsModule.approve(db, args.id, note=args.note)})
        return EXIT_OK
    if args.proposalsCommand == "reject":
        emit({"ok": True, **proposalsModule.reject(db, args.id, reason=args.reason)})
        return EXIT_OK
    if args.proposalsCommand == "expire":
        expired = proposalsModule.expire(db)
        emit({"ok": True, "expired": expired, "count": len(expired)})
        return EXIT_OK
    raise contract.ContractError(f"unknown proposals command {args.proposalsCommand!r}")


def loadInbox(path: str | None) -> tuple[list[dict], dict]:
    """Load fetched messages. Fetching itself is delegated to Composio.

    A cron job cannot hold a Composio session, and this module must not require
    that credential to be testable, so `collect` consumes a JSON drop. The
    `/gmail-sync` flow (or any future fetcher) writes it; `collect` then does
    matching, classification, and staging deterministically.
    """
    candidate = pathlib.Path(path).expanduser() if path else pathlib.Path(
        str(client.REPO_ROOT / "generated" / "gmail-inbox.json")
    )
    if not candidate.exists():
        return [], {"found": False, "path": str(candidate)}
    value = json.loads(candidate.read_text(encoding="utf-8"))
    if isinstance(value, list):
        return value, {"found": True, "path": str(candidate)}
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        return value["messages"], {"found": True, "path": str(candidate), **{
            key: value[key] for key in ("lookbackFrom", "query") if key in value
        }}
    raise contract.ContractError(f"{candidate} must be a list of messages or {{messages: [...]}}")


def collectOnce(db, *, inboxPath: str | None, trigger: str, dryRun: bool) -> dict:
    messages, source = loadInbox(inboxPath)
    if not source.get("found"):
        return {
            "staged": False,
            "reason": "no inbox payload; fetching is delegated to the Composio-backed /gmail-sync",
            "inbox": source["path"],
        }
    applications = {
        snapshot.id: contract.fromFirestore(snapshot.to_dict())
        for snapshot in db.collection(client.APPLICATIONS).stream()
    }
    if not messages:
        return {"staged": False, "reason": "inbox payload is empty", "messages": 0}
    proposal = proposalsModule.buildProposal(
        messages,
        applications,
        trigger=trigger,
        lookbackFrom=source.get("lookbackFrom"),
        query=source.get("query"),
    )
    if dryRun:
        return {"dryRun": True, "proposal": proposal}
    if not proposal["changes"] and not proposal["unmatched"]:
        return {"staged": False, "reason": "no messages to stage"}
    result = proposalsModule.stage(db, proposal)
    messageIds = [
        change["sourceEmail"]["messageId"]
        for change in proposal["changes"]
        if change.get("sourceEmail")
    ]
    from google.cloud import firestore

    db.collection(client.META).document(client.META_SYNC).set(
        {
            "gmail": {
                "processedMessageIds": firestore.ArrayUnion(messageIds),
                "lastCollectedAt": eventsModule.nowIso(),
                "inboxPath": source["path"],
            }
        },
        merge=True,
    )
    return {"staged": True, **result}


def commandCollect(args) -> int:
    db = client.getDb()
    result = collectOnce(db, inboxPath=args.json, trigger=args.trigger, dryRun=args.dry_run)
    emit({"ok": True, **result})
    return EXIT_OK


def commandSyncDue(args) -> int:
    db = client.getDb()
    requests = tasks.due(db)
    if args.dry_run:
        emit({"dryRun": True, "due": len(requests), "requests": requests})
        return EXIT_OK

    results = []
    for request in requests:
        claimed = tasks.claim(db, request["requestId"])
        if claimed is None:
            results.append({"requestId": request["requestId"], "state": "lost-race"})
            continue
        try:
            outcome = collectOnce(
                db,
                inboxPath=args.json,
                trigger="trigger:cron",
                dryRun=False,
            )
            tasks.complete(db, request["requestId"])
            results.append({"requestId": request["requestId"], "state": "done", "collect": outcome})
        except Exception as error:  # noqa: BLE001 - one bad request must not stop the rest
            results.append(
                {"requestId": request["requestId"], "state": "failed", "error": str(error), **tasks.fail(db, request["requestId"], str(error))}
            )
    expired = proposalsModule.expire(db)
    emit({"ok": True, "processed": len(results), "results": results, "expiredProposals": expired})
    return EXIT_OK


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Single write path for the job search tracker store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The CSV remains authoritative until Phase 10. Writers require\n"
            "TRACKER_ALLOW_WRITES=1; read-only commands always work."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate contract and connection")
    check.add_argument(
        "--offline", action="store_true", help="skip the Firestore reachability probe"
    )
    check.set_defaults(handler=commandCheck)

    seed = subparsers.add_parser("seed", help="write meta/schema from the contract")
    seed.set_defaults(handler=commandSeed)

    listing = subparsers.add_parser("list", help="list applications as JSON")
    listing.add_argument("--status", help="filter by status, legacy or namespaced")
    listing.add_argument("--open", action="store_true", help="only open applications")
    listing.add_argument("--tag", help="filter by an exact tag value")
    listing.add_argument("--limit", type=int, default=0)
    listing.set_defaults(handler=commandList)

    get = subparsers.add_parser("get", help="show one application and its events")
    get.add_argument("id")
    get.set_defaults(handler=commandGet)

    add = subparsers.add_parser("add", help="create an application from a JSON payload")
    add.add_argument("--json", required=True, help="payload file, or - for stdin")
    add.add_argument("--dry-run", action="store_true", help="resolve and validate only")
    add.set_defaults(handler=commandAdd)

    upsert = subparsers.add_parser("upsert", help="create-or-merge from a JSON payload")
    upsert.add_argument("--json", required=True, help="payload file, or - for stdin")
    upsert.add_argument("--dry-run", action="store_true", help="resolve and validate only")
    upsert.set_defaults(handler=commandUpsert)

    status = subparsers.add_parser("status", help="record a funnel status change")
    status.add_argument("id")
    status.add_argument("status", help="target status, legacy or namespaced")
    status.add_argument("--date", help="ISO date the change happened (default: now)")
    status.add_argument("--note", help="what happened")
    status.add_argument(
        "--correction",
        action="store_true",
        help="emit event:status-corrected instead of a progress event",
    )
    status.add_argument("--dry-run", action="store_true")
    status.set_defaults(handler=commandStatus)

    event = subparsers.add_parser("event", help="append a timeline event")
    event.add_argument("id")
    event.add_argument("--type", required=True, help="an event:* value")
    event.add_argument("--date", help="ISO date the event happened (default: now)")
    event.add_argument("--note")
    event.add_argument("--json", help="extra fields: actor, derivedFrom, confidence, ...")
    event.add_argument("--dry-run", action="store_true")
    event.set_defaults(handler=commandEvent)

    artifact = subparsers.add_parser("artifact", help="register and upload a file")
    artifact.add_argument("id")
    artifact.add_argument("--kind", required=True, help="an artifact:* value")
    artifact.add_argument("--path", required=True, help="the local file")
    artifact.add_argument("--filename", help="override the stored filename")
    artifact.add_argument("--no-upload", action="store_true", help="record the path only")
    artifact.add_argument("--dry-run", action="store_true")
    artifact.set_defaults(handler=commandArtifact)

    bulk = subparsers.add_parser("bulk", help="add many applications from one JSON file")
    bulk.add_argument("--json", required=True, help="a JSON array, or {applications: [...]}")
    bulk.add_argument("--allow-partial", action="store_true", help="write the valid rows")
    bulk.add_argument("--dry-run", action="store_true")
    bulk.set_defaults(handler=commandBulk)

    importing = subparsers.add_parser("import", help="one-shot import of the legacy CSV")
    importing.add_argument("--csv", required=True, help="path to job_search_tracker.csv")
    importing.add_argument("--dry-run", action="store_true", help="report only, never write")
    importing.add_argument(
        "--report",
        nargs="?",
        const=str(client.REPO_ROOT / "docs" / "import-reconciliation.md"),
        help="write the markdown reconciliation report",
    )
    importing.set_defaults(handler=commandImport)

    exporting = subparsers.add_parser("export", help="read the record back out")
    exporting.add_argument("--json", action="store_true", help="the full JSON payload (default)")
    exporting.add_argument("--csv", action="store_true", help="the legacy CSV shape (rollback)")
    exporting.add_argument("--ndjson", action="store_true", help="one application per line")
    exporting.add_argument("-o", "--output", help="write to a file instead of stdout")
    exporting.add_argument("--refresh-cache", action="store_true", help="also refresh generated/tracker-cache.json")
    exporting.set_defaults(handler=commandExport)

    sheet = subparsers.add_parser("sheet", help="regenerate the Sheets view")
    sheet.add_argument("--sync", action="store_true", help="accepted for symmetry")
    sheet.add_argument("--sheet-id", help="spreadsheet ID (default: TRACKER_SHEET_ID)")
    sheet.add_argument("--local-only", action="store_true", help="write CSVs under generated/sheets-view")
    sheet.add_argument("--directory", help="where --local-only writes")
    sheet.set_defaults(handler=commandSheet)

    proposals = subparsers.add_parser("proposals", help="review the collector queue")
    proposalSubs = proposals.add_subparsers(dest="proposalsCommand", required=True)
    proposalList = proposalSubs.add_parser("list")
    proposalList.add_argument("--state", help="e.g. proposal:pending")
    proposalList.set_defaults(handler=commandProposals)
    proposalShow = proposalSubs.add_parser("show")
    proposalShow.add_argument("id")
    proposalShow.set_defaults(handler=commandProposals)
    proposalApprove = proposalSubs.add_parser("approve")
    proposalApprove.add_argument("id")
    proposalApprove.add_argument("--note", help="resolution text")
    proposalApprove.set_defaults(handler=commandProposals)
    proposalReject = proposalSubs.add_parser("reject")
    proposalReject.add_argument("id")
    proposalReject.add_argument("--reason", help="why it was rejected")
    proposalReject.set_defaults(handler=commandProposals)
    proposalExpire = proposalSubs.add_parser("expire")
    proposalExpire.set_defaults(handler=commandProposals)

    collect = subparsers.add_parser("collect", help="stage proposals from fetched messages")
    collect.add_argument("--json", help="inbox payload (default: generated/gmail-inbox.json)")
    collect.add_argument(
        "--trigger",
        default="trigger:manual",
        help="trigger:manual | trigger:cron | trigger:application-updated",
    )
    collect.add_argument("--dry-run", action="store_true")
    collect.set_defaults(handler=commandCollect)

    syncDue = subparsers.add_parser("sync-due", help="run due sync-requests")
    syncDue.add_argument("--json", help="inbox payload for every request")
    syncDue.add_argument("--dry-run", action="store_true", help="list what is due")
    syncDue.set_defaults(handler=commandSyncDue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = buildParser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except contract.ContractError as error:
        warn(f"contract error: {error}")
        return EXIT_USAGE
    except client.WriteNotAllowed as error:
        warn(str(error))
        return EXIT_USAGE
    except client.CredentialError as error:
        warn(f"credential error: {error}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
