# SPEC-2026-11-06-fase-1-fondazioni
"""Isolation by KB: AC-023, AC-024 (REQ-010), through the API and through KnowledgeBaseRepository called directly.

The filter on kb_members is in the SQL query (plan): the repository returns nothing for a non-member even when called
without the API, and a read for a user is one statement that joins kb_members. A KB that is not assigned, an unknown
id and an id that is not a UUID get the same 404 kb_not_found (review focus), on every KB endpoint that takes a kb_id.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import User
from app.knowledge_bases.repository import KnowledgeBaseRepository
from tests.test_knowledge_bases import (
    ADMIN,
    ADMIN_PASSWORD,
    KB_OUT_KEYS,
    KBS,
    add_kb,
    add_user,
    assign,
    client,
    error_code,
    kb_rows,
    listed_names,
    login,
    member_rows,
    signed_in,
)
from tests.test_knowledge_bases import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_knowledge_bases import schema as schema  # noqa: F401

INVALID_IDS = ["not-a-uuid", "123", "8a1c7f9e-0000-4000-8000-00000000000z", "%27%20or%201%3D1%20--"]


class World:
    """mario is assigned to Manuali (A) and not to Procedure (B); luigi only to B; peach to none; anna is the admin."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.a = uuid.UUID(int=0)
        self.b = uuid.UUID(int=0)
        self.mario = uuid.UUID(int=0)
        self.luigi = uuid.UUID(int=0)
        self.peach = uuid.UUID(int=0)
        self.anna = uuid.UUID(int=0)

    async def build(self) -> World:
        self.a = await add_kb(self.engine, "Manuali", "Manuali tecnici")
        self.b = await add_kb(self.engine, "Procedure", "Procedure riservate")
        self.anna = await add_user(self.engine, ADMIN, ADMIN_PASSWORD, role="admin")
        self.mario = await add_user(self.engine, "mario")
        self.luigi = await add_user(self.engine, "luigi")
        self.peach = await add_user(self.engine, "peach")
        await assign(self.engine, self.a, self.mario)
        await assign(self.engine, self.b, self.luigi)
        return self


@pytest.fixture
async def world(engine: AsyncEngine) -> World:  # noqa: F811
    return await World(engine).build()


@pytest.fixture
async def mario_api(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with client() as api:
        assert (await login(api, "mario")).status_code == 200
        yield api


@pytest.fixture
async def admin_api(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with client() as api:
        assert (await login(api, ADMIN, ADMIN_PASSWORD)).status_code == 200
        yield api


@pytest.fixture
async def session(world: World) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        yield session


async def user_named(session: AsyncSession, username: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    assert user is not None
    return user


def values(parameters: Any) -> set[str]:
    return {str(value) for value in tuple(parameters)}


class Statements:
    """The SQL statements an engine sends while the block runs."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine.sync_engine
        self.sent: list[tuple[str, Any]] = []

    def record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool) -> None:
        self.sent.append((statement, parameters))

    def __enter__(self) -> Statements:
        event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self.record)


@pytest.fixture
def statements(world: World) -> Iterator[Statements]:
    yield Statements(world.engine)


# AC-023: a user lists only the KBs assigned to them -------------------------------------------------------------------


async def test_AC_023_a_user_assigned_to_A_and_not_to_B_lists_only_A(
    world: World, mario_api: httpx.AsyncClient
) -> None:
    response = await mario_api.get(KBS)

    assert response.status_code == 200
    kbs = response.json()
    assert all(set(kb) == KB_OUT_KEYS for kb in kbs)
    assert [(kb["id"], kb["name"], kb["description"]) for kb in kbs] == [(str(world.a), "Manuali", "Manuali tecnici")]
    assert "Procedure" not in response.text
    assert str(world.b) not in response.text


async def test_AC_023_every_user_lists_only_their_own_kbs_and_the_admin_lists_all(
    world: World, admin_api: httpx.AsyncClient
) -> None:
    async with client() as luigi, client() as peach:
        assert (await login(luigi, "luigi")).status_code == 200
        assert (await login(peach, "peach")).status_code == 200

        assert listed_names(await luigi.get(KBS)) == ["Procedure"]
        assert listed_names(await peach.get(KBS)) == []
    assert listed_names(await admin_api.get(KBS)) == ["Manuali", "Procedure"]


async def test_AC_023_a_user_assigned_to_both_lists_both(world: World, mario_api: httpx.AsyncClient) -> None:
    await assign(world.engine, world.b, world.mario)

    assert listed_names(await mario_api.get(KBS)) == ["Manuali", "Procedure"]


async def test_AC_023_the_repository_lists_only_the_kbs_of_a_user_when_called_directly(
    world: World, session: AsyncSession
) -> None:
    repository = KnowledgeBaseRepository(session)

    assert [kb.id for kb in await repository.list_for(await user_named(session, "mario"))] == [world.a]
    assert [kb.id for kb in await repository.list_for(await user_named(session, "luigi"))] == [world.b]
    assert await repository.list_for(await user_named(session, "peach")) == []
    assert [kb.id for kb in await repository.list_for(await user_named(session, ADMIN))] == [world.a, world.b]


async def test_AC_023_the_repository_does_not_return_a_kb_of_others_when_called_directly(
    world: World, session: AsyncSession
) -> None:
    repository = KnowledgeBaseRepository(session)
    mario = await user_named(session, "mario")
    peach = await user_named(session, "peach")
    anna = await user_named(session, ADMIN)

    assert await repository.get_for(mario, world.b) is None
    assert await repository.get_for(peach, world.a) is None
    assert await repository.get_for(mario, uuid.uuid4()) is None
    found = await repository.get_for(mario, world.a)
    assert found is not None
    assert (found.id, found.name) == (world.a, "Manuali")
    admin_found = await repository.get_for(anna, world.b)
    assert admin_found is not None
    assert admin_found.name == "Procedure"


async def test_AC_023_the_repository_filters_in_one_sql_query_joined_with_kb_members(
    world: World, session: AsyncSession, statements: Statements
) -> None:
    """The user filter is part of the query that reads knowledge_bases, not a check made afterwards in Python."""
    repository = KnowledgeBaseRepository(session)
    mario = await user_named(session, "mario")

    with statements:
        listed = await repository.list_for(mario)
    assert [kb.id for kb in listed] == [world.a]
    assert len(statements.sent) == 1
    sql, parameters = statements.sent[0]
    assert "knowledge_bases" in sql
    assert "kb_members" in sql
    assert str(world.mario) in values(parameters)

    statements.sent.clear()
    with statements:
        assert await repository.get_for(mario, world.b) is None
    assert len(statements.sent) == 1
    sql, parameters = statements.sent[0]
    assert "knowledge_bases" in sql
    assert "kb_members" in sql
    assert str(world.mario) in values(parameters)
    assert str(world.b) in values(parameters)


async def test_AC_023_a_role_other_than_admin_is_filtered_like_a_user(world: World, session: AsyncSession) -> None:
    """Fail closed: only the admin role reads every KB, whatever else the role holds."""
    repository = KnowledgeBaseRepository(session)
    stranger = User(id=uuid.uuid4(), username="stranger", password_hash="x", role="ADMIN", is_active=True)
    # Objects outside the session: the database itself refuses such roles (users_role_check).
    mario = User(id=world.mario, username="mario", password_hash="x", role="Admin", is_active=True)

    assert await repository.list_for(stranger) == []
    assert await repository.get_for(stranger, world.a) is None
    assert [kb.id for kb in await repository.list_for(mario)] == [world.a]
    assert await repository.get_for(mario, world.b) is None


async def test_AC_023_a_user_removed_from_a_kb_no_longer_lists_it(
    world: World, admin_api: httpx.AsyncClient, mario_api: httpx.AsyncClient
) -> None:
    assert (await admin_api.delete(f"{KBS}/{world.a}/members/{world.mario}")).status_code == 204

    assert listed_names(await mario_api.get(KBS)) == []
    assert await member_rows(world.engine) == [(world.b, world.luigi)]


# AC-024: a KB not assigned and a KB that does not exist get the same 404 ----------------------------------------------


async def test_AC_024_kb_B_and_an_unknown_kb_get_the_same_404(world: World, mario_api: httpx.AsyncClient) -> None:
    not_assigned = await mario_api.get(f"{KBS}/{world.b}")
    unknown = await mario_api.get(f"{KBS}/{uuid.uuid4()}")

    assert not_assigned.status_code == 404
    assert unknown.status_code == 404
    assert error_code(not_assigned) == "kb_not_found"
    assert not_assigned.content == unknown.content
    assert not_assigned.headers["content-type"] == unknown.headers["content-type"]
    assert "Procedure" not in not_assigned.text


async def test_AC_024_the_assigned_kb_opens_with_its_data(world: World, mario_api: httpx.AsyncClient) -> None:
    response = await mario_api.get(f"{KBS}/{world.a}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == KB_OUT_KEYS
    assert (body["id"], body["name"], body["description"]) == (str(world.a), "Manuali", "Manuali tecnici")
    rows = {row["id"]: row for row in await kb_rows(world.engine)}
    assert datetime.fromisoformat(body["created_at"]) == rows[world.a]["created_at"]


@pytest.mark.parametrize("bad_id", INVALID_IDS)
async def test_AC_024_a_kb_id_that_is_not_a_uuid_gets_the_same_404_as_an_unknown_kb(
    world: World, mario_api: httpx.AsyncClient, bad_id: str
) -> None:
    unknown = await mario_api.get(f"{KBS}/{uuid.uuid4()}")
    malformed = await mario_api.get(f"{KBS}/{bad_id}")

    assert malformed.status_code == 404
    assert malformed.content == unknown.content
    assert error_code(malformed) == "kb_not_found"


async def test_AC_024_a_user_with_no_kb_gets_the_same_404_for_every_kb(world: World) -> None:
    async with client() as peach:
        assert (await login(peach, "peach")).status_code == 200
        responses = [await peach.get(f"{KBS}/{kb_id}") for kb_id in (world.a, world.b, uuid.uuid4(), "not-a-uuid")]

    assert [response.status_code for response in responses] == [404] * 4
    assert len({response.content for response in responses}) == 1


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123"])
async def test_AC_024_every_admin_endpoint_with_a_kb_id_answers_404_kb_not_found_for_unknown_and_invalid_ids(
    world: World, admin_api: httpx.AsyncClient, bad_id: str
) -> None:
    endpoints: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "{kb}", None),
        ("PATCH", "{kb}", {"name": "Nuovo nome"}),
        ("DELETE", "{kb}", None),
        ("GET", "{kb}/members", None),
        ("PUT", "{kb}/members/" + str(world.mario), None),
        ("DELETE", "{kb}/members/" + str(world.luigi), None),
    ]
    before_kbs, before_members = await kb_rows(world.engine), await member_rows(world.engine)

    for method, template, body in endpoints:
        unknown = await admin_api.request(method, f"{KBS}/" + template.format(kb=uuid.uuid4()), json=body)
        malformed = await admin_api.request(method, f"{KBS}/" + template.format(kb=bad_id), json=body)
        assert unknown.status_code == 404, (method, template)
        assert error_code(unknown) == "kb_not_found", (method, template)
        assert malformed.content == unknown.content, (method, template)

    assert await kb_rows(world.engine) == before_kbs
    assert await member_rows(world.engine) == before_members


async def test_AC_024_a_user_removed_from_a_kb_gets_the_same_404_as_for_an_unknown_kb(
    world: World, admin_api: httpx.AsyncClient, mario_api: httpx.AsyncClient
) -> None:
    assert (await mario_api.get(f"{KBS}/{world.a}")).status_code == 200
    assert (await admin_api.delete(f"{KBS}/{world.a}/members/{world.mario}")).status_code == 204

    removed = await mario_api.get(f"{KBS}/{world.a}")
    unknown = await mario_api.get(f"{KBS}/{uuid.uuid4()}")

    assert removed.status_code == 404
    assert removed.content == unknown.content


async def test_AC_024_a_user_signed_in_before_the_assignment_sees_the_kb_at_once(world: World) -> None:
    """The filter is read at every request, not stored in the session."""
    async with client() as peach:
        assert (await login(peach, "peach")).status_code == 200
        assert (await peach.get(f"{KBS}/{world.b}")).status_code == 404

        await assign(world.engine, world.b, world.peach)

        assert (await peach.get(f"{KBS}/{world.b}")).status_code == 200
        assert listed_names(await peach.get(KBS)) == ["Procedure"]


async def test_AC_024_a_new_user_sees_nothing_until_assigned(world: World) -> None:
    async with signed_in(world.engine, "toad") as (user_id, api):
        assert listed_names(await api.get(KBS)) == []
        assert (await api.get(f"{KBS}/{world.a}")).status_code == 404
    assert user_id not in {user for _, user in await member_rows(world.engine)}
