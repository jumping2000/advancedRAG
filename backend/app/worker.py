"""The ingestion worker: `python -m app.worker` runs the jobs of the RQ queue `ingestion`, one at a time (REQ-001).

While it runs, a daemon thread writes the key `ingestion:worker:alive:{hostname}` (value "1", expiry 30 seconds) every
10 seconds. `GET /api/health` looks for any key with that prefix, and `python -m app.worker --healthcheck` (the Compose
health check) exits 0 if the key of this host exists, 1 otherwise. The jobs run in the main thread (`SimpleWorker`: no
fork per job), so the thread keeps beating while a long document is processed.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from datetime import datetime

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, SimpleWorker

from app.config import Settings, get_settings
from app.documents.queue import INGESTION_QUEUE

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


def startup(settings: Settings, now: datetime | None = None) -> None:
    """Skeleton (T-06): the recovery and the cleanup before the worker accepts jobs."""


def start_cleanup_loop(settings: Settings, interval_seconds: float = CLEANUP_INTERVAL_SECONDS) -> threading.Event:
    """Skeleton (T-06): the hourly cleanup in a daemon thread."""
    return threading.Event()


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    connection = Redis.from_url(get_settings().redis_url)
    if "--healthcheck" in arguments:
        try:
            alive = healthcheck(connection)
        except RedisError:
            alive = False
        raise SystemExit(0 if alive else 1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start_heartbeat(connection)
    SimpleWorker(
        [Queue(INGESTION_QUEUE, connection=connection)], connection=connection, worker_ttl=WORKER_TTL_SECONDS
    ).work()


if __name__ == "__main__":
    main()
