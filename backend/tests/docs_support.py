"""Helpers shared by the documentation tests of phase 5 (SPEC-2026-11-20-fase-5-documentazione).

They read the files of the repository (the guides, `.env.example`, `compose.yaml`, the language files of the interface)
and the Markdown of a guide. Each one that reads a file takes the repository root as an optional last argument, so that
a test can run the same check on a small tree built under `tmp_path`. This is a helper module, not a test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LANGS = ("en", "it")
GUIDES = ("user.md", "admin.md", "installation.md", "architecture.md")

FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
HEADING = re.compile(r"^ {0,3}(#{2,3})[ \t]+(.*\S)[ \t]*$")
TITLE = re.compile(r"^# +\S")
ENV_ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=")
TABLE_NAME = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|")
IMAGE = re.compile(r"""!\[[^\]]*\]\(\s*([^)\s]+)(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)""")


def guide_path(lang: str, name: str, root: Path = REPO_ROOT) -> Path:
    """`docs/guide/<lang>/<name>`."""
    return root / "docs" / "guide" / lang / name


def read_text(path: Path) -> str:
    """The file as UTF-8 text with `\\n` line ends and without a BOM, so that a file saved on Windows reads the same."""
    text = path.read_bytes().decode("utf-8").removeprefix("﻿")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def lines_outside_code(text: str) -> Iterator[str]:
    """The lines of a Markdown text that are not inside a fenced code block (``` or ~~~, the fence lines included in
    the block): a fence closes with the same character, at least as long, and no text after it."""
    marker = ""
    for line in text.split("\n"):
        fence = FENCE.match(line)
        if not marker and fence:
            marker = fence.group(1)
        elif (
            marker
            and fence
            and not fence.group(2)
            and fence.group(1)[0] == marker[0]
            and len(fence.group(1)) >= len(marker)
        ):
            marker = ""
        elif not marker:
            yield line


def headings(text: str) -> list[str]:
    """The `##` and `###` titles outside code blocks, as `## Title` and `### Title`."""
    found = []
    for line in lines_outside_code(text):
        match = HEADING.match(line)
        if match:
            found.append(f"{match.group(1)} {match.group(2)}")
    return found


def first_line_after_title(text: str) -> str:
    """The first non-blank line after the `# Title` line (empty when there is no title or nothing after it)."""
    seen_title = False
    for line in lines_outside_code(text):
        if not seen_title:
            seen_title = bool(TITLE.match(line))
        elif line.strip():
            return line.strip()
    return ""


def env_example_names(root: Path = REPO_ROOT) -> list[str]:
    """The names of the `NAME=` lines of `.env.example`, in order; comments are not read."""
    text = read_text(root / ".env.example")
    return [match.group(1) for match in map(ENV_ASSIGNMENT.match, text.split("\n")) if match]


def variable_table_names(text: str) -> list[str]:
    """The names of the first column of the rows that start with ``| `NAME` |`` (capitals, digits and `_`), outside
    code blocks. Any other word of the text, or any other column, is not read as a variable."""
    return [match.group(1) for match in map(TABLE_NAME.match, lines_outside_code(text)) if match]


def ui_text(lang: str, key: str, root: Path = REPO_ROOT) -> str:
    """The value of the dotted `key` (`settings.menu`) in `frontend/src/i18n/<lang>.json`; `KeyError` when unknown."""
    value: Any = json.loads(read_text(root / "frontend" / "src" / "i18n" / f"{lang}.json"))
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"{key} is not in {lang}.json")
        value = value[part]
    if not isinstance(value, str):
        raise KeyError(f"{key} in {lang}.json is not a text")
    return value


def image_refs(text: str) -> list[str]:
    """The paths of the Markdown images `![alt](path)` outside code blocks, as written."""
    return [ref for line in lines_outside_code(text) for ref in IMAGE.findall(line)]


def compose_volumes(root: Path = REPO_ROOT) -> list[str]:
    """The names of the top-level `volumes:` section of `compose.yaml`, in order."""
    compose = yaml.safe_load(read_text(root / "compose.yaml"))
    return list(compose.get("volumes") or {})
