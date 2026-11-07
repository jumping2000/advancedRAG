"""Client of docling-serve and the reader of its DoclingDocument (REQ-008). Skeleton: nothing is converted yet."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.documents.formats import DocumentFormat
from app.ingestion.blocks import Block


def docling_json_to_blocks(json_content: Mapping[str, object]) -> list[Block]:
    return []


class DoclingClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def convert(self, path: Path, filename: str, fmt: DocumentFormat) -> list[Block]:
        return []
