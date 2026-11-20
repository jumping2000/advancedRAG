# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The switches of the two steps (T-01): AC-001 (`rewrite.configured`), AC-006 and AC-007 (REQ-004), the migration 0006,
`AppSettingsRepository` and `active_steps` (REQ-003: a step is active only when it is switched on and configured).

Fresh state per test: no users, no sessions, both switches off.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.app_settings.repository import AppSettingsRepository, Switches, active_steps
from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User
from app.main import create_app
from app.run import BACKEND_DIR, migrate
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

ADMIN = "anna"
USER = "mario"
PASSWORD = "correct-horse-battery"
SETTINGS = "/api/admin/settings"
REWRITE = {"rewrite_base_url": "http://rewrite.example.internal:8000/v1", "rewrite_model": "rewrite-model"}
RERANK = {"rerank_base_url": "http://reranker.example.internal:8080"}
OFF = {"enabled": False, "configured": False}


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table users cascade"))
        await connection.execute(text("update app_settings set rerank_enabled = false, rewrite_enabled = false"))
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("update app_settings set rerank_enabled = false, rewrite_enabled = false"))
    await engine.dispose()


async def add_user(engine: AsyncEngine, username: str, role: str) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(User(username=username, password_hash=hash_password(PASSWORD), role=role))
        await session.commit()


def client(settings: Settings | None = None) -> httpx.AsyncClient:
    app = create_app(settings or make_settings())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@asynccontextmanager
async def signed_in(
    engine: AsyncEngine, username: str, role: str, settings: Settings | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """A new user with this role and a client signed in as that user."""
    await add_user(engine, username, role)
    async with client(settings) as api:
        login = await api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200
        yield api


async def stored(engine: AsyncEngine) -> list[tuple[int, bool, bool]]:
    """Every row of app_settings: (id, rerank_enabled, rewrite_enabled)."""
    async with engine.connect() as connection:
        result = await connection.execute(text("select id, rerank_enabled, rewrite_enabled from app_settings"))
        return [(row.id, row.rerank_enabled, row.rewrite_enabled) for row in result]


async def store(engine: AsyncEngine, rerank: bool, rewrite: bool) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("update app_settings set rerank_enabled = :rerank, rewrite_enabled = :rewrite"),
            {"rerank": rerank, "rewrite": rewrite},
        )


def error_code(response: httpx.Response) -> str:
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["message"]
    code: str = body["error"]["code"]
    return code


# GET and PUT by the admin --------------------------------------------------------------------------------------------


async def test_a_new_database_has_both_switches_off_and_the_admin_reads_them(engine: AsyncEngine) -> None:
    async with signed_in(engine, ADMIN, "admin") as admin:
        response = await admin.get(SETTINGS)

    assert response.status_code == 200
    assert response.json() == {"rerank": OFF, "rewrite": OFF}
    assert await stored(engine) == [(1, False, False)]


async def test_AC_001_without_a_rewrite_base_url_rewrite_configured_is_false(engine: AsyncEngine) -> None:
    async with signed_in(engine, ADMIN, "admin", make_settings(rewrite_model="unused-without-a-url")) as admin:
        response = await admin.get(SETTINGS)

    assert response.status_code == 200
    assert response.json()["rewrite"] == {"enabled": False, "configured": False}


async def test_configured_reflects_the_settings_of_each_service(engine: AsyncEngine) -> None:
    async with signed_in(engine, ADMIN, "admin", make_settings(**REWRITE)) as admin:
        only_rewrite = (await admin.get(SETTINGS)).json()
    async with client(make_settings(**RERANK)) as other:
        login = await other.post("/api/auth/login", json={"username": ADMIN, "password": PASSWORD})
        assert login.status_code == 200
        only_rerank = (await other.get(SETTINGS)).json()

    assert only_rewrite == {"rerank": OFF, "rewrite": {"enabled": False, "configured": True}}
    assert only_rerank == {"rerank": {"enabled": False, "configured": True}, "rewrite": OFF}


async def test_the_admin_switches_a_configured_step_on_and_off_and_the_value_is_saved(engine: AsyncEngine) -> None:
    settings = make_settings(**REWRITE, **RERANK)
    async with signed_in(engine, ADMIN, "admin", settings) as admin:
        on = await admin.put(SETTINGS, json={"rewrite_enabled": True})
        assert on.status_code == 200
        assert on.json() == {
            "rerank": {"enabled": False, "configured": True},
            "rewrite": {"enabled": True, "configured": True},
        }
        assert await stored(engine) == [(1, False, True)]
        assert (await admin.get(SETTINGS)).json() == on.json()

        both = await admin.put(SETTINGS, json={"rerank_enabled": True})  # the other field is kept
        assert both.status_code == 200
        assert both.json() == {
            "rerank": {"enabled": True, "configured": True},
            "rewrite": {"enabled": True, "configured": True},
        }
        assert await stored(engine) == [(1, True, True)]

        off = await admin.put(SETTINGS, json={"rerank_enabled": False, "rewrite_enabled": False})
        assert off.status_code == 200
        assert off.json() == {
            "rerank": {"enabled": False, "configured": True},
            "rewrite": {"enabled": False, "configured": True},
        }
    assert await stored(engine) == [(1, False, False)]


async def test_the_saved_value_is_read_by_another_application_without_a_restart(engine: AsyncEngine) -> None:
    settings = make_settings(**REWRITE)
    async with signed_in(engine, ADMIN, "admin", settings) as admin:
        assert (await admin.put(SETTINGS, json={"rewrite_enabled": True})).status_code == 200
    async with client(settings) as other:
        assert (await other.post("/api/auth/login", json={"username": ADMIN, "password": PASSWORD})).status_code == 200
        response = await other.get(SETTINGS)

    assert response.json()["rewrite"] == {"enabled": True, "configured": True}


async def test_a_step_that_is_not_configured_can_be_switched_off(engine: AsyncEngine) -> None:
    """A switch left on while its service was removed from .env (the case of AC-004) is switched off without a 409."""
    await store(engine, rerank=True, rewrite=True)
    async with signed_in(engine, ADMIN, "admin") as admin:
        before = (await admin.get(SETTINGS)).json()
        response = await admin.put(SETTINGS, json={"rerank_enabled": False})

    assert before == {
        "rerank": {"enabled": True, "configured": False},
        "rewrite": {"enabled": True, "configured": False},
    }
    assert response.status_code == 200
    assert response.json() == {"rerank": OFF, "rewrite": {"enabled": True, "configured": False}}
    assert await stored(engine) == [(1, False, True)]


# AC-006: 409 service_not_configured and 422 validation_error ----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "body"),
    [
        pytest.param({}, {"rerank_enabled": True}, id="rerank-alone"),
        pytest.param({}, {"rewrite_enabled": True}, id="rewrite-alone"),
        pytest.param(REWRITE, {"rerank_enabled": True}, id="rerank-with-rewrite-configured"),
        pytest.param(REWRITE, {"rerank_enabled": True, "rewrite_enabled": True}, id="other-field-valid"),
        pytest.param(RERANK, {"rerank_enabled": True, "rewrite_enabled": True}, id="other-field-valid-rerank"),
        pytest.param({}, {"rerank_enabled": True, "rewrite_enabled": False}, id="with-a-valid-off"),
    ],
)
async def test_AC_006_switching_on_a_step_that_is_not_configured_is_409_and_nothing_is_saved(
    engine: AsyncEngine, overrides: dict[str, str], body: dict[str, bool]
) -> None:
    async with signed_in(engine, ADMIN, "admin", make_settings(**overrides)) as admin:
        response = await admin.put(SETTINGS, json=body)
        after = (await admin.get(SETTINGS)).json()

    assert response.status_code == 409
    assert error_code(response) == "service_not_configured"
    assert after["rerank"]["enabled"] is False
    assert after["rewrite"]["enabled"] is False
    assert await stored(engine) == [(1, False, False)]


async def test_AC_006_a_409_keeps_a_switch_that_was_on(engine: AsyncEngine) -> None:
    await store(engine, rerank=False, rewrite=True)
    async with signed_in(engine, ADMIN, "admin", make_settings(**REWRITE)) as admin:
        response = await admin.put(SETTINGS, json={"rerank_enabled": True, "rewrite_enabled": False})

    assert response.status_code == 409
    assert error_code(response) == "service_not_configured"
    assert await stored(engine) == [(1, False, True)]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="empty"),
        pytest.param({"rewrite_enabled": "yes"}, id="string-yes"),
        pytest.param({"rewrite_enabled": "true"}, id="string-true"),
        pytest.param({"rerank_enabled": 1}, id="integer-1"),
        pytest.param({"rerank_enabled": 0}, id="integer-0"),
        pytest.param({"rerank_enabled": None}, id="null"),
        pytest.param({"rerank_enabled": True, "rewrite_enabled": None}, id="one-null"),
        pytest.param({"rerank_enabled": [True]}, id="list"),
        pytest.param({"rerank_enabled": False, "rewrite_enabled": "no"}, id="one-valid-one-string"),
        pytest.param({"unknown": True}, id="unknown-field-only"),
        pytest.param([], id="array"),
        pytest.param(True, id="bare-boolean"),
        pytest.param("rerank_enabled", id="bare-string"),
    ],
)
async def test_AC_006_a_body_without_the_fields_or_with_a_value_that_is_not_a_boolean_is_422(
    engine: AsyncEngine, body: Any
) -> None:
    await store(engine, rerank=True, rewrite=False)
    async with signed_in(engine, ADMIN, "admin", make_settings(**REWRITE, **RERANK)) as admin:
        response = await admin.put(SETTINGS, json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert await stored(engine) == [(1, True, False)]


async def test_AC_006_a_put_without_a_body_or_with_broken_json_is_422(engine: AsyncEngine) -> None:
    async with signed_in(engine, ADMIN, "admin", make_settings(**REWRITE, **RERANK)) as admin:
        no_body = await admin.put(SETTINGS)
        broken = await admin.put(
            SETTINGS, content=b'{"rerank_enabled": tru', headers={"Content-Type": "application/json"}
        )

    assert (no_body.status_code, error_code(no_body)) == (422, "validation_error")
    assert (broken.status_code, error_code(broken)) == (422, "validation_error")
    assert await stored(engine) == [(1, False, False)]


# AC-007: 401 without a session, 403 forbidden for a user, before the body ---------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"rerank_enabled": False}, id="valid"),
        pytest.param({"rerank_enabled": True}, id="not-configured"),
        pytest.param({}, id="empty"),
        pytest.param({"rewrite_enabled": "yes"}, id="not-a-boolean"),
    ],
)
async def test_AC_007_a_user_gets_403_forbidden_on_get_and_put_and_nothing_changes(
    engine: AsyncEngine, body: dict[str, Any]
) -> None:
    await store(engine, rerank=True, rewrite=True)
    async with signed_in(engine, USER, "user", make_settings(**REWRITE, **RERANK)) as user:
        read = await user.get(SETTINGS)
        write = await user.put(SETTINGS, json=body)

    assert read.status_code == 403
    assert error_code(read) == "forbidden"
    assert write.status_code == 403
    assert error_code(write) == "forbidden"
    assert await stored(engine) == [(1, True, True)]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"rerank_enabled": False}, id="valid"),
        pytest.param({}, id="empty"),
        pytest.param({"rewrite_enabled": "yes"}, id="not-a-boolean"),
    ],
)
async def test_AC_007_without_a_session_get_and_put_are_401_and_nothing_changes(
    engine: AsyncEngine, body: dict[str, Any]
) -> None:
    await store(engine, rerank=True, rewrite=True)
    async with client(make_settings(**REWRITE, **RERANK)) as anonymous:
        read = await anonymous.get(SETTINGS)
        write = await anonymous.put(SETTINGS, json=body)
        no_body = await anonymous.put(SETTINGS)

    for response in (read, write, no_body):
        assert response.status_code == 401
        assert error_code(response) == "not_authenticated"
    assert await stored(engine) == [(1, True, True)]


async def test_AC_007_a_session_that_is_not_valid_is_401(engine: AsyncEngine) -> None:
    async with client() as api:
        api.cookies.set("rag_session", "not-a-session")
        response = await api.get(SETTINGS)

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


# AppSettingsRepository and active_steps -----------------------------------------------------------------------------


async def test_the_repository_reads_and_updates_the_switches(engine: AsyncEngine) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        repository = AppSettingsRepository(session)
        assert await repository.switches() == Switches(rerank_enabled=False, rewrite_enabled=False)
        assert await repository.update(rerank_enabled=True) == Switches(rerank_enabled=True, rewrite_enabled=False)
        assert await repository.update(rewrite_enabled=True) == Switches(rerank_enabled=True, rewrite_enabled=True)
        assert await repository.update(rerank_enabled=False, rewrite_enabled=None) == Switches(False, True)
        assert await repository.update() == Switches(False, True)  # nothing to change

    async with sessionmaker() as other:  # committed: another session reads it
        assert await AppSettingsRepository(other).switches() == Switches(rerank_enabled=False, rewrite_enabled=True)
    assert await stored(engine) == [(1, False, True)]


def test_switches_are_immutable() -> None:
    switches = Switches(rerank_enabled=True, rewrite_enabled=False)
    with pytest.raises(AttributeError):
        switches.rerank_enabled = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "switches", "expected"),
    [
        pytest.param({}, Switches(False, False), (False, False), id="off-not-configured"),
        pytest.param({}, Switches(True, True), (False, False), id="on-not-configured"),
        pytest.param({**REWRITE, **RERANK}, Switches(False, False), (False, False), id="off-configured"),
        pytest.param({**REWRITE, **RERANK}, Switches(True, True), (True, True), id="on-configured"),
        pytest.param({**REWRITE, **RERANK}, Switches(True, False), (True, False), id="rerank-only-on"),
        pytest.param({**REWRITE, **RERANK}, Switches(False, True), (False, True), id="rewrite-only-on"),
        pytest.param(RERANK, Switches(True, True), (True, False), id="rewrite-not-configured"),
        pytest.param(REWRITE, Switches(True, True), (False, True), id="rerank-not-configured"),
    ],
)
def test_a_step_is_active_only_when_switched_on_and_configured(
    overrides: dict[str, str], switches: Switches, expected: tuple[bool, bool]
) -> None:
    assert active_steps(make_settings(**overrides), switches) == expected


# Migration 0006 -----------------------------------------------------------------------------------------------------


def alembic_config(database_url: Any) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = database_url
    return config


async def scratch_database(action: str, name: str) -> None:
    engine = create_async_engine(make_url(TEST_DATABASE_URL), isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        if action == "create":
            await connection.execute(text(f'create database "{name}"'))
        else:
            await connection.execute(text(f'drop database if exists "{name}" with (force)'))
    await engine.dispose()


async def add_message(url: Any) -> uuid.UUID:
    """A conversation with one question, written before the migration 0006."""
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        user_id = await connection.scalar(
            text("insert into users (username, password_hash, role) values ('old', 'x', 'user') returning id")
        )
        kb_id = await connection.scalar(text("insert into knowledge_bases (name) values ('Old KB') returning id"))
        conversation_id = await connection.scalar(
            text("insert into conversations (kb_id, user_id, title) values (:kb, :user, 'Old') returning id"),
            {"kb": kb_id, "user": user_id},
        )
        message_id: uuid.UUID = await connection.scalar(
            text(
                "insert into messages (conversation_id, role, content, status)"
                " values (:conversation, 'assistant', 'Old answer', 'COMPLETED') returning id"
            ),
            {"conversation": conversation_id},
        )
    await engine.dispose()
    return message_id


async def schema_state(url: Any) -> dict[str, Any]:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        tables = set(
            (await connection.scalars(text("select tablename from pg_tables where schemaname = 'public'"))).all()
        )
        column = (
            await connection.execute(
                text(
                    "select data_type, is_nullable from information_schema.columns"
                    " where table_name = 'messages' and column_name = 'search_query'"
                )
            )
        ).all()
        rows: list[tuple[Any, ...]] = []
        columns: list[tuple[Any, ...]] = []
        if "app_settings" in tables:
            rows = [
                tuple(row)
                for row in await connection.execute(
                    text("select id, rerank_enabled, rewrite_enabled, updated_at is not null from app_settings")
                )
            ]
            columns = [
                tuple(row)
                for row in await connection.execute(
                    text(
                        "select column_name, data_type, is_nullable, column_default is not null"
                        " from information_schema.columns where table_name = 'app_settings' order by column_name"
                    )
                )
            ]
        version = await connection.scalar(text("select version_num from alembic_version"))
    await engine.dispose()
    return {"tables": tables, "column": column, "rows": rows, "columns": columns, "version": version}


async def search_queries(url: Any) -> list[Any]:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        values = list((await connection.scalars(text("select search_query from messages"))).all())
    await engine.dispose()
    return values


async def insert_second_row(url: Any) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("insert into app_settings (id) values (2)"))
    finally:
        await engine.dispose()


def test_migration_0006_upgrades_downgrades_and_upgrades_again_keeping_the_messages() -> None:
    name = f"rag_migration_{uuid.uuid4().hex[:8]}"
    url = make_url(TEST_DATABASE_URL).set(database=name)
    config = alembic_config(url)
    asyncio.run(scratch_database("create", name))
    try:
        command.upgrade(config, "0005_conversations")
        asyncio.run(add_message(url))
        command.upgrade(config, "head")
        first = asyncio.run(schema_state(url))
        first_queries = asyncio.run(search_queries(url))
        with pytest.raises(IntegrityError):  # one row only: CHECK (id = 1)
            asyncio.run(insert_second_row(url))
        command.downgrade(config, "0005_conversations")
        down = asyncio.run(schema_state(url))
        command.upgrade(config, "head")
        second = asyncio.run(schema_state(url))
    finally:
        asyncio.run(scratch_database("drop", name))

    assert first["version"] == "0006_rerank_rewrite"
    assert first["rows"] == [(1, False, False, True)]
    assert first["columns"] == [
        ("id", "smallint", "NO", False),
        ("rerank_enabled", "boolean", "NO", True),
        ("rewrite_enabled", "boolean", "NO", True),
        ("updated_at", "timestamp with time zone", "NO", True),
    ]
    assert first["column"] == [("text", "YES")]
    assert first_queries == [None]  # the answers saved before this phase are not changed

    assert down["version"] == "0005_conversations"
    assert "app_settings" not in down["tables"]
    assert "messages" in down["tables"]
    assert down["column"] == []

    assert second == first
