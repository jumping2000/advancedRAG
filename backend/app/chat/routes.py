"""The routes that ask a question (SPEC-2026-11-17-fase-3-chat, T-03 and T-04): `POST /api/kbs/{kb_id}/conversations`
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
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth.deps import ApiError, CurrentUser, DbSession
from app.chat import service
from app.chat.repository import (
    AnswerInProgress,
    ConversationGone,
    ConversationRepository,
    KbNotVisible,
    MessageNotFound,
    NotRetryable,
)
from app.chat.schemas import QuestionIn
from app.config import Settings
from app.db.models import Conversation, Message, User
from app.knowledge_bases.repository import KnowledgeBaseRepository
from app.knowledge_bases.routes import kb_not_found, visible_kb_id

# The title is the first characters of the question, without the spaces the cut may leave at its end
TITLE_LENGTH = 80
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

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


async def own_conversation(conversation_id: str, user: CurrentUser, session: DbSession) -> Conversation:
    """The conversation of the user, to write in. 404 for anyone else's (a user), 403 for an admin on another user's."""
    try:
        parsed = uuid.UUID(conversation_id)
    except ValueError:
        raise conversation_not_found() from None
    repository = ConversationRepository(session)
    conversation = await repository.get_for(user, parsed)
    if conversation is not None:
        return conversation
    if user.role == "admin" and await repository.get_for_admin(parsed) is not None:
        raise ApiError(403, "forbidden", "Only the author can write in a conversation")
    raise conversation_not_found()


KbId = Annotated[uuid.UUID, Depends(visible_kb)]
OwnConversation = Annotated[Conversation, Depends(own_conversation)]


def checked_question(question: str, settings: Settings) -> str:
    """The question without the spaces at both ends; 422 if it is empty, too long or not storable (a NUL character, a
    lone surrogate), 503 if no provider is configured: both before anything is saved."""
    trimmed = question.strip()
    try:
        trimmed.encode("utf-8")
        storable = "\x00" not in trimmed
    except UnicodeEncodeError:
        storable = False
    if not storable or not 1 <= len(trimmed) <= settings.chat_max_question_chars:
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
        if question is None:  # an answer with no question before it: not made by the application
            raise RuntimeError("the answer has no question")
        return await respond(request, session, user, conversation, question, answer)
    except BaseException:
        await service.settle_interrupted(request.app, conversation, answer)  # it is PENDING and nobody will answer it
        raise
