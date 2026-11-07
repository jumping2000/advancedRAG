"""The processing of one document by the worker (REQ-007 to REQ-009, REQ-012, REQ-014 to REQ-016).

The first step takes the document from QUEUED to PROCESSING in one statement: without a QUEUED row (deleted, already
taken, not in the queue) the job ends quietly. A PDF, DOCX, PPTX or XLSX is converted by Docling, a TXT or MD is read
directly. From then on every outcome is written on the document: READY with its chunk count and the original deleted;
UNSUPPORTED `no_text_extracted` (fewer than MIN_EXTRACTED_CHARS characters that are not white after the normalization)
with the original deleted at once; or FAILED with a code and a fixed message and no chunk of it left (the original is
kept, to retry). A document deleted while it is processed makes the job stop without writing anything: the chunks are
saved under a lock on the document row (`DocumentGone` when it is gone) and the READY, UNSUPPORTED and FAILED marks
change only a PROCESSING row. So the original that is missing after a deletion (the file goes with the row) is not told
apart by a check: the FAILED mark finds no row and the job ends quietly, whereas a file lost under a row that is still
PROCESSING makes that document FAILED `unexpected_error`.

A temporary error of Docling or Ollama repeats that one step (the Docling call, each group of embeddings) up to
INGESTION_MAX_ATTEMPTS attempts in all (`with_retries`); any other error ends the document at once. An error that is not
an `IngestionError` (a bug, a database error, a missing original) is FAILED `unexpected_error`, and so is the timeout of
the job, with a message of its own.

Logs carry the document id, the status, the reason code, the step and the attempt number only: never the content, the
name of the file or the vectors (REQ-102), and so no exception message either, only the exception type.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from rq.timeouts import JobTimeoutException
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.db.models import Document
from app.db.session import create_engine
from app.documents.formats import DocumentFormat
from app.documents.repository import DocumentRepository
from app.documents.storage import UploadStorage
from app.ingestion.blocks import Block
from app.ingestion.chunker import chunk_blocks
from app.ingestion.docling import DoclingClient
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.errors import IngestionError, PermanentError, TemporaryError
from app.ingestion.normalize import normalize_blocks
from app.ingestion.persist import DocumentGone, persist_chunks, verify_indexes
from app.ingestion.profile import ensure_profile
from app.ingestion.readers import read_text_document
from app.ingestion.retry import with_retries

logger = logging.getLogger(__name__)

_TEXT_FORMATS = ("txt", "md")
_UNEXPECTED = "Unexpected error while processing the document"
_TIMEOUT = "The processing time limit was exceeded"
_NO_TEXT = "No text could be extracted from the document"

type _Sleep = Callable[[float], Awaitable[None]]


async def process_document(document_id: uuid.UUID, settings: Settings, *, sleep: _Sleep = asyncio.sleep) -> None:
    """Take the document from QUEUED to READY, to UNSUPPORTED or to FAILED with its reason. `sleep` is for the waits
    between the attempts of a step; the tests inject one that does not wait."""
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            document = await DocumentRepository(session).claim(document_id)
        if document is None:
            logger.info("document %s: not queued, nothing to do", document_id)
            return
        logger.info("document %s: PROCESSING", document_id)
        try:
            await _process(engine, document, settings, sleep)
        except DocumentGone:
            logger.info("document %s: deleted while processing, work discarded", document_id)
        except IngestionError as error:
            await _fail(engine, document_id, error.code, error.message, type(error).__name__)
        except JobTimeoutException as error:
            await _fail(engine, document_id, "unexpected_error", _TIMEOUT, type(error).__name__)
        except Exception as error:
            await _fail(engine, document_id, "unexpected_error", _UNEXPECTED, type(error).__name__)
    finally:
        await engine.dispose()


async def _process(engine: AsyncEngine, document: Document, settings: Settings, sleep: _Sleep) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = UploadStorage(settings.upload_dir)
    blocks = normalize_blocks(await _read_blocks(document, storage, settings, sleep))
    characters = sum(len("".join(block.text.split())) for block in blocks)
    drafts = chunk_blocks(blocks, settings.chunker_config) if characters >= settings.min_extracted_chars else []
    if not drafts:
        async with sessions() as session:
            marked = await DocumentRepository(session).mark_unsupported(document.id, "no_text_extracted", _NO_TEXT)
        if marked:
            logger.info("document %s: UNSUPPORTED no_text_extracted", document.id)
            storage.remove(document.id)
        else:
            logger.info("document %s: no longer PROCESSING, left as it is", document.id)
        return
    profile = await ensure_profile(engine, settings)
    embedder = OllamaEmbedder(
        settings.ollama_base_url, settings.embedding_model, settings.embedding_timeout_seconds, profile.dimensions
    )

    async def embed_group(texts: list[str]) -> list[list[float]]:
        return await _with_attempts(lambda: embedder.embed(texts), "embeddings", document.id, settings, sleep)

    vectors: list[list[float]] = []
    size = settings.embedding_batch_size
    for start in range(0, len(drafts), size):
        vectors.extend(await embed_group([draft.content for draft in drafts[start : start + size]]))
    async with sessions() as session:
        saved = await persist_chunks(session, document.id, profile, drafts, vectors)
        if not await verify_indexes(session, document.id, profile, saved):
            raise PermanentError("verification_failed", "The saved chunks were not found in the search indexes")
    async with sessions() as session:
        if not await DocumentRepository(session).mark_ready(document.id, saved):
            logger.info("document %s: no longer PROCESSING, left as it is", document.id)
            return
    logger.info("document %s: READY", document.id)
    storage.remove(document.id)


async def _read_blocks(document: Document, storage: UploadStorage, settings: Settings, sleep: _Sleep) -> list[Block]:
    """The blocks of the original: read directly for a TXT or MD, converted by Docling (with attempts) otherwise."""
    if document.format in _TEXT_FORMATS:
        data = await asyncio.to_thread(storage.path(document.id).read_bytes)
        try:
            return read_text_document(data, cast(Literal["txt", "md"], document.format))
        except UnicodeDecodeError:
            raise PermanentError("parse_failed", "The file is not valid UTF-8 text") from None
    key = settings.docling_api_key.get_secret_value() if settings.docling_api_key else None
    client = DoclingClient(
        settings.docling_base_url,
        key or None,
        settings.docling_timeout_seconds,
        include_images=settings.docling_include_images,
    )
    fmt = cast(DocumentFormat, document.format)
    # The name sent to Docling is made of the id and the format: the name of the user's file stays here.
    blocks = await _with_attempts(
        lambda: client.convert(storage.path(document.id), f"{document.id}.{fmt}", fmt),
        "docling",
        document.id,
        settings,
        sleep,
    )
    if client.partial_errors is not None:
        # Only the number: what Docling says about its errors may hold the content of the document.
        logger.warning("document %s: Docling reported %d errors (partial success)", document.id, client.partial_errors)
    return blocks


async def _with_attempts[T](
    operation: Callable[[], Awaitable[T]], step: str, document_id: uuid.UUID, settings: Settings, sleep: _Sleep
) -> T:
    """Run one step with the attempts of the settings; log the attempt number, and the code of a failed attempt."""
    attempt = 0

    async def once() -> T:
        nonlocal attempt
        attempt += 1
        if attempt > 1:
            logger.info("document %s: %s attempt %d of %d", document_id, step, attempt, settings.ingestion_max_attempts)
        try:
            return await operation()
        except TemporaryError as error:
            logger.warning("document %s: %s attempt %d failed (%s)", document_id, step, attempt, error.code)
            raise

    return await with_retries(
        once,
        attempts=settings.ingestion_max_attempts,
        backoff_seconds=settings.ingestion_retry_backoff_seconds,
        sleep=sleep,
    )


async def _fail(engine: AsyncEngine, document_id: uuid.UUID, code: str, message: str, error_type: str) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        failed = await DocumentRepository(session).mark_failed(document_id, code, message)
    if failed:
        logger.warning("document %s: FAILED %s (%s)", document_id, code, error_type)
    else:
        logger.info("document %s: no longer PROCESSING, left as it is", document_id)
