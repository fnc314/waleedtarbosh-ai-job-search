# Junie

Shared agent instructions are in `.agents/`:

- `.agents/README.md` — index and rules
- `.agents/skills/` — canonical skills (`job-tracker`, `job-scraper`, `upskill`, …)
- `AGENTS.md` — workspace role, candidate profile, shared rules
- `.claude/commands/` — the one implementation of the workflow commands

Do not keep copies of skills or commands in `.junie/`. Read them from the paths
above, so a change lands for every agent at once.
