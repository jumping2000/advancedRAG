"""GET /api/health: state of database, Redis, Ollama, Docling and the ingestion worker (REQ-003, REQ-001 of phase 2)."""

import asyncio
from collections.abc import Awaitable
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.worker import HEARTBEAT_PREFIX

router = APIRouter()

CheckState = Literal["ok", "unreachable"]


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


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    timeout = settings.health_check_timeout_seconds
    database, redis, ollama, docling, worker = await asyncio.gather(
        _state(_database(request.app.state.engine), timeout),
        _state(_redis(request.app.state.redis), timeout),
        _state(_http(f"{settings.ollama_base_url.rstrip('/')}/api/version", timeout), timeout),
        _state(_http(f"{settings.docling_base_url.rstrip('/')}/health", timeout), timeout),
        _state(_worker(request.app.state.redis), timeout),
    )
    checks = {"database": database, "redis": redis, "ollama": ollama, "docling": docling, "worker": worker}
    status = "ok" if all(state == "ok" for state in checks.values()) else "degraded"
    code = 200 if database == "ok" and redis == "ok" else 503
    return JSONResponse({"status": status, "checks": checks}, status_code=code)
