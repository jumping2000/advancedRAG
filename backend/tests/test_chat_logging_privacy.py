# SPEC-2026-11-17-fase-3-chat
"""Nothing private in the logs over a whole life of a conversation: AC-046 (REQ-102).

A question with recognizable words is answered from a chunk with recognizable words, of a document with a recognizable
file name, by a provider that answers with recognizable words and cites the chunk. The conversation is renamed (with a
recognizable title), listed, opened and its source is read; a question fails at the provider, the retry fails again and
then works; a question fails at Ollama; the admin reads the conversation (list, detail, source) and a write of the
admin is refused; a second conversation is deleted while its answer is being generated, and the first one is deleted.
Every log record of the process is collected at INFO (as in the other privacy tests: all the loggers, with their
tracebacks); none may hold a question, an answer (not even the fragment streamed before the deletion), the title, the
chunk, the file name, the vector of the question or of the chunk, or `GENERATION_API_KEY`. The chat lines hold the id of
the conversation, and the code of each error.

The chat does not run in the worker (REQ-103: the worker has no generation setting and no chat code): its lines are
those of the ingestion, covered by the privacy test of phase 2 (test_logging_privacy.py), which this one reuses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fakes.services import delta_event, fake_vector
from tests.test_chat_ask import (
    API_KEY,
    DIMENSIONS,
    Chat,
    ask,
    ask_new,
    chat_world,
    chunk_row,
    done_message,
    events_of,
    names_of,
    ranked_vector,
    seed,
    table_counts,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_streaming import SECONDS, eventually
from tests.test_logging_privacy import Collector, all_logs

QUESTION = "Zorgonauta7741 qual e' il valore del parametro?"
SECOND_QUESTION = "Seconda Baobab6615 domanda sul parametro?"
THIRD_QUESTION = "Terza Ghepardo8830 domanda sul parametro?"
FOURTH_QUESTION = "Quarta Pellicano3342 domanda sul parametro?"
CHUNK_TEXT = "Il Frobnicatore9902 si regola con la chiave riservata del tecnico"
OTHER_CHUNK = "Il Girasole6638 richiede una taratura annuale"
ANSWER_TEXT = "Rispondo con Cavolfiore5523 come indicato [1]"
STREAMED_BEFORE_DELETE = "Inizio-Narvalo9917 "
TITLE = "Titolo-Ippogrifo-4417"
FILE_NAME = "segreto-Pippo3318.pdf"


@pytest.fixture
async def chat(engine: AsyncEngine) -> Any:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


def chat_lines(logs: Collector) -> list[str]:
    return [line for name, line in logs.lines if name.startswith("app.chat")]


def lines_with(logs: Collector, conversation: str) -> int:
    return sum(conversation in line for line in chat_lines(logs))


async def test_AC_046_a_whole_life_of_a_conversation_leaves_nothing_of_its_content_in_the_logs(chat: Chat) -> None:
    await seed(chat, [chunk_row(0, CHUNK_TEXT, QUESTION), chunk_row(1, OTHER_CHUNK, QUESTION)], filename=FILE_NAME)
    chat.provider.fragments = [ANSWER_TEXT]
    vectors = [fake_vector(QUESTION, DIMENSIONS), ranked_vector(QUESTION, 0), ranked_vector(QUESTION, 1)]
    grown: dict[str, bool] = {}

    with all_logs() as logs:
        async with chat.api("mario") as api, chat.api("anna") as admin:
            first = events_of(await ask_new(api, chat.kb_a, QUESTION))
            conversation = first[0][1]["conversation_id"]
            answer = done_message(first)["id"]
            source = f"/api/conversations/{conversation}/messages/{answer}/sources/1"

            renamed = await api.patch(f"/api/conversations/{conversation}", json={"title": TITLE})
            invalid = await api.patch(f"/api/conversations/{conversation}", json={"title": "   "})
            listed = await api.get(f"/api/kbs/{chat.kb_a}/conversations")
            detail = await api.get(f"/api/conversations/{conversation}")
            before = lines_with(logs, conversation)
            excerpt = await api.get(source)
            grown["excerpt"] = lines_with(logs, conversation) > before

            chat.provider.fail_status = 500
            failed = events_of(await ask(api, conversation, SECOND_QUESTION))
            (_, last_answer) = [m for m in (await api.get(f"/api/conversations/{conversation}")).json()["messages"]][
                -2:
            ]
            retry = f"/api/conversations/{conversation}/messages/{last_answer['id']}/retry"
            retry_failed = events_of(await api.post(retry))
            chat.provider.fail_status = None
            chat.provider.fragments = [ANSWER_TEXT]
            retried = events_of(await api.post(retry))
            chat.ollama.fail_status = 500
            embedding_failed = events_of(await ask(api, conversation, THIRD_QUESTION))
            chat.ollama.fail_status = None

            admin_list = await admin.get("/api/admin/conversations")
            admin_detail = await admin.get(f"/api/conversations/{conversation}")
            admin_excerpt = await admin.get(source)
            admin_write = await admin.patch(f"/api/conversations/{conversation}", json={"title": "Dell'admin"})

            # a second conversation, deleted while its answer is generated (a fragment was streamed already)
            chat.provider.script = [delta_event(STREAMED_BEFORE_DELETE), *([b": slow\n\n", 0.1] * 600)]
            generating = asyncio.create_task(ask_new(api, chat.kb_a, FOURTH_QUESTION))

            async def started() -> Any:
                return chat.provider.request_received.is_set() and (await table_counts(chat.engine))[0] == 2

            assert await eventually(started), "the answer is not being generated"
            ids = await api.get(f"/api/kbs/{chat.kb_a}/conversations")
            (second,) = [c["id"] for c in ids.json() if c["id"] != conversation]
            before = lines_with(logs, second)
            stopped = await api.delete(f"/api/conversations/{second}")
            grown["delete while generating"] = lines_with(logs, second) > before
            await asyncio.wait_for(generating, SECONDS)

            before = lines_with(logs, conversation)
            deleted = await api.delete(f"/api/conversations/{conversation}")
            grown["delete"] = lines_with(logs, conversation) > before

    assert renamed.status_code == 200 and renamed.json()["title"] == TITLE
    assert invalid.status_code == 422
    assert listed.status_code == detail.status_code == excerpt.status_code == 200
    assert excerpt.json()["content"] == CHUNK_TEXT
    assert names_of(failed)[-1] == names_of(retry_failed)[-1] == names_of(embedding_failed)[-1] == "error"
    assert failed[-1][1]["error"]["code"] == retry_failed[-1][1]["error"]["code"] == "generation_error"
    assert embedding_failed[-1][1]["error"]["code"] == "embedding_error"
    assert names_of(retried)[-1] == "done"
    assert admin_list.status_code == admin_detail.status_code == admin_excerpt.status_code == 200
    assert admin_write.status_code == 403
    assert stopped.status_code == 204 and deleted.status_code == 204
    assert await table_counts(chat.engine) == (0, 0, 0)

    names = [name for name, _ in logs.lines]
    assert any(name.startswith("app.chat") for name in names), "the chat logged nothing: the collection is not working"
    assert any(name.startswith("httpx") for name in names), "no HTTP request was logged: nothing collected"
    forbidden = [
        "Zorgonauta7741", "Baobab6615", "Ghepardo8830", "Pellicano3342", "parametro",
        "Frobnicatore9902", "Girasole6638", "chiave riservata",
        "Cavolfiore5523", "Narvalo9917",
        "Ippogrifo4417", TITLE,
        "Pippo3318", FILE_NAME,
        API_KEY,
    ]  # fmt: skip
    for vector in vectors:
        forbidden += [repr(number)[:9] for number in vector[:3]]
    leaks = [(logger, line) for logger, line in logs.lines for item in forbidden if item in line]
    assert leaks == []

    # what the chat does write: the conversation, the codes, and a line for each thing that was done to a conversation
    lines = chat_lines(logs)
    assert any(conversation in line for line in lines)
    for code in ("generation_error", "embedding_error"):
        assert any(code in line and conversation in line for line in lines), code
    assert grown == {"excerpt": True, "delete while generating": True, "delete": True}


async def test_AC_046_the_rename_of_a_conversation_is_logged_with_its_id_and_not_its_title(chat: Chat) -> None:
    await seed(chat, [chunk_row(0, CHUNK_TEXT, QUESTION)], filename=FILE_NAME)

    with all_logs() as logs:
        async with chat.api("mario") as api:
            conversation = events_of(await ask_new(api, chat.kb_a, QUESTION))[0][1]["conversation_id"]
            before = lines_with(logs, conversation)
            renamed = await api.patch(f"/api/conversations/{conversation}", json={"title": TITLE})

    assert renamed.status_code == 200
    assert lines_with(logs, conversation) > before
    assert not any(TITLE in line or "Ippogrifo4417" in line for _, line in logs.lines)
