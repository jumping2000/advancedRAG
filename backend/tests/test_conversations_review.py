# SPEC-2026-11-17-fase-3-chat
"""What the review of T-05 found, and the cases it asked for: AC-037 (REQ-018) and AC-020 (REQ-010).

- A question and a retry in the same moment must not deadlock: one of them wins and the other is refused with
  `AnswerInProgress` (409 `answer_in_progress`), never a database error (500). The two take the conversation row
  first, then touch the messages.
- A delete that comes before the stream of the answer has started, or before the task of the answer has taken its
  first step, still stops the generation: nothing is sent to Ollama or to the provider, and the stream ends (no endless
  keepalive loop).
- A document that is deleted between the retrieval and the saving of the answer leaves a saved source that the excerpt
  shows as no longer available.
- `ActiveAnswers.cancel` when the task ignores the cancellation (the wait ends, with a warning), and when the answer is
  in the embedding of the question or in the retrieval.

Written after the code of T-05, from the review: the spec is the oracle.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat import service
from app.chat.repository import AnswerInProgress, ConversationRepository
from app.main import create_app
from tests.test_chat_ask import (
    QUESTION,
    Chat,
    add_conversation,
    ask_new,
    chat_world,
    chunk_row,
    done_message,
    events_of,
    fetch,
    messages_of,
    names_of,
    seed,
    table_counts,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_streaming import SECONDS, eventually, pending_question
from tests.test_conversations import conversation_url, sharing_an_app, wait_for_blocked
from tests.test_knowledge_bases import PASSWORD, login
from tests.test_logging_privacy import all_logs


@pytest.fixture
async def chat(engine: AsyncEngine) -> Any:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


# A question and a retry at the same moment ----------------------------------------------------------------------------


async def test_AC_037_a_question_and_a_retry_at_the_same_moment_do_not_deadlock_and_one_gets_409(chat: Chat) -> None:
    """The last answer is FAILED. A test holds the conversation row against its updates (`FOR NO KEY UPDATE`, which lets
    the inserts of a question through), the retry queues for the row first and the question second, then the row is
    released. If the question inserts its PENDING answer before it takes the row, the retry gets the row, waits for the
    question's unique entry, and the question waits for the row: a deadlock that PostgreSQL ends by aborting one."""
    conversation_id = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "q"),
        ("assistant", "", "FAILED", "generation_error"),
    )
    (_, failed) = await messages_of(chat.engine, conversation_id)
    sessions = chat.sessions()
    async with sessions() as loading:
        mario = await chat.user(loading, "mario")
        conversation = await ConversationRepository(loading).get_for(mario, conversation_id)
        assert conversation is not None

    async def retry() -> Any:
        async with sessions() as session:
            return await ConversationRepository(session).restart_answer(conversation, failed["id"])

    async def question() -> Any:
        async with sessions() as session:
            return await ConversationRepository(session).start_answer(conversation, "nuova domanda")

    holder = await chat.engine.connect()
    try:
        await holder.execute(
            text("select 1 from conversations where id = :id for no key update"), {"id": conversation_id}
        )
        retrying = asyncio.create_task(retry())
        await wait_for_blocked(chat.engine, 1)
        asking = asyncio.create_task(question())
        await wait_for_blocked(chat.engine, 2)
    finally:
        await holder.rollback()
        await holder.close()
    outcomes = await asyncio.wait_for(asyncio.gather(retrying, asking, return_exceptions=True), 30)

    refused = [outcome for outcome in outcomes if isinstance(outcome, AnswerInProgress)]
    won = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert len(refused) == 1 and len(won) == 1, outcomes  # exactly one winner, the other is 409, no database error
    assert len(await fetch(chat.engine, "select id from messages where status = 'PENDING'")) == 1


# A delete before the answer has started ----------------------------------------------------------------


@pytest.mark.parametrize("how", ["deleted", "settled"])
async def test_AC_037_an_answer_whose_conversation_is_deleted_before_it_starts_calls_nobody_and_ends(
    chat: Chat, how: str
) -> None:
    """The route saved the question and the PENDING answer; before the stream took its first step the conversation was
    deleted (nothing to cancel yet) or the answer was already settled: the generation must not start."""
    await seed(chat, [chunk_row(0)])
    app, request, mario, conversation, (asked, answer) = await pending_question(chat)
    async with app.state.sessionmaker() as session:
        repository = ConversationRepository(session)
        if how == "deleted":
            assert await repository.delete(conversation) is True
        else:
            assert await repository.fail_answer(answer.id, "interrupted") is True

    async def consume() -> list[str]:
        return [
            event.split("\n", 1)[0]
            async for event in service.answer_events(request, mario, conversation, asked, answer)
        ]

    try:
        seen = await asyncio.wait_for(consume(), SECONDS)
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()

    assert seen == ["event: conversation"]  # neither delta, nor done, nor error
    assert chat.ollama.requests == []
    assert chat.provider.requests == []
    if how == "deleted":
        assert await table_counts(chat.engine) == (0, 0, 0)  # nothing came back
    else:
        (row,) = await fetch(chat.engine, "select status, error_code from messages where role = 'assistant'")
        assert (row["status"], row["error_code"]) == ("FAILED", "interrupted")  # as it was settled


async def test_AC_037_a_task_cancelled_before_its_first_step_still_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delete cancels the task of the answer before `produce` has run a line: nobody puts anything in the queue
    from inside the task, and the stream must not wait for it for ever sending keepalives."""
    monkeypatch.setattr(service, "KEEPALIVE_SECONDS", 0.05)

    async def never() -> Any:
        yield "never sent"

    seen: list[str] = []

    async def consume() -> None:
        async for item in service._with_keepalive(never(), lambda task: task.cancel()):
            seen.append(item)

    with pytest.raises(service._Stopped):
        await asyncio.wait_for(consume(), 3)
    assert "never sent" not in seen


async def test_AC_037_a_task_that_registers_between_the_cancel_and_the_delete_is_cancelled_too(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete route cancels, then deletes, then cancels again: an answer whose task registered after the first
    cancel (its stream started meanwhile) and passed its check before the rows were gone is stopped by the second."""
    conversation_id = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "q"))
    app = create_app(chat.settings_with())
    started, cleaned = asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.set()

    original = ConversationRepository.delete

    async def delete_after_a_task_registered(self: ConversationRepository, conversation: Any) -> bool:
        task = asyncio.create_task(work())
        tasks.append(task)
        service.active_answers(app).register(conversation.id, task)
        await started.wait()
        return await original(self, conversation)

    monkeypatch.setattr(ConversationRepository, "delete", delete_after_a_task_registered)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            assert (await login(api, "mario", PASSWORD)).status_code == 200
            deleted = await api.delete(conversation_url(conversation_id))
        stopped = cleaned.is_set()
    finally:
        for task in tasks:
            task.cancel()
        await app.state.engine.dispose()
        await app.state.redis.aclose()

    assert deleted.status_code == 204
    assert stopped, "the task registered before the delete went on after it"


# A document deleted between the retrieval and the saving --------------------------------------------------------------


async def test_AC_020_a_document_deleted_between_the_retrieval_and_the_saving_leaves_a_source_shown_as_gone(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = await seed(chat, [chunk_row(0, "Il Frobnicatore9902 si regola con cura")], filename="sparito.pdf")
    (row,) = await fetch(chat.engine, "select document_id from chunks where id = :id", id=chunks[0])
    original = service.load_candidates

    async def load_then_lose_the_document(*args: Any, **kwargs: Any) -> Any:
        candidates = await original(*args, **kwargs)
        async with chat.engine.begin() as connection:
            await connection.execute(text("delete from documents where id = :id"), {"id": row["document_id"]})
        return candidates

    monkeypatch.setattr(service, "load_candidates", load_then_lose_the_document)
    chat.provider.fragments = ["Vedi [1]."]

    async with chat.api("mario") as api:
        events = events_of(await ask_new(api, chat.kb_a, QUESTION))
        message = done_message(events)
        conversation = events[0][1]["conversation_id"]
        opened = await api.get(conversation_url(conversation))
        excerpt = await api.get(f"{conversation_url(conversation)}/messages/{message['id']}/sources/1")

    assert message["status"] == "COMPLETED"
    assert [(s["number"], s["document_name"]) for s in message["sources"]] == [(1, "sparito.pdf")]  # saved all the same
    assert opened.status_code == 200
    assert excerpt.status_code == 200, excerpt.text
    assert excerpt.json()["available"] is False
    assert excerpt.json()["content"] is None
    assert excerpt.json()["document_name"] == "sparito.pdf"
    assert "Frobnicatore9902" not in excerpt.text


# ActiveAnswers.cancel -------------------------------------------------------------------------------------------------


async def test_cancel_stops_waiting_for_a_task_that_ignores_the_cancellation_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "CANCEL_WAIT_SECONDS", 0.2)
    active = service.ActiveAnswers()
    conversation = uuid.uuid4()
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()  # goes on: it ignores the cancellation

    task = asyncio.create_task(stubborn())
    active.register(conversation, task)
    await asyncio.sleep(0)

    with all_logs() as logs:
        started = time.monotonic()
        cancelled = await active.cancel(conversation)
        waited = time.monotonic() - started
    stuck = not task.done()
    release.set()
    await task

    assert cancelled is True
    assert 0.1 <= waited < 5  # the wait ended at the limit, not when the task ended (it had not)
    assert stuck
    warnings = [line for name, line in logs.lines if name == "app.chat.service" and "did not stop" in line]
    assert warnings and all(str(conversation) in line for line in warnings)


@pytest.mark.parametrize("where", ["embedding", "retrieval"])
async def test_AC_037_deleting_the_conversation_while_the_question_is_embedded_or_searched_stops_the_answer(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    await seed(chat, [chunk_row(0)])
    entered = asyncio.Event()
    if where == "embedding":
        chat.ollama.delay_seconds = 30.0  # Ollama does not answer
    else:

        async def hang(*_: Any, **__: Any) -> Any:
            entered.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(service, "bm25_search", hang)

    async with sharing_an_app(chat, "mario") as clients:
        asking = asyncio.create_task(ask_new(clients["mario"], chat.kb_a, QUESTION))

        async def reached() -> bool:
            return bool(chat.ollama.requests) if where == "embedding" else entered.is_set()

        assert await eventually(reached), "the answer did not reach the step"
        (conversation,) = [r["id"] for r in await fetch(chat.engine, "select id from conversations")]
        started = time.monotonic()
        deleted = await clients["mario"].delete(conversation_url(conversation))
        events = events_of(await asyncio.wait_for(asking, SECONDS))

    assert deleted.status_code == 204
    assert time.monotonic() - started < SECONDS  # not the 30 s of Ollama
    assert names_of(events) == ["conversation"]  # neither delta, nor done, nor error
    assert chat.provider.requests == []
    assert await table_counts(chat.engine) == (0, 0, 0)
