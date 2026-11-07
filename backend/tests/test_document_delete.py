# SPEC-2026-11-07-fase-2-ingestion
"""Deleting a document or a knowledge base leaves nothing behind, not even while a job runs: AC-037, AC-038, AC-039
(REQ-019), and AC-012 on the five document routes (REQ-005).

`DELETE /api/kbs/{kb_id}/documents/{document_id}` (admin only) answers 204 and removes the document, its chunks (by
cascade) and its original at once; a document of another KB, an unknown one and an id that is not a UUID get the same
404 `document_not_found`. A job that is processing the document when it is deleted writes nothing: no chunk, no READY,
no FAILED row, no file, and nothing in its log at WARNING or above. `DELETE /api/kbs/{kb_id}` removes the documents
and chunks by cascade and the originals of the deleted documents afterwards (best effort; the hourly sweep of
`cleanup_originals` is the safety net).

Real PostgreSQL and Redis of compose.test.yaml, the fake Ollama and Docling on local sockets (never the real
services), fresh state per test, an upload folder inside pytest's tmp_path. The slow Docling of AC-038 is held and
released by the test: nothing sleeps for real.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from app.documents.repository import DocumentRepository
from app.documents.storage import UploadStorage
from app.ingestion import pipeline
from app.ingestion.cleanup import cleanup_originals
from app.ingestion.pipeline import process_document
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_docling, fake_document_file, fake_ollama

DIMENSIONS = 8
PASSWORD = "correct-horse-battery"
AJAX = {"X-Requested-With": "XMLHttpRequest"}
TEXT = "Prima riga del racconto. Seconda parte del racconto, con accenti: è à ò ù. Terza e ultima parte.".encode()
MANUAL = [
    ("section_header", "Introduzione", 1, 1),
    ("text", "Questo manuale spiega come usare il prodotto in ogni sua parte.", 1),
]
NOT_FOUND = {"error": {"code": "document_not_found", "message": "Document not found"}}


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


@pytest.fixture(autouse=True)
def loggers_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alembic's logging setup can disable the loggers created before it ran: the log assertions need them on."""
    for name in ("app.ingestion.pipeline", "app.documents.routes", "app.knowledge_bases.routes"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def pipeline_settings(upload_dir: Path, ollama_url: str | None = None, docling_url: str | None = None) -> Settings:
    return make_settings(
        upload_dir=upload_dir,
        ollama_base_url=ollama_url or closed_port_url(),
        docling_base_url=docling_url or closed_port_url(),
        embedding_model="test-model",
        embedding_dimensions=DIMENSIONS,
        chunk_min_words=1,
        chunk_target_words=10,
        chunk_max_words=20,
        chunk_overlap_percent=0,
    )


async def no_sleep(seconds: float) -> None:
    return None


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


async def assign(engine: AsyncEngine, kb_id: uuid.UUID, username: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("insert into kb_members (kb_id, user_id) select :kb, id from users where username = :name"),
            {"kb": kb_id, "name": username},
        )


def put_original(upload_dir: Path, document_id: uuid.UUID, content: bytes) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / str(document_id)).write_bytes(content)


async def add_document(
    engine: AsyncEngine,
    upload_dir: Path,
    kb_id: uuid.UUID,
    status: str,
    *,
    original: bool = True,
    fmt: str = "txt",
    content: bytes = TEXT,
) -> uuid.UUID:
    """A document inserted in the given status (READY gets chunk_count 3 and no real chunk)."""
    document_id = uuid.uuid4()
    has_reason = status in ("FAILED", "UNSUPPORTED")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count)"
                " values (:id, :kb_id, :filename, :format, :size, :checksum, :status, :code, :message, :chunks)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "filename": f"nota.{fmt}",
                "format": fmt,
                "size": len(content),
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_unreachable" if has_reason else None,
                "message": "The document parsing service could not be reached" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
            },
        )
    if original:
        put_original(upload_dir, document_id, content)
    return document_id


async def ready_document(engine: AsyncEngine, upload_dir: Path, kb_id: uuid.UUID, ollama_url: str) -> uuid.UUID:
    """A document that the pipeline took to READY: it has chunks and no original."""
    document_id = await add_document(engine, upload_dir, kb_id, "QUEUED")
    await process_document(document_id, pipeline_settings(upload_dir, ollama_url), sleep=no_sleep)
    assert (await state_of(engine, document_id)) == ("READY", None)
    assert await chunks_of(engine, document_id) >= 1
    assert not (upload_dir / str(document_id)).exists()
    return document_id


async def state_of(engine: AsyncEngine, document_id: uuid.UUID) -> tuple[str, str | None] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("select status, status_reason_code from documents where id = :id"), {"id": document_id}
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])


async def chunks_of(engine: AsyncEngine, document_id: uuid.UUID) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("select count(*) from chunks where document_id = :id"), {"id": document_id}
        )
    assert isinstance(count, int)
    return count


async def count_rows(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(text(f"select count(*) from {table}"))  # noqa: S608 (fixed table names)
    assert isinstance(count, int)
    return count


def queued_jobs() -> list[Any]:
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        return list(Queue("ingestion", connection=connection).jobs)
    finally:
        connection.close()


def files_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def error_of(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"}
    return dict(body["error"])


def document_url(kb_id: uuid.UUID | str, document_id: uuid.UUID | str) -> str:
    return f"/api/kbs/{kb_id}/documents/{document_id}"


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


async def wait_until(condition: Callable[[], Awaitable[bool] | bool], what: str, seconds: float = 20.0) -> None:
    """Wait for a condition (polling, with a deadline): never a fixed sleep."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        result = condition()
        if (await result) if isinstance(result, Awaitable) else result:
            return
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


# AC-037: a document is deleted with its chunks and its original -------------------------------------------------------


async def test_AC_037_a_ready_document_with_chunks_is_deleted_and_nothing_of_it_remains(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        target = await ready_document(engine, tmp_path, kb, ollama.url)
        kept = await ready_document(engine, tmp_path, kb, ollama.url)
    kept_chunks = await chunks_of(engine, kept)

    response = await admin.delete(document_url(kb, target))

    assert response.status_code == 204
    assert response.content == b""
    after = await admin.get(document_url(kb, target))
    assert after.status_code == 404
    assert after.json() == NOT_FOUND
    assert await state_of(engine, target) is None
    assert await chunks_of(engine, target) == 0
    assert not (tmp_path / str(target)).exists()
    # the other document of the KB is untouched
    assert (await admin.get(document_url(kb, kept))).status_code == 200
    assert await chunks_of(engine, kept) == kept_chunks
    assert [d["id"] for d in (await admin.get(f"/api/kbs/{kb}/documents")).json()] == [str(kept)]


async def test_AC_037_a_failed_document_is_deleted_with_its_original(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    assert (tmp_path / str(document_id)).is_file()

    response = await admin.delete(document_url(kb, document_id))

    assert response.status_code == 204
    assert (await admin.get(document_url(kb, document_id))).json() == NOT_FOUND
    assert await state_of(engine, document_id) is None
    assert not (tmp_path / str(document_id)).exists()
    assert files_in(tmp_path) == []


@pytest.mark.parametrize("status", ["QUEUED", "UNSUPPORTED"])
async def test_AC_037_a_queued_or_unsupported_document_can_be_deleted_too(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path, status: str
) -> None:
    document_id = await add_document(engine, tmp_path, kb, status, original=status == "QUEUED")

    response = await admin.delete(document_url(kb, document_id))

    assert response.status_code == 204
    assert await state_of(engine, document_id) is None
    assert files_in(tmp_path) == []


async def test_AC_037_deleting_a_queued_document_makes_its_waiting_job_do_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED")
        assert (await admin.delete(document_url(kb, document_id))).status_code == 204

        await process_document(document_id, pipeline_settings(tmp_path, ollama.url), sleep=no_sleep)

    assert await state_of(engine, document_id) is None
    assert await count_rows(engine, "documents") == 0
    assert await count_rows(engine, "chunks") == 0
    assert files_in(tmp_path) == []
    assert ollama.requests == []


async def test_AC_037_the_second_delete_of_the_same_document_is_a_404(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")

    first = await admin.delete(document_url(kb, document_id))
    second = await admin.delete(document_url(kb, document_id))

    assert (first.status_code, second.status_code) == (204, 404)
    assert second.json() == NOT_FOUND


async def test_AC_037_two_deletes_arriving_together_give_one_204_and_one_404(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    async with (
        signed_in(engine, tmp_path, "anna", "admin") as first,
        signed_in(engine, tmp_path, "berta", "admin") as second,
    ):
        answers = await asyncio.gather(
            first.delete(document_url(kb, document_id)), second.delete(document_url(kb, document_id))
        )

    assert sorted(answer.status_code for answer in answers) == [204, 404]
    assert await state_of(engine, document_id) is None
    assert files_in(tmp_path) == []


async def test_AC_037_an_unknown_non_uuid_or_foreign_document_gets_the_same_404_and_nothing_is_deleted(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    other_kb = await add_kb(engine, "Altra")
    foreign = await add_document(engine, tmp_path, other_kb, "FAILED")
    own = await add_document(engine, tmp_path, kb, "FAILED")

    answers = [
        await admin.delete(document_url(kb, uuid.uuid4())),
        await admin.delete(document_url(kb, "non-un-uuid")),
        await admin.delete(document_url(kb, foreign)),  # a document of another KB
    ]

    assert [answer.status_code for answer in answers] == [404, 404, 404]
    assert [answer.json() for answer in answers] == [NOT_FOUND] * 3
    assert await state_of(engine, foreign) == ("FAILED", "docling_unreachable")
    assert (tmp_path / str(foreign)).is_file()  # the original of a document of another KB is not touched either
    assert (tmp_path / str(own)).is_file()


async def test_AC_037_an_unknown_or_non_uuid_kb_gets_404_kb_not_found(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")

    answers = [
        await admin.delete(document_url(uuid.uuid4(), document_id)),
        await admin.delete(document_url("xx", document_id)),
    ]

    assert [answer.status_code for answer in answers] == [404, 404]
    assert {error_of(answer)["code"] for answer in answers} == {"kb_not_found"}
    assert await state_of(engine, document_id) is not None
    assert (tmp_path / str(document_id)).is_file()


async def test_AC_037_an_original_that_cannot_be_removed_does_not_fail_the_delete_and_the_sweep_takes_it_later(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    original_remove = UploadStorage.remove

    def broken(self: UploadStorage, target: uuid.UUID) -> bool:
        raise PermissionError("not allowed: nome-file-riservato")

    monkeypatch.setattr(UploadStorage, "remove", broken)
    caplog.set_level(logging.INFO)

    response = await admin.delete(document_url(kb, document_id))

    assert response.status_code == 204
    assert await state_of(engine, document_id) is None  # the document is gone whatever happens to the file
    assert (tmp_path / str(document_id)).is_file()
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert str(document_id) in messages
    assert "nome-file-riservato" not in messages
    monkeypatch.setattr(UploadStorage, "remove", original_remove)
    stale = datetime.now(UTC) + timedelta(hours=2)

    await cleanup_originals(pipeline_settings(tmp_path), stale)  # the hourly sweep: a file without a document

    assert files_in(tmp_path) == []


# AC-038: a document deleted while it is processed ---------------------------------------------------------------------


async def start_job(document_id: uuid.UUID, tmp_path: Path, ollama_url: str, docling_url: str) -> asyncio.Task[None]:
    return asyncio.create_task(
        process_document(document_id, pipeline_settings(tmp_path, ollama_url, docling_url), sleep=no_sleep)
    )


def pipeline_problems(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The log records of the pipeline at WARNING or above, and those that carry an exception."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("app.ingestion") and (record.levelno >= logging.WARNING or record.exc_info)
    ]


def pipeline_logged(caplog: pytest.LogCaptureFixture) -> bool:
    return any(record.name == "app.ingestion.pipeline" for record in caplog.records)


async def assert_nothing_left(engine: AsyncEngine, document_id: uuid.UUID, tmp_path: Path) -> None:
    assert await state_of(engine, document_id) is None  # no document, and so no FAILED row
    assert await count_rows(engine, "documents") == 0
    assert await count_rows(engine, "chunks") == 0
    assert files_in(tmp_path) == []


async def test_AC_038_a_document_deleted_while_docling_is_slow_does_not_come_back_when_the_job_ends(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(delay_seconds=60.0) as docling:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED", fmt="pdf", content=fake_document_file(MANUAL))
        job = await start_job(document_id, tmp_path, ollama.url, docling.url)
        await wait_until(lambda: len(docling.requests) == 1, "Docling to receive the file")
        assert await state_of(engine, document_id) == ("PROCESSING", None)

        response = await admin.delete(document_url(kb, document_id))

        assert response.status_code == 204
        assert not (tmp_path / str(document_id)).exists()
        docling.release.set()  # Docling answers at last
        await asyncio.wait_for(job, 30)

    assert ollama.requests  # the job did go on to the end: its work was done and then discarded
    await assert_nothing_left(engine, document_id, tmp_path)
    assert (await admin.get(document_url(kb, document_id))).json() == NOT_FOUND
    assert (await admin.get(f"/api/kbs/{kb}/documents")).json() == []
    assert pipeline_logged(caplog)
    assert pipeline_problems(caplog) == []


async def test_AC_038_a_delete_between_the_saving_of_the_chunks_and_the_ready_mark_takes_the_chunks_with_it(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The chunks are committed and verified; then the admin deletes the document; the job's READY mark and its
    removal of the original find nothing to do and fail nothing."""
    caplog.set_level(logging.INFO)
    verify = pipeline.verify_indexes
    during: dict[str, int] = {}

    async def verify_then_delete(*args: Any, **kwargs: Any) -> bool:
        verified = await verify(*args, **kwargs)
        document_id = args[1]
        during["chunks"] = await chunks_of(engine, document_id)
        during["status"] = (await admin.delete(document_url(kb, document_id))).status_code
        return verified

    monkeypatch.setattr(pipeline, "verify_indexes", verify_then_delete)
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED")

        await process_document(document_id, pipeline_settings(tmp_path, ollama.url), sleep=no_sleep)

    assert during["chunks"] >= 1  # the chunks existed when the document was deleted
    assert during["status"] == 204
    await assert_nothing_left(engine, document_id, tmp_path)
    assert pipeline_problems(caplog) == []


async def test_AC_038_a_document_deleted_before_the_job_reads_its_original_ends_quietly(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The job took the document (PROCESSING) and the admin deleted it before the original was read: the missing file
    is not an error of the document, there is no document left to fail."""
    caplog.set_level(logging.INFO)
    claim = DocumentRepository.claim

    async def claim_then_delete(self: DocumentRepository, document_id: uuid.UUID) -> Any:
        claimed = await claim(self, document_id)
        assert claimed is not None
        assert (await admin.delete(document_url(kb, document_id))).status_code == 204
        return claimed

    monkeypatch.setattr(DocumentRepository, "claim", claim_then_delete)
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling() as docling:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED")
        pdf_id = await add_document(engine, tmp_path, kb, "QUEUED", fmt="pdf", content=fake_document_file(MANUAL))

        for target in (document_id, pdf_id):
            await process_document(target, pipeline_settings(tmp_path, ollama.url, docling.url), sleep=no_sleep)

        assert ollama.requests == []
        assert docling.requests == []

    for target in (document_id, pdf_id):
        assert await state_of(engine, target) is None
    assert await count_rows(engine, "chunks") == 0
    assert files_in(tmp_path) == []
    assert pipeline_logged(caplog)
    assert pipeline_problems(caplog) == []


async def test_AC_038_a_missing_original_of_a_document_that_still_exists_is_still_an_unexpected_error(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    """Only a deleted document is told apart: if the row is there and the file is not, the document fails."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED", original=False)

        await process_document(document_id, pipeline_settings(tmp_path, ollama.url), sleep=no_sleep)

    assert await state_of(engine, document_id) == ("FAILED", "unexpected_error")


# AC-039: a knowledge base is deleted with everything in it ------------------------------------------------------------


async def test_AC_039_deleting_a_kb_with_a_ready_and_a_failed_document_removes_documents_chunks_and_originals(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    other_kb = await add_kb(engine, "Altra")
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        ready = await ready_document(engine, tmp_path, kb, ollama.url)
        failed = await add_document(engine, tmp_path, kb, "FAILED")
        survivor = await ready_document(engine, tmp_path, other_kb, ollama.url)
        survivor_failed = await add_document(engine, tmp_path, other_kb, "FAILED")
    survivor_chunks = await chunks_of(engine, survivor)
    assert (tmp_path / str(failed)).is_file()

    response = await admin.delete(f"/api/kbs/{kb}")

    assert response.status_code == 204
    for document_id in (ready, failed):
        assert await state_of(engine, document_id) is None
        assert await chunks_of(engine, document_id) == 0
        assert not (tmp_path / str(document_id)).exists()
    assert (await admin.get(f"/api/kbs/{kb}")).status_code == 404
    # the other KB keeps its documents, its chunks and its original
    assert await state_of(engine, survivor) == ("READY", None)
    assert await chunks_of(engine, survivor) == survivor_chunks
    assert await state_of(engine, survivor_failed) == ("FAILED", "docling_unreachable")
    assert files_in(tmp_path) == [str(survivor_failed)]


async def test_AC_039_a_kb_deleted_while_one_of_its_documents_is_processed_leaves_nothing_when_the_job_ends(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with fake_ollama(dimensions=DIMENSIONS) as ollama, fake_docling(delay_seconds=60.0) as docling:
        document_id = await add_document(engine, tmp_path, kb, "QUEUED", fmt="pdf", content=fake_document_file(MANUAL))
        job = await start_job(document_id, tmp_path, ollama.url, docling.url)
        await wait_until(lambda: len(docling.requests) == 1, "Docling to receive the file")

        response = await admin.delete(f"/api/kbs/{kb}")

        assert response.status_code == 204
        docling.release.set()
        await asyncio.wait_for(job, 30)

    await assert_nothing_left(engine, document_id, tmp_path)
    assert pipeline_problems(caplog) == []


async def test_AC_039_an_original_that_cannot_be_removed_does_not_fail_the_kb_delete_and_the_sweep_takes_it_later(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = await add_document(engine, tmp_path, kb, "FAILED")
    await add_document(engine, tmp_path, kb, "FAILED")
    original_remove = UploadStorage.remove
    stuck = first  # the removal of this original fails, the other one must be removed anyway

    def broken(self: UploadStorage, target: uuid.UUID) -> bool:
        if target == stuck:
            raise PermissionError("not allowed: nome-file-riservato")
        return original_remove(self, target)

    monkeypatch.setattr(UploadStorage, "remove", broken)
    caplog.set_level(logging.INFO)

    response = await admin.delete(f"/api/kbs/{kb}")

    assert response.status_code == 204
    assert await count_rows(engine, "documents") == 0
    assert files_in(tmp_path) == [str(stuck)]
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert str(stuck) in messages
    assert "nome-file-riservato" not in messages
    monkeypatch.setattr(UploadStorage, "remove", original_remove)

    await cleanup_originals(pipeline_settings(tmp_path), datetime.now(UTC) + timedelta(hours=2))

    assert files_in(tmp_path) == []


async def test_AC_039_deleting_an_unknown_kb_removes_no_original(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")

    answers = [await admin.delete(f"/api/kbs/{uuid.uuid4()}"), await admin.delete("/api/kbs/not-a-uuid")]

    assert [answer.status_code for answer in answers] == [404, 404]
    assert {error_of(answer)["code"] for answer in answers} == {"kb_not_found"}
    assert (tmp_path / str(document_id)).is_file()
    assert await state_of(engine, document_id) is not None


# AC-012: the five document routes are for the admin only --------------------------------------------------------------


def multipart_body(content: bytes) -> tuple[bytes, str]:
    boundary = "----deleteboundary7MA4YWxkTrZu0gW"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="nuovo.txt"\r\n'
    body = head.encode() + b"Content-Type: text/plain\r\n\r\n" + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


async def call_the_five_routes(
    api: httpx.AsyncClient, kb_id: uuid.UUID | str, document_id: uuid.UUID | str
) -> list[httpx.Response]:
    body, content_type = multipart_body(b"contenuto nuovo del documento")
    return [
        await api.post(f"/api/kbs/{kb_id}/documents", content=body, headers={**AJAX, "Content-Type": content_type}),
        await api.get(f"/api/kbs/{kb_id}/documents"),
        await api.get(document_url(kb_id, document_id)),
        await api.post(f"{document_url(kb_id, document_id)}/retry"),
        await api.delete(document_url(kb_id, document_id)),
    ]


async def nothing_changed(engine: AsyncEngine, tmp_path: Path, document_id: uuid.UUID) -> None:
    assert await state_of(engine, document_id) == ("FAILED", "docling_unreachable")
    assert await count_rows(engine, "documents") == 1
    assert files_in(tmp_path) == [str(document_id)]
    assert queued_jobs() == []


async def test_AC_012_a_user_assigned_to_the_kb_gets_403_on_upload_list_detail_retry_and_delete(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    async with signed_in(engine, tmp_path, "ugo", "user") as user:
        await assign(engine, kb, "ugo")

        answers = await call_the_five_routes(user, kb, document_id)

    assert [answer.status_code for answer in answers] == [403] * 5
    assert {error_of(answer)["code"] for answer in answers} == {"forbidden"}
    await nothing_changed(engine, tmp_path, document_id)


async def test_AC_012_a_user_not_assigned_to_the_kb_gets_the_same_403_whatever_the_ids(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    async with signed_in(engine, tmp_path, "ugo", "user") as user:
        answers = [
            *await call_the_five_routes(user, kb, document_id),
            *await call_the_five_routes(user, uuid.uuid4(), uuid.uuid4()),
            *await call_the_five_routes(user, "xx", "yy"),
        ]

    assert [answer.status_code for answer in answers] == [403] * 15
    assert {answer.text for answer in answers} == {answers[0].text}
    await nothing_changed(engine, tmp_path, document_id)


async def test_AC_012_without_a_session_the_five_routes_get_401(
    engine: AsyncEngine, kb: uuid.UUID, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, kb, "FAILED")
    app = create_app(make_settings(upload_dir=tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anonymous:
        answers = await call_the_five_routes(anonymous, kb, document_id)

    assert [answer.status_code for answer in answers] == [401] * 5
    assert {error_of(answer)["code"] for answer in answers} == {"not_authenticated"}
    await nothing_changed(engine, tmp_path, document_id)
