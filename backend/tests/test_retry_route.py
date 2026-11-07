# SPEC-2026-11-07-fase-2-ingestion
"""Retry of a failed document: AC-036 (REQ-018), and the double press of the plan's review focus (REQ-007).

`POST /api/kbs/{kb_id}/documents/{document_id}/retry` (admin only) puts a FAILED document whose original still exists
back to QUEUED and on the ingestion queue (202); any other state is 409 `not_retryable`; a FAILED document without its
original is 409 `original_missing`. Pressing it twice, one after the other or together, queues the document once: the
second press is 409 `not_retryable` and Docling sees the file once. AC-012 (admin only on every route) belongs to T-06;
the tests here cite it for this route.

Real PostgreSQL and Redis of compose.test.yaml, the fake Docling and Ollama on local sockets, fresh state per test, an
upload folder inside pytest's tmp_path.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from rq import Queue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User
from app.ingestion import jobs
from app.ingestion.jobs import process_document_job
from app.ingestion.pipeline import process_document
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import FakeDocling, fake_docling, fake_document_file, fake_ollama

DIMENSIONS = 8
PASSWORD = "correct-horse-battery"
ITEMS = [
    ("section_header", "Introduzione", 1, 1),
    ("text", "Questo manuale spiega come usare il prodotto in ogni sua parte.", 1),
]
DOCUMENT_OUT_KEYS = {
    "id",
    "kb_id",
    "filename",
    "size_bytes",
    "status",
    "status_reason",
    "chunk_count",
    "created_at",
    "updated_at",
}


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no users, KBs, documents or profiles, nothing in Redis (sessions and queue)."""
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
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield engine
    await engine.dispose()


def settings_for(upload_dir: Path, ollama_url: str, docling_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upload_dir": upload_dir,
        "ollama_base_url": ollama_url,
        "docling_base_url": docling_url,
        "embedding_model": "test-model",
        "embedding_dimensions": DIMENSIONS,
        "chunk_min_words": 1,
        "chunk_target_words": 10,
        "chunk_max_words": 20,
        "chunk_overlap_percent": 0,
    }
    values.update(overrides)
    return make_settings(**values)


async def add_user(engine: AsyncEngine, username: str, role: str) -> uuid.UUID:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        user = User(username=username, password_hash=hash_password(PASSWORD), role=role)
        session.add(user)
        await session.commit()
        return user.id


async def add_kb(engine: AsyncEngine, name: str = "Manuali") -> uuid.UUID:
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": name}
        )
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


def put_original(upload_dir: Path, document_id: uuid.UUID, content: bytes) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / str(document_id)).write_bytes(content)


async def add_document(
    engine: AsyncEngine,
    upload_dir: Path,
    kb_id: uuid.UUID,
    status: str,
    original: bool = True,
    fmt: str = "pdf",
) -> uuid.UUID:
    """A document in the given status; FAILED and UNSUPPORTED get a reason, READY gets chunk_count 3."""
    document_id = uuid.uuid4()
    has_reason = status in ("FAILED", "UNSUPPORTED")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count)"
                " values (:id, :kb_id, :filename, :format, 1, :checksum, :status, :code, :message, :chunks)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "filename": f"manuale.{fmt}",
                "format": fmt,
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_unreachable" if has_reason else None,
                "message": "The document parsing service could not be reached" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
            },
        )
    if original:
        put_original(upload_dir, document_id, fake_document_file(ITEMS))
    return document_id


async def row_of(engine: AsyncEngine, document_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text(
                "select status, status_reason_code, status_reason_message, chunk_count, updated_at"
                " from documents where id = :id"
            ),
            {"id": document_id},
        )
        return dict(row.one()._mapping)


def only_job() -> Any:
    """The one job on the queue (the test fails, with the number, if there is none or more than one)."""
    found = queued_jobs()
    assert len(found) == 1, f"{len(found)} jobs on the queue"
    return found[0]


def queued_jobs() -> list[Any]:
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        return list(Queue("ingestion", connection=connection).jobs)
    finally:
        connection.close()


@asynccontextmanager
async def signed_in(
    engine: AsyncEngine, upload_dir: Path, username: str, role: str
) -> AsyncIterator[httpx.AsyncClient]:
    await add_user(engine, username, role)
    app = create_app(make_settings(upload_dir=upload_dir))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        response = await api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200
        yield api


@pytest.fixture
async def admin(engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with signed_in(engine, tmp_path, "anna", "admin") as api:
        yield api


@pytest.fixture
async def kb(engine: AsyncEngine) -> uuid.UUID:
    return await add_kb(engine)


def retry_url(kb_id: uuid.UUID | str, document_id: uuid.UUID | str) -> str:
    return f"/api/kbs/{kb_id}/documents/{document_id}/retry"


def error_of(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    return dict(body["error"])


async def run_queued_job(engine: AsyncEngine, tmp_path: Path, ollama_url: str, docling_url: str) -> None:
    """What the worker does with the job on the queue: it runs the function the job names, on the document it names."""
    job = only_job()
    assert job.func_name == "app.ingestion.jobs.process_document_job"
    settings = settings_for(tmp_path, ollama_url, docling_url)
    original = jobs.get_settings
    jobs.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        # asyncio.run inside the job needs a thread without a running event loop
        await asyncio.to_thread(process_document_job, *job.args)
    finally:
        jobs.get_settings = original  # type: ignore[assignment]


# AC-036: FAILED with the original -> QUEUED -> READY ------------------------------------------------------------------


async def fail_with_docling_down(
    engine: AsyncEngine, tmp_path: Path, kb: uuid.UUID, ollama_url: str, docling: FakeDocling
) -> uuid.UUID:
    """A PDF that Docling could not reach: the document is FAILED and its original is kept."""

    async def no_sleep(seconds: float) -> None:
        return None

    document_id = await add_document(engine, tmp_path, kb, "QUEUED")
    docling.fail_status = 503
    await process_document(document_id, settings_for(tmp_path, ollama_url, docling.url), sleep=no_sleep)
    state = await row_of(engine, document_id)
    assert (state["status"], state["status_reason_code"]) == ("FAILED", "docling_error")
    assert (tmp_path / str(document_id)).is_file()
    return document_id


async def test_AC_036_a_failed_document_with_its_original_goes_back_to_queued_and_then_to_ready(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        document_id = await fail_with_docling_down(engine, tmp_path, kb, ollama.url, docling)
        before = await row_of(engine, document_id)
        assert queued_jobs() == []
        docling.fail_status = None  # Docling is reachable again

        response = await admin.post(retry_url(kb, document_id))

        assert response.status_code == 202
        body = response.json()
        assert set(body) == DOCUMENT_OUT_KEYS
        assert (body["id"], body["kb_id"], body["filename"]) == (str(document_id), str(kb), "manuale.pdf")
        assert body["status"] == "QUEUED"
        assert body["status_reason"] is None
        assert body["chunk_count"] == 0
        after = await row_of(engine, document_id)
        assert (after["status"], after["status_reason_code"], after["status_reason_message"]) == ("QUEUED", None, None)
        assert after["updated_at"] > before["updated_at"]
        job = only_job()
        assert job.func_name == "app.ingestion.jobs.process_document_job"
        assert tuple(job.args) == (str(document_id),)
        assert (tmp_path / str(document_id)).is_file()

        await run_queued_job(engine, tmp_path, ollama.url, docling.url)

    final = await admin.get(f"/api/kbs/{kb}/documents/{document_id}")
    assert final.json()["status"] == "READY"
    assert final.json()["status_reason"] is None
    assert final.json()["chunk_count"] >= 1
    assert not (tmp_path / str(document_id)).exists()
    assert len(docling.requests) == 3 + 1  # three failed attempts, then the retry


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/json"}])
async def test_AC_036_the_retry_needs_no_body_and_takes_json_or_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path, headers: dict[str, str]
) -> None:
    """The UI presses the button with a POST without body (no Content-Type); a JSON content type is fine too."""
    document_id = await add_document(engine, tmp_path, kb, "FAILED")

    response = await admin.post(retry_url(kb, document_id), headers=headers, content=b"{}" if headers else None)

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"


# AC-036: what cannot be retried --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["READY", "QUEUED", "PROCESSING", "UNSUPPORTED"])
async def test_AC_036_a_document_that_is_not_failed_answers_409_not_retryable_and_nothing_changes(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path, status: str
) -> None:
    document_id = await add_document(engine, tmp_path, kb, status, original=status in ("QUEUED", "PROCESSING"))
    before = await row_of(engine, document_id)

    response = await admin.post(retry_url(kb, document_id))

    assert response.status_code == 409
    assert error_of(response)["code"] == "not_retryable"
    assert error_of(response)["message"]
    assert await row_of(engine, document_id) == before
    assert queued_jobs() == []


async def test_AC_036_a_failed_document_whose_original_is_gone_answers_409_original_missing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED", original=False)
    before = await row_of(engine, document_id)

    response = await admin.post(retry_url(kb, document_id))

    assert response.status_code == 409
    assert error_of(response)["code"] == "original_missing"
    assert await row_of(engine, document_id) == before  # still FAILED, with its reason
    assert queued_jobs() == []


# Review focus: the double press ---------------------------------------------------------------------------------------


async def test_AC_036_pressing_retry_twice_in_a_row_gives_409_not_retryable_and_the_document_is_processed_once(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        document_id = await add_document(engine, tmp_path, kb, "FAILED")

        first = await admin.post(retry_url(kb, document_id))
        second = await admin.post(retry_url(kb, document_id))

        assert first.status_code == 202
        assert second.status_code == 409
        assert error_of(second)["code"] == "not_retryable"
        assert len(queued_jobs()) == 1
        await run_queued_job(engine, tmp_path, ollama.url, docling.url)
        third = await admin.post(retry_url(kb, document_id))  # READY by now

    assert third.status_code == 409
    assert error_of(third)["code"] == "not_retryable"
    assert (await row_of(engine, document_id))["status"] == "READY"
    assert len(docling.requests) == 1


async def test_AC_036_two_retries_arriving_together_queue_the_document_once(
    engine: AsyncEngine, tmp_path: Path, kb: uuid.UUID
) -> None:
    """Two admins (two sessions, two apps) press at the same moment: one gets 202, the other 409 not_retryable."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        document_id = await add_document(engine, tmp_path, kb, "FAILED")
        async with (
            signed_in(engine, tmp_path, "anna", "admin") as first,
            signed_in(engine, tmp_path, "berta", "admin") as second,
        ):
            responses = await asyncio.gather(
                first.post(retry_url(kb, document_id)), second.post(retry_url(kb, document_id))
            )

        assert sorted(response.status_code for response in responses) == [202, 409]
        refused = next(response for response in responses if response.status_code == 409)
        assert error_of(refused)["code"] == "not_retryable"
        assert len(queued_jobs()) == 1
        await run_queued_job(engine, tmp_path, ollama.url, docling.url)

    assert (await row_of(engine, document_id))["status"] == "READY"
    assert len(docling.requests) == 1


# Unknown ids ----------------------------------------------------------------------------------------------------------


async def test_AC_036_unknown_non_uuid_and_foreign_document_ids_get_the_same_404_document_not_found(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    other_kb = await add_kb(engine, "Altra")
    foreign = await add_document(engine, tmp_path, other_kb, "FAILED")
    before = await row_of(engine, foreign)

    answers = [
        await admin.post(retry_url(kb, uuid.uuid4())),
        await admin.post(retry_url(kb, "non-un-uuid")),
        await admin.post(retry_url(kb, foreign)),  # a document of another KB
    ]

    assert [response.status_code for response in answers] == [404, 404, 404]
    assert {response.text for response in answers} == {answers[0].text}
    assert error_of(answers[0])["code"] == "document_not_found"
    assert await row_of(engine, foreign) == before
    assert queued_jobs() == []


async def test_AC_036_an_unknown_or_non_uuid_kb_gets_404_kb_not_found(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")

    answers = [await admin.post(retry_url(uuid.uuid4(), document_id)), await admin.post(retry_url("xx", document_id))]

    assert [response.status_code for response in answers] == [404, 404]
    assert {error_of(response)["code"] for response in answers} == {"kb_not_found"}
    assert (await row_of(engine, document_id))["status"] == "FAILED"


# AC-012 for this route: admin only ------------------------------------------------------------------------------------


async def test_AC_012_a_user_assigned_to_the_kb_gets_403_and_nothing_changes(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    before = await row_of(engine, document_id)
    async with signed_in(engine, tmp_path, "ugo", "user") as user:
        async with engine.begin() as connection:
            await connection.execute(
                text("insert into kb_members (kb_id, user_id) select :kb, id from users where username = 'ugo'"),
                {"kb": kb},
            )

        answers = [
            await user.post(retry_url(kb, document_id)),
            await user.post(retry_url(uuid.uuid4(), uuid.uuid4())),  # the same 403 whatever the ids
            await user.post(retry_url("xx", "yy")),
        ]

    assert [response.status_code for response in answers] == [403, 403, 403]
    assert {error_of(response)["code"] for response in answers} == {"forbidden"}
    assert await row_of(engine, document_id) == before
    assert queued_jobs() == []


async def test_AC_012_without_a_session_the_retry_gets_401_and_nothing_changes(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    before = await row_of(engine, document_id)
    app = create_app(make_settings(upload_dir=tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anonymous:
        response = await anonymous.post(retry_url(kb, document_id))

    assert response.status_code == 401
    assert error_of(response)["code"] == "not_authenticated"
    assert await row_of(engine, document_id) == before
    assert queued_jobs() == []


async def test_AC_036_a_retry_of_a_document_that_failed_again_can_be_pressed_again(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    """FAILED -> QUEUED -> FAILED -> QUEUED: the original is kept while the document fails, so it can go on."""

    async def no_sleep(seconds: float) -> None:
        return None

    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url, closed_port_url())
        document_id = await add_document(engine, tmp_path, kb, "FAILED")
        for _ in range(2):
            assert (await admin.post(retry_url(kb, document_id))).status_code == 202
            await process_document(document_id, settings, sleep=no_sleep)
            state = await row_of(engine, document_id)
            assert (state["status"], state["status_reason_code"]) == ("FAILED", "docling_unreachable")
            assert (tmp_path / str(document_id)).is_file()
