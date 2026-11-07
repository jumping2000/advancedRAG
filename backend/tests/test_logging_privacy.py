# SPEC-2026-11-07-fase-2-ingestion
"""Nothing private in the logs: AC-046 (REQ-102).

A document with recognizable content and file name is uploaded and processed to READY; another is refused by Docling;
a third fails on the embeddings (after its attempts); the second is retried. The whole path runs: the upload and the
retry through the API, the job from the real RQ queue with a real RQ worker, the pipeline against the fake Docling and
Ollama. Every log record of the process is collected at INFO, the level the worker runs with, whatever its logger (the
root, the application, httpx and httpcore, SQLAlchemy with its statements, RQ) and with its traceback; none may hold the
content, the file name or a vector. The lines of the processing hold the id of the document, its status and the code of
the reason.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from rq import Queue, SimpleWorker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User
from app.db.session import create_engine
from app.documents.queue import INGESTION_QUEUE
from app.ingestion import jobs
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.services import fake_docling, fake_document_file, fake_ollama, fake_vector

DIMENSIONS = 8
PASSWORD = "correct-horse-battery"
AJAX = {"X-Requested-With": "XMLHttpRequest"}
NAME_OK, NAME_PARSE, NAME_EMBED = (
    "contratto-ACME-riservato.pdf",
    "bilancio-segreto-2024.pdf",
    "verbali-direzione-M7.pdf",
)
CONTENT_OK = "Zibaldone7731 clausola riservata sul compenso dell'amministratore delegato"
CONTENT_PARSE = "Quokka9921 allegato che Docling non riesce a leggere"
CONTENT_EMBED = "Sbirulino4410 verbale della riunione della direzione sulle acquisizioni"
LOGGERS = (
    "",
    "app",
    "httpx",
    "httpcore",
    "sqlalchemy",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "rq",
    "rq.worker",
    "asyncio",
)


class Collector(logging.Handler):
    """Every record it is given, as the text a log file would hold: message, arguments applied, and traceback."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.lines: list[tuple[str, str]] = []  # (logger name, text)

    def emit(self, record: logging.LogRecord) -> None:
        formatter = logging.Formatter()
        self.lines.append((record.name, formatter.format(record)))


@contextmanager
def all_logs() -> Iterator[Collector]:
    """Collect every record of every logger at INFO. The migrations of the schema fixture run Alembic's `fileConfig`,
    which disables every logger that exists at that moment (the application's, httpx, RQ): they are enabled again here
    for the time of the test, or nothing of them would be collected."""
    collector = Collector()
    loggers = [logging.getLogger(name) for name in LOGGERS]
    existing = [logger for logger in logging.root.manager.loggerDict.values() if isinstance(logger, logging.Logger)]
    saved = {logger: (logger.level, logger.propagate, logger.disabled) for logger in {*loggers, *existing}}
    for logger in saved:
        logger.disabled = False
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.addHandler(collector)
    try:
        yield collector
    finally:
        for logger in loggers:
            logger.removeHandler(collector)
        for logger, (level, propagate, disabled) in saved.items():
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


async def reset_state() -> AsyncEngine:
    engine = create_engine(make_settings().sqlalchemy_url)
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
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    return engine


def worker_settings(upload_dir: Path, ollama_url: str, docling_url: str) -> Settings:
    return make_settings(
        upload_dir=upload_dir,
        ollama_base_url=ollama_url,
        docling_base_url=docling_url,
        docling_api_key="docling-key-123",
        embedding_model="test-model",
        embedding_dimensions=DIMENSIONS,
        ingestion_retry_backoff_seconds=0,
        chunk_min_words=1,
        chunk_target_words=10,
        chunk_max_words=20,
        chunk_overlap_percent=0,
    )


def run_queue() -> None:
    """A real RQ worker takes every job of the queue `ingestion`, as the worker service does, and stops when empty."""
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        SimpleWorker([Queue(INGESTION_QUEUE, connection=connection)], connection=connection).work(burst=True)
    finally:
        connection.close()


async def admin_client(engine: AsyncEngine, upload_dir: Path) -> httpx.AsyncClient:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(User(username="anna", password_hash=hash_password(PASSWORD), role="admin"))
        await session.commit()
    app = create_app(make_settings(upload_dir=upload_dir))
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    login = await api.post("/api/auth/login", json={"username": "anna", "password": PASSWORD})
    assert login.status_code == 200
    return api


async def upload_all(upload_dir: Path) -> tuple[uuid.UUID, dict[str, str]]:
    """The KB, and the three documents (by name) uploaded through the API; their jobs are on the queue."""
    engine = await reset_state()
    api = await admin_client(engine, upload_dir)
    try:
        kb = (await api.post("/api/kbs", json={"name": "Riservato"})).json()["id"]
        files = {
            NAME_OK: fake_document_file([("text", CONTENT_OK, 1)]),
            NAME_PARSE: b"%PDF-1.4\nnon e' un documento " + CONTENT_PARSE.encode(),
            NAME_EMBED: fake_document_file([("text", CONTENT_EMBED, 1)]),
        }
        ids: dict[str, str] = {}
        for name, content in files.items():
            response = await api.post(
                f"/api/kbs/{kb}/documents", files={"file": (name, content, "application/pdf")}, headers=AJAX
            )
            assert response.status_code == 202, response.text
            ids[name] = response.json()["id"]
        return uuid.UUID(kb), ids
    finally:
        await api.aclose()
        await engine.dispose()


async def retry_and_read(upload_dir: Path, kb: uuid.UUID, ids: dict[str, str], retry: str | None) -> dict[str, Any]:
    """Optionally press Retry on one document, then list the documents of the KB by their id."""
    app = create_app(make_settings(upload_dir=upload_dir))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        login = await api.post("/api/auth/login", json={"username": "anna", "password": PASSWORD})
        assert login.status_code == 200
        if retry is not None:
            assert (await api.post(f"/api/kbs/{kb}/documents/{ids[retry]}/retry")).status_code == 202
        listed = (await api.get(f"/api/kbs/{kb}/documents")).json()
    return {document["id"]: document for document in listed}


def test_AC_046_no_log_of_a_processed_or_failed_document_holds_its_content_its_name_or_a_vector(
    schema: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        all_logs() as logs,
        fake_ollama(dimensions=DIMENSIONS, fail_status=500, fail_requests={2, 3, 4}) as ollama,
        fake_docling() as docling,
    ):
        settings = worker_settings(tmp_path, ollama.url, docling.url)
        monkeypatch.setattr(jobs, "get_settings", lambda: settings)

        kb, ids = asyncio.run(upload_all(tmp_path))
        run_queue()  # the first document is READY, the second FAILED parse_failed, the third FAILED embedding_error
        first = asyncio.run(retry_and_read(tmp_path, kb, ids, retry=NAME_PARSE))
        run_queue()  # the retried one fails again
        last = asyncio.run(retry_and_read(tmp_path, kb, ids, retry=None))

    assert first[ids[NAME_PARSE]]["status"] == "QUEUED"
    assert {
        name: (last[doc_id]["status"], (last[doc_id]["status_reason"] or {}).get("code"))
        for name, doc_id in ids.items()
    } == {
        NAME_OK: ("READY", None),
        NAME_PARSE: ("FAILED", "parse_failed"),
        NAME_EMBED: ("FAILED", "embedding_error"),
    }
    # Everything that was sent to Ollama for the first document, and the vectors it got back
    assert "Zibaldone7731" in " ".join(ollama.requests[0].texts)
    vectors = [fake_vector(sent, DIMENSIONS) for sent in ollama.requests[0].texts]

    names = [logger for logger, _ in logs.lines]
    assert any(name.startswith("app.") for name in names), (
        "the application logged nothing: the collection is not working"
    )
    assert any(name.startswith("rq") for name in names), "the RQ worker logged nothing: the collection is not working"
    assert any(name.startswith("sqlalchemy") for name in names), "no SQL statement was logged: nothing collected"
    assert any(name.startswith("httpx") for name in names), "no HTTP request was logged: nothing collected"

    forbidden = [
        NAME_OK, NAME_PARSE, NAME_EMBED, "ACME", "bilancio-segreto", "verbali-direzione",
        CONTENT_OK, CONTENT_PARSE, CONTENT_EMBED, "Zibaldone7731", "Quokka9921", "Sbirulino4410", "compenso",
    ]  # fmt: skip
    for vector in vectors:
        forbidden += [repr(number)[:9] for number in vector[:3]]
    leaks = [(logger, line) for logger, line in logs.lines for item in forbidden if item in line]
    assert leaks == []


def test_AC_046_the_lines_of_a_processing_hold_the_document_id_its_status_and_the_reason_code(
    schema: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        all_logs() as logs,
        fake_ollama(dimensions=DIMENSIONS, fail_status=500, fail_requests={2, 3, 4}) as ollama,
        fake_docling() as docling,
    ):
        settings = worker_settings(tmp_path, ollama.url, docling.url)
        monkeypatch.setattr(jobs, "get_settings", lambda: settings)
        _, ids = asyncio.run(upload_all(tmp_path))
        run_queue()

    def lines_of(name: str) -> list[str]:
        return [line for _, line in logs.lines if ids[name] in line]

    assert any("READY" in line for line in lines_of(NAME_OK))
    assert any("FAILED" in line and "parse_failed" in line for line in lines_of(NAME_PARSE))
    assert any("FAILED" in line and "embedding_error" in line for line in lines_of(NAME_EMBED))
    # The step and the attempt number of a step that is repeated (3 attempts for the embeddings of the third document)
    assert any(re.search(r"attempt\D*3", line) for line in lines_of(NAME_EMBED))
    assert not any("FAILED" in line for line in lines_of(NAME_OK))
