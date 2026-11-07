"""Skeleton (T-06): the signatures of the cleanup of the originals and of the recovery at the start of the worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import Settings


@dataclass(frozen=True)
class CleanupReport:
    expired_originals: int
    orphan_files: int


async def cleanup_originals(settings: Settings, now: datetime) -> CleanupReport:
    return CleanupReport(expired_originals=0, orphan_files=0)


async def recover_interrupted(settings: Settings) -> int:
    return 0


async def requeue_pending(settings: Settings) -> int:
    return 0
