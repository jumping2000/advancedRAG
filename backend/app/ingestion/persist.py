"""Saving the chunks of a document in one transaction and checking the indexes (REQ-014, REQ-015). Skeleton of T-03."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IndexingProfile
from app.ingestion.chunker import ChunkDraft


class DocumentGone(Exception):
    """The document was deleted, or is no longer PROCESSING, while the worker was preparing its chunks."""


async def persist_chunks(
    session: AsyncSession,
    document_id: uuid.UUID,
    profile: IndexingProfile,
    drafts: Sequence[ChunkDraft],
    vectors: Sequence[Sequence[float]],
) -> int:
    raise NotImplementedError


async def verify_indexes(
    session: AsyncSession, document_id: uuid.UUID, profile: IndexingProfile, expected: int
) -> bool:
    return False
