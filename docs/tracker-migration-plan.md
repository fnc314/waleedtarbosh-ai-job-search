# Tracker Migration Plan — CSV → Firestore

**Status:** complete — Phases 0–8, 9a, 9b, and 10 landed. Firestore holds the record (84 imported applications), every writer goes through `tracker.py`, the Sheet and dashboard are generated views, the CSV lives in `archive/`, and a 12-hour `launchd` job fetches Gmail and stages proposals for review
**Owner:** Franco N. Colaizzi
**Supersedes:** `job_search_tracker.schema.json` v1 (CSV contract), `scripts/export_tracker_json.py`
**Last updated:** 2026-10-31
**Update rule:** this file and `docs/tracker-migration.md` are revised at the end of **every phase**, including when an implementation decision changes the approved scope (§16 records those).

## Progress

| phase | state | artifact |
|---|---|---|
| 0 | **complete** | project `job-search-fnc314-dev` (Blaze, $5 budget alert), APIs enabled, bucket `gs://job-search-fnc314-dev-artifacts` (US, uniform access, public-access-prevention), SA `tracker@…` with `datastore.user` + bucket-scoped `storage.objectAdmin`, key at `~/.config/job-tracker/sa.json` (600), Firestore `nam5` native + 7-day PITR, deny-all rules and 6 composite indexes deployed, `meta/schema` seeded |
| env | **complete** | `GOOGLE_API_KEY` moved from `mise.toml` to `.env.local` (gitignored, loaded via `[env] _.file`); `TRACKER_ALLOW_WRITES=1` set there; `.env.example` documents the variables |
| 1 | **complete** | `tracker.schema.json` v4, `tests/test_tracker_schema.py` (34 tests), `tests/test_contract.py` (43 tests), `requirements-tracker.txt` |
| 2 | **complete** | `scripts/{client,contract,events,render,tracker}.py`; subcommands `check`, `seed`, `list`, `get`, `add`, `upsert`, `status`, `event`; `tests/test_tracker_cli.py` (39 tests, 13 emulator-backed) |
| 2b | **complete** | `scripts/{importer,cache,artifacts,tasks,proposals,sheets}.py`; subcommands `artifact`, `bulk`, `import`, `export`, `sheet`, `proposals`, `collect`, `sync-due`; `tests/test_tracker_modules.py` (47 tests, 10 emulator-backed); `export --csv` byte-identical to the live 84-row file in the emulator |
| 3 | **complete** | live import: 84 applications, 84 `event:imported`, 89 artifacts (40 `localPath`, 45 `driveUrl`, 4 `externalUrl`); re-run skipped all 84; `export --csv` byte-identical (SHA-256 `3367ce42…f6ab`) |
| 4 | **complete** | `scripts/reader.py` (store → cache → CSV, provenance badge); `tracker_tui.py` and `generate_report.py` read through it; regenerated dashboard differs from the committed one only by the `Source: store (live)` line; `tests/test_reader.py` (24 tests) |
| 5 | **complete** | `tracker.py score`; `/apply`, `/outcome`, `/gmail-sync`, `/rank`, `/interview`, `/notion-sync`, `/upskill`, `/html-report`, `/scrape` read/write the store; TUI `r` writes a structured recalc request; `bulk_add_tracker.py` and `update_state.py` deleted; no script or command opens the CSV for writing; `tests/test_tracker_cli.py` +4 emulator tests, `tests/test_tracker_tui.py` +2 |
| 7 | **complete** | Drive API enabled (to locate the shared file); `TRACKER_SHEET_ID=1na6ir5P…` in `.env.local`; `tracker.py sheet --sync` wrote 3 tabs — Applications 84 rows, Events 84, Summary 13; the default `Sheet1` was removed |
| 8 | **complete** | `tracker.py backfill` fills `storage`, `sha256`, `size`, `contentType` in place; 61 of 89 artifacts uploaded to `gs://job-search-fnc314-dev-artifacts` (40 `localPath` + 21 Drive-mount files, 20.2 MB), 28 correctly skipped (24 `.gdoc` stubs, 4 `externalUrl`); a second run reuses all 61 and uploads none; `tests/test_tracker_cli.py` +6 path-resolution tests +1 emulator dry-run test |
| 9a | **complete** | `deploy/launchd/dev.fnc314.tracker-collect.plist` installed as `gui/$UID/dev.fnc314.tracker-collect` (every 15 min, `RunAtLoad`, key path from disk); first run exited 0 with `due: 0`; `sync-due` now leaves requests pending when no inbox payload exists instead of claiming and completing them; `tests/test_tracker_modules.py` +1 emulator test |
| 6 | **complete** | `/gmail-sync` split into fetch → `collect` → human review; `collect` stages one proposal and writes `meta/sync` (processed ids + Gmail label inventory) only; `proposals approve --only` supports selective review; TUI header shows the pending-review count; `tracker.py labels` exposes the last-seen inventory for `--label` validation; `tests/test_tracker_modules.py` +4 emulator tests, `tests/test_tracker_cli.py` +3 label tests, `tests/test_reader.py` +1 |
| 10 | **complete** | `.agents/README.md` + `.agents/skills/job-tracker/SKILL.md` are the canonical instructions; each `.<agent>` directory holds one pointer file (`.claude/CLAUDE.md`, `.junie/JUNIE.md`, `.gemini/GEMINI.md`, `.opencode/OPENCODE.md`, `.qwen/QWEN.md`) instead of mirrored command/skill trees; `job_search_tracker.csv`, the retired CSV schema, and `export_tracker_json.py` moved to `archive/` with an `archive/README.md`; `tests/test_export_tracker.py` deleted; `.gitignore` and `tools/security_guards.py` now guard `archive/*.csv`; README/SETUP/documents/AGENTS scrubbed of CSV-era instructions |
| 9b | **complete** | `scripts/gmail_fetch.py` + `scripts/gmail-fetch.sh` + `deploy/launchd/dev.fnc314.tracker-gmail-fetch.plist` installed as `gui/$UID/dev.fnc314.tracker-gmail-fetch` (every 12 h, `RunAtLoad`): fetch → `collect --trigger trigger:cron` → `sync-due`; fetches in pages of 50 because Composio rejects larger payloads; `collect` now marks every examined message processed so a scheduled run is idempotent; `tests/test_gmail_fetch.py` (13 tests) + 1 emulator idempotency test. Cloud Run/Scheduler was considered and rejected: it would add a credential surface for no benefit on an always-on laptop |


---

## 1. Why

`job_search_tracker.csv` is **gitignored and untracked** (`.gitignore:79`, enforced by
`tools/security_guards.py:60`). The 84 tracked applications — company names, recruiter
contacts, rejection reasons, compensation bands — exist on exactly one laptop, with no
version history, no backup, and no concurrency control.

Three concrete failure modes today:

1. **Total data loss** if the disk fails. Nothing else holds this record.
2. **Silent corruption from hand-editing.** The schema documents one real incident: an
   unquoted comma in `role` shifted every later column. A 14-column CSV edited by hand
   and by scripts is one typo away from mangled history.
3. **History lives in prose.** Status transitions, rejection dates, and follow-up counts
   are recovered by regex over a `notes` string. That is unqueryable, lossy, and
   impossible to validate.

The goal is a properly managed database with a single write path, first-class event
history, and enough typed structure to power the TUI and dashboard without parsing text.

## 2. Scope

**In scope**

- Firestore as the authoritative system of record.
- A single Python write path (`scripts/tracker.py`) accepting JSON payloads only.
- Append-only event history replacing free-text notes.
- Google Sheets as a regenerated, never-hand-edited view.
- Artifact metadata in Firestore, blobs in a GCS bucket, local files kept as today.
- Porting every consumer: TUI, dashboard, reports, gmail-sync, bulk-add, state files.
- A collector/reviewer split so Gmail sync can run unattended.
- Skill + documentation migration, with the CSV system scrubbed once cut over.

**Out of scope (deliberately)**

- Any new user-facing surface. The only consumers are `ui/terminal/tracker_tui.py`
  and `ui/web/application-dashboard.html`.
- Public hosting, web SDK, Firebase Auth, client-side security rules. Rules deny all
  client access; only the Admin SDK (service account) may read or write.
- Generalizing the system beyond this one user. Cloud Functions are noted as the future
  home for any logic worth abstracting, but nothing is deployed now.

## 3. Naming conventions

The governing rule set, in priority order.

| Context | Convention | Example |
|---|---|---|
| Firestore / JSON document keys (**all nesting levels**) | **lowerPascalCase** | `date`, `fitRating`, `timeline.lastActivityAt` |
| `metadata` subkeys | preserved verbatim as authored | `metadata.recruiterBand` |
| Enum values | `namespace:kebab` + optional `:UPPERCASE` qualifier | `status:offer-declined`, `event:interview-scheduled`, `compensation:above-super-commuter:NYC` |
| Collections / subcollections | kebab-case, plural | `applications`, `pending-proposals`, `sync-requests` |
| Firestore document IDs — **derived** | kebab-case slug, from content | `doordash--software-engineer-devex-android`, `doordash` |
| Firestore document IDs — **generated** | lowercase UUIDv7, no prefix | `018f3c1a-4b2e-7d3f-9a41-2c6b8e5d0f17` |
| Google Sheets headers | lowerPascalCase (mirrors Firestore) | `date \| company \| role \| status \| fitRating` |
| CLI flags / subcommands | kebab-case | `--dry-run`, `--artifact-kind`, `sync-sheets` |
| Python module filenames | single lowercase word | `tracker.py`, `client.py`, `proposals.py` |
| Python classes | `PascalCase` | `TrackerClient` |
| Python functions / variables | `lowerPascalCase` | `resolveArtifacts()`, `eventDates` |
| Constants / environment variables | `UPPER_SNAKE_CASE` | `TRACKER_SA_KEY`, `DEFAULT_LOOKBACK` |

**Never used in new code:** `lower_snake_case` identifiers, leading-capital document keys.

Module filenames are single lowercase words specifically to sidestep the kebab-vs-import
conflict — Python cannot import a kebab-case module, and `tracker_cli.py` would violate the
`lower_snake_case` ban. One word resolves it legally.

Region qualifiers on tags are `UPPERCASE` abbreviations (`NYC`, `SF`, `LA`, `DC`, `PIT`,
`REMOTE-US`) validated against `enums.regions`, so an unrecognized qualifier is a hard
error rather than a silent new tag.

## 4. Architecture

```
                     ┌──────────────────────────────┐
   agents / TUI ───► │  scripts/tracker.py  (CLI)   │ ──► Firestore (canonical)
   /apply       ───► │  JSON in / JSON out only     │       • applications/{id}
   /outcome     ───► │  validation + event emission │       • applications/{id}/events/*
   collector    ───► │  no ad-hoc SDK calls         │       • applications/{id}/artifacts/*
                     └──────────┬───────────────────┘       • pending-proposals/*
                                │                           • sync-requests/*
                                ├──► GCS bucket (PDF blobs)
                                ├──► Google Sheets (regenerated view)
                                └──► generated/tracker-cache.json (offline read cache)
```

**Invariants**

1. `applications` is written **only** by `scripts/tracker.py` and `scripts/client.py`.
2. The collector writes `pending-proposals` and `meta/sync` — never `applications`.
3. `notesRendered` is derived output. Nothing writes it by hand.
4. Every mutation is a validated JSON payload. No agent edits Firestore directly.
5. Reads tolerate being offline via a local cache; writes never do.

## 5. Infrastructure

| Decision | Choice | Rationale |
|---|---|---|
| Project | `job-search-fnc314-dev` | Dedicated; isolated IAM and rules; trivially deletable |
| Plan | Blaze, budget alert at $5/mo | GCS requires it; actual spend on ~85 documents rounds to $0.00 |
| Firestore location | **`nam5`** (US multi-region) | Contains `us-east4` as a replica, so Pittsburgh latency is the same as a regional database (~15–20 ms) with a 99.999% vs 99.95% SLA. Cost delta is $0.108 vs $0.09 per 100k writes. |
| Bucket location | `US` multi-region | Matches `nam5`; no cross-region egress |
| Blob store | **Plain GCS bucket**, not "Firebase Storage" | Firebase Storage is only a client-rules layer over a GCS bucket. Rules deny all clients, so the layer buys nothing. |
| Auth | Service account `tracker@job-search-fnc314-dev.iam.gserviceaccount.com` | One credential covers Firestore + GCS + Sheets |
| SA roles | `roles/datastore.user`, `roles/storage.objectAdmin` (bucket-scoped) | Least privilege |
| SA key | `~/.config/job-tracker/sa.json` (mode 600), via `TRACKER_SA_KEY` | Never in the repo; new ignore rule added to `security_guards.py` |
| Python | **3.15.2**, pinned in `mise.toml` and CI | Verified wheels: `grpcio 1.85.1` ships `cp315-macosx_10_15_universal2`; `google-crc32c 1.9.0` has cp315. `google-cloud-firestore 2.34.1` declares 3.15. `google-api-python-client`, `google-auth`, `proto-plus`, `protobuf`, `jsonschema`, `google-cloud-storage` are pure-Python. No blocker. |
| Automation host | Local `launchd` (default); Cloud Run documented but undeployed | Avoids putting `COMPOSIO_API_KEY` in GCP Secret Manager for a single-user tool on the machine already in use |

> **Firestore's location is permanent.** A database's region cannot be changed after
> creation — relocating means a new database and a full re-import. This is the only
> irreversible step in the migration and is performed last in Phase 0, after the schema
> and a dry-run reconciliation have been reviewed.

## 6. Firestore schema

Registry lives at `meta/schema` and is the single source of truth for every enum.
`tracker.schema.json` is generated from / validated against it.

### 6.1 Enum registry

```json
{
  "schemaVersion": 4,
  "statusRank": {
    "status:drafting": 0, "status:applied": 1, "status:interview": 2,
    "status:offer": 3, "status:hired": 4, "status:rejected": 5,
    "status:no-response": 6, "status:offer-declined": 7, "status:withdrawn": 8
  },
  "terminalStatuses": [
    "status:hired", "status:rejected", "status:no-response",
    "status:offer-declined", "status:withdrawn"
  ],
  "enums": {
    "eventType": [
      "event:applied", "event:acknowledged", "event:recruiter-screen",
      "event:interview-scheduled", "event:interview-completed", "event:assessment",
      "event:onsite", "event:offer", "event:offer-accepted", "event:offer-declined",
      "event:rejection", "event:no-response", "event:withdrawn", "event:follow-up-sent",
      "event:document-tailored", "event:fit-recalculated", "event:artifact-added",
      "event:note-added", "event:imported", "event:status-corrected"
    ],
    "channel": [
      "channel:greenhouse", "channel:workday", "channel:lever", "channel:ashby",
      "channel:smartrecruiters", "channel:linkedin", "channel:referral",
      "channel:direct", "channel:other"
    ],
    "artifactKind": [
      "artifact:cv", "artifact:cover-letter", "artifact:outcome",
      "artifact:posting", "artifact:assessment"
    ],
    "workMode": ["work-mode:onsite", "work-mode:hybrid", "work-mode:remote"],
    "actor": ["actor:user", "actor:agent", "actor:gmail-sync", "actor:import", "actor:scheduler"],
    "proposalState": [
      "proposal:pending", "proposal:approved", "proposal:rejected", "proposal:expired"
    ],
    "tag": [
      "relocation:required", "relocation:accepted", "relocation:declined",
      "compensation:above-super-commuter:NYC", "compensation:above-super-commuter:SF",
      "compensation:below-nyc-floor", "compensation:below-band", "compensation:at-band",
      "compensation:above-band", "compensation:equity-heavy",
      "fit:strong", "fit:stretch", "fit:gap-ios", "fit:gap-cross-platform",
      "process:take-home", "process:referral", "process:internal-contact",
      "process:no-response-30d"
    ]
  },
  "regions": ["PIT", "NYC", "SF", "LA", "DC", "REMOTE-US"]
}
```

Every enum value carries its own namespace, so `status:rejected` remains unambiguous
wherever it lands — including inside a free-form `tags` array.

### 6.2 `applications/{id}`

Document ID is the existing stable slug, unchanged from the CSV, so the migration is
verifiable field-by-field.

**Tier 1 — 1:1 with the CSV contract**

| field | type | notes |
|---|---|---|
| `id` | string | immutable; `^[a-z0-9]+(-[a-z0-9]+)*(--…)?$` |
| `date` | Timestamp | submission date |
| `company` | string | display name, verbatim |
| `companyId` | string | slug; groups into `companies/*` |
| `sector` | string \| null | |
| `role` | string \| null | |
| `roleType` | string \| null | |
| `channel` | `channel:*` \| null | |
| `status` | `status:*` | closed set |
| `statusRank` | int | derived |
| `isOpen` | bool | derived — `status` not in `terminalStatuses` |
| `contactPerson` | string \| null | promoted to `contactIds` when a `contacts/*` record exists |
| `fitRating` | int \| null | 0–100 |
| `source` | string \| null | posting URL, else portal name |
| `notesRendered` | string \| null | **derived**, read-only |
| `notesLegacy` | string \| null | exact pre-migration `notes` blob, preserved verbatim |

**Tier 2 — typed expansion**

| field | shape |
|---|---|
| `tags` | `string[]` from `enums.tag` |
| `metadata` | `map<string, any>` — unknown keys preserved on update, never dropped |
| `fitEvaluation` | `{rubricVersion, evaluatedAt, gaps: string[]}` |
| `compensation` | `{min, max, currency, period, bandSource, vsTarget}` |
| `location` | `{city, region, country, workMode, relocation, superCommuter}` |
| `counters` | `{events, followUpsSent, interviews, rejections}` |
| `timeline` | `{firstActivityAt, lastActivityAt, daysSinceActivity, nextFollowUpAt}` |
| `contactIds` | `string[]` → `contacts/*` |
| `createdAt`, `updatedAt` | Timestamp |
| `schemaVersion` | int |

**Worked example**

```json
{
  "id": "doordash--software-engineer-devex-android",
  "date": "2026-10-30T00:00:00-04:00",
  "company": "DoorDash",
  "companyId": "doordash",
  "role": "Software Engineer, Developer Experience (Android)",
  "roleType": "Mobile (Android)",
  "channel": "channel:greenhouse",
  "sector": "Tech",
  "status": "status:applied",
  "statusRank": 1,
  "isOpen": true,
  "contactPerson": null,
  "fitRating": 88,
  "source": "https://job-boards.greenhouse.io/doordashusa/jobs/8197854",
  "tags": [
    "relocation:accepted",
    "compensation:above-super-commuter:NYC",
    "compensation:below-nyc-floor",
    "fit:strong"
  ],
  "metadata": { "recruiterBand": "130600-192000", "levelExpected": "staff" },
  "fitEvaluation": { "rubricVersion": 2, "evaluatedAt": "2026-10-30T00:00:00-04:00", "gaps": [] },
  "compensation": { "min": 130600, "max": 192000, "currency": "USD", "period": "year", "vsTarget": null },
  "location": { "city": "New York", "region": "NY", "country": "US", "workMode": "work-mode:hybrid", "relocation": true, "superCommuter": true },
  "counters": { "events": 2, "followUpsSent": 0, "interviews": 0, "rejections": 0 },
  "timeline": { "firstActivityAt": "2026-10-30T00:00:00-04:00", "lastActivityAt": "2026-10-30T00:00:00-04:00", "daysSinceActivity": 1, "nextFollowUpAt": null },
  "notesRendered": "2026-10-30 · event:applied — Applied via Greenhouse; relocation to NYC or LA accepted.",
  "notesLegacy": "Applied 2026-10-30 via Greenhouse; relocation to NYC or LA accepted; band $130.6k-$192k sits below NYC floor, level conversation expected in recruiter screen; 2026-10-30 gmail-sync: application ack (\"Thank you for applying to DoorDash\")",
  "contactIds": [],
  "createdAt": "2026-10-30T00:00:00-04:00",
  "updatedAt": "2026-10-30T14:22:00-04:00",
  "schemaVersion": 4
}
```

### 6.3 `applications/{id}/events/{eventId}` — append-only history

| field | type |
|---|---|
| `eventId` | string — UUIDv7, lexicographically sortable by creation time |
| `ts` | Timestamp (UTC) |
| `date` | string, ISO 8601 |
| `type` | `event:*` |
| `statusFrom`, `statusTo` | `status:*` \| null |
| `note` | string \| null |
| `actor` | `actor:*` |
| `emailMessageId` | string \| null |
| `derivedFrom` | `manual` \| `notes-regex` \| `gmail` \| `csv-import` |
| `confidence` | `high` \| `medium` \| `low` |
| `metadata` | map |

This is the direct answer to "notes should be discouraged." `tracker.py event` is the only
way to record anything; `notesRendered` is a generated one-line-per-event summary.

`derivedFrom` + `confidence` exist because migrated history is genuinely lossy — a
rejection date recovered by regex is not the same evidence as a recorded fact. Marking it
keeps the record honest and revisable.

### 6.4 `applications/{id}/artifacts/{artifactId}`

| field | type |
|---|---|
| `artifactId` | string — UUIDv7, generated locally |
| `kind` | `artifact:*` |
| `filename`, `contentType`, `size`, `sha256` | |
| `storage` | `{bucket, path}` \| null |
| `localPath` | repo-relative, as today |
| `driveUrl`, `externalUrl` | string \| null |
| `createdAt`, `source` | |

Blobs live at `gs://job-search-fnc314-dev-artifacts/applications/{id}/{kind}/{filename}`.

The historical record is mostly **not** repo-relative: 45 of 89 populated artifact cells are
absolute Google Drive mount paths (`driveUrl`), 4 are Google Docs URLs (`externalUrl`), and
40 are repo files (`localPath`). Uploading a file that no longer resolves is a per-artifact
decision made by `artifact --path`, never an assumption made by the importer.

### 6.5 `pending-proposals/{proposalId}`

The queue that makes unattended collection possible.

| field | type |
|---|---|
| `createdAt`, `dueAt`, `collectedAt` | Timestamp |
| `kind` | `proposal:gmail-sync` \| `proposal:sheet-sync` |
| `state` | `proposal:*` |
| `trigger` | `trigger:cron` \| `trigger:application-created` \| `trigger:application-updated` \| `trigger:manual` |
| `scope` | `{applicationIds[], lookbackFrom, query}` |
| `changes` | `[{applicationId, eventType, statusFrom, statusTo, sourceEmail, confidence}]` |
| `unmatched` | `[{sender, subject, receivedAt, reason}]` |
| `reviewedAt`, `reviewedBy`, `resolution` | |

### 6.6 `sync-requests/{requestId}`

Local stand-in for Cloud Tasks. Created by `tracker.py` writes, consumed by the
`launchd` poller.

| field | type |
|---|---|
| `dueAt` | Timestamp — `now + 2h`, or `+6h` for terminal statuses |
| `kind` | `sync:gmail-collect` |
| `applicationIds` | `string[]` — used for dedupe |
| `state` | `sync:pending` \| `sync:claimed` \| `sync:done` \| `sync:failed` |
| `attempts` | int |
| `createdAt`, `lastError` | |

### 6.7 Supporting documents

- `companies/{companyId}` — `{name, aliases[], sector, careersUrl, notes, metadata}`
- `contacts/{contactId}` — `{name, role, companyId, linkedinUrl, email, applicationIds[], metadata}`
- `meta/schema` — the enum registry above
- `meta/sync` — `{gmail: {processedMessageIds[], lastCollectedAt, lastApprovedAt}, sheets: {lastFullSyncAt}}`

`meta/sync` replaces `gmail_sync/state.json`, `.gemini/last_gmail_sync.timestamp`, and
`.qwen/last_gmail_sync.timestamp`.

### 6.8 Indexes

- `applications`: `status + date desc`
- `applications`: `isOpen + timeline.lastActivityAt desc`
- `applications`: `companyId + date desc`
- `applications`: `fitRating desc`
- `applications`: `tags` array-contains
- collection-group `events`: `type + ts`

### 6.9 Security rules

```
rules_version = '2';
service cloud.firestore {
  match /databases/{db}/documents {
    match /{document=**} { allow read, write: if false; }
  }
}
```

Client access is denied entirely. The Admin SDK bypasses rules, which is the correct
posture for a single-user, local-only tool.

## 7. Google Sheets view

Spreadsheet **"Job Search Tracker (Firestore view)"**, three tabs, **full replace on every
sync**, never hand-edited.

| tab | columns |
|---|---|
| Applications | `id, date, company, role, sector, channel, status, fitRating, workMode, compMin, compMax, tags, lastActivityAt, daysSinceActivity, followUpsSent, source, cv, coverLetter` |
| Events | `applicationId, date, type, statusFrom, statusTo, note, actor` |
| Summary | totals, funnel %, by sector, by channel, median days to first response |

"Dynamic on view": `application-dashboard-server.py` calls `tracker.sheets.sync()` on
startup and on `/refresh`, before regenerating the HTML.

**Implementation status (2b).** `scripts/sheets.py` builds all three tabs as pure row
builders and replaces them wholesale on sync. When no spreadsheet exists yet,
`sheet --local-only` writes the same tables to `generated/sheets-view/*.csv` so the view can
be reviewed before a spreadsheet is created and shared with the service account.

**Composio assessment.** The `googlesuper` connection is ACTIVE, so Composio *can* write
Sheets and Drive. The plan uses the **Sheets API with the service account** instead:
one credential already required for Firestore and GCS, one deterministic `batchUpdate`
per tab, and no dependency on unverified Composio write scopes. Composio remains the
right tool for Gmail (already working) and as a fallback if sharing the sheet with a
service-account email is ever blocked.

## 8. The JSON payload

Replaces hand-editing. `id` is derived from `company` + `role` when omitted, using the
same slug algorithm as today, so IDs stay identical across the migration.

```json
{
  "company": "DoorDash",
  "role": "Software Engineer, Developer Experience (Android)",
  "date": "2026-10-30",
  "status": "status:applied",
  "channel": "channel:greenhouse",
  "source": { "url": "https://job-boards.greenhouse.io/doordashusa/jobs/8197854", "portal": "greenhouse", "reqId": "8197854" },
  "fitRating": 88,
  "compensation": { "min": 130600, "max": 192000, "currency": "USD", "period": "year" },
  "location": { "workMode": "work-mode:hybrid", "city": "New York", "region": "NY", "relocation": true, "superCommuter": true },
  "tags": ["relocation:accepted", "compensation:below-nyc-floor", "fit:strong"],
  "metadata": { "levelExpected": "staff" },
  "events": [{ "type": "event:applied", "date": "2026-10-30", "actor": "actor:user" }],
  "artifacts": [{ "kind": "artifact:cv", "localPath": "cv/main_DoorDash_SE_DevEx_Android.pdf" }]
}
```

`--dry-run` prints the resolved document, all derived fields, and the events that would be
emitted, without writing.

## 9. CLI surface

| command | purpose |
|---|---|
| `add --json <file\|->` | create; fails on duplicate ID |
| `upsert --json <file\|-> [--dry-run]` | create-or-merge; merges `metadata` and `tags` |
| `status <id> <status> [--date] [--note]` | emits a status event, updates derived fields |
| `event <id> --type T --json {...}` | append a timeline event |
| `artifact <id> --kind K --path P [--no-upload]` | register (and by default upload) a file |
| `score <id> --fit N` | record a recalculated fit rating |
| `backfill [--apply]` | add hashes and storage to imported artifacts |
| `bulk --json <file>` | batch add with per-row validation |
| `import --csv job_search_tracker.csv [--dry-run]` | one-shot migration |
| `export [--csv\|--json\|--ndjson] -o PATH` | read back out; the rollback path |
| `sheet --sync` | regenerate the Sheets view |
| `proposals list\|show\|approve\|reject` | review the queue |
| `collect [--dry-run]` | run the unattended Gmail collector |
| `sync-due` | execute due `sync-requests` (the `launchd` entrypoint); no-op when the inbox was not fetched |
| `check` | schema validation; CI gate |
| `list [--status --tag --open]` / `get <id>` | JSON to stdout |

All output is JSON on stdout, diagnostics on stderr, so everything composes with `jq`.

Implemented: `check`, `seed`, `list`, `get`, `add`, `upsert`, `status`, `score`,
`event`, `artifact`, `backfill`, `bulk`, `import`, `export`, `sheet`, `proposals`,
`collect`, `sync-due`. Outstanding: nothing in the CLI surface.

## 10. Automation

```
launchd agent dev.fnc314.tracker-gmail-fetch   (every 12 h)
└── scripts/gmail-fetch.sh
    ├── gmail_fetch.py        → generated/gmail-inbox.json + gmail-labels.json
    ├── tracker.py collect --trigger trigger:cron
    └── tracker.py sync-due

launchd agent dev.fnc314.tracker-collect       (every 15 min)
└── tracker.py sync-due
    ├── claims due sync-requests → tracker.py collect
    └── deletes completed requests
```

### The collector / reviewer split

`/gmail-sync` as written **cannot be cron'd**. `gmail-sync.md` Step 7 reads: *"Stop here
and wait for the user's reply. Do not write anything from this run's classification before
an explicit response arrives."* A scheduled job has no approver.

So the command splits:

| stage | deterministic? | runs where |
|---|---|---|
| **collect** — fetch, match, classify, stage | yes | `launchd` / cron |
| **review** — present batch, wait, emit events | no (LLM + human) | `/gmail-sync`, as today |

`collect` writes `pending-proposals` and `meta/sync` only. It **never** touches
`applications`. The approval gate is preserved exactly, and moves from "keep a session
open" to "review a queue at your convenience."

### The 2-hour backlog

Deferred sync after a write, using `sync-requests` locally:

| write | enqueues |
|---|---|
| `add` (from `/apply`) | `gmail-collect` @ +2h |
| `status <id> <terminal>` (from `/outcome`) | `gmail-collect` @ +6h — rejection confirmations lag |
| `status <id> <non-terminal>` | `gmail-collect` @ +2h |
| any `event` | nothing — the 6-hourly run catches it |

Requests collapse: a pending request covering the same applications is reused rather than
duplicated, so five writes in ten minutes produce one run.

When enabled, Cloud Tasks replaces the poller with native `scheduleTime` and named-task
dedupe. The collector is a plain script either way, so the scheduler is a deployment
detail, not an architectural one.

### The scheduled fetch

`gmail_fetch.py` is the deterministic half of `/gmail-sync` without an agent: it reads
the same Composio Gmail connection the manual flow uses and writes the payload `collect`
already consumed. `gmail-fetch.sh` chains fetch → `collect` → `sync-due`, and
`dev.fnc314.tracker-gmail-fetch` runs it every 12 hours.

Two rules make the cadence safe:

- **Paged fetches.** Composio answers a single request above ~50 full messages with
  *"The tool response payload is too large"*, so the fetcher pages 50 at a time up to
  `--max` (default 200).
- **Examined means processed.** `collect` records every message it looked at in
  `meta/sync.gmail.processedMessageIds`, not only the ones that produced a change.
  Otherwise the same unmatched newsletter would be re-staged every 12 hours. The
  reason it went unmatched stays in the review table.

`launchd` runs the missed interval on wake, so a closed laptop delays the fetch rather
than losing it. A failed fetch exits non-zero and stages nothing; the older payload is
never presented as new. Review remains interactive: the job writes `pending-proposals`
and never approves one.

Cloud Run was considered for this and rejected. It would require a runtime service
account with Firestore/GCS access, a Secret Manager entry and accessor for the Composio
credential, and a Scheduler service account — net-new credentials for a single-user tool
on the machine already running the job.

**Digest.** The collector cannot notify an LLM. Two surfaces instead: the TUI header gains
a badge (`AI JOB TRACKER | 84 Apps | 3 PROPOSALS`), and an optional daily digest email via
Composio.

## 11. Skills and documentation

### New skill

`.agents/skills/tracker/` — one skill, progressive disclosure via `references/`:

| file | contents |
|---|---|
| `SKILL.md` | trigger description, the command table, the golden rules, pointer table |
| `references/schema.md` | every collection and field, the enum registry, derived fields, the naming convention table from §3 |
| `references/payloads.md` | canonical JSON for `add`, `upsert`, `event`, `artifact`, `bulk`, with real examples |
| `references/events.md` | event taxonomy, legal status transitions, `derivedFrom` / `confidence` provenance rules |
| `references/proposals.md` | the collect → review → approve loop, what `confidence: low` means, how to reject a row |
| `references/automation.md` | schedules, the `sync-requests` dedupe rule, the Cloud Tasks equivalent, secret paths |
| `references/sheets.md` | view semantics, why it is never hand-edited |
| `references/migration.md` | CSV column → field map, `notesLegacy` / `event:imported` rules, the `export --csv` rollback |

Mirrored into `.claude/skills/tracker/`, `.junie/skills/tracker/`, and
`plugins/ai-job-search/skills/job-tracker/` (replacing the existing skill).
`tools/lint_skills.py` must pass on all four copies.

The golden rules stated in `SKILL.md`:

1. Never write `applications` from a collector.
2. Never write `notesRendered`.
3. Never hand-edit the Sheet.
4. Never invent a status transition — emit `event:status-corrected` instead.
5. Prefer a new event over editing an existing one.

### Scrub list — 36 files reference the deprecated system

| group | files |
|---|---|
| Root docs | `README.md`, `SETUP.md`, `AGENTS.md`, `CLAUDE.md`, `CODEX.md`, `GEMINI.md`, `OPENCODE.md`, `CHANGELOG.md` |
| Commands (`.claude`, `.junie`) | `apply`, `gmail-sync`, `html-report`, `interview`, `notion-sync`, `outcome`, `rank` |
| Skills (3 trees) | `job-scraper`, `upskill`, `job-tracker` |
| Scripts | `bulk_add_tracker.py` (delete), `cross_reference.py`, `export_tracker_json.py`, `generate_report.py`, `update_state.py` (delete) |
| Surfaces | `ui/terminal/tracker_tui.py`, `ui/web/application-dashboard-server.py`, `documents/README.md` |
| Guards / tests | `tools/security_guards.py`, `job_search_tracker.schema.json` → `tracker.schema.json`, `tests/test_tracker_tui.py`, `tests/test_export_tracker.py` |

### Regression guard

`tools/security_guards.py` gains a check that no module outside
`scripts/tracker.py` and `scripts/client.py` opens a CSV for writing, and that
`job_search_tracker.csv` appears in no `open(..., 'a'|'w')` call. The single-write-path
rule is enforced by CI rather than by convention.

## 12. CI

`.github/workflows/ci.yml`:

- `python-version: "3.12"` → **`"3.15"`** in all three jobs (`lint`, `security-guards`, `python-tests`).
- Add `requirements-tracker.txt` (pinned) and a `pip install -r` step to `python-tests`.
- New optional job `firestore-emulator`: installs `firebase-tools` + a JRE, runs
  `firebase emulators:exec --only firestore "python -m unittest tests.test_tracker_cli"`.
  Path-filtered to `scripts/tracker*.py`, `scripts/client.py`, `tracker.schema.json` so
  unrelated PRs don't pay for it.

`mise.toml` gains `[tools] python = "3.15.2"`.

Running the emulator suite locally (verified, 38 tests green):

```bash
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \
  firebase emulators:exec --only firestore --project job-search-tracker-test \
  ".venv/bin/python -m unittest tests.test_tracker_cli"
```

The suite skips itself when `FIRESTORE_EMULATOR_HOST` is unset, so the default
`unittest discover` run stays offline and credential-free. It forces
`TRACKER_PROJECT=job-search-tracker-test` at import time, so it cannot reach production.

## 13. Phases

| phase | work | exit criteria |
|---|---|---|
| **0** | Project, APIs, SA + key, bucket, rules, indexes, budget alert. **Firestore database created last.** | `tracker.py check` runs against an empty database |
| **1** | `tracker.schema.json` v4 (v3 → v4 = UUIDv7 identifiers); convention, enum, and contract tests | `tests/test_tracker_schema.py` + `tests/test_contract.py` green — **met 2026-10-31** |
| **2** | `scripts/{tracker,client,events,render}.py` + the read/write CLI (`check`, `seed`, `list`, `get`, `add`, `upsert`, `status`, `event`) | emulator-backed CLI tests green — **met 2026-10-31** (`tests/test_tracker_cli.py`, 39 tests) |
| **2b** | Remaining modules: `sheets.py`, `artifacts.py` (GCS upload + `sha256`), `cache.py`, `proposals.py`, `tasks.py`; `artifact`, `bulk`, `import`, `export`, `sheet`, `proposals`, `collect`, `sync-due` | emulator-backed tests for each; `export --csv` reproduces the current CSV byte-for-byte for the 84 rows — **met 2026-10-31** (`tests/test_tracker_modules.py`, 47 tests) |
| **3** | `import --csv` — idempotent, deterministic IDs, notes → `event:imported` + `notesLegacy` | reconciliation: 84 docs, 0 ID mismatches, every CSV cell accounted for — **met 2026-10-31** (84 apps, 84 events, 89 artifacts; re-run wrote nothing; `export --csv` byte-identical) |
| **4** | Readers: one projection (`scripts/reader.py`) shared by the TUI, `generate_report.py`, and `cross_reference.py`; provenance badge on both surfaces; `salary_lookup.py` reads its own `salary_data.json` and needed no change | `test_tracker_tui.py` passes with the CSV absent — **met 2026-10-31** (`tests/test_reader.py`, 24 tests; regenerated dashboard identical apart from the source line) |
| **5** | Writers: `bulk_add_tracker.py` → `tracker.py bulk`; `update_state.py` → `meta/sync` (via `collect`); `.recalc_queue` → `tracker.py score` / `event:fit-recalculated`; all commands emit JSON | no script opens the CSV for writing — **met 2026-10-31** (both scripts deleted, every command doc ported) |
| **6** | `pending-proposals`, the `collect` split, review loop, TUI badge | a staged proposal can be approved into events end-to-end — **met 2026-10-31** (fetch → `collect` → review; `proposals approve --only`; `tracker.py labels`; 4 emulator + 3 label tests) |
| **7** | Sheets view + `sync-sheets` on dashboard start and `/refresh` | Sheet matches Firestore row-for-row — **met 2026-10-31** (Applications 84 / Events 84 / Summary 13; `Sheet1` removed) |
| **8** | Artifact backfill to GCS with `sha256` dedupe | storage object count == artifact document count — **met 2026-10-31** (61 uploaded, 28 skipped by design; a re-run is idempotent) |
| **9a** | `sync-requests`, `sync-due`, `launchd` plist, dedupe rules | a write produces exactly one deferred collection run — **met 2026-10-31** (installed as `gui/$UID/dev.fnc314.tracker-collect`; `sync-due` refuses to consume a request without an inbox payload) |
| **9b** | Headless fetcher + 12-hour `launchd` fetch: `gmail_fetch.py`, `gmail-fetch.sh`, `dev.fnc314.tracker-gmail-fetch.plist` | an installed job exits 0 and a second run stages nothing new — **met 2026-10-31** (paged 50 at a time; installed run fetched 200 messages, staged 13 changes, `sync-due` clean; 13 fetcher tests) |
| **10** | Skills + pointer files, doc scrub, security guard, CSV → `archive/` | repo contains no reference to the CSV system outside the archive note — **met 2026-10-31** (canonical instructions in `.agents/`; pointer files only; `archive/*.csv` guarded) |

**Rollback at every phase.** The importer only reads the CSV. `tracker.py export --csv`
regenerates the original file. The CSV is never modified in place until Phase 10.

## 14. Risks

| risk | mitigation |
|---|---|
| **Firestore location is irreversible** | `nam5` is chosen precisely because it contains `us-east4`; database creation is the last step of Phase 0, after schema review and a dry-run reconciliation |
| Migrated notes are lossy | every derived event carries `derivedFrom: notes-regex` and `confidence`; the raw text is preserved verbatim in `notesLegacy` and in the `event:imported` note |
| Migration writes corrupt history | the importer is dry-run-first, ID-deterministic, and idempotent; production write happens only after the reconciliation report is reviewed |
| Python 3.15 is five days old | all binary dependencies verified to ship cp315 wheels; `grpcio` is the only real risk and it has a `macosx_10_15_universal2` wheel. Fallback is a one-line pin to 3.14.10 |
| Composio cannot manage Firestore/GCS/schema | it was never intended to; the service account covers all of it. Composio is used only for Gmail |
| A future agent bypasses the CLI | `security_guards.py` fails CI on any new CSV write path; `SKILL.md` states the invariant |
| Cloud secrets sprawl | no cloud secrets in the default path; the collector runs under `launchd` with the local key |
| `mise.toml` held a plaintext `GOOGLE_API_KEY` | **closed 2026-10-31:** moved to `.env.local` (gitignored), which mise loads via `[env] _.file`; `.env.example` documents the variables. The leaked key was **revoked in Google Cloud** and replaced in project `job-search-fnc314-dev`. History was rewritten with `git-filter-repo --replace-text` and verified: no `AIza` string exists in any blob or commit. |

## 15. Decision log

| # | decision | alternatives rejected |
|---|---|---|
| 1 | Firestore canonical + Sheets as a regenerated view | Sheets-only (no queries, no history); JSON-only (no durability) |
| 2 | Single Python CLI as the only write path | Cloud Functions (deploy overhead for one user); direct agent SDK calls (no guardrails); hand-editing the Sheet (no validation) |
| 3 | Append-only `events` subcollection; notes deprecated | notes-string-only (unqueryable, regex-dependent) |
| 4 | lowerPascalCase keys, namespaced kebab enum values | snake_case (banned); leading-capital keys (banned) |
| 5 | `nam5` multi-region | `us-east4` regional (same latency, weaker SLA); `us-central1` (further from Pittsburgh) |
| 6 | Plain GCS bucket over Firebase Storage | Firebase Storage (a client-rules layer that is unused when rules deny everyone) |
| 7 | `launchd` default, Cloud Run documented | Cloud Run default (puts a Composio API key in Secret Manager for no benefit) |
| 8 | Python 3.15.2 | 3.12 (CI baseline, two versions stale); 3.14.10 (safe fallback if a wheel breaks) |
| 9 | Collector/reviewer split for Gmail | attempting to cron the whole `/gmail-sync` (impossible — it requires an approver) |
| 10 | **All generated IDs are UUIDv7** (`contract.newUuid()`), replacing ULID | ULID (two identifier formats for one concept; UUIDv7 is stdlib, monotonic, and universally understood); UUIDv4 (random — destroys `events` ordering); database-generated IDs (requires a round trip, so IDs fail offline) |
| 11 | **No `namespace:UUID` prefix** on generated IDs | `event:<uuid>` / `artifact:<uuid>` prefixes: every generated ID already sits in a typed field of a typed document and never shares an ID space with another kind, so a prefix would add a second pattern, a parse rule, and a Shell/CSV concern for zero query benefit. Revisit only if IDs start travelling untyped (e.g. a generic audit feed). |
| 12 | Schema `v4` before any data existed | editing v3 in place: the contract version must state the shape, and identifier format is shape. v4 is declared *pre-data*, so it required no migration. |
| 13 | Unknown legacy `channel` labels → `channel:other`, original in `metadata.legacyChannel` | dropping the cell (erases a recorded fact); inferring a channel from the company (invents one); adding free-text channel values (breaks the closed enum) |
| 14 | **`event:imported` is dated at submission**, not at migration time | dating it at import time: every one of the 84 applications would show "activity today", so `daysSinceActivity` and the TUI's staleness view would be useless. The event means "this is where the imported history begins", and submission is the only date the CSV actually supports. |
| 15 | `collect` reads a JSON inbox drop instead of calling a mail API | fetching inside `collect`: puts a Composio credential in the cron path, makes the deterministic stage untestable, and cannot hold the approver gate. The drop is an interface; the fetcher can change without touching staging. |

## 16. Scope changes during execution

A running record of decisions that moved the approved scope. Each entry states what
changed, why it is not merely cosmetic, and which documents were revised with it. New
entries are appended at the end of the phase that produced them.

| date | phase | change | scope impact | documents updated |
|---|---|---|---|---|
| 2026-10-31 | 0 | Firestore database created at `nam5` | closes the only irreversible step; the plan's "database last" ordering was honoured | plan §5, §15; migration §6 |
| 2026-10-31 | 1 | Schema `v3` + contract tests | establishes the enum registry as the single source of truth | plan §6, §13 |
| 2026-10-31 | 1→2 | **Artifact paths are polymorphic**: 2 of 47 `cv_file` values are Google Docs URLs | the import map could not be "path → `localPath`"; it became a four-case rule (`localPath`, `externalUrl`, `driveUrl`, missing-on-disk) with an explicit contract test. Without this the Phase 3 import would have produced two artifact records pointing at files that can never exist. | plan §6.4; migration §3; `tracker.schema.json` → `x-legacy-csv.artifactPathRule` |
| 2026-10-31 | 2 | **Identifiers switched from ULID to local UUIDv7** (schema `v4`) | touches every generated ID: `eventId`, `artifactId`, `proposalId`, `requestId`. Does **not** touch `applications`/`companies`/`contacts` IDs, which remain derived kebab slugs, so the CSV import map and the Phase 3 "byte-identical ID" check are unaffected. No documents existed under v3, so no data migration was required, and `meta/schema` was re-seeded at v4. | plan §3, §6.1–§6.4, §9, §15; migration §3, §7; `tracker.schema.json` (`uuidV7`, `x-revision-log`), `scripts/contract.py`, `scripts/events.py`, `scripts/tracker.py`, both schema test modules |
| 2026-10-31 | 2 | **No `namespace:UUID` prefix** on generated IDs | keeps document IDs a single shape (slugs for derived, bare UUIDs for generated) so the export path and the Sheets view need no de-prefixing. Rationale in decision 11. | plan §3, §15 |
| 2026-10-31 | 2 | Phase 2 split into **2 (core CLI)** and **2b (remaining modules)** | the CLI read/write path is provably correct against the emulator today; Sheets, GCS upload, cache, proposals, and the task queue are separable and do not block the Phase 3 dry run. This keeps the import gate from waiting on non-essential surface area. | plan §13 |
| 2026-10-31 | 2b | **Artifact census corrected**: 45 of 89 cells are absolute Google Drive mount paths, 40 are repo-relative, 4 are Google Docs URLs | the approved plan and migration record said "45 local paths, 2 Google Docs URLs". That was wrong: only 20 CVs and 20 cover letters are repo-relative. Without the correction the Phase 3 checklist would have demanded 45 files that do not exist in the repo and would have marked a correct import as failing. The CSV is unchanged; only the expectation moves. | plan §6.4; migration §3, §4; `tracker.schema.json` → `x-legacy-csv.artifactPathRule` (rule already allowed absolute paths) |
| 2026-10-31 | 2b | **Notes census corrected**: 46 rows are exactly the boilerplate marker, 50 contain it, 34 distinct | the plan said "50 share one boilerplate string". A containment count was written down as an equality count, so a future audit would have looked for 4 rows that do not exist. No mapping changes. | migration §2, §3; `tracker.schema.json` → `x-legacy-csv.notesRule.description` |
| 2026-10-31 | 2b | **Free-text `channel` gets an explicit mapping rule**: known ATS word → that `channel:*`; unknown → `channel:other`; original kept in `metadata.legacyChannel` | two legacy labels (`Workday (Snap careers)`, `MLB careers site`) are not enum values. Dropping them would erase a recorded fact; guessing a channel from a company name would invent one. `channel:other` is the schema's explicit catch-all, and preserving the label keeps the import reversible. | plan §16; migration §4; `scripts/importer.py` |
| 2026-10-31 | 2b | **Import idempotency is keyed on `event:imported`, probed per candidate subcollection** | a collection-group query on `events.type` needs a new collection-group index; probing only the 84 candidate IDs uses the automatic subcollection index and is cheaper. Document-exists alone is *not* the key, so a hand-created application with a colliding ID is never skipped as "already imported". | `scripts/tracker.py` |
| 2026-10-31 | 2b | **`collect` consumes a JSON inbox drop; fetching stays delegated** | the plan implies `collect` fetches Gmail. A cron job cannot hold a Composio session, and the deterministic stage must be testable without that credential. `collect --json` (default `generated/gmail-inbox.json`) does matching, classification, and staging deterministically; the `/gmail-sync` flow writes the drop. If the drop is absent, `sync-due` completes the request and reports `staged: false` rather than failing forever. | plan §10; §9; `scripts/proposals.py` |
| 2026-10-31 | 3 | **The import ran and is the acceptance evidence**: 84 apps / 84 events / 89 artifacts; re-run wrote nothing; `export --csv` byte-identical | the exit gate called for a reviewed dry run before writing. The dry run was reviewed and the real import reproduced it exactly, so the gate is satisfied by measurement rather than by assertion. Nothing was corrected during the write, which is what makes the byte-identity meaningful. | migration §4; plan progress table |
| 2026-10-31 | 3→5 | **The migration period rule is restated: the CSV is the write target until Phase 5, and Firestore is a dated snapshot, not a live mirror** | the original wording ("Firestore is a mirror under construction") implied the mirror tracked the CSV. It does not: nothing propagates a CSV edit, so the divergence window opened the moment the import committed. Leaving that implicit would have let a `/apply` silently make the store stale with no warning. Re-import is the documented remedy, and its known limit is stated: a changed cell on an already-imported row is skipped, because corrections belong in the event log, not in a document rewrite. | migration §1 |
| 2026-10-31 | 4 | **Consumers keep the legacy row shape**: `scripts/reader.py` projects the store back to the CSV's 14 columns, and both consumers switch in one line | rewriting the TUI's and the dashboard generator's rendering to a new data model would have been a large, unreviewable change for zero user-visible gain. Projecting instead made the port verifiable: the regenerated `ui/web/application-dashboard.html` is byte-identical to the committed file except for the added provenance line. The reader reports its source and cache age, so "which store answered" is never implicit. | plan §13; migration §6; `scripts/reader.py` |
| 2026-10-31 | 4 | **Readers detect CSV/store divergence and fall back to the CSV** | porting the readers at Phase 4 while the writers stay on the CSV until Phase 5 leaves a gap: a `/apply` adds a CSV row the store has never seen, and a store-only reader would make that application vanish from both UIs with no warning. Comparing IDs on load is cheap at 84 rows and self-healing; the badge says `csv (store behind)` rather than presenting a stale store as live. | plan §13; migration §1; `scripts/reader.py` |
| 2026-10-31 | 4 | **The dashboard generator honours `TRACKER_DASHBOARD_OUTPUT`, and the server test sets it** | once `/refresh` reads the store instead of the CSV, the existing test regenerated the tracked `ui/web/application-dashboard.html` under the throwaway test project, so a test run left a cache-backed file in the working tree. Redirecting output keeps the test honest and the repo clean; it also caught that the refresh test's 5-second timeout was too short for a network read. | `scripts/generate_report.py`; `tests/test_dashboard_server.py` |
| 2026-10-31 | 5 | **New `score` subcommand** (`--fit 0-100`), emitting `event:fit-recalculated` and storing the previous rating in `metadata.previousFitRating` | `.recalc_queue` was a raw CSV-row dump, so its consumer had to re-derive the application from free text; a fit change also had no history, which made a recalculation indistinguishable from an overwrite. Making it a command gives the write one validation path and the change an auditable event. The rating stays a direct field because it is an input to the record, not a projection of the log. | plan §12; `.claude/commands/apply.md` |
| 2026-10-31 | 5 | **The TUI queues a structured request; it does not write** | the TUI cannot compute a score (that is the evaluation rubric), and it must not hold write credentials. It now writes `{schemaVersion, kind: "fit-recalculation", requestedAt, applicationId, company, role, currentFitRating}` to `.recalc_queue`, which a resolve step turns into `tracker.py score`. Keeps the split the collector already uses: stage, then let a write path commit. | `ui/terminal/tracker_tui.py` |
| 2026-10-31 | 5 | **Read-side command docs were ported in Phase 5, pulled forward from Phase 10** | Phase 5 moved the writers, so leaving `/rank`, `/interview`, `/notion-sync`, `/upskill`, `/html-report`, and `/scrape` reading the CSV would have frozen their view at the 2026-10-31 snapshot the moment the first post-migration write landed. That is a silent correctness bug, not a cosmetic scrub, so the read references moved with the writers. Phase 10 still owns the prose cleanup and the CSV's removal. | plan §12; `docs/tracker-migration.md` §1 |
| 2026-10-31 | 5 | **The write-gate message no longer says the CSV is authoritative** | with the writers ported, the old message described a world that no longer exists and would have told a future maintainer to keep writing the CSV. The gate itself is kept: writes stay opt-in so an accidental or agent-driven mutation cannot silently rewrite an append-only history. | `scripts/client.py` |
| 2026-10-31 | env | **Secrets moved to `.env.local`, loaded by mise; the write gate is now explicit in the environment** | `mise.toml` is tracked and held a live `GOOGLE_API_KEY`, so the file could not be committed safely. `.env.local` is gitignored and holds the key plus `TRACKER_ALLOW_WRITES=1`; `[env] _.file` keeps a single loading mechanism instead of a second dotenv layer. Removing the key from HEAD does not remove it from history, so it was rotated and revoked, and history was rewritten with `git-filter-repo`; the open item is now closed, not deferred. | `mise.toml`; `.env.example`; plan §14 |
| 2026-10-31 | 7 | **The Sheet was located by the service account, not supplied by hand; the Drive API had to be enabled for that** | `TRACKER_SHEET_ID` was never recorded, and the Sheets scope cannot list files. Rather than ask the user to copy an id, the SA listed its visible spreadsheets with `drive.metadata.readonly`; that required enabling `drive.googleapis.com` on the project, which was done and is reversible. The found file (`Job Tracker`) already had the service account as Editor, so no sharing change was needed. The default empty `Sheet1` tab was deleted so the view contains only the three generated tabs. | plan §7; `.env.local` |
| 2026-10-31 | 8 | **Backfill updates existing artifacts instead of registering new ones, and skips `.gdoc` stubs** | re-registering would have created a second artifact per file and, worse, a fake `event:artifact-added` in an append-only history for something that was already there. In-place merge keeps the imported provenance (`source: imported`) and just adds the content facts. A `.gdoc` is a shortcut to a Google Doc, so uploading its bytes would store a pointer and lose the document; the 24 stubs and 4 `externalUrl` records therefore stay blob-less by design. `ensureUpload` makes the run idempotent and refuses to overwrite a blob whose recorded hash differs, so a silently replaced file is an error rather than a lost submission. | `scripts/artifacts.py`; `scripts/tracker.py` |
| 2026-10-31 | 9a | **`sync-due` will not consume a request when no inbox payload exists** | the first cron design claimed a due request and called `collect`, which returns "no inbox payload" when the managed fetch has not run. Completing on that result would mark the 2-hour backlog as handled while nothing was read. It now checks the payload first and leaves the request pending, attempts untouched, so a late fetch still gets its run. **Scope note:** `collect` does not fetch Gmail — fetching stays with the agentic `/gmail-sync` step — so the backlog only advances after a fetch writes `generated/gmail-inbox.json`. Wiring a headless fetcher is a separate decision, recorded for approval rather than assumed. | `scripts/tracker.py`; `deploy/launchd/` |
| 2026-10-31 | 6 | **Gmail scopes are stored, not hard-coded** | the default fetch covers Primary, Updates, `JOBS`, and `JOBS/*`. Gmail has no sub-label wildcard, so the only way to expand `JOBS/*` is to know the labels — and a hard-coded list would silently stop seeing a label the user adds later. `collect --labels` stores the inventory in `meta/sync.gmail.labels`; `tracker.py labels` reads it; `/gmail-sync --label` enumerates against it and refuses an unknown value instead of guessing. Freeform was the fallback, but enumeration was cheap once the inventory had a home. | `.claude/commands/gmail-sync.md`; `scripts/tracker.py` |
| 2026-10-31 | 8 | **Backfill never rewrites a differing blob** | an artifact whose local bytes no longer match its recorded `sha256` is an error, not a silent re-upload. Re-uploading would let an edited file quietly replace the copy that matched an application, and the record would still claim the old hash. `ensureUpload` is idempotent on matching hashes and raises on a mismatch, so backfill is safe to re-run. `.gdoc` shortcuts and `externalUrl` records are skipped by design — a Google Docs URL is not a file to copy into a bucket. | `scripts/artifacts.py`; `scripts/tracker.py` |
| 2026-10-31 | 10 | **One shared instruction tree, one pointer per agent** | mirroring commands and skills into `.claude`, `.junie`, `.gemini`, `.opencode`, `.qwen`, and `plugins/` produced three divergent copies of the tracker instructions within a single migration. The fix is structural: `.agents/` holds the canonical skills, `.claude/commands/` holds the one implementation of the workflows, and each agent directory contains a single `<AGENT_NAME>.md` that points there. Copies cannot drift because they no longer exist. | `.agents/README.md`; `.agents/skills/job-tracker/SKILL.md`; `.<agent>/<AGENT_NAME>.md` |
| 2026-10-31 | 10 | **The CSV moves to `archive/`, not to a tracked `data/` folder** | deleting it would discard the only pre-migration artifact; keeping it at the repo root leaves a file that every reader and doc can accidentally treat as live. `archive/` plus an `archive/README.md` states what the files are, and `.gitignore`/`tools/security_guards.py` guard `archive/*.csv` so personal data still never enters git. The retired CSV schema and exporter moved with it rather than being deleted, because they document what the old cells meant. | `archive/`; `.gitignore`; `tools/security_guards.py` |
| 2026-10-31 | 6 | **`collect` filters against `meta/sync`, not a local state file** | the pre-migration `gmail_sync/state.json` was per-machine and would have been lost on a new laptop. Processed message ids now live beside the collector watermark in `meta/sync`, so a re-fetch on any machine is idempotent. | `scripts/tracker.py`; `scripts/proposals.py` |
| 2026-10-31 | 6 | **Selective review is `proposals approve --only 1,3`** | the original approval applied a whole proposal, which forced an all-or-nothing reply and contradicted the review UX. The flag filters changes before any event is prepared; unselected rows are reported in `skipped`. The proposal still has one resolution, so no new schema field was needed. | `scripts/proposals.py`; `scripts/tracker.py` |
| 2026-10-31 | 6 | **`collect` and `sync-due` are behind the write gate too** | they write `pending-proposals` and `meta/sync`, which are not application history but are still record-adjacent. Applying the same opt-in keeps one rule for every writer; the launchd plist sets it explicitly. `collect --dry-run` and `labels` stay read-only. | `scripts/tracker.py` |
| 2026-10-31 | 9a | **The collector runs under `launchd`, not Cloud Run/Scheduler** | the machine is already on and holds the service-account key, so a user agent adds no new credential surface or cost. The plist reads `TRACKER_SA_KEY` from disk and sets only the write opt-in, so no secret is copied into a tracked file. Cloud Run stays documented as the optional 9b path and is undeployed. | `deploy/launchd/dev.fnc314.tracker-collect.plist` |
| 2026-10-31 | 9b | **The scheduled fetch is a second `launchd` agent, not Cloud Run** | 9a left the 2-hour backlog with no way to advance on its own, because `collect` never fetches. A 12-hour agent closing that loop needs a headless fetcher and nothing else — no new credentials beyond the two already on disk (tracker SA, Composio login). Cloud Run would add a runtime SA, a Secret Manager entry/accessor, and a Scheduler SA for a job that runs on the laptop anyway. | `deploy/launchd/dev.fnc314.tracker-gmail-fetch.plist`; `scripts/gmail_fetch.py`; `scripts/gmail-fetch.sh`; plan §10 |
| 2026-10-31 | 9b | **`collect` marks every examined message processed, not only matched ones** | a 12-hour run makes "unmatched" repeat forever: the same newsletters and low-signal alerts would be re-examined and re-listed on every run because only changes were recorded. Recording the whole examined batch makes the run idempotent; the reason a message produced no change is still in the review table, so nothing is hidden. | `scripts/tracker.py`; `tests/test_tracker_modules.py` |
| 2026-10-31 | 9b | **Fetches page at 50 messages** | Composio rejects a single response far above 50 full messages ("The tool response payload is too large"), which made the installed job fail on its first run. Paging at 50 and stopping at `--max` keeps every request under the cap while still covering a 30-day backfill. | `scripts/gmail_fetch.py`; `tests/test_gmail_fetch.py` |
| 2026-10-31 | 4 | **`event:imported` no longer decorates its note with `date · type — `**; the 84 live documents were reprojected | the renderer treated the import marker as an activity, so every restored record's `notesRendered` became `2026-10-30 · event:imported — <prose>`. That is noise on 84 of 84 records and it made the dashboard differ from the pre-migration one for no informational gain. `notesRendered` is a derived field by contract, so regenerating it is a projection refresh, not a data edit: no application, event, or artifact document changed, and notes now equal `notesLegacy` exactly until a real post-import event appends a line. | `scripts/render.py`; migration §6 |

### What a scope change must not do

- It must not weaken a reconciliation invariant. The UUIDv7 switch passed this test: IDs
  the CSV import depends on are unchanged.
- It must not be silent. If an implementation needs to deviate from this plan, the plan is
  corrected in the same change, with a row here.
- It must not defer a durability decision. Anything affecting the only copy of the record
  (location, credentials, write gating) is decided before the phase that depends on it.
