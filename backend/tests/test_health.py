# SPEC-2026-11-06-fase-1-fondazioni
"""GET /api/health: AC-005, AC-006 (REQ-003).

Revised for phase 2 (its REQ-001 and AC-002): `checks` also has `worker`, which is `ok` when a worker
heartbeat key `ingestion:worker:alive:*` exists in Redis. These tests start with no heartbeat key, so `worker` is
`unreachable` unless a test sets one.

Revised for phase 3 (its REQ-002): `checks` also has `generation`, `not_configured` with `GENERATION_BASE_URL` empty,
so the overall `status` is `degraded` unless a test configures a provider. The tests of the phase 3 ACs on the health
check are in test_generation_client.py (this file declares phase 1 only).

Revised for phase 4 (SPEC-2026-11-20-fase-4-rerank-rewrite, REQ-011, which revises requirement 002 of phase 3): `checks`
also has `rewrite` and `rerank`, `not_configured` with `REWRITE_BASE_URL` and `RERANK_BASE_URL` empty. The tests of the
phase 4 ACs are at the end of this file, with the qualified name of the spec.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from redis import Redis as SyncRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.run import migrate
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
        "rewrite": "not_configured",
        "rerank": "not_configured",
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
        "rewrite": "not_configured",
        "rerank": "not_configured",
    }


async def test_AC_005_all_dependencies_ok(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis
) -> None:
    redis_connection.set("ingestion:worker:alive:worker-host", "1", ex=30)
    with (
        fake_service() as ollama,
        fake_service() as docling,
        fake_generation_provider() as provider,
        fake_generation_provider() as rewrite,
        recording_service() as reranker,
    ):
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            rewrite_base_url=rewrite.url,
            rewrite_model="rewrite-model",
            rerank_base_url=reranker.url,
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
            "rewrite": "ok",
            "rerank": "ok",
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


# Phase 4 (SPEC-2026-11-20-fase-4-rerank-rewrite, REQ-011): the `rewrite` and `rerank` checks -------------------------

PHASE_4_NOT_CONFIGURED = {"rewrite": "not_configured", "rerank": "not_configured"}


@dataclass(frozen=True)
class SeenRequest:
    method: str
    path: str
    authorization: str | None


@dataclass
class RecordingService:
    """A fake HTTP service that records every request and answers each path of `routes` with its status (404 for any
    other path), after `delay_seconds`; with `redirect_to`, every request is answered 302 to that URL."""

    routes: dict[str, int]
    delay_seconds: float = 0.0
    redirect_to: str | None = None
    url: str = ""
    requests: list[SeenRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def seen(self) -> list[tuple[str, str]]:
        return [(request.method, request.path) for request in self.requests]


@contextmanager
def recording_service(
    routes: dict[str, int] | None = None,
    base_path: str = "",
    delay_seconds: float = 0.0,
    redirect_to: str | None = None,
) -> Iterator[RecordingService]:
    """Serve on a free local port; `url` is the base URL to configure (with `base_path`). By default it is a
    text-embeddings-inference reranker: `GET /health` answers 200."""
    state = RecordingService(
        routes=routes if routes is not None else {"/health": 200}, delay_seconds=delay_seconds, redirect_to=redirect_to
    )

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            with state.lock:
                state.requests.append(SeenRequest(self.command, self.path, self.headers.get("Authorization")))
            if state.delay_seconds:
                threading.Event().wait(state.delay_seconds)
            if state.redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", state.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = state.routes.get(self.path, 404)
            body = b'{"status": "fake"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _serve  # noqa: N815 (http.server API)
        do_POST = _serve  # noqa: N815 (http.server API)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}{base_path}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


class SentUrls:
    """Every URL that any httpx client of the process sends while a test runs (the test's own calls excluded)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.urls: list[str] = []
        send = httpx.AsyncClient.send
        urls = self.urls

        async def recording_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: object) -> httpx.Response:
            if request.url.host != "testserver":
                urls.append(str(request.url))
            return await send(self, request, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_not_configured_services_make_the_status_degraded_with_200(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other check is ok: the two services alone make the status degraded, and nothing is sent for them, also
    when their other variables are set."""
    redis_connection.set("ingestion:worker:alive:worker-host", "1", ex=30)
    sent = SentUrls(monkeypatch)
    with fake_service() as ollama, fake_service() as docling, fake_generation_provider() as provider:
        settings = make_settings(
            ollama_base_url=ollama,
            docling_base_url=docling,
            generation_base_url=provider.url,
            generation_model="fake-model",
            rewrite_model="rewrite-model",
            rewrite_api_key="sk-rewrite-unused",
            rerank_api="cohere",
            rerank_model="rerank-model",
            rerank_api_key="sk-rerank-unused",
        )
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
            "generation": "ok",
            **PHASE_4_NOT_CONFIGURED,
        },
    }
    assert sorted(sent.urls) == sorted([f"{ollama}/api/version", f"{docling}/health", f"{provider.url}/models"])


@pytest.mark.parametrize("trailing_slash", ["", "/"], ids=["no-slash", "trailing-slash"])
async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_services_that_answer_2xx_are_ok(
    client_for: Callable[[Settings], httpx.AsyncClient], trailing_slash: str
) -> None:
    with fake_generation_provider() as rewrite, recording_service({"/health": 200}) as reranker:
        settings = make_settings(
            rewrite_base_url=rewrite.url + trailing_slash,
            rewrite_model="rewrite-model",
            rerank_base_url=reranker.url + trailing_slash,
            rerank_api="tei",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["rewrite"] == "ok"
    assert response.json()["checks"]["rerank"] == "ok"
    assert [(request.method, request.path) for request in rewrite.requests] == [("GET", "/v1/models")]
    assert reranker.seen() == [("GET", "/health")]


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_services_that_cannot_be_reached_are_unreachable_with_200(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    settings = make_settings(
        rewrite_base_url=closed_port_url() + "/v1", rewrite_model="rewrite-model", rerank_base_url=closed_port_url()
    )
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["rewrite"] == "unreachable"
    assert body["checks"]["rerank"] == "unreachable"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.parametrize("status", [401, 404, 500, 503])
async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_a_status_that_is_not_2xx_is_unreachable(
    client_for: Callable[[Settings], httpx.AsyncClient], status: int
) -> None:
    with fake_generation_provider(models_status=status) as rewrite, recording_service({"/health": status}) as reranker:
        settings = make_settings(rewrite_base_url=rewrite.url, rewrite_model="m", rerank_base_url=reranker.url)
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["rewrite"] == "unreachable"
    assert response.json()["checks"]["rerank"] == "unreachable"


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_slow_services_are_unreachable_within_the_health_timeout(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    timeout = 1.0
    with (
        fake_generation_provider(models_delay_seconds=30) as rewrite,
        recording_service({"/health": 200}, delay_seconds=5.0) as reranker,
    ):
        settings = make_settings(
            rewrite_base_url=rewrite.url,
            rewrite_model="m",
            # long timeouts of the steps must not stretch the health check: it uses HEALTH_CHECK_TIMEOUT_SECONDS
            rewrite_timeout_seconds=300,
            rerank_base_url=reranker.url,
            rerank_timeout_seconds=300,
            health_check_timeout_seconds=timeout,
        )
        async with client_for(settings) as client:
            started = time.monotonic()
            response = await client.get("/api/health")
            elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["checks"]["rewrite"] == "unreachable"
    assert response.json()["checks"]["rerank"] == "unreachable"
    assert elapsed <= timeout + 1.0


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_the_http_code_is_still_decided_by_database_and_redis_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_generation_provider() as rewrite, recording_service() as reranker:
        settings = make_settings(
            redis_url=closed_port_url("redis") + "/0",
            rewrite_base_url=rewrite.url,
            rewrite_model="m",
            rerank_base_url=reranker.url,
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"] == "unreachable"
    assert response.json()["checks"]["rewrite"] == "ok"
    assert response.json()["checks"]["rerank"] == "ok"


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_020_the_cohere_format_is_checked_with_get_models(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with recording_service({"/v2/models": 200}, base_path="/v2") as reranker:
        settings = make_settings(rerank_base_url=reranker.url, rerank_api="cohere", rerank_model="rerank-v3.5")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["rerank"] == "ok"
    assert reranker.seen() == [("GET", "/v2/models")]


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_020_the_tei_format_is_not_checked_with_models(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    """A tei reranker that answers only /models is unreachable: its check is /health."""
    with recording_service({"/models": 200}) as reranker:
        settings = make_settings(rerank_base_url=reranker.url, rerank_api="tei")
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["rerank"] == "unreachable"
    assert reranker.seen() == [("GET", "/health")]


@pytest.mark.parametrize(("rerank", "rewrite"), [(False, False), (True, True), (True, False)])
async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_020_the_checks_run_whatever_the_switches(
    client_for: Callable[[Settings], httpx.AsyncClient], rerank: bool, rewrite: bool
) -> None:
    settings = make_settings()
    await asyncio.to_thread(migrate, settings.sqlalchemy_url)  # Alembic runs its own event loop
    engine = create_async_engine(settings.sqlalchemy_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("update app_settings set rerank_enabled = :rerank, rewrite_enabled = :rewrite"),
                {"rerank": rerank, "rewrite": rewrite},
            )
        with (
            fake_generation_provider() as rewriter,
            recording_service({"/v1/models": 200}, base_path="/v1") as reranker,
        ):
            configured = make_settings(
                rewrite_base_url=rewriter.url,
                rewrite_model="m",
                rerank_base_url=reranker.url,
                rerank_api="cohere",
                rerank_model="rerank-model",
            )
            async with client_for(configured) as client:
                response = await client.get("/api/health")
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("update app_settings set rerank_enabled = false, rewrite_enabled = false"))
        await engine.dispose()

    assert response.json()["checks"]["rewrite"] == "ok"
    assert response.json()["checks"]["rerank"] == "ok"
    assert [(request.method, request.path) for request in rewriter.requests] == [("GET", "/v1/models")]
    assert reranker.seen() == [("GET", "/v1/models")]


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_each_key_goes_as_bearer_to_its_own_service_only(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_generation_provider() as rewrite, recording_service() as reranker:
        settings = make_settings(
            rewrite_base_url=rewrite.url,
            rewrite_model="m",
            rewrite_api_key="sk-rewrite-key",
            rerank_base_url=reranker.url,
            rerank_api_key="sk-rerank-key",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["rewrite"] == "ok"
    assert response.json()["checks"]["rerank"] == "ok"
    assert [request.authorization for request in rewrite.requests] == ["Bearer sk-rewrite-key"]
    assert [request.authorization for request in reranker.requests] == ["Bearer sk-rerank-key"]


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_without_a_key_no_authorization_is_sent(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with fake_generation_provider() as rewrite, recording_service() as reranker:
        settings = make_settings(
            rewrite_base_url=rewrite.url,
            rewrite_model="m",
            rewrite_api_key="",
            rerank_base_url=reranker.url,
            rerank_api_key="  ",
        )
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.json()["checks"]["rerank"] == "ok"
    assert [request.authorization for request in rewrite.requests] == [None]
    assert [request.authorization for request in reranker.requests] == [None]


async def test_SPEC_2026_11_20_fase_4_rerank_rewrite_AC_019_a_redirect_is_unreachable_and_the_key_stays_home(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    with recording_service() as elsewhere:
        with (
            recording_service(redirect_to=f"{elsewhere.url}/health") as reranker,
            fake_generation_provider(redirect_to=f"{elsewhere.url}/v1/models") as rewrite,
        ):
            settings = make_settings(
                rewrite_base_url=rewrite.url,
                rewrite_model="m",
                rewrite_api_key="sk-rewrite-key",
                rerank_base_url=reranker.url,
                rerank_api_key="sk-rerank-key",
            )
            async with client_for(settings) as client:
                response = await client.get("/api/health")

    assert response.json()["checks"]["rewrite"] == "unreachable"
    assert response.json()["checks"]["rerank"] == "unreachable"
    assert elsewhere.requests == []
