# SPEC-2026-11-06-fase-1-fondazioni
"""AC-001, AC-002 (REQ-001): the full docker compose stack, started from a filled .env."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "advancedrag-e2e"
HTTP_PORT = 18080

pytestmark = pytest.mark.e2e


def compose(*args: str, env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    assert (ROOT / "compose.yaml").is_file(), "compose.yaml is missing"
    env_file = tmp_path_factory.mktemp("e2e") / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=e2epassword",
                f"HTTP_PORT={HTTP_PORT}",
                "OLLAMA_BASE_URL=http://127.0.0.1:9",
                "DOCLING_BASE_URL=http://127.0.0.1:9",
                "HEALTH_CHECK_TIMEOUT_SECONDS=1",
                "ADMIN_USERNAME=e2eadmin",
                "ADMIN_PASSWORD=e2e-admin-password",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180", env_file=env_file)
        yield env_file
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                PROJECT,
                "--env-file",
                str(env_file),
                "-f",
                str(ROOT / "compose.yaml"),
                "down",
                "-v",
                "--remove-orphans",
            ],
            cwd=ROOT,
            capture_output=True,
        )


def test_AC_001_four_services_run_and_only_nginx_publishes_ports(stack: Path) -> None:
    output = compose("ps", "--format", "json", env_file=stack).stdout
    containers = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert {c["Service"] for c in containers} == {"nginx", "backend", "redis", "db"}
    assert all(c["State"] == "running" for c in containers)
    published = {c["Service"] for c in containers if any(p.get("PublishedPort") for p in c.get("Publishers") or [])}
    assert published == {"nginx"}


def test_AC_001_nginx_serves_frontend_and_proxies_api(stack: Path) -> None:
    page = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/", timeout=10)
    assert page.status_code == 200
    assert '<div id="root"></div>' in page.text

    health = httpx.get(f"http://127.0.0.1:{HTTP_PORT}/api/health", timeout=10)
    assert health.status_code == 200
    assert health.json()["checks"]["database"] == "ok"
    assert health.json()["checks"]["redis"] == "ok"


def test_AC_002_database_has_vector_and_pg_search(stack: Path) -> None:
    output = compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "rag",
        "-d",
        "ragdb",
        "-tAc",
        "select extname from pg_extension",
        env_file=stack,
    ).stdout
    extensions = set(output.split())
    assert {"vector", "pg_search"} <= extensions
