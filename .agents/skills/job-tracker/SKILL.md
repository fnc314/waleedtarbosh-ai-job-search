---
name: job-tracker
description: >
  Operates the job application record. Firestore is the only write target and
  `scripts/tracker.py` is the only way to write it; the Gmail collector stages
  proposals for review, artifacts live in GCS, and the Google Sheet is a
  regenerated view. Use for tracking applications, recording outcomes,
  recalculating fit, syncing Gmail status, regenerating the Sheet or dashboard,
  or answering "what does the tracker know?".
---

# Job Tracker

One record, one write path, one history. This skill states the invariants an
agent must not break; the detail lives in `docs/tracker-migration-plan.md` and
the contract in `tracker.schema.json`.

## The three rules

1. **Firestore is the record.** `job_search_tracker.csv` is a frozen pre-migration
   snapshot in `archive/`. Never read it as the source and never write it.
2. **Every write goes through `scripts/tracker.py`.** No hand-edited documents,
   no direct Firestore writes from a workflow, no CSV cells. Writes are opt-in:
   the CLI refuses unless `TRACKER_ALLOW_WRITES=1`.
3. **History is append-only.** A status change is a new event. A correction is
   `event:status-corrected`, not an edit. Derived fields (`isOpen`, counters,
   `notesRendered`, `timeline`) are projections of the log and are recomputed,
   never typed.

## Commands

| goal | command |
|---|---|
| Inspect | `tracker.py list [--open --status --tag --limit]`, `get <id>` |
| Add | `tracker.py add --json <file\|->`, `upsert --json …`, `bulk --json …` |
| Move the funnel | `tracker.py status <id> <status> [--date --note] [--correction]` |
| Record activity | `tracker.py event <id> --type event:* [--date --note --json -]` |
| Record a fit change | `tracker.py score <id> --fit N [--note]` |
| Register a file | `tracker.py artifact <id> --kind artifact:* --path P` |
| Review Gmail | `tracker.py proposals list\|show\|approve\|reject` |
| Fetch support | `tracker.py labels` (last seen Gmail labels) |
| Views | `tracker.py sheet --sync`; `scripts/generate_report.py` |
| Verify | `tracker.py check`; `export --csv` is the rollback path |

All commands print JSON on stdout. Status accepts legacy labels (`applied`) or
namespaced values (`status:applied`); the CLI normalizes. Never invent a status
or an event type — both are closed enums in `tracker.schema.json`.

## Reading

Use `scripts/reader.py` (the TUI and dashboard already do). It returns the
legacy row shape with a provenance badge: `store (live)`, `cache (…old)`,
`csv (pre-migration)`. `TRACKER_SOURCE=store|cache|csv` forces a source.
A UI that shows stale data must say so; never hide the badge.

## Gmail

`/gmail-sync` has two halves:

- **Fetch + stage:** read Gmail, write `generated/gmail-inbox.json` and
  `generated/gmail-labels.json`, run `tracker.py collect --labels …`.
  `collect` stages one proposal and writes `meta/sync` only. It never touches
  applications.
- **Review:** present `proposals show`, wait for an explicit reply, then
  `proposals approve <id> [--only 1,3]`. Approval emits events in one batch.

Scopes: Primary, Updates, `JOBS`, and every `JOBS/*` label seen last run.
`--label` is validated against `tracker.py labels`; unknown values are refused,
not guessed.

## Artifacts and views

- Blob metadata (`sha256`, `size`, `contentType`, `storage`) lives on the
  artifact document; bytes live in `gs://job-search-fnc314-dev-artifacts`.
  `.gdoc` shortcuts and `externalUrl` records have no blob by design.
- The Google Sheet is regenerated wholesale (`sheet --sync`) and is never
  hand-edited.
- `generated/` holds disposable caches. Local documents stay in `documents/`.

## Automating

Two `launchd` user agents, both reading credentials already on disk (the tracker
service account and the Composio CLI login):

- `dev.fnc314.tracker-gmail-fetch` — every 12 hours, runs
  `scripts/gmail-fetch.sh`: `gmail_fetch.py` writes `generated/gmail-inbox.json`
  and `gmail-labels.json`, `tracker.py collect --trigger trigger:cron` stages a
  proposal, then `sync-due` runs. Review stays interactive.
- `dev.fnc314.tracker-collect` — every 15 minutes, runs `tracker.py sync-due`.
  A write queues one deferred collection run; `sync-due` leaves requests pending
  when no inbox payload exists, so a missing fetch never consumes the backlog.

The fetcher pages 50 messages at a time because Composio rejects larger single
responses, and `collect` records every message it examined — not only those that
produced a change — so a scheduled run over the same mail stages nothing twice.

Inspect with `launchctl print gui/$(id -u)/<label>` and
`~/Library/Logs/job-tracker-*.log`; install from `deploy/launchd/`.

## Before finishing a tracker task

- `TRACKER_ALLOW_WRITES=1` was set deliberately for the write, not exported
  globally by accident.
- The change is an event, and `tracker.py get <id>` shows it.
- If the Sheet matters, regenerate it; do not describe it as current otherwise.
- Never commit personal data (`documents/`, `generated/`, `.env.local`).
