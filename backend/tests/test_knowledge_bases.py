# SPEC-2026-11-06-fase-1-fondazioni
"""Knowledge base management by the admin: AC-020, AC-021, AC-022, AC-025 (REQ-008, REQ-009, REQ-010).

Review focus of T-06: names with upper case, leading or trailing spaces and Unicode characters (spaces are removed
before the uniqueness check, the comparison is on lower()); concurrent creation or renaming to the same name gets 409,
not 500; a kb_id that is not a UUID gets the same 404 kb_not_found as an unknown KB (also in test_isolation.py).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password
from app.db.models import User
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_REDIS_URL, make_settings

ADMIN = "anna"
ADMIN_PASSWORD = "admin-password-123"
PASSWORD = "correct-horse-battery"
KBS = "/api/kbs"
KB_OUT_KEYS = {"id", "name", "description", "created_at"}
USER_OUT_KEYS = {"id", "username", "role", "is_active", "language"}


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


@pytest.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    """Fresh state per test: no users, knowledge bases or members in the database, no sessions in Redis."""
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table kb_members, knowledge_bases, users cascade"))
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


async def add_kb(engine: AsyncEngine, name: str, description: str | None = None) -> uuid.UUID:
    async with engine.begin() as connection:
        kb_id = await connection.scalar(
            text("insert into knowledge_bases (name, description) values (:name, :description) returning id"),
            {"name": name, "description": description},
        )
    assert isinstance(kb_id, uuid.UUID)
    return kb_id


async def assign(engine: AsyncEngine, kb_id: uuid.UUID, *user_ids: uuid.UUID) -> None:
    async with engine.begin() as connection:
        for user_id in user_ids:
            await connection.execute(
                text("insert into kb_members (kb_id, user_id) values (:kb_id, :user_id)"),
                {"kb_id": kb_id, "user_id": user_id},
            )


async def kb_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("select id, name, description, created_at from knowledge_bases order by name")
        )
        return [dict(row._mapping) for row in result]


async def member_rows(engine: AsyncEngine, kb_id: uuid.UUID | None = None) -> list[tuple[uuid.UUID, uuid.UUID]]:
    async with engine.connect() as connection:
        result = await connection.execute(text("select kb_id, user_id from kb_members order by kb_id, user_id"))
        return [(row.kb_id, row.user_id) for row in result if kb_id is None or row.kb_id == kb_id]


async def names(engine: AsyncEngine) -> list[str]:
    return [row["name"] for row in await kb_rows(engine)]


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


@asynccontextmanager
async def signed_in(engine: AsyncEngine, username: str) -> AsyncIterator[tuple[uuid.UUID, httpx.AsyncClient]]:
    """A new user with role user and a client signed in as that user."""
    user_id = await add_user(engine, username)
    async with client() as api:
        assert (await login(api, username)).status_code == 200
        yield user_id, api


@pytest.fixture
async def mario(engine: AsyncEngine) -> AsyncIterator[tuple[uuid.UUID, httpx.AsyncClient]]:
    """The user mario (role user) and a client signed in as mario."""
    async with signed_in(engine, "mario") as signed:
        yield signed


async def create(api: httpx.AsyncClient, name: str, description: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"name": name}
    if description is not None:
        body["description"] = description
    return await api.post(KBS, json=body)


def error_code(response: httpx.Response) -> str:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    code: str = body["error"]["code"]
    return code


def listed_names(response: httpx.Response) -> list[str]:
    assert response.status_code == 200
    return [kb["name"] for kb in response.json()]


# AC-020: names unique without distinction of case, after removing the surrounding spaces ---------------------------


async def test_AC_020_creating_manuali_when_Manuali_exists_gets_409(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    await add_kb(engine, "Manuali", "Manuali tecnici")

    response = await create(admin, "manuali")

    assert response.status_code == 409
    assert error_code(response) == "kb_name_taken"
    rows = await kb_rows(engine)
    assert [(row["name"], row["description"]) for row in rows] == [("Manuali", "Manuali tecnici")]


@pytest.mark.parametrize("name", ["Manuali", "MANUALI", "mAnUaLi", " manuali", "manuali ", "  Manuali\t", "\nmanuali"])
async def test_AC_020_same_name_in_any_case_or_with_surrounding_spaces_gets_409(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    await add_kb(engine, "Manuali")

    response = await create(admin, name)

    assert response.status_code == 409
    assert error_code(response) == "kb_name_taken"
    assert await names(engine) == ["Manuali"]


async def test_AC_020_renaming_another_kb_to_manuali_gets_409_and_keeps_its_name(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure", "Procedure interne")

    response = await admin.patch(f"{KBS}/{other}", json={"name": "manuali"})

    assert response.status_code == 409
    assert error_code(response) == "kb_name_taken"
    rows = await kb_rows(engine)
    assert [(row["name"], row["description"]) for row in rows] == [
        ("Manuali", None),
        ("Procedure", "Procedure interne"),
    ]


@pytest.mark.parametrize("name", ["MANUALI", " Manuali ", "manuali\t"])
async def test_AC_020_renaming_with_spaces_or_another_case_of_a_taken_name_gets_409(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure")

    response = await admin.patch(f"{KBS}/{other}", json={"name": name})

    assert response.status_code == 409
    assert error_code(response) == "kb_name_taken"
    assert await names(engine) == ["Manuali", "Procedure"]


async def test_AC_020_a_kb_can_be_renamed_to_another_case_of_its_own_name(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "manuali")

    response = await admin.patch(f"{KBS}/{kb_id}", json={"name": "Manuali"})

    assert response.status_code == 200
    assert response.json()["name"] == "Manuali"
    assert await names(engine) == ["Manuali"]


async def test_AC_020_unicode_names_are_compared_without_case(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    await add_kb(engine, "Élite àè")

    response = await create(admin, "élite ÀÈ")

    assert response.status_code == 409
    assert error_code(response) == "kb_name_taken"
    assert await names(engine) == ["Élite àè"]


async def test_AC_020_concurrent_creation_of_the_same_name_gives_one_201_and_409s(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    variants = ["Manuali", "manuali", "MANUALI", " Manuali", "mAnuali "]

    responses = await asyncio.gather(*(create(admin, name) for name in variants))

    assert sorted(response.status_code for response in responses) == [201, 409, 409, 409, 409]
    assert all(error_code(r) == "kb_name_taken" for r in responses if r.status_code == 409)
    assert len(await kb_rows(engine)) == 1


async def test_AC_020_concurrent_renaming_to_the_same_name_gives_one_200_and_one_409(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    first = await add_kb(engine, "Procedure")
    second = await add_kb(engine, "Ricette")

    responses = await asyncio.gather(
        admin.patch(f"{KBS}/{first}", json={"name": "Manuali"}),
        admin.patch(f"{KBS}/{second}", json={"name": "manuali"}),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert error_code(next(r for r in responses if r.status_code == 409)) == "kb_name_taken"
    lowered = sorted(name.lower() for name in await names(engine))
    assert lowered in (["manuali", "procedure"], ["manuali", "ricette"])


async def test_AC_020_creation_returns_201_and_a_kb_out_with_the_trimmed_name(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    response = await create(admin, "  Manuali tecnici ", "Manuali delle macchine")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == KB_OUT_KEYS
    assert (body["name"], body["description"]) == ("Manuali tecnici", "Manuali delle macchine")
    rows = await kb_rows(engine)
    assert len(rows) == 1
    assert uuid.UUID(body["id"]) == rows[0]["id"]
    assert rows[0]["name"] == "Manuali tecnici"
    assert datetime.fromisoformat(body["created_at"]) == rows[0]["created_at"]


async def test_AC_020_the_description_is_optional(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    without = await create(admin, "Manuali")
    with_null = await admin.post(KBS, json={"name": "Procedure", "description": None})

    assert without.status_code == 201
    assert without.json()["description"] is None
    assert with_null.status_code == 201
    assert with_null.json()["description"] is None
    assert [row["description"] for row in await kb_rows(engine)] == [None, None]


@pytest.mark.parametrize("name", ["M", "a" * 100, "è" * 100, "Manuali tecnici 2026 (v1) / è"])
async def test_AC_020_names_from_1_to_100_characters_are_accepted(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    response = await create(admin, name)

    assert response.status_code == 201
    assert response.json()["name"] == name
    assert await names(engine) == [name]


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "a" * 101, "è" * 101, " " + "a" * 101 + " "])
async def test_AC_020_an_empty_or_too_long_name_gets_422_and_no_kb(
    engine: AsyncEngine, admin: httpx.AsyncClient, name: str
) -> None:
    response = await create(admin, name)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert await kb_rows(engine) == []


async def test_AC_020_spaces_do_not_count_in_the_100_characters(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await create(admin, "  " + "a" * 100 + "  ")

    assert response.status_code == 201
    assert response.json()["name"] == "a" * 100


@pytest.mark.parametrize("body", [{}, {"name": None}, {"name": 5}, {"description": "only a description"}])
async def test_AC_020_creation_without_a_valid_name_gets_422_and_no_kb(
    engine: AsyncEngine, admin: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await admin.post(KBS, json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert await kb_rows(engine) == []


async def test_AC_020_a_description_of_500_characters_is_accepted_and_501_gets_422(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    accepted = await create(admin, "Manuali", "è" * 500)
    rejected = await create(admin, "Procedure", "d" * 501)

    assert accepted.status_code == 201
    assert accepted.json()["description"] == "è" * 500
    assert rejected.status_code == 422
    assert error_code(rejected) == "validation_error"
    assert await names(engine) == ["Manuali"]


async def test_AC_020_rename_trims_the_name_and_keeps_the_description(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")

    response = await admin.patch(f"{KBS}/{kb_id}", json={"name": "  Manuali 2026 "})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == KB_OUT_KEYS
    assert (body["id"], body["name"], body["description"]) == (str(kb_id), "Manuali 2026", "Manuali tecnici")
    rows = await kb_rows(engine)
    assert [(row["name"], row["description"]) for row in rows] == [("Manuali 2026", "Manuali tecnici")]


async def test_AC_020_patch_changes_or_clears_only_the_description(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")

    changed = await admin.patch(f"{KBS}/{kb_id}", json={"description": "Manuali delle macchine"})
    assert changed.status_code == 200
    assert (changed.json()["name"], changed.json()["description"]) == ("Manuali", "Manuali delle macchine")

    cleared = await admin.patch(f"{KBS}/{kb_id}", json={"description": None})
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None
    assert [(row["name"], row["description"]) for row in await kb_rows(engine)] == [("Manuali", None)]


async def test_AC_020_an_empty_patch_changes_nothing(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")
    before = await kb_rows(engine)

    response = await admin.patch(f"{KBS}/{kb_id}", json={})

    assert response.status_code == 200
    assert (response.json()["name"], response.json()["description"]) == ("Manuali", "Manuali tecnici")
    assert await kb_rows(engine) == before


@pytest.mark.parametrize(
    "body", [{"name": ""}, {"name": "   "}, {"name": None}, {"name": "a" * 101}, {"description": "d" * 501}]
)
async def test_AC_020_an_invalid_rename_gets_422_and_changes_nothing(
    engine: AsyncEngine, admin: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")
    before = await kb_rows(engine)

    response = await admin.patch(f"{KBS}/{kb_id}", json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert await kb_rows(engine) == before


async def test_AC_020_patch_and_post_ignore_id_and_created_at_in_the_body(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    forged_id = str(uuid.uuid4())
    created = await admin.post(KBS, json={"name": "Manuali", "id": forged_id, "created_at": "2000-01-01T00:00:00Z"})
    assert created.status_code == 201
    assert created.json()["id"] != forged_id
    before = await kb_rows(engine)

    patched = await admin.patch(
        f"{KBS}/{created.json()['id']}", json={"id": forged_id, "created_at": "2000-01-01T00:00:00Z"}
    )

    assert patched.status_code == 200
    assert await kb_rows(engine) == before
    assert before[0]["created_at"].year != 2000


async def test_AC_020_renaming_an_unknown_kb_gets_404(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    response = await admin.patch(f"{KBS}/{uuid.uuid4()}", json={"name": "Manuali"})

    assert response.status_code == 404
    assert error_code(response) == "kb_not_found"
    assert await kb_rows(engine) == []


async def test_AC_020_the_admin_lists_every_kb_ordered_by_name(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    await add_kb(engine, "ricette")
    await add_kb(engine, "Manuali", "Manuali tecnici")
    await add_kb(engine, "Procedure")

    response = await admin.get(KBS)

    assert listed_names(response) == ["Manuali", "Procedure", "ricette"]
    assert all(set(kb) == KB_OUT_KEYS for kb in response.json())
    assert response.json()[0]["description"] == "Manuali tecnici"


async def test_AC_020_the_kb_api_accepts_only_json(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    kb_id = await add_kb(engine, "Manuali")

    created = await admin.post(KBS, data={"name": "Procedure"})
    renamed = await admin.patch(
        f"{KBS}/{kb_id}", content=b'{"name": "Ricette"}', headers={"Content-Type": "text/plain"}
    )

    assert created.status_code == 422
    assert renamed.status_code == 422
    assert error_code(created) == "validation_error"
    assert await names(engine) == ["Manuali"]


# AC-021: deleting a KB removes it and its assignments ----------------------------------------------------------------


async def test_AC_021_deleting_a_kb_with_two_users_removes_it_and_its_assignments(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure")
    async with signed_in(engine, "mario") as (mario_id, mario_api), signed_in(engine, "luigi") as (luigi_id, luigi_api):
        await assign(engine, kb_id, mario_id, luigi_id)
        await assign(engine, other, mario_id)
        assert listed_names(await mario_api.get(KBS)) == ["Manuali", "Procedure"]
        assert listed_names(await luigi_api.get(KBS)) == ["Manuali"]

        response = await admin.delete(f"{KBS}/{kb_id}")

        assert response.status_code == 204
        assert response.content == b""
        assert await names(engine) == ["Procedure"]
        assert await member_rows(engine, kb_id) == []
        assert await member_rows(engine) == [(other, mario_id)]
        assert listed_names(await mario_api.get(KBS)) == ["Procedure"]
        assert listed_names(await luigi_api.get(KBS)) == []
        for api in (mario_api, luigi_api):
            opened = await api.get(f"{KBS}/{kb_id}")
            assert opened.status_code == 404
            assert error_code(opened) == "kb_not_found"


async def test_AC_021_the_deleted_kb_is_gone_for_the_admin_too_and_the_users_remain(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    mario_id = await add_user(engine, "mario")
    await assign(engine, kb_id, mario_id)

    assert (await admin.delete(f"{KBS}/{kb_id}")).status_code == 204

    assert listed_names(await admin.get(KBS)) == []
    opened = await admin.get(f"{KBS}/{kb_id}")
    assert opened.status_code == 404
    assert error_code(opened) == "kb_not_found"
    members = await admin.get(f"{KBS}/{kb_id}/members")
    assert members.status_code == 404
    assert error_code(members) == "kb_not_found"
    users = await admin.get("/api/admin/users")
    assert sorted(user["username"] for user in users.json()) == [ADMIN, "mario"]
    async with client() as api:
        assert (await login(api, "mario")).status_code == 200


async def test_AC_021_deleting_twice_or_an_unknown_kb_gets_404(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    kb_id = await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure")

    first = await admin.delete(f"{KBS}/{kb_id}")
    second = await admin.delete(f"{KBS}/{kb_id}")
    unknown = await admin.delete(f"{KBS}/{uuid.uuid4()}")

    assert first.status_code == 204
    assert second.status_code == 404
    assert error_code(second) == "kb_not_found"
    assert unknown.json() == second.json()
    assert await names(engine) == ["Procedure"]
    assert other == (await kb_rows(engine))[0]["id"]


async def test_AC_021_the_name_of_a_deleted_kb_can_be_used_again(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    kb_id = await add_kb(engine, "Manuali")
    assert (await admin.delete(f"{KBS}/{kb_id}")).status_code == 204

    response = await create(admin, "manuali")

    assert response.status_code == 201
    assert response.json()["id"] != str(kb_id)


# AC-022: assigning and removing users, without duplicates ------------------------------------------------------------


async def test_AC_022_assigning_an_assigned_user_again_keeps_one_assignment(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    mario_id = await add_user(engine, "mario")

    first = await admin.put(f"{KBS}/{kb_id}/members/{mario_id}")
    second = await admin.put(f"{KBS}/{kb_id}/members/{mario_id}")

    assert (first.status_code, second.status_code) == (204, 204)
    assert (first.content, second.content) == (b"", b"")
    assert await member_rows(engine) == [(kb_id, mario_id)]
    members = await admin.get(f"{KBS}/{kb_id}/members")
    assert members.status_code == 200
    assert [user["username"] for user in members.json()] == ["mario"]


async def test_AC_022_concurrent_assignments_of_the_same_user_keep_one_row(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    mario_id = await add_user(engine, "mario")

    responses = await asyncio.gather(*(admin.put(f"{KBS}/{kb_id}/members/{mario_id}") for _ in range(5)))

    assert [response.status_code for response in responses] == [204] * 5
    assert await member_rows(engine) == [(kb_id, mario_id)]


async def test_AC_022_an_assigned_user_sees_the_kb_and_loses_it_when_removed(
    engine: AsyncEngine, admin: httpx.AsyncClient, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    mario_id, mario_api = mario
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")
    assert listed_names(await mario_api.get(KBS)) == []

    assert (await admin.put(f"{KBS}/{kb_id}/members/{mario_id}")).status_code == 204

    assert listed_names(await mario_api.get(KBS)) == ["Manuali"]
    opened = await mario_api.get(f"{KBS}/{kb_id}")
    assert opened.status_code == 200
    assert (opened.json()["name"], opened.json()["description"]) == ("Manuali", "Manuali tecnici")

    removed = await admin.delete(f"{KBS}/{kb_id}/members/{mario_id}")

    assert removed.status_code == 204
    assert removed.content == b""
    assert await member_rows(engine) == []
    assert listed_names(await mario_api.get(KBS)) == []
    assert (await mario_api.get(f"{KBS}/{kb_id}")).status_code == 404
    assert (await admin.get(f"{KBS}/{kb_id}/members")).json() == []


async def test_AC_022_removing_a_user_who_is_not_assigned_is_harmless(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    mario_id = await add_user(engine, "mario")
    luigi_id = await add_user(engine, "luigi")
    await assign(engine, kb_id, luigi_id)

    response = await admin.delete(f"{KBS}/{kb_id}/members/{mario_id}")

    assert response.status_code == 204
    assert await member_rows(engine) == [(kb_id, luigi_id)]


async def test_AC_022_members_lists_the_assigned_users_as_user_out(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure")
    mario_id = await add_user(engine, "mario")
    luigi_id = await add_user(engine, "Luigi")
    peach_id = await add_user(engine, "peach")
    await assign(engine, kb_id, mario_id, luigi_id)
    await assign(engine, other, peach_id)

    response = await admin.get(f"{KBS}/{kb_id}/members")

    assert response.status_code == 200
    members = response.json()
    assert all(set(user) == USER_OUT_KEYS for user in members)
    assert [(user["id"], user["username"], user["role"]) for user in members] == [
        (str(luigi_id), "Luigi", "user"),
        (str(mario_id), "mario", "user"),
    ]
    assert "$argon2" not in response.text
    assert "password" not in response.text


async def test_AC_022_assignments_of_one_kb_do_not_touch_the_others(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali")
    other = await add_kb(engine, "Procedure")
    mario_id = await add_user(engine, "mario")
    await assign(engine, other, mario_id)

    assert (await admin.put(f"{KBS}/{kb_id}/members/{mario_id}")).status_code == 204
    assert (await admin.delete(f"{KBS}/{other}/members/{mario_id}")).status_code == 204

    assert await member_rows(engine) == [(kb_id, mario_id)]


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_AC_022_an_unknown_or_invalid_user_id_gets_404_user_not_found(
    engine: AsyncEngine, admin: httpx.AsyncClient, method: str
) -> None:
    kb_id = await add_kb(engine, "Manuali")

    unknown = await admin.request(method, f"{KBS}/{kb_id}/members/{uuid.uuid4()}")
    malformed = await admin.request(method, f"{KBS}/{kb_id}/members/not-a-uuid")

    assert unknown.status_code == 404
    assert error_code(unknown) == "user_not_found"
    assert malformed.status_code == 404
    assert malformed.json() == unknown.json()
    assert await member_rows(engine) == []


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_AC_022_an_unknown_kb_gets_404_kb_not_found_for_a_known_user(
    engine: AsyncEngine, admin: httpx.AsyncClient, method: str
) -> None:
    mario_id = await add_user(engine, "mario")

    response = await admin.request(method, f"{KBS}/{uuid.uuid4()}/members/{mario_id}")

    assert response.status_code == 404
    assert error_code(response) == "kb_not_found"
    assert await member_rows(engine) == []


async def test_AC_022_an_inactive_user_can_be_assigned(engine: AsyncEngine, admin: httpx.AsyncClient) -> None:
    """Minimal choice pending the owner's decision: an inactive user can be assigned (and sees the KB when
    reactivated); the assignment of an inactive user is listed in the members."""
    kb_id = await add_kb(engine, "Manuali")
    luigi_id = await add_user(engine, "luigi", is_active=False)

    response = await admin.put(f"{KBS}/{kb_id}/members/{luigi_id}")

    assert response.status_code == 204
    members = (await admin.get(f"{KBS}/{kb_id}/members")).json()
    assert [(user["username"], user["is_active"]) for user in members] == [("luigi", False)]


# AC-025: the KB admin APIs answer 403 to a user and 401 without a session ---------------------------------------------


def admin_requests(kb_id: uuid.UUID | str, user_id: uuid.UUID | str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every KB admin API: creation, rename, deletion, members, assignment and removal."""
    return [
        ("POST", KBS, {"name": "Rubata"}),
        ("PATCH", f"{KBS}/{kb_id}", {"name": "Rubata"}),
        ("DELETE", f"{KBS}/{kb_id}", None),
        ("GET", f"{KBS}/{kb_id}/members", None),
        ("PUT", f"{KBS}/{kb_id}/members/{user_id}", None),
        ("DELETE", f"{KBS}/{kb_id}/members/{user_id}", None),
    ]


async def test_AC_025_a_user_gets_403_on_every_kb_admin_api_and_nothing_changes(
    engine: AsyncEngine, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    mario_id, mario_api = mario
    assigned = await add_kb(engine, "Manuali")
    not_assigned = await add_kb(engine, "Procedure")
    await assign(engine, assigned, mario_id)
    luigi_id = await add_user(engine, "luigi")
    kbs_before, members_before = await kb_rows(engine), await member_rows(engine)

    for kb_id in (assigned, not_assigned):
        for user_id in (mario_id, luigi_id):
            for method, path, body in admin_requests(kb_id, user_id):
                response = await mario_api.request(method, path, json=body)
                assert response.status_code == 403, (method, path)
                assert error_code(response) == "forbidden"

    assert await kb_rows(engine) == kbs_before
    assert await member_rows(engine) == members_before
    assert (await mario_api.get("/api/auth/me")).status_code == 200


async def test_AC_025_a_user_gets_the_same_403_for_known_unknown_and_invalid_ids(
    engine: AsyncEngine, mario: tuple[uuid.UUID, httpx.AsyncClient]
) -> None:
    """The role is checked before the ids and the body: a user learns nothing about which KBs exist."""
    mario_id, mario_api = mario
    known = await add_kb(engine, "Procedure")
    bodies: set[str] = set()

    for kb_id in (known, uuid.uuid4(), "not-a-uuid"):
        for user_id in (mario_id, uuid.uuid4(), "not-a-uuid"):
            for method, path, _ in admin_requests(kb_id, user_id):
                response = await mario_api.request(method, path, json={"name": ""})
                assert response.status_code == 403, (method, path)
                bodies.add(response.text)

    assert len(bodies) == 1
    assert await names(engine) == ["Procedure"]


async def test_AC_025_without_a_session_every_kb_api_gets_401(engine: AsyncEngine) -> None:
    """AC-026 as well: no KB API answers without a valid session."""
    kb_id = await add_kb(engine, "Manuali")
    mario_id = await add_user(engine, "mario")
    requests = [("GET", KBS, None), ("GET", f"{KBS}/{kb_id}", None), *admin_requests(kb_id, mario_id)]

    async with client() as api:
        for method, path, body in requests:
            response = await api.request(method, path, json=body)
            assert response.status_code == 401, (method, path)
            assert error_code(response) == "not_authenticated"

    assert await names(engine) == ["Manuali"]
    assert await member_rows(engine) == []


# Owner's decision after the review: the description is trimmed like the name ---------------------------------------


async def test_AC_020_the_description_is_trimmed_and_a_blank_one_becomes_null(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    trimmed = await create(admin, "Manuali", "  Manuali tecnici \t")
    blank = await create(admin, "Procedure", "   ")

    assert trimmed.status_code == 201
    assert trimmed.json()["description"] == "Manuali tecnici"
    assert blank.status_code == 201
    assert blank.json()["description"] is None
    assert [row["description"] for row in await kb_rows(engine)] == ["Manuali tecnici", None]


async def test_AC_020_the_500_characters_of_the_description_are_counted_after_trimming(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    accepted = await create(admin, "Manuali", "  " + "d" * 500 + "  ")
    rejected = await create(admin, "Procedure", "  " + "d" * 501 + "  ")

    assert accepted.status_code == 201
    assert accepted.json()["description"] == "d" * 500
    assert rejected.status_code == 422
    assert error_code(rejected) == "validation_error"
    assert await names(engine) == ["Manuali"]


async def test_AC_020_patch_trims_the_description_and_a_blank_one_clears_it(
    engine: AsyncEngine, admin: httpx.AsyncClient
) -> None:
    kb_id = await add_kb(engine, "Manuali", "Manuali tecnici")

    trimmed = await admin.patch(f"{KBS}/{kb_id}", json={"description": " Manuali delle macchine  "})
    assert trimmed.status_code == 200
    assert trimmed.json()["description"] == "Manuali delle macchine"

    blank = await admin.patch(f"{KBS}/{kb_id}", json={"description": " \t "})
    assert blank.status_code == 200
    assert blank.json()["description"] is None
    assert [row["description"] for row in await kb_rows(engine)] == [None]


# Review (low): characters PostgreSQL text refuses get 422, not 500 --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Man\x00uali"},
        {"name": "Manuali", "description": "Manuali\x00tecnici"},
        {"name": "Man\ud800uali"},
        {"name": "Manuali", "description": "\udfffManuali"},
    ],
)
async def test_AC_020_a_nul_or_lone_surrogate_character_gets_422_and_no_kb(
    engine: AsyncEngine, admin: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    # A 500 must be seen as a response, not as an exception raised through the test transport.
    app = create_app(make_settings())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:
        assert (await login(api, ADMIN, ADMIN_PASSWORD)).status_code == 200
        # json.dumps escapes the characters as \u sequences, as a browser would send them.
        content, headers = json.dumps(body), {"Content-Type": "application/json"}
        created = await api.post(KBS, content=content, headers=headers)
        kb_id = await add_kb(engine, "Procedure")
        patched = await api.patch(f"{KBS}/{kb_id}", content=content, headers=headers)

    assert created.status_code == 422
    assert error_code(created) == "validation_error"
    assert patched.status_code == 422
    assert error_code(patched) == "validation_error"
    assert [(row["name"], row["description"]) for row in await kb_rows(engine)] == [("Procedure", None)]
