"""Connection layer for the tracker store.

Nothing else in the repo instantiates a Firestore or Storage client. Keeping
that in one module is what makes the "single write path" invariant enforceable:
`tools/security_guards.py` can grep for stray client construction, and a future
reader has exactly one place to look for credential handling.

Credentials resolve in this order, and never from the repository:

  1. TRACKER_SA_KEY          explicit path to a service-account JSON file
  2. ~/.config/job-tracker/sa.json   the canonical location (mode 600)
  3. GOOGLE_APPLICATION_CREDENTIALS  standard Google resolution
  4. Application Default Credentials (`gcloud auth application-default login`)

The service account needs roles/datastore.user and storage.objectAdmin on the
artifacts bucket, plus edit access to the tracker spreadsheet.
"""

from __future__ import annotations

import os
import pathlib

PROJECT_ID = os.environ.get("TRACKER_PROJECT", "job-search-fnc314-dev")
DATABASE = os.environ.get("TRACKER_DATABASE", "(default)")
ARTIFACT_BUCKET = os.environ.get("TRACKER_BUCKET", "job-search-fnc314-dev-artifacts")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "tracker.schema.json"
CACHE_PATH = REPO_ROOT / "generated" / "tracker-cache.json"
CANONICAL_KEY = pathlib.Path.home() / ".config" / "job-tracker" / "sa.json"

# Collection and subcollection names. Kebab-case, plural, per x-naming.
APPLICATIONS = "applications"
COMPANIES = "companies"
CONTACTS = "contacts"
PENDING_PROPOSALS = "pending-proposals"
SYNC_REQUESTS = "sync-requests"
META = "meta"
EVENTS = "events"
ARTIFACTS = "artifacts"

# meta/ document IDs
META_SCHEMA = "schema"
META_SYNC = "sync"


class CredentialError(RuntimeError):
    """Raised when no usable credential can be found."""


class WriteNotAllowed(RuntimeError):
    """Raised when a write is attempted without the explicit opt-in."""


def resolveCredentialsPath() -> pathlib.Path | None:
    """Return the service-account key path, or None to fall back to ADC."""
    explicit = os.environ.get("TRACKER_SA_KEY")
    if explicit:
        path = pathlib.Path(explicit).expanduser()
        if not path.exists():
            raise CredentialError(f"TRACKER_SA_KEY points at a missing file: {path}")
        return path
    if CANONICAL_KEY.exists():
        return CANONICAL_KEY
    return None


def _prepareEnvironment() -> None:
    """Point google-auth at the resolved key, without mutating repo state."""
    path = resolveCredentialsPath()
    if path is not None:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(path))


def getDb():
    """Return the Firestore client for the tracker database."""
    from google.cloud import firestore

    _prepareEnvironment()
    return firestore.Client(project=PROJECT_ID, database=DATABASE)


def getBucket():
    """Return the artifact bucket handle. Does not touch the network."""
    from google.cloud import storage

    _prepareEnvironment()
    return storage.Client(project=PROJECT_ID).bucket(ARTIFACT_BUCKET)


SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def getCredentials(scopes: list[str] | None = None):
    """Return credentials for Google APIs that are not covered by a client lib.

    Firestore and Storage construct their own credentials; Sheets does not, so
    it is resolved here and nowhere else.
    """
    resolvedScopes = scopes or SHEETS_SCOPES
    path = resolveCredentialsPath()
    if path is not None:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(path), scopes=resolvedScopes
        )
    _prepareEnvironment()
    from google.auth import default

    credentials, _ = default(scopes=resolvedScopes)
    return credentials


def assertWritesAllowed(purpose: str = "application data") -> None:
    """Gate writes to the record.

    The gate is explicit and inverted: you must opt in with
    TRACKER_ALLOW_WRITES=1. Writers have been ported to this path, so the gate
    now exists to make "the record changed" a deliberate act rather than a side
    effect of running a command, and to keep a stray agent run from mutating
    history. Infrastructure documents (meta/*) are exempt because they describe
    the store rather than record applications.
    """
    if os.environ.get("TRACKER_ALLOW_WRITES") == "1":
        return
    raise WriteNotAllowed(
        f"refusing to write {purpose} without explicit permission.\n"
        "  Writes are opt-in so the record cannot change as a side effect.\n"
        "  Read-only: check, list, get, export, collect --dry-run, score --dry-run\n"
        "  To proceed: TRACKER_ALLOW_WRITES=1"
    )


def getSheetId() -> str | None:
    """Spreadsheet ID for the regenerated Sheets view, if one has been created."""
    return os.environ.get("TRACKER_SHEET_ID") or None
