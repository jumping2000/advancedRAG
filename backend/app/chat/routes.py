"""The routes of the chat (SPEC-2026-11-17-fase-3-chat, T-03, T-04 and T-05): `POST /api/kbs/{kb_id}/conversations`
starts a conversation with its first question, `POST /api/conversations/{conversation_id}/messages` asks in an existing
one, `POST /api/conversations/{conversation_id}/messages/{message_id}/retry` regenerates the last answer when it failed.
All three answer with a stream of server-sent events (`app.chat.service`), except for what is refused before it, in JSON
with the error format of the project and with nothing saved.

Order of the checks: 401 (no session); 404 `kb_not_found` (a KB the user cannot see, or an id that is not a UUID) or
404 `conversation_not_found` (a conversation that is not the user's, of a KB the user cannot see, unknown, or an id that
is not a UUID); 403 `forbidden` (an admin on the conversation of another user: the admin reads it elsewhere, never
writes); 422 `validation_error` (a question that is empty after the spaces at both ends are removed, longer than
`CHAT_MAX_QUESTION_CHARS`, or that the database cannot hold); 503 `generation_not_configured` (no
`GENERATION_BASE_URL`: nothing is saved and nothing is sent); 409 `answer_in_progress` (the conversation has an answer
being generated, also when two questions arrive together). The question is saved trimmed, and trimmed is what is
embedded and sent to the model.

The retry has no question to check: after the conversation (404 / 403 as above) and the provider (503) it is 404
`conversation_not_found` for a message id that is not a UUID or not in the conversation (the same 404 as an unknown
conversation: no new error code), 409 `answer_in_progress` when the conversation has an answer being generated (any
retry, whichever message it names), 409 `not_retryable` when the message is not the last message of the conversation, or
is not an answer, or is not FAILED. A retry of a KB that has lost its documents is a question like any other: it ends
with the notice `no_documents`.

T-05 adds what the author does with the conversations and what the admin reads, all in JSON:
- `GET /api/kbs/{kb_id}/conversations`: the caller's own conversations of a KB they can see (404 `kb_not_found`);
- `GET /api/conversations/{conversation_id}`: `ConversationDetail`; the author, or the admin for any conversation (then
  `read_only` is true); a conversation of someone else is 404 for a `user`, never 403;
- `PATCH /api/conversations/{conversation_id}` `{"title"}`: the author only, as a question (404 for a user, 403 for the
  admin on another's), then 422 `validation_error` for a title that is empty after trimming, longer than 200 characters
  or not storable; the title is saved trimmed;
- `DELETE /api/conversations/{conversation_id}`: 204, the author or the admin; the generation in progress is stopped
  only after the check of who asks, then the conversation is deleted with its messages and sources;
- `GET /api/conversations/{conversation_id}/messages/{message_id}/sources/{number}`: `SourceExcerpt`, for whoever can
  read the conversation; a message id or a number that does not name a cited source of a message of that conversation
  (or is not a UUID, or not a number) is the same 404 `conversation_not_found`;
- `GET /api/admin/conversations?kb_id=&user_id=`: admin only (403 before the query is read); a filter that matches
  nothing or is not a UUID gives an empty list, an empty one is no filter.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.auth.deps import ApiError, CurrentUser, DbSession, require_admin
from app.chat import service
from app.chat.repository import (
    AnswerInProgress,
    ConversationGone,
    ConversationRepository,
    KbNotVisible,
    MessageNotFound,
    NotRetryable,
)
from app.chat.schemas import (
    AdminConversationOut,
    ConversationDetail,
    ConversationOut,
    QuestionIn,
    SourceExcerpt,
    TitleIn,
)
from app.config import Settings
from app.db.models import Conversation, Message, User
from app.knowledge_bases.repository import KnowledgeBaseRepository
from app.knowledge_bases.routes import kb_not_found, visible_kb_id

# The title is the first characters of the question, without the spaces the cut may leave at its end
TITLE_LENGTH = 80
MAX_TITLE_CHARS = 200
# The most digits of a source number that can name a source (the column is a 32-bit integer)
MAX_NUMBER_DIGITS = 9
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

logger = logging.getLogger(__name__)

router = APIRouter()


def conversation_not_found() -> ApiError:
    return ApiError(404, "conversation_not_found", "Conversation not found")


def answer_in_progress() -> ApiError:
    return ApiError(409, "answer_in_progress", "An answer is already being generated in this conversation")


def not_retryable() -> ApiError:
    return ApiError(409, "not_retryable", "Only the last answer of a conversation, when it failed, can be retried")


async def visible_kb(kb_id: str, user: CurrentUser, session: DbSession) -> uuid.UUID:
    """The id of the KB if the user can see it (404 kb_not_found otherwise, also for an id that is not a UUID)."""
    return await visible_kb_id(kb_id, user, KnowledgeBaseRepository(session))


def parse_conversation_id(conversation_id: str) -> uuid.UUID:
    """An id that is not a UUID is answered like an unknown conversation."""
    try:
        return uuid.UUID(conversation_id)
    except ValueError:
        raise conversation_not_found() from None


async def own_conversation(conversation_id: str, user: CurrentUser, session: DbSession) -> Conversation:
    """The conversation of the user, to write in. 404 for anyone else's (a user), 403 for an admin on another user's."""
    parsed = parse_conversation_id(conversation_id)
    repository = ConversationRepository(session)
    conversation = await repository.get_for(user, parsed)
    if conversation is not None:
        return conversation
    if user.role == "admin" and await repository.get_for_admin(parsed) is not None:
        raise ApiError(403, "forbidden", "Only the author can write in a conversation")
    raise conversation_not_found()


async def readable_conversation(conversation_id: str, user: CurrentUser, session: DbSession) -> Conversation:
    """The conversation to read or to delete: the user's own, or any for the admin. 404 for anyone else's (a user)."""
    parsed = parse_conversation_id(conversation_id)
    repository = ConversationRepository(session)
    conversation = await repository.get_for(user, parsed)
    if conversation is None and user.role == "admin":
        conversation = await repository.get_for_admin(parsed)
    if conversation is None:
        raise conversation_not_found()
    return conversation


KbId = Annotated[uuid.UUID, Depends(visible_kb)]
OwnConversation = Annotated[Conversation, Depends(own_conversation)]
ReadableConversation = Annotated[Conversation, Depends(readable_conversation)]


def storable(text: str) -> bool:
    """Whether the database can hold the text: no NUL character, no lone surrogate."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def checked_title(title: str) -> str:
    """The title without the spaces at both ends; 422 if it is empty, longer than 200 characters or not storable."""
    trimmed = title.strip()
    if not storable(trimmed) or not 1 <= len(trimmed) <= MAX_TITLE_CHARS:
        raise ApiError(422, "validation_error", "The request is not valid")
    return trimmed


def checked_question(question: str, settings: Settings) -> str:
    """The question without the spaces at both ends; 422 if it is empty, too long or not storable (a NUL character, a
    lone surrogate), 503 if no provider is configured: both before anything is saved."""
    trimmed = question.strip()
    if not storable(trimmed) or not 1 <= len(trimmed) <= settings.chat_max_question_chars:
        raise ApiError(422, "validation_error", "The request is not valid")
    if not settings.generation_base_url:
        raise ApiError(503, "generation_not_configured", "The generation provider is not configured")
    return trimmed


async def stream_answer(
    request: Request, session: DbSession, user: User, conversation: Conversation, question: str
) -> service.AnswerResponse:
    """Save the question and its PENDING answer, and answer with the stream of the events."""
    try:
        user_message, answer = await ConversationRepository(session).start_answer(conversation, question)
    except AnswerInProgress:
        raise answer_in_progress() from None
    except ConversationGone:
        raise conversation_not_found() from None
    return await respond(request, session, user, conversation, user_message, answer)


async def respond(
    request: Request, session: DbSession, user: User, conversation: Conversation, user_message: Message, answer: Message
) -> service.AnswerResponse:
    """The streaming response of `answer`, which is PENDING and waits for the answer to `user_message`."""
    response = service.AnswerResponse(
        request, user, conversation, user_message, answer, media_type="text/event-stream", headers=STREAM_HEADERS
    )
    try:
        await session.close()  # the request holds no connection while the answer is generated
    except BaseException:
        await response.abandon()  # the answer is PENDING and no response will ever settle it
        raise
    return response


@router.post("/kbs/{kb_id}/conversations")
async def start_conversation(
    request: Request, body: QuestionIn, kb_id: KbId, user: CurrentUser, session: DbSession
) -> service.AnswerResponse:
    question = checked_question(body.question, request.app.state.settings)
    try:
        conversation = await ConversationRepository(session).create(user, kb_id, question[:TITLE_LENGTH].rstrip())
    except KbNotVisible:
        raise kb_not_found() from None
    return await stream_answer(request, session, user, conversation, question)


@router.post("/conversations/{conversation_id}/messages")
async def ask_in_conversation(
    request: Request, body: QuestionIn, conversation: OwnConversation, user: CurrentUser, session: DbSession
) -> service.AnswerResponse:
    question = checked_question(body.question, request.app.state.settings)
    return await stream_answer(request, session, user, conversation, question)


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry")
async def retry_answer(
    request: Request, message_id: str, conversation: OwnConversation, user: CurrentUser, session: DbSession
) -> service.AnswerResponse:
    """«Riprova»: regenerate the FAILED answer `message_id`, which must be the last message of the conversation, for the
    same question; the answer is the same row put back to PENDING (`restart_answer`), so the conversation ends with one
    answer in place of the failed one. No body (a POST without Content-Type passes `require_json`)."""
    if not request.app.state.settings.generation_base_url:
        raise ApiError(503, "generation_not_configured", "The generation provider is not configured")
    try:
        parsed = uuid.UUID(message_id)
    except ValueError:
        raise conversation_not_found() from None  # an id that is not a UUID is an unknown id
    repository = ConversationRepository(session)
    try:
        answer = await repository.restart_answer(conversation, parsed)
    except AnswerInProgress:
        raise answer_in_progress() from None
    except NotRetryable:
        raise not_retryable() from None
    except MessageNotFound:
        raise conversation_not_found() from None
    try:
        question = await repository.question_of(answer)
        if question is None:
            if await repository.get_for(user, conversation.id) is None:
                raise conversation_not_found()  # deleted between the restart and now, as when a question is saved
            raise RuntimeError("the answer has no question")  # not made by the application
        return await respond(request, session, user, conversation, question, answer)
    except BaseException as error:
        # The answer is PENDING again and nothing else would ever settle it
        await service.settle_interrupted(request.app, conversation, answer)
        if isinstance(error, Exception) and not isinstance(error, ApiError):
            logger.error(
                "chat retry conversation=%s message=%s: unexpected error %s",
                conversation.id,
                answer.id,
                type(error).__name__,
            )  # the type, never the message
            raise ApiError(500, "internal_error", "Internal server error") from None
        raise


@router.get("/kbs/{kb_id}/conversations")
async def list_conversations(kb_id: KbId, user: CurrentUser, session: DbSession) -> list[ConversationOut]:
    conversations = await ConversationRepository(session).list_for(user, kb_id)
    return [ConversationOut.model_validate(conversation) for conversation in conversations]


@router.get("/conversations/{conversation_id}")
async def read_conversation(
    conversation: ReadableConversation, user: CurrentUser, session: DbSession
) -> ConversationDetail:
    messages = await ConversationRepository(session).read_messages(conversation)
    return ConversationDetail(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=messages,
        read_only=conversation.user_id != user.id,  # only the admin gets to read the conversation of someone else
    )


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(body: TitleIn, conversation: OwnConversation, session: DbSession) -> ConversationOut:
    title = checked_title(body.title)  # after the author check: a 403 or a 404 comes before a 422
    renamed = await ConversationRepository(session).rename(conversation, title)
    if renamed is None:
        raise conversation_not_found()
    logger.info("chat conversation renamed conversation=%s kb=%s", renamed.id, renamed.kb_id)  # never the title
    return ConversationOut.model_validate(renamed)


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation(request: Request, conversation: ReadableConversation, session: DbSession) -> None:
    """Delete the conversation with its messages and sources. An answer being generated is stopped first (the
    connection to the provider closes), and only after the check of who asks: `ReadableConversation` has refused anyone
    who may not delete it."""
    registry = service.active_answers(request.app)
    stopped = await registry.cancel(conversation.id)
    deleted = await ConversationRepository(session).delete(conversation)
    # Again: an answer whose stream started after the first cancel and checked that it was pending before the rows
    # were gone is stopped now (the next one to check finds nothing)
    stopped = await registry.cancel(conversation.id) or stopped
    if not deleted:
        raise conversation_not_found()  # deleted by someone else in the meantime
    logger.info(
        "chat conversation deleted conversation=%s kb=%s answer_stopped=%s",
        conversation.id,
        conversation.kb_id,
        stopped,
    )


@router.get("/conversations/{conversation_id}/messages/{message_id}/sources/{number}")
async def read_source(
    message_id: str, number: str, conversation: ReadableConversation, session: DbSession
) -> SourceExcerpt:
    """The excerpt of a cited source: the content of its chunk as it is now. What does not name a cited source of a
    message of this conversation is the same 404 as an unknown conversation (no other error code)."""
    parsed = parse_conversation_id(message_id)  # a message id that is not a UUID is an unknown one
    if not (number.isascii() and number.isdigit()) or len(number) > MAX_NUMBER_DIGITS:
        raise conversation_not_found()
    excerpt = await ConversationRepository(session).excerpt(conversation, parsed, int(number))
    if excerpt is None:
        raise conversation_not_found()
    logger.info(
        "chat source read conversation=%s message=%s number=%s available=%s",
        conversation.id,
        parsed,
        number,
        excerpt.available,
    )
    return excerpt


@router.get("/admin/conversations", dependencies=[Depends(require_admin)])
async def list_all_conversations(
    session: DbSession, kb_id: str | None = None, user_id: str | None = None
) -> list[AdminConversationOut]:
    """Every conversation of every user, by last activity, for the admin. The role is checked before the query is read.
    A filter that is not a UUID matches nothing, as an unknown one; an empty one is no filter."""
    try:
        kb = uuid.UUID(kb_id) if kb_id else None
        author = uuid.UUID(user_id) if user_id else None
    except ValueError:
        return []
    rows = await ConversationRepository(session).list_all(kb, author)
    logger.info("chat admin list conversations=%d", len(rows))
    return [AdminConversationOut.model_validate(row, from_attributes=True) for row in rows]
