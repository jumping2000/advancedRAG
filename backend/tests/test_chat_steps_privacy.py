# SPEC-2026-11-20-fase-4-rerank-rewrite
"""The logs and the destinations of the two steps (T-03): AC-025 (REQ-101) and AC-026 (REQ-102), through the API against
the real PostgreSQL (ParadeDB) of compose.test.yaml and the fakes of Ollama, of the generation provider, of the
rewriting service and of the reranker (real local sockets, never a real service).

AC-025: a follow-up question, a history, a rewritten question and chunks with recognizable words, the two keys and
recognizable scores of the reranker; an answer with both steps used, then one with both falling back. Every record of
every logger is collected at INFO (as in the privacy tests of phases 2 and 3); none holds those texts, scores or keys,
and the lines of the two steps hold the ids, the use or the fallback and the reason, exactly once per answer. The chat
does not run in the worker (phase 3, REQ-103: no generation, rewriting or rerank setting, no chat code): its lines are
those of the ingestion, covered by test_logging_privacy.py.

AC-026: every HTTP request the backend sends is recorded (the `outgoing` spy of test_chat_ask); with both steps active
the only ones are the rewriting (`/chat/completions` of REWRITE_BASE_URL), the embedding (`/api/embed` of
OLLAMA_BASE_URL), the reranking (`/rerank` of RERANK_BASE_URL) and the generation (`/chat/completions` of
GENERATION_BASE_URL); with both steps off nothing goes to REWRITE_BASE_URL or RERANK_BASE_URL.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import pytest

from tests.fakes.services import fake_reranker
from tests.test_chat_ask import add_conversation, ask, chunk_row, done_message, events_of, seed
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import outgoing as outgoing  # noqa: F401  (the HTTP requests the backend sends)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_rerank import World, rerank_lines, service_logs
from tests.test_chat_rerank import world as world  # noqa: F401  (the fakes and both switches off)
from tests.test_chat_rewrite import store_switches

FIRST = "Quanto Zebra4471 costa il prodotto?"
FIRST_ANSWER = "Il prodotto Koala5512 costa dieci euro [1]."
FOLLOW_UP = "e la Lince6623?"
REWRITTEN = "Quanto costa la Orso7734 del prodotto?"
CHUNK_1 = "Il Tasso8845 regola la consegna"
CHUNK_2 = "Il Furetto9956 descrive la garanzia"
CHUNK_3 = "La Donnola1067 indica il montaggio"
REWRITE_KEY = "sk-riscrittura-Gufo2278"
RERANK_KEY = "sk-reranker-Falco3389"
SCORES = {"Tasso8845": 0.8137291, "Furetto9956": 0.4271828, "Donnola1067": 0.1414213}


async def follow_up_conversation(world: World) -> uuid.UUID:  # noqa: F811
    return await add_conversation(
        world.chat.engine, world.chat.kb_a, world.chat.users["mario"], ("user", FIRST), ("assistant", FIRST_ANSWER)
    )


async def seed_chunks(world: World) -> None:  # noqa: F811
    """The three chunks, and the rewritten question of this module as the answer of the fake rewriting service."""
    world.rewriter.answer = REWRITTEN
    await seed(
        world.chat,
        [chunk_row(0, CHUNK_1, FOLLOW_UP), chunk_row(1, CHUNK_2, FOLLOW_UP), chunk_row(2, CHUNK_3, FOLLOW_UP)],
    )


KEYS: dict[str, Any] = {"rewrite_api_key": REWRITE_KEY, "rerank_api_key": RERANK_KEY}


# AC-025: nothing of the steps in the logs ---------------------------------------------------------------------------


async def test_AC_025_both_steps_used_and_both_falling_back_leave_no_text_score_or_key_in_the_logs(
    world: World,  # noqa: F811
) -> None:
    await seed_chunks(world)
    world.reranker.scores = SCORES
    conversation = await follow_up_conversation(world)
    await store_switches(world.chat.engine, rerank=True, rewrite=True)

    with service_logs() as (records, logs):
        async with world.api(**KEYS) as api:
            used = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))
            world.rewriter.fail_status = 500
            world.reranker.results = [{"index": 0, "score": float("nan")}, {"index": 1, "score": 0.8137291}]
            fallback = done_message(events_of(await ask(api, conversation, FOLLOW_UP)))

    assert used["status"] == fallback["status"] == "COMPLETED"
    assert used["search_query"] == REWRITTEN and fallback["search_query"] is None
    assert len(world.rewriter.completions()) == 2 and len(world.reranker.reranks()) == 2

    # what the services received holds the texts, so their absence from the logs means something
    assert world.reranker.reranks()[0].body["query"] == REWRITTEN
    assert CHUNK_1 in world.reranker.texts_sent()[0]
    assert FIRST in json.dumps(world.rewriter.completions()[0].body, ensure_ascii=False)

    names = [name for name, _ in logs.lines]
    assert any(name.startswith("app.chat") for name in names), "the chat logged nothing: the collection is not working"
    assert any(name.startswith("httpx") for name in names), "no HTTP request was logged: nothing collected"
    everything = "\n".join(text for _, text in logs.lines)
    forbidden = [
        FIRST, "Zebra4471", FIRST_ANSWER, "Koala5512", FOLLOW_UP, "Lince6623", REWRITTEN, "Orso7734",
        CHUNK_1, "Tasso8845", CHUNK_2, "Furetto9956", CHUNK_3, "Donnola1067",
        REWRITE_KEY, "Gufo2278", RERANK_KEY, "Falco3389",
        "0.8137291", "0.4271828", "0.1414213", "8137291", "4271828", "1414213",
    ]  # fmt: skip
    leaked = [secret for secret in forbidden if secret in everything]
    assert leaked == []

    for message, used_value, rewrite_reason, rerank_reason in (
        (used, "true", "none", "none"),
        (fallback, "false", "http_error", "invalid_response"),
    ):
        ids = f"conversation={conversation} message={message['id']} kb={world.chat.kb_a}"
        (rewrite,) = [r for r in records.starting("chat rewrite ") if ids in r.getMessage()]  # once per answer
        (rerank,) = [r for r in records.starting("chat rerank ") if ids in r.getMessage()]
        assert re.fullmatch(
            rf"chat rewrite {re.escape(ids)} used={used_value} reason={rewrite_reason} ms=\d+", rewrite.getMessage()
        ), rewrite.getMessage()
        assert re.fullmatch(
            rf"chat rerank {re.escape(ids)} used={used_value} reason={rerank_reason} candidates=3 ms=\d+",
            rerank.getMessage(),
        ), rerank.getMessage()
        level = logging.INFO if used_value == "true" else logging.WARNING
        assert rewrite.levelno == rerank.levelno == level
    assert len(rerank_lines(records)) == 2


# AC-026: the only destinations of the chat content -----------------------------------------------------------------


@pytest.mark.parametrize("api", ["tei", "cohere"])
async def test_AC_026_with_both_steps_active_the_content_goes_only_to_the_four_configured_endpoints(
    world: World,  # noqa: F811
    outgoing: list[tuple[str, str, bytes]],  # noqa: F811
    api: str,
) -> None:
    await seed_chunks(world)
    conversation = await follow_up_conversation(world)
    await store_switches(world.chat.engine, rerank=True, rewrite=True)

    with fake_reranker(api, scores=SCORES) as reranker:
        async with world.api(reranker=reranker, **KEYS) as client:
            message = done_message(events_of(await ask(client, conversation, FOLLOW_UP)))

    assert message["status"] == "COMPLETED"
    assert [(method, url) for method, url, _ in outgoing] == [
        ("POST", f"{world.rewriter.url}/chat/completions"),
        ("POST", f"{world.chat.ollama.url}/api/embed"),
        ("POST", f"{reranker.url}/rerank"),
        ("POST", f"{world.chat.provider.url}/chat/completions"),
    ]
    rewrite, embed, rerank, generation = (json.loads(body) for _, _, body in outgoing)
    assert rewrite["messages"][-1]["content"] == FOLLOW_UP  # the question and the history to the rewriting
    assert embed["input"] == [REWRITTEN]  # the searched question to Ollama
    texts = rerank["documents"] if api == "cohere" else rerank["texts"]
    assert rerank["query"] == REWRITTEN and CHUNK_1 in texts  # the searched question and the candidates
    assert FOLLOW_UP in json.dumps(generation, ensure_ascii=False)
    assert world.reranker.requests == []  # the `tei` fake of the world is not the configured one here


async def test_AC_026_with_both_steps_off_nothing_goes_to_the_rewriting_or_the_reranker(
    world: World,  # noqa: F811
    outgoing: list[tuple[str, str, bytes]],  # noqa: F811
) -> None:
    await seed_chunks(world)
    conversation = await follow_up_conversation(world)

    async with world.api(**KEYS) as client:  # both services configured, both switches off
        message = done_message(events_of(await ask(client, conversation, FOLLOW_UP)))

    assert message["status"] == "COMPLETED"
    assert [(method, url) for method, url, _ in outgoing] == [
        ("POST", f"{world.chat.ollama.url}/api/embed"),
        ("POST", f"{world.chat.provider.url}/chat/completions"),
    ]
    assert not [url for _, url, _ in outgoing if url.startswith((world.rewriter.url, world.reranker.url))]
    assert world.rewriter.requests == [] and world.reranker.requests == []
