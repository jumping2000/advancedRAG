"""POSTGRES_PASSWORD with any character (owner's decision on the T-01 review; REQ-001, configuration anti-goal).

Phase 3 (SPEC-2026-11-17-fase-3-chat, T-01): the settings of the generation provider, AC-002 and AC-003 (REQ-001).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app import run
from app.config import Settings, get_settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

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


# Phase 3: the generation provider -----------------------------------------------------------------------------------

PROVIDER_URL = "http://provider.example.internal:11434/v1"
GENERATION_VARIABLES = ("GENERATION_BASE_URL", "GENERATION_MODEL", "GENERATION_API_KEY", "GENERATION_TIMEOUT_SECONDS")


def test_the_generation_defaults_mean_not_configured_with_a_timeout_of_300_seconds() -> None:
    settings = make_settings()

    assert settings.generation_base_url == ""
    assert settings.generation_model == ""
    assert not settings.generation_api_key  # no key: None or empty
    assert settings.generation_timeout_seconds == 300


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation_base_url": PROVIDER_URL, "generation_model": ""},
        {"generation_base_url": PROVIDER_URL, "generation_model": "   "},
        {"generation_base_url": PROVIDER_URL},
    ],
)
def test_SPEC_2026_11_17_fase_3_chat_AC_002_a_base_url_without_a_model_is_refused_naming_the_variable(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as refused:
        make_settings(**overrides)

    assert "GENERATION_MODEL" in str(refused.value)  # the variable as written in .env


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"generation_base_url": ""},
        {"generation_base_url": "", "generation_model": "gpt-test", "generation_api_key": "sk-test"},
        {"generation_base_url": "   ", "generation_model": ""},
        {"generation_base_url": PROVIDER_URL, "generation_model": "gpt-test"},
        {"generation_base_url": PROVIDER_URL, "generation_model": "gpt-test", "generation_api_key": ""},
    ],
)
def test_SPEC_2026_11_17_fase_3_chat_AC_002_the_other_combinations_start(overrides: dict[str, Any]) -> None:
    settings = make_settings(**overrides)

    assert settings.generation_base_url.strip() == settings.generation_base_url
    assert settings.generation_model.strip() == settings.generation_model


@pytest.mark.parametrize("seconds", [0, -1])
def test_the_generation_timeout_must_be_above_zero(seconds: float) -> None:
    with pytest.raises(ValidationError, match="generation_timeout_seconds"):
        make_settings(generation_timeout_seconds=seconds)


def test_the_generation_key_is_never_shown_by_the_settings() -> None:
    settings = make_settings(generation_base_url=PROVIDER_URL, generation_model="m", generation_api_key="sk-chiave-xyz")

    assert "sk-chiave-xyz" not in repr(settings)
    assert "sk-chiave-xyz" not in str(settings)
    assert "sk-chiave-xyz" not in settings.model_dump_json()
    assert settings.generation_api_key is not None
    assert settings.generation_api_key.get_secret_value() == "sk-chiave-xyz"


@pytest.fixture
def backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[str]]:
    """The environment of a backend for `python -m app.run`, with uvicorn and the migrations replaced by markers:
    a settings error is the only thing that can stop it before them. Yields the markers reached."""
    monkeypatch.chdir(tmp_path)  # no .env file is read, except the one a test writes there
    for name in GENERATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DOCLING_BASE_URL", "http://127.0.0.1:9")
    reached: list[str] = []

    def migrate(_: object) -> None:
        reached.append("migrate")
        raise SystemExit("stop here: the settings were accepted")

    monkeypatch.setattr(run, "migrate", migrate)
    monkeypatch.setattr(run.uvicorn, "run", lambda *args, **kwargs: reached.append("uvicorn"))
    get_settings.cache_clear()
    yield reached
    get_settings.cache_clear()


def test_SPEC_2026_11_17_fase_3_chat_AC_002_the_backend_does_not_start_with_a_url_and_no_model(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENERATION_BASE_URL", PROVIDER_URL)
    monkeypatch.setenv("GENERATION_MODEL", "")  # an empty value counts as unset

    with pytest.raises(ValidationError) as refused:
        run.main()

    assert "GENERATION_MODEL" in str(refused.value)
    assert backend_env == []  # neither the migrations nor uvicorn


def test_SPEC_2026_11_17_fase_3_chat_AC_002_the_backend_does_not_start_with_the_pair_in_the_env_file(
    backend_env: list[str], tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(f"GENERATION_BASE_URL={PROVIDER_URL}\nGENERATION_MODEL=\n", encoding="utf-8")

    with pytest.raises(ValidationError) as refused:
        run.main()

    assert "GENERATION_MODEL" in str(refused.value)
    assert backend_env == []


def test_the_backend_starts_with_no_generation_setting_and_with_empty_ones(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit, match="settings were accepted"):  # the run went on to the migrations
        run.main()
    assert backend_env == ["migrate"]

    for name in GENERATION_VARIABLES[:3]:
        monkeypatch.setenv(name, "")  # Compose passes empty values when .env leaves them empty
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="settings were accepted"):
        run.main()
    assert backend_env == ["migrate", "migrate"]


def test_an_empty_key_from_the_environment_is_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_API_KEY", "")

    assert not make_settings().generation_api_key


def test_compose_passes_the_generation_settings_to_the_backend_and_not_to_the_worker() -> None:
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    backend = compose.split("\n  backend:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  nginx:\n", 1)[0]

    for name in GENERATION_VARIABLES:
        assert f"{name}: ${{{name}:-" in backend, f"{name} is not passed to the backend"
        assert name not in worker
    assert "GENERATION_TIMEOUT_SECONDS: ${GENERATION_TIMEOUT_SECONDS:-300}" in backend
