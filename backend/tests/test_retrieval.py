# SPEC-2026-11-17-fase-3-chat
"""Hybrid retrieval limited to the authorized KB, to READY documents and to the active profile: AC-006, AC-007, AC-008
and AC-010 (REQ-003, REQ-005), against the real PostgreSQL (ParadeDB) of compose.test.yaml.

The searches are called directly (not through an API): the access check is in the same SQL statement as the search, so
a user who is not assigned to the KB gets nothing from `dense_search`, `bm25_search`, `load_candidates` and
`has_searchable_chunks`, whatever ids are passed. The rows are inserted with plain SQL, with vectors the test chooses.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.chat.retrieval import (
    Candidate,
    Ranked,
    bm25_search,
    dense_search,
    fuse,
    has_searchable_chunks,
    load_candidates,
)
from app.db.models import IndexingProfile, User
from app.ingestion.profile import ensure_profile
from app.run import migrate
from tests.conftest import make_settings
from tests.test_knowledge_bases import ADMIN, ADMIN_PASSWORD, add_kb, add_user, assign

DIMENSIONS = 4
QUESTION_VECTOR = [1.0, 0.0, 0.0, 0.0]

CHUNK_SQL = (
    "insert into chunks (id, kb_id, document_id, profile_id, dimensions, position, section_title, page_start, page_end,"
    " content, word_count, embedding) values (:id, :kb_id, :document_id, :profile_id, :dimensions, :position,"
    " :section_title, :page_start, :page_end, :content, :word_count, CAST(:embedding AS vector))"
)


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no users, KBs, documents, chunks or profiles; the HNSW indexes of earlier tests go."""
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("truncate table kb_members, users, indexing_profiles, documents, knowledge_bases cascade")
        )
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


def literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


async def add_document(
    engine: AsyncEngine, kb_id: uuid.UUID, status: str = "READY", filename: str = "manuale.pdf"
) -> uuid.UUID:
    has_reason = status in ("FAILED", "UNSUPPORTED")
    document_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count)"
                " values (:id, :kb_id, :filename, 'pdf', 1, :checksum, :status, :code, :message, :chunks)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "filename": filename,
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_error" if has_reason else None,
                "message": "Docling could not read the file" if has_reason else None,
                "chunks": 1 if status == "READY" else 0,
            },
        )
    return document_id


@dataclass
class Row:
    content: str
    vector: Sequence[float] = field(default_factory=lambda: list(QUESTION_VECTOR))
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


async def add_chunks(
    engine: AsyncEngine, kb_id: uuid.UUID, document_id: uuid.UUID, profile: IndexingProfile, *rows: Row
) -> list[uuid.UUID]:
    """Insert the rows as chunks of the document (positions 0, 1, ...) and return their ids."""
    async with engine.begin() as connection:
        await connection.execute(
            text(CHUNK_SQL),
            [
                {
                    "id": row.id,
                    "kb_id": kb_id,
                    "document_id": document_id,
                    "profile_id": profile.id,
                    "dimensions": profile.dimensions,
                    "position": position,
                    "section_title": row.section_title,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "content": row.content,
                    "word_count": len(row.content.split()),
                    "embedding": literal(row.vector),
                }
                for position, row in enumerate(rows)
            ],
        )
    return [row.id for row in rows]


async def add_chunk(
    engine: AsyncEngine,
    kb_id: uuid.UUID,
    profile: IndexingProfile,
    row: Row,
    status: str = "READY",
    filename: str = "manuale.pdf",
) -> uuid.UUID:
    document_id = await add_document(engine, kb_id, status, filename)
    (chunk_id,) = await add_chunks(engine, kb_id, document_id, profile, row)
    return chunk_id


@dataclass
class World:
    """kb_a and kb_b; mario is assigned to kb_a only, luigi to kb_b only, peach to none, anna is the admin.
    `profile` is the active profile (4 dimensions, with its HNSW index), `other` another one (with its own index)."""

    engine: AsyncEngine
    kb_a: uuid.UUID
    kb_b: uuid.UUID
    profile: IndexingProfile
    other: IndexingProfile
    users: dict[str, uuid.UUID]

    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def user(self, session: AsyncSession, username: str) -> User:
        user = await session.get(User, self.users[username])
        assert user is not None
        return user


@pytest.fixture
async def world(engine: AsyncEngine) -> World:
    profile = await ensure_profile(engine, make_settings(embedding_model="model-a", embedding_dimensions=DIMENSIONS))
    other = await ensure_profile(engine, make_settings(embedding_model="model-b", embedding_dimensions=DIMENSIONS))
    assert profile.id != other.id
    kb_a = await add_kb(engine, "Manuali")
    kb_b = await add_kb(engine, "Procedure")
    users = {
        "anna": await add_user(engine, ADMIN, ADMIN_PASSWORD, role="admin"),
        "mario": await add_user(engine, "mario"),
        "luigi": await add_user(engine, "luigi"),
        "peach": await add_user(engine, "peach"),
    }
    await assign(engine, kb_a, users["mario"])
    await assign(engine, kb_b, users["luigi"])
    return World(engine, kb_a, kb_b, profile, other, users)


async def dense(world: World, username: str, kb_id: uuid.UUID, vector: Sequence[float] = QUESTION_VECTOR, **kw: Any):
    async with world.sessions()() as session:
        user = await world.user(session, username)
        return await dense_search(session, user, kb_id, kw.get("profile", world.profile), vector, kw.get("limit", 50))


async def bm25(world: World, username: str, kb_id: uuid.UUID, question: str, **kw: Any) -> list[uuid.UUID]:
    async with world.sessions()() as session:
        user = await world.user(session, username)
        return await bm25_search(
            session, user, kb_id, kw.get("profile_id", world.profile.id), question, kw.get("limit", 50)
        )


TEXT = "Il prezzo comprende IVA al 22%"


# AC-006: isolation, called directly ---------------------------------------------------------------------------------


async def test_AC_006_a_user_not_assigned_to_the_kb_gets_nothing_from_either_search(world: World) -> None:
    """Two KBs with a chunk of the same text each; mario is assigned to the first only."""
    chunk_a = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    chunk_b = await add_chunk(world.engine, world.kb_b, world.profile, Row(TEXT))

    assert await dense(world, "mario", world.kb_b) == []
    assert await bm25(world, "mario", world.kb_b, "prezzo IVA") == []
    assert await dense(world, "mario", world.kb_a) == [chunk_a]
    assert await bm25(world, "mario", world.kb_a, "prezzo IVA") == [chunk_a]
    assert chunk_b not in await dense(world, "mario", world.kb_a)


async def test_AC_006_a_user_assigned_to_no_kb_gets_nothing_anywhere(world: World) -> None:
    await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    await add_chunk(world.engine, world.kb_b, world.profile, Row(TEXT))

    for kb_id in (world.kb_a, world.kb_b):
        assert await dense(world, "peach", kb_id) == []
        assert await bm25(world, "peach", kb_id, "prezzo IVA") == []


async def test_AC_006_load_candidates_and_has_searchable_chunks_do_not_leak_either(world: World) -> None:
    """The ids of chunks of a KB the user cannot see load nothing, and the KB looks empty."""
    chunk_a = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    chunk_b = await add_chunk(world.engine, world.kb_b, world.profile, Row(TEXT))
    ranked = [Ranked(chunk_b, 1, 1, 0.5), Ranked(chunk_a, 2, None, 0.1)]

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        on_b = await load_candidates(session, mario, world.kb_b, world.profile.id, ranked)
        on_a = await load_candidates(session, mario, world.kb_a, world.profile.id, ranked)
        assert on_b == []
        assert [c.chunk_id for c in on_a] == [chunk_a]  # the chunk of kb_b is not loaded through kb_a either
        assert await has_searchable_chunks(session, mario, world.kb_b, world.profile.id) is False
        assert await has_searchable_chunks(session, mario, world.kb_a, world.profile.id) is True


async def test_AC_006_a_chunk_of_another_kb_is_never_returned_for_the_kb_asked(world: World) -> None:
    """Even the admin, who sees every KB, gets only the chunks of the KB that the call names."""
    chunk_a = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    chunk_b = await add_chunk(world.engine, world.kb_b, world.profile, Row(TEXT))

    assert await dense(world, "anna", world.kb_a) == [chunk_a]
    assert await dense(world, "anna", world.kb_b) == [chunk_b]
    assert await bm25(world, "anna", world.kb_a, "prezzo") == [chunk_a]
    assert await bm25(world, "anna", world.kb_b, "prezzo") == [chunk_b]


@pytest.mark.parametrize("role", ["Admin", "ADMIN", " admin", "admin ", "administrator", "superuser", ""])
async def test_AC_006_only_the_exact_admin_role_reads_every_kb(world: World, role: str) -> None:
    """Fail closed: any other role value falls back to the assignments (mario's id here: kb_a only)."""
    chunk_a = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    await add_chunk(world.engine, world.kb_b, world.profile, Row(TEXT))
    impostor = User(id=world.users["mario"], username="x", password_hash="x", role=role)
    stranger = User(id=uuid.uuid4(), username="y", password_hash="y", role=role)

    async with world.sessions()() as session:
        assert await dense_search(session, impostor, world.kb_a, world.profile, QUESTION_VECTOR, 50) == [chunk_a]
        assert await dense_search(session, impostor, world.kb_b, world.profile, QUESTION_VECTOR, 50) == []
        assert await bm25_search(session, impostor, world.kb_b, world.profile.id, "prezzo", 50) == []
        for kb_id in (world.kb_a, world.kb_b):
            assert await dense_search(session, stranger, kb_id, world.profile, QUESTION_VECTOR, 50) == []
            assert await bm25_search(session, stranger, kb_id, world.profile.id, "prezzo", 50) == []
            assert await has_searchable_chunks(session, stranger, kb_id, world.profile.id) is False


async def test_AC_006_an_absent_kb_returns_nothing_to_the_admin_too(world: World) -> None:
    await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    absent = uuid.uuid4()

    assert await dense(world, "anna", absent) == []
    assert await bm25(world, "anna", absent, "prezzo") == []
    async with world.sessions()() as session:
        anna = await world.user(session, "anna")
        assert await has_searchable_chunks(session, anna, absent, world.profile.id) is False


async def test_AC_006_a_kb_deleted_after_the_chunks_were_saved_returns_nothing(world: World) -> None:
    await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    async with world.engine.begin() as connection:
        await connection.execute(text("delete from knowledge_bases where id = :id"), {"id": world.kb_a})

    assert await dense(world, "anna", world.kb_a) == []
    assert await bm25(world, "anna", world.kb_a, "prezzo") == []


async def test_AC_006_a_member_removed_from_the_kb_loses_the_chunks(world: World) -> None:
    chunk_a = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    assert await dense(world, "mario", world.kb_a) == [chunk_a]
    async with world.engine.begin() as connection:
        await connection.execute(text("delete from kb_members where user_id = :id"), {"id": world.users["mario"]})

    assert await dense(world, "mario", world.kb_a) == []
    assert await bm25(world, "mario", world.kb_a, "prezzo") == []


class Statements:
    """The SQL statements an engine sends while the block runs."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine.sync_engine
        self.sent: list[str] = []

    def record(self, _conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool) -> None:
        self.sent.append(statement)

    def __enter__(self) -> Statements:
        event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.record)

    def reading_chunks(self) -> list[str]:
        return [sql for sql in self.sent if "chunks" in sql.lower()]


@pytest.mark.parametrize(("username", "table"), [("mario", "kb_members"), ("anna", "knowledge_bases")])
async def test_AC_006_every_statement_that_reads_chunks_carries_the_access_check(
    world: World, username: str, table: str
) -> None:
    """Not a second query to check access first: the check is in the SQL statement that reads the chunks."""
    chunk = await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))
    ranked = [Ranked(chunk, 1, 1, 0.5)]

    async with world.sessions()() as session:
        user = await world.user(session, username)
        with Statements(world.engine) as statements:
            await dense_search(session, user, world.kb_a, world.profile, QUESTION_VECTOR, 50)
            await bm25_search(session, user, world.kb_a, world.profile.id, "prezzo", 50)
            await load_candidates(session, user, world.kb_a, world.profile.id, ranked)
            await has_searchable_chunks(session, user, world.kb_a, world.profile.id)

    reading = statements.reading_chunks()
    assert len(reading) == 4
    assert all(table in sql for sql in reading), reading


# AC-007: READY documents of the active profile only ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["QUEUED", "PROCESSING", "FAILED", "UNSUPPORTED"])
async def test_AC_007_chunks_of_a_document_that_is_not_ready_are_not_returned(world: World, status: str) -> None:
    ready = await add_chunk(world.engine, world.kb_a, world.profile, Row("garanzia due anni sul prodotto"))
    await add_chunk(world.engine, world.kb_a, world.profile, Row("garanzia tre anni sul prodotto"), status=status)

    assert await dense(world, "mario", world.kb_a) == [ready]
    assert await bm25(world, "mario", world.kb_a, "garanzia prodotto") == [ready]


async def test_AC_007_the_context_holds_only_chunks_of_the_ready_document_of_the_active_profile(world: World) -> None:
    """A READY, a PROCESSING and a FAILED document with chunks, and a READY one of another profile: a question with the
    words of the four finds, fuses and loads only the chunk of the READY document of the active profile."""
    kb = world.kb_a
    ready = await add_chunk(world.engine, kb, world.profile, Row("alfa comune testo"), filename="pronto.pdf")
    await add_chunk(world.engine, kb, world.profile, Row("beta comune testo"), status="PROCESSING")
    await add_chunk(world.engine, kb, world.profile, Row("gamma comune testo"), status="FAILED")
    await add_chunk(world.engine, kb, world.other, Row("delta comune testo"), filename="altro-profilo.pdf")

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        dense_ids = await dense_search(session, mario, kb, world.profile, QUESTION_VECTOR, 50)
        bm25_ids = await bm25_search(session, mario, kb, world.profile.id, "alfa beta gamma delta comune", 50)
        ranked = fuse(dense_ids, bm25_ids, 60)
        candidates = await load_candidates(session, mario, kb, world.profile.id, ranked)

    assert dense_ids == [ready]
    assert bm25_ids == [ready]
    assert [c.chunk_id for c in candidates] == [ready]
    assert candidates[0].document_name == "pronto.pdf"


async def test_AC_007_chunks_of_another_profile_are_not_returned_by_either_search(world: World) -> None:
    mine = await add_chunk(world.engine, world.kb_a, world.profile, Row("manutenzione ordinaria"))
    theirs = await add_chunk(world.engine, world.kb_a, world.other, Row("manutenzione ordinaria"))

    assert await dense(world, "mario", world.kb_a) == [mine]
    assert await bm25(world, "mario", world.kb_a, "manutenzione") == [mine]
    assert await dense(world, "mario", world.kb_a, profile=world.other) == [theirs]
    assert await bm25(world, "mario", world.kb_a, "manutenzione", profile_id=world.other.id) == [theirs]


async def test_AC_007_load_candidates_drops_chunks_of_documents_that_are_not_ready_or_of_another_profile(
    world: World,
) -> None:
    ready = await add_chunk(world.engine, world.kb_a, world.profile, Row("testo uno"))
    processing = await add_chunk(world.engine, world.kb_a, world.profile, Row("testo due"), status="PROCESSING")
    other = await add_chunk(world.engine, world.kb_a, world.other, Row("testo tre"))
    ranked = [Ranked(processing, 1, None, 0.3), Ranked(other, 2, None, 0.2), Ranked(ready, 3, None, 0.1)]

    async with world.sessions()() as session:
        anna = await world.user(session, "anna")
        candidates = await load_candidates(session, anna, world.kb_a, world.profile.id, ranked)

    assert [c.chunk_id for c in candidates] == [ready]


async def test_AC_007_has_searchable_chunks_needs_a_ready_document_in_the_active_profile(world: World) -> None:
    kb = world.kb_a
    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        assert await has_searchable_chunks(session, mario, kb, world.profile.id) is False  # empty KB

    await add_chunk(world.engine, kb, world.profile, Row("testo"), status="PROCESSING")
    await add_chunk(world.engine, kb, world.profile, Row("testo"), status="FAILED")
    await add_chunk(world.engine, kb, world.other, Row("testo"))  # READY, but another profile
    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        assert await has_searchable_chunks(session, mario, kb, world.profile.id) is False
        assert await has_searchable_chunks(session, mario, kb, world.other.id) is True

    await add_chunk(world.engine, kb, world.profile, Row("testo"))
    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        assert await has_searchable_chunks(session, mario, kb, world.profile.id) is True


# AC-008: the filters do not shrink the result of the vector search ---------------------------------------------------


async def force_hnsw_scan(session: AsyncSession) -> None:
    """For the rest of the transaction the planner may not scan the table, use a bitmap or sort the rows: the only way
    left to read the nearest rows in order is the HNSW index, as it does on a table too large for a sort."""
    await session.execute(
        text(
            "select set_config('enable_seqscan', 'off', true), set_config('enable_bitmapscan', 'off', true),"
            " set_config('enable_sort', 'off', true)"
        )
    )


def close_to_question(offset: float) -> list[float]:
    """A vector at an angle from the question that grows with `offset` (cosine distance grows with it)."""
    return [1.0, offset, 0.0, 0.0]


async def test_AC_008_sixty_chunks_of_the_active_profile_and_sixty_closer_ones_of_another_profile(world: World) -> None:
    """The vector search asks for 50: it returns the 50 nearest chunks of the active profile, in distance order, not
    fewer because the nearest rows of the other profile were found first and filtered out."""
    document = await add_document(world.engine, world.kb_a)
    mine = await add_chunks(
        world.engine,
        world.kb_a,
        document,
        world.profile,
        *(Row(f"attivo {i}", close_to_question(0.1 + 0.01 * i)) for i in range(60)),
    )
    other_document = await add_document(world.engine, world.kb_a)
    theirs = await add_chunks(
        world.engine,
        world.kb_a,
        other_document,
        world.other,
        *(Row(f"altro {i}", close_to_question(0.0001 * i)) for i in range(60)),
    )

    found = await dense(world, "mario", world.kb_a, limit=50)

    assert len(found) == 50
    assert not set(found) & set(theirs)
    assert found == mine[:50]


async def test_AC_008_rows_filtered_after_the_index_scan_do_not_shrink_the_result(world: World) -> None:
    """The same with the filters that the HNSW scan applies after it: 60 distant chunks in the KB asked, and nearer
    ones of the same profile in another KB and in documents that are not READY. The index is forced, where
    `hnsw.ef_search` alone would return the 50 nearest rows of the whole index and nothing of this KB."""
    mine = await add_chunks(
        world.engine,
        world.kb_a,
        await add_document(world.engine, world.kb_a),
        world.profile,
        *(Row(f"lontano {i}", close_to_question(0.3 + 0.01 * i)) for i in range(60)),
    )
    await add_chunks(
        world.engine,
        world.kb_b,
        await add_document(world.engine, world.kb_b),
        world.profile,
        *(Row(f"altra kb {i}", close_to_question(0.002 * i)) for i in range(60)),
    )
    await add_chunks(
        world.engine,
        world.kb_a,
        await add_document(world.engine, world.kb_a, "PROCESSING"),
        world.profile,
        *(Row(f"in corso {i}", close_to_question(0.12 + 0.002 * i)) for i in range(60)),
    )

    async with world.sessions()() as session:
        await force_hnsw_scan(session)
        anna = await world.user(session, "anna")
        found = await dense_search(session, anna, world.kb_a, world.profile, QUESTION_VECTOR, 50)

    assert len(found) == 50
    assert found == mine[:50]


async def test_AC_008_the_limit_is_honoured_and_fewer_chunks_than_the_limit_are_all_returned(world: World) -> None:
    document = await add_document(world.engine, world.kb_a)
    chunks = await add_chunks(
        world.engine,
        world.kb_a,
        document,
        world.profile,
        *(Row(f"punto {i}", close_to_question(0.1 * i)) for i in range(10)),
    )

    assert await dense(world, "mario", world.kb_a, limit=3) == chunks[:3]
    assert await dense(world, "mario", world.kb_a, limit=1) == chunks[:1]
    assert await dense(world, "mario", world.kb_a, limit=50) == chunks
    assert await dense(world, "mario", world.kb_a, limit=0) == []


async def test_AC_008_a_limit_above_the_default_ef_search_returns_that_many(world: World) -> None:
    """150 asked of the index (the default `hnsw.ef_search` is 40): 150 returned, the nearest ones in order."""
    chunks = await add_chunks(
        world.engine,
        world.kb_a,
        await add_document(world.engine, world.kb_a),
        world.profile,
        *(Row(f"punto {i}", close_to_question(0.01 * i)) for i in range(200)),
    )

    async with world.sessions()() as session:
        await force_hnsw_scan(session)
        mario = await world.user(session, "mario")
        found = await dense_search(session, mario, world.kb_a, world.profile, QUESTION_VECTOR, 150)

    assert found == chunks[:150]


async def test_AC_008_equal_distances_are_ordered_by_chunk_id(world: World) -> None:
    rows = [Row(f"uguale {i}") for i in range(6)]
    await add_chunks(world.engine, world.kb_a, await add_document(world.engine, world.kb_a), world.profile, *rows)

    assert await dense(world, "mario", world.kb_a) == sorted(row.id for row in rows)


async def test_AC_008_a_vector_with_the_wrong_number_of_dimensions_is_refused(world: World) -> None:
    await add_chunk(world.engine, world.kb_a, world.profile, Row(TEXT))

    with pytest.raises(ValueError):
        await dense(world, "mario", world.kb_a, vector=[1.0, 0.0, 0.0])


async def test_AC_008_a_kb_without_documents_gives_empty_lists(world: World) -> None:
    assert await dense(world, "anna", world.kb_a) == []
    assert await bm25(world, "anna", world.kb_a, "qualsiasi parola") == []


# AC-010: the question is text for BM25 -------------------------------------------------------------------------------

HOSTILE = [
    '"prezzo": (IVA) AND *',
    "NOT",
    "C++ / C#?",
    ":::",
    "",
    "   ",
    '"unbalanced quote',
    "(((",
    "AND OR NOT",
    "prezzo:iva",
    "title:foo^2 ~3",
    "\\",
    "'; drop table chunks; --",
    "a\x00b",
    "caff\ud800e",
    "\n\t",
    "{[]}",
    "*",
    "?",
    "-prezzo +iva",
    "ΑΒΓ 中文 🙂",
]


async def corpus(world: World) -> dict[str, uuid.UUID]:
    kb = world.kb_a
    document = await add_document(world.engine, kb)
    rows = {
        "prezzo": Row("Il prezzo comprende IVA al 22% per tutti gli articoli"),
        "other": Row("La consegna avviene entro cinque giorni lavorativi"),
        "cpp": Row("Il linguaggio C++ è veloce"),
        "operators": Row("regole: AND, OR e NOT sono parole come le altre"),
        "accents": Row("Perché la città è già qui, con un buon caffè"),
        "title": Row("Contenuto senza la parola cercata", section_title="Garanzia legale"),
    }
    ids = await add_chunks(world.engine, kb, document, world.profile, *rows.values())
    return dict(zip(rows, ids, strict=True))


@pytest.mark.parametrize("question", HOSTILE, ids=[repr(q) for q in HOSTILE])
async def test_AC_010_hostile_questions_do_not_raise(world: World, question: str) -> None:
    await corpus(world)

    found = await bm25(world, "mario", world.kb_a, question)

    assert isinstance(found, list)
    assert all(isinstance(chunk_id, uuid.UUID) for chunk_id in found)


async def test_AC_010_the_first_question_finds_the_chunk_with_prezzo_and_iva(world: World) -> None:
    ids = await corpus(world)

    found = await bm25(world, "mario", world.kb_a, '"prezzo": (IVA) AND *')

    assert found[0] == ids["prezzo"]
    assert ids["other"] not in found


@pytest.mark.parametrize("question", ["", "   ", ":::", "*", "?", "\n\t", "{[]}"])
async def test_AC_010_a_question_without_indexable_words_gives_an_empty_list(world: World, question: str) -> None:
    await corpus(world)

    assert await bm25(world, "mario", world.kb_a, question) == []


async def test_AC_010_operators_are_plain_words(world: World) -> None:
    """AND, OR and NOT are searched as words: the chunk that contains them is found, none is excluded or required."""
    ids = await corpus(world)

    assert await bm25(world, "mario", world.kb_a, "NOT") == [ids["operators"]]
    assert await bm25(world, "mario", world.kb_a, "AND") == [ids["operators"]]
    found = await bm25(world, "mario", world.kb_a, "prezzo AND consegna NOT")
    assert set(found) == {ids["prezzo"], ids["other"], ids["operators"]}


async def test_AC_010_special_characters_do_not_change_the_meaning_of_the_search(world: World) -> None:
    ids = await corpus(world)

    plain = await bm25(world, "mario", world.kb_a, "prezzo IVA")
    decorated = await bm25(world, "mario", world.kb_a, '"prezzo": (IVA) * ?')

    assert plain == decorated == [ids["prezzo"]]
    assert await bm25(world, "mario", world.kb_a, "C++ / C#?") == [ids["cpp"]]
    assert await bm25(world, "mario", world.kb_a, "title:garanzia") == [ids["title"]]  # 'title' is just a word here


async def test_AC_010_the_analyzer_ignores_case_and_accents(world: World) -> None:
    ids = await corpus(world)

    for question in ("CAFFE", "caffè", "Caffè", "caffe"):
        assert await bm25(world, "mario", world.kb_a, question) == [ids["accents"]], question
    assert await bm25(world, "mario", world.kb_a, "PERCHE CITTA GIA") == [ids["accents"]]
    assert await bm25(world, "mario", world.kb_a, "perché città già") == [ids["accents"]]


async def test_AC_010_the_section_title_is_searched_too(world: World) -> None:
    ids = await corpus(world)

    assert await bm25(world, "mario", world.kb_a, "garanzia") == [ids["title"]]
    assert await bm25(world, "mario", world.kb_a, "GARANZIA legale") == [ids["title"]]


async def test_AC_010_better_matches_come_first_and_equal_scores_are_ordered_by_chunk_id(world: World) -> None:
    short = Row("garanzia")
    long = Row("garanzia " + " ".join(f"parola{i}" for i in range(40)))
    twins = [Row("sconto") for _ in range(5)]
    await add_chunks(
        world.engine, world.kb_a, await add_document(world.engine, world.kb_a), world.profile, long, short, *twins
    )

    assert await bm25(world, "mario", world.kb_a, "garanzia") == [short.id, long.id]
    assert await bm25(world, "mario", world.kb_a, "sconto") == sorted(row.id for row in twins)


async def test_AC_010_the_limit_is_honoured(world: World) -> None:
    rows = [Row(f"tema comune {i}") for i in range(12)]
    await add_chunks(world.engine, world.kb_a, await add_document(world.engine, world.kb_a), world.profile, *rows)

    assert len(await bm25(world, "mario", world.kb_a, "tema", limit=5)) == 5
    assert len(await bm25(world, "mario", world.kb_a, "tema", limit=1)) == 1
    assert len(await bm25(world, "mario", world.kb_a, "tema", limit=50)) == 12
    assert await bm25(world, "mario", world.kb_a, "tema", limit=0) == []


async def test_AC_010_a_very_long_question_is_searched_as_text(world: World) -> None:
    ids = await corpus(world)

    found = await bm25(world, "mario", world.kb_a, "prezzo IVA " + "riempitivo " * 600)

    assert found == [ids["prezzo"]]


# load_candidates ------------------------------------------------------------------------------------------------------


async def test_load_candidates_keeps_the_order_of_ranked_and_the_fields_of_the_chunk(world: World) -> None:
    """AC-007 (the context): one query by ids, in the order of `ranked`, with the document name and the ranks."""
    document = await add_document(world.engine, world.kb_a, filename="guida utente.pdf")
    first, second, third = await add_chunks(
        world.engine,
        world.kb_a,
        document,
        world.profile,
        Row("primo testo", section_title="Introduzione", page_start=1, page_end=2),
        Row("secondo testo", section_title=None, page_start=None, page_end=None),
        Row("terzo testo", section_title="Fine", page_start=9, page_end=9),
    )
    ranked = [Ranked(third, 1, None, 0.5), Ranked(first, None, 1, 0.25), Ranked(second, 2, 2, 0.125)]

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        candidates = await load_candidates(session, mario, world.kb_a, world.profile.id, ranked)

    assert candidates == [
        Candidate(third, document, "guida utente.pdf", "Fine", 9, 9, "terzo testo", 1, None, 0.5),
        Candidate(first, document, "guida utente.pdf", "Introduzione", 1, 2, "primo testo", None, 1, 0.25),
        Candidate(second, document, "guida utente.pdf", None, None, None, "secondo testo", 2, 2, 0.125),
    ]


async def test_load_candidates_drops_the_chunks_that_disappeared_in_between(world: World) -> None:
    document = await add_document(world.engine, world.kb_a)
    kept, deleted = await add_chunks(world.engine, world.kb_a, document, world.profile, Row("uno"), Row("due"))
    ranked = [Ranked(deleted, 1, 1, 0.5), Ranked(kept, 2, 2, 0.25), Ranked(uuid.uuid4(), 3, None, 0.1)]
    async with world.engine.begin() as connection:
        await connection.execute(text("delete from chunks where id = :id"), {"id": deleted})

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        candidates = await load_candidates(session, mario, world.kb_a, world.profile.id, ranked)
        assert [c.chunk_id for c in candidates] == [kept]
        assert await load_candidates(session, mario, world.kb_a, world.profile.id, []) == []


async def test_load_candidates_returns_nothing_for_a_document_deleted_in_between(world: World) -> None:
    document = await add_document(world.engine, world.kb_a)
    (chunk,) = await add_chunks(world.engine, world.kb_a, document, world.profile, Row("uno"))
    async with world.engine.begin() as connection:
        await connection.execute(text("delete from documents where id = :id"), {"id": document})

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        assert await load_candidates(session, mario, world.kb_a, world.profile.id, [Ranked(chunk, 1, 1, 0.5)]) == []


async def test_a_full_retrieval_round_through_the_five_functions(world: World) -> None:
    """AC-009 on real rows: dense and BM25 lists fuse into the order the spec's formula gives."""
    document = await add_document(world.engine, world.kb_a)
    near, middle, far = await add_chunks(
        world.engine,
        world.kb_a,
        document,
        world.profile,
        Row("vicino alla domanda ma senza parola chiave", close_to_question(0.1)),
        Row("contiene la parola chiave zafferano", close_to_question(0.5)),
        Row("zafferano zafferano zafferano", close_to_question(2.0)),
    )

    async with world.sessions()() as session:
        mario = await world.user(session, "mario")
        dense_ids = await dense_search(session, mario, world.kb_a, world.profile, QUESTION_VECTOR, 50)
        bm25_ids = await bm25_search(session, mario, world.kb_a, world.profile.id, "zafferano", 50)
        assert dense_ids == [near, middle, far]
        assert set(bm25_ids) == {middle, far}
        ranked = fuse(dense_ids, bm25_ids, 60)
        candidates = await load_candidates(session, mario, world.kb_a, world.profile.id, ranked)

    assert bm25_ids == [far, middle]  # three occurrences in three words beat one in six
    assert [r.chunk_id for r in ranked] == [far, middle, near]  # 1/63 + 1/61 > 1/62 + 1/62 > 1/61
    assert [c.chunk_id for c in candidates] == [far, middle, near]
    assert [(c.dense_rank, c.bm25_rank) for c in candidates] == [(3, 1), (2, 2), (1, None)]
