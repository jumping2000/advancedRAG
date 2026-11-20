# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The settings of the rewriting service and of the reranker (T-01): AC-001 (REQ-001) and AC-002 (REQ-002).

A value that is not valid stops the backend before the migrations, with a message that names the variable as it is
written in `.env`; the message never repeats a key. An empty `REWRITE_BASE_URL` or `RERANK_BASE_URL` means that the step
is not configured, and the backend starts.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import run
from app.config import Settings, get_settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

REWRITE_URL = "http://rewrite.example.internal:8000/v1"
RERANK_URL = "http://reranker.example.internal:8080"
REWRITE_VARIABLES = ("REWRITE_BASE_URL", "REWRITE_MODEL", "REWRITE_API_KEY", "REWRITE_TIMEOUT_SECONDS")
RERANK_VARIABLES = (
    "RERANK_BASE_URL",
    "RERANK_API",
    "RERANK_MODEL",
    "RERANK_API_KEY",
    "RERANK_CANDIDATES",
    "RERANK_TIMEOUT_SECONDS",
)
KEY = "sk-chiave-segreta-xyz"


def refusal(**overrides: Any) -> str:
    """The message of the settings error for these values (the test fails if they are accepted)."""
    with pytest.raises(ValidationError) as refused:
        make_settings(**overrides)
    return str(refused.value)


@pytest.fixture
def backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[str]]:
    """The environment of a backend for `python -m app.run`, with the migrations and uvicorn replaced by markers: a
    settings error is the only thing that can stop it before them. Yields the markers reached."""
    monkeypatch.chdir(tmp_path)  # no .env file is read, except the one a test writes there
    for name in (*REWRITE_VARIABLES, *RERANK_VARIABLES, "RETRIEVAL_CANDIDATES"):
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


def start(reached: list[str]) -> None:
    """Run the backend: it must get to the migrations."""
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="settings were accepted"):
        run.main()
    assert reached[-1] == "migrate"


def refused_start(reached: list[str]) -> str:
    """Run the backend: it must stop on the settings, before the migrations and uvicorn. Returns the message."""
    get_settings.cache_clear()
    before = list(reached)
    with pytest.raises(ValidationError) as refused:
        run.main()
    assert reached == before  # neither the migrations nor uvicorn
    return str(refused.value)


# Defaults -----------------------------------------------------------------------------------------------------------


def test_the_defaults_leave_both_services_not_configured() -> None:
    settings = make_settings()

    assert settings.rewrite_base_url == ""
    assert settings.rewrite_model == ""
    assert not settings.rewrite_api_key
    assert settings.rewrite_timeout_seconds == 30
    assert settings.rerank_base_url == ""
    assert settings.rerank_api == "tei"
    assert settings.rerank_model == ""
    assert not settings.rerank_api_key
    assert settings.rerank_candidates == 20
    assert settings.rerank_timeout_seconds == 15
    assert settings.rewrite_configured is False
    assert settings.rerank_configured is False


# AC-001: the rewriting service --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"rewrite_base_url": REWRITE_URL}, id="no-model"),
        pytest.param({"rewrite_base_url": REWRITE_URL, "rewrite_model": ""}, id="empty-model"),
        pytest.param({"rewrite_base_url": REWRITE_URL, "rewrite_model": "   "}, id="blank-model"),
    ],
)
def test_AC_001_a_rewrite_base_url_without_a_model_is_refused_naming_the_variable(overrides: dict[str, Any]) -> None:
    assert "REWRITE_MODEL" in refusal(**overrides)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("ftp://x", id="ftp"),
        pytest.param("http://", id="http-without-host"),
        pytest.param("https://", id="https-without-host"),
        pytest.param("rewrite:8000/v1", id="no-scheme"),
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("http://rewrite.example.internal:port/v1", id="port-not-a-number"),
        pytest.param("http://rewrite example.internal/v1", id="space-in-the-host"),
    ],
)
def test_AC_001_a_rewrite_base_url_that_is_not_an_http_url_with_a_host_is_refused_naming_the_variable(url: str) -> None:
    assert "REWRITE_BASE_URL" in refusal(rewrite_base_url=url, rewrite_model="rewrite-model")


@pytest.mark.parametrize("seconds", [0, -1])
def test_AC_001_the_rewrite_timeout_must_be_above_zero(seconds: float) -> None:
    assert "REWRITE_TIMEOUT_SECONDS" in refusal(rewrite_timeout_seconds=seconds)


@pytest.mark.parametrize(
    ("variables", "named"),
    [
        pytest.param({"REWRITE_BASE_URL": REWRITE_URL, "REWRITE_MODEL": ""}, "REWRITE_MODEL", id="no-model"),
        pytest.param({"REWRITE_BASE_URL": "ftp://x", "REWRITE_MODEL": "m"}, "REWRITE_BASE_URL", id="ftp"),
        pytest.param({"REWRITE_BASE_URL": "http://", "REWRITE_MODEL": "m"}, "REWRITE_BASE_URL", id="no-host"),
        pytest.param({"REWRITE_TIMEOUT_SECONDS": "0"}, "REWRITE_TIMEOUT_SECONDS", id="timeout-zero"),
    ],
)
def test_AC_001_the_backend_does_not_start_and_names_the_variable(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], named: str
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    assert named in refused_start(backend_env)


def test_AC_001_the_backend_does_not_start_with_the_pair_in_the_env_file(
    backend_env: list[str], tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(f"REWRITE_BASE_URL={REWRITE_URL}\nREWRITE_MODEL=\n", encoding="utf-8")

    assert "REWRITE_MODEL" in refused_start(backend_env)


def test_AC_001_an_empty_rewrite_base_url_starts_and_rewriting_is_not_configured(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    start(backend_env)  # nothing set
    for name in REWRITE_VARIABLES[:3]:
        monkeypatch.setenv(name, "")  # Compose passes empty values when .env leaves them empty
    start(backend_env)
    monkeypatch.setenv("REWRITE_BASE_URL", "   ")
    monkeypatch.setenv("REWRITE_MODEL", "rewrite-model")  # a model without a base URL is not a configuration
    start(backend_env)

    assert backend_env == ["migrate", "migrate", "migrate"]
    assert get_settings().rewrite_configured is False
    assert make_settings(rewrite_model="m", rewrite_api_key="sk-unused").rewrite_configured is False


@pytest.mark.parametrize(
    "url", [REWRITE_URL, REWRITE_URL + "/", "https://api.openai.com/v1", "  http://[::1]:8000/v1  "]
)
def test_AC_001_a_valid_rewrite_configuration_starts_and_is_configured(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("REWRITE_BASE_URL", url)
    monkeypatch.setenv("REWRITE_MODEL", " rewrite-model ")
    start(backend_env)

    settings = get_settings()
    assert settings.rewrite_configured is True
    assert settings.rewrite_base_url == url.strip()
    assert settings.rewrite_model == "rewrite-model"


def test_AC_001_the_rewrite_key_is_never_shown_by_the_settings_nor_by_a_refusal() -> None:
    settings = make_settings(rewrite_base_url=REWRITE_URL, rewrite_model="m", rewrite_api_key=KEY)

    assert KEY not in repr(settings)
    assert KEY not in settings.model_dump_json()
    assert settings.rewrite_api_key is not None
    assert settings.rewrite_api_key.get_secret_value() == KEY
    assert KEY not in refusal(rewrite_base_url="ftp://x", rewrite_model="m", rewrite_api_key=KEY)


# AC-002: the reranker -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        pytest.param({"rerank_base_url": RERANK_URL, "rerank_api": "xyz"}, "RERANK_API", id="api-xyz"),
        pytest.param({"rerank_base_url": RERANK_URL, "rerank_api": "TEI-or-cohere"}, "RERANK_API", id="api-other"),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_api": "cohere", "rerank_model": ""},
            "RERANK_MODEL",
            id="cohere-empty",
        ),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_api": "cohere", "rerank_model": "  "},
            "RERANK_MODEL",
            id="cohere-blank",
        ),
        pytest.param({"rerank_base_url": RERANK_URL, "rerank_api": "cohere"}, "RERANK_MODEL", id="cohere-no-model"),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_candidates": 0, "retrieval_candidates": 50},
            "RERANK_CANDIDATES",
            id="candidates-0",
        ),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_candidates": 51, "retrieval_candidates": 50},
            "RERANK_CANDIDATES",
            id="candidates-51",
        ),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_candidates": -3}, "RERANK_CANDIDATES", id="candidates-negative"
        ),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_candidates": 11, "retrieval_candidates": 10},
            "RERANK_CANDIDATES",
            id="above-retrieval",
        ),
        pytest.param({"rerank_timeout_seconds": 0}, "RERANK_TIMEOUT_SECONDS", id="timeout-0"),
        pytest.param({"rerank_timeout_seconds": -1}, "RERANK_TIMEOUT_SECONDS", id="timeout-negative"),
        pytest.param({"rerank_base_url": "ftp://x"}, "RERANK_BASE_URL", id="url-ftp"),
        pytest.param({"rerank_base_url": "http://"}, "RERANK_BASE_URL", id="url-no-host"),
        pytest.param({"rerank_base_url": "reranker:8080"}, "RERANK_BASE_URL", id="url-no-scheme"),
    ],
)
def test_AC_002_a_reranker_value_that_is_not_valid_is_refused_naming_the_variable(
    overrides: dict[str, Any], named: str
) -> None:
    assert named in refusal(**overrides)


@pytest.mark.parametrize(
    ("variables", "named"),
    [
        pytest.param({"RERANK_BASE_URL": RERANK_URL, "RERANK_API": "xyz"}, "RERANK_API", id="api-xyz"),
        pytest.param(
            {"RERANK_BASE_URL": RERANK_URL, "RERANK_API": "cohere", "RERANK_MODEL": ""}, "RERANK_MODEL", id="cohere"
        ),
        pytest.param(
            {"RERANK_BASE_URL": RERANK_URL, "RERANK_CANDIDATES": "0", "RETRIEVAL_CANDIDATES": "50"},
            "RERANK_CANDIDATES",
            id="candidates-0",
        ),
        pytest.param(
            {"RERANK_BASE_URL": RERANK_URL, "RERANK_CANDIDATES": "51", "RETRIEVAL_CANDIDATES": "50"},
            "RERANK_CANDIDATES",
            id="candidates-51",
        ),
        pytest.param({"RERANK_TIMEOUT_SECONDS": "0"}, "RERANK_TIMEOUT_SECONDS", id="timeout-0"),
        pytest.param({"RERANK_BASE_URL": "ftp://x"}, "RERANK_BASE_URL", id="url-ftp"),
    ],
)
def test_AC_002_the_backend_does_not_start_and_names_the_variable(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], named: str
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    assert named in refused_start(backend_env)


def test_AC_002_the_backend_does_not_start_with_cohere_and_no_model_in_the_env_file(
    backend_env: list[str], tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"RERANK_BASE_URL={RERANK_URL}\nRERANK_API=cohere\nRERANK_MODEL=\n", encoding="utf-8"
    )

    assert "RERANK_MODEL" in refused_start(backend_env)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"rerank_candidates": 1}, id="candidates-1"),
        pytest.param({"rerank_candidates": 50, "retrieval_candidates": 50}, id="candidates-equal-to-retrieval"),
        pytest.param({"rerank_candidates": 30, "retrieval_candidates": 30}, id="candidates-30-of-30"),
        pytest.param({"rerank_base_url": RERANK_URL}, id="tei-without-model"),
        pytest.param({"rerank_base_url": RERANK_URL, "rerank_api": "tei", "rerank_model": "ignored"}, id="tei-model"),
        pytest.param({"rerank_base_url": RERANK_URL, "rerank_api": "cohere", "rerank_model": "rerank-v3"}, id="cohere"),
        pytest.param({"rerank_timeout_seconds": 0.5}, id="timeout-half-second"),
    ],
)
def test_AC_002_the_valid_reranker_values_are_accepted(overrides: dict[str, Any]) -> None:
    settings = make_settings(**overrides)

    for name, value in overrides.items():
        assert getattr(settings, name) == value


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_rerank_api_is_the_default_tei(value: str) -> None:
    """Empty counts as not set, as for the other settings of the `.env` file."""
    assert make_settings(rerank_api=value).rerank_api == "tei"


def test_AC_002_the_backend_starts_without_a_reranker_and_with_a_valid_one(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    start(backend_env)
    assert get_settings().rerank_configured is False
    for name in ("RERANK_BASE_URL", "RERANK_MODEL", "RERANK_API_KEY"):
        monkeypatch.setenv(name, "")  # Compose passes empty values when .env leaves them empty
    start(backend_env)
    assert get_settings().rerank_configured is False

    monkeypatch.setenv("RERANK_BASE_URL", RERANK_URL)
    monkeypatch.setenv("RERANK_API", "cohere")
    monkeypatch.setenv("RERANK_MODEL", "rerank-v3")
    monkeypatch.setenv("RERANK_CANDIDATES", "50")
    start(backend_env)

    settings = get_settings()
    assert settings.rerank_configured is True
    assert (settings.rerank_api, settings.rerank_model, settings.rerank_candidates) == ("cohere", "rerank-v3", 50)
    assert backend_env == ["migrate", "migrate", "migrate"]


def test_AC_002_the_rerank_key_is_never_shown_by_the_settings_nor_by_a_refusal() -> None:
    settings = make_settings(rerank_base_url=RERANK_URL, rerank_api_key=KEY)

    assert KEY not in repr(settings)
    assert KEY not in settings.model_dump_json()
    assert settings.rerank_api_key is not None
    assert settings.rerank_api_key.get_secret_value() == KEY
    assert KEY not in refusal(rerank_base_url="ftp://x", rerank_api_key=KEY)
    assert KEY not in refusal(rerank_base_url=RERANK_URL, rerank_api="cohere", rerank_api_key=KEY)


def test_an_empty_key_from_the_environment_is_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWRITE_API_KEY", "")
    monkeypatch.setenv("RERANK_API_KEY", "")

    settings = make_settings()
    assert not settings.rewrite_api_key
    assert not settings.rerank_api_key


# Compose: the backend gets the variables, the worker does not ------------------------------------------------------


def test_compose_passes_the_rewrite_and_rerank_settings_to_the_backend_and_not_to_the_worker() -> None:
    compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
    backend = compose.split("\n  backend:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  nginx:\n", 1)[0]

    for name in (*REWRITE_VARIABLES, *RERANK_VARIABLES):
        assert f"{name}: ${{{name}:-" in backend, f"{name} is not passed to the backend"
        assert name not in worker
    for line in (
        "REWRITE_TIMEOUT_SECONDS: ${REWRITE_TIMEOUT_SECONDS:-30}",
        "RERANK_API: ${RERANK_API:-tei}",
        "RERANK_CANDIDATES: ${RERANK_CANDIDATES:-20}",
        "RERANK_TIMEOUT_SECONDS: ${RERANK_TIMEOUT_SECONDS:-15}",
    ):
        assert line in backend


def test_settings_fields_match_the_plan() -> None:
    """The names of the plan's interface (T-02 and T-03 read them)."""
    fields = set(Settings.model_fields)
    assert {
        "rewrite_base_url",
        "rewrite_model",
        "rewrite_api_key",
        "rewrite_timeout_seconds",
        "rerank_base_url",
        "rerank_api",
        "rerank_model",
        "rerank_api_key",
        "rerank_candidates",
        "rerank_timeout_seconds",
    } <= fields


# Revision of AC-002 (spec revision after the T-01 review): the reranker values are checked only with RERANK_BASE_URL
# set, so that a configuration of phase 3 starts as before; the two timeouts must be finite numbers above 0, always.


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        pytest.param({"rerank_timeout_seconds": float("inf")}, "RERANK_TIMEOUT_SECONDS", id="rerank-inf"),
        pytest.param({"rerank_timeout_seconds": float("nan")}, "RERANK_TIMEOUT_SECONDS", id="rerank-nan"),
        pytest.param(
            {"rerank_base_url": RERANK_URL, "rerank_timeout_seconds": float("inf")},
            "RERANK_TIMEOUT_SECONDS",
            id="rerank-inf-configured",
        ),
        pytest.param({"rewrite_timeout_seconds": float("nan")}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-nan"),
        pytest.param({"rewrite_timeout_seconds": float("inf")}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-inf"),
        pytest.param({"rewrite_timeout_seconds": float("-inf")}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-minus-inf"),
        pytest.param(
            {"rewrite_base_url": REWRITE_URL, "rewrite_model": "m", "rewrite_timeout_seconds": float("nan")},
            "REWRITE_TIMEOUT_SECONDS",
            id="rewrite-nan-configured",
        ),
    ],
)
def test_AC_002_a_timeout_that_is_not_a_finite_number_is_refused_naming_the_variable(
    overrides: dict[str, Any], named: str
) -> None:
    assert named in refusal(**overrides)


@pytest.mark.parametrize(
    ("variables", "named"),
    [
        pytest.param({"RERANK_TIMEOUT_SECONDS": "inf"}, "RERANK_TIMEOUT_SECONDS", id="rerank-inf"),
        pytest.param(
            {"RERANK_BASE_URL": RERANK_URL, "RERANK_TIMEOUT_SECONDS": "inf"},
            "RERANK_TIMEOUT_SECONDS",
            id="rerank-inf-configured",
        ),
        pytest.param({"RERANK_TIMEOUT_SECONDS": "nan"}, "RERANK_TIMEOUT_SECONDS", id="rerank-nan"),
        pytest.param({"REWRITE_TIMEOUT_SECONDS": "nan"}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-nan"),
        pytest.param({"REWRITE_TIMEOUT_SECONDS": "inf"}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-inf"),
        pytest.param({"REWRITE_TIMEOUT_SECONDS": "-inf"}, "REWRITE_TIMEOUT_SECONDS", id="rewrite-minus-inf"),
    ],
)
def test_AC_002_the_backend_does_not_start_with_a_timeout_that_is_not_finite(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], named: str
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    assert named in refused_start(backend_env)


@pytest.mark.parametrize(
    "variables",
    [
        pytest.param({"RETRIEVAL_CANDIDATES": "10"}, id="retrieval-10-default-candidates-20"),
        pytest.param({"RERANK_API": "cohere", "RERANK_MODEL": ""}, id="cohere-without-model"),
        pytest.param({"RERANK_API": "cohere"}, id="cohere-model-unset"),
        pytest.param({"RERANK_API": "xyz"}, id="api-xyz"),
        pytest.param({"RERANK_CANDIDATES": "0"}, id="candidates-0"),
        pytest.param({"RERANK_CANDIDATES": "51", "RETRIEVAL_CANDIDATES": "50"}, id="candidates-51"),
        pytest.param({"RERANK_BASE_URL": "", "RETRIEVAL_CANDIDATES": "10"}, id="empty-url-retrieval-10"),
        pytest.param({"RERANK_BASE_URL": "   ", "RERANK_API": "cohere", "RERANK_MODEL": ""}, id="blank-url-cohere"),
    ],
)
def test_AC_002_without_a_reranker_base_url_its_other_values_are_not_checked_and_the_backend_starts(
    backend_env: list[str], monkeypatch: pytest.MonkeyPatch, variables: dict[str, str]
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    start(backend_env)

    assert backend_env == ["migrate"]
    assert get_settings().rerank_configured is False


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"retrieval_candidates": 10}, id="retrieval-10-default-candidates-20"),
        pytest.param({"rerank_api": "cohere", "rerank_model": ""}, id="cohere-without-model"),
        pytest.param({"rerank_api": "xyz"}, id="api-xyz"),
        pytest.param({"rerank_candidates": 0}, id="candidates-0"),
        pytest.param({"rerank_candidates": 51, "retrieval_candidates": 50}, id="candidates-51"),
    ],
)
def test_AC_002_without_a_reranker_base_url_the_settings_are_accepted_and_not_configured(
    overrides: dict[str, Any],
) -> None:
    settings = make_settings(**overrides)

    assert settings.rerank_configured is False
    assert settings.rerank_api in ("tei", "cohere")  # the value read by the code is always one of the two


def test_AC_002_with_a_reranker_base_url_the_phase_3_retrieval_of_10_needs_rerank_candidates_within_it() -> None:
    assert "RERANK_CANDIDATES" in refusal(rerank_base_url=RERANK_URL, retrieval_candidates=10)
    settings = make_settings(rerank_base_url=RERANK_URL, retrieval_candidates=10, rerank_candidates=10)
    assert settings.rerank_configured is True
