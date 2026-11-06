# SPEC-2026-11-06-fase-1-fondazioni
"""AC-033 (REQ-102): every variable read by the backend and the Compose is in .env.example, secrets empty."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY")


def env_example() -> dict[str, str]:
    path = ROOT / ".env.example"
    assert path.is_file(), ".env.example is missing"
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def compose_variables() -> set[str]:
    path = ROOT / "compose.yaml"
    assert path.is_file(), "compose.yaml is missing"
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"\$\{([A-Z0-9_]+)", text))


def test_AC_033_backend_settings_are_in_env_example() -> None:
    names = {field.upper() for field in Settings.model_fields}
    assert names - env_example().keys() == set()


def test_AC_033_compose_variables_are_in_env_example() -> None:
    variables = compose_variables()
    assert {"POSTGRES_PASSWORD", "HTTP_PORT"} <= variables
    assert variables - env_example().keys() == set()


def test_AC_033_secrets_are_empty_in_env_example() -> None:
    secrets = {name: value for name, value in env_example().items() if SECRET_NAME.search(name)}
    assert "POSTGRES_PASSWORD" in secrets
    assert {name for name, value in secrets.items() if value} == set()


def test_AC_033_env_file_is_ignored_by_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
