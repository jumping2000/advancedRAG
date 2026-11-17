# SPEC-2026-11-06-fase-1-fondazioni
"""GET /api/health: AC-005, AC-006 (REQ-003).

Revised for phase 2 (its REQ-001 and AC-002): `checks` also has `worker`, which is `ok` when a worker
heartbeat key `ingestion:worker:alive:*` exists in Redis. These tests start with no heartbeat key, so `worker` is
`unreachable` unless a test sets one.

Revised for phase 3 (SPEC-2026-11-17-fase-3-chat, its REQ-002, AC-004 and AC-005): `checks` also has `generation`:
`not_configured` with `GENERATION_BASE_URL` empty (no request is sent), else `ok` or `unreachable` by
`GET {GENERATION_BASE_URL}/models`. Overall `status` is `ok` only if every check is `ok`, so `not_configured` is
`degraded`. The tests of this phase cite their ACs in the qualified form, as this file is declared for phase 1.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import httpx
import pytest
from redis import Redis as SyncRedis

from app.config import Settings
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url, fake_service
from tests.fakes.services import fake_generation_provider

HEARTBEAT_PATTERN = "ingestion:worker:alive:*"


def clear_heartbeats(connection: SyncRedis) -> None:
    for key in connection.scan_iter(match=HEARTBEAT_PATTERN):
        connection.delete(key)


@pytest.fixture(autouse=True)
def redis_connection() -> Iterator[SyncRedis]:
    """The Redis of compose.test.yaml without any worker heartbeat, before and after each test."""
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    clear_heartbeats(connection)
    yield connection
    clear_heartbeats(connection)
    connection.close()


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
        "worker": "unreachable",
        "generation": "not_configured",
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
    assert body["checks"] == {
        "database": "ok",
        "redis": "ok",
        "ollama": "unreachable",
        "docling": "unreachable",
        "worker": "unreachable",
        "generation": "not_configured",
    }


async def test_AC_005_all_dependencies_ok(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis
) -> None:
    redis_connection.set("ingestion:worker:alive:worker-host", "1", ex=30)
    with fake_service() as ollama, fake_service() as docling, fake_generation_provider() as provider:
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "ollama": "ok",
            "docling": "ok",
            "worker": "ok",
            "generation": "ok",
        },
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


# Phase 3: the `generation` check ------------------------------------------------------------------------------------


class RecordedRequests:
    """Every HTTP request any httpx client of the process sends while a test runs (the URLs, in order)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.urls: list[str] = []
        send = httpx.AsyncClient.send
        urls = self.urls

        async def recording_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: object) -> httpx.Response:
            if request.url.host != "testserver":  # the test's own calls to the application
                urls.append(str(request.url))
            return await send(self, request, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_no_provider_is_not_configured_and_degraded(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis_connection.set("ingestion:worker:alive:worker-host", "1", ex=30)  # everything else is ok
    sent = RecordedRequests(monkeypatch)
    with fake_service() as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        assert settings.generation_base_url == ""
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "database": "ok",
            "redis": "ok",
            "ollama": "ok",
            "docling": "ok",
            "worker": "ok",
            "generation": "not_configured",
        },
    }
    # only Ollama and Docling were called: nothing went out for the provider
    assert sorted(sent.urls) == sorted([f"{ollama}/api/version", f"{docling}/health"])


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_model_and_key_without_a_base_url_are_still_not_configured(
    client_for: Callable[[Settings], httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = RecordedRequests(monkeypatch)
    settings = make_settings(generation_model="fake-model", generation_api_key="sk-unused")
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "not_configured"
    assert not [url for url in sent.urls if url.endswith("/models")]


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_generation_is_ok_then_unreachable_when_the_provider_stops(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with ExitStack() as stack:
        ollama = stack.enter_context(fake_service())
        docling = stack.enter_context(fake_service())
        provider = stack.enter_context(fake_generation_provider())
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            health_check_timeout_seconds=timeout,
        )
        async with client_for(settings) as client:
            started = time.monotonic()
            first = await client.get("/api/health")
            first_elapsed = time.monotonic() - started
            assert [(request.method, request.path) for request in provider.requests] == [("GET", "/v1/models")]

            stack.pop_all().close()  # the fake services and the provider stop: the same application is asked again
            started = time.monotonic()
            second = await client.get("/api/health")
            second_elapsed = time.monotonic() - started

    assert first.status_code == 200
    assert first.json()["checks"]["generation"] == "ok"
    assert second.status_code == 200
    assert second.json()["checks"]["generation"] == "unreachable"
    assert second.json()["status"] == "degraded"
    assert first_elapsed <= timeout + 1.0
    assert second_elapsed <= timeout + 1.0


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_a_slow_provider_is_unreachable_within_the_health_timeout(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with (
        fake_service() as ollama,
        fake_service() as docling,
        fake_generation_provider(models_delay_seconds=30) as provider,
    ):
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            # a long generation timeout must not stretch the health check: it uses HEALTH_CHECK_TIMEOUT_SECONDS
            generation_timeout_seconds=300,
            health_check_timeout_seconds=timeout,
        )
        async with client_for(settings) as client:
            started = time.monotonic()
            response = await client.get("/api/health")
            elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "unreachable"
    assert elapsed <= timeout + 1.0


@pytest.mark.parametrize("status", [401, 500])
async def test_SPEC_2026_11_17_fase_3_chat_AC_005_a_provider_that_errors_on_models_is_unreachable(
    client_for: Callable[[Settings], httpx.AsyncClient], status: int
) -> None:
    with fake_generation_provider(models_status=status) as provider:
        settings = make_settings(generation_base_url=provider.url, generation_model="fake-model")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["generation"] == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_005_the_health_check_sends_the_key_to_the_provider_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_service() as ollama, fake_generation_provider() as provider:
        settings = make_settings(
            ollama_base_url=ollama,
            generation_base_url=provider.url,
            generation_model="fake-model",
            generation_api_key="sk-health-key",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["generation"] == "ok"
    assert [request.authorization for request in provider.requests] == ["Bearer sk-health-key"]


async def test_SPEC_2026_11_17_fase_3_chat_AC_004_the_http_code_is_still_decided_by_database_and_redis_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    settings = make_settings(
        redis_url=closed_port_url("redis") + "/0", generation_base_url=closed_port_url() + "/v1", generation_model="m"
    )
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "unreachable"
    assert response.json()["checks"]["generation"] == "unreachable"


async def test_SPEC_2026_11_17_fase_3_chat_AC_003_an_empty_key_sends_no_authorization_to_the_provider(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_generation_provider() as provider:
        settings = make_settings(generation_base_url=provider.url, generation_model="fake-model", generation_api_key="")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["generation"] == "ok"
    assert [request.authorization for request in provider.requests] == [None]
