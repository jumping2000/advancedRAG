# SPEC-2026-11-07-fase-2-ingestion
"""A TXT or MD document from QUEUED to READY: AC-015 (REQ-007), and AC-034 for the original deleted at READY.

The pipeline (`process_document`) runs against real PostgreSQL of compose.test.yaml and the fake Ollama on a local
socket (never the real services), with fresh state per test and an upload folder inside pytest's tmp_path. Also here:
the cases of a document that cannot be processed (not UTF-8 text, no text, a format that needs Docling, Ollama down,
a missing original): the document becomes FAILED with a reason, no chunk of it exists and its original is kept; the
job that finds the document deleted or not QUEUED does nothing; and the RQ entry point `process_document_job`, run by
a real RQ worker from the real queue.

Interim rules until T-05, so that no document stays in an unexplained state: a format that needs Docling is FAILED
`unexpected_error`, and a text with no chunk is FAILED `parse_failed` (T-05 makes them a Docling call and UNSUPPORTED).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from redis import Redis as SyncRedis
from rq import Queue, SimpleWorker
from rq.utils import import_attribute
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Chunk, IndexingProfile
from app.documents.queue import INGESTION_QUEUE, PROCESS_DOCUMENT_JOB, enqueue_document
from app.ingestion import jobs, pipeline
from app.ingestion.jobs import process_document_job
from app.ingestion.pipeline import process_document
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_ollama, fake_vector

DIMENSIONS = 8
MODEL = "test-model"
GUIDE = (
    "# Guida\n\nQuesta guida spiega come usare il sistema.\n\n"
    "## Installazione\n\nSi scarica il pacchetto e si avvia il programma.\n\n"
    "Poi si apre il browser e si accede con il proprio utente.\n\n"
    "## Uso\n\nSi carica un documento e si attende lo stato Pronto.\n"
)
STORY = "Prima riga del racconto.\n\nSeconda parte del racconto, con accenti: è à ò ù.\n\nTerza e ultima parte.\n"


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


async def reset_database() -> None:
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
    await engine.dispose()


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no profiles, KBs, documents or chunks; the HNSW indexes of earlier tests are dropped."""
    await reset_database()
    engine = create_async_engine(make_settings().sqlalchemy_url)
    yield engine
    await engine.dispose()


def settings_for(upload_dir: Path, ollama_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upload_dir": upload_dir,
        "ollama_base_url": ollama_url,
        "embedding_model": MODEL,
        "embedding_dimensions": DIMENSIONS,
    }
    values.update(overrides)
    return make_settings(**values)


def put_original(upload_dir: Path, document_id: uuid.UUID, content: bytes) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / str(document_id)).write_bytes(content)


def files_in(folder: Path) -> list[Path]:
    return list(folder.iterdir())


async def add_document(
    engine: AsyncEngine,
    upload_dir: Path,
    content: bytes | None,
    fmt: str = "txt",
    status: str = "QUEUED",
) -> uuid.UUID:
    """A document in a new KB with its original in the upload folder (none when `content` is None)."""
    document_id = uuid.uuid4()
    has_reason = status in ("FAILED", "UNSUPPORTED")
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": uuid.uuid4().hex}
        )
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count)"
                " values (:id, :kb_id, :filename, :format, 1, :checksum, :status, :code, :message, :chunks)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "filename": f"file.{fmt}",
                "format": fmt,
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_error" if has_reason else None,
                "message": "Docling could not read the file" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
            },
        )
    if content is not None:
        put_original(upload_dir, document_id, content)
    return document_id


async def state_of(engine: AsyncEngine, document_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text(
                "select status, status_reason_code, status_reason_message, chunk_count, profile_id"
                " from documents where id = :id"
            ),
            {"id": document_id},
        )
        return dict(row.one()._mapping)


async def chunks_of(engine: AsyncEngine, document_id: uuid.UUID) -> list[Chunk]:
    async with async_sessionmaker[AsyncSession](engine, expire_on_commit=False)() as session:
        query = select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.position)
        return list((await session.scalars(query)).all())


async def profile_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text("select count(*) from indexing_profiles")) or 0)


# QUEUED -> PROCESSING -> READY ---------------------------------------------------------------------------------------


async def test_AC_015_a_txt_goes_from_queued_through_processing_to_ready(engine: AsyncEngine, tmp_path: Path) -> None:
    # Ollama answers after half a second, which keeps the document PROCESSING long enough to be seen from outside.
    with fake_ollama(dimensions=DIMENSIONS, delay_seconds=0.5) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, STORY.encode())
        assert (await state_of(engine, document_id))["status"] == "QUEUED"

        job = asyncio.create_task(process_document(document_id, settings))
        seen = {"QUEUED"}
        async with asyncio.timeout(20):
            while not job.done():
                seen.add((await state_of(engine, document_id))["status"])
                await asyncio.sleep(0.02)
        await job

    assert "PROCESSING" in seen
    assert seen <= {"QUEUED", "PROCESSING", "READY"}
    state = await state_of(engine, document_id)
    chunks = await chunks_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["status_reason_code"] is None
    assert state["status_reason_message"] is None
    assert len(chunks) >= 1
    assert state["chunk_count"] == len(chunks)
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.section_title for chunk in chunks} == {None}
    assert "Seconda parte del racconto, con accenti: è à ò ù." in "\n".join(chunk.content for chunk in chunks)


async def test_AC_015_a_markdown_goes_to_ready_with_the_section_titles_and_the_profile_of_the_settings(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, GUIDE.encode(), fmt="md")

        await process_document(document_id, settings)

    state = await state_of(engine, document_id)
    chunks = await chunks_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["status_reason_code"] is None
    assert state["chunk_count"] == len(chunks) == 3
    assert [chunk.section_title for chunk in chunks] == ["Guida", "Guida > Installazione", "Guida > Uso"]
    async with async_sessionmaker[AsyncSession](engine, expire_on_commit=False)() as session:
        profile = await session.scalar(select(IndexingProfile))
    assert profile is not None
    assert (profile.embedding_model, profile.dimensions) == (MODEL, DIMENSIONS)
    assert state["profile_id"] == profile.id
    assert {chunk.profile_id for chunk in chunks} == {profile.id}
    assert {chunk.kb_id for chunk in chunks} == {await kb_of(engine, document_id)}
    for chunk in chunks:
        assert chunk.embedding == pytest.approx(fake_vector(chunk.content, DIMENSIONS), abs=1e-6)


async def kb_of(engine: AsyncEngine, document_id: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as connection:
        kb_id = await connection.scalar(text("select kb_id from documents where id = :id"), {"id": document_id})
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


@pytest.mark.parametrize(("fmt", "content"), [("txt", STORY), ("md", GUIDE)])
async def test_AC_034_the_original_is_deleted_when_the_document_is_ready(
    engine: AsyncEngine, tmp_path: Path, fmt: str, content: str
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, content.encode(), fmt=fmt)
        assert (tmp_path / str(document_id)).is_file()

        await process_document(document_id, settings)

    assert (await state_of(engine, document_id))["status"] == "READY"
    assert files_in(tmp_path) == []


# A job that has nothing to do ----------------------------------------------------------------------------------------


async def test_AC_015_a_document_deleted_while_queued_makes_the_job_a_no_op(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, STORY.encode())
        async with engine.begin() as connection:
            await connection.execute(text("delete from documents where id = :id"), {"id": document_id})

        await process_document(document_id, settings)

    assert ollama.requests == []
    assert await chunks_of(engine, document_id) == []
    assert await profile_count(engine) == 0


@pytest.mark.parametrize("status", ["PROCESSING", "READY", "FAILED", "UNSUPPORTED"])
async def test_AC_015_a_document_that_is_not_queued_is_left_as_it_is(
    engine: AsyncEngine, tmp_path: Path, status: str
) -> None:
    """The job is idempotent: a second delivery, or a document already taken by another worker, changes nothing."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, STORY.encode(), status=status)
        before = await state_of(engine, document_id)

        await process_document(document_id, settings)

    assert await state_of(engine, document_id) == before
    assert ollama.requests == []
    assert await chunks_of(engine, document_id) == []
    assert (tmp_path / str(document_id)).is_file()


# A document that cannot be processed ---------------------------------------------------------------------------------


async def assert_failed_without_chunks(
    engine: AsyncEngine, tmp_path: Path, document_id: uuid.UUID, code: str, original_kept: bool = True
) -> dict[str, Any]:
    state = await state_of(engine, document_id)
    assert state["status"] == "FAILED"
    assert state["status_reason_code"] == code
    assert state["status_reason_message"]
    assert state["chunk_count"] == 0
    assert await chunks_of(engine, document_id) == []
    assert (tmp_path / str(document_id)).is_file() is original_kept
    return state


@pytest.mark.parametrize("fmt", ["txt", "md"])
async def test_AC_033_a_text_that_is_not_valid_utf8_is_failed_parse_failed_and_the_original_is_kept(
    engine: AsyncEngine, tmp_path: Path, fmt: str
) -> None:
    latin1 = "Perché la città è già così".encode("latin-1")  # not UTF-8
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, b"Un testo valido.\n\n" + latin1, fmt=fmt)

        await process_document(document_id, settings)

    state = await assert_failed_without_chunks(engine, tmp_path, document_id, "parse_failed")
    assert state["status_reason_message"] == "The file is not valid UTF-8 text"
    assert ollama.requests == []


@pytest.mark.parametrize("fmt", ["txt", "md"])
@pytest.mark.parametrize("content", [b"   \n\n \t \n", b"\x00\x01\x02 \x0b\x0c"])
async def test_AC_015_a_text_without_any_chunk_is_failed_parse_failed_for_now(
    engine: AsyncEngine, tmp_path: Path, fmt: str, content: bytes
) -> None:
    """Interim (until T-05, which makes it UNSUPPORTED): nothing could be read, and the admin is told so."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, content, fmt=fmt)

        await process_document(document_id, settings)

    state = await assert_failed_without_chunks(engine, tmp_path, document_id, "parse_failed")
    assert state["status_reason_message"] == "No text could be read"
    assert ollama.requests == []


@pytest.mark.parametrize("fmt", ["pdf", "docx", "pptx", "xlsx"])
async def test_AC_015_a_format_that_needs_docling_is_failed_unexpected_error_for_now(
    engine: AsyncEngine, tmp_path: Path, fmt: str
) -> None:
    """Interim (until T-05, which sends these formats to Docling): the document gets a state and a reason."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, b"%PDF-1.7 not read", fmt=fmt)

        await process_document(document_id, settings)

    state = await assert_failed_without_chunks(engine, tmp_path, document_id, "unexpected_error")
    assert state["status_reason_message"] == "Docling parsing is not available yet"
    assert ollama.requests == []


async def test_AC_015_ollama_unreachable_fails_the_document_with_its_reason_and_keeps_the_original(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, closed_port_url())
    document_id = await add_document(engine, tmp_path, STORY.encode())

    await process_document(document_id, settings, sleep=no_sleep)

    await assert_failed_without_chunks(engine, tmp_path, document_id, "embedding_unreachable")


async def test_AC_015_an_embedding_error_on_the_second_group_leaves_no_chunk_of_the_document(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The chunks are saved only after every group has its vectors, so a failure half way saves nothing."""
    with fake_ollama(dimensions=DIMENSIONS, fail_status=500, fail_from_request=2) as ollama:
        settings = settings_for(
            tmp_path, ollama.url, embedding_batch_size=2
        )  # the 3 chunks of GUIDE: groups of 2 and 1
        document_id = await add_document(engine, tmp_path, GUIDE.encode(), fmt="md")

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 2
    await assert_failed_without_chunks(engine, tmp_path, document_id, "embedding_error")


async def test_AC_015_a_vector_of_the_wrong_size_fails_the_document_and_saves_no_chunk(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Ollama answers vectors of 5 numbers for a profile of 8. The exact reason code is T-05's (AC-025)."""
    with fake_ollama(dimensions=5) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, STORY.encode())

        await process_document(document_id, settings)

    state = await state_of(engine, document_id)
    assert state["status"] == "FAILED"
    assert state["status_reason_code"]
    assert state["status_reason_message"]
    assert await chunks_of(engine, document_id) == []
    assert (tmp_path / str(document_id)).is_file()


async def test_AC_015_a_missing_original_fails_the_document_with_unexpected_error(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, None)

        await process_document(document_id, settings)

    await assert_failed_without_chunks(engine, tmp_path, document_id, "unexpected_error", original_kept=False)
    assert ollama.requests == []


async def test_AC_015_chunks_that_fail_the_index_check_are_removed_and_the_document_is_failed(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index check cannot be made to fail from outside without damaging the shared test database, so it is the
    one place where the function is replaced: the document must be FAILED `verification_failed`, with the chunks it had
    already saved removed and the original kept."""

    async def indexes_miss_a_chunk(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(pipeline, "verify_indexes", indexes_miss_a_chunk)
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, STORY.encode())

        await process_document(document_id, settings)

    assert len(ollama.requests) == 1
    await assert_failed_without_chunks(engine, tmp_path, document_id, "verification_failed")


# The RQ entry point --------------------------------------------------------------------------------------------------


def test_AC_015_the_queue_names_the_job_function_of_the_worker() -> None:
    assert INGESTION_QUEUE == "ingestion"
    assert import_attribute(PROCESS_DOCUMENT_JOB) is process_document_job


async def test_AC_015_process_document_job_takes_the_id_as_a_string_and_runs_the_pipeline(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        monkeypatch.setattr(jobs, "get_settings", lambda: settings_for(tmp_path, ollama.url))
        document_id = await add_document(engine, tmp_path, STORY.encode())

        # asyncio.run inside the job needs a thread without a running event loop
        await asyncio.to_thread(process_document_job, str(document_id))

    state = await state_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["chunk_count"] == len(await chunks_of(engine, document_id)) >= 1
    assert not (tmp_path / str(document_id)).exists()


def test_AC_015_a_real_rq_worker_takes_the_enqueued_document_from_queued_to_ready(
    schema: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path of a job: enqueue_document puts it on the `ingestion` queue, an RQ SimpleWorker (the class of the
    worker service) runs it from the real queue, the document is READY. Sync, because the job owns its event loop."""
    asyncio.run(reset_database())
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    queue = Queue(INGESTION_QUEUE, connection=connection)
    queue.empty()

    async def prepare() -> uuid.UUID:
        engine = create_async_engine(make_settings().sqlalchemy_url)
        try:
            return await add_document(engine, tmp_path, STORY.encode())
        finally:
            await engine.dispose()

    async def read_state(document_id: uuid.UUID) -> dict[str, Any]:
        engine = create_async_engine(make_settings().sqlalchemy_url)
        try:
            return await state_of(engine, document_id)
        finally:
            await engine.dispose()

    try:
        with fake_ollama(dimensions=DIMENSIONS) as ollama:
            monkeypatch.setattr(jobs, "get_settings", lambda: settings_for(tmp_path, ollama.url))
            document_id = asyncio.run(prepare())
            enqueue_document(TEST_REDIS_URL, document_id)
            assert queue.count == 1

            SimpleWorker([queue], connection=connection).work(burst=True)

        state = asyncio.run(read_state(document_id))
        assert state["status"] == "READY"
        assert state["chunk_count"] >= 1
        assert queue.count == 0
        assert not (tmp_path / str(document_id)).exists()
    finally:
        queue.empty()
        connection.close()
