# SPEC-2026-11-17-fase-3-chat
"""Retrying the last failed answer: AC-034, AC-035 and the «Riprova» side of AC-031 (REQ-014, REQ-016), through
`POST /api/conversations/{conversation_id}/messages/{message_id}/retry` (no body) against the real PostgreSQL
(ParadeDB) of compose.test.yaml, the fake Ollama and the fake generation provider.

The route answers like the question route: the same checks before the stream (404 `conversation_not_found`, 403
`forbidden` for an admin on another user's conversation, 503 `generation_not_configured`, 409 `answer_in_progress` and
`not_retryable`, all in JSON with nothing changed), then the same stream of events; the retry repeats retrieval and
generation for the same question and replaces the failed answer, so the conversation ends with one answer. Requests
that must be truly concurrent run against a real uvicorn (`running`).

Review focus of T-04: only the LAST answer of the conversation can be retried, and only when it is FAILED; a retry
never duplicates the question, never sends the failed exchange back to the provider, and never leaves two answers.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.chat.repository import AnswerInProgress, ConversationRepository, NotRetryable
from tests.test_chat_ask import (
    ADMIN,
    QUESTION,
    Chat,
    add_conversation,
    ask,
    ask_new,
    assert_no_requests,
    chunk_row,
    done_message,
    error_of,
    events_of,
    fetch,
    messages_of,
    names_of,
    pending_count,
    request_messages,
    seed,
    sources_of,
    table_counts,
)
from tests.test_chat_ask import chat as chat  # noqa: F401  (the fixture)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_streaming import SECONDS, eventually, running, signed_in
from tests.test_logging_privacy import all_logs

FAILED = ("assistant", "", "FAILED", "generation_error")


async def retry(api: httpx.AsyncClient, conversation_id: Any, message_id: Any) -> httpx.Response:
    return await api.post(f"/api/conversations/{conversation_id}/messages/{message_id}/retry")


async def failed_conversation(
    chat: Chat, user: str = "mario", code: str = "generation_error", earlier: tuple[tuple[Any, ...], ...] = ()
) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation of the user whose last exchange failed (the question is `QUESTION`): (its id, the id of the failed
    answer); `earlier` are the messages before that exchange."""
    kb = chat.kb_a
    conversation = await add_conversation(
        chat.engine,
        kb,
        chat.users[user],
        *earlier,
        ("user", QUESTION),
        ("assistant", "", "FAILED", code),
    )
    return conversation, (await messages_of(chat.engine, conversation))[-1]["id"]


def shape(messages: list[dict[str, Any]]) -> list[tuple[str, str, str, str | None]]:
    return [(m["role"], m["status"], m["content"], m["error_code"]) for m in messages]


# AC-034: the last failed answer is regenerated in place --------------------------------------------------------------


async def test_AC_034_retry_runs_retrieval_and_generation_again_and_leaves_one_completed_answer_in_place(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0, "Il manuale dice questo", section_title="Garanzia", page_start=2, page_end=3)])
    chat.provider.fragments = ["Vedi ", "[1]"]
    conversation, failed = await failed_conversation(chat)

    async with chat.api("mario") as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    events = events_of(response)
    assert names_of(events) == ["conversation", "delta", "delta", "done"]
    message = done_message(events)
    assert (message["role"], message["status"], message["content"], message["error_code"]) == (
        "assistant",
        "COMPLETED",
        "Vedi [1]",
        None,
    )
    assert message["sources"] == [
        {"number": 1, "document_name": "manuale.pdf", "section_title": "Garanzia", "page_start": 2, "page_end": 3}
    ]
    assert events[0][1] == {"conversation_id": str(conversation), "message_id": message["id"]}
    # one question and ONE answer, completed, in place of the failed one: nothing is duplicated, nothing is left failed
    saved = await messages_of(chat.engine, conversation)
    assert shape(saved) == [("user", "COMPLETED", QUESTION, None), ("assistant", "COMPLETED", "Vedi [1]", None)]
    assert str(saved[1]["id"]) == message["id"]
    assert [(r["number"], r["cited"]) for r in await sources_of(chat.engine, message["id"])] == [(1, True)]
    assert await pending_count(chat.engine) == 0
    # the retrieval and the generation were repeated for the same question
    assert chat.ollama.texts_sent() == [QUESTION]
    assert len(chat.provider.completions()) == 1
    messages = request_messages(chat)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"].endswith(QUESTION)
    assert json.dumps(messages).count(QUESTION) == 1  # the question is not sent twice


async def test_AC_034_the_retried_request_has_the_completed_history_but_no_failed_exchange_and_roles_alternate(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    earlier = (
        ("user", "domanda scartata"),
        ("assistant", "", "FAILED", "generation_timeout"),
        ("user", "u1"),
        ("assistant", "a1"),
    )
    conversation, failed = await failed_conversation(chat, earlier=earlier)

    async with chat.api("mario") as api:
        assert names_of(events_of(await retry(api, conversation, failed)))[-1] == "done"

    messages = request_messages(chat)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in messages[1:3]] == ["u1", "a1"]
    assert messages[3]["content"].endswith(QUESTION)
    assert "domanda scartata" not in json.dumps(messages)  # the earlier failed exchange stays out, as for a question
    assert json.dumps(messages).count(QUESTION) == 1
    saved = await messages_of(chat.engine, conversation)
    assert [(m["role"], m["status"]) for m in saved] == [
        ("user", "COMPLETED"),
        ("assistant", "FAILED"),
        ("user", "COMPLETED"),
        ("assistant", "COMPLETED"),
        ("user", "COMPLETED"),
        ("assistant", "COMPLETED"),
    ]
    assert saved[-2]["content"] == QUESTION
    assert saved[-1]["content"] == "Ciao mondo"  # the answer is after its question, in the order of the conversation


@pytest.mark.parametrize(
    "code", ["embedding_error", "generation_error", "generation_timeout", "internal_error", "interrupted"]
)
async def test_AC_034_an_answer_that_failed_for_any_reason_can_be_retried(chat: Chat, code: str) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat, code=code)

    async with chat.api("mario") as api:
        events = events_of(await retry(api, conversation, failed))

    assert done_message(events)["status"] == "COMPLETED"
    assert shape(await messages_of(chat.engine, conversation)) == [
        ("user", "COMPLETED", QUESTION, None),
        ("assistant", "COMPLETED", "Ciao mondo", None),
    ]


async def test_AC_034_a_retry_that_fails_again_keeps_one_failed_answer_with_the_new_code_and_can_be_retried_again(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat, code="generation_timeout")

    async with chat.api("mario") as api:
        chat.ollama.fail_status = 500
        first = events_of(await retry(api, conversation, failed))
        assert names_of(first) == ["conversation", "error"]
        assert first[1][1]["error"]["code"] == "embedding_error"
        assert shape(await messages_of(chat.engine, conversation)) == [
            ("user", "COMPLETED", QUESTION, None),
            ("assistant", "FAILED", "", "embedding_error"),  # the new reason, still one answer
        ]
        (answer,) = await fetch(chat.engine, "select id from messages where role = 'assistant'")
        assert str(answer["id"]) == first[0][1]["message_id"]

        chat.ollama.fail_status = None
        chat.provider.fail_status = 400
        second = events_of(await retry(api, conversation, answer["id"]))
        assert second[-1][1]["error"]["code"] == "generation_error"

        chat.provider.fail_status = None
        third = events_of(await retry(api, conversation, answer["id"]))

    assert done_message(third)["status"] == "COMPLETED"
    assert shape(await messages_of(chat.engine, conversation)) == [
        ("user", "COMPLETED", QUESTION, None),
        ("assistant", "COMPLETED", "Ciao mondo", None),
    ]
    assert await pending_count(chat.engine) == 0


async def test_AC_034_the_retry_of_an_answer_that_failed_in_a_real_question_regenerates_that_answer(
    chat: Chat,
) -> None:
    """The whole path: the question fails (the provider answers 500), the provider recovers, «Riprova»."""
    await seed(chat, [chunk_row(0)])
    chat.provider.fail_status = 500

    async with chat.api("mario") as api:
        first = events_of(await ask_new(api, chat.kb_a, QUESTION))
        conversation, failed = first[0][1]["conversation_id"], first[0][1]["message_id"]
        assert first[-1][1]["error"]["code"] == "generation_error"
        chat.provider.fail_status = None
        second = events_of(await retry(api, conversation, failed))

    assert done_message(second)["status"] == "COMPLETED"
    assert shape(await messages_of(chat.engine, conversation)) == [
        ("user", "COMPLETED", QUESTION, None),
        ("assistant", "COMPLETED", "Ciao mondo", None),
    ]
    assert chat.ollama.texts_sent() == [QUESTION, QUESTION]
    assert len(chat.provider.completions()) == 2


async def test_AC_034_a_retry_in_a_kb_that_lost_its_documents_answers_like_a_question_with_the_notice(
    chat: Chat,
) -> None:
    conversation, failed = await failed_conversation(chat)  # no document in the KB

    async with chat.api("mario") as api:
        events = events_of(await retry(api, conversation, failed))

    message = done_message(events)
    assert (message["status"], message["content"], message["notice"], message["sources"]) == (
        "COMPLETED",
        "",
        "no_documents",
        [],
    )
    assert_no_requests(chat)
    assert len(await messages_of(chat.engine, conversation)) == 2


async def test_AC_034_the_admin_can_retry_a_failed_answer_of_an_own_conversation(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat, user="anna")

    async with chat.api(ADMIN) as api:
        events = events_of(await retry(api, conversation, failed))

    assert done_message(events)["status"] == "COMPLETED"


async def test_AC_034_the_logs_of_a_retry_hold_no_question_answer_or_chunk(chat: Chat) -> None:
    question = "Zorgonauta9917 quanto costa?"
    await seed(chat, [chunk_row(0, "CHUNK-RISERVATO-5521", question=question)])
    chat.provider.fragments = ["RISPOSTA-RISERVATA-3318"]
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", question), FAILED, title="TITOLO-RISERVATO-8842"
    )
    failed = (await messages_of(chat.engine, conversation))[-1]["id"]

    with all_logs() as logs:
        async with chat.api("mario") as api:
            assert names_of(events_of(await retry(api, conversation, failed)))[-1] == "done"

    text = "\n".join(line for _, line in logs.lines)
    for secret in ("Zorgonauta9917", "CHUNK-RISERVATO-5521", "RISPOSTA-RISERVATA-3318", "TITOLO-RISERVATO-8842"):
        assert secret not in text
    assert str(conversation) in text  # the lines of the chat carry the id of the conversation


# AC-035: only the last answer, and only when it failed ---------------------------------------------------------------


def traffic(chat: Chat) -> tuple[int, int]:
    """How many requests the fake provider and the fake Ollama have received so far."""
    return len(chat.provider.requests), len(chat.ollama.requests)


async def assert_refused(chat: Chat, response: httpx.Response, code: str, before: Any, sent: tuple[int, int]) -> None:
    """A 409 in JSON that is not a stream, the same rows as before and nothing new sent to Ollama or the provider."""
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert error_of(response)[0] == code
    assert error_of(response)[1]
    assert await all_rows(chat) == before
    assert traffic(chat) == sent


async def all_rows(chat: Chat) -> list[dict[str, Any]]:
    return await fetch(chat.engine, "select * from messages order by conversation_id, created_at, id")


@pytest.mark.parametrize("last", ["COMPLETED", "FAILED"])
async def test_AC_035_a_failed_answer_followed_by_a_new_question_is_not_retryable_and_stays_failed(
    chat: Chat, last: str
) -> None:
    await seed(chat, [chunk_row(0)])
    conversation = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "prima"),
        ("assistant", "", "FAILED", "generation_error"),
        ("user", "seconda"),
        ("assistant", "ok" if last == "COMPLETED" else "", last, None if last == "COMPLETED" else "generation_error"),
    )
    first_answer = (await messages_of(chat.engine, conversation))[1]["id"]
    before = await all_rows(chat)
    sent = traffic(chat)

    async with chat.api("mario") as api:
        response = await retry(api, conversation, first_answer)

    await assert_refused(chat, response, "not_retryable", before, sent)
    assert (await messages_of(chat.engine, conversation))[1]["status"] == "FAILED"


async def test_AC_035_after_a_question_the_first_failure_has_no_retry_but_the_last_failed_answer_has(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.fail_status = 500

    async with chat.api("mario") as api:
        first = events_of(await ask_new(api, chat.kb_a, "prima domanda"))
        conversation, first_answer = first[0][1]["conversation_id"], first[0][1]["message_id"]
        second = events_of(await ask(api, conversation, QUESTION))
        second_answer = second[0][1]["message_id"]
        assert first[-1][0] == second[-1][0] == "error"
        chat.provider.fail_status = None
        sent = traffic(chat)
        before = await all_rows(chat)

        refused = await retry(api, conversation, first_answer)
        await assert_refused(chat, refused, "not_retryable", before, sent)
        accepted = events_of(await retry(api, conversation, second_answer))

    assert done_message(accepted)["status"] == "COMPLETED"
    assert [(m["role"], m["status"]) for m in await messages_of(chat.engine, conversation)] == [
        ("user", "COMPLETED"),
        ("assistant", "FAILED"),  # the first one stays failed, with no retry
        ("user", "COMPLETED"),
        ("assistant", "COMPLETED"),
    ]


async def test_AC_035_a_completed_answer_a_question_and_an_answer_with_a_notice_are_not_retryable(chat: Chat) -> None:
    completed = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "q"), ("assistant", "risposta", "COMPLETED")
    )
    failed_then_question = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "q"),
        ("assistant", "", "FAILED", "generation_error"),
    )
    cases = [
        (completed, (await messages_of(chat.engine, completed))[1]["id"]),  # the last answer, completed
        (completed, (await messages_of(chat.engine, completed))[0]["id"]),  # a question
        (failed_then_question, (await messages_of(chat.engine, failed_then_question))[0]["id"]),  # a question
    ]
    before = await all_rows(chat)

    async with chat.api("mario") as api:
        for conversation, message in cases:
            response = await retry(api, conversation, message)
            await assert_refused(chat, response, "not_retryable", before, (0, 0))

    # the answer with the notice `no_documents` (kb_b has no document) is COMPLETED: not retryable either
    async with chat.api("luigi") as api:
        empty = events_of(await ask_new(api, chat.kb_b, "q"))
        assert done_message(empty)["notice"] == "no_documents"
        before = await all_rows(chat)
        response = await retry(api, empty[0][1]["conversation_id"], empty[0][1]["message_id"])
    await assert_refused(chat, response, "not_retryable", before, (0, 0))


# AC-031: a retry while an answer is in progress -----------------------------------------------------------------


async def wait_for_provider(chat: Chat, requests: int = 1) -> None:
    deadline = asyncio.get_running_loop().time() + SECONDS
    while len(chat.provider.completions()) < requests:
        assert asyncio.get_running_loop().time() < deadline, "the provider got no request"
        await asyncio.sleep(0.05)


async def test_AC_031_a_retry_or_a_question_while_a_retry_is_generated_gets_409_answer_in_progress(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    chat.provider.delay_seconds = 30.0

    async with chat.api("mario") as api:
        generating = asyncio.create_task(retry(api, conversation, failed))
        await wait_for_provider(chat)
        assert await pending_count(chat.engine) == 1
        before = await all_rows(chat)
        second_retry = await retry(api, conversation, failed)
        question = await ask(api, conversation, "quokka seconda")
        assert not generating.done()
        assert await all_rows(chat) == before  # the refused requests changed nothing
        chat.provider.release.set()
        answered = await asyncio.wait_for(generating, SECONDS)

    for refused in (second_retry, question):
        assert refused.status_code == 409
        assert refused.headers["content-type"].startswith("application/json")
        assert error_of(refused)[0] == "answer_in_progress"
    assert done_message(events_of(answered))["status"] == "COMPLETED"
    assert len(chat.provider.completions()) == 1
    assert await pending_count(chat.engine) == 0


async def test_AC_031_a_retry_when_the_conversation_has_a_pending_answer_gets_409_answer_in_progress(
    chat: Chat,
) -> None:
    """Whichever message the retry names: while an answer is in progress the answer is 409 `answer_in_progress`."""
    conversation = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "prima"),
        ("assistant", "", "FAILED", "generation_error"),
        ("user", "seconda"),
        ("assistant", "", "PENDING"),
    )
    messages = await messages_of(chat.engine, conversation)
    before = await all_rows(chat)

    async with chat.api("mario") as api:
        for message in (messages[1], messages[3]):  # the earlier failed answer, the pending one
            response = await retry(api, conversation, message["id"])
            await assert_refused(chat, response, "answer_in_progress", before, (0, 0))


@pytest.mark.parametrize("rival", ["retry", "question"])
async def test_AC_031_two_truly_concurrent_requests_on_a_failed_answer_one_proceeds_and_the_other_gets_409(
    chat: Chat, rival: str
) -> None:
    """A retry and a retry, or a retry and a question, at the same instant on real sockets: the partial unique index
    lets exactly one answer be PENDING, the other request gets 409 `answer_in_progress` with nothing saved."""
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    chat.provider.delay_seconds = 30.0  # the one that proceeds stays in progress while the other is refused

    with running(chat.settings_with()) as base_url:
        one, two = await signed_in(base_url), await signed_in(base_url)
        try:
            tasks = [
                asyncio.create_task(retry(one, conversation, failed)),
                asyncio.create_task(
                    retry(two, conversation, failed)
                    if rival == "retry"
                    else two.post(f"/api/conversations/{conversation}/messages", json={"question": "quokka bis"})
                ),
            ]
            refused, waiting = await asyncio.wait(tasks, timeout=SECONDS, return_when=asyncio.FIRST_COMPLETED)
            chat.provider.release.set()
            answered = await asyncio.gather(*waiting)
        finally:
            chat.provider.release.set()
            await one.aclose()
            await two.aclose()

    (loser,) = (task.result() for task in refused)
    assert loser.status_code == 409, loser.text
    assert error_of(loser)[0] == "answer_in_progress"
    (winner,) = answered
    assert names_of(events_of(winner))[-1] == "done"
    saved = [(m["role"], m["status"]) for m in await messages_of(chat.engine, conversation)]
    retried = [("user", "COMPLETED"), ("assistant", "COMPLETED")]  # the retry won: one question, one answer
    asked = [("user", "COMPLETED"), ("assistant", "FAILED"), ("user", "COMPLETED"), ("assistant", "COMPLETED")]
    assert saved == retried or (rival == "question" and saved == asked)  # the question won: the failure stays
    assert len(chat.provider.completions()) == 1
    assert await pending_count(chat.engine) == 0


# The checks before the stream: the same as for a question -----------------------------------------------------------


async def test_the_retry_of_the_conversation_of_another_user_or_of_a_kb_the_user_left_is_404(chat: Chat) -> None:
    conversation, failed = await failed_conversation(chat, user="luigi")
    before = await all_rows(chat)

    async with chat.api("mario") as api:
        other = await retry(api, conversation, failed)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("delete from kb_members where user_id = :user and kb_id = :kb"),
            {"user": chat.users["luigi"], "kb": chat.kb_a},
        )
    async with chat.api("luigi") as api:
        removed = await retry(api, conversation, failed)

    for response in (other, removed):
        assert response.status_code == 404
        assert error_of(response)[0] == "conversation_not_found"
    assert await all_rows(chat) == before
    assert_no_requests(chat)


@pytest.mark.parametrize("conversation_id", ["not-a-uuid", "00000000-0000-4000-8000-000000000000"])
async def test_the_retry_of_an_unknown_conversation_or_one_that_is_not_a_uuid_is_404(
    chat: Chat, conversation_id: str
) -> None:
    _, failed = await failed_conversation(chat)
    before = await all_rows(chat)

    async with chat.api("mario") as api:
        response = await retry(api, conversation_id, failed)

    assert response.status_code == 404
    assert error_of(response)[0] == "conversation_not_found"
    assert await all_rows(chat) == before
    assert_no_requests(chat)


async def test_the_admin_gets_403_forbidden_retrying_the_conversation_of_another_user(chat: Chat) -> None:
    conversation, failed = await failed_conversation(chat)
    before = await all_rows(chat)

    async with chat.api(ADMIN) as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 403
    assert error_of(response)[0] == "forbidden"
    assert await all_rows(chat) == before
    assert_no_requests(chat)


async def test_a_retry_without_a_session_gets_401_and_changes_nothing(chat: Chat) -> None:
    conversation, failed = await failed_conversation(chat)
    before = await all_rows(chat)

    async with chat.api(None) as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 401
    assert await all_rows(chat) == before
    assert_no_requests(chat)


async def test_the_retry_of_an_unknown_message_or_of_a_message_of_another_conversation_is_404(chat: Chat) -> None:
    conversation, failed = await failed_conversation(chat)
    other, other_failed = await failed_conversation(chat)
    before = await all_rows(chat)
    assert other != conversation

    async with chat.api("mario") as api:
        responses = [
            await retry(api, conversation, "not-a-uuid"),
            await retry(api, conversation, uuid.uuid4()),
            await retry(api, conversation, other_failed),  # a failed answer, but of the other conversation
            await retry(api, other, failed),
        ]

    for response in responses:
        assert response.status_code == 404, response.text
        assert error_of(response)[0] == "conversation_not_found"
    assert await all_rows(chat) == before
    assert_no_requests(chat)


async def test_a_retry_without_a_configured_provider_gets_503_and_the_answer_stays_failed(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    before = await all_rows(chat)

    async with chat.api("mario", generation_base_url="", generation_model="") as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 503
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert error_of(response)[0] == "generation_not_configured"
    assert await all_rows(chat) == before
    assert_no_requests(chat)


async def test_a_refused_retry_leaves_the_failed_answer_retryable(chat: Chat) -> None:
    """Nothing changes on a pre-stream error: after a 404, a 403 and a 503 the same answer is retried at once."""
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)

    async with chat.api("luigi") as api:
        assert (await retry(api, conversation, failed)).status_code == 404
    async with chat.api(ADMIN) as api:
        assert (await retry(api, conversation, failed)).status_code == 403
    async with chat.api("mario", generation_base_url="", generation_model="") as api:
        assert (await retry(api, conversation, failed)).status_code == 503
    async with chat.api("mario") as api:
        events = events_of(await retry(api, conversation, failed))

    assert done_message(events)["status"] == "COMPLETED"


# The repository (plan: Interfaces) ------------------------------------------------------------------------------------


async def test_restart_answer_returns_the_answer_pending_again_and_refuses_what_is_not_the_last_failed_one(
    chat: Chat,
) -> None:
    conversation_id = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "prima"),
        ("assistant", "", "FAILED", "generation_error"),
        ("user", "seconda"),
        ("assistant", "", "FAILED", "generation_timeout"),
    )
    first, second = (m["id"] for m in (await messages_of(chat.engine, conversation_id))[1::2])
    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        conversation = await repository.get_for(await chat.user(session, "mario"), conversation_id)
        assert conversation is not None
        with pytest.raises(NotRetryable):
            await repository.restart_answer(conversation, first)  # a question follows it
        answer = await repository.restart_answer(conversation, second)
        assert (answer.id, answer.role, answer.status, answer.content, answer.error_code) == (
            second,
            "assistant",
            "PENDING",
            "",
            None,
        )
        with pytest.raises(AnswerInProgress):
            await repository.restart_answer(conversation, second)  # it is pending now
        with pytest.raises(AnswerInProgress):
            await repository.restart_answer(conversation, first)  # whichever the message: an answer is in progress

    rows = await messages_of(chat.engine, conversation_id)
    assert [(m["status"], m["error_code"]) for m in rows[1::2]] == [("FAILED", "generation_error"), ("PENDING", None)]


async def test_restart_answer_of_two_sessions_at_once_gives_the_answer_to_one_and_answer_in_progress_to_the_other(
    chat: Chat,
) -> None:
    conversation_id, failed = await failed_conversation(chat)

    async def restart() -> str:
        async with chat.sessions()() as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_for(await chat.user(session, "mario"), conversation_id)
            assert conversation is not None
            try:
                await repository.restart_answer(conversation, failed)
            except AnswerInProgress:
                return "in progress"
            except NotRetryable:
                return "not retryable"
            return "restarted"

    outcomes = await asyncio.gather(*(restart() for _ in range(6)))

    assert sorted(outcomes) == ["in progress"] * 5 + ["restarted"]
    assert await pending_count(chat.engine) == 1
    assert await table_counts(chat.engine) == (1, 2, 0)


async def test_AC_031_a_retry_that_meets_a_question_saving_its_pending_answer_gets_answer_in_progress(
    chat: Chat,
) -> None:
    """The window that no check before the UPDATE can close: another request has written a question and its PENDING
    answer and not committed yet. The retry cannot see them, its UPDATE waits for the unique index
    `messages_one_pending`, and when the other request commits the retry is refused with `answer_in_progress` and
    changes nothing (it does not fail with a database error)."""
    conversation_id, failed = await failed_conversation(chat)
    asked_at = (await messages_of(chat.engine, conversation_id))[-1]["created_at"]

    async def retry_outcome() -> str:
        async with chat.sessions()() as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_for(await chat.user(session, "mario"), conversation_id)
            assert conversation is not None
            try:
                await repository.restart_answer(conversation, failed)
            except AnswerInProgress:
                return "in progress"
            except NotRetryable:
                return "not retryable"
            return "restarted"

    async def retry_is_waiting() -> bool:
        waiting = await fetch(
            chat.engine,
            "select 1 from pg_stat_activity where wait_event_type = 'Lock' and query ilike 'update messages%'",
        )
        return bool(waiting)

    async with chat.engine.connect() as other:
        transaction = await other.begin()
        await other.execute(
            text(
                "insert into messages (conversation_id, role, content, status, created_at) values"
                " (:c, 'user', 'nuova domanda', 'COMPLETED', :first), (:c, 'assistant', '', 'PENDING', :second)"
            ),
            {"c": conversation_id, "first": asked_at + timedelta(minutes=1), "second": asked_at + timedelta(minutes=2)},
        )
        retrying = asyncio.create_task(retry_outcome())
        try:
            assert await eventually(retry_is_waiting), "the retry did not wait for the other request"
        finally:
            await transaction.commit()
        outcome = await asyncio.wait_for(retrying, SECONDS)

    assert outcome == "in progress"
    saved = await messages_of(chat.engine, conversation_id)
    assert [(m["role"], m["status"], m["error_code"]) for m in saved] == [
        ("user", "COMPLETED", None),
        ("assistant", "FAILED", "generation_error"),  # the failed answer was not touched
        ("user", "COMPLETED", None),
        ("assistant", "PENDING", None),
    ]


# The retry route settles the answer when it fails after the restart and before the response ------------------------


async def assert_settled_interrupted(chat: Chat, conversation: uuid.UUID) -> None:
    """The conversation holds its question and one FAILED `interrupted` answer, and nothing is PENDING."""
    assert shape(await messages_of(chat.engine, conversation)) == [
        ("user", "COMPLETED", QUESTION, None),
        ("assistant", "FAILED", "", "interrupted"),
    ]
    assert await pending_count(chat.engine) == 0


async def test_a_retry_that_fails_after_the_restart_settles_the_answer_in_json_and_can_be_retried_at_once(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route has put the answer back to PENDING and then cannot go on (here: the database fails when it reads the
    question). Nobody would ever settle that answer: the route does, FAILED `interrupted`, and the client gets a JSON
    error, not a stream and not a plain-text 500."""
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    secret = "TESTO-SEGRETO-DEL-DATABASE"

    async def broken(*_: Any, **__: Any) -> Any:
        raise OperationalError("select", {}, Exception(secret))

    monkeypatch.setattr(ConversationRepository, "question_of", broken)
    with all_logs() as logs:
        async with chat.api("mario") as api:
            response = await retry(api, conversation, failed)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    code, message = error_of(response)
    assert code == "internal_error"
    assert secret not in message
    assert secret not in "\n".join(line for _, line in logs.lines)
    await assert_settled_interrupted(chat, conversation)
    monkeypatch.undo()
    async with chat.api("mario") as api:  # the conversation is not blocked: the same answer is retried at once
        events = events_of(await retry(api, conversation, failed))
    assert done_message(events)["status"] == "COMPLETED"


async def test_a_retry_that_finds_no_question_settles_the_answer_with_a_500_in_json_and_can_be_retried_at_once(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    original = ConversationRepository.question_of
    calls: list[int] = []

    async def none_once(self: ConversationRepository, answer: Any) -> Any:
        calls.append(1)
        return None if len(calls) == 1 else await original(self, answer)

    monkeypatch.setattr(ConversationRepository, "question_of", none_once)
    async with chat.api("mario") as api:
        response = await retry(api, conversation, failed)
        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/json")
        assert error_of(response)[0] == "internal_error"
        await assert_settled_interrupted(chat, conversation)
        events = events_of(await retry(api, conversation, failed))  # the conversation exists: the second read is real

    assert done_message(events)["status"] == "COMPLETED"


async def test_an_answer_with_no_question_in_an_existing_conversation_is_a_500_and_is_settled(chat: Chat) -> None:
    """Not made by the application, but a retry on it must neither hang nor leave a PENDING answer."""
    conversation = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], FAILED)
    failed = (await messages_of(chat.engine, conversation))[0]["id"]

    async with chat.api("mario") as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 500
    assert error_of(response)[0] == "internal_error"
    (answer,) = await messages_of(chat.engine, conversation)
    assert (answer["status"], answer["error_code"], answer["content"]) == ("FAILED", "interrupted", "")
    assert await pending_count(chat.engine) == 0


async def test_a_retry_whose_conversation_is_deleted_right_after_the_restart_gets_404_conversation_not_found(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conversation goes between the restart and the read of the question: 404 as for a conversation that is not
    there (T-03 does the same when the conversation goes while the question is saved), nothing is left."""
    await seed(chat, [chunk_row(0)])
    conversation, failed = await failed_conversation(chat)
    original = ConversationRepository.question_of

    async def deleted_meanwhile(self: ConversationRepository, answer: Any) -> Any:
        async with chat.engine.begin() as connection:
            await connection.execute(text("delete from conversations where id = :id"), {"id": conversation})
        return await original(self, answer)  # the question is gone with the conversation

    monkeypatch.setattr(ConversationRepository, "question_of", deleted_meanwhile)
    async with chat.api("mario") as api:
        response = await retry(api, conversation, failed)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert error_of(response)[0] == "conversation_not_found"
    assert await table_counts(chat.engine) == (0, 0, 0)
    assert_no_requests(chat)
