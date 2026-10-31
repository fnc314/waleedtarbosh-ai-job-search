# /outcome - Record the Result of an Application

You are recording what happened to a job application: progress updates (interview invitations, stages completed, offers) and final resolutions (hired, rejected, no response). The data lands in two places the framework already reads but nothing systematically writes:

- the tracker store (Firestore, written through `scripts/tracker.py`) - the status and event history that `/scrape` and `/rank` use for dedup and exclusion
- `documents/applications/<company>_<role>/` - the per-application archive (posting, submitted drafts, `outcome.md`) that `/setup` Path A mines to calibrate `04-job-evaluation.md` and surface STAR candidates

`/outcome` writes the data; `/setup` interprets it. This command never edits the evaluation framework or profile files itself.

**Every tracker write goes through `scripts/tracker.py`.** Never hand-edit a document, and never write `job_search_tracker.csv` — that file is a frozen snapshot from the 2026-10 migration, and nothing updates it (see `docs/tracker-migration.md`).

The command also owns the stretch *before* there is an outcome to record: the **follow-up branch** (Step 2b) surfaces open applications that have gone quiet, drafts a brief follow-up note in the user's voice, and logs it - so the chase and the resolution it eventually leads to live in one flow.

Follow these steps **in order**.

---

## Step 0: Parse Input

`$ARGUMENTS` may contain:

- Nothing → list open applications and ask which one to update
- A company name (optionally with a role), e.g. `/outcome acme` or `/outcome acme ml engineer` → target that application
- `followup` → enter the follow-up branch (Step 2b) over every quiet open application, using the default threshold of **10 days**
- `followup <N>`, e.g. `/outcome followup 14` → follow-up branch with an N-day threshold
- `followup <company>`, e.g. `/outcome followup acme` → draft a follow-up for that application now, regardless of threshold

---

## Step 1: Load State and Identify the Application

1. Read the record with `.venv/bin/python scripts/tracker.py list --open` (optionally `--limit N`). It prints JSON: each document carries `id`, `company`, `role`, `date`, `status`, `fitRating`, plus derived `counters` and `timeline`.
2. **With an argument:** match documents case-insensitively on company (and role, if given). One match → proceed. Several → list them and ask. None → the application was made outside the workflow; collect company, role, date applied, channel, and posting URL from the user, then create the record with a JSON payload:
   ```bash
   TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py add --json - <<'JSON'
   {
     "company": "Acme",
     "role": "Staff Android Engineer",
     "date": "2026-10-30",
     "status": "applied",
     "channel": "channel:other",
     "source": "https://example.com/posting",
     "events": [{"type": "event:applied", "date": "2026-10-30"}]
   }
   JSON
   ```
   - The `id` is derived from `company` + `role`, never typed by hand. Add `--dry-run` first and read the resolved document back.
   - `channel` must be a `channel:*` value from `tracker.schema.json`. If the source is not a recognised ATS, use `channel:other` and keep the human label in `metadata.legacyChannel`.
   - `status` accepts the legacy labels below; the CLI namespaces them.
3. **Without an argument:** list every open application (`--open`) as a numbered table (company, role, date applied, current status, days quiet, follow-ups sent) and ask which to update. **Days quiet** is `timeline.daysSinceActivity`; **follow-ups sent** is `counters.followUpsSent`. Both are computed from the event log — never re-parse prose to derive them. If any open record is 10+ days quiet with fewer than two follow-ups, add one line under the table: "Some of these have gone quiet - want a follow-up draft? (Step 2b)". If every record is resolved, say so and stop.
4. Derive the archive folder name: `documents/applications/<company>_<role>/` - lowercase, underscores for spaces (the convention documented in `documents/README.md`). Check whether the folder and an `outcome.md` already exist - if so, you are updating, not creating.

---

## Step 2: Collect What Happened

Ask the user what happened, then classify:

**Progress updates** (application still open):
- Interview invitation / stage scheduled or completed (phone screen, technical, case, final round)
- Offer received (not yet accepted or declined)

**Resolutions** (application closed) - these map to the status enum in `documents/README.md` that `/setup` parses:
- `hired` - accepted an offer
- `offer_declined` - received an offer, turned it down
- `rejected` - explicit rejection at any stage
- `no_response` - no reply; if the user is unsure whether to call it, note how long it has been since the last contact and let them decide - do not impose a cutoff
- `interview_only` - reached interviews but the process stalled or was abandoned without an explicit rejection

Also collect, without interrogating - one or two open questions are enough:
- Dates for the stages reached
- Any feedback received, verbatim where the user remembers it
- What they'd do differently, and any signal about what the company valued (these feed `/setup`'s calibration and STAR-candidate mining, so concrete beats polished)

---

## Step 2b: Follow-Up Branch (chase a quiet application)

Enter this branch from the `followup` argument (Step 0) or from the offer under the open-pipeline table (Step 1.3). Standard practice is a brief, polite follow-up one to two weeks after applying, at most twice; this branch operationalizes that.

**Candidates.** An application qualifies when its status is not final, the threshold has passed since its `date` (or since the last `followed up` marker in `notes`, if any), and it has fewer than **two** logged follow-ups. Parse dates defensively - skip rows whose dates do not parse and say so rather than guessing. Present qualifying applications as a table (company, role, days quiet, follow-ups sent, channel, contact person) and draft only for the ones the user picks.

**Threshold.** The 10-day default is deliberately earlier than `/gmail-sync`'s 30-day staleness flag (its Step 9): that check is a read-only alarm that a row has been forgotten entirely; this branch is the proactive nudge while a reply is still plausible. The two numbers serve different moments, which is why they differ.

**Drafting.** For each selected application:

1. Read the archive folder: the `job_posting.md`, `cv_draft.tex`, and `cover_letter.tex` that Step 3 maintains are the source of **every claim** the note may make - this is Rule 3 (never fabricate) widened to "no new claims": a follow-up that introduces skills or experience the submitted materials don't contain is a fabrication vector.
2. Apply the writing style rules from `03-writing-style.md` (no cliches, no em-dashes, warm but direct), and match the application's language - draw the register from the archived cover letter.
3. Write roughly **60 to 120 words**: address the `contact_person` from the tracker if present (otherwise the team, in the application's language); one sentence restating interest in the specific role; one concrete value-reminder drawn from the submitted materials; one polite question about the timeline. No pressure, no "just checking in" filler.
4. Shape it for the `channel` column: email (with a subject line reusing the application's headline), LinkedIn message (shorter, no subject), or portal message (plain text).
5. Present the draft and iterate until the user is happy.

**Logging.** Once the user confirms they will send it (or have sent it), log it in the same turn - an unlogged follow-up breaks the next run's quiet-days math:

- Append a `event:follow-up-sent` event:
  ```bash
  TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py event <applicationId> --type event:follow-up-sent --date YYYY-MM-DD --note "followed up via <channel>"
  ```
  The event increments `counters.followUpsSent` and resets `timeline.daysSinceActivity`; nothing needs to parse a note to recount.
- Save the final note as `followup_YYYY-MM-DD.md` in the application's archive folder. Safe by documented convention: `/setup` reads only the four named archive files and ignores extras (the same rule that covers `/interview`'s prep files), and `documents/applications/**` is gitignored personal data.

If the user decides not to send, log nothing.

**Termination.** When an application hits two follow-ups and stays silent, do not offer a third. This is the moment to continue in this same command's Step 2: note how long it has been since last contact and let the user decide whether to record `no_response` - as ever, no imposed cutoff. And if the user mentions an actual response while in this branch (an interview invitation, a rejection), drop out of the branch and record it through the normal Step 2 flow.

---

## Step 3: Archive the Application Materials

Create or update `documents/applications/<company>_<role>/`. All content here is personal data - the folder is already gitignored (`documents/applications/**`), so nothing needs redacting.

1. **`cv_draft.tex` and `cover_letter.tex`** - copy (never move) the submitted files. Locate them via `.venv/bin/python scripts/tracker.py get <applicationId>` → `artifacts[]` with `kind: artifact:cv` / `artifact:cover-letter` (each carries `localPath`, `driveUrl`, or `externalUrl`); if those are empty, look for `cv/main_<company>*.tex` and `cover_letters/cover_<company>_*.tex`. If a file already exists in the archive, leave it - the archived version is what was actually submitted. If no draft files exist (application made outside `/apply`), skip with a note.
2. **`job_posting.md`** - if it already exists, leave it. Otherwise try WebFetch on the tracker row's `source` URL and save the posting text. If the URL is dead (postings expire fast - this is exactly why the archive matters), ask the user to paste the posting, or write a stub noting the posting is unavailable. **Never reconstruct a posting from memory.**
3. **`outcome.md`** - write or update it in exactly the format documented in `documents/README.md`, so `/setup` Path A parses it without special cases:

```markdown
# Outcome: <Company> — <Role>

**Status:** in_progress | hired | offer_declined | rejected | no_response | interview_only

**Date resolved:** YYYY-MM-DD   <- only when resolved; omit while in_progress

## Interview stages reached
- [x] Phone screen (YYYY-MM-DD)
- [ ] Technical interview
- [ ] Case interview
- [ ] Final round
- [ ] Offer received

## Notes
<feedback received, what to do differently, signals about what they valued -
appended per update with a date, never overwritten>
```

Update rules: tick stage checkboxes as they are reached (add the date in parentheses), append dated entries to Notes, and only change `Status` from `in_progress` to a final value on resolution. Re-running `/outcome` on the same application is idempotent - it appends new information, never duplicates or rewrites history.

**Thank-you note trigger:** when this step ticks a newly completed interview stage, offer in the same turn: "Want a short thank-you note for the interviewer? A prompt one is standard practice." If accepted, draft it under Step 2b's drafting and logging rules (same voice, same no-new-claims boundary, same `followup_YYYY-MM-DD.md` archive convention). Recording the stage is the trigger - no scanning for recent stages is ever needed.

---

## Step 4: Update the Tracker

Record the change as an **event**. The record is an append-only log, so the update is a new entry rather than a field overwrite — that is what makes the history auditable and the derived counters correct.

**Status change (funnel moved):**
```bash
TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py status <applicationId> <status> --date YYYY-MM-DD --note "<what happened>"
```
The CLI namespaces the status, emits the matching event (`event:interview-scheduled`, `event:rejection`, …), updates `isOpen`, recomputes `notesRendered`, and queues a Gmail sync check two hours out.

**Activity that does not move the funnel** (follow-up sent, interview completed, assessment taken):
```bash
TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py event <applicationId> --type event:follow-up-sent --date YYYY-MM-DD --note "..."
```

**Corrections.** If the stored status is simply wrong, pass `--correction`; it emits `event:status-corrected` instead of pretending the funnel moved. A no-op status call is refused for the same reason.

**Never** delete an event, patch a derived field, or re-write history. A mistaken entry is corrected by a later event, so the record still shows both.

**Canonical `status` vocabulary (closed set).** The CLI accepts these or their `status:*` forms; anything else is rejected:

`drafting` → `applied` → `interview` → `offer` → `hired` / `rejected` / `no_response` / `offer_declined` / `withdrawn`

- `drafting` means documents were prepared but no submission is recorded. It is not an application yet.
- Never write `in_progress` to the tracker; that value belongs to `outcome.md`'s own `Status:` field, not to the tracker enum. Use the stage-appropriate value above instead.
- Values with spaces or capitals (`no response`, `Offer Declined`) are rejected by the schema check; always snake_case.

The full contract lives in `tracker.schema.json`.

---

## Step 5: Calibration Handoff

Count the `outcome.md` files under `documents/applications/` with a **final** status (not `in_progress`).

- If 3 or more are resolved (or 2+ share a pattern - same role type rejected twice, same sector going silent), suggest:
  > "You now have <N> resolved applications on record. Run `/setup` (Path A) to fold them into your evaluation framework - it calibrates fit scoring from what actually got interviews, and mines your interview feedback for STAR examples."
- Do **not** write anything into `04-job-evaluation.md` or other skill files yourself. `/setup` Path A owns that merge - it is read-before-write and idempotent, and duplicating its logic here would race it.

---

## Step 6: Confirm

Summarize what was recorded:

> **Outcome recorded for <Role> at <Company>.**
>
> - `documents/applications/<company>_<role>/outcome.md` - status: <status>, <what changed>
> - Archived: <which of cv_draft.tex / cover_letter.tex / job_posting.md were copied or fetched, and which were skipped and why>
> - Tracker: status → <new status>
>
> [Calibration suggestion from Step 5, if triggered]

If the update recorded an upcoming or newly scheduled interview stage, also suggest:

> "Interview coming up? `/interview <company>` builds a prep pack for that stage from this application's archive - the posting, the documents you submitted, and any feedback recorded from earlier rounds."

If the recorded status is `hired`, congratulate the user warmly first - this is the moment the whole framework exists for. Then add this single line (once; never on re-runs for the same application, and never for any other status):

> "If this framework helped you get there, consider [buying it a coffee](https://ko-fi.com/madslorentzen) - it keeps this free for the next job-seeker out there. ☕"

---

## Important Rules

1. **Write data, don't interpret it.** The archive and tracker are the outputs; calibration belongs to `/setup`. This command never edits profile or framework files.
2. **The archived version is the submitted version.** Existing files in the application folder are never overwritten by fresher drafts.
3. **Never fabricate.** A dead posting URL gets a user-pasted copy or an explicit "unavailable" stub, not a reconstruction. Feedback is recorded as the user reports it.
4. **Stay schema-compatible.** `outcome.md` follows the format in `documents/README.md` exactly (`in_progress` is the one addition, for open applications); every tracker write goes through `scripts/tracker.py`, which validates against `tracker.schema.json`.
5. **Idempotent updates.** Re-running on the same application appends new stages and notes; it never duplicates folders, rows, or history.
6. **Follow-ups: draft only, never send.** The follow-up branch produces text for the user to send themselves. It never emails, messages, or submits anything, and it must not be wired to tools that do.
7. **Follow-ups: no new claims.** Every substantive statement in a follow-up or thank-you note comes from the archived submitted materials. Rule 3 applies with no exceptions.
8. **Maximum two follow-ups per application.** After the second silent follow-up, the honest move is recording the resolution, not persistence.
