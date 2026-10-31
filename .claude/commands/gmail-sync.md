# /gmail-sync - Sync Application Status from Gmail

You keep the tracker's event history in step with the user's inbox. The work is split in two because they have different trust requirements:

- **Fetch + stage (agent, deterministic result).** Read Gmail, write what you found to `generated/gmail-inbox.json`, and let `tracker.py collect` match, classify, and stage a proposal. This half never touches an application.
- **Review (agent + human).** Present the staged proposal, wait for approval, then apply the approved changes as events. This half is the only thing that writes application history, and it writes nothing until the user answers.

A cron job cannot do the second half, and an LLM cannot be the authority for the first. `tracker.py sync-due` runs the same `collect` on a 15-minute schedule when a write has queued a deferred sync, so a fetch you stage is reviewed whenever you next run `/gmail-sync`.

Follow these steps **in order**.

---

## Step 0: Prerequisites

Confirm that a Gmail integration is available via either:
1. Composio Gmail tool (`GMAIL_FETCH_EMAILS`, `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`, `GMAIL_LIST_LABELS`, executed via `COMPOSIO_MULTI_EXECUTE_TOOL`), OR
2. Native Anthropic/Claude Gmail MCP tools (`mcp__claude_ai_Gmail__*` or standard Gmail MCP `list_labels`, `search_threads`, `get_thread`).

If neither is available/active, instruct the user to connect Gmail (Composio `COMPOSIO_MANAGE_CONNECTIONS`, or claude.ai Settings → Connectors → Gmail) and stop. Never attempt this via Bash, IMAP, or any other channel.

---

## Step 1: Choose the Mode

`$ARGUMENTS` decides:

| input | mode |
|---|---|
| `review` | review the queue only — no Gmail access |
| `fetch` | fetch with the default 30-day window, then stage |
| `fetch <company>` or just `<company>` | fetch scoped to one tracked application, then stage |
| `fetch since <YYYY-MM-DD>` or `since <YYYY-MM-DD>` | fetch with an explicit lookback, then stage |
| `--label <name>` (with any fetch form) | restrict the fetch to that Gmail label |
| nothing | if a pending proposal exists, review it; otherwise fetch with the default window |

Check the queue first:

```bash
TRACKER_ALLOW_WRITES=0 .venv/bin/python scripts/tracker.py proposals list --state proposal:pending
```

`proposals list` is read-only and needs no opt-in. If the user asked to fetch, go to Step 3 even when the queue is non-empty — do not silently switch modes.

---

## Step 2: Load Tracked Applications

1. Read the tracker with `.venv/bin/python scripts/tracker.py list --open`. If it returns no documents, tell the user there is nothing to sync against yet (suggest `/outcome` or `/apply` first) and stop. `/gmail-sync` never originates new applications.
2. For each open application, note `id`, `company`, `role`, and current `status`; the collector uses these to match and classify, and the review table quotes them.
3. If `$ARGUMENTS` named a company, filter this set case-insensitively. No match → say so and stop; do not guess.

---

## Step 3: Fetch (agentic half)

### Step 3a: Build the query

Lookback: the `since <date>` argument if given, else `newer_than:30d`. Already-processed messages are filtered by the collector, so a generous window is safe.

#### Which mail to check

The default scope is all four job-search inbox areas:

| area | Gmail selector |
|---|---|
| Primary | `category:primary` |
| Updates | `category:updates` |
| JOBS | `label:JOBS` |
| JOBS sub-labels | `label:"JOBS/<sub>"` for each sub-label seen last run |

Sub-labels are expanded from the inventory stored by the previous sync, because Gmail has no `label:JOBS/*` wildcard. Read it first:

```bash
.venv/bin/python scripts/tracker.py labels
```

That returns `{count, labels: [{id, name, type}]}` from `meta/sync.gmail.labels`. If the list is empty (first run, or a wipe), fall back to the label whose name contains "job", "application", or "career" from this run's `GMAIL_LIST_LABELS` / `list_labels`, and record the full inventory so the next run can enumerate.

#### The `--label` flag

`--label <name-or-id>` restricts the fetch to one label instead of the default four areas. **Enumerate it against the stored inventory**, not against a hard-coded list:

1. Run `tracker.py labels`.
2. If `--label` matches a stored `name` or `id` (case-insensitive, and exact — do not fuzzy-match), use `label:"<name>"`.
3. If it does not match, **stop** and reply with the available names:
   `Unknown label "<input>". Last sync saw: Primary-style categories, JOBS, JOBS/Applications, …`
   Do not guess, and do not fall back to the default scope.
4. If the inventory is empty, say that the labels are discovered on the first fetch and ask the user to run `/gmail-sync fetch` without `--label` once.

#### The query

Combine, with OR groups:

- the selected scope: either `--label`'s single `label:"…"`, or the default group `(category:primary OR category:updates OR label:"JOBS" OR label:"JOBS/<sub>" …)`
- a quoted-name OR-group of the open applications' company names, e.g. `{"Acme Corp" "BigCo"}`
- a sender-domain OR-group of common ATS platforms: `{from:greenhouse.io from:lever.co from:myworkday.com from:ashbyhq.com from:smartrecruiters.com from:icims.com from:bamboohr.com}`
- the lookback bound, e.g. `newer_than:30d` or `after:2026/06/15`
- `in:inbox` when the scope is a category; omit it for an explicit label (a filter rule may have archived the mail, and the label is already the scope)

Example: `newer_than:30d in:inbox ((category:primary OR category:updates) OR label:"JOBS" OR label:"JOBS/Applications") ({"Acme Corp" "BigCo"} OR {from:greenhouse.io from:lever.co})`

3. Execute:
   - **Composio:** `GMAIL_FETCH_EMAILS` with `query`, `include_payload: false` (or `ids_only: true`), `max_results: 50`, paginating via `page_token`.
   - **Claude/Anthropic MCP:** `search_threads` with `view: THREAD_VIEW_MINIMAL`, `pageSize: 50`, paginating via `pageToken`.

### Step 3b: Read the full bodies

Fetch every returned message by id:
- **Composio:** `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` with `format: "full"`.
- **Claude/Anthropic MCP:** `get_thread` with `messageFormat: FULL_CONTENT`.

The classifier reads `subject` + `snippet`, so the snippet you write must contain the signal phrase, not a truncated preview. Put the subject and the first ~800 characters of the plain-text body into `snippet`.

### Step 3c: Write the inbox payload

Create `generated/gmail-inbox.json` (gitignored, regenerated every run). One object per new message:

```json
{
  "lookbackFrom": "2026-10-01T00:00:00+00:00",
  "query": "<the query you ran>",
  "messages": [
    {
      "messageId": "<gmail message id>",
      "applicationId": "<tracker id, only when the match is certain>",
      "subject": "<subject>",
      "sender": "<from address>",
      "snippet": "<subject + first ~800 chars of the plain-text body>",
      "receivedAt": "2026-10-29T14:03:00+00:00"
    }
  ]
}
```

Rules for this file:

- **`applicationId` is optional, and only for certainty.** Omit it when the match is not obvious; the collector matches conservatively on the company name and lists anything ambiguous under `unmatched`. Never set it to "the most likely" application.
- **`receivedAt` is the email's date**, ISO-8601 with an offset. It becomes the event date.
- **Never edit an earlier payload.** Each fetch replaces the file; history lives in Firestore, not here.
- If nothing new was found, write `{"messages": []}` and say so. Do not invent messages to make the run look productive.

Write the label inventory beside it, from this run's `GMAIL_LIST_LABELS` / `list_labels` (every label, not just the scoped ones):

```json
{
  "labels": [
    {"id": "Label_1", "name": "JOBS", "type": "user"},
    {"id": "Label_2", "name": "JOBS/Applications", "type": "user"},
    {"id": "CATEGORY_PERSONAL", "name": "CATEGORY_PERSONAL", "type": "system"}
  ]
}
```

### Step 3d: Stage it

Preview, then stage:

```bash
TRACKER_ALLOW_WRITES=0 .venv/bin/python scripts/tracker.py collect --dry-run --labels generated/gmail-labels.json
TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py collect --trigger trigger:manual --labels generated/gmail-labels.json
```

`--labels` stores the inventory in `meta/sync.gmail.labels` (with `labelsSeenAt`), which is what `tracker.py labels` and the next `--label` call enumerate. It is written even when no proposal is staged, so a quiet fetch still teaches the next run which labels exist.

`collect` matches and classifies deterministically, stages **one** proposal, and writes `meta/sync` (processed message ids + watermark). It writes `pending-proposals` and `meta/sync` and nothing else. It never creates, updates, or closes an application.

If the result says `staged: false`, report the reason (`no new messages`, `no messages to stage`) and stop. Do not force a proposal.

---

## Step 4: Review the Queue

Everything below is the human half.

```bash
.venv/bin/python scripts/tracker.py proposals list --state proposal:pending
.venv/bin/python scripts/tracker.py proposals show <proposalId>
```

Present every pending proposal as one table. The collector's `changes[]` already carries the matched application, the event type, the current and proposed status, the confidence, and the source email; quote them rather than re-deriving anything:

```
## Gmail Sync - Review Queue - YYYY-MM-DD

Proposal <proposalId> · staged <createdAt> · trigger <trigger>

### Changes (reply "approve all", or name the rows, e.g. "1, 3")
| # | Company | Role | Signal | Current -> Proposed | Confidence | Source Email (date) |
|---|---|---|---|---|---|---|
| 1 | Acme | Staff Android Engineer | rejection | applied -> rejected | high | "Update on your application" (2026-10-29) |
| 2 | BigCo | Android Lead | interview | applied -> interview | medium | "Let's schedule a call" (2026-10-30) |

### Unmatched (no change proposed)
- "<subject>" from <sender> — <collector's reason>

### Offer Emails
- **<Company>** — the tracker moves to `offer`. Accepting or declining is the user's decision: never propose `hired` or `offer_declined` from an email.
```

If there are no pending proposals, say so briefly and stop.

**Conflicts.** The collector never overwrites a final status with a later signal, and approval skips a change whose target status already matches. If an email contradicts what the user knows (a rejection after an offer, two messages that disagree), do not edit the proposal. Tell the user, and point them at `/outcome <company>` so the correction is recorded deliberately.

---

## Step 5: Wait for Approval

Stop and wait. Nothing is written before the reply.

- "approve all" / "yes" → approve every change in the proposal.
- "1, 3" / "approve the first and third" → approve only those indexes.
- "skip 2" → approve all but those indexes.
- "no" → reject the proposal with the user's reason.

---

## Step 6: Apply the Approved Changes

Approval is a single command; it emits one event per selected change and updates the derived fields in the same batch, so a partly-applied proposal cannot exist:

```bash
TRACKER_ALLOW_WRITES=1 .venv/bin/python scripts/tracker.py proposals approve <proposalId> --only 1,3 --note "reviewed 2026-10-31"
```

`--only` takes the 1-based row numbers from Step 4. Omit it to approve everything. The result reports `approved` and `skipped` (unselected rows, already-matching statuses, deleted applications) — never claim a row was written without seeing it under `approved`.

For a rejection or a batch the user declines entirely:

```bash
.venv/bin/python scripts/tracker.py proposals reject <proposalId> --reason "not a status change"
```

Rejecting writes nothing to `applications`. The message ids stay processed, so the same mail is not re-proposed.

### Step 6a: Update the per-application archive

The tracker is updated by `approve`; `documents/applications/<company>_<role>/outcome.md` is not. For each **approved** change, update the archive:

| Signal | `outcome.md` action |
|---|---|
| Rejection | Set `Status: rejected` and `Date resolved:` to the email's date |
| Interview invite / scheduled | Tick the matching stage checkbox with the email's date |
| Assessment / OA | Tick the nearest stage checkbox, or add a Notes line if no checkbox fits |
| Offer extended | Tick "Offer received" |
| Acknowledgement | No change (the event is enough) |

Append a dated line to `## Notes` — never rewrite existing notes:

```
YYYY-MM-DD (via /gmail-sync): <one-line summary>. Source: "<subject>" from <sender>, <email date>.
```

If no archive folder exists for an application added outside `/apply`/`/outcome`, create it with a minimal `outcome.md` following `documents/README.md`.

---

## Step 7: Staleness Check

For open applications whose `timeline.daysSinceActivity` is 30+ days (from `tracker.py list`, never re-parsed prose), add one "needs follow-up" line to the closing summary. Surface only — never write for staleness.

---

## Step 8: Present the Closing Summary

```
## Gmail Sync - Done - YYYY-MM-DD

### Written
| Company | Role | Signal | Status | Source Email |
|---|---|---|---|---|
| Acme | Staff Android Engineer | rejection | applied -> rejected | "Update on your application", 2026-10-29 |

### Skipped
- **BigCo** — interview change left unselected; it will not be re-proposed. Use `/outcome BigCo` if it is still true.

### Offers Requiring Your Decision
- **<Company>** — tracker set to `offer` on 2026-10-30. Run `/outcome <company>` when you decide.

### Stale Applications (30+ days, no activity)
- **<Company>** — last activity YYYY-MM-DD, still `<status>`.
```

If nothing was proposed or approved, a short note is enough. If this run pushed the number of **final** `outcome.md` statuses to 3+, suggest the `/setup` Path A calibration handoff (same as `/outcome`; do not duplicate its logic).

---

## Important Rules

1. **The fetch half never writes application history.** Only `proposals approve` does, and only after an explicit reply.
2. **Never fabricate a match.** Omit `applicationId` when unsure; the collector's `unmatched` list exists for that.
3. **Classify from the body, not the preview.** The snippet you write must contain the phrase the classifier needs.
4. **Never propose `hired` or `offer_declined`.** Those are the user's real-world decision; the chain stops at `offer`.
5. **A conflict is a manual-review flag, not an overwrite.** When in doubt, don't propose it — surface it and point at `/outcome`.
6. **Idempotent by message id.** Processed ids live in `meta/sync`; re-fetching the same window must not re-propose it.
7. **Read-only against Gmail.** No applying labels, archiving, or deleting in the user's mailbox. The only label writes are the local inventory in `meta/sync`.
8. **All state is personal data.** `generated/gmail-inbox.json`, the local `generated/` cache, and `documents/applications/**` are gitignored. Tracker data lives in Firestore, not in the repo.
9. **The Sheet is a view.** Never hand-edit it; regenerate it with `tracker.py sheet --sync`.
