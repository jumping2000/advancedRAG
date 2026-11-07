# SPEC-2026-11-07-fase-2-ingestion
"""PDF, DOCX, PPTX and XLSX through Docling, and every way a document ends badly: AC-016, AC-017, AC-019, AC-025,
AC-029, AC-030, AC-031, AC-032, AC-033 (REQ-007 to REQ-009, REQ-012, REQ-014 to REQ-016).

`process_document` runs against real PostgreSQL of compose.test.yaml, the fake Ollama and the fake Docling on local
sockets (never the real services), with fresh state per test and an upload folder inside pytest's tmp_path. The wait
between two attempts is injected (`sleep`) and recorded, so no test sleeps for real.

What a document can end as: READY; UNSUPPORTED `no_text_extracted` with the original deleted at once (REQ-009); FAILED
with a code and a fixed English message, no chunk of it left and the original kept for a retry. A temporary error of
Docling or Ollama repeats that step only, up to INGESTION_MAX_ATTEMPTS attempts in all, waiting 10 s, then 20 s...;
any other error is not repeated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from rq.timeouts import JobTimeoutException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import Chunk, User
from app.ingestion.docling import DoclingClient
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.errors import PermanentError
from app.ingestion.pipeline import process_document
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_docling, fake_document_file, fake_ollama, fake_vector

DIMENSIONS = 8
MODEL = "test-model"
API_KEY = "docling-key-123"
SECRET = "contenuto-riservato-del-documento"
FILENAME = "segretissimo-contratto.pdf"
DOCLING_FORMATS = ["pdf", "docx", "pptx", "xlsx"]
MANUAL = [
    ("section_header", "Introduzione", 1, 1),
    ("text", "Questo manuale spiega come usare il prodotto in ogni sua parte.", 1),
    ("section_header", "Dettagli", 2, 2),
    ("text", "Un secondo paragrafo a pagina due con altre parole utili al lettore.", 2),
    ("table", [["Voce", "Prezzo"], ["Caffè", "1,20"]], 2),
]
TEXT_SENTENCE = "Prima riga del racconto. Seconda parte del racconto, con accenti: è à ò ù. Terza e ultima parte."


async def no_sleep(seconds: float) -> None:
    return None


class Waits:
    """The injected sleep: records the waits instead of waiting."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def non_white_text(count: int) -> str:
    """A text with exactly `count` non-white characters, in words of ten, with extra white space in between."""
    letters = ("abcdefghij" * (count // 10 + 1))[:count]
    return "  \n ".join(" ".join(letters[start : start + 10] for start in range(0, count, 10)).split(" "))


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no profiles, KBs, documents or chunks; the HNSW indexes of earlier tests are dropped."""
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
    yield engine
    await engine.dispose()


def settings_for(upload_dir: Path, ollama_url: str, docling_url: str | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upload_dir": upload_dir,
        "ollama_base_url": ollama_url,
        "docling_base_url": docling_url or closed_port_url(),
        "docling_api_key": API_KEY,
        "embedding_model": MODEL,
        "embedding_dimensions": DIMENSIONS,
    }  # the chunker keeps its defaults: a short section is one chunk
    values.update(overrides)
    return make_settings(**values)


def put_original(upload_dir: Path, document_id: uuid.UUID, content: bytes) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / str(document_id)).write_bytes(content)


def files_in(folder: Path) -> list[Path]:
    return list(folder.iterdir())


async def add_document(
    engine: AsyncEngine, upload_dir: Path, content: bytes | None, fmt: str = "pdf", filename: str = FILENAME
) -> uuid.UUID:
    """A QUEUED document in a new KB with its original in the upload folder (none when `content` is None)."""
    document_id = uuid.uuid4()
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": uuid.uuid4().hex}
        )
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum)"
                " values (:id, :kb_id, :filename, :format, 1, :checksum)"
            ),
            {"id": document_id, "kb_id": kb_id, "filename": filename, "format": fmt, "checksum": document_id.hex},
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


def original_exists(upload_dir: Path, document_id: uuid.UUID) -> bool:
    return (upload_dir / str(document_id)).is_file()


async def assert_failed(
    engine: AsyncEngine, upload_dir: Path, document_id: uuid.UUID, code: str, message: str | None = None
) -> dict[str, Any]:
    """FAILED with this code (and this fixed message), no chunk of the document, the original kept for a retry."""
    state = await state_of(engine, document_id)
    assert state["status"] == "FAILED"
    assert state["status_reason_code"] == code
    assert state["status_reason_message"]
    if message is not None:
        assert state["status_reason_message"] == message
    assert state["chunk_count"] == 0
    assert await chunks_of(engine, document_id) == []
    assert original_exists(upload_dir, document_id)
    return state


def five_sections() -> bytes:
    return "\n\n".join(
        f"# Sezione {number}\n\nIl paragrafo numero {number} ha alcune parole di prova." for number in range(5)
    ).encode()


# AC-017: the four formats go to Docling, the text ones do not --------------------------------------------------------


@pytest.mark.parametrize("fmt", DOCLING_FORMATS)
async def test_AC_017_a_document_is_sent_to_docling_with_the_key_and_ends_ready_with_sections_and_pages(
    engine: AsyncEngine, tmp_path: Path, fmt: str
) -> None:
    content = fake_document_file(MANUAL)
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, content, fmt=fmt)

        await process_document(document_id, settings, sleep=no_sleep)

    (request,) = docling.requests
    assert request.content == content
    assert request.api_key == API_KEY
    assert request.fields == {"from_formats": fmt, "to_formats": "json", "do_ocr": "false", "include_images": "false"}
    # The name of the user's file stays here; Docling gets a name made of the document id and the format
    assert request.filename == f"{document_id}.{fmt}"
    state = await state_of(engine, document_id)
    chunks = await chunks_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["status_reason_code"] is None
    assert state["chunk_count"] == len(chunks) == 2
    assert [(chunk.section_title, chunk.page_start, chunk.page_end) for chunk in chunks] == [
        ("Introduzione", 1, 1),
        ("Introduzione > Dettagli", 2, 2),
    ]
    assert "Caffè | 1,20" in chunks[1].content
    assert "Voce | Prezzo" in chunks[1].content
    assert ollama.texts_sent() == [chunk.content for chunk in chunks]
    for chunk in chunks:
        assert chunk.embedding == pytest.approx(fake_vector(chunk.content, DIMENSIONS), abs=1e-6)
    assert not original_exists(tmp_path, document_id)


async def test_AC_017_without_a_docling_key_no_x_api_key_header_is_sent(engine: AsyncEngine, tmp_path: Path) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, docling_api_key=None)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert [request.api_key for request in docling.requests] == [None]
    assert (await state_of(engine, document_id))["status"] == "READY"


@pytest.mark.parametrize("fmt", ["txt", "md"])
async def test_AC_017_a_txt_or_md_never_calls_docling(engine: AsyncEngine, tmp_path: Path, fmt: str) -> None:
    body = (
        "# Guida\n\nIl testo della guida con alcune parole.\n\nUn altro paragrafo qui sotto."
        if fmt == "md"
        else (TEXT_SENTENCE)
    )
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, body.encode(), fmt=fmt)

        await process_document(document_id, settings, sleep=no_sleep)

    assert docling.requests == []
    assert (await state_of(engine, document_id))["status"] == "READY"
    assert len(ollama.requests) == 1


# AC-019: too little text -> UNSUPPORTED, original deleted ------------------------------------------------------------


async def assert_unsupported(engine: AsyncEngine, upload_dir: Path, document_id: uuid.UUID) -> None:
    state = await state_of(engine, document_id)
    assert state["status"] == "UNSUPPORTED"
    assert state["status_reason_code"] == "no_text_extracted"
    assert state["status_reason_message"] == "No text could be extracted from the document"
    assert state["chunk_count"] == 0
    assert await chunks_of(engine, document_id) == []
    assert not original_exists(upload_dir, document_id)
    assert files_in(upload_dir) == []


@pytest.mark.parametrize("fmt", DOCLING_FORMATS)
async def test_AC_019_docling_returning_20_characters_makes_the_document_unsupported_and_deletes_the_original(
    engine: AsyncEngine, tmp_path: Path, fmt: str
) -> None:
    content = fake_document_file([("text", non_white_text(20), 1)])
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, content, fmt=fmt)

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(docling.requests) == 1
    assert ollama.requests == []
    await assert_unsupported(engine, tmp_path, document_id)


@pytest.mark.parametrize(("chars", "status"), [(49, "UNSUPPORTED"), (50, "READY"), (51, "READY")])
async def test_AC_019_the_limit_is_50_characters_that_are_not_white_and_it_is_the_setting(
    engine: AsyncEngine, tmp_path: Path, chars: int, status: str
) -> None:
    content = fake_document_file([("text", non_white_text(chars), 1)])
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, content)

        await process_document(document_id, settings, sleep=no_sleep)

    assert (await state_of(engine, document_id))["status"] == status
    assert original_exists(tmp_path, document_id) is False  # READY and UNSUPPORTED both delete the original


async def test_AC_019_min_extracted_chars_sets_the_limit(engine: AsyncEngine, tmp_path: Path) -> None:
    content = fake_document_file([("text", non_white_text(20), 1)])
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, min_extracted_chars=20)
        document_id = await add_document(engine, tmp_path, content)

        await process_document(document_id, settings, sleep=no_sleep)

    assert (await state_of(engine, document_id))["status"] == "READY"


async def test_AC_019_the_characters_are_counted_after_the_normalization(engine: AsyncEngine, tmp_path: Path) -> None:
    """60 control characters and 15 letters: 75 characters that are not white before the normalization, 15 after."""
    content = fake_document_file([("text", "venti caratteri" + "\x01" * 60, 1)])
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, content)

        await process_document(document_id, settings, sleep=no_sleep)

    assert ollama.requests == []
    await assert_unsupported(engine, tmp_path, document_id)


async def test_AC_019_a_document_with_nothing_in_it_is_unsupported_even_with_a_minimum_of_zero(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    content = fake_document_file([])
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, min_extracted_chars=0)
        document_id = await add_document(engine, tmp_path, content)

        await process_document(document_id, settings, sleep=no_sleep)

    assert ollama.requests == []
    await assert_unsupported(engine, tmp_path, document_id)


@pytest.mark.parametrize("fmt", ["txt", "md"])
@pytest.mark.parametrize(
    "content", [b"", b"   \n\n \t \n", b"\x00\x01\x02 \x0b\x0c", b"Troppo corto.", b"# Titolo\n\nNiente."]
)
async def test_AC_019_a_txt_or_md_with_little_or_no_text_is_unsupported_too(
    engine: AsyncEngine, tmp_path: Path, fmt: str, content: bytes
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, content, fmt=fmt)

        await process_document(document_id, settings, sleep=no_sleep)

    assert docling.requests == []
    assert ollama.requests == []
    await assert_unsupported(engine, tmp_path, document_id)


# AC-031: temporary errors are repeated ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [503, 500, 429])
async def test_AC_031_docling_answering_5xx_twice_and_then_well_makes_the_document_ready_after_three_attempts(
    engine: AsyncEngine, tmp_path: Path, status: int
) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(fail_status=status, fail_count=2) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 3
    assert waits.seconds == [10, 20]  # INGESTION_RETRY_BACKOFF_SECONDS and its double
    state = await state_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["status_reason_code"] is None
    assert state["chunk_count"] == len(await chunks_of(engine, document_id)) == 2
    assert not original_exists(tmp_path, document_id)


async def test_AC_031_the_waits_follow_the_setting_and_double(engine: AsyncEngine, tmp_path: Path) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(fail_status=503, fail_count=3) as docling:
        settings = settings_for(
            tmp_path, ollama.url, docling.url, ingestion_max_attempts=4, ingestion_retry_backoff_seconds=2.5
        )
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 4
    assert waits.seconds == [2.5, 5.0, 10.0]
    assert (await state_of(engine, document_id))["status"] == "READY"


async def test_AC_031_a_group_of_embeddings_is_repeated_alone_and_docling_is_not_called_again(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Five chunks in groups of two: the second group fails once and is sent again; the first group and the Docling
    call are not repeated."""
    waits = Waits()
    items: list[tuple[Any, ...]] = []
    for number in range(5):
        items += [
            ("section_header", f"Sezione {number}", 1, 1),
            ("text", f"Il paragrafo numero {number} ha alcune parole di prova.", 1),
        ]
    with fake_ollama(dimensions=DIMENSIONS, fail_status=503, fail_requests={2}) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, embedding_batch_size=2)
        document_id = await add_document(engine, tmp_path, fake_document_file(items))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 1
    assert waits.seconds == [10]
    chunks = await chunks_of(engine, document_id)
    assert (await state_of(engine, document_id))["status"] == "READY"
    assert len(chunks) == 5
    # Request 1: chunks 0-1; request 2 (503): chunks 2-3; request 3: chunks 2-3 again; request 4: chunk 4
    assert [request.texts for request in ollama.requests] == [
        [chunk.content for chunk in chunks[0:2]],
        [chunk.content for chunk in chunks[2:4]],
        [chunk.content for chunk in chunks[2:4]],
        [chunk.content for chunk in chunks[4:5]],
    ]


# AC-032: unreachable services, exactly the attempts ------------------------------------------------------------------


async def test_AC_032_docling_refusing_every_connection_fails_the_document_after_3_attempts_and_keeps_the_original(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url, closed_port_url())
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert waits.seconds == [10, 20]  # three attempts: a wait after the first and after the second
    await assert_failed(
        engine, tmp_path, document_id, "docling_unreachable", "The document parsing service could not be reached"
    )
    assert ollama.requests == []


@pytest.mark.parametrize("attempts", [1, 3, 5])
async def test_AC_032_docling_that_hangs_up_gets_exactly_ingestion_max_attempts_requests(
    engine: AsyncEngine, tmp_path: Path, attempts: int
) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(drop_connection=True) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, ingestion_max_attempts=attempts)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == attempts
    assert waits.seconds == [10 * 2**number for number in range(attempts - 1)]
    await assert_failed(engine, tmp_path, document_id, "docling_unreachable")


async def test_AC_032_docling_that_answers_too_late_is_repeated_up_to_the_attempts_then_unreachable(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(delay_seconds=5.0) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, docling_timeout_seconds=0.2)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(docling.requests) == 3
    await assert_failed(engine, tmp_path, document_id, "docling_unreachable")


async def test_AC_032_docling_answering_5xx_every_time_fails_with_docling_error_after_3_attempts(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(fail_status=503) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(docling.requests) == 3
    await assert_failed(
        engine, tmp_path, document_id, "docling_error", "The document parsing service answered with an error (503)"
    )


async def test_AC_032_ollama_unreachable_fails_with_embedding_unreachable_after_3_attempts_and_no_chunk(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    waits = Waits()
    with fake_docling() as docling:
        settings = settings_for(tmp_path, closed_port_url(), docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 1  # the parsing is not repeated because the embeddings failed
    assert waits.seconds == [10, 20]
    await assert_failed(
        engine, tmp_path, document_id, "embedding_unreachable", "The embedding service could not be reached"
    )


async def test_AC_032_ollama_that_answers_too_late_gets_exactly_the_attempts(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, delay_seconds=5.0) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, embedding_timeout_seconds=0.2)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 3
    await assert_failed(engine, tmp_path, document_id, "embedding_unreachable")


# AC-033: what cannot be repeated is not ------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [{"fail_status": 422}, {"fail_status": 400}, {"result_status": "failure"}])
async def test_AC_033_docling_unable_to_read_the_file_fails_the_document_after_one_attempt(
    engine: AsyncEngine, tmp_path: Path, mode: dict[str, Any]
) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(**mode) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 1
    assert waits.seconds == []
    await assert_failed(engine, tmp_path, document_id, "parse_failed", "The document could not be parsed")
    assert ollama.requests == []


async def test_AC_033_a_file_the_fake_docling_cannot_convert_is_a_422_and_a_parse_failed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, b"%PDF-1.7 truncated, not a document")

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(docling.requests) == 1
    await assert_failed(engine, tmp_path, document_id, "parse_failed")


async def test_AC_033_an_embedding_error_that_is_not_temporary_is_not_repeated(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, fail_status=400) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 1
    await assert_failed(engine, tmp_path, document_id, "embedding_error")


@pytest.mark.parametrize("status", [401, 403])
async def test_AC_033_a_refused_docling_key_fails_the_document_with_docling_error_after_one_attempt(
    engine: AsyncEngine, tmp_path: Path, status: int
) -> None:
    waits = Waits()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(fail_status=status) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=waits)

    assert len(docling.requests) == 1
    assert waits.seconds == []
    await assert_failed(
        engine,
        tmp_path,
        document_id,
        "docling_error",
        "The document parsing service refused the request: check DOCLING_API_KEY",
    )
    assert ollama.requests == []


@pytest.mark.parametrize(("include", "sent"), [(None, "false"), (False, "false"), (True, "true")])
async def test_AC_017_the_setting_docling_include_images_decides_the_form_field_include_images(
    engine: AsyncEngine, tmp_path: Path, include: bool | None, sent: str
) -> None:
    overrides = {} if include is None else {"docling_include_images": include}
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, **overrides)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert [request.fields["include_images"] for request in docling.requests] == [sent]
    assert (await state_of(engine, document_id))["status"] == "READY"


@pytest.fixture
def pipeline_logs(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> pytest.LogCaptureFixture:
    """The records of the pipeline's logger (Alembic's fileConfig, run by the schema fixture, disabled it)."""
    monkeypatch.setattr(logging.getLogger("app.ingestion.pipeline"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ingestion.pipeline")
    return caplog


async def test_AC_031_a_partial_success_continues_and_logs_one_line_with_the_id_and_the_number_of_errors(
    engine: AsyncEngine, tmp_path: Path, pipeline_logs: pytest.LogCaptureFixture
) -> None:
    """Owner's decision: Docling's `partial_success` is accepted and the document goes on. The one warning holds the id
    of the document and the number of errors Docling reported (3), nothing of what they say, no file name, no text."""
    with (
        fake_ollama(dimensions=DIMENSIONS) as ollama,
        fake_docling(result_status="partial_success", error_text=f"page 2 failed: {SECRET}", error_count=3) as docling,
    ):
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(docling.requests) == 1
    assert (await state_of(engine, document_id))["status"] == "READY"
    warnings = [record for record in pipeline_logs.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(document_id) in message
    assert re.findall(r"\d+", message.replace(str(document_id), "")) == ["3"]
    assert SECRET not in message
    assert "segretissimo" not in message
    assert warnings[0].exc_info is None


async def test_AC_031_a_plain_success_logs_no_warning(
    engine: AsyncEngine, tmp_path: Path, pipeline_logs: pytest.LogCaptureFixture
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert (await state_of(engine, document_id))["status"] == "READY"
    assert [record for record in pipeline_logs.records if record.levelno >= logging.WARNING] == []


# AC-016: an error on the last group ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ollama_modes", "code", "requests"),
    [
        ({"fail_status": 500, "fail_from_request": 3}, "embedding_error", 2 + 3),  # the last group, 3 attempts
        ({"fail_status": 400, "fail_from_request": 3}, "embedding_error", 3),
        ({"vector_count_deltas": {3: -1}}, "embedding_invalid_response", 3),
    ],
    ids=["temporary error", "permanent error", "wrong number of vectors"],
)
async def test_AC_016_an_embedding_error_on_the_last_group_fails_the_document_and_leaves_no_chunk(
    engine: AsyncEngine, tmp_path: Path, ollama_modes: dict[str, Any], code: str, requests: int
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, **ollama_modes) as ollama:
        settings = settings_for(tmp_path, ollama.url, embedding_batch_size=2)  # 5 chunks: groups of 2, 2 and 1
        document_id = await add_document(engine, tmp_path, five_sections(), fmt="md")

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == requests
    assert [len(request.texts) for request in ollama.requests][:3] == [2, 2, 1]
    await assert_failed(engine, tmp_path, document_id, code)


# AC-025: vectors that cannot be used --------------------------------------------------------------------------------


async def test_AC_025_vectors_of_768_dimensions_for_a_profile_of_1024_fail_the_document_before_any_chunk_is_saved(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=768) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, embedding_dimensions=1024)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 1  # not a temporary error: no repetition
    await assert_failed(
        engine,
        tmp_path,
        document_id,
        "embedding_dimension_mismatch",
        "The embedding vectors do not have the size of the indexing profile",
    )


async def test_AC_025_fewer_vectors_than_texts_fail_the_document_with_embedding_invalid_response(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, vector_count_delta=-1) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 1
    await assert_failed(
        engine,
        tmp_path,
        document_id,
        "embedding_invalid_response",
        "The embedding service returned a wrong number of vectors",
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
async def test_AC_025_a_vector_with_a_value_that_is_not_finite_fails_the_document_with_embedding_invalid_response(
    engine: AsyncEngine, tmp_path: Path, bad: float
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, bad_number=bad) as ollama, fake_docling() as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert len(ollama.requests) == 1
    await assert_failed(
        engine,
        tmp_path,
        document_id,
        "embedding_invalid_response",
        "The embedding service returned an invalid response",
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
async def test_AC_025_the_embedder_refuses_a_value_that_is_not_finite(bad: float) -> None:
    with fake_ollama(dimensions=DIMENSIONS, bad_number=bad) as ollama:
        with pytest.raises(PermanentError) as caught:
            await OllamaEmbedder(ollama.url, MODEL, 5.0).embed(["uno", "due"])

    assert caught.value.code == "embedding_invalid_response"


@pytest.mark.parametrize(
    "body",
    [
        {"embeddings": [[1.0, "x"], [1.0, 2.0]]},
        {"embeddings": [[1.0, None], [1.0, 2.0]]},
        {"embeddings": [[1.0, True], [1.0, 2.0]]},
        {"embeddings": [[[1.0], [2.0]], [[1.0], [2.0]]]},
        {"embeddings": [[], []]},
        {"embeddings": [[1.0, 2.0], [1.0]]},
        {"embeddings": [1.0, 2.0]},
        {"embeddings": [None, None]},
        {"embeddings": {"a": [1.0]}},
        {"embeddings": [[1e999, 1.0], [1.0, 2.0]]},
        [[1.0, 2.0], [1.0, 2.0]],
        {"other": 1},
    ],
)
async def test_AC_025_a_body_with_the_wrong_shape_is_embedding_invalid_response(body: Any) -> None:
    with fake_ollama(dimensions=DIMENSIONS, body_override=body) as ollama:
        with pytest.raises(PermanentError) as caught:
            await OllamaEmbedder(ollama.url, MODEL, 5.0).embed(["uno", "due"])

    assert caught.value.code == "embedding_invalid_response"


# AC-029: an error while saving the chunks ---------------------------------------------------------------------------


async def install_fault(engine: AsyncEngine, name: str, when: str, body: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(f"create function {name}() returns trigger language plpgsql as $$ begin {body} end $$")
        )
        await connection.execute(text(f"create trigger {name} {when} on chunks for each row execute function {name}()"))


async def remove_fault(engine: AsyncEngine, name: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"drop trigger if exists {name} on chunks"))
        await connection.execute(text(f"drop function if exists {name}()"))


async def test_AC_029_an_error_at_the_third_chunk_insert_leaves_nothing_visible_and_the_document_failed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The database refuses the third insert, with an error message that holds the text of the chunk: the document is
    FAILED with a fixed message, none of the chunks before the third is visible and the profile is not set."""
    await install_fault(
        engine,
        "t05_fault_third",
        "before insert",
        "if new.position = 2 then raise exception 'injected fault on: %', new.content; end if; return new;",
    )
    try:
        with fake_ollama(dimensions=DIMENSIONS) as ollama:
            settings = settings_for(tmp_path, ollama.url)
            document_id = await add_document(engine, tmp_path, five_sections(), fmt="md")

            await process_document(document_id, settings, sleep=no_sleep)
    finally:
        await remove_fault(engine, "t05_fault_third")

    state = await assert_failed(
        engine, tmp_path, document_id, "unexpected_error", "Unexpected error while processing the document"
    )
    assert state["profile_id"] is None
    assert "paragrafo" not in state["status_reason_message"]
    assert len(ollama.requests) == 1  # a database error is not repeated, and the vectors were fine
    async with engine.connect() as connection:
        assert await connection.scalar(text("select count(*) from chunks")) == 0


# AC-030: READY only after the check ---------------------------------------------------------------------------------


async def test_AC_030_the_document_is_ready_only_after_the_indexes_returned_every_chunk(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS, delay_seconds=0.3) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await add_document(engine, tmp_path, five_sections(), fmt="md")

        await process_document(document_id, settings, sleep=no_sleep)

    state = await state_of(engine, document_id)
    chunks = await chunks_of(engine, document_id)
    assert state["status"] == "READY"
    assert state["chunk_count"] == len(chunks) == 5
    async with engine.connect() as connection:
        bm25 = await connection.scalar(
            text("select count(*) from chunks where id @@@ pdb.all() and document_id = :id"), {"id": document_id}
        )
    assert bm25 == 5


async def test_AC_030_a_check_that_finds_fewer_chunks_deletes_them_and_fails_the_document(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The index check is the real one. After the third chunk is inserted the database takes it away again, so the
    indexes hold 4 of the 5 saved chunks: no chunk may stay, the document is FAILED `verification_failed` and never
    READY, and the original is kept."""
    await install_fault(
        engine,
        "t05_fault_lose",
        "after insert",
        "if new.position = 2 then delete from chunks where id = new.id; end if; return new;",
    )
    seen: set[str] = set()
    try:
        with fake_ollama(dimensions=DIMENSIONS, delay_seconds=0.2) as ollama:
            settings = settings_for(tmp_path, ollama.url)
            document_id = await add_document(engine, tmp_path, five_sections(), fmt="md")
            job = asyncio.create_task(process_document(document_id, settings, sleep=no_sleep))
            async with asyncio.timeout(30):
                while not job.done():
                    seen.add((await state_of(engine, document_id))["status"])
                    await asyncio.sleep(0.02)
            await job
    finally:
        await remove_fault(engine, "t05_fault_lose")

    seen.add((await state_of(engine, document_id))["status"])
    assert "READY" not in seen
    await assert_failed(
        engine,
        tmp_path,
        document_id,
        "verification_failed",
        "The saved chunks were not found in the search indexes",
    )


# Unexpected errors and job timeouts ----------------------------------------------------------------------------------


async def test_AC_032_a_job_timeout_of_the_queue_fails_the_document_with_unexpected_error_and_is_not_repeated(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def timed_out(self: DoclingClient, *args: object) -> None:
        calls.append("convert")
        raise JobTimeoutException(f"Task exceeded maximum timeout value (3600 seconds) {SECRET}")

    monkeypatch.setattr(DoclingClient, "convert", timed_out)
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url, "http://127.0.0.1:9")
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    assert calls == ["convert"]
    # T-06: a timeout of the queue has a message of its own (it was the generic text of unexpected_error)
    state = await assert_failed(
        engine, tmp_path, document_id, "unexpected_error", "The processing time limit was exceeded"
    )
    assert SECRET not in state["status_reason_message"]


async def test_AC_016_a_bug_in_a_step_fails_the_document_with_a_fixed_message_that_hides_the_error(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: OllamaEmbedder, texts: object) -> list[list[float]]:
        raise RuntimeError(f"cannot embed {SECRET}")

    monkeypatch.setattr(OllamaEmbedder, "embed", broken)
    with fake_docling() as docling:
        settings = settings_for(tmp_path, "http://127.0.0.1:9", docling.url)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))

        await process_document(document_id, settings, sleep=no_sleep)

    state = await assert_failed(
        engine, tmp_path, document_id, "unexpected_error", "Unexpected error while processing the document"
    )
    assert SECRET not in json.dumps(state, default=str)


# AC-045: the API stays quick while a job waits for Docling ----------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[1]
JOB_SCRIPT = "import sys; from app.ingestion.jobs import process_document_job; process_document_job(sys.argv[1])"


async def test_AC_045_health_and_the_kb_list_answer_in_time_while_a_job_waits_30_seconds_for_docling(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The job runs in a process of its own, as the worker service does, on a document whose Docling answers after 30
    seconds. While Docling holds the request, `GET /api/health` and `GET /api/kbs` are called (every other API is
    served by the same app): both answer within HEALTH_CHECK_TIMEOUT_SECONDS plus 1 second. Docling is released at
    the end, instead of waiting for the 30 seconds, and the job then finishes READY."""
    limit = 1.0
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(User(username="anna", password_hash=hash_password("correct-horse-battery"), role="admin"))
        await session.commit()
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(delay_seconds=30.0) as docling:
        settings = settings_for(tmp_path, ollama.url, docling.url, health_check_timeout_seconds=limit)
        document_id = await add_document(engine, tmp_path, fake_document_file(MANUAL))
        environment = {
            **os.environ,
            "DATABASE_URL": TEST_DATABASE_URL,
            "REDIS_URL": TEST_REDIS_URL,
            "OLLAMA_BASE_URL": ollama.url,
            "DOCLING_BASE_URL": docling.url,
            "UPLOAD_DIR": str(tmp_path),
            "EMBEDDING_MODEL": MODEL,
            "EMBEDDING_DIMENSIONS": str(DIMENSIONS),
            "CHUNK_MIN_WORDS": "1",
            "CHUNK_TARGET_WORDS": "10",
            "CHUNK_MAX_WORDS": "20",
            "CHUNK_OVERLAP_PERCENT": "0",
        }
        job = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            JOB_SCRIPT,
            str(document_id),
            cwd=BACKEND_DIR,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(60):
                while not docling.requests:
                    assert job.returncode is None, "the job ended before Docling was called"
                    await asyncio.sleep(0.05)
            assert (await state_of(engine, document_id))["status"] == "PROCESSING"

            app = create_app(settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
                login = await api.post(
                    "/api/auth/login", json={"username": "anna", "password": "correct-horse-battery"}
                )
                assert login.status_code == 200
                timings: dict[str, float] = {}
                for path in ("/api/health", "/api/kbs"):
                    started = time.monotonic()
                    response = await api.get(path)
                    timings[path] = time.monotonic() - started
                    assert response.status_code == 200, path
                health = (await api.get("/api/health")).json()

            assert job.returncode is None  # the job is still waiting for Docling
            assert max(timings.values()) <= limit + 1, timings
            assert health["checks"]["docling"] == "ok"
            assert health["checks"]["database"] == "ok"
        finally:
            docling.release.set()  # do not wait the 30 seconds out
            try:
                _, errors = await asyncio.wait_for(job.communicate(), 60)
            except TimeoutError:
                job.kill()
                raise
        assert job.returncode == 0, (errors or b"").decode(errors="replace")[-2000:]

    assert (await state_of(engine, document_id))["status"] == "READY"
