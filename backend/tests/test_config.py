"""POSTGRES_PASSWORD with any character (owner's decision on the T-01 review; REQ-001, configuration anti-goal)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
from sqlalchemy.engine import make_url

from app.config import Settings
from tests.conftest import TEST_DATABASE_URL, make_settings

SPECIAL = "p@ss:w/o%rd#?&$ è"


def test_password_with_url_characters_is_kept_intact() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://rag@db:5432/ragdb", postgres_password=SPECIAL)
    url = settings.sqlalchemy_url
    assert url.password == SPECIAL
    assert (url.host, url.port, url.database, url.username) == ("db", 5432, "ragdb", "rag")
    rendered = url.render_as_string(hide_password=False)
    assert make_url(rendered).password == SPECIAL


def test_without_postgres_password_the_url_is_unchanged() -> None:
    settings = make_settings(database_url="postgresql+asyncpg://rag:secret@db:5432/ragdb")
    assert settings.sqlalchemy_url.password == "secret"


async def test_the_engine_uses_postgres_password(client_for: Callable[[Settings], httpx.AsyncClient]) -> None:
    url = make_url(TEST_DATABASE_URL)
    password = url.password
    without_password = url.set(password=None).render_as_string(hide_password=False)
    settings = make_settings(database_url=without_password, postgres_password=password)
    async with client_for(settings) as client:
        response = await client.get("/api/health")
    assert response.json()["checks"]["database"] == "ok"
