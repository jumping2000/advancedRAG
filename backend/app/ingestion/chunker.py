"""Structural chunker (REQ-011)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.blocks import Block


@dataclass(frozen=True)
class ChunkerConfig:
    target_words: int
    min_words: int
    max_words: int
    overlap_percent: int


@dataclass(frozen=True)
class ChunkDraft:
    position: int
    section_title: str | None
    page_start: int | None
    page_end: int | None
    content: str
    word_count: int


def chunk_blocks(blocks: Sequence[Block], config: ChunkerConfig) -> list[ChunkDraft]:
    return []
