"""Helpers shared by the documentation tests of phase 5 (SPEC-2026-11-20-fase-5-documentazione).

Skeleton of T-01: the signatures of the plan, no behaviour yet (every helper returns an empty value), so that the tests
import and fail on their assertions.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LANGS = ("en", "it")
GUIDES = ("user.md", "admin.md", "installation.md", "architecture.md")


def guide_path(lang: str, name: str, root: Path = REPO_ROOT) -> Path:
    return root / "docs" / "guide" / lang / name


def read_text(path: Path) -> str:
    return ""


def headings(text: str) -> list[str]:
    return []


def first_line_after_title(text: str) -> str:
    return ""


def env_example_names(root: Path = REPO_ROOT) -> list[str]:
    return []


def variable_table_names(text: str) -> list[str]:
    return []


def ui_text(lang: str, key: str, root: Path = REPO_ROOT) -> str:
    return ""


def image_refs(text: str) -> list[str]:
    return []


def compose_volumes(root: Path = REPO_ROOT) -> list[str]:
    return []
