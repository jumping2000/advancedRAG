"""Hybrid retrieval over one knowledge base: vector search, BM25 and Reciprocal Rank Fusion (SPEC-2026-11-17, T-02).

Skeleton: the interface of the plan, to be implemented.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IndexingProfile, User


@dataclass(frozen=True)
class Ranked:
    """A chunk of the fused list with its rank in each list (None when it is not in it) and its RRF score."""

    chunk_id: uuid.UUID
    dense_rank: int | None
    bm25_rank: int | None
    score: float


@dataclass(frozen=True)
class Candidate:
    """A fused chunk loaded with what the context and the sources need."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    section_title: str | None
    page_start: int | None
    page_end: int | None
    content: str
    dense_rank: int | None
    bm25_rank: int | None
    score: float


async def dense_search(
    session: AsyncSession,
    user: User,
    kb_id: uuid.UUID,
    profile: IndexingProfile,
    vector: Sequence[float],
    limit: int,
) -> list[uuid.UUID]:
    raise NotImplementedError


async def bm25_search(
    session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID, question: str, limit: int
) -> list[uuid.UUID]:
    raise NotImplementedError


def fuse(dense: Sequence[uuid.UUID], bm25: Sequence[uuid.UUID], k: int) -> list[Ranked]:
    raise NotImplementedError


async def load_candidates(
    session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID, ranked: Sequence[Ranked]
) -> list[Candidate]:
    raise NotImplementedError


async def has_searchable_chunks(session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID) -> bool:
    raise NotImplementedError
