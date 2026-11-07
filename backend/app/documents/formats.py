"""Accepted document formats, file name cleaning and the check of an uploaded file (skeleton)."""

from pathlib import Path
from typing import Literal

DocumentFormat = Literal["pdf", "docx", "pptx", "xlsx", "txt", "md"]


class UnsupportedType(Exception):
    """The extension, the declared type and the content do not indicate the same accepted format."""


def sanitize_filename(raw: str) -> str:
    return raw


def inspect_upload(path: Path, filename: str, declared_type: str | None) -> DocumentFormat:
    raise UnsupportedType
