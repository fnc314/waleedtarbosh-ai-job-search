"""Artifact metadata and blob storage.

Two things live here and nowhere else:

1. How a file becomes an artifact document (`sha256`, `size`, `contentType`).
2. Where its bytes go in the bucket.

Local files stay the working copy in `documents/`; the bucket is a durable
mirror for the migration and for a machine that is not this laptop. A file that
exists only in the bucket is still openable, because the artifact document
carries `storage`.

The module never constructs a client. It receives a bucket handle, which keeps
`scripts/client.py` the single credential boundary.
"""

from __future__ import annotations

import hashlib
import mimetypes
import pathlib

import client
import contract

CHUNK = 1024 * 1024


class ArtifactError(RuntimeError):
    """A file cannot be registered as an artifact."""


def sha256File(path: str | pathlib.Path) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def contentTypeFor(path: str | pathlib.Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def describe(path: str | pathlib.Path) -> dict:
    """Hash and size a file. Read-only: the file is never modified."""
    filePath = pathlib.Path(path).expanduser()
    if not filePath.exists() or not filePath.is_file():
        raise ArtifactError(f"not a regular file: {filePath}")
    return {
        "filename": filePath.name,
        "contentType": contentTypeFor(filePath),
        "size": filePath.stat().st_size,
        "sha256": sha256File(filePath),
    }


def blobPath(applicationId: str, kind: str, filename: str) -> str:
    """`applications/{id}/{kind}/{filename}` with the enum namespace stripped."""
    if kind not in contract.enums("artifactKind"):
        raise ArtifactError(f"unknown artifact kind {kind!r}")
    leaf = kind.split(":", 1)[1]
    return f"{client.APPLICATIONS}/{applicationId}/{leaf}/{filename}"


def upload(bucket, *, applicationId: str, kind: str, path: str | pathlib.Path,
           blobName: str | None = None) -> dict:
    """Upload one file and return the `storage` block.

    `if_generation_match=0` makes the write create-only, so a re-upload can
    never silently replace a blob whose hash differs. The caller dedupes by
    `sha256` before calling.
    """
    filePath = pathlib.Path(path).expanduser()
    destination = blobPath(applicationId, kind, filePath.name) if blobName is None else blobName
    blob = bucket.blob(destination)
    blob.content_type = contentTypeFor(filePath)
    blob.upload_from_filename(str(filePath), if_generation_match=0)
    return {"bucket": bucket.name, "path": destination}


def ensureUpload(bucket, *, applicationId: str, kind: str, path: str | pathlib.Path,
                 sha256: str, size: int) -> tuple[dict, bool]:
    """Upload unless the blob already exists; return (storage, uploaded).

    Backfill has to be re-runnable. `upload` is create-only, so a second run
    would fail on every file it had already placed. Here an existing blob is
    reused when its recorded hash matches, or adopted when it predates hash
    metadata. A *different* hash at the same path is an error: that is a
    silently replaced file, and overwriting it would lose the submitted copy.
    """
    filePath = pathlib.Path(path).expanduser()
    destination = blobPath(applicationId, kind, filePath.name)
    blob = bucket.blob(destination)
    storage = {"bucket": bucket.name, "path": destination}
    if blob.exists():
        blob.reload()
        recorded = (blob.metadata or {}).get("sha256")
        if recorded and recorded != sha256:
            raise ArtifactError(
                f"{destination} already holds different content "
                f"(recorded {recorded[:12]}…, file {sha256[:12]}…)"
            )
        if not recorded:
            blob.metadata = {**(blob.metadata or {}), "sha256": sha256, "size": str(size)}
            blob.patch()
        return storage, False
    blob.content_type = contentTypeFor(filePath)
    blob.metadata = {"sha256": sha256, "size": str(size)}
    blob.upload_from_filename(str(filePath), if_generation_match=0)
    return storage, True


def exists(bucket, name: str) -> bool:
    return bucket.blob(name).exists()


def remoteHash(bucket, name: str) -> str | None:
    blob = bucket.blob(name)
    if not blob.exists():
        return None
    blob.reload()
    return (blob.metadata or {}).get("sha256")


def artifactDocument(entry: dict, *, source: str, now: str) -> dict:
    """Build and validate an artifact document from a resolved payload entry."""
    locations = [key for key in ("localPath", "externalUrl", "driveUrl") if entry.get(key)]
    if len(locations) != 1:
        raise contract.ContractError(
            f"artifact {entry.get('filename')!r} must set exactly one of "
            f"localPath, externalUrl, driveUrl (got {locations})"
        )
    document = {
        "artifactId": contract.newUuid(),
        "kind": entry["kind"],
        "filename": entry["filename"],
        "contentType": entry.get("contentType"),
        "size": entry.get("size"),
        "sha256": entry.get("sha256"),
        "storage": entry.get("storage"),
        "localPath": entry.get("localPath"),
        "driveUrl": entry.get("driveUrl"),
        "externalUrl": entry.get("externalUrl"),
        "createdAt": now,
        "source": source,
    }
    return contract.validate("artifact", document)
