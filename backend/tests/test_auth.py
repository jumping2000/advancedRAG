# SPEC-2026-11-06-fase-1-fondazioni
"""Login, logout and the session cookie: AC-010, AC-011, AC-012, AC-013, AC-026 (REQ-005, REQ-010).

Review focus of T-03: the cookie is Secure only in HTTPS; a POST whose Content-Type is not application/json gets 422
and creates nothing. Every authenticated request reloads the user (is_active, session_epoch).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings

PASSWORD = "correct-horse-battery"
COOKIE = "rag_session"


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


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL)
    yield client
    await client.aclose()


async def add_user(
    engine: AsyncEngine, username: str, password: str = PASSWORD, role: str = "user", is_active: bool = True
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(User(username=username, password_hash=hash_password(password), role=role, is_active=is_active))
        await session.commit()


async def change_user(engine: AsyncEngine, username: str, **values: Any) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await session.execute(update(User).where(User.username == username).values(**values))
        await session.commit()


def client(settings: Settings | None = None, base_url: str = "http://testserver") -> httpx.AsyncClient:
    app = create_app(settings or make_settings())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


@pytest.fixture
def api() -> Iterator[httpx.AsyncClient]:
    yield client()


def session_cookies(response: httpx.Response) -> list[str]:
    return [h for h in response.headers.get_list("set-cookie") if h.split("=", 1)[0].strip() == COOKIE]


def cookie_attributes(header: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in header.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    return attributes


async def login(api: httpx.AsyncClient, username: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post("/api/auth/login", json={"username": username, "password": password})


async def session_keys(redis: Redis) -> list[bytes]:
    return [key async for key in redis.scan_iter(match="session:*")]


def session_token(api: httpx.AsyncClient) -> str:
    assert COOKIE in api.cookies, "no session cookie after login"
    return api.cookies[COOKIE]


def with_cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={token}"}


# AC-010 ---------------------------------------------------------------------------------------------------------------


async def test_AC_010_login_returns_200_and_an_httponly_lax_session_cookie(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    await add_user(engine, "mario")
    async with api:
        response = await login(api, "mario")

    assert response.status_code == 200
    cookies = session_cookies(response)
    assert len(cookies) == 1
    attributes = cookie_attributes(cookies[0])
    assert "httponly" in attributes
    assert attributes.get("samesite", "").lower() == "lax"
    assert "secure" not in attributes  # the request arrived in plain HTTP


async def test_AC_010_me_returns_name_role_and_language(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    await add_user(engine, "mario")
    async with api:
        logged_in = await login(api, "mario")
        me = await api.get("/api/auth/me")

    assert me.status_code == 200
    body = me.json()
    assert set(body) == {"id", "username", "role", "is_active", "language"}
    assert (body["username"], body["role"], body["is_active"], body["language"]) == ("mario", "user", True, None)
    assert logged_in.json() == body


async def test_AC_010_login_and_me_never_contain_the_password_or_its_hash(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    await add_user(engine, "mario")
    async with api:
        logged_in = await login(api, "mario")
        me = await api.get("/api/auth/me")

    for response in (logged_in, me):
        assert response.status_code == 200
        assert PASSWORD not in response.text
        assert "$argon2" not in response.text
        assert "password" not in response.text


async def test_AC_010_cookie_is_secure_when_the_request_arrives_in_https(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with client(base_url="https://testserver") as api:
        response = await login(api, "mario")

    assert response.status_code == 200
    attributes = cookie_attributes(session_cookies(response)[0])
    assert "secure" in attributes
    assert "httponly" in attributes
    assert attributes.get("samesite", "").lower() == "lax"


async def test_AC_010_forwarded_proto_header_alone_does_not_make_the_app_trust_https(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    """The app reads the scheme only from the ASGI server, which trusts the header only behind Nginx."""
    await add_user(engine, "mario")
    async with api:
        response = await api.post(
            "/api/auth/login",
            json={"username": "mario", "password": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    assert "secure" not in cookie_attributes(session_cookies(response)[0])


async def test_AC_010_admin_login_reports_the_admin_role(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    await add_user(engine, "anna", role="admin")
    async with api:
        await login(api, "anna")
        me = await api.get("/api/auth/me")

    assert me.status_code == 200
    assert (me.json()["username"], me.json()["role"]) == ("anna", "admin")


# AC-011 ---------------------------------------------------------------------------------------------------------------


async def test_AC_011_wrong_password_and_unknown_user_get_the_same_401(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    await add_user(engine, "mario")
    async with api:
        wrong_password = await login(api, "mario", "wrong-password-123")
        unknown_user = await login(api, "nobody", "wrong-password-123")

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"
    assert set(wrong_password.json()["error"]) == {"code", "message"}
    assert session_cookies(wrong_password) == []
    assert session_cookies(unknown_user) == []


async def test_AC_011_deactivated_user_gets_the_same_401_and_no_session(
    engine: AsyncEngine, api: httpx.AsyncClient, redis: Redis
) -> None:
    await add_user(engine, "mario")
    await add_user(engine, "luigi", is_active=False)
    async with api:
        wrong_password = await login(api, "mario", "wrong-password-123")
        deactivated = await login(api, "luigi")

    assert deactivated.status_code == 401
    assert deactivated.json() == wrong_password.json()
    assert session_cookies(deactivated) == []
    assert await session_keys(redis) == []


async def test_AC_011_login_username_must_match_exactly(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    """Owner's decision: no case folding and no trimming at login (uniqueness on lower(username) is REQ-007)."""
    await add_user(engine, "mario")
    async with api:
        wrong_password = await login(api, "mario", "wrong-password-123")
        other_case = await login(api, "Mario")
        upper_case = await login(api, "MARIO")
        with_spaces = await login(api, " mario ")
        exact = await login(api, "mario")

    for response in (other_case, upper_case, with_spaces):
        assert response.status_code == 401
        assert response.json() == wrong_password.json()
        assert session_cookies(response) == []
    assert exact.status_code == 200


# AC-012 ---------------------------------------------------------------------------------------------------------------


async def test_AC_012_after_logout_the_same_cookie_gets_401(engine: AsyncEngine, redis: Redis) -> None:
    await add_user(engine, "mario")
    async with client() as api:
        await login(api, "mario")
        token = session_token(api)
        assert (await api.get("/api/auth/me")).status_code == 200
        logout = await api.post("/api/auth/logout")

    assert logout.status_code == 204
    async with client() as other:
        replay = await other.get("/api/auth/me", headers=with_cookie(token))

    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "not_authenticated"
    assert await session_keys(redis) == []


# AC-013 ---------------------------------------------------------------------------------------------------------------


async def test_AC_013_session_lives_session_idle_timeout_minutes_default_480(
    engine: AsyncEngine, api: httpx.AsyncClient, redis: Redis
) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        token = session_token(api)

    ttl = await redis.ttl(f"session:{token}")
    assert 480 * 60 - 5 <= ttl <= 480 * 60


async def test_AC_013_each_request_renews_the_idle_timeout(engine: AsyncEngine, redis: Redis) -> None:
    await add_user(engine, "mario")
    async with client(make_settings(session_idle_timeout_minutes=30)) as api:
        await login(api, "mario")
        token = session_token(api)
        key = f"session:{token}"
        assert 30 * 60 - 5 <= await redis.ttl(key) <= 30 * 60

        await redis.expire(key, 10)  # as if the user had been idle for almost 30 minutes
        me = await api.get("/api/auth/me")

        assert me.status_code == 200
        assert 30 * 60 - 5 <= await redis.ttl(key) <= 30 * 60


async def test_AC_013_idle_session_beyond_the_timeout_gets_401(
    engine: AsyncEngine, api: httpx.AsyncClient, redis: Redis
) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        token = session_token(api)
        key = f"session:{token}"

        await redis.expireat(key, 1)  # the idle timeout has run out: an expiry in the past removes the session
        assert not await redis.exists(key)
        response = await api.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# AC-026 ---------------------------------------------------------------------------------------------------------------


async def test_AC_026_no_cookie_gets_401_not_authenticated(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    async with api:
        me = await api.get("/api/auth/me")
        logout = await api.post("/api/auth/logout")

    for response in (me, logout):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"
        assert set(response.json()["error"]) == {"code", "message"}


async def test_AC_026_unknown_session_token_gets_401(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    async with api:
        response = await api.get("/api/auth/me", headers=with_cookie("not-a-real-session"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_AC_026_session_of_a_user_deactivated_afterwards_gets_401(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        await change_user(engine, "mario", is_active=False)
        response = await api.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_AC_026_session_with_an_old_session_epoch_gets_401(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        await change_user(engine, "mario", session_epoch=User.session_epoch + 1)
        response = await api.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_AC_026_session_of_a_deleted_user_gets_401(engine: AsyncEngine, api: httpx.AsyncClient) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        async with engine.begin() as connection:
            await connection.execute(text("delete from users"))
        response = await api.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# Review focus: only JSON requests (CSRF) ------------------------------------------------------------------------------


async def test_AC_010_login_with_a_form_body_gets_422_and_creates_no_session(
    engine: AsyncEngine, api: httpx.AsyncClient, redis: Redis
) -> None:
    await add_user(engine, "mario")
    async with api:
        response = await api.post("/api/auth/login", data={"username": "mario", "password": PASSWORD})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert session_cookies(response) == []
    assert await session_keys(redis) == []


async def test_AC_010_login_with_text_plain_json_gets_422_and_creates_no_session(
    engine: AsyncEngine, api: httpx.AsyncClient, redis: Redis
) -> None:
    await add_user(engine, "mario")
    async with api:
        response = await api.post(
            "/api/auth/login",
            content=f'{{"username": "mario", "password": "{PASSWORD}"}}',
            headers={"Content-Type": "text/plain"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert session_cookies(response) == []
    assert await session_keys(redis) == []


async def test_AC_012_logout_with_a_non_json_content_type_gets_422_and_keeps_the_session(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    await add_user(engine, "mario")
    async with api:
        await login(api, "mario")
        logout = await api.post("/api/auth/logout", content="x", headers={"Content-Type": "text/plain"})
        me = await api.get("/api/auth/me")

    assert logout.status_code == 422
    assert logout.json()["error"]["code"] == "validation_error"
    assert me.status_code == 200


async def test_AC_011_login_without_a_password_gets_422_validation_error(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    async with api:
        response = await api.post("/api/auth/login", json={"username": "mario"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert set(response.json()["error"]) == {"code", "message"}
