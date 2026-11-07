"""The originals do not outlive the rules, and a worker that died leaves nothing stuck (REQ-007, REQ-017).

`cleanup_originals` (at the start of the worker and every hour) deletes files only, never a row or a chunk:
- the original of a FAILED document once more than FAILED_ORIGINAL_RETENTION_DAYS days have passed since it failed
  (`documents.updated_at`, set by the FAILED transition; a retry that fails again starts the time again); the document
  stays FAILED with its reason;
- the files of UPLOAD_DIR that have no document and were last written more than one hour ago: a file named by an id
  that is not a document, a file whose name is not an id, and a `{id}.part` of an upload that never finished.
The original of a QUEUED or PROCESSING document is never deleted, however old.

`recover_interrupted` makes FAILED `unexpected_error` ("Processing was interrupted") every document left PROCESSING,
which at the start of the worker can only be the work of a worker that died, through `DocumentRepository.mark_failed`
(it deletes the chunks too); the original is kept, so that the admin can retry. This assumes one worker.
`requeue_pending` puts every QUEUED document on the queue again (a job whose message was lost, a retry whose enqueue
failed); a document that also has a job waiting just gets a second one, and the claim at the start of the job lets only
one of them work.

Logs carry ids, counts and the type of an error: never a file name, a content or a message of an exception (REQ-102).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.session import create_engine
from app.documents import queue
from app.documents.repository import DocumentRepository
from app.documents.storage import UploadStorage

logger = logging.getLogger(__name__)

# A file without a document must be older than this to be swept: an upload in progress is not one.
ORPHAN_MIN_AGE = timedelta(hours=1)
_PART_SUFFIX = ".part"
INTERRUPTED_MESSAGE = "Processing was interrupted"


@dataclass(frozen=True)
class CleanupReport:
    """What one run deleted: original files of expired FAILED documents, and files that had no document."""

    expired_originals: int
    orphan_files: int


async def cleanup_originals(settings: Settings, now: datetime) -> CleanupReport:
    """Delete the expired originals of FAILED documents and the files without a document; `now` is injected."""
    storage = UploadStorage(settings.upload_dir)
    cutoff = now - timedelta(days=settings.failed_original_retention_days)
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            repo = DocumentRepository(session)
            # The files are listed before the rows are read: a listed file had its row inserted before it was renamed
            # from `.part`, so a missing row means a deleted document.
            old_files = await asyncio.to_thread(storage.orphans, ORPHAN_MIN_AGE, now)
            expired = await repo.ids_in_status("FAILED", changed_before=cutoff)
            named = {path: _document_id(path) for path in old_files}
            existing = await repo.existing_ids([found for found in named.values() if found is not None])
    finally:
        await engine.dispose()
    expired_paths = [storage.path(document_id) for document_id in expired]
    orphan_paths = [path for path, found in named.items() if found is None or found not in existing]
    removed_originals = await asyncio.to_thread(_delete_files, expired_paths)
    removed_orphans = await asyncio.to_thread(_delete_files, orphan_paths)
    logger.info(
        "cleanup: %d expired originals, %d files without a document deleted", removed_originals, removed_orphans
    )
    return CleanupReport(expired_originals=removed_originals, orphan_files=removed_orphans)


def _document_id(path: Path) -> uuid.UUID | None:
    """The document an original belongs to, from its name; None for a file that cannot be an original: any name that is
    not an id in its canonical form, and an unfinished `{id}.part` (its upload is over, whatever the document)."""
    if path.name.endswith(_PART_SUFFIX):
        return None
    try:
        found = uuid.UUID(path.name)
    except ValueError:
        return None
    return found if str(found) == path.name else None


def _delete_files(paths: Iterable[Path]) -> int:
    """Delete the files; a file that cannot be deleted is logged by the type of the error and skipped, so that one bad
    file does not stop the others. Returns how many were deleted."""
    deleted = 0
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            logger.warning("cleanup: a file could not be deleted (%s)", type(error).__name__)
            continue
        deleted += 1
    return deleted


async def recover_interrupted(settings: Settings) -> int:
    """FAILED `unexpected_error` for every PROCESSING document, chunks deleted, original kept; how many were changed."""
    engine = create_engine(settings.sqlalchemy_url)
    recovered = 0
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            repo = DocumentRepository(session)
            for document_id in await repo.ids_in_status("PROCESSING"):
                if await repo.mark_failed(document_id, "unexpected_error", INTERRUPTED_MESSAGE):
                    recovered += 1
                    logger.warning("document %s: FAILED unexpected_error, its processing was interrupted", document_id)
    finally:
        await engine.dispose()
    logger.info("recovery: %d interrupted documents marked FAILED", recovered)
    return recovered


async def requeue_pending(settings: Settings) -> int:
    """Put every QUEUED document on the queue again; how many jobs were queued."""
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            pending = await DocumentRepository(session).ids_in_status("QUEUED")
    finally:
        await engine.dispose()
    for document_id in pending:
        await asyncio.to_thread(queue.enqueue_document, settings.redis_url, document_id)
    logger.info("recovery: %d queued documents put back on the queue", len(pending))
    return len(pending)
