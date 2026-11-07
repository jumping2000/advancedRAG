"""Repeating one step of the pipeline after a temporary error (REQ-016)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.ingestion.errors import TemporaryError


async def with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `operation`; after a `TemporaryError` run it again, up to `attempts` attempts in all, waiting
    `backoff_seconds` before the second attempt, twice that before the third, and so on. The last error is raised when
    the attempts are used up. A `PermanentError`, or any other exception, ends it at once: repeating cannot help.
    `sleep` is injected by the tests, so that none waits for real."""
    delay = backoff_seconds
    attempt = 1
    while True:
        try:
            return await operation()
        except TemporaryError:
            if attempt >= attempts:
                raise
        await sleep(delay)
        delay *= 2
        attempt += 1
