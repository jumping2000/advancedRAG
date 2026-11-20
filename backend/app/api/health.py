"""GET /api/health: state of database, Redis, Ollama, Docling, the ingestion worker, the generation provider, the
rewriting service and the reranker.

REQ-003 of phase 1, REQ-001 of phase 2, REQ-002 of phase 3 and REQ-011 of phase 4: `generation`, `rewrite` and `rerank`
are `not_configured` when their base URL is empty (and then nothing is sent to them), so the overall status is
`degraded` then; `rewrite` and `rerank` depend on the configuration, not on the switches of the admin. The keys go as
"Authorization: Bearer" to their own service only (a redirect is not followed). The HTTP code depends only on database
and Redis.
"""

import asyncio
from collections.abc import Awaitable
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat.generation import OpenAICompatibleClient
from app.config import Settings
from app.worker import HEARTBEAT_PREFIX

router = APIRouter()

CheckState = Literal["ok", "unreachable", "not_configured"]


async def _database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("select 1"))


async def _redis(client: Redis) -> None:
    await client.ping()


async def _worker(client: Redis) -> None:
    """Any heartbeat key of a worker will do (SCAN, not KEYS, so Redis is never blocked)."""
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=f"{HEARTBEAT_PREFIX}*", count=100)
        if keys:
            return
        if cursor == 0:
            raise LookupError("no worker heartbeat")


async def _http(url: str, limit_seconds: float) -> None:
    async with httpx.AsyncClient(timeout=limit_seconds) as client:
        response = await client.get(url)
        response.raise_for_status()


async def _state(check: Awaitable[None], limit_seconds: float) -> CheckState:
    try:
        await asyncio.wait_for(check, limit_seconds)
    except Exception:
        return "unreachable"
    return "ok"


async def _generation(settings: Settings, limit_seconds: float) -> CheckState:
    """`not_configured` without a base URL (and then no request is sent), else `ok` or `unreachable`."""
    if not settings.generation_base_url:
        return "not_configured"
    key = settings.generation_api_key.get_secret_value() if settings.generation_api_key else None
    client = OpenAICompatibleClient(
        settings.generation_base_url, settings.generation_model, key, settings.generation_timeout_seconds
    )
    return await client.check(limit_seconds)


async def _rewrite(settings: Settings, limit_seconds: float) -> CheckState:
    """`GET {REWRITE_BASE_URL}/models`, the same check as the generation provider (an OpenAI-compatible API)."""
    if not settings.rewrite_configured:
        return "not_configured"
    key = settings.rewrite_api_key.get_secret_value() if settings.rewrite_api_key else None
    client = OpenAICompatibleClient(
        settings.rewrite_base_url, settings.rewrite_model, key, settings.rewrite_timeout_seconds
    )
    return await client.check(limit_seconds)


async def _rerank(settings: Settings, limit_seconds: float) -> CheckState:
    """`GET {RERANK_BASE_URL}/health` for `tei`, `GET {RERANK_BASE_URL}/models` for `cohere`: `ok` for a 2xx within
    `limit_seconds`, `unreachable` otherwise (the error itself is not kept: it could show the key)."""
    if not settings.rerank_configured:
        return "not_configured"
    path = "/health" if settings.rerank_api == "tei" else "/models"
    key = settings.rerank_api_key.get_secret_value().strip() if settings.rerank_api_key else ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with asyncio.timeout(limit_seconds), httpx.AsyncClient(timeout=limit_seconds) as client:
            response = await client.get(f"{settings.rerank_base_url.rstrip('/')}{path}", headers=headers)
    except Exception:
        return "unreachable"
    return "ok" if response.is_success else "unreachable"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    timeout = settings.health_check_timeout_seconds
    database, redis, ollama, docling, worker, generation, rewrite, rerank = await asyncio.gather(
        _state(_database(request.app.state.engine), timeout),
        _state(_redis(request.app.state.redis), timeout),
        _state(_http(f"{settings.ollama_base_url.rstrip('/')}/api/version", timeout), timeout),
        _state(_http(f"{settings.docling_base_url.rstrip('/')}/health", timeout), timeout),
        _state(_worker(request.app.state.redis), timeout),
        _generation(settings, timeout),
        _rewrite(settings, timeout),
        _rerank(settings, timeout),
    )
    checks = {
        "database": database,
        "redis": redis,
        "ollama": ollama,
        "docling": docling,
        "worker": worker,
        "generation": generation,
        "rewrite": rewrite,
        "rerank": rerank,
    }
    status = "ok" if all(state == "ok" for state in checks.values()) else "degraded"
    code = 200 if database == "ok" and redis == "ok" else 503
    return JSONResponse({"status": status, "checks": checks}, status_code=code)
