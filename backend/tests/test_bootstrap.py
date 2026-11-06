# SPEC-2026-11-06-fase-1-fondazioni
"""The first admin is created from the environment at startup: AC-007, AC-008, AC-009 (REQ-004)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import run
from app.auth.bootstrap import MissingAdminConfig, ensure_admin
from app.auth.passwords import MIN_PASSWORD_LENGTH, hash_password
from app.config import Settings, get_settings
from app.db.models import User
from app.main import create_app
from app.run import migrate
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
ADMIN = "admin"
FIRST_PASSWORD = "first-admin-password"
SECOND_PASSWORD = "second-admin-password"


@pytest.fixture(scope="module")
def schema() -> None:
    migrate(make_settings().sqlalchemy_url)


async def truncate_users() -> None:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.begin() as connection:
        await connection.execute(text("truncate table users cascade"))
    await engine.dispose()


@pytest.fixture
def empty_database(schema: None) -> None:
    """Fresh state per test: no users. Sync, so that the tests may run the real entry point (it owns its loop)."""
    asyncio.run(truncate_users())


@pytest.fixture
async def engine(empty_database: None) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    yield engine
    await engine.dispose()


async def users() -> list[tuple[str, str, bool, str]]:
    engine = create_async_engine(make_settings().sqlalchemy_url)
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(User.username, User.role, User.is_active, User.password_hash).order_by(User.username)
        )
        result = [(r.username, r.role, r.is_active, r.password_hash) for r in rows]
    await engine.dispose()
    return result


async def login_status(username: str, password: str) -> int:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(make_settings())), base_url="http://testserver"
    ) as api:
        response = await api.post("/api/auth/login", json={"username": username, "password": password})
    return response.status_code


async def add_user(engine: AsyncEngine, username: str, password: str, role: str, is_active: bool = True) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(User(username=username, password_hash=hash_password(password), role=role, is_active=is_active))
        await session.commit()


async def bootstrap(engine: AsyncEngine, settings: Settings) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await ensure_admin(session, settings)


# The real entry point (python -m app.run), with uvicorn replaced so that it returns after the startup steps.


@pytest.fixture
def start_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Callable[..., None]]:
    started: list[int] = []
    monkeypatch.chdir(tmp_path)  # no .env file is read
    monkeypatch.setattr(run.uvicorn, "run", lambda *args, **kwargs: started.append(1))
    for name in ("POSTGRES_PASSWORD", "ADMIN_USERNAME", "ADMIN_PASSWORD", "SESSION_IDLE_TIMEOUT_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DOCLING_BASE_URL", "http://127.0.0.1:9")

    def start(**env: str) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        before = len(started)
        run.main()
        assert len(started) == before + 1, "uvicorn was not started"

    yield start
    get_settings.cache_clear()


def test_AC_007_backend_started_twice_creates_one_admin_that_can_log_in(
    empty_database: None, start_backend: Callable[..., None]
) -> None:
    start = start_backend
    start(ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD=FIRST_PASSWORD)
    start(ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD=FIRST_PASSWORD)

    rows = asyncio.run(users())
    assert [(name, role, active) for name, role, active, _ in rows] == [(ADMIN, "admin", True)]
    assert rows[0][3].startswith("$argon2id$")
    assert asyncio.run(login_status(ADMIN, FIRST_PASSWORD)) == 200


def test_AC_008_restart_with_a_changed_admin_password_keeps_the_old_one(
    empty_database: None, start_backend: Callable[..., None]
) -> None:
    start = start_backend
    start(ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD=FIRST_PASSWORD)
    first = asyncio.run(users())
    assert len(first) == 1
    hash_before = first[0][3]
    start(ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD=SECOND_PASSWORD)

    rows = asyncio.run(users())
    assert len(rows) == 1
    assert rows[0][3] == hash_before
    assert asyncio.run(login_status(ADMIN, FIRST_PASSWORD)) == 200
    assert asyncio.run(login_status(ADMIN, SECOND_PASSWORD)) == 401


# ensure_admin directly ------------------------------------------------------------------------------------------------


async def test_AC_007_ensure_admin_twice_creates_a_single_active_admin(engine: AsyncEngine) -> None:
    settings = make_settings(admin_username=ADMIN, admin_password=FIRST_PASSWORD)
    await bootstrap(engine, settings)
    await bootstrap(engine, settings)

    rows = await users()
    assert [(name, role, active) for name, role, active, _ in rows] == [(ADMIN, "admin", True)]
    assert await login_status(ADMIN, FIRST_PASSWORD) == 200


async def test_AC_008_active_admin_exists_so_the_variables_are_ignored(engine: AsyncEngine) -> None:
    await add_user(engine, "boss", FIRST_PASSWORD, role="admin")
    before = await users()

    await bootstrap(engine, make_settings(admin_username=ADMIN, admin_password=SECOND_PASSWORD))
    await bootstrap(engine, make_settings())  # variables missing: nothing to create, no error

    assert await users() == before
    assert await login_status("boss", FIRST_PASSWORD) == 200
    assert await login_status(ADMIN, SECOND_PASSWORD) == 401


async def test_AC_007_inactive_admin_does_not_count_so_the_admin_is_created(engine: AsyncEngine) -> None:
    await add_user(engine, "former", FIRST_PASSWORD, role="admin", is_active=False)
    await add_user(engine, "mario", FIRST_PASSWORD, role="user")

    await bootstrap(engine, make_settings(admin_username=ADMIN, admin_password=SECOND_PASSWORD))

    rows = {name: (role, active) for name, role, active, _ in await users()}
    assert rows.get(ADMIN) == ("admin", True)
    assert await login_status(ADMIN, SECOND_PASSWORD) == 200


@pytest.mark.parametrize(
    ("username", "password", "missing"),
    [
        (ADMIN, None, "ADMIN_PASSWORD"),
        (ADMIN, "", "ADMIN_PASSWORD"),
        (None, FIRST_PASSWORD, "ADMIN_USERNAME"),
        ("", FIRST_PASSWORD, "ADMIN_USERNAME"),
    ],
)
async def test_AC_009_ensure_admin_names_the_missing_variable(
    engine: AsyncEngine, username: str | None, password: str | None, missing: str
) -> None:
    settings = make_settings(admin_username=username, admin_password=password)
    with pytest.raises(MissingAdminConfig) as raised:
        await bootstrap(engine, settings)

    assert raised.value.var_name == missing
    assert missing in str(raised.value)
    assert await users() == []


async def test_AC_007_name_taken_by_a_non_admin_user_stops_without_changing_it(engine: AsyncEngine) -> None:
    """No active admin, but ADMIN_USERNAME already belongs to another account: it is not modified."""
    await add_user(engine, "Admin", FIRST_PASSWORD, role="user")
    before = await users()

    with pytest.raises(MissingAdminConfig) as raised:
        await bootstrap(engine, make_settings(admin_username=ADMIN, admin_password=SECOND_PASSWORD))

    assert raised.value.var_name == "ADMIN_USERNAME"
    assert await users() == before


# python -m app.run in a separate process ------------------------------------------------------------------------------


def run_backend(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"ADMIN_USERNAME", "ADMIN_PASSWORD", "POSTGRES_PASSWORD"}
    }
    environment.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "DATABASE_URL": TEST_DATABASE_URL,
            "REDIS_URL": TEST_REDIS_URL,
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",
            "DOCLING_BASE_URL": "http://127.0.0.1:9",
            **env,
        }
    )
    try:
        return subprocess.run(
            [sys.executable, "-m", "app.run"],
            cwd=tmp_path,  # no .env file is read
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the backend started instead of stopping")


def test_AC_009_missing_admin_password_stops_the_backend_naming_it(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_USERNAME=ADMIN)

    assert result.returncode != 0
    assert "ADMIN_PASSWORD" in result.stderr + result.stdout
    assert asyncio.run(users()) == []


def test_AC_009_empty_admin_password_stops_the_backend_naming_it(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD="")

    assert result.returncode != 0
    assert "ADMIN_PASSWORD" in result.stderr + result.stdout


def test_AC_009_missing_admin_username_stops_the_backend_naming_it(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_PASSWORD=FIRST_PASSWORD)

    assert result.returncode != 0
    assert "ADMIN_USERNAME" in result.stderr + result.stdout
    assert asyncio.run(users()) == []


def test_AC_009_the_message_does_not_contain_the_password(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_PASSWORD=FIRST_PASSWORD)

    assert result.returncode != 0
    assert FIRST_PASSWORD not in result.stderr + result.stdout


async def test_AC_009_existing_active_admin_needs_no_variables(engine: AsyncEngine) -> None:
    await add_user(engine, "boss", FIRST_PASSWORD, role="admin")
    await bootstrap(engine, make_settings())

    async with engine.connect() as connection:
        admins = await connection.scalar(select(func.count()).select_from(User).where(User.role == "admin"))
    assert admins == 1


# Startup validation of the first admin (owner's decision: REQ-007 username rules, MIN_PASSWORD_LENGTH) ---------------


@pytest.mark.parametrize(
    "username",
    ["ab", "a" * 65, "first admin", "admin@example", "admin/root", "admin!"],
)
async def test_AC_009_invalid_admin_username_stops_naming_it(engine: AsyncEngine, username: str) -> None:
    settings = make_settings(admin_username=username, admin_password=FIRST_PASSWORD)
    with pytest.raises(MissingAdminConfig) as raised:
        await bootstrap(engine, settings)

    assert raised.value.var_name == "ADMIN_USERNAME"
    assert "ADMIN_USERNAME" in str(raised.value)
    assert await users() == []


@pytest.mark.parametrize("username", ["abc", "a" * 64, "first.admin_01-x", "ADMIN"])
async def test_AC_007_valid_admin_username_is_created(engine: AsyncEngine, username: str) -> None:
    await bootstrap(engine, make_settings(admin_username=username, admin_password=FIRST_PASSWORD))

    assert [(name, role, active) for name, role, active, _ in await users()] == [(username, "admin", True)]


async def test_AC_009_admin_password_shorter_than_12_stops_naming_it(engine: AsyncEngine) -> None:
    short = "a" * (MIN_PASSWORD_LENGTH - 1)
    with pytest.raises(MissingAdminConfig) as raised:
        await bootstrap(engine, make_settings(admin_username=ADMIN, admin_password=short))

    assert raised.value.var_name == "ADMIN_PASSWORD"
    assert "ADMIN_PASSWORD" in str(raised.value)
    assert short not in str(raised.value)
    assert await users() == []


async def test_AC_007_admin_password_of_exactly_12_is_accepted(engine: AsyncEngine) -> None:
    password = "b" * MIN_PASSWORD_LENGTH
    await bootstrap(engine, make_settings(admin_username=ADMIN, admin_password=password))

    assert await login_status(ADMIN, password) == 200


async def test_AC_008_invalid_variables_are_ignored_when_an_active_admin_exists(engine: AsyncEngine) -> None:
    await add_user(engine, "boss", FIRST_PASSWORD, role="admin")
    before = await users()

    await bootstrap(engine, make_settings(admin_username="x", admin_password="short"))

    assert await users() == before


def test_AC_009_short_admin_password_stops_the_backend_naming_it(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_USERNAME=ADMIN, ADMIN_PASSWORD="elevenchars")

    assert result.returncode != 0
    assert "ADMIN_PASSWORD" in result.stderr + result.stdout
    assert "elevenchars" not in result.stderr + result.stdout
    assert asyncio.run(users()) == []


def test_AC_009_invalid_admin_username_stops_the_backend_naming_it(empty_database: None, tmp_path: Path) -> None:
    result = run_backend(tmp_path, ADMIN_USERNAME="first admin", ADMIN_PASSWORD=FIRST_PASSWORD)

    assert result.returncode != 0
    assert "ADMIN_USERNAME" in result.stderr + result.stdout
    assert asyncio.run(users()) == []
