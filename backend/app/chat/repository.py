"""Conversations, messages and the sources of the answers (skeleton: T-03 fills it in)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.retrieval import Candidate
from app.db.models import Conversation, Message, User


class AnswerInProgress(Exception):
    """The conversation already has a PENDING answer."""


class KbNotVisible(Exception):
    """The KB does not exist or the user cannot see it."""


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for(self, user: User, conversation_id: uuid.UUID) -> Conversation | None:
        raise NotImplementedError

    async def get_for_admin(self, conversation_id: uuid.UUID) -> Conversation | None:
        raise NotImplementedError

    async def create(self, user: User, kb_id: uuid.UUID, title: str) -> Conversation:
        raise NotImplementedError

    async def start_answer(self, conversation: Conversation, question: str) -> tuple[Message, Message]:
        raise NotImplementedError

    async def complete_answer(
        self, message_id: uuid.UUID, content: str, notice: str | None, sources: Sequence[tuple[int, Candidate, bool]]
    ) -> Message | None:
        raise NotImplementedError

    async def fail_answer(self, message_id: uuid.UUID, code: str) -> None:
        raise NotImplementedError

    async def history(self, conversation: Conversation, limit: int, before: Message | None = None) -> list[Message]:
        raise NotImplementedError
