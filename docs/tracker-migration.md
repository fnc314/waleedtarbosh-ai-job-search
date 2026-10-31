# Tracker Migration Record

**Purpose:** the operational record for moving `job_search_tracker.csv` into Firestore.
Covers the import map, the reconciliation checklist, and the cutover rules — including
what the CSV is still good for now that Firestore is authoritative.

Companion documents: `docs/tracker-migration-plan.md` (design and rationale),
`tracker.schema.json` (the contract).

**Last updated:** 2026-10-31 (after Phase 10: the CSV moved to `archive/`, instructions consolidated under `.agents/`, and the CSV-era docs are scrubbed)
**Update rule:** revised at the end of every phase, including when an implementation decision
changes the approved scope (see `docs/tracker-migration-plan.md` §16).

---

## 1. The rule that governs the record

> **Firestore is the write target from Phase 5 onward.**
> `job_search_tracker.csv` is a frozen snapshot of the pre-migration record: it is read as
> a fallback, never written.

**Phase 5 has landed (2026-10-31).** Every writer now goes through `scripts/tracker.py`:
`/apply` records fit changes with `score`, `/outcome` records status and activity as events,
`/gmail-sync` writes events instead of CSV cells, `/rank`, `/interview`, `/notion-sync`,
`/upskill`, `/html-report`, and `/scrape` read the store, the TUI's recalc key queues a
structured request, and `bulk_add_tracker.py` and `update_state.py` are deleted. **No
script or command opens the CSV for writing any more.**

The store still holds exactly what the Phase 3 import put there — 84 applications, 84
`event:imported` events, 89 artifacts — plus anything written since. `export --csv` still
reproduces the original file byte-for-byte from the store, so the rollback path outlives
the CSV itself: the file now sits in `archive/` as history, not as an input.

Concretely, from now on:

| you want to… | do this | NOT this |
|---|---|---|
| Add an application | `tracker.py add --json …`, or let `/outcome` do it | editing `job_search_tracker.csv` |
| Record a status change | `tracker.py status <id> <status>`, or `/outcome` | editing the CSV's `status` cell |
| Record a fit change | `tracker.py score <id> --fit N`, or the TUI's `r` request | editing the CSV's `fit_rating` cell |
| Record an activity (follow-up, acknowledgement) | `tracker.py event <id> --type …` | appending prose to a notes column |
| Inspect the record | `tracker.py list` / `get` / `export` | parsing the CSV |

`tracker.py` commands that **write** (`add`, `upsert`, `status`, `score`, `event`,
`artifact`, `bulk`, `import`, proposal `approve`) refuse to run unless
`TRACKER_ALLOW_WRITES=1` is set. The gate is now a guard against accidental mutation, not a
migration switch: writes are opt-in so a stray agent run cannot rewrite history.
Read-only commands (`check`, `list`, `get`, `export`, `sheet --local-only`) always work, and
`--dry-run` resolves and validates the full document without writing, even when the gate is
off.

The gate is enforced in one function (`scripts/client.py: assertWritesAllowed`) and covered
by tests, so it cannot be bypassed by a subcommand that forgets to check.

### If something still writes the CSV

Treat it as a bug, not a workflow. The import can absorb it — it is idempotent, keyed on
`event:imported` — but it only picks up **new** rows:

```bash
TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py import --csv job_search_tracker.csv
```

A **changed** cell on an already-imported row (a corrected status, an edited note) is **not**
picked up, because the document is skipped. Corrections belong in the event log
(`tracker.py status --correction` / `event`), which appends instead of rewriting history.

**The readers guard against a stale store for you.** `scripts/reader.py` compares the CSV's
IDs with the store's on every load, and when the CSV holds a row the store does not, it
serves the CSV and labels itself `csv (store behind)` instead of hiding the new application.
That fallback is now only reachable if a writer is reintroduced. Force a source with
`TRACKER_SOURCE=store|cache|csv`.

## 2. What the CSV actually contains

Profiled from the live file on 2026-10-31, not assumed. This is the expectation the Phase 3
reconciliation is measured against.

| column | populated | distinct | note |
|---|---:|---:|---|
| `id` | 84 / 84 | 84 | no duplicates, none empty |
| `date` | 84 | 42 | ISO `YYYY-MM-DD` |
| `company` | 84 | 75 | verbatim display names |
| `sector` | **3** | 2 | nearly unpopulated |
| `role` | **81** | 58 | 3 legacy rows have no title — do not invent one |
| `role_type` | **0** | 0 | entirely unused |
| `channel` | **2** | 2 | nearly unpopulated |
| `status` | 84 | 4 | `applied` 58, `rejected` 21, `interview` 4, `drafting` 1 |
| `contact_person` | **1** | 1 | |
| `fit_rating` | 84 | 17 | integer 68–98, none empty |
| `notes` | 84 | **34** | 46 are exactly the boilerplate string; 50 contain it |
| `cv_file` | 47 | 45 | polymorphic path — see §3 |
| `cover_letter_file` | 42 | 40 | polymorphic path — see §3 |
| `source` | 16 | 16 | posting URLs |

The near-empty columns (`role_type`, `channel`, `sector`, `contact_person`) are the reason
the new schema keeps them nullable. They are populated *going forward*, not backfilled with
invented values — the same discipline the original contract stated: *"never invented to
fill the cell."*

## 3. Import map

Authoritative copy lives in `tracker.schema.json` → `x-legacy-csv`. Summary:

| CSV | Firestore |
|---|---|
| `id` | `applications/{id}` document ID, and `id` — **unchanged derived slug** |
| `date` | `date` |
| `company` | `company`, plus derived `companyId` = slug |
| `sector` | `sector` |
| `role` | `role` |
| `role_type` | `roleType` |
| `channel` | `channel`, normalized to `channel:*` |
| `status` | `status`, normalized to `status:*` via `statusMap` |
| `contact_person` | `contactPerson` |
| `fit_rating` | `fitRating` (int) |
| `notes` | `notesLegacy` (verbatim) + one `event:imported` |
| `cv_file` | `artifacts/{artifact:cv}` — prefix-dependent, see below |
| `cover_letter_file` | `artifacts/{artifact:cover-letter}` — prefix-dependent |
| `source` | `source` |

Application IDs stay **derived** (`slug(company)--slug(role)`), which is what makes the
Phase 3 check "every imported ID is byte-identical to the CSV `id`" meaningful. Artifact and
event IDs are **generated** UUIDv7s, since the CSV has no equivalent column for them.

### Artifact paths are polymorphic

This is the one place a naive importer silently corrupts data. Three forms exist, and 4 of
the 89 populated cells are Google Docs URLs rather than paths.

| observed form | maps to | CV | cover letter |
|---|---|---:|---:|
| `documents/applications/…/main_*.pdf` | `artifacts[].localPath` | 20 | 20 |
| `https://docs.google.com/document/…` | `artifacts[].externalUrl` | **2** | **2** |
| absolute path under a Google Drive mount (`/Users/…/CloudStorage/GoogleDrive-…`) | `artifacts[].driveUrl` | **25** | **20** |
| empty | no artifact record | 37 | 42 |

This is the one place a naive importer silently corrupts data. The historical record is
**mostly not repo-relative**: 45 of 89 artifacts are absolute Google Drive mount paths
that will not resolve on another machine, and 4 (2 CVs, 2 cover letters) are Google Docs
URLs. Only 40 are repo-relative files.

Paths that point at a repo-relative file which no longer exists still produce an artifact
record with `localPath` set — the reference is preserved and the gap is listed in the
reconciliation report. Dropping it would erase the fact that a document was submitted.

**Unfixable by design:** the 4 Google Docs links cannot be resolved to uploaded artifacts,
because the underlying documents are external to this repo. They become `externalUrl` and
are never uploaded to the bucket. The 45 absolute Drive-mount paths likewise become
`driveUrl`; they are usable from this machine while the Drive mount exists and are not
uploaded unless a file is still readable when `artifact --path` runs.

### Notes must not be treated as evidence

46 of 84 rows are exactly the string `Added via historical gmail sync`; 50 contain it.
That is a provenance marker, not a status signal. The importer:

- preserves the exact text in `notesLegacy`,
- emits one `event:imported` per row carrying that text,
- **may optionally** extract dated fragments into additional events, each marked
  `derivedFrom: "notes-regex"` and `confidence: "low"`,
- never converts boilerplate into a status transition.

Regex-derived events are additive and advisory. `notesLegacy` remains the record of truth
for pre-migration history, so a wrong guess is revisable without losing the original.

## 4. Reconciliation checklist (Phase 3 exit gate) — **passed 2026-10-31**

Verified against the live store after the real import, not just the dry run:

- [x] 84 documents, 84 unique IDs, 0 duplicates.
- [x] Every row's `id` is byte-identical to the CSV `id` (the slug scheme is unchanged, so
      any mismatch is a bug in the importer, not in the data).
- [x] Every `status` maps through `statusMap`; no row falls through to a default —
      `applied` 58, `rejected` 21, `interview` 4, `drafting` 1.
- [x] Every `fit_rating` is an integer in 0–100; none become `null`.
- [x] `notesLegacy` is byte-identical to the CSV `notes` cell for all 84 rows.
- [x] Exactly 1 `event:imported` per record (84 total); 0 events of any other type from the
      mandatory pass. Every `eventId` validated as UUIDv7.
- [x] 47 `artifact:cv` and 42 `artifact:cover-letter` records: 20 + 20 `localPath`,
      25 + 20 `driveUrl`, 2 + 2 `externalUrl`. Every `artifactId` validated as UUIDv7.
- [x] The 3 rows with an empty `role` produce `role: null` (not `""`, not a placeholder).
- [x] The near-empty columns stay `null`, not `""`. The two non-empty `channel` labels are
      mapped explicitly (`Workday (Snap careers)` → `channel:workday`,
      `MLB careers site` → `channel:other`) and the original text is kept in
      `metadata.legacyChannel`.
- [x] 0 rows produce an invented `status`, `date`, or `company`.
- [x] Warnings, not errors: 45 absolute Drive paths, 46 boilerplate-only notes, 3 rows with
      no role, 2 channel mappings — 96 non-blocking warnings, 0 blocking issues.

**Idempotency:** a second `import` prepared 0 and skipped 84, writing nothing.
**Rollback:** `export --csv` produced a file with the same SHA-256 as the live CSV
(`3367ce42…f6ab`, 26,837 bytes) — byte-identical, including CRLF endings and row order.

The reviewed report lives at **`docs/import-reconciliation.md`**; regenerate it with
`tracker.py import --csv job_search_tracker.csv --dry-run --report docs/import-reconciliation.md`.

## 5. Rollback

Any phase, at any time:

```bash
# Regenerate the exact CSV shape from Firestore
.venv/bin/python scripts/tracker.py export --csv -o /tmp/restored.csv

# Compare against the live file
diff <(sort job_search_tracker.csv) <(sort /tmp/restored.csv)
```

The emulator suite asserts a stronger version than `diff <(sort …)`: it requires the output
to be byte-identical to the live file, including row order. That is why the importer stores
`metadata.importIndex` — Firestore has no row order, and a rollback that reorders the file
would be a silent change.

The importer opened the CSV read-only, and the file was never modified in place. As of
Phase 10 it lives at `archive/job_search_tracker.csv` as the historical record; nothing
reads it as a source any more.

If Firestore has to be abandoned (wrong region, wrong project), the sequence is: export to
CSV (`tracker.py export --csv`), delete the project, recreate, re-import. This stays cheap
because the export path is generated from the store, not rescued from the archive copy.

## 6. Phase-by-phase update instructions

What changes for you and for agents at each step.

| phase | what you should do | what agents should do |
|---|---|---|
| **0–1** *(done)* | nothing changes; keep using `/apply`, `/outcome`, `/gmail-sync` | keep writing the CSV; `tracker.py check` and `import --dry-run` only |
| **2** — CLI exists *(done)* | still nothing; writes are gated | use `tracker.py` for **reads** (`list`, `get`, `export`) to sanity-check the store; `add/upsert/status/event/artifact --dry-run` are safe because they cannot write |
| **2b** — import/export/views *(done)* | review the reconciliation report at `docs/import-reconciliation.md` | `import --dry-run` writes that report; `export --csv` is verified byte-identical to the live file in the emulator; `sheet --local-only` previews the view |
| **3** — import *(done)* | treat Firestore as a **snapshot taken 2026-10-31**, not a live mirror; keep writing the CSV | import verified: 84 applications, 84 `event:imported`, 89 artifacts (40 `localPath`, 45 `driveUrl`, 4 `externalUrl`); re-run skips all 84; `export --csv` byte-identical |
| **4** — readers ported *(done)* | the TUI header and the dashboard header now show their source (`store (live)`, `cache (…old)`, or `csv (pre-migration)`); set `TRACKER_SOURCE=csv` to force the old behaviour | both consumers read through `scripts/reader.py`; a read failure degrades to cache, then CSV, and the badge says which |
| **5** — writers ported *(done)* | everything writes through `tracker.py`; the CSV is a read-only fallback | `/apply` → `score`; `/outcome` → `status`/`event`; `/gmail-sync` → events; TUI `r` → structured recalc request; `bulk_add_tracker.py` and `update_state.py` deleted |
| **env** *(done)* | secrets are now in `.env.local`; `TRACKER_ALLOW_WRITES=1` is set there, so `tracker.py` writes work from a shell in this repo | never put a secret in `mise.toml`; copy `.env.example` for a new machine. The key once committed to `mise.toml` was revoked and replaced, and history was rewritten so it appears in no commit |
| **6** — collector split *(done)* | `/gmail-sync` fetches into a payload and stages a proposal; review the queue at your convenience | `collect` stages proposals only; `proposals approve` emits events; `sync-due` is the cron entrypoint; `--label` validates against the last-seen inventory |
| **7** — Sheets *(done)* | the Sheet is regenerated, never edited | `tracker.py sheet --sync` replaces all three tabs wholesale; never hand-edit the Sheet — fix the record and re-sync |
| **8** — artifacts *(done)* | nothing to do; local files stay where they are and the Drive sync stays manual | `tracker.py backfill` added hashes and `storage` to 61 artifacts; `.gdoc` stubs and `externalUrl` records have no blob by design; new files upload through `artifact --path …` |
| **9a** — automation *(done)* | a `launchd` agent runs `tracker.py sync-due` every 15 min; a write queues one deferred run | do not enqueue duplicate sync requests; `sync-due` is a no-op until `generated/gmail-inbox.json` exists |
| **10** — cutover *(done)* | the CSV is gone from the working tree; `archive/README.md` explains what the frozen files are | instructions live once in `.agents/` (plus `.claude/commands/`); agent directories hold one pointer file each; CI guards `archive/*.csv` |
| **9b** — scheduled fetch *(done)* | Gmail is read every 12 hours without you: a `launchd` agent fetches, stages a proposal, and runs due syncs | review `tracker.py proposals list` when convenient; the job never approves anything. It pages 50 messages at a time and treats "examined" as "processed", so an unremarkable mailbox stays quiet |

### The one irreversible moment

Creating the Firestore database fixes its location permanently. That happened on
2026-10-31 with `nam5` (US multi-region). Everything else in this migration is reversible.

## 7. Contract revisions during the migration

The record is not yet imported, so the contract is still allowed to move. Each move is
recorded here, with its effect on the import.

| version | date | change | effect on the import |
|---|---|---|---|
| 3 | 2026-10-31 | initial contract: typed core, append-only events, namespaced enums | none — design only |
| 4 | 2026-10-31 | generated identifiers are **UUIDv7** instead of ULID | **none.** Nothing had been written under v3 apart from `meta/schema`, which is internal and was re-seeded at v4. Application IDs remain derived slugs, so every CSV row still maps to the same document ID. |

### Why UUIDv7, and why no prefix

- **Local generation.** An identifier must never require a service round trip: the record
  has to be writable offline, and a failed ID allocation must not become a failed
  application.
- **Time-ordered.** UUIDv7 puts a 48-bit millisecond timestamp in the high bits, so the
  textual form sorts in creation order and the `events` log needs no secondary index.
  CPython's `uuid.uuid7()` is monotonic *within* a millisecond, so two events recorded
  back-to-back cannot sort randomly — this is asserted by test.
- **No `namespace:UUID` prefix.** Every generated ID already lives in a typed field of a
  typed document (`eventId`, `artifactId`, `proposalId`, `requestId`) and never shares an
  ID space with another kind. Document IDs therefore have exactly two shapes — a derived
  kebab slug or a bare UUID — which keeps `export --csv`, the Sheets view, and the `jq`
  workflows free of de-prefixing logic.

### If you find a v3 record

There should be none. If one appears (for example from a stale local script), do not edit
it in place: `tracker.py check` reports the store's `schemaVersion` from `meta/schema`, and
anything whose `schemaVersion` does not match the contract is a defect to be re-created
through the CLI, not patched by hand.
