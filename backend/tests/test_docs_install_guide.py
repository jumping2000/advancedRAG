# SPEC-2026-11-20-fase-5-documentazione
"""The installation and operations guide, in both languages, says what an admin needs to install and run the stack:
AC-007 (REQ-006).

The guide must name the HTTPS override, the reranker on CPU and on a NVIDIA GPU, the health route with its four states,
and every volume of `compose.yaml` (read from the file, so a new volume that the guide forgets fails here). The table
of the variables is checked by test_docs_alignment.py (AC-011). The commands of the guide were run by hand and are
listed, with their result, in the pull request (AC-017, REQ-103).
"""

from __future__ import annotations

import pytest

from tests.docs_support import LANGS, REPO_ROOT, compose_volumes, guide_path, read_text, ui_text

INSTALLATION = "installation.md"
# Paths, options and routes the admin types or reads: they appear as written, in any language
LITERALS = ("compose.tls.yaml", "--profile rerank", "compose.rerank-gpu.yaml", "/api/health", "nginx/certs")
CERTIFICATE_FILES = ("cert.pem", "key.pem")
# The commands of .env.example and of the compose files, as the guide shows them
COMMANDS = (
    "docker compose up --build",
    "docker compose -f compose.yaml -f compose.tls.yaml up --build",
    "docker compose --profile rerank up --build",
    "docker compose -f compose.yaml -f compose.rerank-gpu.yaml --profile rerank up --build",
)
STATES = ("ok", "degraded", "unreachable", "not_configured")
# Interface elements the guide names: the sign-in page of the first start, and the page «Impostazioni» that switches
# the reranker on once the service is configured
REQUIRED_UI_KEYS = ("login.title", "settings.menu", "settings.rerank", "settings.notConfigured")
SYSTEM_PAGES = ("fondazioni.md", "ingestion.md", "chat.md")


def missing_literals(text: str) -> list[str]:
    return [item for item in (*LITERALS, *CERTIFICATE_FILES) if item not in text]


def missing_commands(text: str) -> list[str]:
    return [command for command in COMMANDS if command not in text]


def missing_states(text: str) -> list[str]:
    return [f"`{state}`" for state in STATES if f"`{state}`" not in text]


def missing_volumes(text: str, volumes: list[str]) -> list[str]:
    return [f"`{volume}`" for volume in volumes if f"`{volume}`" not in text]


def guide_text(lang: str) -> str:
    path = guide_path(lang, INSTALLATION)
    assert path.is_file(), f"{path.relative_to(REPO_ROOT).as_posix()} does not exist"
    return read_text(path)


def complete_text(volumes: list[str]) -> str:
    return "\n".join([*LITERALS, *CERTIFICATE_FILES, *COMMANDS, *(f"`{item}`" for item in (*STATES, *volumes))])


@pytest.mark.parametrize("lang", LANGS)
def test_AC_007_the_guide_names_the_https_override_the_reranker_and_the_health_route(lang: str) -> None:
    assert missing_literals(guide_text(lang)) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_007_the_guide_shows_the_start_commands(lang: str) -> None:
    assert missing_commands(guide_text(lang)) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_007_the_guide_explains_the_four_states_of_the_health_check(lang: str) -> None:
    assert missing_states(guide_text(lang)) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_AC_007_the_guide_names_every_volume_of_the_compose_file(lang: str) -> None:
    volumes = compose_volumes()
    assert volumes, "compose.yaml has no top-level volumes"
    assert missing_volumes(guide_text(lang), volumes) == [], f"missing from the guide in '{lang}'"


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_names_the_interface_texts_exactly_as_the_language_file_has_them(lang: str) -> None:
    text = guide_text(lang)

    assert [key for key in REQUIRED_UI_KEYS if ui_text(lang, key) not in text] == []


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_links_the_area_pages_for_the_technical_details(lang: str) -> None:
    text = guide_text(lang)

    assert [page for page in SYSTEM_PAGES if f"](../../system/{page})" not in text] == []


def test_AC_007_a_text_with_everything_passes_every_check() -> None:
    volumes = ["pgdata", "uploads", "reranker-models"]
    text = complete_text(volumes)

    assert missing_literals(text) == []
    assert missing_commands(text) == []
    assert missing_states(text) == []
    assert missing_volumes(text, volumes) == []


def test_AC_007_an_empty_guide_misses_everything_and_each_item_is_named() -> None:
    volumes = ["pgdata", "uploads"]

    assert missing_literals("") == [*LITERALS, *CERTIFICATE_FILES]
    assert missing_commands("") == list(COMMANDS)
    assert missing_states("") == [f"`{state}`" for state in STATES]
    assert missing_volumes("", volumes) == ["`pgdata`", "`uploads`"]


def test_AC_007_a_guide_without_the_https_override_or_the_gpu_file_is_reported() -> None:
    volumes = ["pgdata"]
    text = complete_text(volumes)

    assert missing_literals(text.replace("compose.tls.yaml", "")) == ["compose.tls.yaml"]
    assert missing_literals(text.replace("compose.rerank-gpu.yaml", "")) == ["compose.rerank-gpu.yaml"]
    assert missing_literals(text.replace("/api/health", "")) == ["/api/health"]
    assert "docker compose -f compose.yaml -f compose.tls.yaml up --build" in missing_commands(
        text.replace("compose.tls.yaml", "")
    )


def test_AC_007_a_state_that_is_not_in_backticks_or_a_forgotten_volume_is_reported() -> None:
    volumes = ["pgdata", "uploads", "reranker-models"]
    text = complete_text(volumes)

    assert missing_states(text.replace("`not_configured`", "not_configured")) == ["`not_configured`"]
    assert missing_states(text.replace("`degraded`", "")) == ["`degraded`"]
    assert missing_volumes(text.replace("`reranker-models`", "reranker-models"), volumes) == ["`reranker-models`"]
    assert missing_volumes(text, [*volumes, "a-new-volume"]) == ["`a-new-volume`"]
