# SPEC-2026-11-07-fase-2-ingestion
"""Upload and listing of the documents by the admin: AC-003 to AC-011, AC-013, AC-014 (REQ-002 to REQ-006).

AC-012 (every document route is admin only) belongs to T-06, which owns all five routes; the tests here cite it for
the three routes of this task. Review focus of T-01: corrupt, truncated or empty ZIP archives with a .docx, .pptx or
.xlsx name get 415, never a 500, and the archive is never extracted; a ZIP with the right marker but the wrong
extension; Unicode (NFC and NFD), very long and dot-only file names; the same content under different names; identical
uploads arriving together.

Real PostgreSQL and Redis of compose.test.yaml, fresh state per test, an upload folder inside pytest's tmp_path. A
file is "created" if there is a row in `documents`, a file in the upload folder or a job in the RQ queue `ingestion`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from rq import Queue
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User
from app.documents.formats import UnsupportedType, inspect_upload, sanitize_filename
from app.documents.repository import DocumentRepository
from app.documents.storage import FileTooLarge, UploadStorage
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings

ADMIN = "anna"
ADMIN_PASSWORD = "admin-password-123"
PASSWORD = "correct-horse-battery"
MIB = 1024 * 1024
AJAX = {"X-Requested-With": "XMLHttpRequest"}
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
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
TEXT = "Prima riga\nSeconda riga con accenti: è à ò ù\n".encode()
FILE_TOO_LARGE = {"error": {"code": "file_too_large", "message": "The file is too large"}}


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


DOCX = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
PPTX = make_zip({"[Content_Types].xml": b"<Types/>", "ppt/presentation.xml": b"<p:presentation/>"})
XLSX = make_zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>"})
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18


def multipart(filename: str, content: bytes, content_type: str | None, field: str = "file") -> tuple[bytes, str]:
    """A multipart body written by hand, so that the file name travels exactly as given (control characters too)."""
    boundary = "----documentsboundary7MA4YWxkTrZu0gW"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="'.encode()
    head += filename.encode("utf-8") + b'"\r\n'
    if content_type is not None:
        head += f"Content-Type: {content_type}\r\n".encode()
    body = head + b"\r\n" + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


async def post_file(
    api: httpx.AsyncClient,
    kb_id: uuid.UUID | str,
    filename: str,
    content: bytes,
    content_type: str | None = "text/plain",
    headers: dict[str, str] | None = AJAX,
) -> httpx.Response:
    body, boundary_type = multipart(filename, content, content_type)
    return await api.post(
        f"/api/kbs/{kb_id}/documents", content=body, headers={**(headers or {}), "Content-Type": boundary_type}
    )


# State ---------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no users, KBs or documents in the database, nothing in Redis (sessions and queue)."""
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table documents, kb_members, knowledge_bases, users cascade"))
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield engine
    await engine.dispose()


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"  # not created: the first upload creates it


async def add_user(engine: AsyncEngine, username: str, password: str, role: str) -> uuid.UUID:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        user = User(username=username, password_hash=hash_password(password), role=role)
        session.add(user)
        await session.commit()
        return user.id


async def add_kb(engine: AsyncEngine, name: str) -> uuid.UUID:
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name) values (:name) returning id"), {"name": name}
        )
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


async def assign(engine: AsyncEngine, kb_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("insert into kb_members (kb_id, user_id) values (:kb_id, :user_id)"),
            {"kb_id": kb_id, "user_id": user_id},
        )


async def add_document(
    engine: AsyncEngine,
    kb_id: uuid.UUID,
    content: bytes,
    status: str = "QUEUED",
    filename: str = "existing.txt",
    created_at: datetime | None = None,
) -> uuid.UUID:
    """A document inserted directly in the given status, with the checksum of `content`."""
    has_reason = status in ("FAILED", "UNSUPPORTED")
    document_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "insert into documents (id, kb_id, filename, format, size_bytes, checksum, status,"
                " status_reason_code, status_reason_message, chunk_count, created_at)"
                " values (:id, :kb_id, :filename, 'txt', :size, :checksum, :status, :code, :message, :chunks,"
                " coalesce(:created_at, now()))"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "filename": filename,
                "size": len(content),
                "checksum": hashlib.sha256(content).hexdigest(),
                "status": status,
                "code": "docling_error" if has_reason else None,
                "message": "Docling could not read the file" if has_reason else None,
                "chunks": 3 if status == "READY" else 0,
                "created_at": created_at,
            },
        )
    return document_id


async def document_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(text("select * from documents order by created_at, id"))
        return [dict(row._mapping) for row in result]


def stored_files(folder: Path) -> list[str]:
    """Names of every file under the folder (an empty list if it does not exist)."""
    return sorted(path.name for path in folder.rglob("*") if path.is_file()) if folder.exists() else []


def stored_content(folder: Path, name: str) -> bytes:
    return (folder / name).read_bytes()


def every_file(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def queued_jobs() -> list[Any]:
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        return list(Queue("ingestion", connection=connection).jobs)
    finally:
        connection.close()


async def assert_nothing_created(engine: AsyncEngine, folder: Path) -> None:
    assert await document_rows(engine) == []
    assert stored_files(folder) == []
    assert queued_jobs() == []


def make_client(settings: Settings) -> httpx.AsyncClient:
    app = create_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@asynccontextmanager
async def signed_in(
    engine: AsyncEngine, upload_dir: Path, username: str, role: str, **overrides: Any
) -> AsyncIterator[httpx.AsyncClient]:
    await add_user(engine, username, PASSWORD, role)
    async with make_client(make_settings(upload_dir=upload_dir, **overrides)) as api:
        response = await api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200
        yield api


@pytest.fixture
async def admin(engine: AsyncEngine, upload_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with signed_in(engine, upload_dir, ADMIN, "admin") as api:
        yield api


@pytest.fixture
async def small_admin(engine: AsyncEngine, upload_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The admin of an app with MAX_UPLOAD_SIZE_MB=1."""
    async with signed_in(engine, upload_dir, ADMIN, "admin", max_upload_size_mb=1) as api:
        yield api


@pytest.fixture
async def kb(engine: AsyncEngine) -> uuid.UUID:
    return await add_kb(engine, "Manuali")


# AC-003: a valid file is stored, queued and answered with 202 --------------------------------------------------------


async def test_AC_003_a_valid_txt_of_1_kb_gets_202_queued_and_is_stored_under_its_id(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    content = (b"Una riga di prova.\n" * 60)[:1024]
    assert len(content) == 1024

    response = await post_file(admin, kb, "note.txt", content, "text/plain")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == DOCUMENT_OUT_KEYS
    assert body["status"] == "QUEUED"
    assert body["status_reason"] is None
    assert body["chunk_count"] == 0
    assert body["filename"] == "note.txt"
    assert body["size_bytes"] == 1024
    assert body["kb_id"] == str(kb)
    assert stored_files(upload_dir) == [body["id"]]
    assert stored_content(upload_dir, body["id"]) == content
    rows = await document_rows(engine)
    assert [(str(row["id"]), row["format"], row["status"]) for row in rows] == [(body["id"], "txt", "QUEUED")]
    assert rows[0]["checksum"] == hashlib.sha256(content).hexdigest()


async def test_AC_003_the_upload_puts_one_job_for_the_document_on_the_ingestion_queue(
    admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    response = await post_file(admin, kb, "note.txt", TEXT)

    assert response.status_code == 202
    (job,) = queued_jobs()
    assert job.func_name == "app.ingestion.jobs.process_document_job"
    assert tuple(job.args) == (response.json()["id"],)
    assert job.timeout == 3600
    assert job.result_ttl == 0


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "fmt"),
    [
        ("report.pdf", PDF, "application/pdf", "pdf"),
        ("REPORT.PDF", PDF, "application/pdf", "pdf"),
        ("no-type.pdf", PDF, None, "pdf"),
        ("octet.pdf", PDF, "application/octet-stream", "pdf"),
        ("letter.docx", DOCX, DOCX_MIME, "docx"),
        ("Slides.PPTX", PPTX, PPTX_MIME, "pptx"),
        ("table.xlsx", XLSX, XLSX_MIME, "xlsx"),
        ("plain.txt", TEXT, "text/plain", "txt"),
        ("charset.txt", TEXT, "text/plain; charset=utf-8", "txt"),
        ("bom.txt", b"\xef\xbb\xbf" + TEXT, "text/plain", "txt"),
        ("guide.md", b"# Guida\n\nTesto\n", "text/markdown", "md"),
        ("guide-x.md", b"# Guida\n", "text/x-markdown", "md"),
        ("guide-plain.md", b"# Guida\n", "text/plain", "md"),
        ("guide-unknown.md", b"# Guida\n", "application/octet-stream", "md"),
    ],
)
async def test_AC_003_each_of_the_six_formats_is_accepted_with_its_declared_type(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str | None,
    fmt: str,
) -> None:
    response = await post_file(admin, kb, filename, content, content_type)

    assert response.status_code == 202, response.text
    assert [row["format"] for row in await document_rows(engine)] == [fmt]


# AC-004: the displayed name is cleaned and never used as a path -----------------------------------------------------


async def test_AC_004_a_path_in_the_name_is_removed_and_only_the_id_file_is_written(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, tmp_path: Path
) -> None:
    response = await post_file(admin, kb, "../../segreto/passwd.txt", TEXT)

    assert response.status_code == 202
    body = response.json()
    assert body["filename"] == "passwd.txt"
    assert stored_files(upload_dir) == [body["id"]]
    assert every_file(tmp_path) == [upload_dir / body["id"]]
    assert [row["filename"] for row in await document_rows(engine)] == ["passwd.txt"]


@pytest.mark.parametrize("raw", ["..\\..\\segreto\\passwd.txt", "C:\\Users\\anna\\passwd.txt", "a/b\\c/../passwd.txt"])
async def test_AC_004_a_windows_or_mixed_path_in_the_name_is_removed(
    admin: httpx.AsyncClient, kb: uuid.UUID, raw: str
) -> None:
    response = await post_file(admin, kb, raw, TEXT)

    assert response.status_code == 202
    assert response.json()["filename"] == "passwd.txt"


async def test_AC_004_control_characters_in_the_name_are_removed(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    response = await post_file(admin, kb, "re\x07po\x1frt\x7f\u202e\u200b.txt", TEXT)

    assert response.status_code == 202
    body = response.json()
    assert body["filename"] == "report.txt"
    assert stored_files(upload_dir) == [body["id"]]


async def test_AC_004_a_decomposed_unicode_name_is_stored_in_nfc(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    decomposed = "Cafe\u0301 menu\u0300.txt"
    assert unicodedata.normalize("NFC", decomposed) != decomposed

    response = await post_file(admin, kb, decomposed, TEXT)

    assert response.status_code == 202
    assert response.json()["filename"] == "Caf\u00e9 men\u00f9.txt"
    assert [row["filename"] for row in await document_rows(engine)] == ["Caf\u00e9 men\u00f9.txt"]


async def test_AC_004_a_name_of_300_characters_is_cut_to_255_and_keeps_its_extension(
    admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    response = await post_file(admin, kb, "\u00e8" * 300 + ".txt", TEXT)

    assert response.status_code == 202
    filename = response.json()["filename"]
    assert len(filename) == 255
    assert filename.endswith(".txt")


@pytest.mark.parametrize("raw", ["...", "..", ".", "/", "../", "dir/", "\x07\x08", " "])
async def test_AC_004_a_name_without_a_usable_name_or_extension_is_refused_without_side_effects(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, raw: str
) -> None:
    response = await post_file(admin, kb, raw, TEXT)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_type"
    await assert_nothing_created(engine, upload_dir)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("passwd.txt", "passwd.txt"),
        ("../../segreto/passwd.txt", "passwd.txt"),
        ("..\\..\\segreto\\passwd.txt", "passwd.txt"),
        ("C:\\Users\\anna\\scan.pdf", "scan.pdf"),
        ("a/b\\c/d.md", "d.md"),
        ("re\x00po\x07rt.txt", "report.txt"),
        ("bidi\u202ename\u200b\ufeff.txt", "bidiname.txt"),
        ("line\nbreak\r.txt", "linebreak.txt"),
        ("  spaced name.txt \t", "spaced name.txt"),
        ("Cafe\u0301.txt", "Caf\u00e9.txt"),
        ("Caf\u00e9.txt", "Caf\u00e9.txt"),
        ("rel\u00e1torio \u65e5\u672c\u8a9e.pdf", "rel\u00e1torio \u65e5\u672c\u8a9e.pdf"),
        ("", "document"),
        ("   ", "document"),
        ("dir/", "document"),
        ("\x00\x01", "document"),
        ("...", "document"),
        ("..", "document"),
        (".", "document"),
        ("/", "document"),
    ],
)
def test_AC_004_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_AC_004_sanitize_filename_cuts_to_255_characters_keeping_the_extension() -> None:
    long_name = sanitize_filename("a" * 400 + ".docx")
    assert len(long_name) == 255
    assert long_name.endswith(".docx")
    assert sanitize_filename("b" * 400) == "b" * 255
    assert len(sanitize_filename("\u00e8" * 300 + ".md")) == 255
    assert sanitize_filename("c" * 255) == "c" * 255


# AC-005: CSRF guard, multipart only for the upload route with X-Requested-With --------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Requested-With": "fetch"}, {"X-Requested-With": ""}])
async def test_AC_005_an_upload_without_the_ajax_header_gets_422_and_creates_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, headers: dict[str, str]
) -> None:
    response = await post_file(admin, kb, "note.txt", TEXT, headers=headers)

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "validation_error", "message": "The request is not valid"}}
    await assert_nothing_created(engine, upload_dir)


@pytest.mark.parametrize("path", ["/api/kbs", "/api/auth/login", "/api/auth/logout"])
async def test_AC_005_multipart_is_still_refused_on_every_other_route_even_with_the_header(
    admin: httpx.AsyncClient, path: str
) -> None:
    body, content_type = multipart("note.txt", TEXT, "text/plain")

    response = await admin.post(path, content=body, headers={**AJAX, "Content-Type": content_type})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_AC_005_a_form_urlencoded_or_text_upload_is_refused_even_with_the_header(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    for content_type in ("application/x-www-form-urlencoded", "text/plain"):
        response = await admin.post(
            f"/api/kbs/{kb}/documents", content=b"file=abc", headers={**AJAX, "Content-Type": content_type}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    await assert_nothing_created(engine, upload_dir)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: multipart("note.txt", TEXT, "text/plain", field="other"), id="wrong-field"),
        pytest.param(
            lambda: (
                b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\ntext\r\n--b--\r\n',
                "multipart/form-data; boundary=b",
            ),
            id="field-is-not-a-file",
        ),
        pytest.param(lambda: (b"not a multipart body", "multipart/form-data; boundary=zzz"), id="malformed"),
        pytest.param(lambda: (b"", "multipart/form-data; boundary=zzz"), id="empty-body"),
    ],
)
async def test_AC_005_a_body_without_exactly_one_file_in_the_field_file_gets_422(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, build: Any
) -> None:
    body, content_type = build()

    response = await admin.post(
        f"/api/kbs/{kb}/documents", content=body, headers={**AJAX, "Content-Type": content_type}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    await assert_nothing_created(engine, upload_dir)


async def test_AC_005_two_files_in_one_request_get_422_and_create_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    first, content_type = multipart("one.txt", TEXT, "text/plain")
    boundary = content_type.split("boundary=")[1]
    second = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="two.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\nsecond\r\n--{boundary}--\r\n"
    ).encode()
    body = first.removesuffix(f"--{boundary}--\r\n".encode()) + second

    response = await admin.post(
        f"/api/kbs/{kb}/documents", content=body, headers={**AJAX, "Content-Type": content_type}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    await assert_nothing_created(engine, upload_dir)


# AC-006: type, extension and signature must agree -------------------------------------------------------------------

BAD_FILES = [
    pytest.param("fake.pdf", b"this is not a pdf", "application/pdf", id="pdf-without-signature"),
    pytest.param("fake.docx", b"this is not a zip", DOCX_MIME, id="docx-not-a-zip"),
    pytest.param("pptx-inside.docx", PPTX, DOCX_MIME, id="docx-zip-without-word-document"),
    pytest.param("fake.pptx", b"this is not a zip", PPTX_MIME, id="pptx-not-a-zip"),
    pytest.param("docx-inside.pptx", DOCX, PPTX_MIME, id="pptx-zip-without-presentation"),
    pytest.param("fake.xlsx", b"this is not a zip", XLSX_MIME, id="xlsx-not-a-zip"),
    pytest.param("docx-inside.xlsx", DOCX, XLSX_MIME, id="xlsx-zip-without-workbook"),
    pytest.param("nul.txt", b"abc\x00def", "text/plain", id="txt-with-nul"),
    pytest.param("latin1.md", "caffè\n".encode("latin-1"), "text/markdown", id="md-not-utf8"),
    pytest.param("setup.exe", b"MZ\x90\x00\x03\x00\x00\x00", "application/x-msdownload", id="exe"),
    pytest.param("image.pdf", PDF, "image/png", id="pdf-declared-png"),
    # A ZIP with the right marker under the wrong extension or declared type.
    pytest.param("word.xlsx", DOCX, None, id="docx-marker-under-xlsx"),
    pytest.param("word.pptx", DOCX, None, id="docx-marker-under-pptx"),
    pytest.param("word.pdf", DOCX, None, id="docx-marker-under-pdf"),
    pytest.param("sheet.docx", XLSX, DOCX_MIME, id="xlsx-marker-under-docx"),
    pytest.param("word.txt", DOCX, "text/plain", id="docx-marker-under-txt"),
    pytest.param("word.docx", DOCX, XLSX_MIME, id="docx-declared-xlsx"),
    pytest.param("word.docx", DOCX, "application/pdf", id="docx-declared-pdf"),
    pytest.param("note.txt", TEXT, "application/pdf", id="txt-declared-pdf"),
    pytest.param("note.md", b"# Titolo\n", "application/zip", id="md-declared-zip"),
    pytest.param("doc.pdf", PDF, "text/plain", id="pdf-declared-text"),
    # Extensions that are not accepted.
    pytest.param("archive.zip", DOCX, "application/zip", id="zip-extension"),
    pytest.param("legacy.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/msword", id="legacy-doc"),
    pytest.param("page.html", b"<html></html>", "text/html", id="html"),
    pytest.param("no-extension", TEXT, "text/plain", id="no-extension"),
    pytest.param("report.pdf.exe", PDF, "application/pdf", id="double-extension"),
    pytest.param("trailing-dot.txt.", TEXT, "text/plain", id="trailing-dot"),
    # Corrupt, truncated or strange archives: 415, never a 500.
    pytest.param("cut.docx", DOCX[: len(DOCX) // 2], DOCX_MIME, id="docx-truncated-in-half"),
    pytest.param("cut.pptx", PPTX[:-10], PPTX_MIME, id="pptx-without-the-last-bytes"),
    pytest.param("cut.xlsx", XLSX[:-30] + b"\x00" * 30, XLSX_MIME, id="xlsx-damaged-end-of-directory"),
    pytest.param("header-only.docx", b"PK\x03\x04", DOCX_MIME, id="docx-only-the-local-header"),
    pytest.param("junk.docx", b"PK\x03\x04" + os.urandom(64), DOCX_MIME, id="docx-header-and-junk"),
    pytest.param("empty-archive.docx", EMPTY_ZIP, DOCX_MIME, id="docx-empty-archive"),
    pytest.param("empty-archive.pptx", EMPTY_ZIP, PPTX_MIME, id="pptx-empty-archive"),
    pytest.param("empty-archive.xlsx", EMPTY_ZIP, XLSX_MIME, id="xlsx-empty-archive"),
    pytest.param("dir-entry.docx", make_zip({"word/document.xml/": b""}), DOCX_MIME, id="marker-is-a-directory"),
    pytest.param("nested.docx", make_zip({"inner.docx": DOCX}), DOCX_MIME, id="docx-inside-a-docx"),
    pytest.param("upper.docx", make_zip({"WORD/DOCUMENT.XML": b"<w/>"}), DOCX_MIME, id="marker-in-another-case"),
]


@pytest.mark.parametrize(("filename", "content", "content_type"), BAD_FILES)
async def test_AC_006_a_file_that_is_not_what_it_says_gets_415_and_creates_nothing(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    upload_dir: Path,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> None:
    response = await post_file(admin, kb, filename, content, content_type)

    assert response.status_code == 415
    assert response.json() == {"error": {"code": "unsupported_type", "message": "The file type is not supported"}}
    await assert_nothing_created(engine, upload_dir)


@pytest.mark.parametrize(("filename", "content", "content_type"), BAD_FILES)
def test_AC_006_inspect_upload_refuses_a_file_that_is_not_what_it_says(
    tmp_path: Path, filename: str, content: bytes, content_type: str | None
) -> None:
    path = tmp_path / "upload"
    path.write_bytes(content)

    with pytest.raises(UnsupportedType):
        inspect_upload(path, sanitize_filename(filename), content_type)


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "fmt"),
    [
        ("a.pdf", PDF, "application/pdf", "pdf"),
        ("a.PDF", PDF, "APPLICATION/PDF", "pdf"),
        ("a.pdf", PDF, "", "pdf"),
        ("a.docx", DOCX, DOCX_MIME, "docx"),
        ("a.docx", DOCX, "application/octet-stream", "docx"),
        ("a.pptx", PPTX, PPTX_MIME, "pptx"),
        ("a.xlsx", XLSX, None, "xlsx"),
        ("a.txt", b"", "text/plain", "txt"),
        ("a.txt", "caffè".encode(), "text/plain; charset=utf-8", "txt"),
        ("a.md", b"# t", "text/markdown", "md"),
        ("a.md", b"# t", "text/x-markdown", "md"),
        ("a.md", b"# t", "text/plain", "md"),
        ("a.Md", b"\xef\xbb\xbf# t\r\n", None, "md"),
    ],
)
def test_AC_006_inspect_upload_returns_the_format_when_extension_type_and_content_agree(
    tmp_path: Path, filename: str, content: bytes, content_type: str | None, fmt: str
) -> None:
    path = tmp_path / "upload.part"
    path.write_bytes(content)

    assert inspect_upload(path, filename, content_type) == fmt


def test_AC_006_a_large_text_with_invalid_utf8_late_in_the_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"a" * (3 * MIB) + b"\xff\xfe" + b"b" * 10)

    with pytest.raises(UnsupportedType):
        inspect_upload(path, "big.txt", "text/plain")


def test_AC_006_a_multibyte_character_split_by_the_reading_blocks_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "upload"
    path.write_bytes(b"a" * (MIB - 1) + "è".encode() * 10)

    assert inspect_upload(path, "edge.txt", "text/plain") == "txt"


async def test_AC_006_an_archive_with_a_huge_entry_is_judged_from_its_directory_without_extracting_it(
    admin: httpx.AsyncClient, kb: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus: the code reads only the names of the ZIP directory; a 64 MB entry (a few KB compressed) must
    neither be read nor extracted, and the upload is answered normally."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("word/document.xml", "w") as entry:
            for _ in range(64):
                entry.write(b"\x00" * MIB)
    bomb = buffer.getvalue()
    assert len(bomb) < MIB

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the content of the archive was read")

    for method in ("open", "read", "extract", "extractall", "testzip"):
        monkeypatch.setattr(zipfile.ZipFile, method, forbidden)

    response = await post_file(admin, kb, "bomb.docx", bomb, DOCX_MIME)

    assert response.status_code == 202


# AC-007: an empty file ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["empty.txt", "empty.pdf", "empty.docx", "empty.md"])
async def test_AC_007_an_empty_file_gets_422_empty_file_and_creates_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, filename: str
) -> None:
    response = await post_file(admin, kb, filename, b"", "application/octet-stream")

    assert response.status_code == 422
    assert response.json() == {"error": {"code": "empty_file", "message": "The file is empty"}}
    await assert_nothing_created(engine, upload_dir)


# AC-008: the size limit ---------------------------------------------------------------------------------------------


async def test_AC_008_a_file_of_exactly_1_mb_is_accepted_and_one_byte_more_gets_413(
    engine: AsyncEngine, small_admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    accepted = await post_file(small_admin, kb, "limit.txt", b"a" * MIB)
    assert accepted.status_code == 202
    assert accepted.json()["size_bytes"] == MIB

    refused = await post_file(small_admin, kb, "over.txt", b"b" * (MIB + 1))

    assert refused.status_code == 413
    assert refused.json() == FILE_TOO_LARGE
    rows = await document_rows(engine)
    assert [str(row["id"]) for row in rows] == [accepted.json()["id"]]
    assert stored_files(upload_dir) == [accepted.json()["id"]]
    assert len(queued_jobs()) == 1


async def test_AC_008_a_file_over_the_limit_gets_413_even_when_its_type_is_wrong(
    engine: AsyncEngine, small_admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    response = await post_file(small_admin, kb, "big.exe", b"MZ" + b"\x00" * MIB, "application/x-msdownload")

    assert response.status_code == 413
    assert response.json() == FILE_TOO_LARGE
    await assert_nothing_created(engine, upload_dir)


async def test_AC_008_a_body_more_than_1_mb_over_the_limit_is_refused_before_it_is_parsed(
    engine: AsyncEngine, small_admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    """The size comes from Content-Length: a garbage body that could never be parsed still gets 413, not 422."""
    response = await small_admin.post(
        f"/api/kbs/{kb}/documents",
        content=b"x" * (2 * MIB + 1024),
        headers={**AJAX, "Content-Type": "multipart/form-data; boundary=zzz"},
    )

    assert response.status_code == 413
    assert response.json() == FILE_TOO_LARGE
    await assert_nothing_created(engine, upload_dir)


async def test_AC_008_the_default_limit_is_20_mb(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    refused = await post_file(admin, kb, "over.txt", b"a" * (20 * MIB + 1))
    assert refused.status_code == 413
    assert refused.json() == FILE_TOO_LARGE
    await assert_nothing_created(engine, upload_dir)

    accepted = await post_file(admin, kb, "limit.txt", b"a" * (20 * MIB))
    assert accepted.status_code == 202
    assert accepted.json()["size_bytes"] == 20 * MIB


def test_AC_008_the_settings_default_to_20_mb_and_refuse_a_limit_below_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_UPLOAD_SIZE_MB", raising=False)
    monkeypatch.delenv("UPLOAD_DIR", raising=False)
    assert make_settings().max_upload_size_mb == 20
    assert make_settings().upload_dir == Path("/data/uploads")
    assert make_settings(max_upload_size_mb=1).max_upload_size_mb == 1
    for value in (0, -1):
        with pytest.raises(ValidationError, match="max_upload_size_mb"):
            make_settings(max_upload_size_mb=value)


# AC-010 and AC-011: duplicates --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["QUEUED", "READY", "FAILED", "UNSUPPORTED"])
async def test_AC_010_the_same_content_under_another_name_gets_409_whatever_the_status_of_the_first(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, status: str
) -> None:
    first = await add_document(engine, kb, TEXT, status=status, filename="original.txt")
    other_kb = await add_kb(engine, "Ricette")

    response = await post_file(admin, kb, "copy-with-another-name.txt", TEXT)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "duplicate_document"
    assert error["message"]
    assert error["existing"] == {"id": str(first), "filename": "original.txt"}
    assert set(error) == {"code", "message", "existing"}
    assert [row["id"] for row in await document_rows(engine)] == [first]
    assert stored_files(upload_dir) == []
    assert queued_jobs() == []

    in_other_kb = await post_file(admin, other_kb, "copy-with-another-name.txt", TEXT)

    assert in_other_kb.status_code == 202
    assert len(await document_rows(engine)) == 2


async def test_AC_010_the_same_upload_twice_through_the_api_gets_409_naming_the_first_document(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    first = await post_file(admin, kb, "first.pdf", PDF, "application/pdf")
    assert first.status_code == 202

    second = await post_file(admin, kb, "second.pdf", PDF, "application/pdf")

    assert second.status_code == 409
    assert second.json()["error"]["existing"] == {"id": first.json()["id"], "filename": "first.pdf"}
    assert stored_files(upload_dir) == [first.json()["id"]]
    assert len(await document_rows(engine)) == 1
    assert len(queued_jobs()) == 1


async def test_AC_010_a_different_content_is_not_a_duplicate(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    await add_document(engine, kb, TEXT)

    response = await post_file(admin, kb, "existing.txt", TEXT + b"one more line\n")

    assert response.status_code == 202


@pytest.mark.parametrize("round_number", range(4))
async def test_AC_011_two_simultaneous_identical_uploads_give_one_202_and_one_409(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path, round_number: int
) -> None:
    content = TEXT * (100 + round_number)

    first, second = await asyncio.gather(
        post_file(admin, kb, "first.txt", content), post_file(admin, kb, "second.txt", content)
    )

    assert sorted([first.status_code, second.status_code]) == [202, 409]
    winner, loser = (first, second) if first.status_code == 202 else (second, first)
    error = loser.json()["error"]
    assert error["code"] == "duplicate_document"
    assert error["existing"]["id"] == winner.json()["id"]
    assert error["existing"]["filename"] == winner.json()["filename"]
    rows = await document_rows(engine)
    assert [str(row["id"]) for row in rows] == [winner.json()["id"]]
    assert stored_files(upload_dir) == [winner.json()["id"]]
    assert len(queued_jobs()) == 1


async def test_AC_011_six_simultaneous_identical_uploads_give_one_202_and_five_409(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    responses = await asyncio.gather(*(post_file(admin, kb, f"copy-{n}.txt", TEXT * 50) for n in range(6)))

    assert sorted(response.status_code for response in responses) == [202, 409, 409, 409, 409, 409]
    winner = next(response for response in responses if response.status_code == 202)
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["existing"]["id"] == winner.json()["id"]
    assert len(await document_rows(engine)) == 1
    assert stored_files(upload_dir) == [winner.json()["id"]]
    assert len(queued_jobs()) == 1


async def test_AC_011_simultaneous_uploads_of_different_files_are_all_accepted(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    responses = await asyncio.gather(*(post_file(admin, kb, f"file-{n}.txt", TEXT + str(n).encode()) for n in range(5)))

    assert [response.status_code for response in responses] == [202] * 5
    assert len(await document_rows(engine)) == 5
    assert len(stored_files(upload_dir)) == 5
    assert len(queued_jobs()) == 5


# A failure after the insert leaves nothing ---------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["commit", "enqueue"])
async def test_AC_003_a_failure_after_the_insert_leaves_no_row_no_file_and_no_job(
    engine: AsyncEngine,
    admin: httpx.AsyncClient,
    kb: uuid.UUID,
    upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise OSError("simulated failure")

    if failing == "commit":
        monkeypatch.setattr(UploadStorage, "commit", boom)
    else:
        monkeypatch.setattr(Queue, "enqueue", boom)

    response = await post_file(admin, kb, "note.txt", TEXT)

    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert "simulated" not in response.text
    await assert_nothing_created(engine, upload_dir)
    monkeypatch.undo()

    retry = await post_file(admin, kb, "note.txt", TEXT)  # the same file can be uploaded again

    assert retry.status_code == 202


# AC-013 and REQ-005: ids ----------------------------------------------------------------------------------------------

KB_NOT_FOUND = {"error": {"code": "kb_not_found", "message": "Knowledge base not found"}}
DOCUMENT_NOT_FOUND = {"error": {"code": "document_not_found", "message": "Document not found"}}


@pytest.mark.parametrize("kb_id", [str(uuid.UUID(int=1)), "not-a-uuid", "123", "%20", "kb", "0" * 40])
async def test_AC_013_an_unknown_or_non_uuid_kb_gets_404_kb_not_found_on_every_route(
    engine: AsyncEngine, admin: httpx.AsyncClient, upload_dir: Path, kb_id: str
) -> None:
    document_id = uuid.uuid4()

    upload = await post_file(admin, kb_id, "note.txt", TEXT)
    listing = await admin.get(f"/api/kbs/{kb_id}/documents")
    detail = await admin.get(f"/api/kbs/{kb_id}/documents/{document_id}")

    for response in (upload, listing, detail):
        assert response.status_code == 404
        assert response.json() == KB_NOT_FOUND
    await assert_nothing_created(engine, upload_dir)


async def test_AC_013_an_unknown_kb_is_answered_before_the_size_of_the_body(
    small_admin: httpx.AsyncClient,
) -> None:
    response = await small_admin.post(
        f"/api/kbs/{uuid.uuid4()}/documents",
        content=b"x" * (3 * MIB),
        headers={**AJAX, "Content-Type": "multipart/form-data; boundary=zzz"},
    )

    assert response.status_code == 404
    assert response.json() == KB_NOT_FOUND


async def test_AC_013_an_unknown_non_uuid_or_foreign_document_gets_the_same_404(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    other_kb = await add_kb(engine, "Ricette")
    foreign = await add_document(engine, other_kb, b"foreign content", status="READY")
    mine = await add_document(engine, kb, b"my content")

    unknown = await admin.get(f"/api/kbs/{kb}/documents/{uuid.uuid4()}")
    not_uuid = await admin.get(f"/api/kbs/{kb}/documents/not-a-uuid")
    of_other_kb = await admin.get(f"/api/kbs/{kb}/documents/{foreign}")

    for response in (unknown, not_uuid, of_other_kb):
        assert response.status_code == 404
        assert response.json() == DOCUMENT_NOT_FOUND
    assert (await admin.get(f"/api/kbs/{kb}/documents/{mine}")).status_code == 200
    assert (await admin.get(f"/api/kbs/{other_kb}/documents/{foreign}")).status_code == 200


async def test_AC_013_the_repository_reads_only_the_documents_of_the_given_kb(engine: AsyncEngine) -> None:
    """Isolation test that calls the repository directly (AGENTS.md): the KB id is part of every query."""
    kb_a = await add_kb(engine, "A")
    kb_b = await add_kb(engine, "B")
    doc_a = await add_document(engine, kb_a, b"content a")
    doc_b = await add_document(engine, kb_b, b"content b")

    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        repository = DocumentRepository(session)
        assert [document.id for document in await repository.list_for_kb(kb_a)] == [doc_a]
        assert [document.id for document in await repository.list_for_kb(kb_b)] == [doc_b]
        assert await repository.list_for_kb(uuid.uuid4()) == []
        assert await repository.get(kb_a, doc_b) is None
        found = await repository.get(kb_b, doc_b)
        assert found is not None and found.id == doc_b
        assert await repository.delete(kb_a, doc_b) is False

    assert {row["id"] for row in await document_rows(engine)} == {doc_a, doc_b}


# AC-012 for the routes of this task: admin only ---------------------------------------------------------------------


async def test_AC_012_a_user_assigned_to_the_kb_gets_403_on_upload_list_and_detail_and_nothing_changes(
    engine: AsyncEngine, upload_dir: Path, kb: uuid.UUID
) -> None:
    existing = await add_document(engine, kb, b"existing content")
    async with signed_in(engine, upload_dir, "mario", "user") as api:
        mario = (await api.get("/api/auth/me")).json()["id"]
        await assign(engine, kb, uuid.UUID(mario))

        upload = await post_file(api, kb, "note.txt", TEXT)
        listing = await api.get(f"/api/kbs/{kb}/documents")
        detail = await api.get(f"/api/kbs/{kb}/documents/{existing}")

    for response in (upload, listing, detail):
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "forbidden", "message": "Administrator role required"}}
    assert [row["id"] for row in await document_rows(engine)] == [existing]
    assert stored_files(upload_dir) == []
    assert queued_jobs() == []


async def test_AC_012_a_user_gets_the_same_403_whatever_the_ids_and_the_body_it_sends(
    engine: AsyncEngine, upload_dir: Path, kb: uuid.UUID
) -> None:
    async with signed_in(engine, upload_dir, "mario", "user") as api:
        for kb_id in (kb, uuid.uuid4(), "not-a-uuid"):
            upload = await post_file(api, kb_id, "setup.exe", b"MZ", "application/x-msdownload")
            garbage = await api.post(
                f"/api/kbs/{kb_id}/documents",
                content=b"not multipart at all",
                headers={**AJAX, "Content-Type": "multipart/form-data; boundary=zzz"},
            )
            listing = await api.get(f"/api/kbs/{kb_id}/documents")
            detail = await api.get(f"/api/kbs/{kb_id}/documents/not-a-uuid")
            for response in (upload, garbage, listing, detail):
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "forbidden"
    await assert_nothing_created(engine, upload_dir)


async def test_AC_012_without_a_session_upload_list_and_detail_get_401(
    engine: AsyncEngine, upload_dir: Path, kb: uuid.UUID
) -> None:
    async with make_client(make_settings(upload_dir=upload_dir)) as api:
        upload = await post_file(api, kb, "note.txt", TEXT)
        listing = await api.get(f"/api/kbs/{kb}/documents")
        detail = await api.get(f"/api/kbs/{kb}/documents/{uuid.uuid4()}")

    for response in (upload, listing, detail):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"
    await assert_nothing_created(engine, upload_dir)


# AC-014: the list, newest first, with exactly the fields of DocumentOut -----------------------------------------------


async def test_AC_014_the_list_is_newest_first_with_exactly_the_documentout_keys(
    admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    uploads = []
    for number, name in enumerate(("first.txt", "second.txt", "third.txt")):
        uploads.append(await post_file(admin, kb, name, TEXT + str(number).encode()))
        assert uploads[-1].status_code == 202

    response = await admin.get(f"/api/kbs/{kb}/documents")

    assert response.status_code == 200
    listed = response.json()
    assert [document["filename"] for document in listed] == ["third.txt", "second.txt", "first.txt"]
    assert [document["id"] for document in listed] == [upload.json()["id"] for upload in reversed(uploads)]
    for document in listed:
        assert set(document) == DOCUMENT_OUT_KEYS
        assert document["status"] == "QUEUED"
        assert document["status_reason"] is None
        datetime.fromisoformat(document["created_at"])
        datetime.fromisoformat(document["updated_at"])
    created = [datetime.fromisoformat(document["created_at"]) for document in listed]
    assert created == sorted(created, reverse=True)


async def test_AC_014_no_response_contains_the_checksum_the_path_or_the_content(
    admin: httpx.AsyncClient, kb: uuid.UUID, upload_dir: Path
) -> None:
    content = b"Contenuto riconoscibile: ananas e mango.\n"
    created = await post_file(admin, kb, "frutta.txt", content)
    assert created.status_code == 202
    document_id = created.json()["id"]
    checksum = hashlib.sha256(content).hexdigest()

    responses = [
        created,
        await admin.get(f"/api/kbs/{kb}/documents"),
        await admin.get(f"/api/kbs/{kb}/documents/{document_id}"),
    ]

    for response in responses:
        assert checksum not in response.text
        assert str(upload_dir) not in response.text
        assert "checksum" not in response.text
        assert "ananas" not in response.text
    assert (await admin.get(f"/api/kbs/{kb}/documents/{document_id}")).json() == created.json()


async def test_AC_014_the_list_has_only_the_documents_of_that_kb_and_the_reason_of_failed_ones(
    engine: AsyncEngine, admin: httpx.AsyncClient, kb: uuid.UUID
) -> None:
    other_kb = await add_kb(engine, "Ricette")
    ready = await add_document(engine, kb, b"ready content", status="READY", filename="ready.txt")
    failed = await add_document(engine, kb, b"failed content", status="FAILED", filename="failed.txt")
    await add_document(engine, other_kb, b"foreign content", filename="foreign.txt")

    response = await admin.get(f"/api/kbs/{kb}/documents")

    assert response.status_code == 200
    listed = response.json()
    assert {document["id"] for document in listed} == {str(ready), str(failed)}
    by_id = {document["id"]: document for document in listed}
    assert by_id[str(ready)]["status"] == "READY"
    assert by_id[str(ready)]["chunk_count"] == 3
    assert by_id[str(ready)]["status_reason"] is None
    assert by_id[str(failed)]["status"] == "FAILED"
    assert by_id[str(failed)]["status_reason"] == {
        "code": "docling_error",
        "message": "Docling could not read the file",
    }
    assert all(set(document) == DOCUMENT_OUT_KEYS for document in listed)


async def test_AC_014_a_kb_without_documents_has_an_empty_list(admin: httpx.AsyncClient, kb: uuid.UUID) -> None:
    response = await admin.get(f"/api/kbs/{kb}/documents")

    assert response.status_code == 200
    assert response.json() == []


# The table: constraints chosen in the plan ---------------------------------------------------------------------------


async def insert_raw(engine: AsyncEngine, kb_id: uuid.UUID, **values: Any) -> None:
    columns = {
        "kb_id": kb_id,
        "filename": "f.txt",
        "format": "txt",
        "size_bytes": 1,
        "checksum": uuid.uuid4().hex,
        "status": "QUEUED",
        "status_reason_code": None,
        "status_reason_message": None,
        "chunk_count": 0,
        **values,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    async with engine.begin() as connection:
        await connection.execute(text(f"insert into documents ({names}) values ({placeholders})"), columns)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"status": "DONE"}, id="unknown-status"),
        pytest.param({"status": "FAILED"}, id="failed-without-reason"),
        pytest.param({"status": "UNSUPPORTED", "status_reason_code": "x"}, id="unsupported-with-only-the-code"),
        pytest.param(
            {"status": "FAILED", "status_reason_code": "", "status_reason_message": "m"}, id="failed-empty-code"
        ),
        pytest.param({"status_reason_code": "x", "status_reason_message": "m"}, id="queued-with-a-reason"),
        pytest.param({"status": "PROCESSING", "status_reason_code": "x", "status_reason_message": "m"}, id="proc"),
        pytest.param({"status": "READY", "chunk_count": 0}, id="ready-without-chunks"),
    ],
)
async def test_documents_table_refuses_inconsistent_rows(
    engine: AsyncEngine, kb: uuid.UUID, values: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        await insert_raw(engine, kb, **values)


async def test_documents_table_accepts_every_status_with_its_reason(engine: AsyncEngine, kb: uuid.UUID) -> None:
    reason = {"status_reason_code": "code", "status_reason_message": "message"}
    await insert_raw(engine, kb, status="QUEUED")
    await insert_raw(engine, kb, status="PROCESSING")
    await insert_raw(engine, kb, status="READY", chunk_count=2)
    await insert_raw(engine, kb, status="FAILED", **reason)
    await insert_raw(engine, kb, status="UNSUPPORTED", **reason)

    assert len(await document_rows(engine)) == 5


async def test_documents_table_has_one_row_per_kb_and_checksum_and_follows_the_kb_on_delete(
    engine: AsyncEngine, kb: uuid.UUID
) -> None:
    other_kb = await add_kb(engine, "Ricette")
    await insert_raw(engine, kb, checksum="abc")
    await insert_raw(engine, other_kb, checksum="abc")
    with pytest.raises(IntegrityError):
        await insert_raw(engine, kb, checksum="abc")

    async with engine.begin() as connection:
        await connection.execute(text("delete from knowledge_bases where id = :id"), {"id": kb})

    assert [row["kb_id"] for row in await document_rows(engine)] == [other_kb]


# UploadStorage --------------------------------------------------------------------------------------------------------


def test_AC_003_storage_saves_a_part_file_with_size_and_checksum_and_commit_renames_it(tmp_path: Path) -> None:
    storage = UploadStorage(tmp_path / "nested" / "uploads")  # the folder is created when missing
    document_id = uuid.uuid4()
    content = os.urandom(5000)

    stored = storage.save(document_id, io.BytesIO(content), max_bytes=10_000)

    assert (stored.size, stored.sha256) == (5000, hashlib.sha256(content).hexdigest())
    assert stored_files(tmp_path / "nested" / "uploads") == [f"{document_id}.part"]
    assert not storage.exists(document_id)

    storage.commit(document_id)

    assert stored_files(tmp_path / "nested" / "uploads") == [str(document_id)]
    assert storage.exists(document_id)
    assert storage.path(document_id) == tmp_path / "nested" / "uploads" / str(document_id)
    assert storage.part_path(document_id) == tmp_path / "nested" / "uploads" / f"{document_id}.part"
    assert storage.path(document_id).read_bytes() == content


def test_AC_008_storage_accepts_exactly_max_bytes_and_refuses_one_more_without_leaving_a_file(tmp_path: Path) -> None:
    storage = UploadStorage(tmp_path)
    exact, over = uuid.uuid4(), uuid.uuid4()

    assert storage.save(exact, io.BytesIO(b"a" * 100), max_bytes=100).size == 100
    with pytest.raises(FileTooLarge):
        storage.save(over, io.BytesIO(b"a" * 101), max_bytes=100)

    assert stored_files(tmp_path) == [f"{exact}.part"]


def test_AC_008_storage_stops_reading_a_source_larger_than_the_limit(tmp_path: Path) -> None:
    class Endless(io.RawIOBase):
        def __init__(self) -> None:
            self.served = 0

        def readable(self) -> bool:
            return True

        def readinto(self, buffer: Any) -> int:
            buffer[:] = b"a" * len(buffer)
            self.served += len(buffer)
            return len(buffer)

    source = Endless()
    storage = UploadStorage(tmp_path)

    with pytest.raises(FileTooLarge):
        storage.save(uuid.uuid4(), io.BufferedReader(source), max_bytes=MIB)

    assert source.served < 4 * MIB
    assert stored_files(tmp_path) == []


def test_AC_003_storage_discard_and_remove(tmp_path: Path) -> None:
    storage = UploadStorage(tmp_path)
    partial, stored, missing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    storage.save(partial, io.BytesIO(b"abc"), max_bytes=100)
    storage.save(stored, io.BytesIO(b"abc"), max_bytes=100)
    storage.commit(stored)

    storage.discard(partial)
    storage.discard(partial)  # nothing to discard: no error
    storage.discard(missing)

    assert stored_files(tmp_path) == [str(stored)]
    assert storage.remove(stored) is True
    assert storage.remove(stored) is False
    assert storage.remove(missing) is False
    assert stored_files(tmp_path) == []


def test_AC_003_storage_orphans_are_the_files_older_than_the_given_age(tmp_path: Path) -> None:
    storage = UploadStorage(tmp_path)
    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    ages = {"old": timedelta(hours=2), "old.part": timedelta(days=3), "recent": timedelta(minutes=10)}
    for name, age in ages.items():
        path = tmp_path / name
        path.write_bytes(b"x")
        stamp = (now - age).timestamp()
        os.utime(path, (stamp, stamp))

    orphans = storage.orphans(timedelta(hours=1), now)

    assert sorted(path.name for path in orphans) == ["old", "old.part"]
    assert all(path.parent == tmp_path for path in orphans)
    assert UploadStorage(tmp_path / "missing").orphans(timedelta(hours=1), now) == []
