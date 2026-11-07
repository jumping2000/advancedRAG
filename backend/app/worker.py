"""The ingestion worker process: `python -m app.worker` (REQ-001)."""

from __future__ import annotations

import threading

from redis import Redis

HEARTBEAT_PREFIX = "ingestion:worker:alive:"
HEARTBEAT_TTL_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10.0


def heartbeat_key() -> str:
    return HEARTBEAT_PREFIX


def beat(connection: Redis) -> None:
    return None


def start_heartbeat(connection: Redis, interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS) -> threading.Event:
    return threading.Event()


def healthcheck(connection: Redis) -> bool:
    return True


def main(argv: list[str] | None = None) -> None:
    return None


if __name__ == "__main__":
    main()
