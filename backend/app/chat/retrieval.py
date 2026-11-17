"""Hybrid retrieval over one knowledge base: vector search, BM25 and Reciprocal Rank Fusion (SPEC-2026-11-17, T-02).

Every function takes the session and the user, and the statement that reads `chunks` carries, in the same SQL
statement, the access check (the KB asked is one of `KnowledgeBaseRepository.visible_kb_ids(user)`), the KB asked, a
`documents.status = 'READY'` join and the indexing profile: no path returns a chunk of a KB the user cannot see, of a
document that is not READY, or of another profile (vectors of different profiles never mix). Nothing here logs:
questions, chunk text, file names and vectors never reach a log.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Float,
    Select,
    Text,
    and_,
    bindparam,
    cast,
    exists,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, IndexingProfile, User
from app.db.vector import Vector
from app.ingestion.profile import embedding_expression
from app.knowledge_bases.repository import KnowledgeBaseRepository

# Upper bound of the rows an iterative HNSW scan may visit to fill the LIMIT. pgvector's default (20000) is low for a KB
# that holds a small share of the index of its profile; beyond this bound the scan stops, so a search of a very small
# KB in a very large index may return fewer rows than asked.
_MAX_SCAN_TUPLES = 200_000
_MIN_EF_SEARCH = 40  # pgvector's default
_MAX_EF_SEARCH = 1000  # the largest value pgvector accepts


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


class _SizedVector(Vector):
    """`vector(N)`, the type of the question vector: the parameter is sent as `CAST($1 AS vector(N))`."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dimensions})"


def _scope(user: User, kb_id: uuid.UUID, profile_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """What every read of chunks must satisfy; the statement selects from `Chunk` joined to `Document` (see `_chunks`).

    `kb_id IN visible_kb_ids(user)` is written on the KB asked, as a parameter: it holds no column of the statement,
    so PostgreSQL evaluates it once before it reads any chunk, and a user who cannot see the KB costs no index scan."""
    return [
        Chunk.kb_id == kb_id,
        literal(kb_id, UUID(as_uuid=True)).in_(KnowledgeBaseRepository.visible_kb_ids(user)),
        Chunk.profile_id == profile_id,
        Document.status == "READY",
    ]


def _chunks(*columns: Any) -> Select[Any]:
    return (
        select(*columns)
        .select_from(Chunk)
        .join(Document, and_(Document.id == Chunk.document_id, Document.kb_id == Chunk.kb_id))
    )


async def dense_search(
    session: AsyncSession,
    user: User,
    kb_id: uuid.UUID,
    profile: IndexingProfile,
    vector: Sequence[float],
    limit: int,
) -> list[uuid.UUID]:
    """The ids of the `limit` chunks nearest to the vector by cosine distance, nearest first (ties by chunk id).

    The distance is computed on the expression of the profile's partial HNSW index (`embedding::vector(N)`) and the
    statement filters `profile_id`, so that index is the one scanned. pgvector applies the other filters (KB, document
    status) after the index scan: the scan is iterative (`relaxed_order`: it goes on until `limit` rows pass the
    filters, up to `_MAX_SCAN_TUPLES`) and `hnsw.ef_search` is at least `limit`. The settings are local to the
    transaction of the session. `relaxed_order` can return rows slightly out of order, so the rows found are sorted
    again."""
    if limit < 1:
        return []
    expression = embedding_expression(profile)  # also checks the dimensions of the profile
    dimensions = profile.dimensions
    if len(vector) != dimensions:
        raise ValueError(f"the vector has {len(vector)} dimensions, the profile has {dimensions}")
    if not all(math.isfinite(number) for number in vector):
        raise ValueError("a vector holds a number that is not finite")
    await session.execute(
        text(
            "select set_config('hnsw.iterative_scan', 'relaxed_order', true),"
            " set_config('hnsw.max_scan_tuples', :tuples, true),"
            " set_config('hnsw.ef_search', :ef_search, true)"
        ),
        {"tuples": str(_MAX_SCAN_TUPLES), "ef_search": str(min(max(limit, _MIN_EF_SEARCH), _MAX_EF_SEARCH))},
    )
    sized = _SizedVector(dimensions)
    question = cast(bindparam("question", list(vector), type_=sized), sized)
    distance_expression = literal_column(f"chunks.{expression}").op("<=>", return_type=Float)(question)
    nearest = (
        _chunks(Chunk.id.label("id"), distance_expression.label("distance"))
        .where(*_scope(user, kb_id, profile.id))
        .order_by(distance_expression)
        .limit(limit)
        .subquery("nearest")
    )
    # `+ 0`: the outer order is not the index's expression, so it is a plain sort of the rows found
    found = select(nearest.c.id).order_by(nearest.c.distance + 0, nearest.c.id)
    return list((await session.scalars(found)).all())


def _clean_question(question: str) -> str:
    """The question as text PostgreSQL accepts: no NUL character and no lone surrogate (the driver refuses both)."""
    return question.replace("\x00", " ").encode("utf-8", "replace").decode("utf-8")


async def bm25_search(
    session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID, question: str, limit: int
) -> list[uuid.UUID]:
    """The ids of the `limit` chunks that best match the question by BM25 on the content and the section title, best
    first (ties by chunk id).

    The question is plain text: `|||` tokenizes it with the analyzer of each field (lower case, ASCII folding) and
    looks for any of the tokens, without the syntax of the query parser, so quotes, colons, brackets, asterisks and
    `AND`/`OR`/`NOT` are not operators and cannot raise an error. A question without indexable words matches nothing."""
    if limit < 1:
        return []
    words = cast(bindparam("question", _clean_question(question), type_=Text), Text)
    score = func.pdb.score(Chunk.id)
    query = (
        _chunks(Chunk.id)
        .where(
            or_(Chunk.content.op("|||")(words), Chunk.section_title.op("|||")(words)), *_scope(user, kb_id, profile_id)
        )
        .order_by(score.desc(), Chunk.id)
        .limit(limit)
    )
    return list((await session.scalars(query)).all())


def _first_ranks(ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Rank from 1 in the order of the list; a chunk repeated in the list keeps its first rank and counts once."""
    ranks: dict[uuid.UUID, int] = {}
    for chunk_id in ids:
        if chunk_id not in ranks:
            ranks[chunk_id] = len(ranks) + 1
    return ranks


def fuse(dense: Sequence[uuid.UUID], bm25: Sequence[uuid.UUID], k: int) -> list[Ranked]:
    """Reciprocal Rank Fusion: score(c) = sum of 1 / (k + rank(c)) over the lists c is in. Every chunk appears once.
    Highest score first; at equal score the better dense rank (a chunk that is not in the dense list comes after the
    ones that are), then the chunk id. The scores are exact fractions while they are compared, so ties are real ties;
    the one kept is their float."""
    if k < 1:
        raise ValueError("k must be at least 1")
    dense_ranks = _first_ranks(dense)
    bm25_ranks = _first_ranks(bm25)
    scores: dict[uuid.UUID, Fraction] = {}
    for chunk_id in dense_ranks.keys() | bm25_ranks.keys():
        score = Fraction(0)
        for ranks in (dense_ranks, bm25_ranks):
            if chunk_id in ranks:
                score += Fraction(1, k + ranks[chunk_id])
        scores[chunk_id] = score

    def order(chunk_id: uuid.UUID) -> tuple[Fraction, bool, int, uuid.UUID]:
        dense_rank = dense_ranks.get(chunk_id)
        return (-scores[chunk_id], dense_rank is None, dense_rank or 0, chunk_id)

    return [
        Ranked(chunk_id, dense_ranks.get(chunk_id), bm25_ranks.get(chunk_id), float(scores[chunk_id]))
        for chunk_id in sorted(scores, key=order)
    ]


async def load_candidates(
    session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID, ranked: Sequence[Ranked]
) -> list[Candidate]:
    """The chunks of `ranked`, in its order, with their document name and content, in one query with the same scope as
    the searches. A chunk that is gone or no longer in scope between the search and now (document deleted, no longer
    READY, KB no longer visible) is dropped."""
    if not ranked:
        return []
    query = _chunks(
        Chunk.id,
        Chunk.document_id,
        Document.filename,
        Chunk.section_title,
        Chunk.page_start,
        Chunk.page_end,
        Chunk.content,
    ).where(Chunk.id.in_([item.chunk_id for item in ranked]), *_scope(user, kb_id, profile_id))
    rows = {row.id: row for row in (await session.execute(query)).all()}
    return [
        Candidate(
            chunk_id=item.chunk_id,
            document_id=row.document_id,
            document_name=row.filename,
            section_title=row.section_title,
            page_start=row.page_start,
            page_end=row.page_end,
            content=row.content,
            dense_rank=item.dense_rank,
            bm25_rank=item.bm25_rank,
            score=item.score,
        )
        for item in ranked
        if (row := rows.get(item.chunk_id)) is not None
    ]


async def has_searchable_chunks(session: AsyncSession, user: User, kb_id: uuid.UUID, profile_id: uuid.UUID) -> bool:
    """True when the KB, as the user sees it, has at least one chunk of a READY document in the profile."""
    found = await session.scalar(select(exists(_chunks(Chunk.id).where(*_scope(user, kb_id, profile_id)))))
    return bool(found)
