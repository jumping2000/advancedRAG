# SPEC-2026-11-07-fase-2-ingestion
"""AC-009 (REQ-003): the upload limit through the full docker compose stack, behind Nginx.

MAX_UPLOAD_SIZE_MB is 1, so Nginx lets through bodies up to 2 MB on the upload route (the limit plus 1 MB for the
multipart framing) and answers 413 itself above that, with the same JSON body as the backend; the rest of /api/ stays
at 1 MB. Whether a request reached the backend is read from its access log (a marker in the query string), so the
test tells an Nginx refusal from a backend one.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-upload"
HTTP_PORT = 18380
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
MIB = 1024 * 1024
AJAX = {"X-Requested-With": "XMLHttpRequest"}
FILE_TOO_LARGE = {"error": {"code": "file_too_large", "message": "The file is too large"}}

pytestmark = pytest.mark.e2e


def compose(
    env_file: Path, *args: str, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"), *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    env_file = tmp_path_factory.mktemp("e2e-upload") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=1",
                f"ADMIN_USERNAME={ADMIN}",
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}",
                "MAX_UPLOAD_SIZE_MB=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        compose(env_file, "up", "-d", "--build", "--wait", "--wait-timeout", "180")
        yield env_file
    finally:
        compose(env_file, "down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def kb_documents_url(stack: Path) -> Iterator[tuple[httpx.Client, str]]:
    """An admin client signed in through the API and the upload URL of a new knowledge base."""
    with httpx.Client(base_url=BASE_URL, timeout=60) as api:
        login = api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD})
        assert login.status_code == 200
        created = api.post("/api/kbs", json={"name": "Upload"})
        assert created.status_code == 201
        yield api, f"/api/kbs/{created.json()['id']}/documents"


def multipart(filename: str, content: bytes) -> tuple[bytes, dict[str, str]]:
    boundary = "----e2eboundary7MA4YWxkTrZu0gW"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
    head += "Content-Type: text/plain\r\n\r\n"
    body = head.encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, {**AJAX, "Content-Type": f"multipart/form-data; boundary={boundary}"}


def upload(api: httpx.Client, url: str, content: bytes, marker: str) -> httpx.Response:
    body, headers = multipart("e2e.txt", content)
    return api.post(f"{url}?marker={marker}", content=body, headers=headers)


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.25)


def backend_log(stack: Path) -> str:
    result = compose(stack, "logs", "--no-color", "backend")
    return result.stdout + result.stderr


def test_AC_009_a_body_above_the_limit_gets_413_json_from_nginx_without_reaching_the_backend(
    stack: Path, kb_documents_url: tuple[httpx.Client, str]
) -> None:
    api, url = kb_documents_url
    blocked, reached = f"blocked{uuid.uuid4().hex}", f"reached{uuid.uuid4().hex}"

    response = upload(api, url, b"a" * (3 * MIB), blocked)

    assert response.status_code == 413
    assert response.headers["content-type"].split(";")[0] == "application/json"
    assert response.json() == FILE_TOO_LARGE
    # A request that follows reaches the backend; once it is in the log, the refused one would be too if it had come.
    assert upload(api, url, b"small file\n", reached).status_code == 202
    wait_until(lambda: reached in backend_log(stack), "the access log of the accepted request")
    assert blocked not in backend_log(stack)


def test_AC_009_a_file_within_the_limit_reaches_the_backend_and_is_stored_in_the_uploads_volume(
    stack: Path, kb_documents_url: tuple[httpx.Client, str]
) -> None:
    api, url = kb_documents_url
    marker = f"reached{uuid.uuid4().hex}"
    content = b"A valid text file.\n" * 100

    response = upload(api, url, content, marker)

    assert response.status_code == 202
    document = response.json()
    assert document["status"] == "QUEUED"
    assert document["size_bytes"] == len(content)
    wait_until(lambda: marker in backend_log(stack), "the request in the backend access log")
    listing = compose(stack, "exec", "-T", "backend", "ls", "/data/uploads").stdout.split()
    assert document["id"] in listing


def test_AC_009_a_file_over_the_limit_inside_the_nginx_margin_gets_the_same_413_body_from_the_backend(
    stack: Path, kb_documents_url: tuple[httpx.Client, str]
) -> None:
    api, url = kb_documents_url
    marker = f"reached{uuid.uuid4().hex}"

    response = upload(api, url, b"a" * (MIB + MIB // 2), marker)

    assert response.status_code == 413
    assert response.json() == FILE_TOO_LARGE
    wait_until(lambda: marker in backend_log(stack), "the request in the backend access log")


def test_AC_009_the_rest_of_the_api_keeps_the_limit_of_1_mb(
    stack: Path, kb_documents_url: tuple[httpx.Client, str]
) -> None:
    api, _ = kb_documents_url

    response = api.post("/api/kbs", json={"name": "x" * (MIB + MIB // 2)})

    assert response.status_code == 413
    assert "file_too_large" not in response.text


@pytest.mark.parametrize("value", ["abc", "0"])
def test_AC_009_nginx_does_not_start_with_an_invalid_upload_limit(stack: Path, value: str) -> None:
    try:
        result = compose(
            stack, "run", "--rm", "--no-deps", "-e", f"MAX_UPLOAD_SIZE_MB={value}", "nginx", check=False, timeout=90
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"nginx kept running with MAX_UPLOAD_SIZE_MB={value}")

    assert result.returncode != 0
    assert "MAX_UPLOAD_SIZE_MB" in result.stdout + result.stderr
