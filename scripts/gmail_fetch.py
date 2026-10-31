#!/usr/bin/env python3
"""Fetch Gmail into the payload `tracker.py collect` consumes.

This is the deterministic half of `/gmail-sync`, extracted so a scheduler can
run it without an agent. It talks to the same Composio Gmail tools the manual
flow uses, so there is one mailbox access path and one set of credentials.

It writes two files and nothing else:

- `generated/gmail-inbox.json`  — `{lookbackFrom, query, messages[]}`
- `generated/gmail-labels.json` — `{labels: [{id, name, type}]}`

`collect` is a separate step: this script never stages a proposal and never
touches Firestore. Keeping the split means a failed fetch cannot look like a
quiet mailbox.

Scopes, in the order the manual flow documents them:

- Primary        `category:primary`
- Updates        `category:updates`
- JOBS           `label:"JOBS"`
- JOBS children  `label:"JOBS/<sub>"` for every sub-label seen this run

Gmail has no `label:JOBS/*` wildcard, which is why the label list is fetched
first and the sub-labels are expanded here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_INBOX = ROOT / "generated" / "gmail-inbox.json"
DEFAULT_LABELS = ROOT / "generated" / "gmail-labels.json"

JOB_LABEL = "JOBS"
DEFAULT_LOOKBACK_DAYS = 30
# Composio caps one response at roughly 50 full messages, so the fetcher pages
# in batches of this size instead of asking for everything at once.
PAGE_SIZE = 50
DEFAULT_MAX_RESULTS = 200


class FetchError(RuntimeError):
    """The mailbox could not be read, so no payload was written."""


def composioBin() -> str:
    explicit = os.environ.get("COMPOSIO_BIN")
    if explicit:
        return explicit
    found = shutil.which("composio")
    if found:
        return found
    # launchd starts with a minimal PATH; the default install location is not
    # on it.
    fallback = pathlib.Path.home() / ".local" / "bin" / "composio"
    if fallback.exists():
        return str(fallback)
    raise FetchError(
        "composio CLI not found. Set COMPOSIO_BIN or install it on PATH."
    )


def runTool(slug: str, arguments: dict, *, timeout: int = 180) -> dict:
    """Execute one Composio tool and return its `data` object.

    The CLI stores large results in a temp file and returns a pointer instead
    of the payload, so both shapes are handled.
    """
    command = [composioBin(), "execute", slug, "-d", json.dumps(arguments)]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"{slug} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        # The CLI writes tool-level failures to stdout and exits 1 with an
        # empty stderr, so prefer whatever JSON it printed over the bare code.
        detail = completed.stdout.strip() or completed.stderr.strip()
        try:
            printed = json.loads(detail) if detail else None
        except json.JSONDecodeError:
            printed = None
        if isinstance(printed, dict):
            detail = json.dumps(
                printed.get("error") or printed.get("data") or printed
            )
        raise FetchError(f"{slug} exited {completed.returncode}: {detail[:500]}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(
            f"{slug} did not return JSON: {completed.stdout.strip()[:300]}"
        ) from exc
    if envelope.get("error"):
        raise FetchError(f"{slug} failed: {json.dumps(envelope['error'])[:500]}")
    if envelope.get("successful") is False:
        raise FetchError(f"{slug} failed: {json.dumps(envelope)[:500]}")
    if envelope.get("storedInFile"):
        path = envelope.get("outputFilePath")
        if not path or not pathlib.Path(path).exists():
            raise FetchError(f"{slug} reported a file result without a file: {path}")
        envelope = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise FetchError(f"{slug} returned no data object")
    return data


def listLabels() -> list[dict]:
    data = runTool("GMAIL_LIST_LABELS", {"user_id": "me"})
    labels = []
    for entry in data.get("labels") or []:
        if not entry.get("name"):
            continue
        labels.append(
            {
                "id": str(entry.get("id") or ""),
                "name": str(entry["name"]),
                "type": str(entry.get("type") or "user"),
            }
        )
    return labels


def selectLabel(labels: list[dict], requested: str) -> dict:
    """Resolve `--label` exactly, against this run's inventory."""
    wanted = requested.strip().lower()
    for label in labels:
        if label["name"].lower() == wanted or label["id"].lower() == wanted:
            return label
    known = ", ".join(sorted(label["name"] for label in labels if label["type"] == "user"))
    raise FetchError(f'Unknown label "{requested}". This mailbox has: {known or "(none)"}')


def buildQuery(labels: list[dict], label: dict | None, since: str | None) -> str:
    bound = f"after:{since.replace('-', '/')}" if since else f"newer_than:{DEFAULT_LOOKBACK_DAYS}d"
    if label is not None:
        return f'{bound} label:"{label["name"]}"'
    clauses = ["category:primary", "category:updates"]
    clauses.append(f'label:"{JOB_LABEL}"')
    for entry in labels:
        name = entry["name"]
        if name.startswith(f"{JOB_LABEL}/") and name != JOB_LABEL:
            clauses.append(f'label:"{name}"')
    return f"{bound} (" + " OR ".join(clauses) + ")"


def bodyText(message: dict) -> str:
    text = message.get("messageText")
    if isinstance(text, str) and text.strip():
        return text
    preview = message.get("preview")
    if isinstance(preview, dict):
        preview = preview.get("body")
    if isinstance(preview, str):
        return preview
    return ""


def toInboxMessage(message: dict) -> dict:
    subject = str(message.get("subject") or "").strip()
    body = bodyText(message).strip()
    snippet = f"{subject}\n\n{body}"[:800] if body else subject[:800]
    received = str(message.get("messageTimestamp") or "")
    if received.endswith("Z"):
        received = received[:-1] + "+00:00"
    return {
        "messageId": str(message.get("messageId") or message.get("id") or ""),
        "subject": subject,
        "sender": str(message.get("sender") or ""),
        "snippet": snippet,
        "receivedAt": received,
    }


def fetchMessages(
    query: str,
    limit: int,
    *,
    pageSize: int = PAGE_SIZE,
    timeout: int = 600,
) -> list[dict]:
    """Page through the query until `limit` messages or the mailbox ends.

    Composio rejects a single response much above 50 full messages with
    "The tool response payload is too large", so one big request cannot work.
    Paging keeps every request under that cap while still covering the whole
    lookback window.
    """
    messages: list[dict] = []
    token = None
    while len(messages) < limit:
        arguments = {
            "user_id": "me",
            "query": query,
            "max_results": max(1, min(pageSize, limit - len(messages), 50)),
            "verbose": True,
            "include_payload": True,
        }
        if token:
            arguments["page_token"] = token
        data = runTool("GMAIL_FETCH_EMAILS", arguments, timeout=timeout)
        batch = data.get("messages") or []
        messages.extend(batch)
        token = data.get("nextPageToken")
        if not batch or not token:
            break
    return messages[:limit]


def normalizeSince(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise FetchError(f"--since must be YYYY-MM-DD, got {value!r}") from exc
    return value


def writeJson(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="fetch only this label (validated against this run's labels)")
    parser.add_argument("--since", help="lookback start date, YYYY-MM-DD")
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"total messages to collect, fetched {PAGE_SIZE} at a time",
    )
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX), help="inbox payload path")
    parser.add_argument("--labels-out", default=str(DEFAULT_LABELS), help="label inventory path")
    parser.add_argument("--dry-run", action="store_true", help="read Gmail, write nothing")
    args = parser.parse_args(argv)

    try:
        labels = listLabels()
        label = selectLabel(labels, args.label) if args.label else None
        query = buildQuery(labels, label, normalizeSince(args.since))
        raw = fetchMessages(query, max(1, min(args.max, 500)))
    except FetchError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    messages = [toInboxMessage(message) for message in raw]
    messages = [message for message in messages if message["messageId"]]
    # Paging can overlap at the boundary if the mailbox changes mid-run.
    seen: set[str] = set()
    unique = []
    for message in messages:
        if message["messageId"] in seen:
            continue
        seen.add(message["messageId"])
        unique.append(message)
    messages = unique
    lookbackFrom = (
        f"{args.since}T00:00:00+00:00"
        if args.since
        else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()
    )
    inbox = {"lookbackFrom": lookbackFrom, "query": query, "messages": messages}

    if not args.dry_run:
        writeJson(pathlib.Path(args.inbox).expanduser(), inbox)
        writeJson(pathlib.Path(args.labels_out).expanduser(), {"labels": labels})

    print(
        json.dumps(
            {
                "ok": True,
                "dryRun": args.dry_run,
                "query": query,
                "labels": len(labels),
                "messages": len(messages),
                "inbox": args.inbox if not args.dry_run else None,
                "labelsFile": args.labels_out if not args.dry_run else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
