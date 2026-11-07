"""Repeating one step of the pipeline after a temporary error (REQ-016). Skeleton: the step runs once."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    return await operation()
