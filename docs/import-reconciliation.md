# Import reconciliation

Generated: 2026-10-31T06:11:16+00:00
Mode: write
Source: `job_search_tracker.csv`

## Counts

| metric | value |
|---|---:|
| rows read | 84 |
| unique ids | 84 |
| applications prepared | 84 |
| already present (skipped) | 0 |
| rows with no role | 3 |
| boilerplate notes | 46 |
| CV artifacts | 47 |
| cover-letter artifacts | 42 |
| artifacts with localPath | 40 |
| artifacts with externalUrl | 4 |
| artifacts with driveUrl | 45 |
| warnings | 96 |

## Status map

| stored status | count |
|---|---:|
| `status:applied` | 58 |
| `status:drafting` | 1 |
| `status:interview` | 4 |
| `status:rejected` | 21 |

## Checklist

| check | result |
|---|---|
| rows read == 84 | PASS |
| unique ids == 84 | PASS |
| duplicate ids == 0 | PASS |
| every id derives exactly | PASS |
| notes preserved verbatim | PASS |
| rows with no role == 3 | PASS |
| CV artifacts == 47 | PASS |
| cover-letter artifacts == 42 | PASS |
| one event:imported per row | PASS |
| fitRating within 68-98 | PASS |

## Warnings (non-blocking)

- absolute Drive-mount paths recorded as `driveUrl`: **45**
- rows whose notes are the boilerplate marker: **46**
- line 20: no role recorded; role stays null, not a placeholder
- line 31: no role recorded; role stays null, not a placeholder
- line 40: no role recorded; role stays null, not a placeholder
- line 84: channel 'MLB careers site' mapped to channel:other (original kept in metadata.legacyChannel)
- line 85: channel 'Workday (Snap careers)' mapped to channel:workday (original kept in metadata.legacyChannel)

## What the importer will write

- one `applications/{id}` document per prepared row;
- exactly one `event:imported` per row, dated at submission and marked `derivedFrom: csv-import`;
- one `applications/{id}/artifacts` document per non-empty CV/cover-letter cell.

Nothing in `applications` is overwritten: a row whose document already exists is skipped and reported.
