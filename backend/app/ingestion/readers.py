"""Readers for the formats that are read directly: TXT and MD (REQ-008)."""

from __future__ import annotations

from typing import Literal

from app.ingestion.blocks import Block


def read_text_document(data: bytes, fmt: Literal["txt", "md"]) -> list[Block]:
    return []
