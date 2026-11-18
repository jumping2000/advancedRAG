# SPEC-2026-11-17-fase-3-chat
"""The excerpt of a source: AC-019, AC-020 (REQ-010), through the API and through `ConversationRepository.excerpt`
called directly, against the real PostgreSQL (ParadeDB) of compose.test.yaml and the fake Ollama and provider.

`GET /api/conversations/{conversation_id}/messages/{message_id}/sources/{number}` gives the content of the chunk read at
the time of the request, only to whoever can read the conversation. A conversation of another user, of a KB the user is
not (any more) assigned to, a message that is not in the conversation, a source number that is not a cited source of
that message and ids that are not UUIDs are all the same 404 `conversation_not_found` (no new error code). The chunk is
looked up in the KB of the conversation only; when it is not there any more the source is `available: false`, with the
document name saved with the answer and no content. No copy of a chunk is ever written in a table of the chat.

Review focus of T-05: a document deleted between the retrieval and the saving of the answer, or later, is no error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat.repository import ConversationRepository
from app.chat.schemas import SourceExcerpt
from tests.test_chat_ask import (
    Chat,
    ask_new,
    chat_world,
    chunk_row,
    done_message,
    events_of,
    fetch,
    messages_of,
    seed,
    table_counts,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_knowledge_bases import assign
from tests.test_retrieval import Statements

QUESTION = "quokka zibaldone sbirulino"
NOT_FOUND = {"error": {"code": "conversation_not_found", "message": "Conversation not found"}}
EXCERPT_KEYS = {"number", "document_name", "section_title", "page_start", "page_end", "available", "content"}
TEXTS = [
    "Il Frobnicatore9902 si regola con la chiave riservata del tecnico",
    "La Cornamusa4471 non e' citata dalla risposta",
    "Il Girasole6638 richiede una taratura annuale",
    "Il Pennuto2205 e' l'ultimo paragrafo del manuale",
]


@pytest.fixture
async def chat(engine: AsyncEngine) -> Any:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


@dataclass
class Answered:
    """A conversation of `username` in `kb` with one completed answer that cites the sources 1 and 3 of four."""

    conversation: str
    question: str
    answer: str
    chunks: list[uuid.UUID]  # chunks[i] is the chunk of the source number i + 1
    document: uuid.UUID


async def answered(
    chat: Chat,
    username: str = "mario",
    kb: uuid.UUID | None = None,
    fragments: tuple[str, ...] = ("Vedi [1] e [3].",),
    filename: str = "manuale.pdf",
    chunks: list[uuid.UUID] | None = None,
    question: str = QUESTION,
    texts: list[str] | None = None,
) -> Answered:
    """Ask `question` through the API as `username`; the four chunks are seeded unless `chunks` says where they are."""
    kb_id = kb or chat.kb_a
    if chunks is None:
        rows = [
            chunk_row(i, content, question, section_title=f"Sezione {i + 1}", page_start=i + 1, page_end=i + 2)
            for i, content in enumerate(texts or TEXTS)
        ]
        chunks = await seed(chat, rows, kb_id=kb_id, filename=filename)
    chat.provider.fragments = list(fragments)
    async with chat.api(username) as api:
        events = events_of(await ask_new(api, kb_id, question))
    message = done_message(events)
    conversation = events[0][1]["conversation_id"]
    (asked, _) = await messages_of(chat.engine, conversation)
    (row,) = await fetch(chat.engine, "select document_id from chunks where id = :id", id=chunks[0])
    return Answered(conversation, str(asked["id"]), message["id"], chunks, row["document_id"])


def source_url(a: Answered, number: object = 1, message: object = None) -> str:
    return f"/api/conversations/{a.conversation}/messages/{message or a.answer}/sources/{number}"


def assert_not_found(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND


def expected(number: int, content: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "number": number,
        "document_name": "manuale.pdf",
        "section_title": f"Sezione {number}",
        "page_start": number,
        "page_end": number + 1,
        "available": True,
        "content": content,
    }
    body.update(fields)
    return body


# AC-019: the author reads the chunk as it is now ----------------------------------------------------------------


async def test_AC_019_the_author_opens_a_source_and_gets_the_content_of_the_chunk(chat: Chat) -> None:
    a = await answered(chat)

    async with chat.api("mario") as api:
        first = await api.get(source_url(a, 1))
        third = await api.get(source_url(a, 3))

    assert first.status_code == 200, first.text
    assert first.json() == expected(1, TEXTS[0])
    assert third.status_code == 200, third.text
    assert third.json() == expected(3, TEXTS[2])


async def test_AC_019_the_content_is_read_when_the_source_is_opened_not_when_the_answer_was_saved(chat: Chat) -> None:
    a = await answered(chat)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("update chunks set content = :content where id = :id"),
            {"content": "Testo corretto Basilisco8812", "id": a.chunks[0]},
        )

    async with chat.api("mario") as api:
        response = await api.get(source_url(a, 1))

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Testo corretto Basilisco8812"


async def test_AC_019_a_read_writes_nothing_and_leaves_no_copy_of_the_chunk_in_the_chat_tables(chat: Chat) -> None:
    a = await answered(chat)
    before = await chat_text(chat.engine)
    counts = await table_counts(chat.engine)

    async with chat.api("mario") as api:
        assert (await api.get(source_url(a, 1))).status_code == 200
        assert (await api.get(f"/api/conversations/{a.conversation}")).status_code == 200

    assert await table_counts(chat.engine) == counts
    assert await chat_text(chat.engine) == before
    for content in TEXTS:
        assert content.split()[1] not in before


async def chat_text(engine: AsyncEngine) -> str:
    """Every row of the three tables of the chat as text: a copy of a chunk anywhere in them would show here."""
    (row,) = await fetch(
        engine,
        "select coalesce((select string_agg(c::text, E'\\n' order by c.id) from conversations c), '') || E'\\n' ||"
        " coalesce((select string_agg(m::text, E'\\n' order by m.id) from messages m), '') || E'\\n' ||"
        " coalesce((select string_agg(s::text, E'\\n' order by s.message_id, s.number) from message_sources s), '')"
        " as everything",
    )
    text_: str = row["everything"]
    return text_


@pytest.mark.parametrize("username", ["luigi", "peach"])
async def test_AC_019_another_user_gets_404_whether_in_the_kb_or_not(chat: Chat, username: str) -> None:
    a = await answered(chat)

    async with chat.api(username) as api:
        response = await api.get(source_url(a, 1))

    assert_not_found(response)
    for content in TEXTS:
        assert content.split()[1] not in response.text
    assert "manuale.pdf" not in response.text


async def test_AC_019_a_user_removed_from_the_kb_gets_404_and_the_source_is_back_when_reassigned(chat: Chat) -> None:
    a = await answered(chat)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("delete from kb_members where kb_id = :kb and user_id = :user"),
            {"kb": chat.kb_a, "user": chat.users["mario"]},
        )

    async with chat.api("mario") as api:
        removed = await api.get(source_url(a, 1))
    await assign(chat.engine, chat.kb_a, chat.users["mario"])
    async with chat.api("mario") as api:
        back = await api.get(source_url(a, 1))

    assert_not_found(removed)
    assert back.status_code == 200
    assert back.json() == expected(1, TEXTS[0])


async def test_AC_019_without_a_session_the_answer_is_401(chat: Chat) -> None:
    a = await answered(chat)

    async with chat.api(None) as api:
        response = await api.get(source_url(a, 1))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_AC_019_the_admin_reads_the_source_of_a_conversation_of_another_user(chat: Chat) -> None:
    a = await answered(chat)

    async with chat.api("anna") as api:
        response = await api.get(source_url(a, 3))

    assert response.status_code == 200, response.text
    assert response.json() == expected(3, TEXTS[2])


# What is not a source of this message is the same 404 ------------------------------------------------------------


async def test_AC_019_ids_and_numbers_that_do_not_name_a_cited_source_of_the_message_are_404(chat: Chat) -> None:
    a = await answered(chat)
    other = await answered(chat, chunks=a.chunks, fragments=("Solo [2].",))
    unknown = uuid.uuid4()
    wrong = [
        (a, 2, None),  # in the context of the answer but not cited
        (a, 4, None),  # in the context, not cited
        (a, 5, None),  # no such number
        (a, 0, None),
        (a, -1, None),
        (a, "abc", None),
        (a, "1.0", None),
        (a, "1_0", None),
        (a, "１", None),  # a digit that is not ASCII
        (a, "99999999999999999999", None),  # beyond the integer of the database
        (a, 1, unknown),  # an unknown message
        (a, 1, "not-a-uuid"),
        (a, 1, a.question),  # a message of the conversation that is not an answer
        (a, 1, other.answer),  # the answer of another conversation of the same user
        (a, 3, other.answer),
        (a, 1, str(uuid.UUID(int=0))),
    ]

    async with chat.api("mario") as api:
        responses = [await api.get(source_url(c, number, message)) for c, number, message in wrong]
        own = await api.get(source_url(other, 2))  # the cited one of the other answer is readable in its own
        crossed = await api.get(source_url(other, 1))  # ... and not the source 1 of the first answer

    for response in responses:
        assert_not_found(response)
    assert own.status_code == 200
    assert crossed.status_code == 404


async def test_AC_019_a_message_without_sources_has_no_source(chat: Chat) -> None:
    a = await answered(chat, fragments=("Nessuna citazione.",))

    async with chat.api("mario") as api:
        responses = [await api.get(source_url(a, number)) for number in (1, 2, 3, 4)]

    for response in responses:
        assert_not_found(response)


# AC-020: the chunk is gone -----------------------------------------------------------------------------------


async def delete_document(chat: Chat, a: Answered) -> None:
    async with chat.api("anna") as admin:
        response = await admin.delete(f"/api/kbs/{chat.kb_a}/documents/{a.document}")
    assert response.status_code == 204, response.text


async def test_AC_020_after_the_document_is_deleted_the_source_is_unavailable_with_its_saved_name(chat: Chat) -> None:
    a = await answered(chat, filename="segreto-Pippo3318.pdf")
    await delete_document(chat, a)

    async with chat.api("mario") as api:
        conversation = await api.get(f"/api/conversations/{a.conversation}")
        first = await api.get(source_url(a, 1))
        third = await api.get(source_url(a, 3))
        not_cited = await api.get(source_url(a, 2))

    assert conversation.status_code == 200, conversation.text
    (answer,) = [m for m in conversation.json()["messages"] if m["id"] == a.answer]
    assert [s["number"] for s in answer["sources"]] == [1, 3]  # the metadata saved with the answer is still there
    for response, number in ((first, 1), (third, 3)):
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == EXCERPT_KEYS
        assert body == expected(number, None, document_name="segreto-Pippo3318.pdf", available=False)
    assert_not_found(not_cited)


async def test_AC_020_no_table_of_the_chat_holds_a_copy_of_the_content_of_the_chunk(chat: Chat) -> None:
    a = await answered(chat)
    async with chat.api("mario") as api:
        assert (await api.get(source_url(a, 1))).status_code == 200  # read while it exists: nothing is kept
    await delete_document(chat, a)
    (gone,) = await fetch(chat.engine, "select count(*) as n from chunks where document_id = :id", id=a.document)
    assert gone["n"] == 0

    everything = await chat_text(chat.engine)

    for content in TEXTS:
        for word in content.split():
            if word[-4:].isdigit():  # the marker of the text: Frobnicatore9902, Cornamusa4471, ...
                assert word not in everything, word
    # not in any column of any table of the chat either (the text above is all of them as text)
    assert (await fetch(chat.engine, "select count(*) as n from message_sources"))[0]["n"] == 4


async def test_AC_020_the_document_deleted_with_its_kb_takes_the_conversation_with_it(chat: Chat) -> None:
    a = await answered(chat)
    async with chat.api("anna") as admin:
        assert (await admin.delete(f"/api/kbs/{chat.kb_a}")).status_code == 204

    async with chat.api("mario") as api:
        response = await api.get(source_url(a, 1))

    assert_not_found(response)


async def test_AC_020_a_chunk_that_is_in_another_kb_is_not_available_even_if_it_exists(chat: Chat) -> None:
    """The chunk is looked up in the KB of the conversation only: a source whose chunk id belongs to a chunk of another
    KB (never written by the application) is shown as gone, and the content of that chunk is not returned."""
    a = await answered(chat)
    foreign = await seed(chat, [chunk_row(0, "Il Segreto7777 della KB Procedure")], kb_id=chat.kb_b)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("update message_sources set chunk_id = :chunk where message_id = :message and number = 1"),
            {"chunk": foreign[0], "message": uuid.UUID(a.answer)},
        )

    async with chat.api("mario") as api:
        response = await api.get(source_url(a, 1))

    assert response.status_code == 200, response.text
    assert response.json() == expected(1, None, available=False)
    assert "Segreto7777" not in response.text


# ConversationRepository.excerpt, called directly ---------------------------------------------------------------


async def test_excerpt_of_the_repository_gives_the_live_chunk_and_none_for_what_is_not_a_cited_source(
    chat: Chat,
) -> None:
    a = await answered(chat)
    other = await answered(chat, chunks=a.chunks, fragments=("Solo [2].",))

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        mario = await chat.user(session, "mario")
        conversation = await repository.get_for(mario, uuid.UUID(a.conversation))
        assert conversation is not None
        found = await repository.excerpt(conversation, uuid.UUID(a.answer), 1)
        unavailable = await repository.excerpt(conversation, uuid.UUID(a.answer), 2)  # not cited
        no_number = await repository.excerpt(conversation, uuid.UUID(a.answer), 9)
        of_another_conversation = await repository.excerpt(conversation, uuid.UUID(other.answer), 2)
        no_message = await repository.excerpt(conversation, uuid.uuid4(), 1)

    assert isinstance(found, SourceExcerpt)
    assert found.model_dump() == expected(1, TEXTS[0])
    assert unavailable is None
    assert no_number is None
    assert of_another_conversation is None  # the message exists, in another conversation of the same user
    assert no_message is None


async def test_excerpt_reads_the_chunk_in_the_same_statement_as_the_source_and_scoped_to_the_conversation(
    chat: Chat,
) -> None:
    a = await answered(chat)

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        conversation = await repository.get_for(await chat.user(session, "mario"), uuid.UUID(a.conversation))
        assert conversation is not None
        with Statements(chat.engine) as statements:
            await repository.excerpt(conversation, uuid.UUID(a.answer), 1)

    reading = statements.reading_chunks()
    assert len(reading) == 1, reading  # one statement, not a read of the source and then a read of the chunk
    assert "message_sources" in reading[0]
    assert "kb_id" in reading[0]  # the chunk is looked up in the KB of the conversation
