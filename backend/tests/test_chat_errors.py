# SPEC-2026-11-17-fase-3-chat
"""A failed, timed-out or interrupted answer stays saved with its reason, and one answer at a time: AC-016, AC-031,
AC-032, AC-033, AC-036 (REQ-008, REQ-014, REQ-015, REQ-017), through the API against the real PostgreSQL (ParadeDB) of
compose.test.yaml, the fake Ollama and the fake generation provider (real local sockets, never a real service).

Where the order in time or a real socket matters, the app is served by a real uvicorn started by the test
(`running`, test_chat_streaming.py): a client that closes the connection (AC-033: the provider must see its own request
closed, which `httpx.ASGITransport` can never show) and requests that are truly concurrent. The timeout (AC-016) is
measured, with a tolerance, on the stream that `ASGITransport` returns whole.

What T-03 already covers is not repeated here (an unexpected exception is `internal_error`, the one-PENDING index with
rows inserted by the test, the client that leaves before the first event, a conversation deleted while the answer is
generated): see test_chat_ask.py and test_chat_streaming.py. The retry route is in test_chat_retry.py.

Review focus of T-04: a provider that refuses a context larger than its window (400) ends the answer as
`generation_error`, never as a 500; two truly concurrent questions in one conversation get exactly one 409 per
refused request and one answer.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import run
from app.chat.repository import ConversationRepository
from app.config import get_settings
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.fakes.services import delta_event, finish_event, sse_data
from tests.test_chat_ask import (
    ADMIN,
    ADMIN_PASSWORD,
    QUESTION,
    Chat,
    add_conversation,
    ask,
    ask_new,
    chunk_row,
    error_of,
    events_of,
    fetch,
    messages_of,
    names_of,
    pending_count,
    seed,
    sources_of,
    table_counts,
)
from tests.test_chat_ask import chat as chat  # noqa: F401  (the fixture)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_streaming import SECONDS, events, eventually, running, signed_in
from tests.test_logging_privacy import all_logs

# Generous: the whole question (login excluded) is a few hundred milliseconds besides the time the provider takes
TIMEOUT_TOLERANCE = 4.0


async def saved_answer(chat: Chat) -> dict[str, Any]:
    """The one answer of the database, and the one question that precedes it, checked."""
    asked, answer = await fetch(chat.engine, "select * from messages order by created_at, id")
    assert (asked["role"], asked["status"], asked["content"]) == ("user", "COMPLETED", QUESTION)
    assert answer["role"] == "assistant"
    return answer


def closed_port_url() -> str:
    """The base URL of a provider that is not there: a port that nobody listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


# AC-016: the timeout -------------------------------------------------------------------------------


async def test_AC_016_a_provider_that_never_answers_ends_after_about_2_seconds_with_generation_timeout(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.delay_seconds = 30.0  # it answers after 30 s: far later than GENERATION_TIMEOUT_SECONDS

    async with chat.api("mario", generation_timeout_seconds=2) as api:
        started = time.monotonic()
        response = await ask_new(api, chat.kb_a, QUESTION)
        elapsed = time.monotonic() - started

    events = events_of(response)
    assert 1.9 <= elapsed <= 2.0 + TIMEOUT_TOLERANCE, f"the answer ended after {elapsed:.2f} s, the timeout is 2 s"
    assert names_of(events) == ["conversation", "error"]
    error = events[1][1]["error"]
    assert error["code"] == "generation_timeout"
    assert isinstance(error["message"], str)
    assert error["message"]
    answer = await saved_answer(chat)
    assert (answer["status"], answer["error_code"], answer["content"]) == ("FAILED", "generation_timeout", "")
    assert str(answer["id"]) == events[0][1]["message_id"]
    assert await pending_count(chat.engine) == 0


async def test_AC_016_a_provider_that_stops_after_some_text_times_out_and_the_text_is_discarded(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    gate = threading.Event()  # never opened: the provider sends a fragment and then nothing
    chat.provider.script = [
        delta_event("Testo parziale "),
        gate,
        delta_event("fine"),
        finish_event(),
        sse_data("[DONE]"),
    ]

    try:
        async with chat.api("mario", generation_timeout_seconds=2) as api:
            started = time.monotonic()
            response = await ask_new(api, chat.kb_a, QUESTION)
            elapsed = time.monotonic() - started
    finally:
        gate.set()

    events = events_of(response)
    assert 1.9 <= elapsed <= 2.0 + TIMEOUT_TOLERANCE, f"the answer ended after {elapsed:.2f} s, the timeout is 2 s"
    assert names_of(events) == ["conversation", "delta", "error"]
    assert events[-1][1]["error"]["code"] == "generation_timeout"
    answer = await saved_answer(chat)
    assert (answer["status"], answer["error_code"], answer["content"]) == ("FAILED", "generation_timeout", "")
    assert await sources_of(chat.engine, answer["id"]) == []
    assert await pending_count(chat.engine) == 0


# AC-032: each failure has its code, the question stays and the text is discarded --------------------------------


def break_ollama(chat: Chat) -> dict[str, Any]:
    chat.ollama.fail_status = 500
    return {}


def break_provider_unreachable(chat: Chat) -> dict[str, Any]:
    return {"generation_base_url": closed_port_url()}


def break_provider_500(chat: Chat) -> dict[str, Any]:
    chat.provider.fail_status = 500
    return {}


def break_provider_stream(chat: Chat) -> dict[str, Any]:
    chat.provider.fragments = ["Testo parziale"]
    chat.provider.send_finish = False
    chat.provider.send_done = False
    chat.provider.abort_after_body = True  # the connection is cut after the first fragment, with no end of stream
    return {}


FAILURES = [
    pytest.param(break_ollama, "embedding_error", id="ollama-in-error"),
    pytest.param(break_provider_unreachable, "generation_error", id="provider-unreachable"),
    pytest.param(break_provider_500, "generation_error", id="provider-500"),
    pytest.param(break_provider_stream, "generation_error", id="provider-closes-the-stream-midway"),
]


@pytest.mark.parametrize(("break_it", "code"), FAILURES)
async def test_AC_032_the_question_is_saved_the_answer_failed_with_the_code_and_no_text(
    chat: Chat, break_it: Callable[[Chat], dict[str, Any]], code: str
) -> None:
    await seed(chat, [chunk_row(0)])
    settings = break_it(chat)

    async with chat.api("mario", **settings) as api:
        events = events_of(await ask_new(api, chat.kb_a, QUESTION))

    assert names_of(events)[0] == "conversation"
    assert names_of(events)[-1] == "error"
    assert "done" not in names_of(events)
    error = events[-1][1]["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert "Testo parziale" not in error["message"]
    answer = await saved_answer(chat)  # the question is saved COMPLETED
    assert (answer["status"], answer["error_code"], answer["content"], answer["notice"]) == ("FAILED", code, "", None)
    assert str(answer["id"]) == events[0][1]["message_id"]
    assert await sources_of(chat.engine, answer["id"]) == []
    assert await pending_count(chat.engine) == 0


async def test_AC_032_an_ollama_in_error_never_reaches_the_provider(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.ollama.fail_status = 500

    async with chat.api("mario") as api:
        events_of(await ask_new(api, chat.kb_a, QUESTION))

    assert chat.provider.completions() == []


# Review focus: a context larger than the window of the model -----------------------------------------------------


async def test_a_provider_that_refuses_the_context_with_400_ends_the_answer_as_generation_error_never_a_500(
    chat: Chat,
) -> None:
    """The provider answers 400 (with a JSON body) when the chunks and the history do not fit its window: the request
    of the user is not refused, the answer is saved FAILED `generation_error` and can be retried."""
    await seed(chat, [chunk_row(0, "Paragrafo molto lungo. " * 5000)])
    chat.provider.fail_status = 400

    async with chat.api("mario") as api:
        response = await ask_new(api, chat.kb_a, QUESTION)

    events = events_of(response)  # status 200: the stream carries the failure
    assert names_of(events) == ["conversation", "error"]
    assert events[1][1]["error"]["code"] == "generation_error"
    answer = await saved_answer(chat)
    assert (answer["status"], answer["error_code"], answer["content"]) == ("FAILED", "generation_error", "")
    assert len(chat.provider.completions()) == 1
    assert await pending_count(chat.engine) == 0


# AC-033: the client leaves, the generation stops ------------------------------------------------------------------


async def test_AC_033_when_the_client_closes_after_the_first_fragment_the_provider_sees_its_request_closed(
    chat: Chat,
) -> None:
    """A slow provider: a fragment, then an SSE comment every 0.1 s for a minute (the client skips comments; a write on
    a connection that the backend closed fails, and the fake provider records it in `client_closed`). The browser
    tab closes after the first fragment: the answer is FAILED `interrupted` without text and the connection to the
    provider is closed, so the generation really stops, not only the stream to the client."""
    await seed(chat, [chunk_row(0)])
    chat.provider.script = [delta_event("Inizio "), *([b": slow\n\n", 0.1] * 600)]
    first_fragment: list[str] = []

    async def settled_answer() -> dict[str, Any] | None:
        rows = await fetch(chat.engine, "select * from messages where role = 'assistant'")
        return rows[0] if rows and rows[0]["status"] != "PENDING" else None

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                async for name, data in events(response):
                    if name == "delta":
                        first_fragment.append(data["text"])
                        break  # the client leaves: the connection is closed on leaving the block
            await api.aclose()
            provider_saw_close = await asyncio.to_thread(chat.provider.client_closed.wait, SECONDS)
            row = await eventually(settled_answer)
        finally:
            await api.aclose()

    assert first_fragment == ["Inizio "]
    assert provider_saw_close, "the connection to the provider stayed open: the generation was not stopped"
    assert row is not None, "the answer stayed PENDING after the client left"
    assert (row["status"], row["error_code"], row["content"]) == ("FAILED", "interrupted", "")
    assert await sources_of(chat.engine, row["id"]) == []
    assert await pending_count(chat.engine) == 0
    (user_message,) = await fetch(chat.engine, "select * from messages where role = 'user'")
    assert (user_message["status"], user_message["content"]) == ("COMPLETED", QUESTION)  # the question is kept


async def test_AC_033_after_the_client_left_the_conversation_takes_a_new_question(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.script = [delta_event("Inizio "), *([b": slow\n\n", 0.1] * 600)]
    conversation: list[str] = []

    async def settled_answer() -> bool:
        rows = await fetch(chat.engine, "select status from messages where role = 'assistant'")
        return bool(rows) and rows[0]["status"] != "PENDING"

    with running(chat.settings_with()) as base_url:
        api = await signed_in(base_url)
        try:
            async with api.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                async for name, data in events(response):
                    if name == "conversation":
                        conversation.append(data["conversation_id"])
                    if name == "delta":
                        break
            assert await eventually(settled_answer), "the answer stayed PENDING after the client left"
            chat.provider.script = None  # the provider is itself again
            again = await api.post(f"/api/conversations/{conversation[0]}/messages", json={"question": "quokka bis"})
        finally:
            await api.aclose()

    assert again.status_code == 200, again.text  # not 409 answer_in_progress
    assert names_of(events_of(again))[-1] == "done"


# AC-031: one answer at a time, while an answer is really being generated ------------------------------------------


async def wait_for_provider(chat: Chat, requests: int = 1) -> None:
    async def got() -> bool:
        return len(chat.provider.completions()) >= requests

    assert await eventually(got), "the provider got no request"


async def test_AC_031_a_second_question_during_an_answer_gets_409_in_json_and_another_conversation_proceeds(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    busy = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "prima"), ("assistant", "ok"))
    chat.provider.delay_seconds = 30.0  # the answers wait for the provider until the test releases it

    async with chat.api("mario") as api:
        generating = asyncio.create_task(ask(api, busy, QUESTION))
        await wait_for_provider(chat)
        assert await pending_count(chat.engine) == 1
        before = await table_counts(chat.engine)
        refused = await ask(api, busy, "quokka seconda")
        assert await table_counts(chat.engine) == before  # nothing of the refused question was saved
        assert not generating.done()  # the first answer is still being generated
        other = asyncio.create_task(ask_new(api, chat.kb_a, "quokka altra conversazione"))
        await wait_for_provider(chat, 2)  # the question in another conversation goes to the provider: it proceeds
        assert await pending_count(chat.engine) == 2
        chat.provider.release.set()
        first = await asyncio.wait_for(generating, SECONDS)
        second = await asyncio.wait_for(other, SECONDS)

    assert refused.status_code == 409
    assert refused.headers["content-type"].startswith("application/json")  # before any stream
    assert error_of(refused)[0] == "answer_in_progress"
    assert names_of(events_of(first))[-1] == "done"
    assert names_of(events_of(second))[-1] == "done"
    assert len(chat.provider.completions()) == 2  # the refused question never reached the provider
    texts = [m["content"] for m in await messages_of(chat.engine, busy)]
    assert "quokka seconda" not in texts  # nothing of the refused question was saved
    assert len(texts) == 4
    assert await pending_count(chat.engine) == 0


async def test_AC_031_eight_truly_concurrent_questions_in_one_conversation_one_is_answered_the_others_get_409(
    chat: Chat,
) -> None:
    """Real sockets, a real uvicorn, eight requests at once: the partial unique index decides, so exactly one question
    is answered and each of the other seven gets 409 `answer_in_progress` with nothing saved."""
    await seed(chat, [chunk_row(0)])
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "prima"), ("assistant", "ok")
    )
    chat.provider.delay_seconds = 30.0  # the winner stays in progress while the others are refused

    with running(chat.settings_with()) as base_url:
        clients = [await signed_in(base_url) for _ in range(8)]
        try:
            tasks = [
                asyncio.create_task(
                    client.post(f"/api/conversations/{conversation}/messages", json={"question": f"quokka {number}"})
                )
                for number, client in enumerate(clients)
            ]
            refused, waiting = await asyncio.wait(tasks, timeout=SECONDS)
            chat.provider.release.set()
            answered = await asyncio.gather(*waiting)
        finally:
            chat.provider.release.set()
            for client in clients:
                await client.aclose()

    assert len(refused) == 7, "seven of the eight requests had to be refused while the winner waits for the provider"
    assert len(answered) == 1
    for task in refused:
        response = task.result()
        assert response.status_code == 409, response.text
        assert error_of(response)[0] == "answer_in_progress"
        assert response.headers["content-type"].startswith("application/json")
    assert names_of(events_of(answered[0]))[-1] == "done"
    saved = await messages_of(chat.engine, conversation)
    assert [(m["role"], m["status"]) for m in saved] == [("user", "COMPLETED"), ("assistant", "COMPLETED")] * 2
    assert len(chat.provider.completions()) == 1
    assert await pending_count(chat.engine) == 0


# AC-036: the answers left PENDING when the backend stops are FAILED `interrupted` at the next start ---------------


@pytest.fixture
def backend_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[int]]:
    """The environment of `python -m app.run` on the test database, with the settings of the `chat` world (the active
    indexing profile exists already); uvicorn is replaced by a function that records what the database holds at the
    moment it would start (the list has one number per start: the PENDING answers at that moment)."""
    monkeypatch.chdir(tmp_path)  # no .env file is read
    for name in (
        "POSTGRES_PASSWORD",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_IDLE_TIMEOUT_MINUTES",
        "EMBEDDING_TIMEOUT_SECONDS",
        "CHUNK_TARGET_WORDS",
        "CHUNK_MIN_WORDS",
        "CHUNK_MAX_WORDS",
        "CHUNK_OVERLAP_PERCENT",
        "GENERATION_BASE_URL",
        "GENERATION_MODEL",
        "GENERATION_API_KEY",
        "GENERATION_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DOCLING_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EMBEDDING_MODEL", "model-a")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "8")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    pending_at_start: list[int] = []

    async def count_pending() -> int:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            return await pending_count(engine)
        finally:
            await engine.dispose()

    def uvicorn_run(*_: Any, **__: Any) -> None:
        pending_at_start.append(asyncio.run(count_pending()))

    monkeypatch.setattr(run.uvicorn, "run", uvicorn_run)
    get_settings.cache_clear()
    yield pending_at_start
    get_settings.cache_clear()


async def start_backend() -> None:
    """The real entry point of the backend (migrations, bootstrap, then uvicorn), run in a thread because it has an
    event loop of its own."""
    await asyncio.to_thread(run.main)


async def add_pending(chat: Chat, user: str, kb_id: Any, question: str) -> Any:
    return await add_conversation(
        chat.engine, kb_id, chat.users[user], ("user", question), ("assistant", "", "PENDING"), title=question
    )


async def test_AC_036_an_answer_left_pending_is_failed_interrupted_when_the_backend_starts_before_uvicorn(
    chat: Chat, backend_env: list[int]
) -> None:
    secret = "TESTO-RISERVATO-DELLA-DOMANDA-7731"
    pending_one = await add_pending(chat, "mario", chat.kb_a, secret)
    pending_two = await add_pending(chat, "luigi", chat.kb_b, "domanda in sospeso")
    completed = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "u"), ("assistant", "risposta", "COMPLETED")
    )
    failed = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "u"),
        ("assistant", "", "FAILED", "generation_timeout"),
    )
    before = {key: await messages_of(chat.engine, key) for key in (pending_one, pending_two, completed, failed)}

    with all_logs() as logs:
        await start_backend()

    assert backend_env == [0], "answers were still PENDING when uvicorn would have started"
    for conversation in (pending_one, pending_two):
        asked, answer = await messages_of(chat.engine, conversation)
        assert asked == before[conversation][0]  # the question is untouched
        assert (answer["status"], answer["error_code"], answer["content"], answer["notice"]) == (
            "FAILED",
            "interrupted",
            "",
            None,
        )
    assert await messages_of(chat.engine, completed) == before[completed]
    assert await messages_of(chat.engine, failed) == before[failed]  # its own code stays
    assert await pending_count(chat.engine) == 0
    assert secret not in "\n".join(line for _, line in logs.lines)  # the logs hold the count, never a text


async def test_AC_036_a_pending_answer_of_the_stopped_backend_does_not_block_a_new_question(
    chat: Chat, backend_env: list[int]
) -> None:
    await seed(chat, [chunk_row(0)])
    stale = await add_pending(chat, "mario", chat.kb_a, "domanda rimasta in sospeso")
    async with chat.api("mario") as api:
        assert (await ask(api, stale, QUESTION)).status_code == 409  # before the restart: the answer is in progress

    await start_backend()

    async with chat.api("mario") as api:
        answered = await ask(api, stale, QUESTION)
    assert answered.status_code == 200, answered.text
    assert names_of(events_of(answered))[-1] == "done"
    statuses = [(m["role"], m["status"], m["error_code"]) for m in await messages_of(chat.engine, stale)]
    assert statuses == [
        ("user", "COMPLETED", None),
        ("assistant", "FAILED", "interrupted"),
        ("user", "COMPLETED", None),
        ("assistant", "COMPLETED", None),
    ]


async def test_AC_036_mark_interrupted_fails_every_pending_answer_and_only_those_and_tells_how_many(chat: Chat) -> None:
    for user, kb in (("mario", chat.kb_a), ("luigi", chat.kb_a), ("luigi", chat.kb_b)):
        await add_pending(chat, user, kb, "in sospeso")
    done = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "u"), ("assistant", "risposta", "COMPLETED")
    )

    async with chat.sessions()() as session:
        assert await ConversationRepository(session).mark_interrupted() == 3
    async with chat.sessions()() as session:
        assert await ConversationRepository(session).mark_interrupted() == 0  # nothing left to do

    rows = await fetch(chat.engine, "select status, error_code, content from messages where role = 'assistant'")
    assert sorted((r["status"], r["error_code"], r["content"]) for r in rows) == sorted(
        [("FAILED", "interrupted", "")] * 3 + [("COMPLETED", None, "risposta")]
    )
    assert [m["status"] for m in await messages_of(chat.engine, done)] == ["COMPLETED", "COMPLETED"]
