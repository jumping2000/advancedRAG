"""The processing of one document by the worker (REQ-007, REQ-008, REQ-012, REQ-014, REQ-015)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.ingestion.persist import verify_indexes  # noqa: F401


async def process_document(
    document_id: uuid.UUID, settings: Settings, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> None:
    return None
