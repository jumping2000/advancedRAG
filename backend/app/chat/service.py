"""The answer to a question as a stream of server-sent events (SPEC-2026-11-17-fase-3-chat, REQ-003 to REQ-009,
REQ-101, REQ-102, REQ-103).

`answer_events` is the body of the response of a question. Each event is `event: <name>` and `data: <json>` lines closed
by a blank line (the JSON is ASCII: a line break or U+2028 in a text never splits an event):
- `conversation` `{"conversation_id", "message_id"}`, at once; the message is the (PENDING) answer;
- `delta` `{"text"}`, one per fragment of the provider, as it arrives;
- `done` `{"message": MessageOut}`, last, after the answer is saved COMPLETED (with the text cleaned of the citations
  that are not sources, and the cited sources only); a KB with nothing to search gets `done` at once, with the notice
  `no_documents` and no call to Ollama or to the provider;
- `error` `{"error": {"code", "message"}}`, last, after the answer is saved FAILED, with `code` in
  `embedding_error`, `generation_error`, `generation_timeout`; any other exception is saved and sent as
  `generation_error`;
- a comment `: keepalive`, whenever `KEEPALIVE_SECONDS` pass without another event (under the 60 s that Nginx waits).

The steps of the answer run in a task of their own, so that the keepalive does not depend on them. The database is used
in short sessions, none open while Ollama or the provider is waited for: the session of the request is closed before
the stream starts, and the answer is saved in a new session. An answer is never left PENDING: it ends COMPLETED or
FAILED, and when the client leaves (the response is cancelled or the generator closed) it is saved FAILED
`interrupted`. Logs hold the ids of the conversation, the message and the KB, counts, durations and error codes: never a
question, an answer, a title, a chunk, a vector or the key; an unexpected exception is logged by its type only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, Literal, cast

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.generation import GenerationError, OpenAICompatibleClient
from app.chat.prompt import build_messages, clean_citations
from app.chat.repository import ConversationRepository
from app.chat.retrieval import Candidate, bm25_search, dense_search, fuse, has_searchable_chunks, load_candidates
from app.chat.schemas import MessageOut, SourceOut
from app.config import Settings
from app.db.models import Conversation, IndexingProfile, Message, User
from app.ingestion.embeddings import OllamaEmbedder
from app.ingestion.errors import IngestionError
from app.ingestion.profile import ensure_profile

logger = logging.getLogger(__name__)

# Seconds without an event after which a keepalive comment is sent
KEEPALIVE_SECONDS = 15.0
KEEPALIVE_EVENT = ": keepalive\n\n"

AnswerError = Literal["embedding_error", "generation_error", "generation_timeout", "interrupted"]
_MESSAGES: dict[str, str] = {
    "embedding_error": "The question could not be processed by the embedding service",
    "generation_error": "The generation provider failed or sent an invalid answer",
    "generation_timeout": "The generation provider did not finish in time",
}
# Tasks that must run to the end even if the request that started them is cancelled
_background: set[asyncio.Task[Any]] = set()


class _Failure(Exception):
    """A step of the answer failed in a way that is told to the client with this code."""

    def __init__(self, code: AnswerError) -> None:
        super().__init__(code)
        self.code: AnswerError = code


def _event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=True, separators=(',', ':'))}\n\n"


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


async def active_profile(app: FastAPI) -> IndexingProfile:
    """The indexing profile of the settings, the one the documents are searched in: read (or created) at the first
    question and kept in `app.state`."""
    profile: IndexingProfile | None = getattr(app.state, "indexing_profile", None)
    if profile is None:
        profile = await ensure_profile(app.state.engine, app.state.settings)
        app.state.indexing_profile = profile
    return profile


async def answer_events(
    request: Request, user: User, conversation: Conversation, user_message: Message, answer: Message
) -> AsyncIterator[str]:
    """The events of the answer to `user_message`, which `answer` (PENDING) is waiting for; see the module."""
    app: FastAPI = request.app
    started = time.monotonic()
    ids = f"conversation={conversation.id} message={answer.id} kb={conversation.kb_id}"
    yield _event("conversation", {"conversation_id": str(conversation.id), "message_id": str(answer.id)})
    logger.info("chat answer started %s", ids)
    code: AnswerError
    try:
        async with contextlib.aclosing(
            _with_keepalive(_answer(app, user, conversation, user_message, answer, ids))
        ) as events:
            async for event in events:
                yield event
        return
    except _Failure as failure:
        code = failure.code
    except Exception as error:
        logger.error("chat answer %s: unexpected error %s", ids, type(error).__name__)  # the type, never the message
        code = "generation_error"
    except BaseException:
        # The client left (the response was cancelled or the generator closed): the answer is saved FAILED, by a task
        # that goes on even if this one is cancelled again
        task = asyncio.ensure_future(_fail(app, answer.id, "interrupted", ids, started))
        _background.add(task)
        task.add_done_callback(_background.discard)
        await asyncio.shield(task)
        raise
    await _fail(app, answer.id, code, ids, started)
    yield _event("error", {"error": {"code": code, "message": _MESSAGES[code]}})


async def _with_keepalive(events: AsyncGenerator[str]) -> AsyncGenerator[str]:
    """The events of `events`, produced by a task of their own, plus a keepalive comment whenever nothing arrives for
    `KEEPALIVE_SECONDS`. An exception of `events` is raised here; when this generator is closed or cancelled the task is
    cancelled."""
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in events:
                queue.put_nowait(("event", event))
        except Exception as error:
            queue.put_nowait(("raise", error))
        else:
            queue.put_nowait(("end", None))

    producer = asyncio.ensure_future(produce())
    _background.add(producer)
    producer.add_done_callback(_background.discard)
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield KEEPALIVE_EVENT
                continue
            if kind == "event":
                yield value
            elif kind == "raise":
                raise value
            else:
                return
    finally:
        producer.cancel()


async def _answer(
    app: FastAPI, user: User, conversation: Conversation, user_message: Message, answer: Message, ids: str
) -> AsyncGenerator[str]:
    """The steps of the answer: retrieval, context, generation, saving. Raises `_Failure` or any other exception."""
    settings: Settings = app.state.settings
    sessions: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    kb_id = conversation.kb_id
    question = user_message.content
    started = time.monotonic()
    profile = await active_profile(app)

    async with sessions() as session:
        searchable = await has_searchable_chunks(session, user, kb_id, profile.id)
    if not searchable:
        done = await _finish(sessions, answer.id, "", "no_documents", [], ids, started)
        if done is not None:
            yield done
        return

    vector = await _embed(settings, profile, question)
    async with sessions() as session:
        dense_ids = await dense_search(session, user, kb_id, profile, vector, settings.retrieval_candidates)
        bm25_ids = await bm25_search(session, user, kb_id, profile.id, question, settings.retrieval_candidates)
        ranked = fuse(dense_ids, bm25_ids, settings.rrf_k)[: settings.chat_context_chunks]
        candidates = await load_candidates(session, user, kb_id, profile.id, ranked)
        history = await ConversationRepository(session).history(
            conversation, settings.chat_history_messages, before=user_message
        )
    logger.info(
        "chat retrieval %s dense=%d bm25=%d context=%d history=%d ms=%d",
        ids,
        len(dense_ids),
        len(bm25_ids),
        len(candidates),
        len(history),
        _elapsed_ms(started),
    )
    if not candidates:  # what was found is gone (document deleted meanwhile): nothing to cite, nothing to ask
        done = await _finish(sessions, answer.id, "", "no_documents", [], ids, started)
        if done is not None:
            yield done
        return

    key = settings.generation_api_key.get_secret_value() if settings.generation_api_key else None
    client = OpenAICompatibleClient(
        settings.generation_base_url, settings.generation_model, key, settings.generation_timeout_seconds
    )
    parts: list[str] = []
    try:
        stream = cast("AsyncGenerator[str]", client.stream(build_messages(question, candidates, history)))
        async with contextlib.aclosing(stream) as fragments:
            async for fragment in fragments:
                parts.append(fragment)
                yield _event("delta", {"text": fragment})
    except GenerationError as error:
        raise _Failure(error.code) from None

    text, cited_numbers = clean_citations("".join(parts), len(candidates))
    cited = set(cited_numbers)
    context = [(number, candidate, number in cited) for number, candidate in enumerate(candidates, start=1)]
    done = await _finish(sessions, answer.id, text, None, context, ids, started)
    if done is not None:
        yield done


async def _embed(settings: Settings, profile: IndexingProfile, question: str) -> list[float]:
    """The vector of the question: Ollama and the model of the active profile (the only destination of the question)."""
    embedder = OllamaEmbedder(
        settings.ollama_base_url, profile.embedding_model, settings.embedding_timeout_seconds, profile.dimensions
    )
    try:
        (vector,) = await embedder.embed([question])
    except IngestionError:
        raise _Failure("embedding_error") from None
    return vector


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    answer_id: uuid.UUID,
    content: str,
    notice: str | None,
    context: Sequence[tuple[int, Candidate, bool]],
    ids: str,
    started: float,
) -> str | None:
    """Save the answer as COMPLETED (in a new session) and return its `done` event; None if it is no longer PENDING
    (the conversation was deleted meanwhile): then nothing more is sent."""
    async with sessions() as session:
        message = await ConversationRepository(session).complete_answer(answer_id, content, notice, context)
    if message is None:
        logger.warning("chat answer %s: not saved, it is no longer pending", ids)
        return None
    shown = [SourceOut.from_candidate(number, candidate) for number, candidate, cited in context if cited]
    logger.info(
        "chat answer completed %s notice=%s context=%d cited=%d chars=%d ms=%d",
        ids,
        notice,
        len(context),
        len(shown),
        len(content),
        _elapsed_ms(started),
    )
    return _event("done", {"message": MessageOut.from_message(message, shown).model_dump(mode="json")})


async def _fail(app: FastAPI, answer_id: uuid.UUID, code: AnswerError, ids: str, started: float) -> None:
    """Save the answer as FAILED with the code and no text, in a new session; an error here is only logged."""
    try:
        async with app.state.sessionmaker() as session:
            await ConversationRepository(session).fail_answer(answer_id, code)
    except Exception as error:
        logger.error("chat answer %s: the failure could not be saved (%s)", ids, type(error).__name__)
        return
    logger.warning("chat answer failed %s code=%s ms=%d", ids, code, _elapsed_ms(started))
