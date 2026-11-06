# SPEC-2026-11-06-fase-1-fondazioni
"""User management by the admin: AC-014, AC-015, AC-016, AC-017, AC-018, AC-019, AC-025 (REQ-006, REQ-007, REQ-010).

Review focus of T-05: names with upper case, leading or trailing spaces and Unicode characters (spaces are removed
before the uniqueness check, the comparison is on lower()); concurrent creation of the same name gets 409, not 500.
Also here (deferred from the T-03 review): the session cookie ends with the browser (AC-010), and no log line contains
a password or a hash, not even the one of a database error (REQ-006, AC-015).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password, verify_password
from app.db.models import User
from app.db.session import create_engine
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings

ADMIN = "anna"
ADMIN_PASSWORD = "admin-password-123"
PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "brand-new-password-42"
COOKIE = "rag_session"
USERS = "/api/admin/users"
USER_OUT_KEYS = {"id", "username", "role", "is_active", "language"}


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


async def add_user(
    engine: AsyncEngine, username: str, password: str = PASSWORD, role: str = "user", is_active: bool = True
) -> uuid.UUID:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        user = User(username=username, password_hash=hash_password(password), role=role, is_active=is_active)
        session.add(user)
        await session.commit()
        return user.id


async def rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("select id, username, password_hash, role, is_active, session_epoch from users order by username")
        )
        return [dict(row._mapping) for row in result]


async def row_of(engine: AsyncEngine, username: str) -> dict[str, Any]:
    matching = [row for row in await rows(engine) if row["username"] == username]
    assert len(matching) == 1, f"expected one row for {username!r}, found {len(matching)}"
    return matching[0]


async def count_named(engine: AsyncEngine, name: str) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(User).where(func.lower(User.username) == name))
        return int(count or 0)


def client() -> httpx.AsyncClient:
    app = create_app(make_settings())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def login(api: httpx.AsyncClient, username: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post("/api/auth/login", json={"username": username, "password": password})


@pytest.fixture
async def admin(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """A client signed in as the admin anna."""
    await add_user(engine, ADMIN, ADMIN_PASSWORD, role="admin")
    async with client() as api:
        assert (await login(api, ADMIN, ADMIN_PASSWORD)).status_code == 200
        yield api


@pytest.fixture
async def mario(engine: AsyncEngine) -> AsyncIterator[tuple[uuid.UUID, httpx.AsyncClient]]:
    """The user mario and a client signed in as mario."""
    user_id = await add_user(engine, "mario")
    async with client() as api:
        assert (await login(api, "mario")).status_code == 200
        yield user_id, api


async def create(api: httpx.AsyncClient, username: str, password: str = PASSWORD, role: str = "user") -> httpx.Response:
    return await api.post(USERS, json={"username": username, "password": password, "role": role})


def error_code(response: httpx.Response) -> str:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    code: str = body["error"]["code"]
    return code


def assert_no_secret(response: httpx.Response, *passwords: str) -> None:
    for password in passwords:
        assert password not in response.text
    assert "$argon2" not in response.text
    # No password field in any body; the error code password_too_short (plan's interface) may name the password.
    assert '"password"' not in response.text
    assert "password_hash" not in response.text


# AC-014 ---------------------------------------------------------------------------------------------------------------


async def test_AC_014_creation_with_an_11_character_password_gets_422_and_no_user(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    short = "a" * 11
    response = await create(admin, "mario", short)

    assert response.status_code == 422
    assert error_code(response) == "password_too_short"
    assert short not in response.text
    assert await count_named(engine, "mario") == 0
    listed = await admin.get(USERS)
    assert "mario" not in [user["username"] for user in listed.json()]


async def test_AC_014_a_12_character_password_is_accepted(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await create(admin, "mario", "a" * 12)

    assert response.status_code == 201
    assert await count_named(engine, "mario") == 1


async def test_AC_014_password_length_counts_characters_not_bytes(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    eleven = await create(admin, "luigi", "è" * 11)  # 22 bytes in UTF-8, still 11 characters
    twelve = await create(admin, "mario", "è" * 12)

    assert eleven.status_code == 422
    assert error_code(eleven) == "password_too_short"
    assert twelve.status_code == 201


async def test_AC_014_reset_with_an_11_character_password_gets_422_and_keeps_the_old_one(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    user_id = await add_user(engine, "mario")
    before = await row_of(engine, "mario")

    response = await admin.put(f"{USERS}/{user_id}/password", json={"password": "b" * 11})

    assert response.status_code == 422
    assert error_code(response) == "password_too_short"
    after = await row_of(engine, "mario")
    assert after["password_hash"] == before["password_hash"]
    assert after["session_epoch"] == before["session_epoch"]
    async with client() as api:
        assert (await login(api, "mario")).status_code == 200


async def test_AC_014_creation_without_a_password_gets_422_and_no_user(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    response = await admin.post(USERS, json={"username": "mario", "role": "user"})

    assert response.status_code == 422
    assert await count_named(engine, "mario") == 0


# AC-015 ---------------------------------------------------------------------------------------------------------------


async def test_AC_015_the_row_holds_an_argon2id_hash_of_the_password(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    response = await create(admin, "mario")

    assert response.status_code == 201
    row = await row_of(engine, "mario")
    assert row["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in row["password_hash"]
    assert verify_password(PASSWORD, row["password_hash"])


async def test_AC_015_creation_returns_201_and_a_user_out_without_password_or_hash(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    response = await create(admin, "mario", role="admin")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == USER_OUT_KEYS
    assert (body["username"], body["role"], body["is_active"], body["language"]) == ("mario", "admin", True, None)
    assert uuid.UUID(body["id"]) == (await row_of(engine, "mario"))["id"]
    assert_no_secret(response, PASSWORD)


async def test_AC_015_no_user_api_response_contains_the_password_or_the_hash(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    created = await create(admin, "mario")
    assert created.status_code == 201
    user_id = created.json()["id"]
    listed = await admin.get(USERS)
    deactivated = await admin.patch(f"{USERS}/{user_id}", json={"is_active": False})
    reset = await admin.put(f"{USERS}/{user_id}/password", json={"password": NEW_PASSWORD})
    duplicate = await create(admin, "MARIO", NEW_PASSWORD)
    too_short = await admin.put(f"{USERS}/{user_id}/password", json={"password": "short-pass"})

    assert [created.status_code, listed.status_code, deactivated.status_code] == [201, 200, 200]
    assert [reset.status_code, duplicate.status_code, too_short.status_code] == [204, 409, 422]
    assert reset.content == b""
    for response in (created, listed, deactivated, reset, duplicate, too_short):
        assert_no_secret(response, PASSWORD, NEW_PASSWORD, "short-pass")
        assert ADMIN_PASSWORD not in response.text


async def test_AC_015_the_list_returns_every_user_as_user_out(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    await add_user(engine, "mario")
    await add_user(engine, "luigi", is_active=False)

    response = await admin.get(USERS)

    assert response.status_code == 200
    users = response.json()
    assert all(set(user) == USER_OUT_KEYS for user in users)
    assert {(u["username"], u["role"], u["is_active"]) for u in users} == {
        (ADMIN, "admin", True),
        ("mario", "user", True),
        ("luigi", "user", False),
    }
    assert_no_secret(response, PASSWORD, ADMIN_PASSWORD)


async def test_AC_015_no_log_line_contains_a_password_or_a_hash(
    engine: AsyncEngine, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Every logger at DEBUG, SQLAlchemy's included: its statement logs must not show the bound parameters."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")

    created = await create(admin, "mario")
    assert created.status_code == 201
    duplicate = await create(admin, "Mario", NEW_PASSWORD)
    reset = await admin.put(f"{USERS}/{created.json()['id']}/password", json={"password": NEW_PASSWORD})
    async with client() as api:
        signed_in = await login(api, "mario", NEW_PASSWORD)

    statuses = [created.status_code, duplicate.status_code, reset.status_code, signed_in.status_code]
    assert statuses == [201, 409, 204, 200]
    assert "INSERT INTO users" in caplog.text  # the statements are logged, without their parameters
    for secret in (PASSWORD, NEW_PASSWORD, ADMIN_PASSWORD, "$argon2"):
        assert secret not in caplog.text


async def test_AC_015_a_database_error_does_not_show_the_hash(engine: AsyncEngine) -> None:
    """The text of a database error (what a traceback in the logs prints) does not contain the bound parameters."""
    hashed = hash_password(PASSWORD)
    app_engine = create_engine(make_settings().sqlalchemy_url)
    statement = insert(User).values(username="mario", password_hash=hashed, role="user")
    try:
        async with app_engine.begin() as connection:
            await connection.execute(statement)
        with pytest.raises(IntegrityError) as raised:
            async with app_engine.begin() as connection:
                await connection.execute(statement.values(username="MARIO"))
    finally:
        await app_engine.dispose()

    assert "users_username_lower_key" in str(raised.value)
    assert hashed not in str(raised.value)
    assert "$argon2" not in str(raised.value)


# AC-016 ---------------------------------------------------------------------------------------------------------------


async def test_AC_016_creating_Mario_when_mario_exists_gets_409(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    await add_user(engine, "mario")

    response = await create(admin, "Mario", NEW_PASSWORD)

    assert response.status_code == 409
    assert error_code(response) == "username_taken"
    assert await count_named(engine, "mario") == 1
    assert (await row_of(engine, "mario"))["role"] == "user"


@pytest.mark.parametrize("name", ["mario", "MARIO", "mArIo", " mario", "mario ", "  Mario\t"])
async def test_AC_016_same_name_in_any_case_or_with_surrounding_spaces_gets_409(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    await add_user(engine, "mario")

    response = await create(admin, name, NEW_PASSWORD)

    assert response.status_code == 409
    assert error_code(response) == "username_taken"
    assert await count_named(engine, "mario") == 1


async def test_AC_016_surrounding_spaces_are_removed_and_the_case_is_kept(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    response = await create(admin, "  Mario.Rossi_1-x ")

    assert response.status_code == 201
    assert response.json()["username"] == "Mario.Rossi_1-x"
    assert (await row_of(engine, "Mario.Rossi_1-x"))["role"] == "user"
    async with client() as api:
        assert (await login(api, "Mario.Rossi_1-x")).status_code == 200


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "ab",
        " ab ",
        "a" * 65,
        "mario rossi",
        "màrio",
        "mario€",
        "ｍario",  # fullwidth m
        "mariо",  # Cyrillic o
        "mario!",
        "mario@example.com",
        "mario/rossi",
        "mario\nrossi",
    ],
)
async def test_AC_016_invalid_usernames_get_422_invalid_username_and_no_user(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    response = await create(admin, name)

    assert response.status_code == 422
    assert error_code(response) == "invalid_username"
    assert [row["username"] for row in await rows(engine)] == [ADMIN]


@pytest.mark.parametrize("name", ["abc", "a" * 64, "Mario_Rossi", "mario.rossi", "mario-rossi", "007"])
async def test_AC_016_valid_usernames_from_3_to_64_characters_are_accepted(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    response = await create(admin, name)

    assert response.status_code == 201
    assert response.json()["username"] == name


async def test_AC_016_concurrent_creation_of_the_same_name_gives_one_201_and_409s(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    names = ["mario", "Mario", "MARIO", " mario", "mArio"]

    responses = await asyncio.gather(*(create(admin, name) for name in names))

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409, 409, 409, 409]
    assert all(error_code(r) == "username_taken" for r in responses if r.status_code == 409)
    assert await count_named(engine, "mario") == 1


async def test_AC_016_unknown_role_gets_422_and_no_user(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await create(admin, "mario", role="superuser")

    assert response.status_code == 422
    assert await count_named(engine, "mario") == 0


async def test_AC_016_a_created_user_can_sign_in_with_the_initial_password(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    assert (await create(admin, "mario")).status_code == 201

    async with client() as api:
        signed_in = await login(api, "mario")
        me = await api.get("/api/auth/me")

    assert signed_in.status_code == 200
    assert (me.json()["username"], me.json()["role"]) == ("mario", "user")


# AC-017 ---------------------------------------------------------------------------------------------------------------


async def test_AC_017_deactivation_ends_the_session_and_blocks_login_until_reactivation(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, mario_api = mario
    assert (await mario_api.get("/api/auth/me")).status_code == 200

    deactivated = await admin.patch(f"{USERS}/{user_id}", json={"is_active": False})

    assert deactivated.status_code == 200
    assert set(deactivated.json()) == USER_OUT_KEYS
    assert (deactivated.json()["username"], deactivated.json()["is_active"]) == ("mario", False)
    next_request = await mario_api.get("/api/auth/me")
    assert next_request.status_code == 401
    assert error_code(next_request) == "not_authenticated"
    async with client() as api:
        new_login = await login(api, "mario")
    assert new_login.status_code == 401
    assert error_code(new_login) == "invalid_credentials"

    reactivated = await admin.patch(f"{USERS}/{user_id}", json={"is_active": True})

    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    async with client() as api:
        assert (await login(api, "mario")).status_code == 200
        assert (await api.get("/api/auth/me")).status_code == 200


async def test_AC_017_deactivation_ends_every_session_of_the_user(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, first = mario
    async with client() as second:
        assert (await login(second, "mario")).status_code == 200

        assert (await admin.patch(f"{USERS}/{user_id}", json={"is_active": False})).status_code == 200

        assert (await first.get("/api/auth/me")).status_code == 401
        assert (await second.get("/api/auth/me")).status_code == 401


async def test_AC_017_reactivation_does_not_bring_back_the_old_session(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, mario_api = mario
    old_cookie = mario_api.cookies[COOKIE]

    assert (await admin.patch(f"{USERS}/{user_id}", json={"is_active": False})).status_code == 200
    assert (await admin.patch(f"{USERS}/{user_id}", json={"is_active": True})).status_code == 200

    async with client() as api:
        replay = await api.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={old_cookie}"})
    assert replay.status_code == 401
    assert (await row_of(engine, "mario"))["is_active"] is True


async def test_AC_017_deactivation_does_not_touch_other_users(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    luigi = await add_user(engine, "luigi")
    async with client() as luigi_api:
        assert (await login(luigi_api, "luigi")).status_code == 200

        assert (await admin.patch(f"{USERS}/{luigi}", json={"is_active": False})).status_code == 200

        assert (await mario[1].get("/api/auth/me")).status_code == 200
        assert (await admin.get("/api/auth/me")).status_code == 200
    assert (await row_of(engine, "mario"))["is_active"] is True


async def test_AC_017_the_admin_can_deactivate_another_admin(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    other = await add_user(engine, "bruno", role="admin")

    response = await admin.patch(f"{USERS}/{other}", json={"is_active": False})

    assert response.status_code == 200
    assert (await row_of(engine, "bruno"))["is_active"] is False


async def test_AC_017_deactivating_an_unknown_user_gets_404(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await admin.patch(f"{USERS}/{uuid.uuid4()}", json={"is_active": False})

    assert response.status_code == 404
    assert error_code(response) == "user_not_found"


async def test_AC_017_patch_without_is_active_gets_422_and_changes_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, mario_api = mario

    response = await admin.patch(f"{USERS}/{user_id}", json={"role": "admin"})

    assert response.status_code == 422
    row = await row_of(engine, "mario")
    assert (row["role"], row["is_active"]) == ("user", True)
    assert (await mario_api.get("/api/auth/me")).status_code == 200


# AC-018 ---------------------------------------------------------------------------------------------------------------


async def test_AC_018_reset_ends_the_old_session_and_only_the_new_password_works(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, mario_api = mario

    reset = await admin.put(f"{USERS}/{user_id}/password", json={"password": NEW_PASSWORD})

    assert reset.status_code == 204
    assert reset.content == b""
    old_session = await mario_api.get("/api/auth/me")
    assert old_session.status_code == 401
    assert error_code(old_session) == "not_authenticated"
    async with client() as api:
        old_password = await login(api, "mario", PASSWORD)
        new_password = await login(api, "mario", NEW_PASSWORD)
    assert old_password.status_code == 401
    assert new_password.status_code == 200


async def test_AC_018_reset_stores_a_new_argon2id_hash(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, _ = mario

    assert (await admin.put(f"{USERS}/{user_id}/password", json={"password": NEW_PASSWORD})).status_code == 204

    stored = (await row_of(engine, "mario"))["password_hash"]
    assert stored.startswith("$argon2id$")
    assert verify_password(NEW_PASSWORD, stored)
    assert not verify_password(PASSWORD, stored)


async def test_AC_018_reset_does_not_end_the_sessions_of_other_users(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    luigi = await add_user(engine, "luigi")

    assert (await admin.put(f"{USERS}/{luigi}/password", json={"password": NEW_PASSWORD})).status_code == 204

    assert (await mario[1].get("/api/auth/me")).status_code == 200
    assert (await admin.get("/api/auth/me")).status_code == 200


async def test_AC_018_reset_of_an_unknown_user_gets_404(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await admin.put(f"{USERS}/{uuid.uuid4()}/password", json={"password": NEW_PASSWORD})

    assert response.status_code == 404
    assert error_code(response) == "user_not_found"


# AC-019 ---------------------------------------------------------------------------------------------------------------


async def test_AC_019_the_admin_cannot_deactivate_themself(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    me = (await admin.get("/api/auth/me")).json()

    response = await admin.patch(f"{USERS}/{me['id']}", json={"is_active": False})

    assert response.status_code == 409
    assert error_code(response) == "cannot_deactivate_self"
    row = await row_of(engine, ADMIN)
    assert row["is_active"] is True
    assert row["session_epoch"] == 0
    still_signed_in = await admin.get("/api/auth/me")
    assert still_signed_in.status_code == 200
    assert still_signed_in.json()["is_active"] is True


async def test_AC_019_reactivating_themself_is_harmless(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    me = (await admin.get("/api/auth/me")).json()

    response = await admin.patch(f"{USERS}/{me['id']}", json={"is_active": True})

    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert (await admin.get("/api/auth/me")).status_code == 200


# AC-025 ---------------------------------------------------------------------------------------------------------------


async def test_AC_025_a_user_gets_403_on_every_users_admin_api_and_nothing_changes(
    engine: AsyncEngine, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    user_id, mario_api = mario
    luigi = await add_user(engine, "luigi")
    before = await rows(engine)

    responses = [
        await mario_api.get(USERS),
        await create(mario_api, "peach"),
        await mario_api.patch(f"{USERS}/{luigi}", json={"is_active": False}),
        await mario_api.patch(f"{USERS}/{user_id}", json={"is_active": False}),
        await mario_api.put(f"{USERS}/{luigi}/password", json={"password": NEW_PASSWORD}),
        await mario_api.put(f"{USERS}/{user_id}/password", json={"password": NEW_PASSWORD}),
    ]

    for response in responses:
        assert response.status_code == 403
        assert error_code(response) == "forbidden"
    assert await rows(engine) == before
    assert (await mario_api.get("/api/auth/me")).status_code == 200


async def test_AC_025_a_user_gets_403_also_with_an_invalid_body(
    engine: AsyncEngine, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    """The role is checked before the input: a user learns nothing about the admin API from validation errors."""
    _, mario_api = mario

    response = await mario_api.post(USERS, json={"username": "x"})

    assert response.status_code == 403
    assert error_code(response) == "forbidden"


async def test_AC_025_without_a_session_the_users_admin_api_gets_401(engine: AsyncEngine) -> None:
    luigi = await add_user(engine, "luigi")
    async with client() as api:
        responses = [
            await api.get(USERS),
            await create(api, "peach"),
            await api.patch(f"{USERS}/{luigi}", json={"is_active": False}),
            await api.put(f"{USERS}/{luigi}/password", json={"password": NEW_PASSWORD}),
        ]

    for response in responses:
        assert response.status_code == 401
        assert error_code(response) == "not_authenticated"
    assert [row["username"] for row in await rows(engine)] == ["luigi"]


async def test_AC_025_the_users_admin_api_accepts_only_json(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await admin.post(USERS, data={"username": "mario", "password": PASSWORD, "role": "user"})

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert await count_named(engine, "mario") == 0


# AC-010 (deferred from the T-03 review): the session cookie ends with the browser -----------------------------------


async def test_AC_010_session_cookie_has_no_max_age_or_expires(engine: AsyncEngine) -> None:
    await add_user(engine, "mario")
    async with client() as api:
        response = await login(api, "mario")

    assert response.status_code == 200
    headers = [h for h in response.headers.get_list("set-cookie") if h.split("=", 1)[0].strip() == COOKIE]
    assert len(headers) == 1
    attributes = {part.strip().partition("=")[0].lower() for part in headers[0].split(";")[1:]}
    assert "max-age" not in attributes
    assert "expires" not in attributes
