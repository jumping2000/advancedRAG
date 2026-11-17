# SPEC-2026-11-17-fase-3-chat
"""The answer reaches the client while it is generated, and a long answer does not block the backend: AC-015, AC-045
(REQ-008, REQ-101), against a real uvicorn started by the test (a real socket: `httpx.ASGITransport` returns the whole
stream at the end, and it never closes a connection).

The provider is a fake whose answer the test controls with a script: a fragment, a pause, a gate (a `threading.Event`
that the test sets) and more fragments. The order in time is therefore proved without measuring sleeps: the third
fragment is sent only after the client has read the first one. Also here: the keepalive comment (the interval is a
constant of `app.chat.service`, made short here), and a client that leaves, whose answer must not stay PENDING.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat import service
from app.chat.repository import ConversationRepository
from app.config import Settings
from app.main import create_app
from tests.fakes.services import delta_event, finish_event, sse_data
from tests.test_chat_ask import (
    PASSWORD,
    Chat,
    add_conversation,
    ask,
    ask_new,
    chat_world,
    chunk_row,
    events_of,
    fetch,
    messages_of,
    names_of,
    parse_sse,
    pending_count,
    read_line,
    seed,
    sources_of,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_knowledge_bases import login
from tests.test_logging_privacy import all_logs

QUESTION = "quokka zibaldone sbirulino"
SECONDS = 10.0  # the most a test waits for something that must happen at once


@contextmanager
def running(settings: Settings) -> Iterator[str]:
    """The app served by a real uvicorn on a free local port, in a thread of its own (an event loop of its own: a
    blocking call of the backend cannot be hidden by the loop of the test). Yields the base URL."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), log_level="warning", timeout_graceful_shutdown=2, access_log=False)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + SECONDS
    while not server.started:
        assert thread.is_alive(), "uvicorn stopped at start"
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(SECONDS)
        listener.close()


async def events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """The events of the stream as they arrive: (name, data), and ("comment", line) for a comment line."""
    pending: dict[str, Any] = {}
    async for line in response.aiter_lines():
        event = read_line(line, pending)
        if event is not None:
            yield event


@pytest.fixture
async def chat(engine: AsyncEngine) -> AsyncIterator[Chat]:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


async def eventually(check: Callable[[], Any], within: float = SECONDS) -> Any:
    """Wait until `check()` (a coroutine function) gives a true value; its last value after `within` seconds."""
    deadline = time.monotonic() + within
    while True:
        value = await check()
        if value or time.monotonic() >= deadline:
            return value
        await asyncio.sleep(0.05)


async def signed_in(base_url: str, username: str = "mario") -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(SECONDS, connect=SECONDS))
    assert (await login(client, username, PASSWORD)).status_code == 200
    return client


# AC-015 -------------------------------------------------------------------------------------------


async def test_AC_015_the_client_gets_the_first_fragment_before_the_provider_sends_the_third(chat: Chat) -> None:
    """Three fragments, a second apart; the third is held by a gate that the test opens after reading the first."""
    await seed(chat, [chunk_row(0, "Testo del manuale", section_title="Garanzia", page_start=2, page_end=3)])
    gate = threading.Event()
    chat.provider.script = [
        delta_event("Uno "),
        1.0,
        delta_event("due "),
        1.0,
        gate,
        delta_event("tre [1]"),
        finish_event(),
        sse_data("[DONE]"),
    ]
    received: list[tuple[str, Any]] = []
    first_delta_after = -1.0

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            started = time.monotonic()
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for event in events(response):
                    received.append(event)
                    if event[0] == "delta" and not gate.is_set():
                        first_delta_after = time.monotonic() - started
                        gate.set()  # the provider may now send the third fragment
        finally:
            gate.set()
            await api.aclose()

    # the first fragment arrived while the provider was still holding the third (it waits 30 s for the gate at most)
    assert 0 <= first_delta_after < SECONDS
    names = [name for name, _ in received if name != "comment"]
    assert names == ["conversation", "delta", "delta", "delta", "done"]
    assert [data["text"] for name, data in received if name == "delta"] == ["Uno ", "due ", "tre [1]"]
    message = [data for name, data in received if name == "done"][0]["message"]
    conversation = [data for name, data in received if name == "conversation"][0]
    assert message["content"] == "Uno due tre [1]"
    assert message["id"] == conversation["message_id"]
    assert message["status"] == "COMPLETED"
    assert message["sources"] == [
        {"number": 1, "document_name": "manuale.pdf", "section_title": "Garanzia", "page_start": 2, "page_end": 3}
    ]
    stored = await messages_of(chat.engine, conversation["conversation_id"])
    assert [(m["role"], m["status"], m["content"]) for m in stored] == [
        ("user", "COMPLETED", QUESTION),
        ("assistant", "COMPLETED", "Uno due tre [1]"),
    ]
    assert [(r["number"], r["cited"]) for r in await sources_of(chat.engine, message["id"])] == [(1, True)]


async def test_AC_015_the_fragments_arrive_one_event_each_in_the_order_the_provider_sent_them(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.fragments = [f"f{n} " for n in range(30)]

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                received = [event async for event in events(response)]
        finally:
            await api.aclose()

    assert [data["text"] for name, data in received if name == "delta"] == [f"f{n} " for n in range(30)]
    assert received[-1][0] == "done"
    assert received[-1][1]["message"]["content"] == "".join(f"f{n} " for n in range(30))


# The keepalive (plan: a comment every 15 s while no fragment arrives) -----------------------------


async def test_a_keepalive_comment_is_sent_while_the_provider_has_sent_nothing(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(chat, [chunk_row(0)])
    monkeypatch.setattr(service, "KEEPALIVE_SECONDS", 0.2)
    chat.provider.script = [1.5, delta_event("Ciao"), finish_event(), sse_data("[DONE]")]

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                received = [event async for event in events(response)]
        finally:
            await api.aclose()

    names = [name for name, _ in received]
    first_delta = names.index("delta")
    keepalives = [data for name, data in received[:first_delta] if name == "comment"]
    assert len(keepalives) >= 3  # 1.5 s of silence, a comment every 0.2 s
    assert set(keepalives) == {": keepalive"}
    assert names[0] == "conversation"
    assert names[-1] == "done"
    assert received[-1][1]["message"]["content"] == "Ciao"


# AC-045: a long answer does not block the others --------------------------------------------------


async def test_AC_045_while_an_answer_waits_for_the_provider_the_other_calls_answer_in_time(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.delay_seconds = 30.0  # the provider answers after 30 s, or when the test releases it
    settings = chat.settings_with()
    limit = settings.health_check_timeout_seconds + 1.0
    outcome: dict[str, Any] = {}

    async def long_answer(api: httpx.AsyncClient) -> None:
        async with api.stream("POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}) as response:
            outcome["status"] = response.status_code
            outcome["events"] = [event async for event in events(response)]

    async def timed(api: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response, float]:
        started = time.monotonic()
        response = await api.request(method, path, **kwargs)
        return response, time.monotonic() - started

    probes: list[float] = []

    async def probe(api: httpx.AsyncClient, stop: asyncio.Event) -> None:
        """A quick call every 50 ms for the whole life of the answer, so that no step of it (the embedding, the
        searches, the wait for the provider, the saving) can block the backend without a call noticing."""
        while not stop.is_set():
            probes.append((await timed(api, "GET", "/api/kbs"))[1])
            await asyncio.sleep(0.05)

    with running(settings) as base_url:
        waiting = await signed_in(base_url)
        other = await signed_in(base_url)
        stop = asyncio.Event()
        probing = asyncio.create_task(probe(other, stop))
        try:
            running_answer = asyncio.create_task(long_answer(waiting))
            assert await asyncio.to_thread(chat.provider.request_received.wait, SECONDS), "the provider got no request"
            assert await pending_count(chat.engine) == 1  # the answer is in progress

            health, health_time = await timed(other, "GET", "/api/health")
            kbs, kbs_time = await timed(other, "GET", "/api/kbs")
            (busy_conversation,) = await fetch(chat.engine, "select id from conversations")
            busy, busy_time = await timed(
                other, "POST", f"/api/conversations/{busy_conversation['id']}/messages", json={"question": "altra"}
            )
            unknown, unknown_time = await timed(
                other, "POST", f"/api/conversations/{busy_conversation['id']}x/messages", json={"question": "altra"}
            )
            assert not running_answer.done()  # all of this happened while the answer was still waiting

            async def probed_enough() -> bool:
                return len(probes) >= 5

            assert await eventually(probed_enough), "the quick calls did not go on while the answer waited"
            chat.provider.release.set()
            await asyncio.wait_for(running_answer, SECONDS)
        finally:
            chat.provider.release.set()
            stop.set()
            await probing
            await waiting.aclose()
            await other.aclose()

    assert health.status_code == 200
    assert health.json()["checks"]["generation"] == "ok"
    assert kbs.status_code == 200
    assert [kb["name"] for kb in kbs.json()] == ["Manuali"]
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "answer_in_progress")  # a conversation route
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "conversation_not_found")
    for name, elapsed in (("health", health_time), ("kbs", kbs_time), ("busy", busy_time), ("unknown", unknown_time)):
        assert elapsed <= limit, f"{name} took {elapsed:.2f} s, the limit is {limit} s"
    assert max(probes) <= limit, f"a call took {max(probes):.2f} s during the answer, the limit is {limit} s"
    assert outcome["status"] == 200
    assert outcome["events"][-1][0] == "done"  # the waiting answer finished normally once released


# A client that leaves -----------------------------------------------------------------------------


async def test_a_client_that_closes_the_connection_leaves_a_failed_interrupted_answer_never_a_pending_one(
    chat: Chat,
) -> None:
    """The minimum of REQ-015 that T-03 owes: the answer is not left PENDING (T-04 adds the rest, and the provider's
    side). The provider sends the next fragment only after the client has gone, so that the backend writes to a dead
    connection or is cancelled by it, as a real browser tab that closes."""
    await seed(chat, [chunk_row(0)])
    gate = threading.Event()
    chat.provider.script = [delta_event("Inizio "), gate, delta_event("fine"), finish_event(), sse_data("[DONE]")]

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                async for name, _ in events(response):
                    if name == "delta":
                        break  # the client leaves: the connection is closed on leaving the block
            await api.aclose()

            async def answer_row() -> dict[str, Any] | None:
                rows = await fetch(chat.engine, "select * from messages where role = 'assistant'")
                return rows[0] if rows and rows[0]["status"] != "PENDING" else None

            gate.set()
            row = await eventually(answer_row)
        finally:
            gate.set()

    assert row is not None, "the answer stayed PENDING after the client left"
    assert (row["status"], row["error_code"], row["content"]) == ("FAILED", "interrupted", "")
    assert await pending_count(chat.engine) == 0
    assert await sources_of(chat.engine, row["id"]) == []
    (user_message,) = await fetch(chat.engine, "select * from messages where role = 'user'")
    assert (user_message["status"], user_message["content"]) == ("COMPLETED", QUESTION)  # the question is kept


# A client that leaves before the answer starts or at its first event ---------------------------------------------
# The route saves the question and the PENDING answer before the response exists: the answer must end FAILED
# `interrupted` whenever the response ends without the whole stream, also when the stream never started (nothing of the
# generator ran) or stopped at its first event. The ASGI application is called directly with a scope of ours, so that
# the moment of the departure is exact (a real socket cannot be closed at a chosen instant); `spec_version` 2.4 is the
# protocol where a failed `send` raises `OSError` in the response, 2.3 the one where the response waits for
# `http.disconnect`.


async def leave(chat: Chat, path: str, question: str, spec_version: str, when: str, username: str = "mario") -> None:
    """Send the question as `username` and make the client go at `when`: `start` (the send of the response start
    fails), `first-event` (the send of the first body chunk fails), `disconnect-at-once` (the disconnect message is
    the first thing the response receives) or `disconnect-in-first-event` (it arrives while the first body chunk is
    being sent). Returns when the application call ends, whatever way it ends."""
    async with chat.api(username) as api:
        cookie = "; ".join(f"{name}={value}" for name, value in api.cookies.items())
        app = create_app(chat.settings_with())
        body = json.dumps({"question": question}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cookie", cookie.encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        sent_body = 0
        gone = asyncio.Event()
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            if when == "disconnect-in-first-event":
                await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal sent_body
            if message["type"] == "http.response.start":
                if when == "start":
                    raise OSError("client gone")
                return
            if message["type"] == "http.response.body":
                sent_body += 1
                if sent_body == 1 and when == "first-event":
                    raise OSError("client gone")
                if sent_body == 1 and when == "disconnect-in-first-event":
                    gone.set()
                    await asyncio.sleep(3600)  # the response is cancelled here

        try:
            await app(scope, receive, send)  # type: ignore[arg-type]
        except BaseException as error:  # noqa: BLE001  (the client is gone: how the call ends is not the point)
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
        finally:
            await app.state.engine.dispose()
            await app.state.redis.aclose()


async def answer_state(chat: Chat) -> tuple[str, str | None] | None:
    rows = await fetch(chat.engine, "select status, error_code from messages where role = 'assistant'")
    return (rows[0]["status"], rows[0]["error_code"]) if rows and rows[0]["status"] != "PENDING" else None


LEAVING = [
    ("2.4", "start"),  # the generator never ran: nothing of it can save the answer
    ("2.4", "first-event"),  # the generator is suspended at its first event
    ("2.3", "disconnect-at-once"),  # the response is cancelled before it starts
    ("2.3", "disconnect-in-first-event"),  # the response is cancelled while the first event is sent
]


@pytest.mark.parametrize(("spec_version", "when"), LEAVING)
async def test_a_client_that_leaves_before_the_first_event_leaves_a_failed_interrupted_answer_and_a_free_conversation(
    chat: Chat, spec_version: str, when: str
) -> None:
    await seed(chat, [chunk_row(0)])

    await leave(chat, f"/api/kbs/{chat.kb_a}/conversations", QUESTION, spec_version, when)

    state = await eventually(lambda: answer_state(chat), within=5.0)
    assert state == ("FAILED", "interrupted"), "the answer stayed PENDING after the client left"
    assert await pending_count(chat.engine) == 0
    (conversation,) = await fetch(chat.engine, "select id from conversations")
    (user_message, answer) = await messages_of(chat.engine, conversation["id"])
    assert (user_message["status"], user_message["content"]) == ("COMPLETED", QUESTION)  # the question is kept
    assert (answer["content"], answer["notice"]) == ("", None)
    assert await sources_of(chat.engine, answer["id"]) == []
    assert chat.provider.requests == []  # the departure came before anything was asked of Ollama or the provider
    # the conversation is free: a new question is answered, not refused with 409 answer_in_progress
    async with chat.api("mario") as api:
        again = await ask(api, conversation["id"], "quokka di nuovo")
    assert again.status_code == 200, again.text
    assert [name for name, _ in parse_sse(again.text) if name != "comment"][-1] == "done"
    assert await pending_count(chat.engine) == 0


@pytest.mark.parametrize(("spec_version", "when"), LEAVING)
async def test_a_client_that_leaves_before_the_first_event_of_an_answer_in_an_existing_conversation_frees_it(
    chat: Chat, spec_version: str, when: str
) -> None:
    await seed(chat, [chunk_row(0)])
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "prima"), ("assistant", "ok")
    )

    await leave(chat, f"/api/conversations/{conversation}/messages", QUESTION, spec_version, when)

    async def failed() -> Any:
        rows = await fetch(chat.engine, "select status, error_code from messages where status <> 'COMPLETED'")
        return rows if rows and rows[0]["status"] != "PENDING" else None

    rows = await eventually(failed, within=5.0)
    assert rows is not None, "the answer stayed PENDING after the client left"
    assert [(r["status"], r["error_code"]) for r in rows] == [("FAILED", "interrupted")]
    async with chat.api("mario") as api:
        assert (await ask(api, conversation, "quokka di nuovo")).status_code == 200


async def test_a_response_that_ends_after_the_whole_stream_leaves_the_answer_as_it_is(chat: Chat) -> None:
    """The cleaning of a response that ends is not a failure of an answer that is COMPLETED."""
    await seed(chat, [chunk_row(0)])

    async with chat.api("mario") as api:
        events = events_of(await ask_new(api, chat.kb_a, QUESTION))

    assert names_of(events)[-1] == "done"
    assert await answer_state(chat) == ("COMPLETED", None)


# The answer is saved FAILED even if the request is cancelled while it is being saved ---------------------------------


async def slow_fail(monkeypatch: pytest.MonkeyPatch, started: asyncio.Event) -> None:
    """`fail_answer` that stops for a while before it writes (a database that is slow)."""
    original = ConversationRepository.fail_answer

    async def slow(self: ConversationRepository, message_id: Any, code: str) -> bool:
        started.set()
        await asyncio.sleep(0.5)
        return await original(self, message_id, code)

    monkeypatch.setattr(ConversationRepository, "fail_answer", slow)


async def pending_question(chat: Chat) -> tuple[FastAPI, Request, Any, Any, Any]:
    """The state of the route after `start_answer`: an app, a request, the user, the conversation, the two messages."""
    app = create_app(chat.settings_with())
    request = Request({"type": "http", "app": app, "method": "POST", "path": "/", "headers": []})
    async with app.state.sessionmaker() as session:
        mario = await chat.user(session, "mario")
        repository = ConversationRepository(session)
        conversation = await repository.create(mario, chat.kb_a, QUESTION[:80])
        asked, answer = await repository.start_answer(conversation, QUESTION)
    return app, request, mario, conversation, (asked, answer)


@pytest.mark.parametrize("failure", ["provider", "unexpected"])
async def test_a_failed_answer_is_saved_even_if_the_request_is_cancelled_while_it_is_saved(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    await seed(chat, [chunk_row(0)])
    if failure == "provider":
        chat.provider.fail_status = 500
    else:

        async def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("guasto")

        monkeypatch.setattr(service, "bm25_search", broken)
    saving = asyncio.Event()
    await slow_fail(monkeypatch, saving)
    app, request, mario, conversation, (asked, answer) = await pending_question(chat)

    async def consume() -> None:
        async for _ in service.answer_events(request, mario, conversation, asked, answer):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(saving.wait(), SECONDS)  # the answer failed and is being saved
    task.cancel()  # the client goes now
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await eventually(lambda: answer_state(chat), within=5.0)
    await app.state.engine.dispose()
    await app.state.redis.aclose()

    assert state is not None, "the answer stayed PENDING: the cancellation interrupted the saving of the failure"
    assert state[0] == "FAILED"
    assert state[1] == ("generation_error" if failure == "provider" else "internal_error")


# The client goes right after `done` -----------------------------------------------------------------------------------


async def test_a_client_that_goes_right_after_done_leaves_the_answer_completed_and_logs_no_failure(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    app, request, mario, conversation, (asked, answer) = await pending_question(chat)

    with all_logs() as logs:
        stream = service.answer_events(request, mario, conversation, asked, answer)
        names = []
        async for event in stream:
            names.append(event.split("\n", 1)[0])
            if names[-1] == "event: done":
                break  # the client goes: the generator is closed while it is suspended at `done`
        await stream.aclose()
        await asyncio.sleep(0.3)  # time for the cleaning of the closed generator
    await app.state.engine.dispose()
    await app.state.redis.aclose()

    assert names[-1] == "event: done"
    assert await answer_state(chat) == ("COMPLETED", None)
    lines = [line for name, line in logs.lines if name.startswith("app.chat")]
    assert not any("failed" in line or "interrupted" in line for line in lines), lines


async def test_the_stream_closed_at_the_first_event_of_the_generator_saves_the_answer_failed_interrupted(
    chat: Chat,
) -> None:
    """The generator alone, without the response around it: closed while it is suspended at the `conversation` event."""
    await seed(chat, [chunk_row(0)])
    app, request, mario, conversation, (asked, answer) = await pending_question(chat)

    stream = service.answer_events(request, mario, conversation, asked, answer)
    first = await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
    state = await eventually(lambda: answer_state(chat), within=5.0)
    await app.state.engine.dispose()
    await app.state.redis.aclose()

    assert first.startswith("event: conversation\n")
    assert state == ("FAILED", "interrupted"), "the answer stayed PENDING after the generator was closed"
