"""Text normalization applied before the chunker (REQ-010)."""

from __future__ import annotations

from collections.abc import Sequence

from app.ingestion.blocks import Block


def normalize_text(text: str) -> str:
    return text


def normalize_blocks(blocks: Sequence[Block]) -> list[Block]:
    return list(blocks)
