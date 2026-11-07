# SPEC-2026-11-07-fase-2-ingestion
"""The worker check of GET /api/health and the worker's heartbeat: AC-001 and AC-002 (REQ-001).

The worker writes the Redis key `ingestion:worker:alive:{hostname}` (value "1", expiry 30 seconds) every 10 seconds; the
health check looks for any key with that prefix. These tests use the Redis of compose.test.yaml, which starts with no
heartbeat key before and after each test. The tests of phase 1 that this spec revises are in `test_health.py`.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from redis import Redis as SyncRedis

from app.config import Settings
from app.worker import HEARTBEAT_PREFIX, beat, healthcheck, heartbeat_key, start_heartbeat
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings
from tests.fakes.http_server import closed_port_url, fake_service

HEARTBEAT_PATTERN = "ingestion:worker:alive:*"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def clear_heartbeats(connection: SyncRedis) -> None:
    for key in connection.scan_iter(match=HEARTBEAT_PATTERN):
        connection.delete(key)


@pytest.fixture(autouse=True)
def redis_connection() -> Iterator[SyncRedis]:
    connection = SyncRedis.from_url(TEST_REDIS_URL)
    clear_heartbeats(connection)
    yield connection
    clear_heartbeats(connection)
    connection.close()


async def test_AC_002_no_worker_gives_200_degraded_and_worker_unreachable(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    """Database and Redis are up, Ollama and Docling answer, and no worker has written its heartbeat."""
    with fake_service() as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "redis": "ok", "ollama": "ok", "docling": "ok", "worker": "unreachable"},
    }


async def test_AC_001_a_heartbeat_key_makes_the_worker_ok_and_the_status_ok(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis
) -> None:
    """The check looks for any key with the prefix: the host name in it is not the backend's."""
    redis_connection.set(f"{HEARTBEAT_PREFIX}some-other-container", "1", ex=30)
    with fake_service() as ollama, fake_service() as docling:
        settings = make_settings(ollama_base_url=ollama, docling_base_url=docling)
        async with client_for(settings) as client:
            response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["checks"]["worker"] == "ok"
    assert response.json()["status"] == "ok"


async def test_AC_002_other_keys_are_not_a_worker(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis
) -> None:
    redis_connection.set("ingestion:worker:other:x", "1", ex=30)
    redis_connection.set("rq:workers", "1", ex=30)
    async with client_for(make_settings()) as client:
        response = await client.get("/api/health")

    assert response.json()["checks"]["worker"] == "unreachable"
    redis_connection.delete("ingestion:worker:other:x", "rq:workers")


async def test_AC_002_the_worker_is_unreachable_and_health_503_when_redis_is_down(
    client_for: Callable[[Settings], httpx.AsyncClient],
) -> None:
    settings = make_settings(redis_url=closed_port_url("redis") + "/0")
    async with client_for(settings) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["worker"] == "unreachable"


def test_AC_001_the_heartbeat_is_a_key_per_host_with_value_1_and_ttl_30(
    redis_connection: SyncRedis,
) -> None:
    key = f"ingestion:worker:alive:{socket.gethostname()}"
    assert heartbeat_key() == key

    beat(redis_connection)

    assert redis_connection.get(key) == b"1"
    assert 0 < redis_connection.ttl(key) <= 30


def test_AC_001_the_heartbeat_thread_writes_the_key_again_every_interval(
    redis_connection: SyncRedis,
) -> None:
    stop = start_heartbeat(redis_connection, interval_seconds=0.05)
    try:
        for _ in range(2):  # the key goes away twice and comes back each time, without anyone calling beat
            deadline = time.monotonic() + 10
            while not redis_connection.exists(heartbeat_key()):
                assert time.monotonic() < deadline, "the heartbeat thread did not write the key"
                time.sleep(0.01)
            redis_connection.delete(heartbeat_key())
    finally:
        stop.set()


def test_AC_001_the_health_check_of_the_worker_is_its_own_heartbeat(
    redis_connection: SyncRedis,
) -> None:
    assert healthcheck(redis_connection) is False

    redis_connection.set(f"{HEARTBEAT_PREFIX}another-host", "1", ex=30)
    assert healthcheck(redis_connection) is False  # a heartbeat of another worker is not ours

    beat(redis_connection)
    assert healthcheck(redis_connection) is True


def run_healthcheck(redis_url: str) -> int:
    environment = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": redis_url,
        "OLLAMA_BASE_URL": "http://127.0.0.1:9",
        "DOCLING_BASE_URL": "http://127.0.0.1:9",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.worker", "--healthcheck"],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    return result.returncode


def test_AC_001_worker_healthcheck_exits_1_without_a_heartbeat_and_0_with_it(
    redis_connection: SyncRedis,
) -> None:
    assert run_healthcheck(TEST_REDIS_URL) == 1

    beat(redis_connection)
    assert run_healthcheck(TEST_REDIS_URL) == 0


def test_AC_001_worker_healthcheck_exits_1_when_redis_is_down() -> None:
    assert run_healthcheck(closed_port_url("redis") + "/0") == 1


def first_page_keys(connection: SyncRedis) -> list[bytes]:
    return list(connection.scan(cursor=0, match=HEARTBEAT_PATTERN, count=100)[1])


async def test_AC_002_the_worker_is_found_among_thousands_of_other_keys_beyond_the_first_scan_page(
    client_for: Callable[[Settings], httpx.AsyncClient], redis_connection: SyncRedis
) -> None:
    """SCAN returns a page at a time: a check that read only the first one would miss a heartbeat key that is not in
    it. 20000 unrelated keys make many pages; the host name of the heartbeat key is chosen so that the first page
    (same MATCH and COUNT as the check) does not hold it."""
    unrelated = [f"scan-test:{number}" for number in range(20000)]
    redis_connection.mset(dict.fromkeys(unrelated, "1"))
    try:
        for attempt in range(50):
            key = f"{HEARTBEAT_PREFIX}scan-host-{attempt}"
            redis_connection.set(key, "1", ex=30)
            if key.encode() not in first_page_keys(redis_connection):
                break
            redis_connection.delete(key)
        else:
            pytest.fail("every heartbeat key landed in the first SCAN page")
        assert first_page_keys(redis_connection) == []

        async with client_for(make_settings()) as client:
            response = await client.get("/api/health")
    finally:
        redis_connection.delete(*unrelated)

    assert response.json()["checks"]["worker"] == "ok"
