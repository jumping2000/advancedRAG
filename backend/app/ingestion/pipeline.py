"""The processing of one document by the worker (REQ-007, REQ-008, REQ-012, REQ-014, REQ-015).

The first step takes the document from QUEUED to PROCESSING in one statement: without a QUEUED row (deleted, already
taken, not in the queue) the job ends quietly. From then on every outcome is written on the document: READY with its
chunk count and the original deleted, or FAILED with a reason and no chunk of it left (the original is kept, to retry).
A document deleted while it is processed makes the job stop without writing anything.

Logs carry the document id, the status and the reason code only: never the content, the name of the file or the vectors
(REQ-102), and so no exception message either, only the exception type.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.db.models import Document
from app.db.session import create_engine
from app.documents.repository import DocumentRepository
from app.documents.storage import UploadStorage
from app.ingestion.chunker import ChunkDraft, chunk_blocks
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.errors import IngestionError, PermanentError
from app.ingestion.normalize import normalize_blocks
from app.ingestion.persist import DocumentGone, persist_chunks, verify_indexes
from app.ingestion.profile import ensure_profile
from app.ingestion.readers import read_text_document

logger = logging.getLogger(__name__)

_TEXT_FORMATS = ("txt", "md")
_UNEXPECTED = "Unexpected error while processing the document"


async def process_document(
    document_id: uuid.UUID, settings: Settings, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> None:
    """Take the document from QUEUED to READY, or to FAILED with its reason. `sleep` is for the waits between the
    attempts of a step (T-05); nothing waits yet."""
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            document = await DocumentRepository(session).claim(document_id)
        if document is None:
            logger.info("document %s: not queued, nothing to do", document_id)
            return
        logger.info("document %s: PROCESSING", document_id)
        try:
            await _process(engine, document, settings)
        except DocumentGone:
            logger.info("document %s: deleted while processing, work discarded", document_id)
        except IngestionError as error:
            await _fail(engine, document_id, error.code, error.message, type(error).__name__)
        except Exception as error:
            await _fail(engine, document_id, "unexpected_error", _UNEXPECTED, type(error).__name__)
    finally:
        await engine.dispose()


async def _process(engine: AsyncEngine, document: Document, settings: Settings) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    storage = UploadStorage(settings.upload_dir)
    if document.format not in _TEXT_FORMATS:
        # Interim, until Docling is called (T-05): the document gets a state and a reason.
        raise IngestionError("unexpected_error", "Docling parsing is not available yet")
    data = await asyncio.to_thread(storage.path(document.id).read_bytes)
    drafts = _text_chunks(data, cast(Literal["txt", "md"], document.format), settings)
    profile = await ensure_profile(engine, settings)
    embedder = OllamaEmbedder(settings.ollama_base_url, settings.embedding_model, settings.embedding_timeout_seconds)
    vectors: list[list[float]] = []
    size = settings.embedding_batch_size
    for start in range(0, len(drafts), size):
        vectors.extend(await embedder.embed([draft.content for draft in drafts[start : start + size]]))
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


def _text_chunks(data: bytes, fmt: Literal["txt", "md"], settings: Settings) -> list[ChunkDraft]:
    try:
        blocks = read_text_document(data, fmt)
    except UnicodeDecodeError:
        raise PermanentError("parse_failed", "The file is not valid UTF-8 text") from None
    drafts = chunk_blocks(normalize_blocks(blocks), settings.chunker_config)
    if not drafts:
        # Interim, until T-05 makes it UNSUPPORTED.
        raise PermanentError("parse_failed", "No text could be read")
    return drafts


async def _fail(engine: AsyncEngine, document_id: uuid.UUID, code: str, message: str, error_type: str) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        failed = await DocumentRepository(session).mark_failed(document_id, code, message)
    if failed:
        logger.warning("document %s: FAILED %s (%s)", document_id, code, error_type)
    else:
        logger.info("document %s: no longer PROCESSING, left as it is", document_id)
