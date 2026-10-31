#!/bin/zsh
# Fetch Gmail, stage a proposal, then run any deferred sync requests.
#
# This is the scheduled version of `/gmail-sync`'s deterministic half. It
# writes only generated payloads, `pending-proposals`, and `meta/sync`; it never
# applies a proposal. Review stays interactive.
#
# No arguments are required. `--label` and `--since` are passed through to
# `scripts/gmail_fetch.py` for a manual run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# launchd starts with a minimal PATH and does not read your shell profile.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ -f .env.local ]]; then
    set -a
    . ./.env.local
    set +a
fi

export TRACKER_SA_KEY="${TRACKER_SA_KEY:-$HOME/.config/job-tracker/sa.json}"
export TRACKER_ALLOW_WRITES="${TRACKER_ALLOW_WRITES:-1}"
export PYTHONUNBUFFERED=1

PYTHON="$REPO/.venv/bin/python"

# A failed fetch must stop the run: staging an older payload as if it were new
# would misrepresent the mailbox.
"$PYTHON" scripts/gmail_fetch.py "$@"

"$PYTHON" scripts/tracker.py collect \
    --labels generated/gmail-labels.json \
    --trigger trigger:cron

# Requests queued by /apply or /outcome during the interval run now, so the
# 15-minute collector is not the only thing that ever consumes them.
"$PYTHON" scripts/tracker.py sync-due
