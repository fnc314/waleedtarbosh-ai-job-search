# Shared agent directory

Everything that more than one agent needs lives here, once:

- `AGENTS.md` (repo root) — the workspace role, candidate profile, and shared rules.
- `.agents/skills/` — canonical skills. Start with `job-tracker/SKILL.md` for the
  application record (Firestore, `scripts/tracker.py`, Gmail review queue, Sheets).
- `.claude/commands/` — the single implementation of the workflow commands
  (`/apply`, `/outcome`, `/gmail-sync`, …). Other agents read these files; they
  are not copied per agent.

Rules for agents:

1. Read `AGENTS.md` first, then the skill or command the task names.
2. Do not create agent-specific copies of skills or commands. If something must
   change, change it here (or in `.claude/commands/`) and leave a pointer.
3. Each agent directory has one `<AGENT_NAME>.md` that points back here; that is
   the only file an agent-specific directory should own.
4. Tracker writes go through `scripts/tracker.py` only. `job_search_tracker.csv`
   is a frozen archive snapshot, not a data source.
