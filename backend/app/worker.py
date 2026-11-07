"""The ingestion worker: `python -m app.worker` runs the jobs of the RQ queue `ingestion`, one at a time (REQ-001).

While it runs, a daemon thread writes the key `ingestion:worker:alive:{hostname}` (value "1", expiry 30 seconds) every
10 seconds. `GET /api/health` looks for any key with that prefix, and `python -m app.worker --healthcheck` (the Compose
health check) exits 0 if the key of this host exists, 1 otherwise. The jobs run in the main thread (`SimpleWorker`: no
fork per job), so the thread keeps beating while a long document is processed.

Before the first job (`startup`) the worker recovers from a predecessor that died and cleans up: the documents left
PROCESSING become FAILED `unexpected_error`, the QUEUED ones go back on the queue, and the originals past the rules are
deleted (`app.ingestion.cleanup`); a step that fails is logged by its error type and the worker goes on. The same
cleanup then runs every hour in a daemon thread. There is one worker: a second one starting would fail the document
the first is processing.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, SimpleWorker

from app.config import Settings, get_settings
from app.documents.queue import INGESTION_QUEUE
from app.ingestion import cleanup

HEARTBEAT_PREFIX = "ingestion:worker:alive:"
HEARTBEAT_TTL_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10.0
# RQ's own registration of the worker: renewed while it is alive, so a dead worker disappears after this time.
WORKER_TTL_SECONDS = 60

logger = logging.getLogger(__name__)


def heartbeat_key() -> str:
    return f"{HEARTBEAT_PREFIX}{socket.gethostname()}"


def beat(connection: Redis) -> None:
    connection.set(heartbeat_key(), "1", ex=HEARTBEAT_TTL_SECONDS)


def start_heartbeat(connection: Redis, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS) -> threading.Event:
    """Write the heartbeat now and then every `interval_seconds`, in a daemon thread; set the returned event to stop."""
    stop = threading.Event()
    beat(connection)

    def run() -> None:
        while not stop.wait(interval_seconds):
            try:
                beat(connection)
            except RedisError:
                logger.warning("worker heartbeat: Redis did not answer")

    threading.Thread(target=run, name="worker-heartbeat", daemon=True).start()
    return stop


def healthcheck(connection: Redis) -> bool:
    """True if the heartbeat key of this host exists."""
    return bool(connection.exists(heartbeat_key()))


CLEANUP_INTERVAL_SECONDS = 3600.0


def _run_step(name: str, step: Callable[[], Awaitable[object]]) -> None:
    """Run one async step in an event loop of its own; a failure is logged by its error type and does not stop the
    worker (never the message: it may hold the content of a document or a path)."""
    try:
        asyncio.run(_await(step))
    except Exception as error:
        logger.error("worker %s failed (%s)", name, type(error).__name__)


async def _await(step: Callable[[], Awaitable[object]]) -> object:
    return await step()


def startup(settings: Settings, now: datetime | None = None) -> None:
    """What the worker does before it accepts a job (REQ-007, REQ-017): the documents left PROCESSING by a worker that
    died become FAILED, the QUEUED ones go back on the queue, and the originals past the rules are deleted. The steps
    are independent: one that fails does not stop the others. `now` is injected by the tests."""
    moment = datetime.now(UTC) if now is None else now
    _run_step("recovery of the interrupted documents", lambda: cleanup.recover_interrupted(settings))
    _run_step("requeue of the queued documents", lambda: cleanup.requeue_pending(settings))
    _run_step("cleanup of the originals", lambda: cleanup.cleanup_originals(settings, moment))


def start_cleanup_loop(settings: Settings, interval_seconds: float = CLEANUP_INTERVAL_SECONDS) -> threading.Event:
    """Run the cleanup of the originals every `interval_seconds` (an hour) in a daemon thread, each run in an event loop
    of its own, while the main thread runs the jobs; set the returned event to stop it. The first run is the one of
    `startup`, so the thread waits one interval before its first."""
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(interval_seconds):
            _run_step("cleanup of the originals", lambda: cleanup.cleanup_originals(settings, datetime.now(UTC)))

    threading.Thread(target=run, name="worker-cleanup", daemon=True).start()
    return stop


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url)
    if "--healthcheck" in arguments:
        try:
            alive = healthcheck(connection)
        except RedisError:
            alive = False
        raise SystemExit(0 if alive else 1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The heartbeat comes first: the health check must not fail while the start-up recovers and cleans.
    start_heartbeat(connection)
    startup(settings)
    start_cleanup_loop(settings)
    SimpleWorker(
        [Queue(INGESTION_QUEUE, connection=connection)], connection=connection, worker_ttl=WORKER_TTL_SECONDS
    ).work()


if __name__ == "__main__":
    main()
