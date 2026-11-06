"""Shared fixtures: settings pointing at the services of compose.test.yaml."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://rag:rag@127.0.0.1:55432/rag_test")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "ollama_base_url": "http://127.0.0.1:9",
        "docling_base_url": "http://127.0.0.1:9",
        "health_check_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


ClientFactory = Callable[..., AsyncIterator[httpx.AsyncClient]]


@pytest.fixture
def client_for() -> Callable[[Settings], httpx.AsyncClient]:
    """Build an HTTP client bound to an app created with the given settings."""

    def build(settings: Settings) -> httpx.AsyncClient:
        app = create_app(settings)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    return build
