"""Saving the chunks of a document in one transaction and checking the indexes (REQ-014, REQ-015)."""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, IndexingProfile
from app.db.vector import Vector
from app.ingestion.chunker import ChunkDraft
from app.ingestion.profile import embedding_expression

# Upper bound of the rows an iterative HNSW scan may visit to fill the LIMIT (pgvector's default is 20000).
_MAX_SCAN_TUPLES = 1_000_000


class DocumentGone(Exception):
    """The document was deleted, or is no longer PROCESSING, while the worker was preparing its chunks."""


async def persist_chunks(
    session: AsyncSession,
    document_id: uuid.UUID,
    profile: IndexingProfile,
    drafts: Sequence[ChunkDraft],
    vectors: Sequence[Sequence[float]],
) -> int:
    """Save all the chunks of the document and set its profile in one transaction, and commit it; return how many
    chunks were saved. The document row is locked first (a concurrent deletion waits, then takes the chunks away with
    the document): if it is gone or not PROCESSING, DocumentGone. On any error the transaction is rolled back, so no
    chunk of the document is visible and its profile is unchanged, and the error propagates. The session's current
    transaction, if any, is part of this one.

    Lock order: the document row, then the profile row (key share, by the foreign key). The chunks do not reference the
    KB row, so the deletion of the KB (which locks the KB row and then its documents) cannot deadlock with this save."""
    try:
        locked = await session.execute(
            select(Document.id, Document.kb_id)
            .where(Document.id == document_id, Document.status == "PROCESSING")
            .with_for_update()
        )
        row = locked.one_or_none()
        if row is None:
            raise DocumentGone(str(document_id))
        if len(drafts) != len(vectors):
            raise ValueError(f"{len(drafts)} chunks but {len(vectors)} vectors")
        for vector in vectors:
            if len(vector) != profile.dimensions:
                raise ValueError(f"a vector has {len(vector)} dimensions, the profile has {profile.dimensions}")
            if not all(math.isfinite(number) for number in vector):
                raise ValueError("a vector holds a number that is not finite")
        if drafts:
            await session.execute(
                insert(Chunk),
                [
                    {
                        "kb_id": row.kb_id,
                        "document_id": document_id,
                        "profile_id": profile.id,
                        "dimensions": profile.dimensions,
                        "position": draft.position,
                        "section_title": draft.section_title,
                        "page_start": draft.page_start,
                        "page_end": draft.page_end,
                        "content": draft.content,
                        "word_count": draft.word_count,
                        "embedding": list(vector),
                    }
                    for draft, vector in zip(drafts, vectors, strict=True)
                ],
            )
        await session.execute(
            update(Document).where(Document.id == document_id).values(profile_id=profile.id, updated_at=func.now())
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return len(drafts)


async def verify_indexes(
    session: AsyncSession, document_id: uuid.UUID, profile: IndexingProfile, expected: int
) -> bool:
    """True when both indexes return exactly `expected` chunks of the document: the BM25 index (all chunks of the
    document) and a vector search by cosine distance for the first chunk's own vector, filtered by document and
    profile, with the expression of the profile's HNSW index. Run it after the commit of persist_chunks."""
    if expected < 1:
        return False
    bm25 = await session.scalar(
        text("select count(*) from chunks where id @@@ pdb.all() and document_id = :document_id"),
        {"document_id": document_id},
    )
    if bm25 != expected:
        return False
    query = await session.scalar(
        select(Chunk.embedding)
        .where(Chunk.document_id == document_id, Chunk.profile_id == profile.id)
        .order_by(Chunk.position)
        .limit(1)
    )
    if query is None:
        return False
    expression = embedding_expression(profile)
    # pgvector applies the WHERE of an HNSW scan after it, to the `hnsw.ef_search` (default 40) nearest rows: without
    # iterative scanning a document of more than 40 chunks would be missed whenever the planner picks the HNSW index.
    # The settings are local to a savepoint that is rolled back, so they do not stay in the caller's transaction.
    nested = await session.begin_nested()
    try:
        await session.execute(
            text(
                "select set_config('hnsw.iterative_scan', 'relaxed_order', true),"
                " set_config('hnsw.max_scan_tuples', :tuples, true)"
            ),
            {"tuples": str(_MAX_SCAN_TUPLES)},
        )
        found = await session.scalar(
            text(
                f"select count(*) from (select id from chunks where document_id = :document_id"
                f" and profile_id = :profile_id order by {expression} <=> CAST(:query AS vector) limit :expected)"
                f" as nearest"
            ).bindparams(bindparam("query", type_=Vector())),
            {"document_id": document_id, "profile_id": profile.id, "query": query, "expected": expected},
        )
    finally:
        await nested.rollback()
    return bool(found == expected)
