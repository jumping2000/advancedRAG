# SPEC-2026-11-06-fase-1-fondazioni
"""GET /api/health: AC-005, AC-006 (REQ-003)."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from app.config import Settings
from tests.conftest import make_settings
from tests.fakes.http_server import closed_port_url, fake_service


async def test_AC_005_slow_ollama_is_unreachable_and_health_is_200_within_timeout(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with fake_service(delay_seconds=5.0) as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling, health_check_timeout_seconds=timeout)
        async with client_for(settings) as client:
            started = time.monotonic()
            response = await client.get("/api/health")
            elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["checks"] == {
        "database": "ok",
        "redis": "ok",
        "ollama": "unreachable",
        "docling": "ok",
    }
    assert elapsed <= timeout + 1.0


async def test_AC_005_refused_ollama_and_docling_do_not_block_health(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    settings = make_settings(ollama_base_url=closed_port_url(), docling_base_url=closed_port_url())
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": "ok", "redis": "ok", "ollama": "unreachable", "docling": "unreachable"}


async def test_AC_005_all_dependencies_ok(client_for: Callable[[Settings], httpx.AsyncClient]) -> None:
    with fake_service() as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok", "ollama": "ok", "docling": "ok"},
    }


async def test_AC_005_ollama_error_status_is_unreachable(client_for: Callable[[Settings], httpx.AsyncClient]) -> None:
    with fake_service(status=500) as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["ollama"] == "unreachable"


async def test_AC_006_redis_down_returns_503(client_for: Callable[[Settings], httpx.AsyncClient]) -> None:
    settings = make_settings(redis_url=closed_port_url("redis") + "/0")
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "unreachable"
    assert body["checks"]["database"] == "ok"


async def test_AC_006_database_down_returns_503(client_for: Callable[[Settings], httpx.AsyncClient]) -> None:
    """Same rule of REQ-003 for the database: 503 when database or Redis is not ok."""
    port = closed_port_url().rsplit(":", 1)[1]
    settings = make_settings(database_url=f"postgresql+asyncpg://rag:rag@127.0.0.1:{port}/rag_test")
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unreachable"
    assert response.json()["checks"]["redis"] == "ok"
