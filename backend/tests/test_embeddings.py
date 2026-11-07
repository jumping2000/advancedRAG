# SPEC-2026-11-07-fase-2-ingestion
"""Embeddings in groups: AC-024 (REQ-012), at the level of the Ollama client and of the whole pipeline.

The client (`OllamaEmbedder`) is tested against the fake Ollama on a real local socket: the request it sends, the
vectors it returns and how every failure is classified (temporary: connection refused, timeout, 5xx, 429; permanent:
other 4xx and a body that is not a list of vectors). There is no retry yet (T-05), so a temporary error is only a
`TemporaryError`. The pipeline half of AC-024 runs `process_document` on a Markdown file of 40 chunks against real
PostgreSQL of compose.test.yaml, fresh state per test: Ollama must receive the texts in groups of
`EMBEDDING_BATCH_SIZE` and each stored vector must be the one of its own chunk (the fake derives a vector from its
text).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Chunk
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.errors import IngestionError, PermanentError, TemporaryError
from app.ingestion.pipeline import process_document
from app.run import migrate
from tests.conftest import make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import EmbedRequest, fake_ollama, fake_vector

MODEL = "test-model"
DIMENSIONS = 8
SECRET = "contenuto-riservato-del-documento"


def embedder(url: str, timeout: float = 5.0) -> OllamaEmbedder:
    return OllamaEmbedder(url, MODEL, timeout)


# The client ----------------------------------------------------------------------------------------------------------


async def test_AC_024_the_request_carries_the_model_and_the_texts_and_each_text_gets_its_own_vector() -> None:
    texts = ["uno", "due", "tre"]
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        vectors = await embedder(ollama.url).embed(texts)

    assert ollama.requests == [EmbedRequest(model=MODEL, texts=texts)]
    assert vectors == [fake_vector(one, DIMENSIONS) for one in texts]


async def test_AC_024_a_trailing_slash_in_the_base_url_does_not_change_the_endpoint() -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        vectors = await embedder(ollama.url + "/").embed(["uno"])

    assert vectors == [fake_vector("uno", DIMENSIONS)]


async def test_AC_024_a_refused_connection_is_a_temporary_embedding_unreachable() -> None:
    with pytest.raises(TemporaryError) as caught:
        await embedder(closed_port_url()).embed([SECRET])

    assert caught.value.code == "embedding_unreachable"
    assert caught.value.message
    assert SECRET not in caught.value.message


async def test_AC_024_a_timeout_is_a_temporary_embedding_unreachable() -> None:
    with fake_ollama(dimensions=DIMENSIONS, delay_seconds=2.0) as ollama:
        with pytest.raises(TemporaryError) as caught:
            await embedder(ollama.url, timeout=0.3).embed(["uno"])

    assert caught.value.code == "embedding_unreachable"


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_AC_024_a_5xx_or_429_answer_is_a_temporary_embedding_error(status: int) -> None:
    with fake_ollama(dimensions=DIMENSIONS, fail_status=status) as ollama:
        with pytest.raises(TemporaryError) as caught:
            await embedder(ollama.url).embed([SECRET])

    assert caught.value.code == "embedding_error"
    assert caught.value.message
    assert SECRET not in caught.value.message
    assert len(ollama.requests) == 1  # no retry loop before T-05


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_AC_024_any_other_4xx_answer_is_a_permanent_embedding_error(status: int) -> None:
    with fake_ollama(dimensions=DIMENSIONS, fail_status=status) as ollama:
        with pytest.raises(PermanentError) as caught:
            await embedder(ollama.url).embed(["uno"])

    assert caught.value.code == "embedding_error"
    assert not isinstance(caught.value, TemporaryError)


async def test_AC_024_a_body_that_is_not_a_list_of_vectors_is_a_permanent_embedding_invalid_response() -> None:
    with fake_ollama(dimensions=DIMENSIONS, invalid_body=True) as ollama:
        with pytest.raises(PermanentError) as caught:
            await embedder(ollama.url).embed(["uno", "due"])

    assert caught.value.code == "embedding_invalid_response"


@pytest.mark.parametrize("delta", [-1, 1])
async def test_AC_024_a_different_number_of_vectors_than_texts_is_a_permanent_embedding_invalid_response(
    delta: int,
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, vector_count_delta=delta) as ollama:
        with pytest.raises(PermanentError) as caught:
            await embedder(ollama.url).embed(["uno", "due", "tre"])

    assert caught.value.code == "embedding_invalid_response"


def test_AC_024_the_errors_are_ingestion_errors_with_a_code_and_a_message() -> None:
    temporary = TemporaryError("embedding_unreachable", "Ollama cannot be reached")
    permanent = PermanentError("parse_failed", "The file cannot be read")

    assert isinstance(temporary, IngestionError)
    assert isinstance(permanent, IngestionError)
    assert not isinstance(temporary, PermanentError)
    assert (temporary.code, temporary.message) == ("embedding_unreachable", "Ollama cannot be reached")
    assert (permanent.code, permanent.message) == ("parse_failed", "The file cannot be read")


# The settings --------------------------------------------------------------------------------------------------------


def test_AC_024_the_defaults_are_16_texts_per_request_and_120_seconds() -> None:
    settings = make_settings()

    assert settings.embedding_batch_size == 16
    assert settings.embedding_timeout_seconds == 120


@pytest.mark.parametrize(("name", "value"), [("embedding_batch_size", 0), ("embedding_batch_size", -3)])
def test_AC_024_a_batch_size_below_1_is_refused(name: str, value: int) -> None:
    with pytest.raises(ValidationError, match=name):
        make_settings(**{name: value})


@pytest.mark.parametrize("value", [0, -1.5])
def test_AC_024_a_timeout_that_is_not_positive_is_refused(value: float) -> None:
    with pytest.raises(ValidationError, match="embedding_timeout_seconds"):
        make_settings(embedding_timeout_seconds=value)


# The pipeline --------------------------------------------------------------------------------------------------------


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


def put_original(upload_dir: Path, document_id: uuid.UUID, content: bytes) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / str(document_id)).write_bytes(content)


async def queue_markdown_document(engine: AsyncEngine, upload_dir: Path, content: str) -> uuid.UUID:
    """A QUEUED Markdown document in a new KB, with its original in the upload folder."""
    document_id = uuid.uuid4()
    async with engine.begin() as connection:
        kb_id = await connection.scalar(text("insert into knowledge_bases (name) values ('KB') returning id"))
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum)"
                " values (:id, :kb_id, 'guida.md', 'md', :size, :checksum)"
            ),
            {"id": document_id, "kb_id": kb_id, "size": len(content.encode()), "checksum": document_id.hex},
        )
    put_original(upload_dir, document_id, content.encode())
    return document_id


async def stored_chunks(engine: AsyncEngine, document_id: uuid.UUID) -> list[Chunk]:
    async with async_sessionmaker[AsyncSession](engine, expire_on_commit=False)() as session:
        query = select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.position)
        return list((await session.scalars(query)).all())


async def document_state(engine: AsyncEngine, document_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text("select status, status_reason_code, chunk_count from documents where id = :id"), {"id": document_id}
        )
        return dict(row.one()._mapping)


def forty_sections() -> str:
    """40 headings with one short paragraph each: with the settings below every section is one chunk."""
    return "\n\n".join(
        f"# Titolo {number}\n\nTesto del paragrafo numero {number} con alcune parole." for number in range(40)
    )


def pipeline_settings(upload_dir: Path, ollama_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upload_dir": upload_dir,
        "ollama_base_url": ollama_url,
        "embedding_model": MODEL,
        "embedding_dimensions": DIMENSIONS,
        "chunk_min_words": 1,
        "chunk_target_words": 10,
        "chunk_max_words": 20,
        "chunk_overlap_percent": 0,
    }
    values.update(overrides)
    return make_settings(**values)


async def test_AC_024_40_chunks_with_a_batch_size_of_16_make_three_requests_of_16_16_and_8_texts(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = pipeline_settings(tmp_path, ollama.url, embedding_batch_size=16)
        document_id = await queue_markdown_document(engine, tmp_path, forty_sections())

        await process_document(document_id, settings)

    assert [len(request.texts) for request in ollama.requests] == [16, 16, 8]
    assert {request.model for request in ollama.requests} == {MODEL}
    chunks = await stored_chunks(engine, document_id)
    assert len(chunks) == 40
    # The texts sent are the chunks, in order, and each chunk holds the vector the fake derives from its own text
    # (pgvector keeps 32-bit floats, hence the tolerance).
    assert [chunk.content for chunk in chunks] == ollama.texts_sent()
    for chunk in chunks:
        expected = fake_vector(chunk.content, DIMENSIONS)
        assert chunk.embedding == pytest.approx(expected, abs=1e-6)
    assert len({tuple(chunk.embedding) for chunk in chunks}) == 40


@pytest.mark.parametrize(("batch_size", "expected"), [(40, [40]), (50, [40]), (7, [7, 7, 7, 7, 7, 5]), (1, [1] * 40)])
async def test_AC_024_the_batch_size_setting_decides_the_size_of_the_requests(
    engine: AsyncEngine, tmp_path: Path, batch_size: int, expected: list[int]
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = pipeline_settings(tmp_path, ollama.url, embedding_batch_size=batch_size)
        document_id = await queue_markdown_document(engine, tmp_path, forty_sections())

        await process_document(document_id, settings)

    assert [len(request.texts) for request in ollama.requests] == expected
    assert (await document_state(engine, document_id))["chunk_count"] == 40


async def test_AC_024_a_group_answered_with_the_wrong_number_of_vectors_fails_the_document_and_saves_no_chunk(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two groups that are wrong in opposite ways would still add up to the right total: each group is checked."""
    with fake_ollama(dimensions=DIMENSIONS, vector_count_delta=-1) as ollama:
        settings = pipeline_settings(tmp_path, ollama.url, embedding_batch_size=16)
        document_id = await queue_markdown_document(engine, tmp_path, forty_sections())

        await process_document(document_id, settings)

    state = await document_state(engine, document_id)
    assert state["status"] == "FAILED"
    assert state["status_reason_code"] == "embedding_invalid_response"
    assert await stored_chunks(engine, document_id) == []
    assert (tmp_path / str(document_id)).is_file()
