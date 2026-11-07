# SPEC-2026-11-07-fase-2-ingestion
"""The originals do not outlive the rules: AC-034 and AC-035 (REQ-017).

`cleanup_originals(settings, now)` deletes (a) the original of a FAILED document once more than
`FAILED_ORIGINAL_RETENTION_DAYS` (default 7, at most 3650) days have passed since it failed (`documents.updated_at`,
set by the FAILED transition), checked again just before each deletion; (b) the files named exactly `{uuid}` or
`{uuid}.part` (canonical lower case) in `UPLOAD_DIR` that have no document and were last written more than one hour ago
(any other name is left alone); and (c) a surviving original of a READY or UNSUPPORTED document, whatever its age
(REQ-017: those originals must not exist). It deletes files only: no document, no chunk and no status changes, and the
original of a QUEUED or PROCESSING document is never deleted, however old. Time is injected through `now` and the file
times are set with `os.utime`: no test waits for real.

Real PostgreSQL of compose.test.yaml, the fake Ollama on a local socket (never the real service), fresh state per test,
an upload folder inside pytest's tmp_path.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings
from app.documents.repository import DocumentRepository
from app.documents.storage import UploadStorage
from app.ingestion.cleanup import CleanupReport, cleanup_originals
from app.ingestion.pipeline import process_document
from app.run import migrate
from tests.conftest import make_settings
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import fake_ollama

DIMENSIONS = 8
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
FAILED_MESSAGE = "The document parsing service could not be reached"
TEXT = "Prima riga del racconto. Seconda parte del racconto, con accenti: è à ò ù. Terza e ultima parte.".encode()


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


def age_file(path: Path, age: timedelta) -> None:
    """Make the file `age` old at NOW."""
    moment = (NOW - age).timestamp()
    os.utime(path, (moment, moment))


def put_file(upload_dir: Path, name: str, age: timedelta, content: bytes = b"original") -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / name
    path.write_bytes(content)
    age_file(path, age)
    return path


async def add_kb(engine: AsyncEngine) -> uuid.UUID:
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": uuid.uuid4().hex}
        )
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


async def add_document(
    engine: AsyncEngine,
    upload_dir: Path,
    status: str,
    *,
    changed: timedelta = timedelta(0),
    created: timedelta | None = None,
    original: bool = True,
    content: bytes = TEXT,
) -> uuid.UUID:
    """A TXT document in the given status whose last change (`updated_at`) was `changed` before NOW, with an original
    of the same age in the upload folder (none when `original` is False)."""
    document_id = uuid.uuid4()
    kb_id = await add_kb(engine)
    has_reason = status in ("FAILED", "UNSUPPORTED")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count, created_at, updated_at)"
                " values (:id, :kb_id, 'nota.txt', 'txt', :size, :checksum, :status, :code, :message, :chunks,"
                " :created_at, :updated_at)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "size": len(content),
                "checksum": document_id.hex,
                "status": status,
                "code": "docling_unreachable" if has_reason else None,
                "message": FAILED_MESSAGE if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
                "created_at": NOW - (created if created is not None else changed),
                "updated_at": NOW - changed,
            },
        )
    if original:
        age = created if created is not None else changed
        put_file(upload_dir, str(document_id), age, content)
    return document_id


async def snapshot(engine: AsyncEngine) -> dict[str, Any]:
    """Everything the cleanup must leave alone: every document row and every chunk."""
    async with engine.connect() as connection:
        documents = await connection.execute(
            text(
                "select id, kb_id, status, status_reason_code, status_reason_message, chunk_count, created_at,"
                " updated_at from documents order by id"
            )
        )
        chunks = await connection.execute(text("select id, document_id, position, content from chunks order by id"))
        return {"documents": [tuple(row) for row in documents], "chunks": [tuple(row) for row in chunks]}


async def status_of(engine: AsyncEngine, document_id: uuid.UUID) -> tuple[str, str | None, str | None]:
    async with engine.connect() as connection:
        row = await connection.execute(
            text("select status, status_reason_code, status_reason_message from documents where id = :id"),
            {"id": document_id},
        )
        return tuple(row.one())  # type: ignore[return-value]


def names_in(folder: Path) -> set[str]:
    return {path.name for path in folder.iterdir()}


# AC-034: a READY document has no original ----------------------------------------------------------------------------


async def test_AC_034_a_ready_document_has_no_original_after_the_processing(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The behaviour of T-04, cited here as the first clause of AC-034."""
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        document_id = await add_document(engine, tmp_path, "QUEUED")
        assert (tmp_path / str(document_id)).is_file()

        await process_document(document_id, settings_for(tmp_path, ollama.url))

    assert (await status_of(engine, document_id))[0] == "READY"
    assert not (tmp_path / str(document_id)).exists()


# AC-034: the retention of the FAILED originals ------------------------------------------------------------------------


async def test_AC_034_with_retention_7_the_original_of_8_days_is_deleted_and_the_one_of_6_days_is_kept(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    old = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=8))
    recent = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=6))

    report = await cleanup_originals(settings_for(tmp_path, failed_original_retention_days=7), NOW)

    assert not (tmp_path / str(old)).exists()
    assert (tmp_path / str(recent)).is_file()
    assert await status_of(engine, old) == ("FAILED", "docling_unreachable", FAILED_MESSAGE)
    assert await status_of(engine, recent) == ("FAILED", "docling_unreachable", FAILED_MESSAGE)
    assert report == CleanupReport(expired_originals=1, orphan_files=0)


async def test_AC_034_the_retention_is_the_setting_and_its_default_is_7_days(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    assert settings_for(tmp_path).failed_original_retention_days == 7
    three_days = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=3))
    one_day = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=1))

    await cleanup_originals(settings_for(tmp_path, failed_original_retention_days=2), NOW)

    assert not (tmp_path / str(three_days)).exists()
    assert (tmp_path / str(one_day)).is_file()
    assert (await status_of(engine, three_days))[0] == "FAILED"


@pytest.mark.parametrize("days", [0, -1])
def test_AC_034_a_retention_below_one_day_is_refused(tmp_path: Path, days: int) -> None:
    with pytest.raises(ValidationError):
        settings_for(tmp_path, failed_original_retention_days=days)


async def test_AC_034_the_age_counts_from_the_moment_the_document_failed_not_from_its_upload(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Uploaded 30 days ago, retried, and failed again yesterday: the admin still has 6 days to retry it."""
    document_id = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=1), created=timedelta(days=30))

    await cleanup_originals(settings_for(tmp_path), NOW)

    assert (tmp_path / str(document_id)).is_file()
    assert (await status_of(engine, document_id))[0] == "FAILED"


async def test_AC_034_a_failed_document_whose_original_is_already_gone_stays_failed_without_error(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=9), original=False)
    before = await snapshot(engine)

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert report == CleanupReport(expired_originals=0, orphan_files=0)
    assert await snapshot(engine) == before
    assert (await status_of(engine, document_id))[0] == "FAILED"


async def test_AC_034_the_original_of_a_queued_or_processing_document_is_never_deleted_however_old(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    queued = await add_document(engine, tmp_path, "QUEUED", changed=timedelta(days=60))
    processing = await add_document(engine, tmp_path, "PROCESSING", changed=timedelta(days=60))
    before = await snapshot(engine)

    report = await cleanup_originals(settings_for(tmp_path, failed_original_retention_days=1), NOW)

    assert (tmp_path / str(queued)).is_file()
    assert (tmp_path / str(processing)).is_file()
    assert report == CleanupReport(expired_originals=0, orphan_files=0)
    assert await snapshot(engine) == before


# AC-035: files without a document -------------------------------------------------------------------------------------


async def test_AC_035_an_orphan_of_two_hours_is_deleted_and_one_of_ten_minutes_is_kept(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    old = put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=2))
    recent = put_file(tmp_path, str(uuid.uuid4()), timedelta(minutes=10))

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert not old.exists()
    assert recent.is_file()
    assert report == CleanupReport(expired_originals=0, orphan_files=1)


async def test_AC_035_a_stray_part_file_follows_the_same_hour_rule(engine: AsyncEngine, tmp_path: Path) -> None:
    old = put_file(tmp_path, f"{uuid.uuid4()}.part", timedelta(hours=2))
    recent = put_file(tmp_path, f"{uuid.uuid4()}.part", timedelta(minutes=10))

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert not old.exists()
    assert recent.is_file()
    assert report.orphan_files == 1


async def test_AC_035_a_file_just_under_one_hour_is_kept_and_one_just_over_is_deleted(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    under = put_file(tmp_path, str(uuid.uuid4()), HOUR - timedelta(seconds=30))
    over = put_file(tmp_path, str(uuid.uuid4()), HOUR + timedelta(seconds=30))

    await cleanup_originals(settings_for(tmp_path), NOW)

    assert under.is_file()
    assert not over.exists()


async def test_AC_035_a_file_whose_name_is_not_exactly_a_document_id_or_an_id_with_part_is_left_alone(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The sweep deletes only what the upload creates: `{uuid}` and `{uuid}.part`, in canonical lower case."""
    identifier = str(uuid.uuid4())
    names = [
        "left-by-hand.bin",
        ".hidden",
        identifier.upper(),
        f"{identifier}.txt",
        f"{identifier}.PART",
        f"{identifier}.part.bak",
        identifier.replace("-", ""),
        f"{{{identifier}}}",
    ]
    strays = [put_file(tmp_path, name, timedelta(hours=3)) for name in names]
    folder = tmp_path / "subfolder"
    folder.mkdir()

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert all(path.is_file() for path in strays), [path.name for path in strays if not path.exists()]
    assert folder.is_dir()
    assert report == CleanupReport(expired_originals=0, orphan_files=0)


async def test_AC_035_a_canonical_id_and_a_part_file_are_still_swept_next_to_files_that_are_left_alone(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    kept = put_file(tmp_path, "left-by-hand.bin", timedelta(hours=3))
    orphan = put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=3))
    part = put_file(tmp_path, f"{uuid.uuid4()}.part", timedelta(hours=3))

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert kept.is_file()
    assert not orphan.exists()
    assert not part.exists()
    assert report.orphan_files == 2


# REQ-017: a READY or UNSUPPORTED document has no original ---------------------------------------------------------


async def test_AC_034_a_surviving_original_of_a_ready_or_unsupported_document_is_deleted_whatever_its_age(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The crash window between the status mark and the removal of the original."""
    ready = await add_document(engine, tmp_path, "READY", changed=timedelta(minutes=5))
    unsupported = await add_document(engine, tmp_path, "UNSUPPORTED", changed=timedelta(days=2))
    queued = await add_document(engine, tmp_path, "QUEUED", changed=timedelta(minutes=5))
    processing = await add_document(engine, tmp_path, "PROCESSING", changed=timedelta(minutes=5))
    failed = await add_document(engine, tmp_path, "FAILED", changed=timedelta(minutes=5))
    before = await snapshot(engine)

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert not (tmp_path / str(ready)).exists()
    assert not (tmp_path / str(unsupported)).exists()
    for document_id in (queued, processing, failed):
        assert (tmp_path / str(document_id)).is_file()
    assert report == CleanupReport(expired_originals=2, orphan_files=0)
    assert await snapshot(engine) == before


# A retry at the boundary does not lose its original -------------------------------------------------------------------


@pytest.mark.parametrize("change", ["requeued", "failed_again"])
async def test_AC_034_a_document_that_changed_after_the_listing_keeps_its_original(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """The admin presses Retry (or the document fails again) between the moment the expired documents are listed and
    the moment their originals are deleted: the original is checked again and stays."""
    racing = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=8))
    expired = await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=8))
    real_ids_in_status = DocumentRepository.ids_in_status

    async def listed_then_changed(self: DocumentRepository, status: str, **kwargs: Any) -> list[uuid.UUID]:
        ids = await real_ids_in_status(self, status, **kwargs)
        if status == "FAILED" and kwargs.get("changed_before") is not None:
            assert racing in ids and expired in ids
            statement = (
                "update documents set status = 'QUEUED', status_reason_code = null, status_reason_message = null,"
                " updated_at = :moment where id = :id"
                if change == "requeued"
                else "update documents set updated_at = :moment where id = :id"
            )
            async with engine.begin() as connection:
                await connection.execute(text(statement), {"id": racing, "moment": NOW - timedelta(days=1)})
        return ids

    monkeypatch.setattr(DocumentRepository, "ids_in_status", listed_then_changed)

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert (tmp_path / str(racing)).is_file()
    assert not (tmp_path / str(expired)).exists()
    assert report == CleanupReport(expired_originals=1, orphan_files=0)
    assert (await status_of(engine, racing))[0] == ("QUEUED" if change == "requeued" else "FAILED")


# UploadStorage.orphans: a file that vanishes while the folder is listed ---------------------------------------------


def test_AC_035_a_file_removed_while_the_folder_is_listed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=2))
    vanishing = put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=2))
    real_is_file = Path.is_file

    def is_file_then_gone(self: Path) -> bool:
        found = real_is_file(self)
        if self == vanishing:
            self.unlink()  # renamed (the upload) or removed (a delete) right after the check
        return found

    monkeypatch.setattr(Path, "is_file", is_file_then_gone)

    found = UploadStorage(tmp_path).orphans(HOUR, NOW)

    assert found == [old]


# Many files and a bounded retention ---------------------------------------------------------------------------------


def put_many(folder: Path, count: int, age: timedelta) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    moment = (NOW - age).timestamp()
    paths = []
    for _ in range(count):
        path = folder / str(uuid.uuid4())
        path.write_bytes(b"")
        os.utime(path, (moment, moment))
        paths.append(path)
    return paths


async def test_AC_035_tens_of_thousands_of_files_do_not_hit_the_limit_of_query_arguments(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """asyncpg refuses more than 32767 arguments in one statement: the ids are looked up in batches."""
    orphans = put_many(tmp_path, 33_000, timedelta(hours=2))
    document_id = await add_document(engine, tmp_path, "QUEUED", changed=timedelta(hours=5))

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert report == CleanupReport(expired_originals=0, orphan_files=33_000)
    assert not any(path.exists() for path in orphans)
    assert (tmp_path / str(document_id)).is_file()


async def test_AC_035_the_batched_lookup_finds_the_documents_among_many_ids(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    document_id = await add_document(engine, tmp_path, "QUEUED")
    ids = [uuid.uuid4() for _ in range(40_000)]
    ids[25_000] = document_id
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        found = await DocumentRepository(session).statuses(ids)

    assert found == {document_id: "QUEUED"}


def test_AC_034_the_retention_is_bounded_so_that_it_cannot_overflow_a_date(tmp_path: Path) -> None:
    assert settings_for(tmp_path, failed_original_retention_days=3650).failed_original_retention_days == 3650
    for days in (3651, 10**12):
        with pytest.raises(ValidationError):
            settings_for(tmp_path, failed_original_retention_days=days)


async def test_AC_035_an_old_file_that_has_a_document_is_not_an_orphan(engine: AsyncEngine, tmp_path: Path) -> None:
    """The documents that wait, are being processed or failed recently keep their originals however old the files."""
    queued = await add_document(engine, tmp_path, "QUEUED", changed=timedelta(hours=5))
    processing = await add_document(engine, tmp_path, "PROCESSING", changed=timedelta(hours=5))
    failed = await add_document(engine, tmp_path, "FAILED", changed=timedelta(hours=5))

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    for document_id in (queued, processing, failed):
        assert (tmp_path / str(document_id)).is_file()
    assert report == CleanupReport(expired_originals=0, orphan_files=0)


async def test_AC_035_the_cleanup_touches_no_document_and_no_chunk(engine: AsyncEngine, tmp_path: Path) -> None:
    with fake_ollama(dimensions=DIMENSIONS) as ollama:
        ready = await add_document(engine, tmp_path, "QUEUED")
        await process_document(ready, settings_for(tmp_path, ollama.url))
    assert (await status_of(engine, ready))[0] == "READY"
    await add_document(engine, tmp_path, "QUEUED", changed=timedelta(days=40))
    await add_document(engine, tmp_path, "PROCESSING", changed=timedelta(days=40))
    await add_document(engine, tmp_path, "FAILED", changed=timedelta(days=40))
    await add_document(engine, tmp_path, "UNSUPPORTED", changed=timedelta(days=40), original=False)
    put_file(tmp_path, str(uuid.uuid4()), timedelta(days=3))
    before = await snapshot(engine)
    assert before["chunks"] and len(before["documents"]) == 5

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert report == CleanupReport(expired_originals=1, orphan_files=1)
    assert await snapshot(engine) == before


async def test_AC_035_a_file_that_cannot_be_deleted_does_not_stop_the_sweep(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=2))
    others = [put_file(tmp_path, str(uuid.uuid4()), timedelta(hours=2)) for _ in range(3)]
    original_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == stuck:
            raise PermissionError("not allowed")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    report = await cleanup_originals(settings_for(tmp_path), NOW)

    assert stuck.is_file()
    assert not any(path.exists() for path in others)
    assert report.orphan_files == 3


async def test_AC_035_an_upload_folder_that_does_not_exist_yet_is_not_an_error(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    report = await cleanup_originals(settings_for(tmp_path / "never-created"), NOW)

    assert report == CleanupReport(expired_originals=0, orphan_files=0)
