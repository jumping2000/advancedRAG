# SPEC-2026-11-17-fase-3-chat
"""The conversations of the chat: list, open, rename, delete; the admin reads and deletes all, writes none of others.
AC-022, AC-026, AC-027, AC-028, AC-029, AC-030, AC-037 (REQ-011, REQ-012, REQ-013, REQ-018), through the API against the
real PostgreSQL (ParadeDB) of compose.test.yaml, the fake Ollama and the fake provider.

Contracts fixed here (plan: Interfaces), all errors `{"error": {"code", "message"}}`:
- `GET /api/kbs/{kb_id}/conversations`: the caller's own conversations of the KB, by `updated_at` (then id) descending;
  404 `kb_not_found` for a KB the caller cannot see.
- `GET /api/conversations/{id}`: `ConversationDetail` with all the messages in order (PENDING and FAILED ones too, with
  their status) and the cited sources only; `read_only` is true when the caller is not the author (the admin).
- `PATCH /api/conversations/{id}` `{"title"}`: the title is trimmed, 1 to 200 characters, else 422 `validation_error`.
  A rename is not an activity: it does not move the conversation in the list.
- `DELETE /api/conversations/{id}`: 204; the author or the admin; it stops the answer that is being generated.
- `GET /api/admin/conversations?kb_id=&user_id=`: every conversation with `username`, `kb_name` and `message_count`
  (all the messages of the conversation); admin only (403 before anything else is looked at); an id that matches
  nothing, or that is not a UUID, is an empty list; an empty value is no filter.

Real sockets and timing only where the test needs them (AC-037: the provider sees its connection closed).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat import service
from app.chat.repository import (
    ConversationGone,
    ConversationRepository,
    MessageNotFound,
)
from app.knowledge_bases.repository import KnowledgeBaseRepository
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes.services import delta_event
from tests.test_chat_ask import (
    BASE_TIME,
    QUESTION,
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
    seed,
    table_counts,
)
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_sources import answered, source_url
from tests.test_chat_streaming import SECONDS, events, eventually, running, signed_in
from tests.test_knowledge_bases import ADMIN, ADMIN_PASSWORD, PASSWORD, login

NOT_FOUND = {"error": {"code": "conversation_not_found", "message": "Conversation not found"}}
CONVERSATION_KEYS = {"id", "kb_id", "title", "created_at", "updated_at"}
DETAIL_KEYS = CONVERSATION_KEYS | {"messages", "read_only"}
ADMIN_ROW_KEYS = CONVERSATION_KEYS | {"username", "kb_name", "message_count"}
MESSAGE_KEYS = {"id", "role", "content", "status", "error_code", "notice", "created_at", "sources", "search_query"}
SOURCE_KEYS = {"number", "document_name", "section_title", "page_start", "page_end"}
SLOW = [delta_event("Inizio "), *([b": slow\n\n", 0.1] * 600)]  # a fragment, then a comment every 0.1 s for a minute


@pytest.fixture
async def chat(engine: AsyncEngine) -> Any:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


@asynccontextmanager
async def sharing_an_app(chat: Chat, *usernames: str) -> AsyncIterator[dict[str, httpx.AsyncClient]]:
    """Clients signed in as these users, all of one app: the one process of production. The registry of the answers
    being generated belongs to the app, so a delete must come through the app that generates the answer."""
    app = create_app(chat.settings_with())
    transport = httpx.ASGITransport(app=app)
    clients = {name: httpx.AsyncClient(transport=transport, base_url="http://testserver") for name in usernames}
    try:
        for name, client in clients.items():
            password = ADMIN_PASSWORD if name == ADMIN else PASSWORD
            assert (await login(client, name, password)).status_code == 200
        yield clients
    finally:
        for client in clients.values():
            await client.aclose()
        await app.state.engine.dispose()
        await app.state.redis.aclose()


def at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


async def touch(engine: AsyncEngine, conversation_id: uuid.UUID | str, when: datetime) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("update conversations set updated_at = :at where id = :id"),
            {"at": when, "id": uuid.UUID(str(conversation_id))},
        )


async def snapshot(engine: AsyncEngine) -> tuple[Any, ...]:
    """Every row of the chat tables: what a refused request must leave exactly as it was."""
    return (
        await fetch(engine, "select * from conversations order by id"),
        await fetch(engine, "select * from messages order by id"),
        await fetch(engine, "select * from message_sources order by message_id, number"),
    )


def ids_of(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()]


def conversation_url(conversation_id: object) -> str:
    return f"/api/conversations/{conversation_id}"


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


# The list of the conversations of a KB ------------------------------------------------------------------------------


async def test_the_list_of_a_kb_has_the_own_conversations_by_last_activity_with_the_documented_fields(
    chat: Chat,
) -> None:
    older = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "a"), title="Più vecchia")
    newer = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "c"), title="Più recente")
    middle = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "b"), title="Di mezzo")
    await add_conversation(chat.engine, chat.kb_a, chat.users["luigi"], ("user", "d"), title="Di Luigi")
    await add_conversation(chat.engine, chat.kb_b, chat.users["mario"], ("user", "e"), title="In un'altra KB")
    for conversation, minute in ((older, 1), (middle, 5), (newer, 9)):
        await touch(chat.engine, conversation, at(minute))

    async with chat.api("mario") as api:
        response = await api.get(f"/api/kbs/{chat.kb_a}/conversations")

    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [str(newer), str(middle), str(older)]
    assert [item["title"] for item in items] == ["Più recente", "Di mezzo", "Più vecchia"]
    for item in items:
        assert set(item) == CONVERSATION_KEYS
        assert item["kb_id"] == str(chat.kb_a)
    assert [datetime.fromisoformat(item["updated_at"]) for item in items] == [at(9), at(5), at(1)]


async def test_conversations_with_the_same_last_activity_are_listed_by_id_descending(chat: Chat) -> None:
    created = [
        await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title=f"Uguale {i}")
        for i in range(4)
    ]  # the same `updated_at` for all

    async with chat.api("mario") as api:
        first = ids_of(await api.get(f"/api/kbs/{chat.kb_a}/conversations"))
        second = ids_of(await api.get(f"/api/kbs/{chat.kb_a}/conversations"))

    assert first == second == [str(i) for i in sorted(created, reverse=True)]


async def test_a_kb_without_conversations_of_the_caller_gives_an_empty_list(chat: Chat) -> None:
    await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"))

    async with chat.api("luigi") as api:
        response = await api.get(f"/api/kbs/{chat.kb_b}/conversations")  # luigi is in kb_b, with none there

    assert response.status_code == 200
    assert response.json() == []


async def test_the_list_of_the_admin_has_the_conversations_of_the_admin_only(chat: Chat) -> None:
    await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title="Di Mario")
    own = await add_conversation(chat.engine, chat.kb_a, chat.users["anna"], ("user", "y"), title="Di Anna")

    async with chat.api("anna") as api:
        response = await api.get(f"/api/kbs/{chat.kb_a}/conversations")

    assert ids_of(response) == [str(own)]  # the others' conversations are read in the admin page, not here


@pytest.mark.parametrize("kb", ["not-a-uuid", "123", str(uuid.UUID(int=7))])
async def test_the_list_of_a_kb_the_caller_cannot_see_is_404_kb_not_found(chat: Chat, kb: str) -> None:
    async with chat.api("mario") as api:
        response = await api.get(f"/api/kbs/{kb}/conversations")
        not_assigned = await api.get(f"/api/kbs/{chat.kb_b}/conversations")

    assert_error(response, 404, "kb_not_found")
    assert_error(not_assigned, 404, "kb_not_found")
    assert response.json() == not_assigned.json()


# Opening a conversation ---------------------------------------------------------------------------------------------


async def test_the_detail_has_the_messages_in_order_with_the_cited_sources_only_and_is_not_read_only(
    chat: Chat,
) -> None:
    a = await answered(chat)

    async with chat.api("mario") as api:
        response = await api.get(conversation_url(a.conversation))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == DETAIL_KEYS
    assert body["id"] == a.conversation
    assert body["kb_id"] == str(chat.kb_a)
    assert body["title"] == QUESTION
    assert body["read_only"] is False
    asked, answer = body["messages"]
    assert set(asked) == MESSAGE_KEYS == set(answer)
    assert (asked["id"], asked["role"], asked["content"], asked["status"]) == (
        a.question,
        "user",
        QUESTION,
        "COMPLETED",
    )
    assert asked["sources"] == []
    assert (answer["id"], answer["role"], answer["content"], answer["status"]) == (
        a.answer,
        "assistant",
        "Vedi [1] e [3].",
        "COMPLETED",
    )
    assert (answer["error_code"], answer["notice"]) == (None, None)
    assert answer["sources"] == [  # the cited ones, 1 and 3; the 2 and the 4 were in the context but are not cited
        {
            "number": 1,
            "document_name": "manuale.pdf",
            "section_title": "Sezione 1",
            "page_start": 1,
            "page_end": 2,
        },
        {
            "number": 3,
            "document_name": "manuale.pdf",
            "section_title": "Sezione 3",
            "page_start": 3,
            "page_end": 4,
        },
    ]
    for source in answer["sources"]:
        assert set(source) == SOURCE_KEYS  # the excerpt is never part of a message


async def test_the_detail_shows_failed_and_pending_answers_with_their_status_and_a_notice(chat: Chat) -> None:
    conversation = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "q1"),
        ("assistant", "", "FAILED", "generation_error"),
        ("user", "q2"),
        ("assistant", "", "PENDING"),
    )

    async with chat.api("mario") as api:
        response = await api.get(conversation_url(conversation))

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [(m["role"], m["status"], m["error_code"], m["content"]) for m in messages] == [
        ("user", "COMPLETED", None, "q1"),
        ("assistant", "FAILED", "generation_error", ""),
        ("user", "COMPLETED", None, "q2"),
        ("assistant", "PENDING", None, ""),
    ]
    assert all(m["sources"] == [] for m in messages)


async def test_the_detail_shows_the_notice_of_an_answer_without_documents(chat: Chat) -> None:
    async with chat.api("luigi") as api:  # no document in kb_b
        events = events_of(await ask_new(api, chat.kb_b, QUESTION))
        conversation = events[0][1]["conversation_id"]
        response = await api.get(conversation_url(conversation))

    (_, answer) = response.json()["messages"]
    assert (answer["status"], answer["notice"], answer["content"], answer["sources"]) == (
        "COMPLETED",
        "no_documents",
        "",
        [],
    )


@pytest.mark.parametrize("target", ["unknown", "not-a-uuid"])
async def test_an_unknown_conversation_is_404_conversation_not_found_on_every_method(chat: Chat, target: str) -> None:
    conversation = str(uuid.uuid4()) if target == "unknown" else target

    async with chat.api("mario") as api:
        responses = [
            await api.get(conversation_url(conversation)),
            await api.patch(conversation_url(conversation), json={"title": "Nuovo"}),
            await api.delete(conversation_url(conversation)),
        ]

    for response in responses:
        assert_error(response, 404, "conversation_not_found")
        assert response.json() == NOT_FOUND


# AC-022: rename -----------------------------------------------------------------------------------------------------


async def test_AC_022_a_title_is_saved_without_the_spaces_at_both_ends(chat: Chat) -> None:
    conversation = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title="Vecchio")

    async with chat.api("mario") as api:
        response = await api.patch(conversation_url(conversation), json={"title": "  Budget 2026  "})
        listed = await api.get(f"/api/kbs/{chat.kb_a}/conversations")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CONVERSATION_KEYS
    assert (body["id"], body["kb_id"], body["title"]) == (str(conversation), str(chat.kb_a), "Budget 2026")
    assert [c["title"] for c in listed.json()] == ["Budget 2026"]
    (row,) = await fetch(chat.engine, "select title from conversations")
    assert row["title"] == "Budget 2026"


@pytest.mark.parametrize(
    "bad",
    [
        "   ",
        "",
        "\t\n ",
        "x" * 201,
        "  " + "x" * 201 + "  ",
        "è" * 201,
        "a\x00b",  # the database cannot hold it
    ],
)
async def test_AC_022_an_empty_or_too_long_title_gets_422_and_the_title_stays(chat: Chat, bad: str) -> None:
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title="Resta così"
    )
    before = await snapshot(chat.engine)

    async with chat.api("mario") as api:
        response = await api.patch(conversation_url(conversation), json={"title": bad})

    assert_error(response, 422, "validation_error")
    assert await snapshot(chat.engine) == before


@pytest.mark.parametrize("body", [{}, {"title": None}, {"title": 5}, {"title": ["x"]}, ["x"], "x", None])
async def test_AC_022_a_body_without_a_title_string_gets_422_and_the_title_stays(chat: Chat, body: Any) -> None:
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title="Resta così"
    )
    before = await snapshot(chat.engine)

    async with chat.api("mario") as api:
        response = await api.patch(conversation_url(conversation), json=body)

    assert_error(response, 422, "validation_error")
    assert await snapshot(chat.engine) == before


async def test_AC_022_a_title_that_is_not_json_gets_422_and_the_title_stays(chat: Chat) -> None:
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"), title="Resta così"
    )
    before = await snapshot(chat.engine)

    async with chat.api("mario") as api:
        response = await api.patch(
            conversation_url(conversation), content=b"{not json", headers={"Content-Type": "application/json"}
        )

    assert_error(response, 422, "validation_error")
    assert await snapshot(chat.engine) == before


@pytest.mark.parametrize(
    ("sent", "saved"),
    [
        ("x", "x"),
        ("x" * 200, "x" * 200),
        ("  " + "è" * 200 + "  ", "è" * 200),  # the spaces do not count in the 200 characters
        ("  due  parole  ", "due  parole"),
    ],
)
async def test_AC_022_the_limits_of_the_title_are_1_and_200_characters_after_the_trim(
    chat: Chat, sent: str, saved: str
) -> None:
    conversation = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "x"))

    async with chat.api("mario") as api:
        response = await api.patch(conversation_url(conversation), json={"title": sent})

    assert response.status_code == 200, response.text
    assert response.json()["title"] == saved
    (row,) = await fetch(chat.engine, "select title from conversations")
    assert row["title"] == saved


async def test_a_rename_does_not_count_as_activity_the_order_of_the_list_stays(chat: Chat) -> None:
    older = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "a"), title="Vecchia")
    newer = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], ("user", "b"), title="Nuova")
    await touch(chat.engine, older, at(1))
    await touch(chat.engine, newer, at(9))

    async with chat.api("mario") as api:
        renamed = await api.patch(conversation_url(older), json={"title": "Vecchia rinominata"})
        listed = ids_of(await api.get(f"/api/kbs/{chat.kb_a}/conversations"))

    assert renamed.status_code == 200
    assert datetime.fromisoformat(renamed.json()["updated_at"]) == at(1)
    assert listed == [str(newer), str(older)]


# AC-026: a user taken out of a KB ------------------------------------------------------------------------------------


async def test_AC_026_a_user_taken_out_of_the_kb_loses_the_conversation_until_the_admin_assigns_the_kb_again(
    chat: Chat,
) -> None:
    a = await answered(chat)
    luigis = await add_conversation(chat.engine, chat.kb_a, chat.users["luigi"], ("user", "di luigi"))
    members = f"/api/kbs/{chat.kb_a}/members/{chat.users['mario']}"

    async with chat.api("mario") as mario, chat.api("anna") as admin, chat.api("luigi") as luigi:
        before = (await mario.get(conversation_url(a.conversation))).json()
        assert ids_of(await mario.get(f"/api/kbs/{chat.kb_a}/conversations")) == [a.conversation]

        assert (await admin.delete(members)).status_code == 204
        snapshot_after_removal = await snapshot(chat.engine)
        hidden = [
            await mario.get(f"/api/kbs/{chat.kb_a}/conversations"),
            await mario.get(conversation_url(a.conversation)),
            await mario.patch(conversation_url(a.conversation), json={"title": "Nuovo"}),
            await ask(mario, a.conversation, "quokka"),
            await mario.post(f"{conversation_url(a.conversation)}/messages/{a.answer}/retry"),
            await mario.get(source_url(a, 1)),
            await mario.delete(conversation_url(a.conversation)),
        ]
        still_there = await snapshot(chat.engine)
        luigis_view = ids_of(await luigi.get(f"/api/kbs/{chat.kb_a}/conversations"))

        assert (await admin.put(members)).status_code == 204
        listed = ids_of(await mario.get(f"/api/kbs/{chat.kb_a}/conversations"))
        after = (await mario.get(conversation_url(a.conversation))).json()
        source = await mario.get(source_url(a, 1))

    assert_error(hidden[0], 404, "kb_not_found")
    for response in hidden[1:]:
        assert response.status_code == 404, response.text
        assert response.json() == NOT_FOUND
    assert (
        still_there == snapshot_after_removal
    )  # nothing was written, nothing deleted: the conversation is only hidden
    assert luigis_view == [str(luigis)]
    assert listed == [a.conversation]
    assert after == before  # with all its messages and sources
    assert len(after["messages"]) == 2
    assert source.status_code == 200
    assert len(chat.provider.completions()) == 1  # only the first answer ran: the refused requests sent nothing


# AC-027: the admin lists every conversation --------------------------------------------------------------------------


async def admin_world(chat: Chat) -> dict[str, uuid.UUID]:
    """mario: two in kb_a (the second with 4 messages: failed and pending answers); luigi: one in kb_a and one in kb_b;
    anna: one in kb_b."""
    users = chat.users
    world = {
        "mario_one": await add_conversation(
            chat.engine, chat.kb_a, users["mario"], ("user", "a"), ("assistant", "b"), title="Mario uno"
        ),
        "mario_two": await add_conversation(
            chat.engine,
            chat.kb_a,
            users["mario"],
            ("user", "a"),
            ("assistant", "", "FAILED", "generation_error"),
            ("user", "b"),
            ("assistant", "", "PENDING"),
            title="Mario due",
        ),
        "luigi_a": await add_conversation(
            chat.engine, chat.kb_a, users["luigi"], ("user", "a"), ("assistant", "b"), title="Luigi A"
        ),
        "luigi_b": await add_conversation(chat.engine, chat.kb_b, users["luigi"], ("user", "a"), title="Luigi B"),
        "anna_b": await add_conversation(chat.engine, chat.kb_b, users["anna"], ("user", "a"), title="Anna B"),
    }
    for name, minute in (("mario_one", 1), ("luigi_a", 3), ("anna_b", 5), ("mario_two", 7), ("luigi_b", 9)):
        await touch(chat.engine, world[name], at(minute))
    return world


def listed_titles(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()]


async def test_AC_027_the_admin_lists_all_the_conversations_then_by_kb_then_by_user(chat: Chat) -> None:
    await admin_world(chat)
    users = chat.users

    async with chat.api("anna") as admin:
        everything = await admin.get("/api/admin/conversations")
        of_kb_a = await admin.get(f"/api/admin/conversations?kb_id={chat.kb_a}")
        of_kb_b = await admin.get(f"/api/admin/conversations?kb_id={chat.kb_b}")
        of_luigi = await admin.get(f"/api/admin/conversations?user_id={users['luigi']}")
        of_mario = await admin.get(f"/api/admin/conversations?user_id={users['mario']}")
        of_anna = await admin.get(f"/api/admin/conversations?user_id={users['anna']}")
        both = await admin.get(f"/api/admin/conversations?kb_id={chat.kb_a}&user_id={users['luigi']}")
        nothing = await admin.get(f"/api/admin/conversations?kb_id={chat.kb_a}&user_id={users['anna']}")

    # by last activity, newest first
    assert listed_titles(everything) == ["Luigi B", "Mario due", "Anna B", "Luigi A", "Mario uno"]
    assert listed_titles(of_kb_a) == ["Mario due", "Luigi A", "Mario uno"]
    assert listed_titles(of_kb_b) == ["Luigi B", "Anna B"]
    assert listed_titles(of_luigi) == ["Luigi B", "Luigi A"]
    assert listed_titles(of_mario) == ["Mario due", "Mario uno"]
    assert listed_titles(of_anna) == ["Anna B"]
    assert listed_titles(both) == ["Luigi A"]
    assert listed_titles(nothing) == []


async def test_AC_027_each_row_has_the_user_the_kb_and_the_number_of_messages(chat: Chat) -> None:
    world = await admin_world(chat)

    async with chat.api("anna") as admin:
        rows = (await admin.get("/api/admin/conversations")).json()

    by_title = {row["title"]: row for row in rows}
    for row in rows:
        assert set(row) == ADMIN_ROW_KEYS
    luigi_b = by_title["Luigi B"]
    assert (luigi_b["id"], luigi_b["kb_id"], luigi_b["username"], luigi_b["kb_name"], luigi_b["message_count"]) == (
        str(world["luigi_b"]),
        str(chat.kb_b),
        "luigi",
        "Procedure",
        1,
    )
    assert (by_title["Mario uno"]["username"], by_title["Mario uno"]["kb_name"]) == ("mario", "Manuali")
    assert by_title["Mario uno"]["message_count"] == 2
    assert by_title["Mario due"]["message_count"] == 4  # every message of the conversation, failed and pending too
    assert (by_title["Anna B"]["username"], by_title["Anna B"]["kb_name"]) == ("anna", "Procedure")
    assert datetime.fromisoformat(luigi_b["updated_at"]) == at(9)


@pytest.mark.parametrize(
    "filters", ["kb_id=not-a-uuid", "user_id=not-a-uuid", f"kb_id={uuid.UUID(int=9)}", f"user_id={uuid.UUID(int=9)}"]
)
async def test_AC_027_a_filter_that_matches_nothing_or_is_not_a_uuid_gives_an_empty_list(
    chat: Chat, filters: str
) -> None:
    await admin_world(chat)

    async with chat.api("anna") as admin:
        response = await admin.get(f"/api/admin/conversations?{filters}")

    assert response.status_code == 200
    assert response.json() == []


async def test_AC_027_an_empty_filter_is_no_filter(chat: Chat) -> None:
    await admin_world(chat)

    async with chat.api("anna") as admin:
        response = await admin.get("/api/admin/conversations?kb_id=&user_id=")

    assert len(listed_titles(response)) == 5


@pytest.mark.parametrize("username", ["mario", "luigi", "peach"])
@pytest.mark.parametrize("query", ["", "?kb_id=not-a-uuid&user_id=x", "?kb_id=&user_id=", "?unknown=1"])
async def test_AC_027_a_user_gets_403_forbidden_whatever_the_query(chat: Chat, username: str, query: str) -> None:
    await admin_world(chat)

    async with chat.api(username) as api:
        response = await api.get(f"/api/admin/conversations{query}")

    assert_error(response, 403, "forbidden")
    assert "Mario" not in response.text and "Luigi" not in response.text


async def test_AC_027_without_a_session_the_answer_is_401(chat: Chat) -> None:
    async with chat.api(None) as api:
        response = await api.get("/api/admin/conversations")

    assert_error(response, 401, "not_authenticated")


# AC-028: the admin reads, never writes the conversation of another user ---------------------------------------------


async def test_AC_028_the_admin_opens_the_conversation_of_a_user_read_only_with_its_messages_and_sources(
    chat: Chat,
) -> None:
    a = await answered(chat)

    async with chat.api("anna") as admin, chat.api("mario") as mario:
        detail = await admin.get(conversation_url(a.conversation))
        source = await admin.get(source_url(a, 1))
        authors = await mario.get(conversation_url(a.conversation))

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert set(body) == DETAIL_KEYS
    assert body["read_only"] is True
    assert [m["content"] for m in body["messages"]] == [QUESTION, "Vedi [1] e [3]."]
    assert [s["number"] for s in body["messages"][1]["sources"]] == [1, 3]
    assert source.status_code == 200, source.text
    assert source.json()["available"] is True and source.json()["content"]
    # the same conversation as its author sees it, but for the flag
    assert {**body, "read_only": False} == authors.json()


async def test_AC_028_a_question_a_retry_and_a_rename_by_the_admin_get_403_and_nothing_changes(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    conversation = await add_conversation(
        chat.engine,
        chat.kb_a,
        chat.users["mario"],
        ("user", "q"),
        ("assistant", "", "FAILED", "generation_error"),
        title="Di Mario",
    )
    (_, failed) = await messages_of(chat.engine, conversation)
    before = await snapshot(chat.engine)
    url = conversation_url(conversation)

    async with chat.api("anna") as admin:
        responses = [
            await ask(admin, conversation, "Una domanda dell'admin"),
            await admin.post(f"{url}/messages/{failed['id']}/retry"),
            await admin.patch(url, json={"title": "Rinominata dall'admin"}),
            await admin.patch(url, json={"title": "   "}),  # the 403 comes before the validation of the title
            await ask(admin, conversation, "   "),  # and before the validation of the question
        ]

    for response in responses:
        assert_error(response, 403, "forbidden")
        assert not response.headers["content-type"].startswith("text/event-stream")
    assert await snapshot(chat.engine) == before
    assert chat.provider.requests == []
    assert chat.ollama.requests == []


async def test_AC_028_the_admin_deletes_the_conversation_of_a_user(chat: Chat) -> None:
    a = await answered(chat)
    other = await answered(chat, chunks=a.chunks, fragments=("Solo [2].",))

    async with chat.api("anna") as admin, chat.api("mario") as mario:
        deleted = await admin.delete(conversation_url(a.conversation))
        again = await admin.delete(conversation_url(a.conversation))
        gone = await mario.get(conversation_url(a.conversation))
        listed = ids_of(await mario.get(f"/api/kbs/{chat.kb_a}/conversations"))
        in_admin_list = ids_of(await admin.get("/api/admin/conversations"))

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert_error(again, 404, "conversation_not_found")
    assert_error(gone, 404, "conversation_not_found")
    assert listed == in_admin_list == [other.conversation]
    assert await table_counts(chat.engine) == (1, 2, 4)  # only the other conversation is left, with its sources
    assert await messages_of(chat.engine, a.conversation) == []


async def test_AC_028_the_conversations_of_the_admin_are_writable_and_not_read_only(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])

    async with chat.api("anna") as admin:
        events = events_of(await ask_new(admin, chat.kb_a, QUESTION))
        conversation = events[0][1]["conversation_id"]
        detail = await admin.get(conversation_url(conversation))
        renamed = await admin.patch(conversation_url(conversation), json={"title": "Dell'admin"})
        asked = events_of(await ask(admin, conversation, "quokka ancora"))
        deleted = await admin.delete(conversation_url(conversation))

    assert detail.json()["read_only"] is False
    assert renamed.status_code == 200 and renamed.json()["title"] == "Dell'admin"
    assert names_of(asked)[-1] == "done"
    assert deleted.status_code == 204


async def test_AC_028_for_the_admin_an_unknown_conversation_is_404_not_403(chat: Chat) -> None:
    async with chat.api("anna") as admin:
        responses = [
            await admin.get(conversation_url(uuid.uuid4())),
            await ask(admin, uuid.uuid4(), "quokka"),
            await admin.patch(conversation_url("not-a-uuid"), json={"title": "x"}),
            await admin.delete(conversation_url(uuid.uuid4())),
        ]

    for response in responses:
        assert response.status_code == 404
        assert response.json() == NOT_FOUND


# AC-029: deleting a KB --------------------------------------------------------------------------------------------


async def test_AC_029_deleting_a_kb_deletes_its_conversations_messages_and_sources_and_only_those(chat: Chat) -> None:
    first = await answered(chat, "mario")
    second = await answered(chat, "luigi", chunks=first.chunks, fragments=("Vedi [2].",))
    pending = await add_conversation(
        chat.engine, chat.kb_a, chat.users["anna"], ("user", "q"), ("assistant", "", "PENDING")
    )
    control = await answered(
        chat, "luigi", kb=chat.kb_b, texts=["Altro testo Marmotta1201", "Ancora Narvalo7305"], filename="altro.pdf"
    )
    doomed = [first.conversation, second.conversation, str(pending)]
    assert (await table_counts(chat.engine))[0] == 4

    async with chat.api("anna") as admin:
        deleted = await admin.delete(f"/api/kbs/{chat.kb_a}")
    assert deleted.status_code == 204

    for conversation in doomed:
        assert await messages_of(chat.engine, conversation) == []
    (rows,) = await fetch(
        chat.engine,
        "select (select count(*) from conversations where kb_id = :kb) as conversations,"
        " (select count(*) from message_sources where message_id = any(cast(:ids as uuid[]))) as sources",
        kb=chat.kb_a,
        ids=[uuid.UUID(first.answer), uuid.UUID(second.answer)],
    )
    assert (rows["conversations"], rows["sources"]) == (0, 0)
    assert (await table_counts(chat.engine))[0] == 1  # the conversation of the other KB is untouched ...
    async with chat.api("luigi") as luigi, chat.api("mario") as mario:
        assert (await luigi.get(source_url(control, 1))).status_code == 200  # ... with its messages and sources
        assert ids_of(await luigi.get(f"/api/kbs/{chat.kb_b}/conversations")) == [control.conversation]
        gone = await mario.get(conversation_url(first.conversation))
        no_kb = await mario.get(f"/api/kbs/{chat.kb_a}/conversations")
    assert_error(gone, 404, "conversation_not_found")
    assert_error(no_kb, 404, "kb_not_found")


# AC-030: a deactivated user keeps the conversations ---------------------------------------------------------------


async def test_AC_030_deactivating_and_reactivating_a_user_keeps_their_conversations(chat: Chat) -> None:
    a = await answered(chat)
    mario = f"/api/admin/users/{chat.users['mario']}"
    before = await snapshot(chat.engine)

    async with chat.api("mario") as signed, chat.api("anna") as admin:
        detail_before = (await signed.get(conversation_url(a.conversation))).json()
        assert (await admin.patch(mario, json={"is_active": False})).status_code == 200
        refused = await signed.get(conversation_url(a.conversation))  # the session of a deactivated user is over
        kept = await snapshot(chat.engine)
        in_admin_list = (await admin.get(f"/api/admin/conversations?user_id={chat.users['mario']}")).json()
        read_by_admin = await admin.get(conversation_url(a.conversation))
        assert (await admin.patch(mario, json={"is_active": True})).status_code == 200

    async with chat.api("mario") as again:
        listed = ids_of(await again.get(f"/api/kbs/{chat.kb_a}/conversations"))
        detail_after = (await again.get(conversation_url(a.conversation))).json()
        source = await again.get(source_url(a, 1))

    assert refused.status_code == 401
    assert kept == before
    assert [row["id"] for row in in_admin_list] == [a.conversation]
    assert in_admin_list[0]["username"] == "mario"
    assert read_by_admin.status_code == 200
    assert listed == [a.conversation]
    assert detail_after == detail_before
    assert source.status_code == 200
    assert await snapshot(chat.engine) == before


# AC-037: deleting a conversation while its answer is being generated ------------------------------------------------


async def pending_answers(chat: Chat) -> list[Any]:
    return await fetch(chat.engine, "select conversation_id from messages where status = 'PENDING'")


async def generating(chat: Chat, api: httpx.AsyncClient, how: str = "ask") -> tuple[asyncio.Task[httpx.Response], str]:
    """Start an answer that the slow provider does not finish: returns the task of the request and the conversation id,
    once the provider has the request and the answer is PENDING."""
    if how == "ask":
        task = asyncio.create_task(ask_new(api, chat.kb_a, QUESTION))
    else:
        (conversation,) = [r["id"] for r in await fetch(chat.engine, "select id from conversations")]
        (_, failed) = await messages_of(chat.engine, conversation)
        task = asyncio.create_task(api.post(f"{conversation_url(conversation)}/messages/{failed['id']}/retry"))

    async def started() -> list[Any]:
        return await pending_answers(chat) if chat.provider.request_received.is_set() else []

    pending = await eventually(started)
    assert pending, "the answer is not being generated"
    return task, str(pending[0]["conversation_id"])


async def assert_stopped_and_gone(chat: Chat, task: asyncio.Task[httpx.Response], conversation: str) -> None:
    provider_saw_close = await asyncio.to_thread(chat.provider.client_closed.wait, SECONDS)
    response = await asyncio.wait_for(task, SECONDS)  # the stream of the question ends: nobody is left to read it
    assert provider_saw_close, "the connection to the provider stayed open: the generation was not stopped"
    seen = names_of(events_of(response))
    assert seen[0] == "conversation"
    assert set(seen) <= {"conversation", "delta"}  # neither `done` nor `error`
    assert await table_counts(chat.engine) == (0, 0, 0)
    assert await messages_of(chat.engine, conversation) == []


async def test_AC_037_the_author_deletes_a_conversation_with_an_answer_in_progress(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.script = SLOW

    async with chat.api("mario") as api:
        task, conversation = await generating(chat, api)
        deleted = await api.delete(conversation_url(conversation))
        await assert_stopped_and_gone(chat, task, conversation)
        after = await api.get(conversation_url(conversation))
        asked_again = await ask(api, conversation, "quokka bis")
        listed = await api.get(f"/api/kbs/{chat.kb_a}/conversations")

    assert deleted.status_code == 204
    assert after.status_code == 404 and after.json() == NOT_FOUND
    assert asked_again.status_code == 404 and asked_again.json() == NOT_FOUND
    assert listed.json() == []
    assert len(chat.provider.completions()) == 1  # the question after the deletion never reached the provider
    await asyncio.sleep(0.3)  # time for anything that would write late
    assert await table_counts(chat.engine) == (0, 0, 0)  # nothing came back: no stray row, no conversation


async def test_AC_037_the_admin_deletes_the_conversation_of_a_user_with_an_answer_in_progress(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.script = SLOW

    async with sharing_an_app(chat, "mario", "anna") as clients:
        task, conversation = await generating(chat, clients["mario"])
        deleted = await clients["anna"].delete(conversation_url(conversation))
        await assert_stopped_and_gone(chat, task, conversation)

    assert deleted.status_code == 204


async def test_AC_037_deleting_the_conversation_stops_a_retry_in_progress_too(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", QUESTION), ("assistant", "", "FAILED", "generation_error")
    )
    chat.provider.script = SLOW

    async with chat.api("mario") as api:
        task, conversation = await generating(chat, api, how="retry")
        deleted = await api.delete(conversation_url(conversation))
        await assert_stopped_and_gone(chat, task, conversation)

    assert deleted.status_code == 204


async def test_AC_037_the_delete_of_someone_else_does_not_stop_the_answer(chat: Chat) -> None:
    """The generation is stopped only after the authorization: a user who is refused (404) leaves it running."""
    await seed(chat, [chunk_row(0)])
    chat.provider.delay_seconds = 30.0  # the provider waits for `release`, then answers

    async with sharing_an_app(chat, "mario", "luigi", "peach") as clients:
        task, conversation = await generating(chat, clients["mario"])
        refused = [
            await clients["luigi"].delete(conversation_url(conversation)),
            await clients["peach"].delete(conversation_url(conversation)),
        ]
        await asyncio.sleep(0.3)
        assert not task.done(), "the delete of another user stopped the generation"
        chat.provider.release.set()
        events = events_of(await asyncio.wait_for(task, SECONDS))

    for response in refused:
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert names_of(events)[-1] == "done"
    assert await table_counts(chat.engine) == (1, 2, 1)


async def test_AC_037_a_pending_answer_that_nobody_is_generating_is_deleted_with_its_conversation(chat: Chat) -> None:
    conversation = await add_conversation(
        chat.engine, chat.kb_a, chat.users["mario"], ("user", "q"), ("assistant", "", "PENDING")
    )

    async with chat.api("mario") as api:
        deleted = await api.delete(conversation_url(conversation))

    assert deleted.status_code == 204
    assert await table_counts(chat.engine) == (0, 0, 0)


async def test_AC_037_with_a_real_connection_the_open_stream_ends_cleanly_when_the_conversation_is_deleted(
    chat: Chat,
) -> None:
    await seed(chat, [chunk_row(0)])
    chat.provider.script = SLOW
    seen: list[tuple[str, Any]] = []
    deleted: list[int] = []

    with running(chat.settings_with()) as base_url:
        author = await signed_in(base_url, "mario")
        other = await signed_in(base_url, "mario")
        try:
            async with author.stream(
                "POST", f"/api/kbs/{chat.kb_a}/conversations", json={"question": QUESTION}
            ) as response:
                async for name, data in events(response):
                    seen.append((name, data))
                    if name == "delta":
                        reply = await other.delete(conversation_url(seen[0][1]["conversation_id"]))
                        deleted.append(reply.status_code)
            # leaving the block without an error means the body ended properly (not a broken connection)
            provider_saw_close = await asyncio.to_thread(chat.provider.client_closed.wait, SECONDS)
            gone = await other.get(conversation_url(seen[0][1]["conversation_id"]))
        finally:
            await author.aclose()
            await other.aclose()

    assert deleted == [204]
    assert [name for name, _ in seen if name != "comment"][0] == "conversation"
    assert {name for name, _ in seen} <= {"conversation", "delta", "comment"}
    assert provider_saw_close
    assert gone.status_code == 404
    assert await table_counts(chat.engine) == (0, 0, 0)


# ActiveAnswers (plan: Interfaces) -------------------------------------------------------------------------------------


async def test_active_answers_cancels_the_task_of_the_conversation_and_waits_until_it_has_cleaned_up() -> None:
    active = service.ActiveAnswers()
    conversation, other = uuid.uuid4(), uuid.uuid4()
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.05)  # closing the connection to the provider takes a moment
            cleaned.set()

    task = asyncio.create_task(work())
    active.register(conversation, task)
    await started.wait()

    assert await active.cancel(other) is False  # another conversation: nothing happens
    assert not task.done()
    assert await active.cancel(conversation) is True
    assert task.cancelled()
    assert cleaned.is_set(), "cancel returned before the task had cleaned up"
    assert await active.cancel(conversation) is False  # one cancellation only


async def test_active_answers_forgets_a_task_that_ends_by_itself() -> None:
    active = service.ActiveAnswers()
    conversation = uuid.uuid4()
    task = asyncio.create_task(asyncio.sleep(0))
    active.register(conversation, task)
    await task
    await asyncio.sleep(0)

    assert await active.cancel(conversation) is False


async def test_the_app_has_one_registry_of_the_active_answers_in_its_state() -> None:
    app = create_app(make_settings())
    try:
        registry = service.active_answers(app)
        assert isinstance(registry, service.ActiveAnswers)
        assert app.state.active_answers is registry
        assert service.active_answers(app) is registry
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()


async def test_an_answer_that_ends_leaves_nothing_registered(chat: Chat) -> None:
    await seed(chat, [chunk_row(0)])
    app = create_app(chat.settings_with())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            assert (await login(api, "mario", PASSWORD)).status_code == 200
            events = events_of(await ask_new(api, chat.kb_a, QUESTION))
            conversation = uuid.UUID(events[0][1]["conversation_id"])
            registry = service.active_answers(app)
            assert names_of(events)[-1] == "done"
            assert await registry.cancel(conversation) is False  # the task of the finished answer is not kept
    finally:
        await app.state.engine.dispose()
        await app.state.redis.aclose()


# The order of the locks: a delete and an answer saved at the same moment never deadlock ------------------------------
# The answer saving (`complete_answer`), the retry (`restart_answer`) and the question (`start_answer`) write on the
# conversation; the delete of the conversation, and of its KB, removes it with its messages by cascade. They must take
# the rows in the same order: a test holds the conversation row, lets the delete queue for it first and the other
# operation second, then releases the row. With opposite orders PostgreSQL aborts one of the two with a deadlock error.


async def blocked_on_a_lock(engine: AsyncEngine) -> int:
    (row,) = await fetch(
        engine,
        "select count(*) as n from pg_stat_activity where datname = current_database() and wait_event_type = 'Lock'",
    )
    return int(row["n"])


async def wait_for_blocked(engine: AsyncEngine, count: int) -> None:
    async def reached() -> bool:
        return await blocked_on_a_lock(engine) >= count

    assert await eventually(reached), f"fewer than {count} statements are waiting for a lock"


@pytest.mark.parametrize("deleter", ["conversation", "kb"])
@pytest.mark.parametrize("writer", ["complete", "restart", "start"])
async def test_AC_037_a_delete_and_a_save_at_the_same_moment_do_not_deadlock(
    chat: Chat, deleter: str, writer: str
) -> None:
    turns: dict[str, tuple[Any, ...]] = {
        "complete": (("user", "q"), ("assistant", "", "PENDING")),
        "restart": (("user", "q"), ("assistant", "", "FAILED", "generation_error")),
        "start": (("user", "q"), ("assistant", "ok")),
    }
    conversation_id = await add_conversation(chat.engine, chat.kb_a, chat.users["mario"], *turns[writer])
    (_, answer) = await messages_of(chat.engine, conversation_id)
    sessions = chat.sessions()

    async with sessions() as loading:
        mario = await chat.user(loading, "mario")
        conversation = await ConversationRepository(loading).get_for(mario, conversation_id)
        assert conversation is not None

    async def delete() -> bool:
        async with sessions() as session:
            if deleter == "conversation":
                return await ConversationRepository(session).delete(conversation)
            return await KnowledgeBaseRepository(session).delete(chat.kb_a)

    async def save() -> Any:
        async with sessions() as session:
            repository = ConversationRepository(session)
            if writer == "complete":
                return await repository.complete_answer(answer["id"], "ok", None, [])
            if writer == "restart":
                return await repository.restart_answer(conversation, answer["id"])
            return await repository.start_answer(conversation, "nuova domanda")

    holder = await chat.engine.connect()
    try:
        await holder.execute(text("select 1 from conversations where id = :id for update"), {"id": conversation_id})
        deleting = asyncio.create_task(delete())
        await wait_for_blocked(chat.engine, 1)  # the delete is first in the queue for the row
        saving = asyncio.create_task(save())
        await wait_for_blocked(chat.engine, 2)
    finally:
        await holder.rollback()
        await holder.close()
    outcomes = await asyncio.wait_for(asyncio.gather(deleting, saving, return_exceptions=True), 30)

    assert not any(isinstance(outcome, DBAPIError) for outcome in outcomes), outcomes  # no deadlock, nothing aborted
    assert outcomes[0] is True
    if writer == "complete":
        assert outcomes[1] is None or outcomes[1].status == "COMPLETED"
    elif writer == "restart":
        assert isinstance(outcomes[1], MessageNotFound) or outcomes[1].status == "PENDING"
    else:
        assert isinstance(outcomes[1], ConversationGone) or len(outcomes[1]) == 2
    assert await table_counts(chat.engine) == (0, 0, 0)  # the delete won in every case: nothing is left
