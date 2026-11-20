# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The reranker reorders the first candidates of the fusion (T-03): AC-005, AC-014, AC-015, AC-016, AC-017 (REQ-003,
REQ-008, REQ-009, REQ-010, REQ-101), through the API against the real PostgreSQL (ParadeDB) of compose.test.yaml, the
fake Ollama, the fake generation provider, the fake rewriting service and the fake reranker (real local sockets, never
a real service).

Five chunks a, b, c, d, e get vectors ranked from the question, which has no word of them (no BM25 match): the fusion is
[a, b, c, d, e], with dense ranks 1 to 5 and RRF scores 1 / (60 + rank). The fake reranker scores a text by the word it
holds: alfa (a) 0.5, beta (b) 0.1, gamma (c) 0.9. With `RERANK_CANDIDATES` 3 and `CHAT_CONTEXT_CHUNKS` 4 the reranker
gets the texts of a, b, c and the context is [c, a, b, d]; on a fallback it is [a, b, c, d] (the RRF order).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tests.fakes.http_server import closed_port_url
from tests.fakes.services import FakeReranker, FakeRewriteProvider, fake_reranker, fake_rewrite_provider
from tests.test_chat_ask import (
    QUESTION,
    Chat,
    add_conversation,
    ask,
    ask_new,
    chat_world,
    chunk_row,
    context_of,
    done_message,
    events_of,
    messages_of,
    names_of,
    request_messages,
    seed,
    sources_of,
    table_counts,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_rewrite import store_switches
from tests.test_chat_streaming import SECONDS, eventually
from tests.test_knowledge_bases import ADMIN, ADMIN_PASSWORD, PASSWORD, login
from tests.test_logging_privacy import Collector, all_logs

RERANK_MODEL = "modello-reranker"
RERANK_KEY = "sk-chiave-reranker-t03"
REWRITE_MODEL = "modello-riscrittura"
REWRITE_KEY = "sk-chiave-riscrittura-t03"
A = "Paragrafo alfa del listino"
B = "Paragrafo beta della garanzia"
C = "Paragrafo gamma della spedizione"
D = "Paragrafo delta del montaggio"
E = "Paragrafo epsilon della manutenzione"
SCORES = {"alfa": 0.5, "beta": 0.1, "gamma": 0.9}
FIRST = "Quanto costa il prodotto X?"
FIRST_ANSWER = "Il prodotto X costa dieci euro [1]."
FOLLOW_UP = "e la spedizione?"
REWRITTEN = "Quanto costa la spedizione del prodotto X?"
SETTINGS = "/api/admin/settings"
CLOSED = "closed port"  # in the settings of a case: a URL where nothing listens, taken when the test runs


# State ---------------------------------------------------------------------------------------------------------------


@dataclass
class World:
    chat: Chat
    reranker: FakeReranker
    rewriter: FakeRewriteProvider

    def configured(self, reranker: FakeReranker | None = None, **overrides: Any) -> dict[str, Any]:
        """The settings of both services (the fakes; the `tei` reranker unless another is given), 3 candidates to
        rerank and 4 chunks of context, plus `overrides`."""
        used = reranker or self.reranker
        values: dict[str, Any] = {
            "rerank_base_url": used.url,
            "rerank_api": used.api,
            "rerank_model": RERANK_MODEL,
            "rerank_api_key": RERANK_KEY,
            "rerank_candidates": 3,
            "chat_context_chunks": 4,
            "rewrite_base_url": self.rewriter.url,
            "rewrite_model": REWRITE_MODEL,
            "rewrite_api_key": REWRITE_KEY,
        }
        values.update(overrides)
        if values["rerank_base_url"] == CLOSED:
            values["rerank_base_url"] = closed_port_url()
        return values

    def api(self, username: str = "mario", reranker: FakeReranker | None = None, **overrides: Any) -> Any:
        return self.chat.api(username, **self.configured(reranker, **overrides))


@pytest.fixture
async def world(engine: AsyncEngine) -> AsyncIterator[World]:  # noqa: F811
    """The world of test_chat_ask plus the fake reranker (`tei`, scores SCORES) and the fake rewriting service; both
    switches off at the start (a new database) and at the end."""
    await store_switches(engine, rerank=False, rewrite=False)
    try:
        async with chat_world(engine) as chat:
            with fake_reranker("tei", scores=SCORES) as reranker, fake_rewrite_provider(answer=REWRITTEN) as rewriter:
                yield World(chat, reranker, rewriter)
    finally:
        await store_switches(engine, rerank=False, rewrite=False)


async def rerank_on(world: World, rewrite: bool = False) -> None:
    await store_switches(world.chat.engine, rerank=True, rewrite=rewrite)


async def seed_five(world: World) -> dict[str, uuid.UUID]:
    """The chunks a to e, nearest to the question in this order (the fusion is [a, b, c, d, e])."""
    ids = await seed(world.chat, [chunk_row(i, text) for i, text in enumerate((A, B, C, D, E))])
    return dict(zip("abcde", ids, strict=True))


async def follow_up_conversation(world: World, *earlier: tuple[Any, ...]) -> uuid.UUID:
    turns = earlier or (("user", FIRST), ("assistant", FIRST_ANSWER))
    return await add_conversation(world.chat.engine, world.chat.kb_a, world.chat.users["mario"], *turns)


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


def context_texts(world: World, position: int = 0) -> list[str]:
    blocks = context_of(world.chat, position)
    assert [block.number for block in blocks] == list(range(1, len(blocks) + 1))
    return [block.text for block in blocks]


async def stored_order(world: World, message_id: Any) -> list[uuid.UUID]:
    return [row["chunk_id"] for row in await sources_of(world.chat.engine, message_id)]


class Records(logging.Handler):
    """Every record of the logger it is added to, once."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def starting(self, prefix: str) -> list[logging.LogRecord]:
        return [record for record in self.records if record.getMessage().startswith(prefix)]


@contextmanager
def service_logs() -> Iterator[tuple[Records, Collector]]:
    """The records of `app.chat.service`, each collected once (the collector of `all_logs` sits on the root and on the
    `app` logger, so it holds each record twice: a line that the code wrote twice could not be told), and the lines of
    every logger collected by `all_logs`."""
    records = Records()
    logger = logging.getLogger("app.chat.service")
    with all_logs() as everything:
        logger.addHandler(records)
        try:
            yield records, everything
        finally:
            logger.removeHandler(records)


def rerank_lines(records: Records) -> list[str]:
    return [record.getMessage() for record in records.starting("chat rerank ")]


# AC-014: the `tei` format reorders the context ----------------------------------------------------------------------


async def test_AC_014_tei_gets_the_first_candidates_and_the_context_and_the_sources_follow_its_scores(
    world: World,
) -> None:
    ids = await seed_five(world)
    await rerank_on(world)

    async with world.api() as api:
        message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert message["status"] == "COMPLETED"
    (request,) = world.reranker.reranks()
    assert request.authorization == f"Bearer {RERANK_KEY}"
    # the content of the chunks (without the document name), in the RRF order, and `truncate: true`
    assert request.body == {"query": QUESTION, "texts": [A, B, C], "truncate": True}
    assert context_texts(world) == [C, A, B, D]
    stored = await sources_of(world.chat.engine, message["id"])
    assert [(row["number"], row["chunk_id"]) for row in stored] == [
        (1, ids["c"]),
        (2, ids["a"]),
        (3, ids["b"]),
        (4, ids["d"]),
    ]
    # the saved score stays the RRF one, with the ranks of the searches (plan assumption)
    assert [(row["dense_rank"], row["bm25_rank"]) for row in stored] == [(3, None), (1, None), (2, None), (4, None)]
    assert [row["score"] for row in stored] == pytest.approx([1 / 63, 1 / 61, 1 / 62, 1 / 64])


async def test_AC_014_the_cited_sources_of_done_follow_the_final_order(world: World) -> None:
    await seed(world.chat, [chunk_row(0, A), chunk_row(1, B), chunk_row(2, C)], filename="primo.pdf")
    await seed(world.chat, [chunk_row(3, D)], filename="secondo.pdf")
    world.reranker.scores = {"delta": 0.99, **SCORES}
    world.chat.provider.fragments = ["Vedi [1] e [2]."]
    await rerank_on(world)

    async with world.api(rerank_candidates=4) as api:
        message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert context_texts(world) == [D, C, A, B]
    assert [(s["number"], s["document_name"]) for s in message["sources"]] == [(1, "secondo.pdf"), (2, "primo.pdf")]


async def test_more_candidates_than_the_context_are_reranked_and_a_low_one_can_enter_it(world: World) -> None:
    """RERANK_CANDIDATES above CHAT_CONTEXT_CHUNKS: the candidates are loaded beyond the context."""
    ids = await seed_five(world)
    world.reranker.scores = {"epsilon": 0.95, **SCORES}
    await rerank_on(world)

    async with world.api(rerank_candidates=5, chat_context_chunks=2) as api:
        message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert world.reranker.texts_sent() == [[A, B, C, D, E]]
    assert context_texts(world) == [E, C]
    assert await stored_order(world, message["id"]) == [ids["e"], ids["c"]]


# AC-015: the `cohere` format ------------------------------------------------------------------------------------------


async def test_AC_015_cohere_gets_model_query_documents_top_n_and_the_key_and_reorders_as_tei(world: World) -> None:
    ids = await seed_five(world)
    await rerank_on(world)

    with fake_reranker("cohere", scores=SCORES) as cohere:
        async with world.api(reranker=cohere) as api:
            message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert message["status"] == "COMPLETED"
    (request,) = cohere.requests
    assert (request.method, request.path) == ("POST", "/v1/rerank")
    assert request.authorization == f"Bearer {RERANK_KEY}"
    assert request.body == {"model": RERANK_MODEL, "query": QUESTION, "documents": [A, B, C], "top_n": 3}
    assert context_texts(world) == [C, A, B, D]
    assert await stored_order(world, message["id"]) == [ids["c"], ids["a"], ids["b"], ids["d"]]
    assert world.reranker.requests == []


# AC-016: the query is the searched question; one candidate is not reranked -------------------------------------------


async def test_AC_016_with_both_steps_the_reranker_gets_the_rewritten_question_and_the_model_the_original(
    world: World,
) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    await rerank_on(world, rewrite=True)

    async with world.api() as api:
        message = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert message["search_query"] == REWRITTEN
    (request,) = world.reranker.reranks()
    assert request.body["query"] == REWRITTEN
    assert request_messages(world.chat)[-1]["content"].endswith(f"Question: {FOLLOW_UP}")


async def test_AC_016_without_the_rewriting_the_reranker_gets_the_question(world: World) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    await rerank_on(world)

    async with world.api() as api:
        done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert [request.body["query"] for request in world.reranker.reranks()] == [FOLLOW_UP]
    assert world.rewriter.requests == []


async def test_AC_016_a_fusion_of_one_chunk_is_not_sent_to_the_reranker(world: World) -> None:
    await seed(world.chat, [chunk_row(0, A)])
    await rerank_on(world)

    with service_logs() as (records, _):
        async with world.api() as api:
            message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert message["status"] == "COMPLETED"
    assert world.reranker.requests == []
    assert context_texts(world) == [A]
    assert rerank_lines(records) == []  # the reranker was not asked: no line


async def test_one_candidate_to_rerank_is_not_sent_to_the_reranker(world: World) -> None:
    """RERANK_CANDIDATES 1: a single candidate, nothing to reorder."""
    await seed_five(world)
    await rerank_on(world)

    async with world.api(rerank_candidates=1) as api:
        done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert world.reranker.requests == []
    assert context_texts(world) == [A, B, C, D]


async def test_a_fusion_shorter_than_the_candidates_sends_what_there_is(world: World) -> None:
    await seed(world.chat, [chunk_row(0, A), chunk_row(1, B)])
    await rerank_on(world)

    async with world.api(rerank_candidates=3) as api:
        done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert world.reranker.texts_sent() == [[A, B]]
    assert context_texts(world) == [A, B]  # alfa 0.5 before beta 0.1


# AC-017: the fallback on the RRF order --------------------------------------------------------------------------------


def tei(*items: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [{"index": index, "score": score} for index, score in items]


FAILURES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    # (modes of the fake, settings)
    "unreachable": ({}, {"rerank_base_url": CLOSED}),
    "500": ({"fail_status": 500}, {}),
    "slower than the timeout": ({"delay_seconds": 5}, {"rerank_timeout_seconds": 1}),
    "an index out of the candidates": ({"results": tei((0, 0.5), (1, 0.1), (3, 0.9))}, {}),
    "a repeated index": ({"results": tei((0, 0.5), (1, 0.1), (1, 0.9))}, {}),
    "a missing candidate": ({"results": tei((0, 0.5), (2, 0.9))}, {}),
    "a score that is not a number": ({"results": tei((0, 0.5), (1, "0.1"), (2, 0.9))}, {}),
    "a score NaN": ({"results": tei((0, 0.5), (1, float("nan")), (2, 0.9))}, {}),
    "a score Infinity": ({"results": tei((0, 0.5), (1, 0.1), (2, float("inf")))}, {}),
    "a boolean score": ({"results": tei((0, 0.5), (1, True), (2, 0.9))}, {}),
    "a body of the wrong shape": ({"json_body": {"results": []}}, {}),
}


@pytest.mark.parametrize("failure", list(FAILURES))
async def test_AC_017_a_failed_reranking_keeps_the_rrf_order_and_the_answer_completes(
    world: World, failure: str
) -> None:
    modes, settings = FAILURES[failure]
    for name, value in modes.items():
        setattr(world.reranker, name, value)
    ids = await seed_five(world)
    await rerank_on(world)

    async with world.api(**settings) as api:
        events = events_of(await ask_new(api, world.chat.kb_a, QUESTION))

    assert names_of(events)[-1] == "done"
    message = done_message(events)
    assert message["status"] == "COMPLETED"
    assert message["content"] == "Ciao mondo"
    assert context_texts(world) == [A, B, C, D]
    assert await stored_order(world, message["id"]) == [ids["a"], ids["b"], ids["c"], ids["d"]]


async def test_AC_017_a_failed_cohere_reranking_keeps_the_rrf_order(world: World) -> None:
    await seed_five(world)
    await rerank_on(world)

    with fake_reranker("cohere", results=[{"index": 0, "relevance_score": float("nan")}]) as cohere:
        async with world.api(reranker=cohere) as api:
            message = done_message(events_of(await ask_new(api, world.chat.kb_a, QUESTION)))

    assert message["status"] == "COMPLETED"
    assert len(cohere.reranks()) == 1
    assert context_texts(world) == [A, B, C, D]


# AC-005: an answer keeps the switches it started with ----------------------------------------------------------------


async def test_AC_005_an_answer_started_with_the_reranker_on_uses_it_after_it_is_switched_off_and_the_next_does_not(
    world: World,
) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    await rerank_on(world)
    world.chat.ollama.delay_seconds = 2  # the answer has read its switches and waits for the embedding

    async with same_app(world) as signed_in:
        admin = await signed_in(ADMIN)
        mario = await signed_in("mario")
        running = asyncio.create_task(ask(mario, conversation, QUESTION))

        async def embedding_asked() -> bool:
            return len(world.chat.ollama.requests) == 1

        assert await eventually(embedding_asked), "the answer did not reach the embedding"
        turned = await admin.put(SETTINGS, json={"rerank_enabled": False})
        assert turned.status_code == 200, turned.text
        assert turned.json()["rerank"]["enabled"] is False
        assert not running.done()  # switched off while the answer was running
        first = done_message(events_of(await asyncio.wait_for(running, SECONDS)))
        world.chat.ollama.delay_seconds = 0
        second = done_message(events_of(await ask(mario, conversation, QUESTION)))

    assert first["status"] == second["status"] == "COMPLETED"
    assert len(world.reranker.reranks()) == 1  # the first answer only
    assert context_texts(world, 0) == [C, A, B, D]
    assert context_texts(world, 1) == [A, B, C, D]


async def test_AC_005_an_answer_started_with_the_reranker_off_does_not_use_it_when_it_is_switched_on(
    world: World,
) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    world.chat.ollama.delay_seconds = 2

    async with same_app(world) as signed_in:
        admin = await signed_in(ADMIN)
        mario = await signed_in("mario")
        running = asyncio.create_task(ask(mario, conversation, QUESTION))

        async def embedding_asked() -> bool:
            return len(world.chat.ollama.requests) == 1

        assert await eventually(embedding_asked), "the answer did not reach the embedding"
        assert (await admin.put(SETTINGS, json={"rerank_enabled": True})).status_code == 200
        first = done_message(events_of(await asyncio.wait_for(running, SECONDS)))
        world.chat.ollama.delay_seconds = 0
        done_message(events_of(await ask(mario, conversation, QUESTION)))

    assert first["status"] == "COMPLETED"
    assert len(world.reranker.reranks()) == 1  # the second answer only
    assert context_texts(world, 0) == [A, B, C, D]
    assert context_texts(world, 1) == [C, A, B, D]


# The retry and the delete --------------------------------------------------------------------------------------------


async def test_a_retry_reranks_with_the_switches_of_the_retry(world: World) -> None:
    """REQ-010: the retry repeats the reranking with the switches active when it starts."""
    ids = await seed_five(world)
    conversation = await follow_up_conversation(
        world,
        ("user", FIRST),
        ("assistant", FIRST_ANSWER),
        ("user", QUESTION),
        ("assistant", "", "FAILED", "generation_error"),
    )
    failed = (await messages_of(world.chat.engine, conversation))[-1]["id"]
    await rerank_on(world)

    async with world.api() as api:
        response = await api.post(f"/api/conversations/{conversation}/messages/{failed}/retry")
    message = done_message(events_of(response))

    assert message["id"] == str(failed)
    assert message["status"] == "COMPLETED"
    assert world.reranker.texts_sent() == [[A, B, C]]
    assert await stored_order(world, failed) == [ids["c"], ids["a"], ids["b"], ids["d"]]


async def test_a_delete_while_the_reranker_is_waited_for_stops_the_answer(world: World) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    world.reranker.delay_seconds = 60  # until the end of the test (release)
    await rerank_on(world)

    async with world.api() as api:
        running = asyncio.create_task(ask(api, conversation, QUESTION))

        async def reranker_asked() -> bool:
            return world.reranker.rerank_received.is_set()

        assert await eventually(reranker_asked), "the reranker was not asked"
        deleted = await api.delete(f"/api/conversations/{conversation}")
        events = events_of(await asyncio.wait_for(running, SECONDS))

    assert deleted.status_code == 204
    assert names_of(events) == ["conversation"]  # neither done nor error
    assert world.chat.provider.requests == []  # the answer stopped before the generation
    assert (await table_counts(world.chat.engine))[0] == 0


# REQ-101: the log line of the step -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("modes", "settings", "used", "reason"),
    [
        ({}, {}, "true", "none"),
        ({}, {"rerank_base_url": CLOSED}, "false", "unreachable"),
        ({"fail_status": 500}, {}, "false", "http_error"),
        ({"delay_seconds": 5}, {"rerank_timeout_seconds": 1}, "false", "timeout"),
        ({"results": tei((0, 0.5), (1, float("nan")), (2, 0.9))}, {}, "false", "invalid_response"),
    ],
)
async def test_the_rerank_line_has_the_ids_the_use_the_reason_and_the_candidates_and_no_text_or_score(
    world: World, modes: dict[str, Any], settings: dict[str, Any], used: str, reason: str
) -> None:
    for name, value in modes.items():
        setattr(world.reranker, name, value)
    await seed_five(world)
    conversation = await follow_up_conversation(world)
    await rerank_on(world)

    with service_logs() as (records, logs):
        async with world.api(**settings) as api:
            message = done_message(events_of(await ask(api, conversation, QUESTION)))

    (line,) = rerank_lines(records)  # exactly one line per answer
    ids = f"conversation={conversation} message={message['id']} kb={world.chat.kb_a}"
    assert re.fullmatch(rf"chat rerank {re.escape(ids)} used={used} reason={reason} candidates=3 ms=\d+", line), line
    (record,) = records.starting("chat rerank ")
    assert record.levelno == (logging.INFO if used == "true" else logging.WARNING)
    everything = "\n".join(text for _, text in logs.lines)
    for secret in (A, B, C, D, QUESTION, RERANK_KEY, "alfa", "gamma"):
        assert secret not in everything


async def test_without_the_step_there_is_no_rerank_line_and_no_request(world: World) -> None:
    await seed_five(world)
    conversation = await follow_up_conversation(world)

    with service_logs() as (records, _):
        async with world.api(chat_context_chunks=8) as api:  # configured, switched off
            done_message(events_of(await ask(api, conversation, QUESTION)))
        await rerank_on(world)
        async with world.chat.api("mario") as api:  # switched on, not configured
            done_message(events_of(await ask(api, conversation, QUESTION)))

    assert rerank_lines(records) == []
    assert world.reranker.requests == []
    assert context_texts(world, 0) == context_texts(world, 1) == [A, B, C, D, E]  # phase 3: CHAT_CONTEXT_CHUNKS 8
