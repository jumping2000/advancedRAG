# SPEC-2026-11-06-fase-1-fondazioni
"""The language chosen with the selector is saved in the profile and found again at the next login: AC-028 (REQ-011).

Interface of the plan: PATCH /api/auth/me {"language": "it"|"en"} -> 200 UserOut; any other value -> 422
validation_error and nothing changes; without a session -> 401 not_authenticated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.db.models import User
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings

PASSWORD = "correct-horse-battery"
ITALIAN_BROWSER = {"Accept-Language": "it-IT,it;q=0.9,en;q=0.5"}


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no users in the database, no sessions in Redis."""
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table users cascade"))
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield engine
    await engine.dispose()


async def add_user(engine: AsyncEngine, username: str, role: str = "user", language: str | None = None) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(User(username=username, password_hash=hash_password(PASSWORD), role=role, language=language))
        await session.commit()


async def stored_user(engine: AsyncEngine, username: str) -> User:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.username == username))
    assert user is not None
    return user


def browser() -> httpx.AsyncClient:
    """A new browser: its own cookie jar, Italian as first language."""
    app = create_app(make_settings())
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=ITALIAN_BROWSER
    )


async def login(api: httpx.AsyncClient, username: str) -> httpx.Response:
    response = await api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


async def choose(api: httpx.AsyncClient, language: Any) -> httpx.Response:
    return await api.patch("/api/auth/me", json={"language": language})


# AC-028 ---------------------------------------------------------------------------------------------------------------


async def test_AC_028_choosing_english_returns_200_and_the_user_with_language_en(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with browser() as api:
        logged_in = await login(api, "mario")
        response = await choose(api, "en")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "username", "role", "is_active", "language"}
    assert body == {**logged_in.json(), "language": "en"}


async def test_AC_028_english_chosen_then_logout_and_login_again_from_an_italian_browser_is_english(
    engine: AsyncEngine,
) -> None:
    await add_user(engine, "mario")
    async with browser() as api:
        await login(api, "mario")
        assert (await choose(api, "en")).status_code == 200
        assert (await api.post("/api/auth/logout")).status_code == 204
        assert (await api.get("/api/auth/me")).status_code == 401

        again = await login(api, "mario")
        me = await api.get("/api/auth/me")

    assert again.json()["language"] == "en"
    assert me.json()["language"] == "en"


async def test_AC_028_the_language_is_found_from_another_browser(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with browser() as first:
        await login(first, "mario")
        assert (await choose(first, "en")).status_code == 200

    async with browser() as second:
        logged_in = await login(second, "mario")

    assert logged_in.json()["language"] == "en"
    assert (await stored_user(engine, "mario")).language == "en"


async def test_AC_028_the_language_can_be_changed_back_to_italian(engine: AsyncEngine) -> None:
    await add_user(engine, "mario", language="en")
    async with browser() as api:
        assert (await login(api, "mario")).json()["language"] == "en"
        response = await choose(api, "it")
        me = await api.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["language"] == "it"
    assert me.json()["language"] == "it"


async def test_AC_028_the_admin_saves_the_language_too(engine: AsyncEngine) -> None:
    await add_user(engine, "admin", role="admin")
    async with browser() as api:
        await login(api, "admin")
        response = await choose(api, "en")

    assert response.status_code == 200
    assert (response.json()["role"], response.json()["language"]) == ("admin", "en")


async def test_AC_028_the_choice_of_one_user_does_not_change_another_user(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    await add_user(engine, "luigi")
    async with browser() as api:
        await login(api, "mario")
        assert (await choose(api, "en")).status_code == 200

    assert (await stored_user(engine, "luigi")).language is None


# AC-028 error cases ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["de", "EN", "It", "en-US", "", " en", None, 1, ["en"]])
async def test_AC_028_a_language_other_than_it_or_en_gets_422_and_changes_nothing(
    engine: AsyncEngine, language: Any
) -> None:
    await add_user(engine, "mario", language="it")
    async with browser() as api:
        await login(api, "mario")
        response = await choose(api, language)
        me = await api.get("/api/auth/me")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert set(response.json()["error"]) == {"code", "message"}
    assert me.json()["language"] == "it"
    assert (await stored_user(engine, "mario")).language == "it"


async def test_AC_028_a_body_without_language_gets_422_validation_error(engine: AsyncEngine) -> None:
    await add_user(engine, "mario", language="it")
    async with browser() as api:
        await login(api, "mario")
        response = await api.patch("/api/auth/me", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert (await stored_user(engine, "mario")).language == "it"


async def test_AC_028_without_a_session_the_language_cannot_be_saved(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with browser() as api:
        response = await choose(api, "en")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert (await stored_user(engine, "mario")).language is None


async def test_AC_028_a_non_json_request_gets_422_and_changes_nothing(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with browser() as api:
        await login(api, "mario")
        response = await api.patch("/api/auth/me", data={"language": "en"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert (await stored_user(engine, "mario")).language is None


async def test_AC_028_other_profile_fields_in_the_body_are_ignored_only_the_language_changes(
    engine: AsyncEngine,
) -> None:
    await add_user(engine, "mario")
    async with browser() as api:
        await login(api, "mario")
        response = await api.patch(
            "/api/auth/me", json={"language": "en", "role": "admin", "username": "root", "is_active": False}
        )
        me = await api.get("/api/auth/me")

    assert response.status_code == 200
    assert (response.json()["username"], response.json()["role"], response.json()["is_active"]) == (
        "mario",
        "user",
        True,
    )
    assert response.json()["language"] == "en"
    assert me.json() == response.json()
    stored = await stored_user(engine, "mario")
    assert (stored.username, stored.role, stored.is_active, stored.language) == ("mario", "user", True, "en")
