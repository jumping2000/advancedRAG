# SPEC-2026-11-20-fase-4-rerank-rewrite
"""A follow-up question is searched in its standalone form (T-02): AC-003, AC-004, AC-008, AC-009, AC-010, AC-011,
AC-012, AC-018, AC-027 (REQ-003, REQ-005, REQ-006, REQ-007, REQ-010, REQ-101, REQ-103), through the API against the
real PostgreSQL (ParadeDB) of compose.test.yaml, the fake Ollama, the fake generation provider and the fake rewriting
service (real local sockets, never a real service).

The conversation has one completed exchange on «Quanto costa il prodotto X?»; the follow-up question is «e la
spedizione?» and the fake rewrites it as «Quanto costa la spedizione del prodotto X?». The chunks get vectors ranked
from the REWRITTEN text (`chunk_row(..., question=REWRITTEN)`), and only the rewritten text holds words of the chunk of
the price («costa», «prodotto»): the ranks saved with the sources tell which text the vector search and the BM25 got.

Review focus of T-02: a rewritten text over several lines, with quotes or a preamble is searched and shown as it is; a
history that the rules of phase 3 leave empty is not rewritten; a rewritten text with a NUL, a lone surrogate or the
special characters of BM25 never gives a 500.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat.rewrite import REWRITE_INSTRUCTIONS
from app.main import create_app
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import FakeRewriteProvider, fake_rewrite_provider
from tests.test_chat_ask import (
    Chat,
    add_conversation,
    ask,
    ask_new,
    chat_world,
    chunk_row,
    done_message,
    events_of,
    messages_of,
    names_of,
    request_messages,
    seed,
    sources_of,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import outgoing as outgoing  # noqa: F401  (the HTTP requests the backend sends)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_knowledge_bases import ADMIN, ADMIN_PASSWORD, PASSWORD, login
from tests.test_logging_privacy import all_logs

REWRITE_MODEL = "modello-riscrittura"
REWRITE_KEY = "sk-chiave-riscrittura-t02"
FIRST = "Quanto costa il prodotto X?"
FIRST_ANSWER = "Il prodotto X costa dieci euro [1]."
FOLLOW_UP = "e la spedizione?"
REWRITTEN = "Quanto costa la spedizione del prodotto X?"
PRICE = "Listino: prodotto X costa dieci euro"
OTHER = "Garanzia di due anni"
SETTINGS = "/api/admin/settings"
CLOSED = "closed port"  # in the settings of a case: a URL where nothing listens, taken when the test runs


# State ---------------------------------------------------------------------------------------------------------------


async def store_switches(engine: AsyncEngine, rerank: bool, rewrite: bool) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("update app_settings set rerank_enabled = :rerank, rewrite_enabled = :rewrite"),
            {"rerank": rerank, "rewrite": rewrite},
        )


@dataclass
class World:
    chat: Chat
    rewriter: FakeRewriteProvider

    def configured(self, **overrides: Any) -> dict[str, Any]:
        """The settings of the rewriting service (the fake), plus `overrides`."""
        values: dict[str, Any] = {
            "rewrite_base_url": self.rewriter.url,
            "rewrite_model": REWRITE_MODEL,
            "rewrite_api_key": REWRITE_KEY,
        }
        values.update(overrides)
        return values

    def api(self, username: str = "mario", **overrides: Any) -> Any:
        return self.chat.api(username, **self.configured(**overrides))


@pytest.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:  # noqa: F811
    """The world of test_chat_ask plus the fake rewriting service; both switches off at the start (a new database) and
    at the end."""
    await store_switches(engine, rerank=False, rewrite=False)
    try:
        async with chat_world(engine) as chat:
            with fake_rewrite_provider(answer=REWRITTEN) as rewriter:
                yield World(chat, rewriter)
    finally:
        await store_switches(engine, rerank=False, rewrite=False)


async def rewrite_on(world: World) -> None:
    await store_switches(world.chat.engine, rerank=False, rewrite=True)


async def seed_price(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    """The chunk of the price (nearest to the rewritten text, the only one with its words) and another one."""
    price, other = await seed(world.chat, [chunk_row(0, PRICE, REWRITTEN), chunk_row(1, OTHER, REWRITTEN)])
    return price, other


async def follow_up_conversation(world: World, *earlier: tuple[Any, ...], user: str = "mario") -> uuid.UUID:
    turns = earlier or (("user", FIRST), ("assistant", FIRST_ANSWER))
    return await add_conversation(world.chat.engine, world.chat.kb_a, world.chat.users[user], *turns)


@asynccontextmanager
async def same_app(world: World, **overrides: Any) -> AsyncIterator[Callable[[str], Awaitable[httpx.AsyncClient]]]:
    """One app (no restart in between), and a factory of clients signed in to it as a given user."""
    app = create_app(world.chat.settings_with(**world.configured(**overrides)))
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def signed_in(username: str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        password = ADMIN_PASSWORD if username == ADMIN else PASSWORD
        assert (await login(client, username, password)).status_code == 200
        return client

    try:
        yield signed_in
    finally:
        for client in clients:
            await client.aclose()
        await app.state.engine.dispose()
        await app.state.redis.aclose()


def with_closed_port(settings: dict[str, Any]) -> dict[str, Any]:
    return {k: closed_port_url() + "/v1" if v == CLOSED else v for k, v in settings.items()}


def search_query_of(message: dict[str, Any]) -> Any:
    assert "search_query" in message, message
    return message["search_query"]


async def saved_answer(world: World, conversation_id: Any) -> dict[str, Any]:
    return (await messages_of(world.chat.engine, conversation_id))[-1]


async def detail(api: httpx.AsyncClient, conversation_id: Any) -> dict[str, Any]:
    response = await api.get(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def retry(api: httpx.AsyncClient, conversation_id: Any, message_id: Any) -> httpx.Response:
    return await api.post(f"/api/conversations/{conversation_id}/messages/{message_id}/retry")


def last_question_sent(world: World, position: int = 0) -> str:
    return request_messages(world.chat, position)[-1]["content"]


# AC-003: the switches start off and a change counts without a restart ------------------------------------------------


async def test_AC_003_a_new_database_has_both_switches_off_and_turning_rewrite_on_counts_at_once(world: World) -> None:
    await seed_price(world)
    conversation = await follow_up_conversation(world)

    async with same_app(world) as signed_in:
        admin = await signed_in(ADMIN)
        mario = await signed_in("mario")
        before = (await admin.get(SETTINGS)).json()
        assert names_of(events_of(await ask(mario, conversation, FOLLOW_UP)))[-1] == "done"
        assert world.rewriter.requests == []  # off: nothing reaches the service
        turned = await admin.put(SETTINGS, json={"rewrite_enabled": True})
        events = events_of(await ask(mario, conversation, FOLLOW_UP))

    assert before["rerank"]["enabled"] is False
    assert before["rewrite"] == {"enabled": False, "configured": True}
    assert turned.status_code == 200, turned.text
    assert len(world.rewriter.completions()) == 1
    assert search_query_of(done_message(events)) == REWRITTEN


# AC-004: switched on but no longer configured ------------------------------------------------------------------------


async def test_AC_004_switched_on_and_then_not_configured_sends_nothing_and_the_api_says_so(
    world: World,
    outgoing: list[tuple[str, str, bytes]],  # noqa: F811
) -> None:
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    # the restart without REWRITE_BASE_URL: a new app with the service not configured
    async with same_app(world, rewrite_base_url="", rewrite_model="", rewrite_api_key=None) as signed_in:
        mario = await signed_in("mario")
        events = events_of(await ask(mario, conversation, FOLLOW_UP))
        state = (await (await signed_in(ADMIN)).get(SETTINGS)).json()

    message = done_message(events)
    assert message["status"] == "COMPLETED"
    assert search_query_of(message) is None
    assert world.rewriter.requests == []
    assert not [url for _, url, _ in outgoing if url.startswith(world.rewriter.url)]
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]
    assert state["rewrite"] == {"enabled": True, "configured": False}


# AC-008: the rewritten text is searched, the original question is answered ------------------------------------------


async def test_AC_008_ollama_and_bm25_get_the_rewritten_text_and_the_provider_the_original_question(
    world: World,
) -> None:
    price, other = await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        events = events_of(await ask(api, conversation, FOLLOW_UP))

    message = done_message(events)
    assert message["status"] == "COMPLETED"
    assert world.chat.ollama.texts_sent() == [REWRITTEN]
    stored = await sources_of(world.chat.engine, message["id"])
    # dense ranks from the vector of the rewritten text; a BM25 rank only for the words of the rewritten text
    assert [(r["chunk_id"], r["dense_rank"], r["bm25_rank"]) for r in stored] == [(price, 1, 1), (other, 2, None)]
    assert last_question_sent(world).endswith(f"Question: {FOLLOW_UP}")
    assert REWRITTEN not in json.dumps(request_messages(world.chat), ensure_ascii=False)


async def test_AC_008_without_the_rewriting_the_same_question_finds_no_bm25_match(world: World) -> None:
    """The control of the test above: the original question has no word of the chunks."""
    await seed_price(world)
    conversation = await follow_up_conversation(world)

    async with world.api() as api:
        events = events_of(await ask(api, conversation, FOLLOW_UP))

    stored = await sources_of(world.chat.engine, done_message(events)["id"])
    assert [r["bm25_rank"] for r in stored] == [None, None]
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]


# AC-009: what the rewriting service receives -------------------------------------------------------------------------


@pytest.mark.parametrize(("limit", "expected"), [(6, ["q1", "a1", "q4", "a4"]), (2, ["q4", "a4"])])
async def test_AC_009_the_request_has_the_instructions_the_history_of_phase_3_and_the_question(
    world: World, limit: int, expected: list[str]
) -> None:
    await seed_price(world)
    conversation = await follow_up_conversation(
        world,
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2"),
        ("assistant", "", "FAILED", "generation_error"),  # a failed exchange: left out with its question
        ("user", "q3"),
        ("assistant", ""),  # an answer without text (a notice): left out with its question
        ("user", "q4"),
        ("assistant", "a4"),
    )
    await rewrite_on(world)

    async with world.api(chat_history_messages=limit) as api:
        assert names_of(events_of(await ask(api, conversation, FOLLOW_UP)))[-1] == "done"

    (request,) = world.rewriter.completions()
    assert request.authorization == f"Bearer {REWRITE_KEY}"
    assert request.body["model"] == REWRITE_MODEL
    assert request.body["stream"] is False
    messages = request.body["messages"]
    assert messages[0] == {"role": "system", "content": REWRITE_INSTRUCTIONS}
    assert [m["content"] for m in messages[1:-1]] == expected
    assert [m["role"] for m in messages[1:-1]] == ["user", "assistant"] * (len(expected) // 2)
    assert messages[-1] == {"role": "user", "content": FOLLOW_UP}
    # the same history as the request to the generation provider
    assert messages[1:-1] == request_messages(world.chat)[1:-1]


# AC-010: no rewriting without a previous exchange or without anything to search ------------------------------------


async def test_AC_010_the_first_question_of_a_conversation_is_not_rewritten(world: World) -> None:
    await seed_price(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask_new(api, world.chat.kb_a, FOLLOW_UP)))

    assert message["status"] == "COMPLETED"
    assert search_query_of(message) is None
    assert world.rewriter.requests == []
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]


@pytest.mark.parametrize("status", [None, "PROCESSING", "FAILED"])
async def test_AC_010_a_follow_up_on_a_kb_without_ready_documents_calls_no_service(
    world: World,
    status: str | None,
    outgoing: list[tuple[str, str, bytes]],  # noqa: F811
) -> None:
    if status is not None:
        await seed(world.chat, [chunk_row(0, PRICE, REWRITTEN)], status=status)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert message["notice"] == "no_documents"
    assert search_query_of(message) is None
    assert world.rewriter.requests == []
    assert outgoing == []


@pytest.mark.parametrize(
    ("earlier", "limit"),
    [
        ((("user", FIRST), ("assistant", "", "FAILED", "generation_error")), 6),
        ((("user", FIRST), ("assistant", "")), 6),  # an answer saved with a notice has no text
        ((("user", FIRST), ("assistant", "", "FAILED", "interrupted"), ("user", "altro"), ("assistant", "  ")), 6),
        ((("user", FIRST), ("assistant", FIRST_ANSWER)), 0),  # CHAT_HISTORY_MESSAGES=0 sends no history
    ],
)
async def test_a_history_that_the_rules_of_phase_3_leave_empty_is_not_rewritten(
    world: World, earlier: tuple[tuple[Any, ...], ...], limit: int
) -> None:
    """Review focus: the same as a first question."""
    await seed_price(world)
    conversation = await follow_up_conversation(world, *earlier)
    await rewrite_on(world)

    async with world.api(chat_history_messages=limit) as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert message["status"] == "COMPLETED"
    assert search_query_of(message) is None
    assert world.rewriter.requests == []
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]


# AC-011: the fallback on the original question -----------------------------------------------------------------------


FAILURES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    # (modes of the fake, settings)
    "unreachable": ({}, {"rewrite_base_url": CLOSED}),
    "500": ({"fail_status": 500}, {}),
    "slower than the timeout": ({"delay_seconds": 5}, {"rewrite_timeout_seconds": 1}),
    "empty text": ({"answer": "   "}, {}),
    "4001 characters": ({"answer": "x" * 4001}, {}),
    "not a completion": ({"raw_body": b'{"choices": []}'}, {}),
}


@pytest.mark.parametrize("failure", list(FAILURES))
async def test_AC_011_a_failed_rewriting_searches_the_original_question_and_the_answer_completes(
    world: World, failure: str
) -> None:
    modes, settings = FAILURES[failure]
    for name, value in modes.items():
        setattr(world.rewriter, name, value)
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api(**with_closed_port(settings)) as api:
        events = events_of(await ask(api, conversation, FOLLOW_UP))

    assert names_of(events)[-1] == "done"
    message = done_message(events)
    assert message["status"] == "COMPLETED"
    assert message["content"] == "Ciao mondo"
    assert search_query_of(message) is None
    assert (await saved_answer(world, conversation))["search_query"] is None
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]
    assert last_question_sent(world).endswith(f"Question: {FOLLOW_UP}")


@pytest.mark.parametrize(
    "rewritten", ["Quanto\x00 costa la spedizione?", "Quanto \ud800 costa la spedizione?"], ids=["NUL", "surrogate"]
)
async def test_a_rewritten_text_that_the_database_cannot_hold_falls_back_without_a_500(
    world: World, rewritten: str
) -> None:
    """Review focus: the checks of a question of phase 3 hold for the rewritten text too."""
    world.rewriter.answer = rewritten
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        response = await ask(api, conversation, FOLLOW_UP)

    message = done_message(events_of(response))
    assert message["status"] == "COMPLETED"
    assert search_query_of(message) is None
    assert world.chat.ollama.texts_sent() == [FOLLOW_UP]


# AC-012: search_query in the done event and in the conversation -------------------------------------------------------


async def test_AC_012_the_rewritten_text_is_the_search_query_of_done_of_the_detail_and_of_the_saved_answer(
    world: World,
) -> None:
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))
        shown = (await detail(api, conversation))["messages"]

    assert search_query_of(message) == REWRITTEN
    assert [(m["role"], search_query_of(m)) for m in shown] == [
        ("user", None),
        ("assistant", None),
        ("user", None),
        ("assistant", REWRITTEN),
    ]
    assert (await saved_answer(world, conversation))["search_query"] == REWRITTEN


@pytest.mark.parametrize("rewritten", ["  E LA SPEDIZIONE?  ", FOLLOW_UP, "\tE La Spedizione?\n"])
async def test_AC_012_a_rewritten_text_equal_to_the_question_but_for_spaces_and_case_leaves_search_query_empty(
    world: World, rewritten: str
) -> None:
    world.rewriter.answer = rewritten
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))
        shown = (await detail(api, conversation))["messages"]

    assert search_query_of(message) is None
    assert search_query_of(shown[-1]) is None
    assert (await saved_answer(world, conversation))["search_query"] is None
    assert world.chat.ollama.texts_sent() == [rewritten.strip()]  # the text received, without the spaces at both ends


@pytest.mark.parametrize(
    "rewritten",
    [
        'Ecco la domanda: "Quanto costa la spedizione del prodotto X?"',
        "Quanto costa\nla spedizione del\nprodotto X?",
        "«Quanto costa la spedizione?» (riscritta)",
    ],
)
async def test_a_rewritten_text_with_lines_quotes_or_a_preamble_is_searched_and_shown_as_it_is(
    world: World, rewritten: str
) -> None:
    """Review focus: what is searched is what the user sees in «Ricerca: …»."""
    world.rewriter.answer = f"\n {rewritten} \n"
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert search_query_of(message) == rewritten
    assert world.chat.ollama.texts_sent() == [rewritten]
    assert (await saved_answer(world, conversation))["search_query"] == rewritten


async def test_a_rewritten_text_with_the_special_characters_of_bm25_is_searched_without_a_500(world: World) -> None:
    """Review focus: the BM25 search reads the rewritten text as plain text, like a question of phase 3."""
    rewritten = 'prodotto AND "X" OR (costa)* : NOT ~1^2 [a TO b] {x} \\ / + - && || !'
    world.rewriter.answer = rewritten
    price, _ = await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert message["status"] == "COMPLETED"
    assert search_query_of(message) == rewritten
    stored = await sources_of(world.chat.engine, message["id"])
    assert {r["chunk_id"]: r["bm25_rank"] for r in stored}[price] == 1  # its words were searched


# AC-018: the retry rewrites with the switches of the retry ------------------------------------------------------------


async def failed_follow_up(world: World, search_query: str | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    conversation = await follow_up_conversation(
        world,
        ("user", FIRST),
        ("assistant", FIRST_ANSWER),
        ("user", FOLLOW_UP),
        ("assistant", "", "FAILED", "generation_error"),
    )
    failed = await saved_answer(world, conversation)
    if search_query is not None:
        async with world.chat.engine.begin() as connection:
            await connection.execute(
                text("update messages set search_query = :query where id = :id"),
                {"query": search_query, "id": failed["id"]},
            )
    return conversation, failed["id"]


async def test_AC_018_after_the_admin_turns_rewrite_on_the_retry_is_rewritten_and_saves_the_search_query(
    world: World,
) -> None:
    await seed_price(world)
    conversation, failed = await failed_follow_up(world)

    async with same_app(world) as signed_in:
        admin = await signed_in(ADMIN)
        assert (await admin.put(SETTINGS, json={"rewrite_enabled": True})).status_code == 200
        events = events_of(await retry(await signed_in("mario"), conversation, failed))

    message = done_message(events)
    assert message["id"] == str(failed)
    assert message["status"] == "COMPLETED"
    assert search_query_of(message) == REWRITTEN
    (request,) = world.rewriter.completions()
    messages = request.body["messages"]
    assert [m["content"] for m in messages[1:]] == [FIRST, FIRST_ANSWER, FOLLOW_UP]  # not the failed exchange
    assert world.chat.ollama.texts_sent() == [REWRITTEN]
    assert (await saved_answer(world, conversation))["search_query"] == REWRITTEN


async def test_AC_018_a_retry_with_rewrite_off_leaves_no_search_query_of_an_earlier_attempt(world: World) -> None:
    await seed_price(world)
    conversation, failed = await failed_follow_up(world, search_query="Una ricerca di un tentativo precedente")

    async with world.api() as api:
        message = done_message(events_of(await retry(api, conversation, failed)))

    assert search_query_of(message) is None
    assert (await saved_answer(world, conversation))["search_query"] is None
    assert world.rewriter.requests == []


async def test_AC_018_a_retry_whose_rewriting_fails_leaves_no_search_query_of_an_earlier_attempt(world: World) -> None:
    world.rewriter.fail_status = 503
    await seed_price(world)
    conversation, failed = await failed_follow_up(world, search_query="Una ricerca di un tentativo precedente")
    await rewrite_on(world)

    async with world.api() as api:
        message = done_message(events_of(await retry(api, conversation, failed)))

    assert message["status"] == "COMPLETED"
    assert search_query_of(message) is None
    assert (await saved_answer(world, conversation))["search_query"] is None
    assert len(world.rewriter.completions()) == 1


# AC-027: with the switches off the chat is the one of phase 3 ---------------------------------------------------------


def comparable(events: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """The events without the ids and the times, which differ between two conversations."""
    shown: list[tuple[str, Any]] = []
    for name, data in events:
        if name == "conversation":
            shown.append((name, sorted(data)))
        elif name == "done":
            message = {k: v for k, v in data["message"].items() if k not in ("id", "created_at")}
            shown.append((name, message))
        else:
            shown.append((name, data))
    return shown


async def saved_state(world: World, conversation: uuid.UUID) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    messages = await messages_of(world.chat.engine, conversation)
    rows = [(m["role"], m["content"], m["status"], m["error_code"], m["notice"], m["search_query"]) for m in messages]
    sources = await sources_of(world.chat.engine, messages[-1]["id"])
    cited = [
        (s["number"], s["chunk_id"], s["document_name"], s["dense_rank"], s["bm25_rank"], s["score"], s["cited"])
        for s in sources
    ]
    return rows, cited


async def test_AC_027_configured_services_with_the_switches_off_change_nothing_of_phase_3(
    world: World,
    outgoing: list[tuple[str, str, bytes]],  # noqa: F811
) -> None:
    await seed_price(world)
    world.chat.provider.fragments = ["Costa ", "[1]", "."]
    with_services = await follow_up_conversation(world)
    without = await follow_up_conversation(world)
    reranker = closed_port_url()

    async with world.api(rerank_base_url=reranker) as api:  # both services configured, both switches off
        configured = events_of(await ask(api, with_services, FOLLOW_UP))
    async with world.chat.api("mario") as api:  # neither service configured (phase 3)
        phase_3 = events_of(await ask(api, without, FOLLOW_UP))

    assert comparable(configured) == comparable(phase_3)
    assert search_query_of(done_message(configured)) is None
    first, second = world.chat.ollama.requests
    assert first == second
    first_body, second_body = (request.body for request in world.chat.provider.completions())
    assert first_body == second_body
    assert await saved_state(world, with_services) == await saved_state(world, without)
    assert (await saved_answer(world, with_services))["search_query"] is None
    assert world.rewriter.requests == []
    assert [url for _, url, _ in outgoing if url.startswith((world.rewriter.url, reranker))] == []


# REQ-101: the log line of the step -----------------------------------------------------------------------------------


def rewrite_lines(lines: Sequence[tuple[str, str]]) -> list[str]:
    return [line for _, line in lines if line.startswith("chat rewrite ")]


@pytest.mark.parametrize(
    ("modes", "settings", "used", "reason"),
    [
        ({}, {}, "true", "none"),
        ({}, {"rewrite_base_url": CLOSED}, "false", "unreachable"),
        ({"fail_status": 500}, {}, "false", "http_error"),
        ({"delay_seconds": 5}, {"rewrite_timeout_seconds": 1}, "false", "timeout"),
        ({"answer": ""}, {}, "false", "invalid_response"),
    ],
)
async def test_the_rewrite_line_has_the_ids_the_use_and_the_reason_and_no_text(
    world: World, modes: dict[str, Any], settings: dict[str, Any], used: str, reason: str
) -> None:
    for name, value in modes.items():
        setattr(world.rewriter, name, value)
    await seed_price(world)
    conversation = await follow_up_conversation(world)
    await rewrite_on(world)

    with all_logs() as logs:
        async with world.api(**with_closed_port(settings)) as api:
            message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    (line,) = rewrite_lines(logs.lines)
    ids = f"conversation={conversation} message={message['id']} kb={world.chat.kb_a}"
    assert re.fullmatch(rf"chat rewrite {re.escape(ids)} used={used} reason={reason} ms=\d+", line), line
    everything = "\n".join(text for _, text in logs.lines)
    for secret in (FOLLOW_UP, REWRITTEN, FIRST, FIRST_ANSWER, PRICE, REWRITE_KEY):
        assert secret not in everything


async def test_without_the_step_there_is_no_rewrite_line(world: World) -> None:
    await seed_price(world)
    conversation = await follow_up_conversation(world)

    with all_logs() as logs:
        async with world.api() as api:
            assert names_of(events_of(await ask(api, conversation, FOLLOW_UP)))[-1] == "done"

    assert rewrite_lines(logs.lines) == []
