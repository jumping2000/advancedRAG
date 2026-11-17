"""The answer to a question as a stream of server-sent events (skeleton: T-03 fills it in)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request

from app.chat.retrieval import dense_search  # noqa: F401
from app.db.models import Conversation, Message, User

KEEPALIVE_SECONDS = 15.0


async def answer_events(
    request: Request, user: User, conversation: Conversation, user_message: Message, answer: Message
) -> AsyncIterator[str]:
    raise NotImplementedError
    yield ""
