# SPEC-2026-11-07-fase-2-ingestion
"""Storage of the chunks: AC-027, AC-028 (REQ-013, REQ-014); persist and verify halves of AC-029 and AC-030.

The pipeline-level halves of AC-029 (the document is FAILED) and AC-030 (the document is READY only after the check)
belong to T-05; here the same tests cover what the storage layer owes them: all the chunks of a document are saved
in one transaction with its profile or none is, a document that is gone is detected under the row lock, and the check
of the two indexes counts exactly the chunks of the document.

Real PostgreSQL of compose.test.yaml, fresh state per test. Rows are inserted with plain SQL where the test is about
what the database refuses; `persist_chunks` is used where the test is about it.
"""

from __future__ import annotations

import asyncio
import math
import random
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Chunk, IndexingProfile
from app.db.vector import Vector
from app.ingestion.chunker import ChunkDraft
from app.ingestion.persist import DocumentGone, persist_chunks, verify_indexes
from app.ingestion.profile import ensure_profile
from app.run import BACKEND_DIR, migrate
from tests.conftest import TEST_DATABASE_URL, make_settings

DIMENSIONS = 4


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no profiles, KBs, documents or chunks; the HNSW indexes of earlier tests are dropped."""
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table indexing_profiles, documents, knowledge_bases cascade"))
        names = await connection.scalars(
            text(
                "select indexname from pg_indexes"
                " where tablename = 'chunks' and indexname like 'chunks_embedding_hnsw%'"
            )
        )
        for name in names.all():
            await connection.execute(text(f'drop index "{name}"'))
    yield engine
    await engine.dispose()


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


def vector_for(index: int, dimensions: int = DIMENSIONS) -> list[float]:
    return [float(index + 1), 1.0, 0.5, 0.25][:dimensions] + [0.0] * max(0, dimensions - 4)


async def add_profile(engine: AsyncEngine, dimensions: int = DIMENSIONS, model: str = "model-a") -> uuid.UUID:
    """A profile inserted directly (no HNSW index), the way a test about the constraints needs it."""
    async with engine.begin() as connection:
        profile_id = await connection.scalar(
            text(
                "insert into indexing_profiles (embedding_model, dimensions, parser, chunker, bm25_config)"
                " values (:model, :dimensions, 'p', CAST('{}' AS jsonb), 'c') returning id"
            ),
            {"model": model, "dimensions": dimensions},
        )
    assert isinstance(profile_id, uuid.UUID)
    return profile_id


async def add_kb(engine: AsyncEngine, name: str) -> uuid.UUID:
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": name}
        )
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


async def add_document(engine: AsyncEngine, kb_id: uuid.UUID, status: str = "PROCESSING") -> uuid.UUID:
    has_reason = status in ("FAILED", "UNSUPPORTED")
    document_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count)"
                " values (:id, :kb_id, 'file.txt', 'txt', 1, :checksum, :status, :code, :message, :chunks)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_error" if has_reason else None,
                "message": "Docling could not read the file" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
            },
        )
    return document_id


CHUNK_SQL = (
    "insert into chunks (kb_id, document_id, profile_id, dimensions, position, section_title, content, word_count,"
    " embedding) values (:kb_id, :document_id, :profile_id, :dimensions, :position, :section_title, :content,"
    " :word_count, CAST(:embedding AS vector))"
)


async def insert_chunk(
    engine: AsyncEngine,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    profile_id: uuid.UUID,
    /,
    **overrides: Any,
) -> None:
    values: dict[str, Any] = {
        "kb_id": kb_id,
        "document_id": document_id,
        "profile_id": profile_id,
        "dimensions": DIMENSIONS,
        "position": 0,
        "section_title": None,
        "content": "testo del chunk",
        "word_count": 3,
        "embedding": literal(vector_for(0)),
    }
    values.update(overrides)
    async with engine.begin() as connection:
        await connection.execute(text(CHUNK_SQL), values)


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


async def chunk_count(engine: AsyncEngine) -> int:
    return int(await scalar(engine, "select count(*) from chunks"))


@dataclass
class Stored:
    """A KB with a PROCESSING document and the 4-dimension profile created the way the backend creates it."""

    kb_id: uuid.UUID
    document_id: uuid.UUID
    profile: IndexingProfile


@pytest.fixture
async def stored(engine: AsyncEngine) -> Stored:
    profile = await ensure_profile(engine, make_settings(embedding_model="model-a", embedding_dimensions=DIMENSIONS))
    kb_id = await add_kb(engine, "kb")
    return Stored(kb_id, await add_document(engine, kb_id), profile)


def drafts(count: int, prefix: str = "parola") -> list[ChunkDraft]:
    return [
        ChunkDraft(
            position=i,
            section_title=f"Sezione {i}" if i % 2 == 0 else None,
            page_start=i + 1 if i % 2 == 0 else None,
            page_end=i + 2 if i % 2 == 0 else None,
            content=f"{prefix}{i} testo del chunk numero {i}",
            word_count=6,
        )
        for i in range(count)
    ]


def vectors(count: int) -> list[list[float]]:
    return [vector_for(i) for i in range(count)]


async def persist(
    engine: AsyncEngine,
    stored: Stored,
    chunk_drafts: Sequence[ChunkDraft],
    chunk_vectors: Sequence[Sequence[float]],
) -> int:
    async with sessions(engine)() as session:
        return await persist_chunks(session, stored.document_id, stored.profile, chunk_drafts, chunk_vectors)


async def verify(engine: AsyncEngine, document_id: uuid.UUID, profile: IndexingProfile, expected: int) -> bool:
    async with sessions(engine)() as session:
        return await verify_indexes(session, document_id, profile, expected)


# AC-027: the database refuses a vector of the wrong size ------------------------------------------------------------


async def test_AC_027_a_chunk_with_a_vector_of_3_dimensions_is_refused_for_a_profile_of_4(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine, dimensions=4)
    kb_id = await add_kb(engine, "kb")
    document_id = await add_document(engine, kb_id)

    with pytest.raises(IntegrityError, match="chunks_embedding_dimensions_check"):
        await insert_chunk(engine, kb_id, document_id, profile_id, dimensions=4, embedding="[1,2,3]")

    assert await chunk_count(engine) == 0
    await insert_chunk(engine, kb_id, document_id, profile_id, dimensions=4, embedding="[1,2,3,4]")  # control
    assert await chunk_count(engine) == 1


async def test_AC_027_a_chunk_whose_dimensions_differ_from_its_profile_is_refused(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine, dimensions=4)
    kb_id = await add_kb(engine, "kb")
    document_id = await add_document(engine, kb_id)

    # Consistent with its own vector (3 = 3) but not with the profile (4).
    with pytest.raises(IntegrityError, match="chunks_profile_fkey"):
        await insert_chunk(engine, kb_id, document_id, profile_id, dimensions=3, embedding="[1,2,3]")

    assert await chunk_count(engine) == 0


async def test_AC_027_profiles_of_different_sizes_hold_their_own_vectors(engine: AsyncEngine) -> None:
    small = await add_profile(engine, dimensions=3, model="small")
    large = await add_profile(engine, dimensions=5, model="large")
    kb_id = await add_kb(engine, "kb")
    document_id = await add_document(engine, kb_id)

    await insert_chunk(engine, kb_id, document_id, small, dimensions=3, position=0, embedding="[1,2,3]")
    await insert_chunk(engine, kb_id, document_id, large, dimensions=5, position=1, embedding="[1,2,3,4,5]")
    with pytest.raises(IntegrityError, match="chunks_embedding_dimensions_check"):
        await insert_chunk(engine, kb_id, document_id, large, dimensions=5, position=2, embedding="[1,2,3]")

    assert await chunk_count(engine) == 2


async def test_AC_027_profile_rows_refuse_dimensions_outside_1_to_2000(engine: AsyncEngine) -> None:
    for dimensions in (0, 2001):
        with pytest.raises(IntegrityError, match="indexing_profiles_dimensions_check"):
            await add_profile(engine, dimensions=dimensions, model=f"m{dimensions}")


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"position": -1}, "chunks_position_check"),
        ({"word_count": -1}, "chunks_word_count_check"),
        ({"embedding": None}, "embedding"),
        ({"content": None}, "content"),
        ({"profile_id": uuid.uuid4()}, "chunks_profile_fkey"),
        ({"document_id": uuid.uuid4()}, "chunks_document_kb_fkey"),
        ({"kb_id": uuid.uuid4()}, "chunks_document_kb_fkey"),
    ],
)
async def test_the_database_refuses_chunks_that_break_the_table_constraints(
    engine: AsyncEngine, overrides: dict[str, Any], constraint: str
) -> None:
    profile_id = await add_profile(engine)
    kb_id = await add_kb(engine, "kb")
    document_id = await add_document(engine, kb_id)

    with pytest.raises(IntegrityError, match=constraint):
        await insert_chunk(engine, kb_id, document_id, profile_id, **overrides)

    assert await chunk_count(engine) == 0


async def test_the_position_is_unique_within_a_document(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    kb_id = await add_kb(engine, "kb")
    first = await add_document(engine, kb_id)
    second = await add_document(engine, kb_id)
    await insert_chunk(engine, kb_id, first, profile_id, position=0)

    with pytest.raises(IntegrityError, match="chunks_document_position_key"):
        await insert_chunk(engine, kb_id, first, profile_id, position=0)
    await insert_chunk(engine, kb_id, second, profile_id, position=0)  # another document: allowed

    assert await chunk_count(engine) == 2


async def test_deleting_a_document_or_a_kb_takes_its_chunks_but_a_profile_in_use_stays(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    kb_one, kb_two = await add_kb(engine, "one"), await add_kb(engine, "two")
    doc_one, doc_two, doc_three = (
        await add_document(engine, kb_one),
        await add_document(engine, kb_one),
        await add_document(engine, kb_two),
    )
    for kb_id, document_id in ((kb_one, doc_one), (kb_one, doc_two), (kb_two, doc_three)):
        await insert_chunk(engine, kb_id, document_id, profile_id)

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text("delete from indexing_profiles"))
    async with engine.begin() as connection:
        await connection.execute(text("delete from documents where id = :id"), {"id": doc_one})
    assert await chunk_count(engine) == 2
    async with engine.begin() as connection:
        await connection.execute(text("delete from knowledge_bases where id = :id"), {"id": kb_one})
    assert await chunk_count(engine) == 1
    assert await scalar(engine, "select document_id from chunks") == doc_three


# The Vector column type ---------------------------------------------------------------------------------------------


async def test_a_vector_survives_the_round_trip_through_the_orm(stored: Stored, engine: AsyncEngine) -> None:
    vector = [1.5, -2.0, 0.0, 3.25]
    async with sessions(engine)() as session:
        session.add(
            Chunk(
                kb_id=stored.kb_id,
                document_id=stored.document_id,
                profile_id=stored.profile.id,
                dimensions=DIMENSIONS,
                position=0,
                content="testo",
                word_count=1,
                embedding=vector,
            )
        )
        await session.commit()

    assert await scalar(engine, "select embedding::text from chunks") == "[1.5,-2,0,3.25]"
    async with sessions(engine)() as session:
        assert await session.scalar(select(Chunk.embedding)) == vector


async def test_the_vector_type_passes_null_through(engine: AsyncEngine) -> None:
    processor = Vector().bind_processor(engine.dialect)

    assert processor is not None
    assert processor(None) is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
async def test_the_vector_type_refuses_numbers_that_are_not_finite(engine: AsyncEngine, bad: float) -> None:
    processor = Vector().bind_processor(engine.dialect)
    assert processor is not None

    with pytest.raises(ValueError):
        processor([1.0, bad, 2.0])


async def test_a_vector_with_a_number_that_is_not_finite_is_never_stored(stored: Stored, engine: AsyncEngine) -> None:
    async with sessions(engine)() as session:
        session.add(
            Chunk(
                kb_id=stored.kb_id,
                document_id=stored.document_id,
                profile_id=stored.profile.id,
                dimensions=DIMENSIONS,
                position=0,
                content="testo",
                word_count=1,
                embedding=[1.0, math.nan, 2.0, 3.0],
            )
        )
        with pytest.raises(StatementError) as refused:
            await session.commit()

    assert isinstance(refused.value.orig, ValueError)
    assert await chunk_count(engine) == 0


# AC-028: BM25 filtered by KB and the two indexes --------------------------------------------------------------------


async def add_searchable(
    engine: AsyncEngine, profile_id: uuid.UUID, kb_name: str, content: str, section_title: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    kb_id = await add_kb(engine, kb_name)
    document_id = await add_document(engine, kb_id)
    await insert_chunk(engine, kb_id, document_id, profile_id, content=content, section_title=section_title)
    return kb_id, document_id


async def bm25_ids(engine: AsyncEngine, condition: str, **params: Any) -> list[uuid.UUID]:
    async with engine.connect() as connection:
        result = await connection.execute(text(f"select id from chunks where {condition} order by id"), params)
        return [row.id for row in result]


async def test_AC_028_bm25_search_filtered_by_kb_returns_only_the_chunks_of_that_kb(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    kb_one, doc_one = await add_searchable(engine, profile_id, "uno", "il trapezio del primo documento")
    kb_two, doc_two = await add_searchable(engine, profile_id, "due", "un trapezio nel secondo documento")
    await insert_chunk(engine, kb_two, doc_two, profile_id, position=1, content="altro testo senza la parola")

    everywhere = await bm25_ids(engine, "content @@@ 'trapezio'")
    in_one = await bm25_ids(engine, "content @@@ 'trapezio' and kb_id = :kb", kb=kb_one)
    in_two = await bm25_ids(engine, "content @@@ 'trapezio' and kb_id = :kb", kb=kb_two)
    in_doc_two = await bm25_ids(engine, "content @@@ 'trapezio' and document_id = :doc", doc=doc_two)

    assert len(everywhere) == 2
    assert len(in_one) == 1
    assert len(in_two) == 1
    assert {*in_one, *in_two} == set(everywhere)
    assert in_doc_two == in_two
    assert await scalar(engine, "select kb_id from chunks where id = :id", id=in_one[0]) == kb_one
    assert await scalar(engine, "select document_id from chunks where id = :id", id=in_one[0]) == doc_one


async def test_AC_028_bm25_has_no_stemming_and_ignores_case_and_accents(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    await add_searchable(engine, profile_id, "uno", "La CAFFETTERIA di Città", section_title="Menù del caffè")

    assert len(await bm25_ids(engine, "content @@@ 'caffetteria'")) == 1
    assert len(await bm25_ids(engine, "content @@@ 'caffetterie'")) == 0
    assert len(await bm25_ids(engine, "content @@@ 'citta'")) == 1
    assert len(await bm25_ids(engine, "section_title @@@ 'caffe'")) == 1
    assert len(await bm25_ids(engine, "section_title @@@ 'menu'")) == 1


async def test_AC_028_the_bm25_and_hnsw_indexes_exist_with_the_expected_fields(engine: AsyncEngine) -> None:
    profile = await ensure_profile(engine, make_settings(embedding_model="model-a", embedding_dimensions=DIMENSIONS))

    async with engine.connect() as connection:
        indexes = {
            row.indexname: row.indexdef
            for row in await connection.execute(
                text("select indexname, indexdef from pg_indexes where tablename = 'chunks'")
            )
        }
        bm25_columns = (
            await connection.scalars(
                text(
                    "select a.attname from pg_attribute a join pg_class c on c.oid = a.attrelid"
                    " where c.relname = 'chunks_bm25' and a.attnum > 0 order by a.attnum"
                )
            )
        ).all()
        bm25_options = await connection.scalar(text("select reloptions from pg_class where relname = 'chunks_bm25'"))

    bm25 = indexes["chunks_bm25"]
    assert "USING bm25" in bm25
    assert bm25_columns == ["id", "kb_id", "document_id", "content", "section_title"]
    assert bm25_options == ["key_field=id"]
    assert bm25.count("lowercase=true") == 2
    assert bm25.count("ascii_folding=true") == 2
    hnsw = indexes[f"chunks_embedding_hnsw_{profile.id.hex[:12]}"]
    assert "USING hnsw" in hnsw
    assert f"((embedding)::vector({DIMENSIONS})) vector_cosine_ops" in hnsw
    assert f"WHERE (profile_id = '{profile.id}'::uuid)" in hnsw


# AC-029 (storage half): one transaction -----------------------------------------------------------------------------


async def test_AC_029_persist_saves_every_chunk_with_the_profile_of_the_document(
    stored: Stored, engine: AsyncEngine
) -> None:
    before = await scalar(engine, "select updated_at from documents where id = :id", id=stored.document_id)
    chunk_drafts = drafts(5)

    inserted = await persist(engine, stored, chunk_drafts, vectors(5))

    assert inserted == 5
    async with engine.connect() as connection:  # another connection: the transaction is committed
        rows = (
            await connection.execute(
                text(
                    "select kb_id, profile_id, dimensions, position, section_title, page_start, page_end, content,"
                    " word_count, embedding::text as embedding from chunks where document_id = :id order by position"
                ),
                {"id": stored.document_id},
            )
        ).all()
        document = (
            await connection.execute(
                text("select status, chunk_count, profile_id, updated_at from documents where id = :id"),
                {"id": stored.document_id},
            )
        ).one()
    assert [row.position for row in rows] == [0, 1, 2, 3, 4]
    for row, draft, vector in zip(rows, chunk_drafts, vectors(5), strict=True):
        assert (row.kb_id, row.profile_id, row.dimensions) == (stored.kb_id, stored.profile.id, DIMENSIONS)
        assert (row.section_title, row.page_start, row.page_end) == (
            draft.section_title,
            draft.page_start,
            draft.page_end,
        )
        assert (row.content, row.word_count) == (draft.content, draft.word_count)
        assert [float(number) for number in row.embedding.strip("[]").split(",")] == vector
    assert document.profile_id == stored.profile.id
    assert document.updated_at > before
    assert (document.status, document.chunk_count) == ("PROCESSING", 0)  # READY and the count come later


async def test_AC_029_an_error_on_the_third_chunk_leaves_no_chunk_and_no_profile(
    stored: Stored, engine: AsyncEngine
) -> None:
    chunk_drafts = drafts(5)
    chunk_drafts[2] = ChunkDraft(2, None, None, None, "terzo chunk", word_count=-1)  # the database refuses it

    with pytest.raises(IntegrityError, match="chunks_word_count_check"):
        await persist(engine, stored, chunk_drafts, vectors(5))

    assert await chunk_count(engine) == 0
    assert await scalar(engine, "select profile_id from documents where id = :id", id=stored.document_id) is None
    assert await scalar(engine, "select status from documents where id = :id", id=stored.document_id) == "PROCESSING"


async def test_AC_029_persist_can_be_retried_after_a_failure(stored: Stored, engine: AsyncEngine) -> None:
    bad = drafts(3)
    bad[2] = ChunkDraft(2, None, None, None, "terzo chunk", word_count=-1)
    with pytest.raises(IntegrityError):
        await persist(engine, stored, bad, vectors(3))

    assert await persist(engine, stored, drafts(3), vectors(3)) == 3
    assert await chunk_count(engine) == 3


@pytest.mark.parametrize(
    "chunk_vectors",
    [
        vectors(4),  # fewer vectors than chunks
        vectors(6),  # more vectors than chunks
        [*vectors(2), [1.0, 2.0, 3.0], *vectors(2)],  # one vector of 3 dimensions for a profile of 4
        [*vectors(4), [1.0, 2.0, 3.0, 4.0, 5.0]],  # one vector of 5 dimensions
    ],
)
async def test_AC_029_vectors_that_do_not_match_the_chunks_or_the_profile_save_nothing(
    stored: Stored, engine: AsyncEngine, chunk_vectors: list[list[float]]
) -> None:
    with pytest.raises(ValueError):
        await persist(engine, stored, drafts(5), chunk_vectors)

    assert await chunk_count(engine) == 0
    assert await scalar(engine, "select profile_id from documents where id = :id", id=stored.document_id) is None


async def test_AC_029_persist_refuses_a_non_finite_vector_and_saves_nothing(
    stored: Stored, engine: AsyncEngine
) -> None:
    chunk_vectors = vectors(3)
    chunk_vectors[2] = [1.0, math.nan, 0.0, 0.0]

    with pytest.raises((ValueError, StatementError)):
        await persist(engine, stored, drafts(3), chunk_vectors)

    assert await chunk_count(engine) == 0
    assert await scalar(engine, "select profile_id from documents where id = :id", id=stored.document_id) is None


async def test_AC_029_a_document_that_does_not_exist_is_gone(stored: Stored, engine: AsyncEngine) -> None:
    async with sessions(engine)() as session:
        with pytest.raises(DocumentGone):
            await persist_chunks(session, uuid.uuid4(), stored.profile, drafts(2), vectors(2))

    assert await chunk_count(engine) == 0


async def test_AC_029_a_deleted_document_is_gone(stored: Stored, engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("delete from documents where id = :id"), {"id": stored.document_id})

    with pytest.raises(DocumentGone):
        await persist(engine, stored, drafts(2), vectors(2))

    assert await chunk_count(engine) == 0


@pytest.mark.parametrize("status", ["QUEUED", "READY", "FAILED", "UNSUPPORTED"])
async def test_AC_029_a_document_that_is_not_processing_is_gone(engine: AsyncEngine, status: str) -> None:
    profile = await ensure_profile(engine, make_settings(embedding_model="model-a", embedding_dimensions=DIMENSIONS))
    kb_id = await add_kb(engine, "kb")
    other = Stored(kb_id, await add_document(engine, kb_id, status=status), profile)

    with pytest.raises(DocumentGone):
        await persist(engine, other, drafts(2), vectors(2))

    assert await chunk_count(engine) == 0
    assert await scalar(engine, "select profile_id from documents where id = :id", id=other.document_id) is None
    assert await scalar(engine, "select status from documents where id = :id", id=other.document_id) == status


async def wait_for_blocked_persist(engine: AsyncEngine, seconds: float = 10.0) -> None:
    """Wait until a statement of the persist waits for a row lock (a condition, not a fixed delay)."""
    sql = text(
        "select count(*) from pg_stat_activity where datname = current_database() and wait_event_type = 'Lock'"
        " and query ilike '%for update%'"
    )
    async with asyncio.timeout(seconds):
        while True:
            async with engine.connect() as connection:
                if await connection.scalar(sql):
                    return
            await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("concurrent_change", "outcome"),
    [
        ("delete from documents where id = :id", "gone"),
        (
            "update documents set status = 'FAILED', status_reason_code = 'unexpected_error',"
            " status_reason_message = 'Interrupted' where id = :id",
            "gone",
        ),
        ("select id from documents where id = :id for update", "saved"),
    ],
)
async def test_AC_029_persist_waits_for_the_lock_of_the_document_and_looks_again(
    stored: Stored, engine: AsyncEngine, concurrent_change: str, outcome: str
) -> None:
    """A concurrent deletion (or change of status) holds the row: the persist waits, then sees the new state."""
    other = engine.connect()
    async with other as connection:
        await connection.execute(text(concurrent_change), {"id": stored.document_id})
        task = asyncio.create_task(persist(engine, stored, drafts(3), vectors(3)))
        try:
            await wait_for_blocked_persist(engine)
            assert not task.done()
        finally:
            await connection.commit()

        if outcome == "gone":
            with pytest.raises(DocumentGone):
                await task
            assert await chunk_count(engine) == 0
        else:
            assert await task == 3
            assert await chunk_count(engine) == 3


# AC-030 (index half): the check counts exactly the chunks of the document -------------------------------------------


async def test_AC_030_verify_indexes_is_true_for_the_chunks_just_saved(stored: Stored, engine: AsyncEngine) -> None:
    await persist(engine, stored, drafts(5), vectors(5))

    assert await verify(engine, stored.document_id, stored.profile, 5) is True


async def test_AC_030_verify_indexes_is_false_when_more_chunks_are_expected_than_saved(
    stored: Stored, engine: AsyncEngine
) -> None:
    await persist(engine, stored, drafts(5), vectors(5))

    assert await verify(engine, stored.document_id, stored.profile, 6) is False


async def test_AC_030_verify_indexes_is_false_when_fewer_chunks_are_expected_than_saved(
    stored: Stored, engine: AsyncEngine
) -> None:
    await persist(engine, stored, drafts(5), vectors(5))

    assert await verify(engine, stored.document_id, stored.profile, 4) is False


async def test_AC_030_verify_indexes_is_false_for_a_document_without_chunks(
    stored: Stored, engine: AsyncEngine
) -> None:
    assert await verify(engine, stored.document_id, stored.profile, 1) is False


async def test_AC_030_verify_indexes_counts_only_the_chunks_of_the_document(
    stored: Stored, engine: AsyncEngine
) -> None:
    other_document = await add_document(engine, stored.kb_id)
    await persist(engine, stored, drafts(3), vectors(3))
    await persist(engine, Stored(stored.kb_id, other_document, stored.profile), drafts(2, prefix="altro"), vectors(2))

    assert await verify(engine, stored.document_id, stored.profile, 3) is True
    assert await verify(engine, other_document, stored.profile, 2) is True
    assert await verify(engine, stored.document_id, stored.profile, 5) is False


async def test_AC_030_verify_indexes_is_false_when_the_vector_search_finds_fewer_chunks(
    stored: Stored, engine: AsyncEngine
) -> None:
    """BM25 finds the 3 chunks, but they belong to another profile than the one the check searches with."""
    await persist(engine, stored, drafts(3), vectors(3))
    other_profile = await ensure_profile(
        engine, make_settings(embedding_model="model-b", embedding_dimensions=DIMENSIONS)
    )

    assert await verify(engine, stored.document_id, other_profile, 3) is False


# A chunk belongs to the KB of its document, enforced by the database ------------------------------------------------


async def test_a_chunk_whose_kb_differs_from_the_kb_of_its_document_is_refused(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    kb_id, other_kb = await add_kb(engine, "kb"), await add_kb(engine, "other")
    document_id = await add_document(engine, kb_id)

    with pytest.raises(IntegrityError, match="chunks_document_kb_fkey"):
        await insert_chunk(engine, other_kb, document_id, profile_id)  # an existing KB, but not the document's

    assert await chunk_count(engine) == 0
    await insert_chunk(engine, kb_id, document_id, profile_id)  # control: the document's own KB
    assert await chunk_count(engine) == 1


async def test_the_kb_of_a_chunk_cannot_be_changed_away_from_its_document(engine: AsyncEngine) -> None:
    profile_id = await add_profile(engine)
    kb_id, other_kb = await add_kb(engine, "kb"), await add_kb(engine, "other")
    document_id = await add_document(engine, kb_id)
    await insert_chunk(engine, kb_id, document_id, profile_id)

    with pytest.raises(IntegrityError, match="chunks_document_kb_fkey"):
        async with engine.begin() as connection:
            await connection.execute(text("update chunks set kb_id = :kb"), {"kb": other_kb})

    assert await scalar(engine, "select kb_id from chunks") == kb_id


async def test_the_kb_and_document_keys_of_chunks_are_indexed(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        indexes = {
            row.indexname: row.indexdef
            for row in await connection.execute(
                text("select indexname, indexdef from pg_indexes where tablename in ('chunks', 'documents')")
            )
        }
        constraints = (
            await connection.scalars(text("select conname from pg_constraint where conrelid = 'documents'::regclass"))
        ).all()

    assert "(kb_id)" in indexes["chunks_kb_id_idx"]
    assert "documents_id_kb_id_key" in constraints


# AC-030 (index half), part 2: the vector check does not depend on the plan ------------------------------------------

BIG = 1024


def random_vector(rng: random.Random, dimensions: int = BIG) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]


async def add_big_document(
    engine: AsyncEngine, profile: IndexingProfile, kb_id: uuid.UUID, count: int, seed: int
) -> uuid.UUID:
    document_id = await add_document(engine, kb_id)
    rng = random.Random(seed)
    await persist(
        engine,
        Stored(kb_id, document_id, profile),
        drafts(count, prefix=f"d{seed}x"),
        [random_vector(rng) for _ in range(count)],
    )
    return document_id


@pytest.fixture
async def big_profile(engine: AsyncEngine) -> IndexingProfile:
    return await ensure_profile(engine, make_settings(embedding_model="big-model", embedding_dimensions=BIG))


async def test_AC_030_verify_indexes_is_true_for_a_document_with_many_chunks_among_others(
    engine: AsyncEngine, big_profile: IndexingProfile
) -> None:
    """More chunks than the default `hnsw.ef_search` (40), other documents of the same profile, statistics present."""
    kb_id = await add_kb(engine, "kb")
    await add_big_document(engine, big_profile, kb_id, 120, seed=1)
    target = await add_big_document(engine, big_profile, kb_id, 300, seed=2)
    await add_big_document(engine, big_profile, kb_id, 120, seed=3)
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("analyze chunks"))

    assert await verify(engine, target, big_profile, 300) is True


async def test_AC_030_verify_indexes_is_true_right_after_the_commit_without_statistics(
    engine: AsyncEngine, big_profile: IndexingProfile
) -> None:
    kb_id = await add_kb(engine, "kb")
    await add_big_document(engine, big_profile, kb_id, 60, seed=4)
    target = await add_big_document(engine, big_profile, kb_id, 100, seed=5)

    assert await verify(engine, target, big_profile, 100) is True


async def test_AC_030_verify_indexes_is_true_when_the_planner_can_only_use_the_hnsw_index(
    engine: AsyncEngine, big_profile: IndexingProfile
) -> None:
    """pgvector filters by document after the HNSW scan, which returns `hnsw.ef_search` (40) rows by default: with the
    other access paths unavailable the check still has to find all the chunks of the document."""
    kb_id = await add_kb(engine, "kb")
    await add_big_document(engine, big_profile, kb_id, 100, seed=6)
    target = await add_big_document(engine, big_profile, kb_id, 300, seed=7)
    await add_big_document(engine, big_profile, kb_id, 100, seed=8)
    hnsw = f"chunks_embedding_hnsw_{big_profile.id.hex[:12]}"
    scans = text("select coalesce(sum(idx_scan), 0) from pg_stat_user_indexes where indexrelname = :name")
    before = await scalar(engine, str(scans), name=hnsw)

    async with sessions(engine)() as session:
        # Transactional DDL and settings: undone by the rollback, other tests and connections never see them.
        await session.execute(text("alter table chunks drop constraint chunks_document_position_key"))
        await session.execute(text("set local enable_seqscan = off"))
        await session.execute(text("set local enable_bitmapscan = off"))

        result = await verify_indexes(session, target, big_profile, 300)

        await session.execute(text("select pg_stat_force_next_flush()"))  # publish this session's index scans
        await session.rollback()
    after = await scalar(engine, str(scans), name=hnsw)
    assert after > before, "the check did not go through the HNSW index"
    assert result is True


async def test_AC_030_the_settings_of_the_check_do_not_leak_into_the_session(
    engine: AsyncEngine, big_profile: IndexingProfile
) -> None:
    kb_id = await add_kb(engine, "kb")
    target = await add_big_document(engine, big_profile, kb_id, 50, seed=9)

    async with sessions(engine)() as session:
        before = await session.scalar(text("select current_setting('hnsw.iterative_scan')"))
        assert await verify_indexes(session, target, big_profile, 50) is True
        after = await session.scalar(text("select current_setting('hnsw.iterative_scan')"))
        await session.rollback()
    assert after == before


# AC-038 and AC-039 (storage side): deleting a KB while its document is being saved ----------------------------------


async def wait_until_blocked(engine: AsyncEngine, statement: str, seconds: float = 10.0) -> None:
    """Wait until a statement whose text contains `statement` waits for a lock (a condition, not a fixed delay)."""
    sql = text(
        "select count(*) from pg_stat_activity where datname = current_database() and wait_event_type = 'Lock'"
        " and query ilike :pattern"
    )
    async with asyncio.timeout(seconds):
        while True:
            async with engine.connect() as connection:
                if await connection.scalar(sql, {"pattern": f"%{statement}%"}):
                    return
            await asyncio.sleep(0.05)


PAUSE_KEY = 7303


@pytest.fixture
async def paused_inserts(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Every insert into chunks waits for the shared advisory lock PAUSE_KEY, which the yielded connection holds
    exclusively until the test unlocks it: the persist stays in the middle of its inserts, with the document locked."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "create function t03_pause_insert() returns trigger language plpgsql as $$ begin"
                " perform pg_advisory_lock_shared(7303); perform pg_advisory_unlock_shared(7303); return new; end $$"
            )
        )
        await connection.execute(
            text("create trigger t03_pause before insert on chunks for each row execute function t03_pause_insert()")
        )
    holder = await engine.connect()
    await holder.execute(text("select pg_advisory_lock(:key)"), {"key": PAUSE_KEY})
    try:
        yield holder
    finally:
        await holder.execute(text("select pg_advisory_unlock_all()"))
        await holder.close()
        async with engine.begin() as connection:
            await connection.execute(text("drop trigger t03_pause on chunks"))
            await connection.execute(text("drop function t03_pause_insert()"))


async def test_AC_038_AC_039_deleting_the_kb_while_the_chunks_are_saved_does_not_deadlock(
    stored: Stored, engine: AsyncEngine, paused_inserts: AsyncConnection
) -> None:
    async def delete_kb() -> None:
        async with engine.begin() as connection:
            await connection.execute(text("delete from knowledge_bases where id = :id"), {"id": stored.kb_id})

    saving = asyncio.create_task(persist(engine, stored, drafts(3), vectors(3)))
    await wait_until_blocked(engine, "insert into chunks")  # the document row is locked, the insert is paused
    deleting = asyncio.create_task(delete_kb())
    await wait_until_blocked(engine, "delete from knowledge_bases")  # waits for the document row
    await paused_inserts.execute(text("select pg_advisory_unlock(:key)"), {"key": PAUSE_KEY})

    saved, deleted = await asyncio.gather(saving, deleting, return_exceptions=True)

    assert deleted is None, f"the deletion failed: {deleted!r}"  # no DeadlockDetected
    assert saved == 3 or isinstance(saved, DocumentGone), f"the persist failed: {saved!r}"
    assert await chunk_count(engine) == 0  # saved before the deletion, then taken away with the document
    assert await scalar(engine, "select count(*) from documents") == 0
    assert await scalar(engine, "select count(*) from knowledge_bases") == 0


async def test_AC_038_AC_039_a_kb_deleted_before_the_save_leaves_no_chunk(stored: Stored, engine: AsyncEngine) -> None:
    other = engine.connect()
    async with other as connection:
        await connection.execute(text("delete from knowledge_bases where id = :id"), {"id": stored.kb_id})
        task = asyncio.create_task(persist(engine, stored, drafts(3), vectors(3)))
        try:
            await wait_until_blocked(engine, "for update")
        finally:
            await connection.commit()

        with pytest.raises(DocumentGone):
            await task
    assert await chunk_count(engine) == 0


# Migration 0004: upgrade, downgrade, upgrade ------------------------------------------------------------------------


def alembic_config(database_url: Any) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = database_url
    return config


async def scratch_database(action: str, name: str) -> None:
    engine = create_async_engine(make_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        if action == "create":
            await connection.execute(text(f'create database "{name}"'))
        else:
            await connection.execute(text(f'drop database if exists "{name}" with (force)'))
    await engine.dispose()


async def schema_state(url: Any) -> dict[str, Any]:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        tables = (await connection.scalars(text("select tablename from pg_tables where schemaname = 'public'"))).all()
        indexes = (await connection.scalars(text("select indexname from pg_indexes where schemaname = 'public'"))).all()
        constraints = (
            await connection.scalars(
                text("select conname from pg_constraint where connamespace = 'public'::regnamespace")
            )
        ).all()
        profile_column = await connection.scalar(
            text(
                "select count(*) from information_schema.columns"
                " where table_name = 'documents' and column_name = 'profile_id'"
            )
        )
        version = await connection.scalar(text("select version_num from alembic_version"))
    await engine.dispose()
    return {
        "tables": set(tables),
        "indexes": set(indexes),
        "constraints": set(constraints),
        "profile_column": profile_column,
        "version": version,
    }


def test_migration_0004_upgrades_downgrades_and_upgrades_again() -> None:
    name = f"rag_migration_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DATABASE_URL).set(database=name)
    config = alembic_config(url)
    asyncio.run(scratch_database("create", name))
    try:
        command.upgrade(config, "head")
        first = asyncio.run(schema_state(url))
        command.downgrade(config, "0003_documents")
        down = asyncio.run(schema_state(url))
        command.upgrade(config, "head")
        second = asyncio.run(schema_state(url))
    finally:
        asyncio.run(scratch_database("drop", name))

    created = {"chunks", "indexing_profiles"}
    assert created <= first["tables"]
    assert {"chunks_bm25", "chunks_kb_id_idx", "chunks_pkey"} <= first["indexes"]
    assert {"documents_id_kb_id_key", "chunks_document_kb_fkey", "chunks_embedding_dimensions_check"} <= first[
        "constraints"
    ]
    assert first["profile_column"] == 1

    assert down["version"] == "0003_documents"
    assert not created & down["tables"]
    assert "documents" in down["tables"]
    assert down["profile_column"] == 0
    assert not {"chunks_bm25", "chunks_kb_id_idx"} & down["indexes"]
    assert "documents_id_kb_id_key" not in down["constraints"]

    assert second == first
