# SPEC-2026-11-07-fase-2-ingestion
"""Indexing profiles: AC-026 (REQ-013), and the settings that define a profile (REQ-011, REQ-012).

A profile is one row per configuration (model, dimensions, parser, chunker parameters, BM25 configuration) plus one
HNSW index per profile, a partial index on `(embedding::vector(N))` for `profile_id`. The backend creates it at startup
(`app.run.main`); a second start with the same configuration changes nothing, and a different model, size or chunker
parameter gives a second profile while the rows already stored keep theirs.

Real PostgreSQL of compose.test.yaml, fresh state per test (no profiles, documents, KBs or users; the HNSW indexes of
earlier tests are dropped).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app import run
from app.config import Settings, get_settings
from app.ingestion.chunker import ChunkerConfig
from app.ingestion.profile import (
    BM25_CONFIG_ID,
    PARSER_ID,
    ProfileSetupError,
    embedding_expression,
    ensure_profile,
)
from app.run import migrate
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

ADMIN = "admin"
ADMIN_PASSWORD = "first-admin-password"
DEFAULT_CHUNKER = {"target_words": 300, "min_words": 50, "max_words": 450, "overlap_percent": 15}


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


async def reset_database() -> None:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("truncate table indexing_profiles, documents, kb_members, knowledge_bases, users cascade")
        )
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
def empty_database(schema: None) -> None:
    """Sync, so that the tests may run the real entry point (it owns its event loop)."""
    asyncio.run(reset_database())


@pytest.fixture
async def engine(empty_database: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    yield engine
    await engine.dispose()


def profile_settings(**overrides: Any) -> Settings:
    """Small vectors keep the fixtures short; the other values are the defaults."""
    values: dict[str, Any] = {"embedding_model": "model-a", "embedding_dimensions": 4}
    values.update(overrides)
    return make_settings(**values)


async def profile_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(text("select * from indexing_profiles order by created_at, id"))
        return [dict(row) for row in result.mappings()]


async def hnsw_indexes(engine: AsyncEngine) -> dict[str, str]:
    """Index name -> definition, for the HNSW indexes of the chunks table."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text("select indexname, indexdef from pg_indexes where tablename = 'chunks' and indexdef like '%hnsw%'")
        )
        return {row.indexname: row.indexdef for row in result}


def hnsw_name(profile_id: uuid.UUID) -> str:
    return f"chunks_embedding_hnsw_{profile_id.hex[:12]}"


# AC-026: one profile per configuration ------------------------------------------------------------------------------


async def test_AC_026_ensure_profile_twice_with_the_same_settings_gives_one_profile(engine: AsyncEngine) -> None:
    settings = profile_settings()
    first = await ensure_profile(engine, settings)
    second = await ensure_profile(engine, settings)

    assert first.id == second.id
    rows = await profile_rows(engine)
    assert len(rows) == 1
    assert rows[0]["id"] == first.id
    assert (rows[0]["embedding_model"], rows[0]["dimensions"]) == ("model-a", 4)
    assert rows[0]["parser"] == "docling-serve+text:v1" == PARSER_ID
    assert rows[0]["chunker"] == DEFAULT_CHUNKER
    assert rows[0]["bm25_config"] == "conservative-v1" == BM25_CONFIG_ID
    assert (first.embedding_model, first.dimensions, first.chunker) == ("model-a", 4, DEFAULT_CHUNKER)


@pytest.mark.parametrize(
    "change",
    [
        {"embedding_model": "model-b"},
        {"embedding_dimensions": 5},
        {"chunk_target_words": 200},
        {"chunk_min_words": 10},
        {"chunk_max_words": 500},
        {"chunk_overlap_percent": 20},
    ],
)
async def test_AC_026_a_different_configuration_gives_a_second_profile(
    engine: AsyncEngine, change: dict[str, Any]
) -> None:
    first = await ensure_profile(engine, profile_settings())
    second = await ensure_profile(engine, profile_settings(**change))
    again = await ensure_profile(engine, profile_settings(**change))

    assert second.id != first.id
    assert again.id == second.id
    assert {row["id"] for row in await profile_rows(engine)} == {first.id, second.id}


async def test_AC_026_documents_and_chunks_already_stored_keep_their_profile(engine: AsyncEngine) -> None:
    first = await ensure_profile(engine, profile_settings())
    async with engine.begin() as connection:
        kb_id = await connection.scalar(text("insert into knowledge_bases (name) values ('kb') returning id"))
        document_id = await connection.scalar(
            text(
                "insert into documents (kb_id, filename, format, size_bytes, checksum, status, chunk_count, profile_id)"
                " values (:kb, 'a.txt', 'txt', 1, 'c', 'READY', 1, :profile) returning id"
            ),
            {"kb": kb_id, "profile": first.id},
        )
        await connection.execute(
            text(
                "insert into chunks (kb_id, document_id, profile_id, dimensions, position, content, word_count,"
                " embedding) values (:kb, :document, :profile, 4, 0, 'testo', 1, CAST('[1,2,3,4]' AS vector))"
            ),
            {"kb": kb_id, "document": document_id, "profile": first.id},
        )

    second = await ensure_profile(engine, profile_settings(embedding_model="model-b"))
    third = await ensure_profile(engine, profile_settings(embedding_dimensions=8))

    assert len({first.id, second.id, third.id}) == 3
    async with engine.connect() as connection:
        document_profile = await connection.scalar(text("select profile_id from documents"))
        chunk_profile = await connection.scalar(text("select profile_id from chunks"))
    assert document_profile == first.id
    assert chunk_profile == first.id


async def test_AC_026_concurrent_calls_give_one_profile_and_one_index(engine: AsyncEngine) -> None:
    settings = profile_settings()
    profiles = await asyncio.gather(*(ensure_profile(engine, settings) for _ in range(8)))

    assert len({profile.id for profile in profiles}) == 1
    rows = await profile_rows(engine)
    assert [row["id"] for row in rows] == [profiles[0].id]
    assert list(await hnsw_indexes(engine)) == [hnsw_name(profiles[0].id)]


async def test_AC_026_each_profile_has_its_hnsw_index_with_the_partial_predicate(engine: AsyncEngine) -> None:
    first = await ensure_profile(engine, profile_settings())
    second = await ensure_profile(engine, profile_settings(embedding_dimensions=8))
    await ensure_profile(engine, profile_settings())  # a second call creates nothing new

    indexes = await hnsw_indexes(engine)
    assert set(indexes) == {hnsw_name(first.id), hnsw_name(second.id)}
    for profile, dimensions in ((first, 4), (second, 8)):
        definition = indexes[hnsw_name(profile.id)]
        assert "USING hnsw" in definition
        assert f"((embedding)::vector({dimensions})) vector_cosine_ops" in definition
        assert f"WHERE (profile_id = '{profile.id}'::uuid)" in definition


async def test_AC_026_embedding_expression_is_the_one_of_the_index(engine: AsyncEngine) -> None:
    profile = await ensure_profile(engine, profile_settings(embedding_dimensions=8))

    assert embedding_expression(profile) == "embedding::vector(8)"


# AC-026 through the real entry point --------------------------------------------------------------------------------


@pytest.fixture
def start_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Callable[..., None]]:
    """python -m app.run with uvicorn replaced, so that it returns after the startup steps."""
    started: list[int] = []
    monkeypatch.chdir(tmp_path)  # no .env file is read
    monkeypatch.setattr(run.uvicorn, "run", lambda *args, **kwargs: started.append(1))
    for name in (
        "POSTGRES_PASSWORD",
        "SESSION_IDLE_TIMEOUT_MINUTES",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "CHUNK_TARGET_WORDS",
        "CHUNK_MIN_WORDS",
        "CHUNK_MAX_WORDS",
        "CHUNK_OVERLAP_PERCENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DOCLING_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)

    def start(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        before = len(started)
        run.main()
        assert len(started) == before + 1, "uvicorn was not started"

    yield start
    get_settings.cache_clear()


async def stored_profiles() -> list[tuple[str, int]]:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    try:
        rows = await profile_rows(engine)
        assert len(await hnsw_indexes(engine)) == len(rows)
    finally:
        await engine.dispose()
    return sorted((row["embedding_model"], row["dimensions"]) for row in rows)


def test_AC_026_backend_started_twice_with_the_same_configuration_creates_one_profile(
    empty_database: None, start_backend: Callable[..., None]
) -> None:
    start_backend()
    start_backend()

    assert asyncio.run(stored_profiles()) == [("bge-m3", 1024)]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"EMBEDDING_MODEL": "nomic-embed-text"}, ("nomic-embed-text", 1024)),
        ({"EMBEDDING_DIMENSIONS": "768"}, ("bge-m3", 768)),
    ],
)
def test_AC_026_backend_started_with_another_model_or_size_creates_a_second_profile(
    empty_database: None, start_backend: Callable[..., None], env: dict[str, str], expected: tuple[str, int]
) -> None:
    start_backend()
    start_backend(**env)
    start_backend(**env)

    assert asyncio.run(stored_profiles()) == sorted([("bge-m3", 1024), expected])


@pytest.mark.parametrize("dimensions", [0, 2001])
async def test_AC_026_dimensions_that_cannot_be_indexed_are_refused_naming_the_variable(
    engine: AsyncEngine, dimensions: int
) -> None:
    settings = profile_settings().model_copy(update={"embedding_dimensions": dimensions})  # skips the validation

    with pytest.raises(ProfileSetupError, match="EMBEDDING_DIMENSIONS"):
        await ensure_profile(engine, settings)

    assert await profile_rows(engine) == []
    assert await hnsw_indexes(engine) == {}


def test_AC_026_backend_stops_naming_the_variable_when_the_profile_cannot_be_created(
    empty_database: None, start_backend: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    unchecked = make_settings(admin_username=ADMIN, admin_password=ADMIN_PASSWORD).model_copy(
        update={"embedding_dimensions": 2001}
    )
    monkeypatch.setattr(run, "get_settings", lambda: unchecked)

    with pytest.raises(SystemExit) as stopped:
        start_backend()

    assert "EMBEDDING_DIMENSIONS" in str(stopped.value)
    assert asyncio.run(stored_profiles()) == []


# Settings that define a profile -------------------------------------------------------------------------------------


def test_the_defaults_are_the_ones_of_the_spec() -> None:
    settings = make_settings()

    assert (settings.embedding_model, settings.embedding_dimensions) == ("bge-m3", 1024)
    assert settings.chunker_config == ChunkerConfig(target_words=300, min_words=50, max_words=450, overlap_percent=15)


def test_the_chunker_config_follows_the_settings() -> None:
    settings = make_settings(chunk_target_words=100, chunk_min_words=20, chunk_max_words=150, chunk_overlap_percent=10)

    assert settings.chunker_config == ChunkerConfig(target_words=100, min_words=20, max_words=150, overlap_percent=10)


def test_equal_minimum_target_and_maximum_are_consistent() -> None:
    settings = make_settings(chunk_target_words=100, chunk_min_words=100, chunk_max_words=100)

    assert settings.chunker_config.max_words == 100


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"chunk_min_words": 400}, ["CHUNK_MIN_WORDS", "CHUNK_TARGET_WORDS"]),
        ({"chunk_max_words": 200}, ["CHUNK_MAX_WORDS", "CHUNK_TARGET_WORDS"]),
        ({"chunk_target_words": 0, "chunk_min_words": 0}, ["CHUNK_TARGET_WORDS"]),
        ({"chunk_min_words": -1}, ["CHUNK_MIN_WORDS"]),
        ({"chunk_overlap_percent": 100}, ["CHUNK_OVERLAP_PERCENT"]),
        ({"chunk_overlap_percent": -1}, ["CHUNK_OVERLAP_PERCENT"]),
        ({"embedding_dimensions": 0}, ["EMBEDDING_DIMENSIONS"]),
        ({"embedding_dimensions": 2001}, ["EMBEDDING_DIMENSIONS"]),
        ({"embedding_model": ""}, ["EMBEDDING_MODEL"]),
    ],
)
def test_inconsistent_values_are_refused_naming_the_variables(overrides: dict[str, Any], named: list[str]) -> None:
    with pytest.raises(ValidationError) as refused:
        make_settings(**overrides)

    message = str(refused.value).upper()
    for name in named:
        assert name in message


def test_the_backend_does_not_start_with_inconsistent_chunk_sizes(
    empty_database: None, start_backend: Callable[..., None]
) -> None:
    with pytest.raises(ValidationError) as refused:
        start_backend(CHUNK_MIN_WORDS="400")

    message = str(refused.value)
    assert "CHUNK_MIN_WORDS" in message
    assert "CHUNK_TARGET_WORDS" in message
    assert asyncio.run(stored_profiles()) == []
