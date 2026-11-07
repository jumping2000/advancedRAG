"""Structured text blocks shared by the readers, the normalizer and the chunker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Block:
    """One unit of a document in reading order: a heading, a paragraph or a table row."""

    kind: Literal["heading", "paragraph", "table_row"]
    text: str
    level: int = 0
    page: int | None = None
