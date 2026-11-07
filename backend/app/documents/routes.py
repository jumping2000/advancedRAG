"""Documents API of a knowledge base (REQ-002 to REQ-006, REQ-018), for the admin only.

The role is checked before the ids and the body (router-level dependency): a `user` gets 403 whatever it sends. Then
the KB (404 kb_not_found, also for an id that is not a UUID), then the size announced by the request, then the body.

Upload: the file is copied to `{id}.part` while its SHA-256 and size are computed, checked (empty, type), inserted
(the unique index (kb_id, checksum) refuses duplicates, also between concurrent requests), renamed to `{id}` and put
on the ingestion queue. No row, file or job is left behind by any error.

Retry: a FAILED document whose original still exists goes back to QUEUED in one statement (`requeue`, which only one of
two simultaneous requests can win) and is put on the queue; any other state is 409 `not_retryable`, a FAILED document
without its original 409 `original_missing`.

Delete: the document row goes first (its chunks with it, by cascade), then the original; it is the same for a document
that a job is processing, which then finds nothing to write (REQ-019). A file that cannot be removed is logged by the
document id and the type of the error and does not fail the request: the document is gone, and the hourly sweep of the
worker deletes a file that has no document.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.deps import ApiError, DbSession, require_admin
from app.config import Settings
from app.db.models import Document, User
from app.documents import queue
from app.documents.formats import UnsupportedType, inspect_upload, sanitize_filename
from app.documents.repository import DocumentRepository, DuplicateDocument
from app.documents.schemas import DocumentOut
from app.documents.storage import FileTooLarge, UploadStorage
from app.knowledge_bases.repository import KnowledgeBaseRepository
from app.knowledge_bases.routes import visible_kb_id

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
Admin = Annotated[User, Depends(require_admin)]

router = APIRouter(prefix="/kbs/{kb_id}/documents", dependencies=[Depends(require_admin)])


def document_not_found() -> ApiError:
    return ApiError(404, "document_not_found", "Document not found")


def file_too_large() -> ApiError:
    # The same body that Nginx answers when it blocks a request above the limit (nginx/default.conf.template).
    return ApiError(413, "file_too_large", "The file is too large")


def not_retryable() -> ApiError:
    return ApiError(409, "not_retryable", "Only a failed document can be retried")


def parse_document_id(document_id: str) -> uuid.UUID:
    """An id that is not a UUID is answered like an unknown one (404 document_not_found), not with a 422."""
    try:
        return uuid.UUID(document_id)
    except ValueError:
        raise document_not_found() from None


async def visible_kb(kb_id: str, admin: Admin, session: DbSession) -> uuid.UUID:
    """The id of the KB if it exists (404 kb_not_found otherwise); every document route starts from it."""
    return await visible_kb_id(kb_id, admin, KnowledgeBaseRepository(session))


KbId = Annotated[uuid.UUID, Depends(visible_kb)]


def repository(session: DbSession) -> DocumentRepository:
    return DocumentRepository(session)


Repository = Annotated[DocumentRepository, Depends(repository)]


def single_file(form: FormData) -> UploadFile:
    """The one file of the field `file`; anything else (no file, a text value, two files) is a 422."""
    values = form.getlist("file")
    if len(values) != 1 or not isinstance(values[0], UploadFile):
        raise RequestValidationError([{"type": "missing", "loc": ("body", "file")}])
    return values[0]


async def store_upload(
    request: Request, repo: DocumentRepository, kb_id: uuid.UUID, upload: UploadFile, max_bytes: int
) -> Document:
    settings: Settings = request.app.state.settings
    storage = UploadStorage(settings.upload_dir)
    document_id = uuid.uuid4()
    inserted = False
    try:
        try:
            stored = await run_in_threadpool(storage.save, document_id, upload.file, max_bytes)
        except FileTooLarge:
            raise file_too_large() from None
        if stored.size == 0:
            raise ApiError(422, "empty_file", "The file is empty")
        filename = sanitize_filename(upload.filename or "")
        try:
            fmt = await run_in_threadpool(inspect_upload, storage.part_path(document_id), filename, upload.content_type)
        except UnsupportedType:
            raise ApiError(415, "unsupported_type", "The file type is not supported") from None
        try:
            document = await repo.create(kb_id, document_id, filename, fmt, stored.size, stored.sha256)
        except DuplicateDocument as duplicate:
            raise ApiError(
                409,
                "duplicate_document",
                "A document with the same content already exists in this knowledge base",
                {"existing": {"id": str(duplicate.existing.id), "filename": duplicate.existing.filename}},
            ) from None
        inserted = True
        storage.commit(document_id)
        await run_in_threadpool(queue.enqueue_document, settings.redis_url, document_id)
    except ApiError:
        storage.discard(document_id)
        raise
    except Exception:
        # Only the id is logged: never the name or the content of the file (REQ-102).
        logger.exception("upload of document %s failed", document_id)
        storage.discard(document_id)
        storage.remove(document_id)
        if inserted:
            await repo.delete(kb_id, document_id)
        raise ApiError(500, "internal_error", "Internal server error") from None
    return document


@router.post("", status_code=202)
async def upload_document(request: Request, kb_id: KbId, repo: Repository) -> DocumentOut:
    max_bytes = request.app.state.settings.max_upload_size_mb * MIB
    announced = request.headers.get("content-length", "")
    # Beyond the limit plus 1 MB of multipart framing the body is not even read.
    if announced.isascii() and announced.isdigit() and int(announced) > max_bytes + MIB:
        raise file_too_large()
    try:
        async with request.form(max_files=1) as form:
            document = await store_upload(request, repo, kb_id, single_file(form), max_bytes)
    except StarletteHTTPException as error:
        if error.status_code != 400:
            raise
        # Starlette answers a malformed multipart body (or a second file) with 400: here it is a 422 as for any body.
        raise RequestValidationError([{"type": "multipart", "loc": ("body",)}]) from None
    return DocumentOut.from_document(document)


@router.get("")
async def list_documents(kb_id: KbId, repo: Repository) -> list[DocumentOut]:
    return [DocumentOut.from_document(document) for document in await repo.list_for_kb(kb_id)]


@router.get("/{document_id}")
async def get_document(kb_id: KbId, document_id: str, repo: Repository) -> DocumentOut:
    document = await repo.get(kb_id, parse_document_id(document_id))
    if document is None:
        raise document_not_found()
    return DocumentOut.from_document(document)


@router.post("/{document_id}/retry", status_code=202)
async def retry_document(request: Request, kb_id: KbId, document_id: str, repo: Repository) -> DocumentOut:
    """FAILED -> QUEUED and a new job. The request has no body (a POST without Content-Type passes `require_json`)."""
    settings: Settings = request.app.state.settings
    parsed_id = parse_document_id(document_id)
    document = await repo.get(kb_id, parsed_id)
    if document is None:
        raise document_not_found()
    if document.status != "FAILED":
        raise not_retryable()
    if not UploadStorage(settings.upload_dir).exists(parsed_id):
        raise ApiError(409, "original_missing", "The original file no longer exists: upload the document again")
    requeued = await repo.requeue(kb_id, parsed_id)
    if requeued is None:
        raise not_retryable()  # another request was faster: it queued the document, this one must not queue it again
    try:
        await run_in_threadpool(queue.enqueue_document, settings.redis_url, parsed_id)
    except Exception as error:
        # Only the id and the type of the error are logged (REQ-102). The document stays QUEUED: the worker puts the
        # QUEUED documents that have no job back on the queue when it starts.
        logger.error("retry of document %s: the job could not be queued (%s)", parsed_id, type(error).__name__)
        raise ApiError(500, "internal_error", "Internal server error") from None
    return DocumentOut.from_document(requeued)


@router.delete("/{document_id}", status_code=204, response_class=Response)
async def delete_document(request: Request, kb_id: KbId, document_id: str, repo: Repository) -> None:
    """The document, its chunks and its original, at once, whatever its state (also PROCESSING)."""
    parsed_id = parse_document_id(document_id)
    if not await repo.delete(kb_id, parsed_id):
        raise document_not_found()
    settings: Settings = request.app.state.settings
    try:
        UploadStorage(settings.upload_dir).remove(parsed_id)
    except OSError as error:
        logger.warning("delete of document %s: its original could not be removed (%s)", parsed_id, type(error).__name__)
