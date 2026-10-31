#!/usr/bin/env python3
"""Validate job_search_tracker.csv and export it for Firebase / other surfaces.

The CSV is the source of truth. This script validates every row against
job_search_tracker.schema.json's contract and emits a normalized JSON document
set ready to load into Firestore (or any other consumer).

Usage:
    python3 scripts/export_tracker_json.py                  # validate + write generated/job_search_tracker.json
    python3 scripts/export_tracker_json.py --check           # validate only, exit 1 on violation
    python3 scripts/export_tracker_json.py --format ndjson   # one document per line
    python3 scripts/export_tracker_json.py -o out.json --date 2026-10-30

Why a real script and not a one-off: every future writer (/apply, /outcome,
/gmail-sync) appends to the CSV, so the export has to be repeatable and has to
fail loudly the moment the file drifts from the contract.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "job_search_tracker.csv"
DEFAULT_SCHEMA = ROOT / "job_search_tracker.schema.json"
DEFAULT_OUT = ROOT / "generated" / "job_search_tracker.json"

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    return SLUG_SAFE.sub("-", (value or "").lower().strip()).strip("-")


def load_schema(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(path: pathlib.Path, header: list[str]) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [{k: (v or "") for k, v in row.items() if k is not None} for row in rows]


def validate(rows: list[dict], schema: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings).

    Errors mean the file is not exportable. Warnings are known gaps in the
    historical record — a role that was never captured, a rejection whose date
    cannot be recovered — and must never block an export, because blocking on
    them would pressure a future writer into inventing the missing value.
    """
    props = schema["properties"]
    status_enum = props["status"]["enum"]
    terminal = set(props["status"]["x-terminal"])
    problems: list[str] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}

    for offset, row in enumerate(rows, start=2):
        where = f"line {offset} ({row.get('company', '?')})"

        missing_cols = set(props) - set(row)
        if missing_cols:
            problems.append(f"{where}: missing column(s) {sorted(missing_cols)}")

        for field in ("id", "date", "company", "status"):
            if not row.get(field):
                problems.append(f"{where}: required field '{field}' is empty")

        if not row.get("role"):
            expected = slug(row.get("company", ""))
            if row.get("id") and row["id"] != expected:
                problems.append(
                    f"{where}: role is empty, so id must be the bare company slug "
                    f"{expected!r}, found {row['id']!r}"
                )
            else:
                warnings.append(f"{where}: role unknown (kept null, not invented)")

        if row.get("id"):
            if not re.fullmatch(props["id"]["pattern"], row["id"]):
                problems.append(f"{where}: id {row['id']!r} violates slug pattern")
            if row["id"] in seen_ids:
                problems.append(
                    f"{where}: duplicate id {row['id']!r} (also line {seen_ids[row['id']]})"
                )
            else:
                seen_ids[row["id"]] = offset

        if row.get("date") and not re.fullmatch(ISO_DATE.pattern, row["date"]):
            problems.append(f"{where}: date {row['date']!r} is not ISO YYYY-MM-DD")

        if row.get("status") and row["status"] not in status_enum:
            problems.append(
                f"{where}: status {row['status']!r} is outside the closed set "
                f"{sorted(status_enum)}"
            )

        fit = row.get("fit_rating") or ""
        if fit:
            if not re.fullmatch(r"\d{1,3}", fit):
                problems.append(f"{where}: fit_rating {fit!r} is not an integer")
            elif not 0 <= int(fit) <= 100:
                problems.append(f"{where}: fit_rating {fit!r} outside 0-100")

        for field, value in row.items():
            if value and value != value.strip():
                problems.append(f"{where}: '{field}' has leading/trailing whitespace")

        if row.get("status") in terminal and not ISO_DATE.search(row.get("notes", "")):
            warnings.append(
                f"{where}: terminal status {row['status']!r} with no dated resolution in notes"
            )

    return problems, warnings


def export_document(row: dict, rank: dict[str, int], terminal: set[str], today: dt.date) -> dict:
    notes = row.get("notes") or ""
    event_dates = sorted(set(ISO_DATE.findall(notes)))
    candidates = [d for d in [row.get("date")] + event_dates if d and ISO_DATE.fullmatch(d)]
    last_activity = max(candidates) if candidates else None
    days_since = None
    if last_activity:
        try:
            days_since = (today - dt.date.fromisoformat(last_activity)).days
        except ValueError:
            days_since = None

    def nullable(field: str):
        value = (row.get(field) or "").strip()
        return value or None

    status = row.get("status") or None
    fit = row.get("fit_rating") or ""

    return {
        "id": row.get("id"),
        "date": nullable("date"),
        "company": nullable("company"),
        "company_id": slug(row.get("company", "")),
        "sector": nullable("sector"),
        "role": nullable("role"),
        "role_type": nullable("role_type"),
        "channel": nullable("channel"),
        "status": status,
        "status_rank": rank.get(status) if status else None,
        "is_open": status not in terminal if status else None,
        "contact_person": nullable("contact_person"),
        "fit_rating": int(fit) if fit else None,
        "notes": nullable("notes"),
        "event_dates": event_dates,
        "last_activity_date": last_activity,
        "days_since_activity": days_since,
        "follow_ups_sent": len(re.findall(r"followed up\s+\d{4}-\d{2}-\d{2}", notes, re.I)),
        "cv_file": nullable("cv_file"),
        "cover_letter_file": nullable("cover_letter_file"),
        "source": nullable("source"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--input", type=pathlib.Path, default=DEFAULT_CSV)
    parser.add_argument("-o", "--output", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--format", choices=("json", "ndjson"), default="json")
    parser.add_argument("--date", help="Override 'today' for derived day counts (YYYY-MM-DD)")
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1

    schema = load_schema(args.schema)
    header = schema["x-csv-header"]
    rows = load_rows(args.input, header)

    with args.input.open(newline="", encoding="utf-8") as handle:
        actual_header = next(csv.reader(handle))
    if actual_header != header:
        print("error: CSV header does not match schema x-csv-header", file=sys.stderr)
        print(f"  expected: {header}", file=sys.stderr)
        print(f"  actual:   {actual_header}", file=sys.stderr)
        return 1

    problems, warnings = validate(rows, schema)
    if problems:
        print(f"FAIL: {len(problems)} contract violation(s) in {args.input.name}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if warnings:
        print(f"WARN: {len(warnings)} known gap(s) in the historical record (non-blocking)", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)

    if args.check:
        print(f"OK: {len(rows)} rows conform to the tracker contract")
        return 0

    rank = schema["properties"]["status"]["x-status-rank"]
    terminal = set(schema["properties"]["status"]["x-terminal"])
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    documents = [export_document(row, rank, terminal, today) for row in rows]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "ndjson":
        payload = "\n".join(json.dumps(doc, ensure_ascii=False) for doc in documents) + "\n"
    else:
        payload = json.dumps(
            {
                "meta": {
                    "source": args.input.name,
                    "collection": schema["x-firestore-collection"],
                    "count": len(documents),
                    "generated_at": today.isoformat(),
                    "schema": schema["$id"],
                },
                "documents": documents,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n"
    args.output.write_text(payload, encoding="utf-8")

    open_rows = sum(1 for doc in documents if doc["is_open"])
    print(f"OK: {len(documents)} documents -> {args.output} ({open_rows} open, {len(documents) - open_rows} closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
