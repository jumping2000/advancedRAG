# SPEC-2026-11-17-fase-3-chat
"""Isolation of the conversations: AC-024, AC-025 (REQ-012) and the rule of AGENTS.md (every access to a conversation or
to a KB goes through `ConversationRepository`/`KnowledgeBaseRepository`, or a query joined with `kb_members` in the same
statement, with a test that calls it directly), plus the anti-goal of the spec: nothing of another user's conversation,
of a KB that is not assigned to the caller, of its chunks, titles, user names and KB names, reaches a response.

Two layers:
- the repository, called directly with the wrong user: `get_for`, `list_for`, `get_for_admin` and the read of the
  admin list. `rename`, `delete` and `excerpt` take the conversation that `get_for`/`get_for_admin` returned (plan:
  Interfaces), so for a user who is not allowed none can be built: the matrix below proves `get_for` gives nothing for
  every user and conversation that are not author and member, and that the three act only on what they are given
  (the scope of `excerpt` to the KB of the conversation is in test_chat_sources.py);
- the API, every route and every kind of caller: another user of the same KB, a user of no KB, a user taken out of the
  KB, no session, ids that are not UUIDs, a message of another conversation, a source of another message: always the
  same 404 `conversation_not_found` (404 `kb_not_found` for a KB), a user never gets 403, nothing changes, and no
  response holds a title, a user name, a KB name or a chunk text that is not the caller's.
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
from tests.test_chat_ask import Chat, add_conversation, chat_world, fetch, messages_of, table_counts
from tests.test_chat_ask import engine as engine  # noqa: F401  (fresh database state per test)
from tests.test_chat_ask import schema as schema  # noqa: F401
from tests.test_chat_sources import TEXTS, Answered, answered, source_url
from tests.test_conversations import NOT_FOUND, at, conversation_url, ids_of, snapshot, touch
from tests.test_knowledge_bases import assign
from tests.test_retrieval import Statements

INVALID_IDS = ["not-a-uuid", "123", "8a1c7f9e-0000-4000-8000-00000000000z", "%27%20or%201%3D1%20--"]
LUIGI_TITLE = "Titolo-riservato-Zanzibar5521"
KB_B_TEXTS = ["Il Segretissimo8801 della KB Procedure", "Il Riservatissimo3317 della KB Procedure"]
KB_B_FILE = "procedure-riservate-Ghepardo4404.pdf"
# What mario must never read: the title and the chunks of luigi's conversation in the other KB, the user name of luigi,
# the name of the KB he is not in, the name of its document and the ids of the conversations of others
LUIGIS = ["Zanzibar5521", "Segretissimo8801", "Riservatissimo3317", "Ghepardo4404", "luigi", "Procedure"]


@pytest.fixture
async def chat(engine: AsyncEngine) -> Any:  # noqa: F811
    async with chat_world(engine) as world:
        yield world


@dataclass
class Hidden:
    """mario: an answered conversation in kb_a. luigi: one in kb_a (same chunks) and one in kb_b."""

    mario: Answered
    luigi_a: Answered
    luigi_b: Answered


async def hidden_world(chat: Chat) -> Hidden:
    mario = await answered(chat, "mario")
    luigi_a = await answered(chat, "luigi", chunks=mario.chunks, fragments=("Solo [2].",))
    luigi_b = await answered(chat, "luigi", kb=chat.kb_b, texts=KB_B_TEXTS, filename=KB_B_FILE)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("update conversations set title = :title where id = any(cast(:ids as uuid[]))"),
            {"title": LUIGI_TITLE, "ids": [uuid.UUID(luigi_a.conversation), uuid.UUID(luigi_b.conversation)]},
        )
    return Hidden(mario, luigi_a, luigi_b)


def calls(conversation: object, message: object, number: object = 1) -> list[tuple[str, str, str, Any]]:
    """Every route that takes a conversation: (name, method, url, json body)."""
    base = f"/api/conversations/{conversation}"
    return [
        ("detail", "GET", base, None),
        ("rename", "PATCH", base, {"title": "Cambiato"}),
        ("delete", "DELETE", base, None),
        ("ask", "POST", f"{base}/messages", {"question": "quokka zibaldone"}),
        ("retry", "POST", f"{base}/messages/{message}/retry", None),
        ("source", "GET", f"{base}/messages/{message}/sources/{number}", None),
    ]


CALL_NAMES = [name for name, *_ in calls("c", "m")]


async def send(api: httpx.AsyncClient, method: str, url: str, body: Any) -> httpx.Response:
    return await api.request(method, url, json=body)


def assert_nothing_of_luigi(response: httpx.Response) -> None:
    for word in LUIGIS:
        assert word not in response.text, (word, response.text)


# The repository, called directly -----------------------------------------------------------------------------------


async def test_AC_025_get_for_gives_a_conversation_only_to_its_author_who_can_see_the_kb(chat: Chat) -> None:
    """Every user against every conversation: the only pairs that give a result are (author, own conversation) of a KB
    that the author can see. The admin is not the author of the conversations of the others either."""
    world = await hidden_world(chat)
    anna = await add_conversation(chat.engine, chat.kb_a, chat.users["anna"], ("user", "x"))
    conversations = {
        "mario_a": (uuid.UUID(world.mario.conversation), "mario"),
        "luigi_a": (uuid.UUID(world.luigi_a.conversation), "luigi"),
        "luigi_b": (uuid.UUID(world.luigi_b.conversation), "luigi"),
        "anna_a": (anna, "anna"),
    }
    # a conversation of mario in a KB where he is not (any more) assigned
    marios_b = await add_conversation(chat.engine, chat.kb_b, chat.users["mario"], ("user", "x"))
    conversations["mario_b"] = (marios_b, "mario")
    found: dict[tuple[str, str], bool] = {}

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        for username in chat.users:
            user = await chat.user(session, username)
            for name, (conversation_id, _) in conversations.items():
                result = await repository.get_for(user, conversation_id)
                found[(username, name)] = result is not None
                if result is not None:
                    assert result.id == conversation_id

    expected = {(username, name): False for username in chat.users for name in conversations}
    for name in ("mario_a", "luigi_a", "luigi_b", "anna_a"):
        author = conversations[name][1]
        expected[(author, name)] = True
    expected[("mario", "mario_b")] = False  # his own conversation, in a KB he cannot see
    assert found == expected


async def test_AC_025_get_for_admin_is_the_only_read_that_ignores_the_author(chat: Chat) -> None:
    world = await hidden_world(chat)
    conversation = uuid.UUID(world.luigi_b.conversation)

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        mario = await chat.user(session, "mario")
        anna = await chat.user(session, "anna")
        assert await repository.get_for(anna, conversation) is None
        assert await repository.get_for(mario, conversation) is None
        any_author = await repository.get_for_admin(conversation)
        unknown = await repository.get_for_admin(uuid.uuid4())

    assert any_author is not None and any_author.id == conversation
    assert unknown is None


async def test_AC_025_list_for_gives_only_the_own_conversations_of_a_kb_the_user_can_see(chat: Chat) -> None:
    world = await hidden_world(chat)
    marios_b = await add_conversation(
        chat.engine, chat.kb_b, chat.users["mario"], ("user", "x")
    )  # not a member of kb_b
    anna_a = await add_conversation(chat.engine, chat.kb_a, chat.users["anna"], ("user", "x"))

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        users = {name: await chat.user(session, name) for name in chat.users}
        lists = {
            ("mario", "a"): await repository.list_for(users["mario"], chat.kb_a),
            ("mario", "b"): await repository.list_for(users["mario"], chat.kb_b),
            ("luigi", "a"): await repository.list_for(users["luigi"], chat.kb_a),
            ("luigi", "b"): await repository.list_for(users["luigi"], chat.kb_b),
            ("peach", "a"): await repository.list_for(users["peach"], chat.kb_a),
            ("peach", "b"): await repository.list_for(users["peach"], chat.kb_b),
            ("anna", "a"): await repository.list_for(users["anna"], chat.kb_a),
            ("anna", "b"): await repository.list_for(users["anna"], chat.kb_b),
            ("mario", "unknown"): await repository.list_for(users["mario"], uuid.uuid4()),
        }

    ids = {key: [c.id for c in value] for key, value in lists.items()}
    assert ids[("mario", "a")] == [uuid.UUID(world.mario.conversation)]
    assert ids[("mario", "b")] == []  # he has a conversation there (marios_b), but the KB is not his
    assert ids[("luigi", "a")] == [uuid.UUID(world.luigi_a.conversation)]
    assert ids[("luigi", "b")] == [uuid.UUID(world.luigi_b.conversation)]
    assert ids[("peach", "a")] == ids[("peach", "b")] == []
    assert ids[("anna", "a")] == [anna_a]  # the admin sees every KB but lists her own conversations only
    assert ids[("anna", "b")] == []
    assert ids[("mario", "unknown")] == []
    assert marios_b not in ids[("mario", "b")]


async def test_AC_025_list_for_and_get_for_are_one_statement_that_carries_the_access_check(chat: Chat) -> None:
    world = await hidden_world(chat)

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        mario, anna = await chat.user(session, "mario"), await chat.user(session, "anna")
        with Statements(chat.engine) as statements:
            await repository.list_for(mario, chat.kb_a)
            await repository.list_for(anna, chat.kb_a)
            await repository.get_for(mario, uuid.UUID(world.mario.conversation))

    reading = [sql for sql in statements.sent if "conversations" in sql.lower()]
    assert len(reading) == 3, reading  # one statement each: no read of the KBs first and of the conversations after
    assert "kb_members" in reading[0] and "knowledge_bases" in reading[1] and "kb_members" in reading[2]


async def test_rename_and_delete_act_only_on_the_conversation_they_are_given(chat: Chat) -> None:
    world = await hidden_world(chat)
    mario_id, luigi_id = uuid.UUID(world.mario.conversation), uuid.UUID(world.luigi_a.conversation)
    luigis_before = (await fetch(chat.engine, "select * from conversations where id = :id", id=luigi_id))[0]

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        mario = await chat.user(session, "mario")
        assert await repository.get_for(mario, luigi_id) is None  # there is no way to get luigi's conversation
        conversation = await repository.get_for(mario, mario_id)
        assert conversation is not None
        renamed = await repository.rename(conversation, "Nuovo titolo")
        assert renamed is not None and renamed.title == "Nuovo titolo"
        deleted = await repository.delete(conversation)
        deleted_again = await repository.delete(conversation)
        renamed_after = await repository.rename(conversation, "Ancora")

    assert deleted is True
    assert deleted_again is False  # already gone
    assert renamed_after is None
    assert await messages_of(chat.engine, mario_id) == []
    assert (await fetch(chat.engine, "select * from conversations where id = :id", id=luigi_id))[0] == luigis_before
    assert len(await messages_of(chat.engine, luigi_id)) == 2  # the messages and sources of luigi's are all there
    assert (await table_counts(chat.engine))[0] == 2


async def test_AC_027_list_all_gives_every_conversation_with_the_filters_applied_in_the_statement(chat: Chat) -> None:
    world = await hidden_world(chat)
    for conversation, minute in ((world.mario, 1), (world.luigi_a, 3), (world.luigi_b, 5)):
        await touch(chat.engine, conversation.conversation, at(minute))

    async with chat.sessions()() as session:
        repository = ConversationRepository(session)
        everything = await repository.list_all(None, None)
        of_kb_a = await repository.list_all(chat.kb_a, None)
        of_luigi = await repository.list_all(None, chat.users["luigi"])
        of_luigi_in_a = await repository.list_all(chat.kb_a, chat.users["luigi"])
        nothing = await repository.list_all(chat.kb_b, chat.users["mario"])
        with Statements(chat.engine) as statements:
            await repository.list_all(chat.kb_a, chat.users["luigi"])

    assert [row.id for row in everything] == [
        uuid.UUID(world.luigi_b.conversation),
        uuid.UUID(world.luigi_a.conversation),
        uuid.UUID(world.mario.conversation),
    ]
    assert [(row.username, row.kb_name, row.message_count) for row in everything] == [
        ("luigi", "Procedure", 2),
        ("luigi", "Manuali", 2),
        ("mario", "Manuali", 2),
    ]
    assert [row.id for row in of_kb_a] == [uuid.UUID(world.luigi_a.conversation), uuid.UUID(world.mario.conversation)]
    assert [row.id for row in of_luigi] == [
        uuid.UUID(world.luigi_b.conversation),
        uuid.UUID(world.luigi_a.conversation),
    ]
    assert [row.id for row in of_luigi_in_a] == [uuid.UUID(world.luigi_a.conversation)]
    assert nothing == []
    assert len(statements.sent) == 1  # one statement for the whole list: no query per conversation


# AC-024: two users of the same KB -------------------------------------------------------------------------------------


async def test_AC_024_each_of_two_users_of_the_same_kb_lists_only_their_own_conversations(chat: Chat) -> None:
    world = await hidden_world(chat)

    async with chat.api("mario") as mario, chat.api("luigi") as luigi:
        marios = await mario.get(f"/api/kbs/{chat.kb_a}/conversations")
        luigis = await luigi.get(f"/api/kbs/{chat.kb_a}/conversations")
        luigis_b = await luigi.get(f"/api/kbs/{chat.kb_b}/conversations")

    assert ids_of(marios) == [world.mario.conversation]
    assert ids_of(luigis) == [world.luigi_a.conversation]
    assert ids_of(luigis_b) == [world.luigi_b.conversation]
    assert_nothing_of_luigi(marios)
    assert LUIGI_TITLE in luigis.text  # his own title, of course


@pytest.mark.parametrize("name", CALL_NAMES)
@pytest.mark.parametrize("username", ["luigi", "peach"])
async def test_AC_024_the_conversation_of_another_user_is_404_on_every_route_and_nothing_changes(
    chat: Chat, username: str, name: str
) -> None:
    """luigi is in the same KB as mario, peach in none: for both, mario's conversation does not exist."""
    world = await hidden_world(chat)
    target = world.mario
    (call,) = [c for c in calls(target.conversation, target.answer) if c[0] == name]
    before = await snapshot(chat.engine)
    requests_before = (len(chat.provider.requests), len(chat.ollama.requests))

    async with chat.api(username) as api:
        response = await send(api, *call[1:])

    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND  # also for an admin-less caller: never 403
    assert await snapshot(chat.engine) == before
    assert (len(chat.provider.requests), len(chat.ollama.requests)) == requests_before
    for marker in TEXTS:
        assert marker.split()[1] not in response.text
    assert "manuale.pdf" not in response.text


@pytest.mark.parametrize("username", ["luigi", "peach"])
async def test_AC_024_the_check_of_the_author_comes_before_the_validation_of_the_title(
    chat: Chat, username: str
) -> None:
    """A rename with an invalid title of the conversation of someone else is a 404, not a 422 that would tell the
    caller that the conversation is there."""
    world = await hidden_world(chat)
    before = await snapshot(chat.engine)

    async with chat.api(username) as api:
        responses = [
            await api.patch(conversation_url(world.mario.conversation), json={"title": "   "}),
            await api.patch(conversation_url(world.mario.conversation), json={"title": "x" * 201}),
            await api.patch(conversation_url(world.mario.conversation), json={"title": None}),
        ]

    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json() == NOT_FOUND
    assert await snapshot(chat.engine) == before


@pytest.mark.parametrize("name", CALL_NAMES)
async def test_AC_024_a_conversation_that_does_not_exist_gets_the_same_404_as_one_of_another_user(
    chat: Chat, name: str
) -> None:
    world = await hidden_world(chat)
    (foreign,) = [c for c in calls(world.luigi_b.conversation, world.luigi_b.answer) if c[0] == name]
    (unknown,) = [c for c in calls(uuid.uuid4(), uuid.uuid4()) if c[0] == name]

    async with chat.api("mario") as api:
        of_another = await send(api, *foreign[1:])
        of_nobody = await send(api, *unknown[1:])

    assert of_another.status_code == of_nobody.status_code == 404
    assert of_another.json() == of_nobody.json() == NOT_FOUND
    assert of_another.headers["content-type"] == of_nobody.headers["content-type"]


@pytest.mark.parametrize("name", CALL_NAMES)
@pytest.mark.parametrize("invalid", INVALID_IDS)
async def test_AC_024_an_id_that_is_not_a_uuid_is_404_conversation_not_found_on_every_route(
    chat: Chat, name: str, invalid: str
) -> None:
    world = await hidden_world(chat)
    own = world.mario
    before = await snapshot(chat.engine)
    wrong_conversation = [c for c in calls(invalid, own.answer) if c[0] == name]
    # the message id is in the URL of the retry and of the source only (the other routes would be valid requests)
    wrong_message = [c for c in calls(own.conversation, invalid) if c[0] == name and name in ("retry", "source")]

    async with chat.api("mario") as api:
        responses = [await send(api, *call[1:]) for call in wrong_conversation + wrong_message]

    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json() == NOT_FOUND
    assert await snapshot(chat.engine) == before


@pytest.mark.parametrize("kb", INVALID_IDS)
async def test_AC_024_a_kb_id_that_is_not_a_uuid_is_404_kb_not_found(chat: Chat, kb: str) -> None:
    async with chat.api("mario") as api:
        response = await api.get(f"/api/kbs/{kb}/conversations")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "kb_not_found"


async def test_AC_024_a_message_or_a_source_of_another_conversation_is_404(chat: Chat) -> None:
    world = await hidden_world(chat)
    mario, luigi = world.mario, world.luigi_a  # the same chunks, two conversations of two users in the same KB

    async with chat.api("mario") as api:
        cross = [
            await api.get(source_url(mario, 1, luigi.answer)),  # the answer of luigi in the conversation of mario
            await api.get(source_url(mario, 2, luigi.answer)),  # the source that luigi cited, with luigi's message
            await api.post(f"/api/conversations/{mario.conversation}/messages/{luigi.answer}/retry"),
            await api.get(source_url(luigi, 2)),  # luigi's conversation, luigi's source
        ]
        own = await api.get(source_url(mario, 1))

    for response in cross:
        assert response.status_code == 404, response.text
        assert response.json() == NOT_FOUND
    assert own.status_code == 200


async def test_AC_024_nothing_of_luigi_reaches_a_response_for_mario(chat: Chat) -> None:
    """Every read that mario can make, and every attempt on what is not his: not a title, a user name, a KB name, a
    chunk or a document name of luigi in any body."""
    world = await hidden_world(chat)
    luigi_ids = [world.luigi_a.conversation, world.luigi_b.conversation]
    bodies: list[httpx.Response] = []

    async with chat.api("mario") as api:
        bodies.append(await api.get(f"/api/kbs/{chat.kb_a}/conversations"))
        bodies.append(await api.get(f"/api/kbs/{chat.kb_b}/conversations"))
        bodies.append(await api.get(f"/api/conversations/{world.mario.conversation}"))
        bodies.append(await api.get(source_url(world.mario, 1)))
        bodies.append(await api.get(source_url(world.mario, 3)))
        bodies.append(await api.get("/api/admin/conversations"))
        for conversation in luigi_ids:
            for _, method, url, body in calls(conversation, world.luigi_b.answer):
                bodies.append(await send(api, method, url, body))
        bodies.append(await api.get(source_url(world.luigi_b, 1)))
        bodies.append(
            await api.get(f"/api/conversations/{world.mario.conversation}/messages/{world.luigi_b.answer}/sources/1")
        )

    # what mario may read he reads (the test would be empty otherwise), what is not his is refused
    assert [response.status_code for response in bodies[:6]] == [200, 404, 200, 200, 200, 403]
    for response in bodies:
        assert_nothing_of_luigi(response)
        for conversation in luigi_ids:
            assert conversation not in response.text


# A user taken out of the KB ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CALL_NAMES)
async def test_AC_026_the_own_conversation_of_a_user_taken_out_of_the_kb_is_404_on_every_route(
    chat: Chat, name: str
) -> None:
    world = await hidden_world(chat)
    async with chat.engine.begin() as connection:
        await connection.execute(
            text("delete from kb_members where kb_id = :kb and user_id = :user"),
            {"kb": chat.kb_a, "user": chat.users["mario"]},
        )
    (call,) = [c for c in calls(world.mario.conversation, world.mario.answer) if c[0] == name]
    before = await snapshot(chat.engine)

    async with chat.api("mario") as api:
        response = await send(api, *call[1:])
        listed = await api.get(f"/api/kbs/{chat.kb_a}/conversations")

    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "kb_not_found"
    assert await snapshot(chat.engine) == before
    await assign(chat.engine, chat.kb_a, chat.users["mario"])
    async with chat.api("mario") as api:
        assert (await api.get(conversation_url(world.mario.conversation))).status_code == 200


# No session ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CALL_NAMES)
async def test_a_request_without_a_session_is_401_on_every_route_and_nothing_changes(chat: Chat, name: str) -> None:
    world = await hidden_world(chat)
    (call,) = [c for c in calls(world.mario.conversation, world.mario.answer) if c[0] == name]
    before = await snapshot(chat.engine)

    async with chat.api(None) as api:
        response = await send(api, *call[1:])
        listed = await api.get(f"/api/kbs/{chat.kb_a}/conversations")
        admin_list = await api.get("/api/admin/conversations")

    for answer in (response, listed, admin_list):
        assert answer.status_code == 401, answer.text
        assert answer.json()["error"]["code"] == "not_authenticated"
    assert await snapshot(chat.engine) == before


async def test_a_user_who_is_not_an_admin_never_gets_403_on_the_conversations_of_others(chat: Chat) -> None:
    """403 is the answer to an admin who writes on a conversation of another user (AC-028); a user is told 404, so that
    the existence of a conversation of someone else is not revealed."""
    world = await hidden_world(chat)
    codes = set()

    async with chat.api("luigi") as api:
        for _, method, url, body in calls(world.mario.conversation, world.mario.answer):
            codes.add((await send(api, method, url, body)).status_code)

    assert codes == {404}
