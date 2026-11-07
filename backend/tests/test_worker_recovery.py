# SPEC-2026-11-07-fase-2-ingestion
"""A worker that died in the middle of a job, and the start of the worker: the plan's review focus "Worker interrotto a
metà di un job" (REQ-007, REQ-017), AC-034 and AC-035 for the hourly run, and two small items of the reviews.

At its start the worker (`startup`) turns every PROCESSING document left by a dead worker into FAILED `unexpected_error`
("Processing was interrupted", chunks deleted, original kept: `recover_interrupted`), puts every QUEUED document back on
the queue (`requeue_pending`: the claim of the job makes a duplicate job harmless), and runs `cleanup_originals`; a
failing step is logged by its error type and does not stop the others or the worker. The same cleanup then runs every
hour in a daemon thread (`start_cleanup_loop`). Small items: a job that exceeds the timeout of the queue ends FAILED
`unexpected_error` with a message of its own, and running the Alembic migrations at the start of the backend does not
disable the loggers created before.

Real PostgreSQL and Redis of compose.test.yaml, the fake Ollama on a local socket (never the real service), fresh state
per test, an upload folder inside pytest's tmp_path. Time is injected (`now`), the hourly loop is given a tiny
interval and waited for by condition: no test sleeps for real.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from rq import Queue
from rq.timeouts import JobTimeoutException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import worker
from app.config import Settings
from app.documents.repository import DocumentRepository
from app.ingestion import cleanup
from app.ingestion.chunker import ChunkDraft
from app.ingestion.cleanup import CleanupReport, recover_interrupted, requeue_pending
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.persist import persist_chunks
from app.ingestion.pipeline import process_document
from app.ingestion.profile import ensure_profile
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_ollama

DIMENSIONS = 8
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SECRET = "contenuto-riservato-del-documento"
TEXT = "Prima riga del racconto. Seconda parte del racconto, con accenti: è à ò ù. Terza e ultima parte.".encode()
INTERRUPTED = ("FAILED", "unexpected_error", "Processing was interrupted")
JOB = "app.ingestion.jobs.process_document_job"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no profiles, KBs, documents or chunks, nothing in Redis (the queue); the HNSW indexes of
    earlier tests are dropped."""
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
    for name in ("app.worker", "app.ingestion.cleanup", "app.ingestion.pipeline"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


def settings_for(upload_dir: Path, ollama_url: str | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "upload_dir": upload_dir,
        "ollama_base_url": ollama_url or closed_port_url(),
        "docling_base_url": closed_port_url(),
        "embedding_model": "test-model",
        "embedding_dimensions": DIMENSIONS,
    }
    values.update(overrides)
    return make_settings(**values)


async def no_sleep(seconds: float) -> None:
    return None


async def add_document(
    engine: AsyncEngine,
    upload_dir: Path,
    status: str,
    *,
    changed: timedelta = timedelta(0),
    original: bool = True,
) -> uuid.UUID:
    """A TXT document in a KB of its own, in the given status, last changed `changed` before NOW."""
    document_id = uuid.uuid4()
    has_reason = status in ("FAILED", "UNSUPPORTED")
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": uuid.uuid4().hex}
        )
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count, created_at, updated_at)"
                " values (:id, :kb_id, 'nota.txt', 'txt', :size, :checksum, :status, :code, :message, :chunks,"
                " :changed_at, :changed_at)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "size": len(TEXT),
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_unreachable" if has_reason else None,
                "message": "The document parsing service could not be reached" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
                "changed_at": NOW - changed,
            },
        )
    if original:
        put_original(upload_dir, document_id, NOW - changed)
    return document_id


def put_original(upload_dir: Path, document_id: uuid.UUID, written: datetime) -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / str(document_id)
    path.write_bytes(TEXT)
    os.utime(path, (written.timestamp(), written.timestamp()))
    return path


async def processing_with_chunks(engine: AsyncEngine, upload_dir: Path, settings: Settings) -> uuid.UUID:
    """What a worker that died after saving the chunks leaves: PROCESSING, with chunks, with its original."""
    document_id = await add_document(engine, upload_dir, "QUEUED")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        assert await DocumentRepository(session).claim(document_id) is not None
    profile = await ensure_profile(engine, settings)
    drafts = [
        ChunkDraft(position=n, section_title=None, page_start=None, page_end=None, content=f"testo {n}", word_count=2)
        for n in range(3)
    ]
    async with sessions() as session:
        await persist_chunks(session, document_id, profile, drafts, [[0.1 * (n + 1)] * DIMENSIONS for n in range(3)])
    assert await chunks_of(engine, document_id) == 3
    return document_id


async def row_of(engine: AsyncEngine, document_id: uuid.UUID) -> tuple[str, str | None, str | None, int] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "select status, status_reason_code, status_reason_message, chunk_count from documents"
                    " where id = :id"
                ),
                {"id": document_id},
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1], row[2], row[3])


async def chunks_of(engine: AsyncEngine, document_id: uuid.UUID) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("select count(*) from chunks where document_id = :id"), {"id": document_id}
        )
    assert isinstance(count, int)
    return count


def queued_jobs() -> list[Any]:
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        return list(Queue("ingestion", connection=connection).jobs)
    finally:
        connection.close()


def queued_ids() -> list[str]:
    jobs = queued_jobs()
    assert {job.func_name for job in jobs} <= {JOB}
    return sorted(job.args[0] for job in jobs)


async def wait_until(condition: Callable[[], Awaitable[bool] | bool], what: str, seconds: float = 20.0) -> None:
    """Wait for a condition (polling, with a deadline): never a fixed sleep."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        result = condition()
        if (await result) if isinstance(result, Awaitable) else result:
            return
        assert asyncio.get_running_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


# recover_interrupted -------------------------------------------------------------------------------------------------


async def test_a_processing_document_with_chunks_left_by_a_dead_worker_becomes_failed_and_loses_its_chunks(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    document_id = await processing_with_chunks(engine, tmp_path, settings)

    recovered = await recover_interrupted(settings)

    assert recovered == 1
    assert await row_of(engine, document_id) == (*INTERRUPTED, 0)
    assert await chunks_of(engine, document_id) == 0
    assert (tmp_path / str(document_id)).is_file()  # the original is kept: the admin can retry


async def test_a_processing_document_without_chunks_also_becomes_failed(engine: AsyncEngine, tmp_path: Path) -> None:
    document_id = await add_document(engine, tmp_path, "PROCESSING")

    assert await recover_interrupted(settings_for(tmp_path)) == 1

    assert await row_of(engine, document_id) == (*INTERRUPTED, 0)
    assert (tmp_path / str(document_id)).is_file()


async def test_only_the_processing_documents_are_recovered_and_a_second_run_finds_nothing(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    others = {
        status: await add_document(engine, tmp_path, status) for status in ("QUEUED", "READY", "FAILED", "UNSUPPORTED")
    }
    before = {status: await row_of(engine, document_id) for status, document_id in others.items()}
    first = await add_document(engine, tmp_path, "PROCESSING")
    second = await add_document(engine, tmp_path, "PROCESSING")

    assert await recover_interrupted(settings_for(tmp_path)) == 2
    assert await recover_interrupted(settings_for(tmp_path)) == 0

    assert await row_of(engine, first) == (*INTERRUPTED, 0)
    assert await row_of(engine, second) == (*INTERRUPTED, 0)
    assert {status: await row_of(engine, document_id) for status, document_id in others.items()} == before


async def test_an_interrupted_document_can_be_retried_because_its_original_was_kept(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        document_id = await processing_with_chunks(engine, tmp_path, settings)
        await recover_interrupted(settings)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            assert await DocumentRepository(session).requeue(await kb_of(engine, document_id), document_id)

        await process_document(document_id, settings, sleep=no_sleep)

    row = await row_of(engine, document_id)
    assert row is not None and row[0] == "READY"
    assert await chunks_of(engine, document_id) == row[3] >= 1


async def kb_of(engine: AsyncEngine, document_id: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as connection:
        kb_id = await connection.scalar(text("select kb_id from documents where id = :id"), {"id": document_id})
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


async def test_the_recovery_logs_ids_and_counts_never_the_content(
    engine: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    document_id = await add_document(engine, tmp_path, "PROCESSING")
    caplog.set_level(logging.INFO)

    await recover_interrupted(settings_for(tmp_path))

    messages = " ".join(record.getMessage() for record in caplog.records if record.name.startswith("app."))
    assert str(document_id) in messages
    assert "nota.txt" not in messages
    assert TEXT.decode() not in messages


# requeue_pending ---------------------------------------------------------------------------------------------------


async def test_every_queued_document_is_put_back_on_the_queue_and_no_other(engine: AsyncEngine, tmp_path: Path) -> None:
    first = await add_document(engine, tmp_path, "QUEUED")
    second = await add_document(engine, tmp_path, "QUEUED")
    for status in ("PROCESSING", "READY", "FAILED", "UNSUPPORTED"):
        await add_document(engine, tmp_path, status)
    assert queued_jobs() == []

    requeued = await requeue_pending(settings_for(tmp_path))

    assert requeued == 2
    assert queued_ids() == sorted([str(first), str(second)])
    assert {job.timeout for job in queued_jobs()} == {3600}


async def test_a_second_run_queues_the_documents_again_and_the_documents_are_still_processed_once(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    first = await add_document(engine, tmp_path, "QUEUED")
    second = await add_document(engine, tmp_path, "QUEUED")
    settings_without_services = settings_for(tmp_path)
    await requeue_pending(settings_without_services)
    await requeue_pending(settings_without_services)
    assert queued_ids() == sorted([str(first), str(first), str(second), str(second)])

    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        settings = settings_for(tmp_path, ollama.url)
        for document_id in queued_ids():  # what the worker does with each job
            await process_document(uuid.UUID(document_id), settings, sleep=no_sleep)

    for document_id in (first, second):
        row = await row_of(engine, document_id)
        assert row is not None and row[0] == "READY"
        assert await chunks_of(engine, document_id) == row[3]
    assert len(ollama.texts_sent()) == 2  # one chunk per document, embedded once: the claim made the copies harmless


async def test_nothing_queued_means_nothing_requeued(engine: AsyncEngine, tmp_path: Path) -> None:
    await add_document(engine, tmp_path, "FAILED")

    assert await requeue_pending(settings_for(tmp_path)) == 0
    assert queued_jobs() == []


# startup: recovery, requeue and cleanup before the worker accepts jobs --------------------------------------------


async def run_startup(settings: Settings, now: datetime | None = NOW) -> None:
    # `startup` runs an event loop per step, like the worker does: it needs a thread without a running loop
    await asyncio.to_thread(worker.startup, settings, now)


async def a_world_to_clean(engine: AsyncEngine, tmp_path: Path) -> dict[str, Any]:
    return {
        "interrupted": await add_document(engine, tmp_path, "PROCESSING"),
        "waiting": await add_document(engine, tmp_path, "QUEUED"),
        "expired": await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=8)),
        "kept": await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=6)),
        "orphan": put_original(tmp_path, uuid.uuid4(), NOW - timedelta(hours=2)),
    }


async def test_the_start_recovers_requeues_and_cleans(engine: AsyncEngine, tmp_path: Path) -> None:
    world = await a_world_to_clean(engine, tmp_path)

    await run_startup(settings_for(tmp_path))

    assert await row_of(engine, world["interrupted"]) == (*INTERRUPTED, 0)
    assert (tmp_path / str(world["interrupted"])).is_file()
    assert queued_ids() == [str(world["waiting"])]
    assert not (tmp_path / str(world["expired"])).exists()
    assert (tmp_path / str(world["kept"])).is_file()
    assert (await row_of(engine, world["expired"]) or ("",))[0] == "FAILED"
    assert not world["orphan"].exists()


STEPS = {
    "recover_interrupted": "interrupted",
    "requeue_pending": "waiting",
    "cleanup_originals": "expired",
}


def step_done(world: dict[str, Any], tmp_path: Path, step: str, row: Any) -> bool:
    """Whether the effect of one start-up step is visible."""
    if step == "recover_interrupted":
        return bool(row[0] == "FAILED")
    if step == "requeue_pending":
        return str(world["waiting"]) in queued_ids()
    return not (tmp_path / str(world["expired"])).exists()


@pytest.mark.parametrize("broken", list(STEPS))
async def test_a_failing_step_is_logged_by_its_error_type_and_the_others_still_run(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    broken: str,
) -> None:
    world = await a_world_to_clean(engine, tmp_path)
    caplog.set_level(logging.INFO)

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cannot do it: {SECRET}")

    monkeypatch.setattr(cleanup, broken, failing)

    await run_startup(settings_for(tmp_path))  # does not raise

    interrupted_row = await row_of(engine, world["interrupted"])
    for step in STEPS:
        assert step_done(world, tmp_path, step, interrupted_row) is (step != broken), step
    records = [record for record in caplog.records if record.name == "app.worker"]
    messages = " ".join(record.getMessage() for record in records)
    assert "RuntimeError" in messages
    assert SECRET not in messages
    assert all(record.exc_info is None for record in records)


async def test_a_start_without_a_database_logs_each_step_and_returns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    settings = settings_for(tmp_path, database_url="postgresql+asyncpg://rag:rag@127.0.0.1:9/none")

    await run_startup(settings)  # three failures, no exception

    records = [record for record in caplog.records if record.name == "app.worker" and record.levelno >= logging.ERROR]
    assert len(records) == 3


# main: the order of things -----------------------------------------------------------------------------------------


async def test_the_worker_beats_then_runs_the_start_then_the_hourly_cleanup_and_only_then_takes_jobs(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)
    interrupted = await add_document(engine, tmp_path, "PROCESSING")
    events: list[str] = []
    real_startup = worker.startup

    def startup(given: Settings, now: datetime | None = None) -> None:
        assert given is settings
        real_startup(given, now)
        events.append("startup")

    class FakeWorker:
        def __init__(self, queues: list[Queue], **options: Any) -> None:
            assert [queue.name for queue in queues] == ["ingestion"]

        def work(self) -> None:
            events.append("work")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "start_heartbeat", lambda connection, *a, **k: events.append("heartbeat"))
    monkeypatch.setattr(worker, "startup", startup)
    monkeypatch.setattr(worker, "start_cleanup_loop", lambda given, *a, **k: events.append("loop"))
    monkeypatch.setattr(worker, "SimpleWorker", FakeWorker)

    await asyncio.to_thread(worker.main, [])

    assert events == ["heartbeat", "startup", "loop", "work"]
    assert (await row_of(engine, interrupted) or ("",))[0] == "FAILED"  # recovered before the first job could start


# the hourly cleanup --------------------------------------------------------------------------------------------------


def cleanup_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "worker-cleanup"]


async def test_the_hourly_cleanup_runs_again_and_again_until_it_is_stopped(engine: AsyncEngine, tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    stop = worker.start_cleanup_loop(settings, interval_seconds=0.05)
    try:
        first = put_original(tmp_path, uuid.uuid4(), datetime.now(UTC) - timedelta(hours=2))
        await wait_until(lambda: not first.exists(), "the first orphan to be swept")
        second = put_original(tmp_path, uuid.uuid4(), datetime.now(UTC) - timedelta(hours=2))
        await wait_until(lambda: not second.exists(), "the second orphan to be swept")
        young = put_original(tmp_path, uuid.uuid4(), datetime.now(UTC))
        await wait_until(lambda: len(cleanup_threads()) == 1, "the cleanup thread")
        assert cleanup_threads()[0].daemon
    finally:
        stop.set()

    await wait_until(lambda: cleanup_threads() == [], "the cleanup thread to end")
    assert young.is_file()  # a file younger than one hour is not swept


async def test_a_failing_run_of_the_hourly_cleanup_does_not_end_the_loop(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    calls: list[datetime] = []

    async def flaky(settings: Settings, now: datetime) -> CleanupReport:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError(f"first run fails: {SECRET}")
        return CleanupReport(expired_originals=0, orphan_files=0)

    monkeypatch.setattr(cleanup, "cleanup_originals", flaky)
    stop = worker.start_cleanup_loop(settings_for(tmp_path), interval_seconds=0.05)
    try:
        await wait_until(lambda: len(calls) >= 3, "three runs of the cleanup")
    finally:
        stop.set()

    messages = " ".join(record.getMessage() for record in caplog.records if record.name == "app.worker")
    assert "RuntimeError" in messages
    assert SECRET not in messages
    assert all(moment.tzinfo is not None for moment in calls)  # the loop passes the current time (UTC)


# a job that exceeds the timeout of the queue -------------------------------------------------------------------------


async def test_a_job_that_exceeds_the_queue_timeout_ends_failed_with_the_message_of_the_time_limit(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def timed_out(self: OllamaEmbedder, texts: object) -> list[list[float]]:
        calls.append(1)
        raise JobTimeoutException(f"Task exceeded maximum timeout value (3600 seconds) {SECRET}")

    monkeypatch.setattr(OllamaEmbedder, "embed", timed_out)
    document_id = await add_document(engine, tmp_path, "QUEUED")

    await process_document(document_id, settings_for(tmp_path, "http://127.0.0.1:9"), sleep=no_sleep)

    assert calls == [1]  # a timeout is not repeated
    assert await row_of(engine, document_id) == (
        "FAILED",
        "unexpected_error",
        "The processing time limit was exceeded",
        0,
    )
    assert await chunks_of(engine, document_id) == 0
    assert (tmp_path / str(document_id)).is_file()  # kept: the admin can retry


# stop_grace_period of the worker (the decision to let a warm shutdown finish a short job) -----------------------


def test_the_worker_service_waits_120_seconds_before_it_is_killed() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    block = re.search(r"^  worker:\n(.*?)^  \S", compose, re.MULTILINE | re.DOTALL)
    assert block is not None
    assert re.search(r"^    stop_grace_period: 120s\s*$", block.group(1), re.MULTILINE)
    assert "FAILED_ORIGINAL_RETENTION_DAYS: ${FAILED_ORIGINAL_RETENTION_DAYS:-7}" in block.group(1)
    assert "FAILED_ORIGINAL_RETENTION_DAYS" not in compose.split("\n  worker:\n")[0]  # not in the backend's environment


# the migrations at the start of the backend --------------------------------------------------------------------------


def test_running_the_migrations_does_not_disable_a_logger_created_before_them() -> None:
    sentinel = logging.getLogger("app.t06.logger_created_before_the_migrations")
    assert not sentinel.disabled

    migrate(make_settings().sqlalchemy_url)

    assert not sentinel.disabled
