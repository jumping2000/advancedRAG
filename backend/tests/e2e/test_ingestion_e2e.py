# SPEC-2026-11-07-fase-2-ingestion
"""AC-001 (REQ-001) and AC-015 (REQ-007) through the full docker compose stack, with the fake Ollama and Docling.

The stack is `compose.yaml` plus `compose.fakes.yaml`, which adds the service `fakes` and points backend and worker at
it. The admin signs in through the API, creates a KB and uploads a small TXT through Nginx; the worker, a service of the
Compose, takes the document from QUEUED to READY. Everything is waited for with a polling loop and a timeout.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e-ingest"
HTTP_PORT = 18480
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN = "e2eadmin"
ADMIN_PASSWORD = "e2e-admin-password"
AJAX = {"X-Requested-With": "XMLHttpRequest"}
FILES = ["compose.yaml", "backend/tests/e2e/compose.fakes.yaml"]
SERVICES = {"nginx", "backend", "redis", "db", "worker"}
GUIDE = (
    b"# Guida\n\nQuesta guida spiega come usare il sistema di prova.\n\n"
    b"## Installazione\n\nSi scarica il pacchetto e si avvia il programma.\n\n"
    b"## Uso\n\nSi carica un documento e si attende lo stato Pronto.\n"
)

pytestmark = pytest.mark.e2e


def compose(env_file: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file)]
    for name in FILES:
        command += ["-f", str(ROOT / name)]
    return subprocess.run(command + list(args), cwd=ROOT, check=check, capture_output=True, text=True)


@pytest.fixture(scope="module")
def env_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    env_file = tmp_path_factory.mktemp("e2e-ingest") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                # Overridden by compose.fakes.yaml; compose.yaml only requires them to be set
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=2",
                f"ADMIN_USERNAME={ADMIN}",
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_file


@pytest.fixture(scope="module")
def stack(env_file: Path) -> Iterator[Path]:
    try:
        compose(env_file, "up", "-d", "--build", "--wait", "--wait-timeout", "240")
        yield env_file
    finally:
        compose(env_file, "down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def admin(stack: Path) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=60) as api:
        login = api.post("/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD})
        assert login.status_code == 200
        yield api


def wait_until(condition: Callable[[], bool], what: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.5)


def containers(env_file: Path) -> list[dict[str, object]]:
    output = compose(env_file, "ps", "--all", "--format", "json").stdout
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def uploads_volume(env_file: Path, service: str) -> str:
    """The name of the volume mounted at /data/uploads in the container of the service."""
    container_id = compose(env_file, "ps", "-q", service).stdout.strip()
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", container_id], check=True, capture_output=True, text=True
    )
    mounts = [mount for mount in json.loads(inspected.stdout) if mount["Destination"] == "/data/uploads"]
    assert len(mounts) == 1, f"{service} has no volume at /data/uploads"
    assert mounts[0]["Type"] == "volume"
    return str(mounts[0]["Name"])


def test_AC_001_the_stack_has_the_five_services_and_only_nginx_publishes_ports(stack: Path) -> None:
    found = containers(stack)

    # `fakes` is the test double of Ollama and Docling that compose.fakes.yaml adds; it is not a service of the product.
    assert {str(c["Service"]) for c in found} == SERVICES | {"fakes"}
    assert all(c["State"] == "running" for c in found)
    published = {c["Service"] for c in found if any(p.get("PublishedPort") for p in c.get("Publishers") or [])}
    assert published == {"nginx"}


def rendered_services(env_file: Path) -> dict[str, dict[str, object]]:
    """The services as Compose renders them (variables resolved, anchors and overrides applied)."""
    return dict(json.loads(compose(env_file, "config", "--format", "json").stdout)["services"])


WORKER_SETTINGS = {
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
    "OLLAMA_BASE_URL",
    "DOCLING_BASE_URL",
    "UPLOAD_DIR",
}
WORKER_ONLY_SETTINGS = (
    "DOCLING_API_KEY",
    "DOCLING_TIMEOUT_SECONDS",
    "DOCLING_INCLUDE_IMAGES",
    "INGESTION_MAX_ATTEMPTS",
    "INGESTION_RETRY_BACKOFF_SECONDS",
    "MIN_EXTRACTED_CHARS",
)
BACKEND_ONLY_SETTINGS = {
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "SESSION_IDLE_TIMEOUT_MINUTES",
    "MAX_UPLOAD_SIZE_MB",
    "HEALTH_CHECK_TIMEOUT_SECONDS",
}


def test_AC_001_the_worker_gets_only_the_settings_it_needs_and_none_of_the_admin_ones(env_file: Path) -> None:
    """Least privilege: no ADMIN_*, session, upload-limit or health-timeout setting reaches the worker. The stack
    boots with this environment (the other tests of this file run on it)."""
    services = rendered_services(env_file)
    worker_environment = set(dict(services["worker"]["environment"]))  # type: ignore[call-overload]
    backend_environment = set(dict(services["backend"]["environment"]))  # type: ignore[call-overload]

    assert not any(name.startswith("ADMIN_") for name in worker_environment)
    assert worker_environment & BACKEND_ONLY_SETTINGS == set()
    assert WORKER_SETTINGS <= worker_environment
    assert {"EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "EMBEDDING_BATCH_SIZE", "EMBEDDING_TIMEOUT_SECONDS"} <= (
        worker_environment
    )
    assert {"CHUNK_TARGET_WORDS", "CHUNK_MIN_WORDS", "CHUNK_MAX_WORDS", "CHUNK_OVERLAP_PERCENT"} <= worker_environment
    # Phase 2, T-05: the worker also gets the settings of Docling, of the attempts and of the minimum text
    assert set(WORKER_ONLY_SETTINGS) <= worker_environment
    allowed = (
        WORKER_SETTINGS
        | set(WORKER_ONLY_SETTINGS)
        | {name for name in worker_environment if name.startswith(("EMBEDDING_", "CHUNK_"))}
    )
    assert worker_environment == allowed
    # The backend keeps all of them, and shares every setting of the worker, except the ones only the worker needs
    # (the backend never calls Docling to convert a file, nor repeats a step: it does not get the Docling key).
    assert BACKEND_ONLY_SETTINGS <= backend_environment
    assert worker_environment - set(WORKER_ONLY_SETTINGS) <= backend_environment
    assert set(WORKER_ONLY_SETTINGS) & backend_environment == set()


def test_AC_001_backend_and_worker_run_one_image_built_once(env_file: Path) -> None:
    services = rendered_services(env_file)

    assert services["backend"]["image"] == services["worker"]["image"]
    assert "build" in services["backend"]
    assert "build" not in services["worker"]


def test_AC_001_backend_and_worker_mount_the_same_uploads_volume(stack: Path) -> None:
    backend, worker = uploads_volume(stack, "backend"), uploads_volume(stack, "worker")

    assert backend == worker
    assert backend.endswith("_uploads")


def test_AC_001_health_shows_the_worker_ok(stack: Path) -> None:
    response = httpx.get(f"{BASE_URL}/api/health", timeout=10)

    assert response.status_code == 200
    assert response.json()["checks"]["worker"] == "ok"
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "ollama": "ok", "docling": "ok", "worker": "ok"},
    }


def test_AC_015_034_an_uploaded_txt_goes_from_queued_to_ready_and_its_original_is_deleted(
    stack: Path, admin: httpx.Client
) -> None:
    kb = admin.post("/api/kbs", json={"name": "Ingestion"})
    assert kb.status_code == 201
    documents = f"/api/kbs/{kb.json()['id']}/documents"

    uploaded = admin.post(documents, files={"file": ("guida.txt", GUIDE, "text/plain")}, headers=AJAX)

    assert uploaded.status_code == 202
    document = uploaded.json()
    assert document["status"] == "QUEUED"
    assert document["chunk_count"] == 0
    seen = {document["status"]}
    current = document

    def finished() -> bool:
        nonlocal current
        current = admin.get(f"{documents}/{document['id']}").json()
        seen.add(current["status"])
        return bool(current["status"] in ("READY", "FAILED", "UNSUPPORTED"))

    wait_until(finished, "the document to be processed")

    assert current["status"] == "READY", current["status_reason"]
    assert seen <= {"QUEUED", "PROCESSING", "READY"}
    assert current["status_reason"] is None
    assert current["chunk_count"] > 0
    stored = compose(
        stack,
        "exec", "-T", "db", "psql", "-U", "rag", "-d", "ragdb", "-tAc",
        f"select count(*) from chunks where document_id = '{uuid.UUID(document['id'])}'",
    ).stdout.strip()  # fmt: skip
    assert int(stored) == current["chunk_count"]
    listing = compose(stack, "exec", "-T", "backend", "ls", "/data/uploads").stdout.split()
    assert document["id"] not in listing
