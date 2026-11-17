"""Conversations, messages and the sources of the answers (SPEC-2026-11-17-fase-3-chat, T-03 part).

A conversation belongs to one user and one KB. Every read of a conversation for a user goes through `get_for`, which is
one SQL statement: the author and a KB the user can see (`KnowledgeBaseRepository.visible_kb_ids`), so a conversation of
another user, of a KB the user is not assigned to (any more) or an unknown id are the same empty result. `get_for_admin`
has no such check: the caller must have checked the admin role.

An answer is saved PENDING by `start_answer` (together with the question, in one transaction) and ends in
`complete_answer` or `fail_answer`, which change it only while it is PENDING: the answer of a conversation that was
deleted, or that ended already, is not touched. The partial unique index `messages_one_pending` refuses a second PENDING
answer in a conversation, also between simultaneous requests (`AnswerInProgress`). Nothing here logs: questions, answers
and titles never reach a log.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Text, and_, func, insert, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.retrieval import Candidate
from app.db.models import Conversation, Message, MessageSource, User
from app.knowledge_bases.repository import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, KnowledgeBaseRepository

# A text has at least one character that Python's `str.isspace` does not count as white space (`build_messages` drops
# a message that `strip()` empties, so the query must agree with it: PostgreSQL's `\S` does not know the same set)
_SPACES = "".join(f"\\U{ord(c):08x}" for c in map(chr, range(0x3001)) if c.isspace())
_HAS_TEXT = f"[^{_SPACES}]"


class AnswerInProgress(Exception):
    """The conversation already has a PENDING answer (the unique index `messages_one_pending` refused another one)."""


class NotRetryable(Exception):
    """The message is not the last answer of its conversation, or that answer is not FAILED."""


class MessageNotFound(Exception):
    """The conversation has no message with this id."""


class KbNotVisible(Exception):
    """The KB does not exist or the user cannot see it: no conversation was created."""


class ConversationGone(Exception):
    """The conversation was deleted while the question was being saved."""


def _sqlstate(error: IntegrityError) -> str | None:
    state: str | None = getattr(error.orig, "sqlstate", None)
    return state


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Reads ----------------------------------------------------------------------------------------------------------

    async def get_for(self, user: User, conversation_id: uuid.UUID) -> Conversation | None:
        """The conversation if the user is its author and can see its KB (one statement), else None."""
        conversation: Conversation | None = await self.session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
                Conversation.kb_id.in_(KnowledgeBaseRepository.visible_kb_ids(user)),
            )
        )
        return conversation

    async def get_for_admin(self, conversation_id: uuid.UUID) -> Conversation | None:
        """Any conversation, whoever its author is: only for a caller that has checked the admin role."""
        conversation: Conversation | None = await self.session.scalar(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        return conversation

    async def history(self, conversation: Conversation, limit: int, before: Message | None = None) -> list[Message]:
        """The last `limit` messages of the conversation that are sent to the model, oldest first, only those before
        the message `before` when given. The unit is the exchange: a question and the answer that follows it. An answer
        is sent only when it is COMPLETED and has text, a question only when the message right after it is such an
        answer. So a failed answer, an answer saved with a notice (no text), a pending one, and the question of each,
        are all left out, and the roles alternate. The limit counts the messages that are left."""
        if limit <= 0:
            return []
        order = (Message.created_at, Message.id)
        numbered = (
            select(
                Message.id.label("id"),
                func.lead(Message.role).over(order_by=order).label("next_role"),
                func.lead(Message.status).over(order_by=order).label("next_status"),
                func.lead(Message.content).over(order_by=order).label("next_content"),
            )
            .where(Message.conversation_id == conversation.id)
            .subquery()
        )
        query = (
            select(Message)
            .join(numbered, numbered.c.id == Message.id)
            .where(
                Message.status == "COMPLETED",
                Message.content.op("~")(_HAS_TEXT),
                or_(
                    Message.role != "user",
                    and_(
                        numbered.c.next_role == "assistant",
                        numbered.c.next_status == "COMPLETED",
                        numbered.c.next_content.op("~")(_HAS_TEXT),
                    ),
                ),
            )
        )
        if before is not None:
            query = query.where(tuple_(Message.created_at, Message.id) < tuple_(before.created_at, before.id))
        newest_first = await self.session.scalars(
            query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
        return list(reversed(newest_first.all()))

    # Writes ---------------------------------------------------------------------------------------------------------

    async def create(self, user: User, kb_id: uuid.UUID, title: str) -> Conversation:
        """A conversation of the user on the KB; `KbNotVisible` if the user cannot see the KB (the check is in the
        INSERT ... SELECT itself, not before it)."""
        conversation_id = uuid.uuid4()
        now = datetime.now(UTC)
        kb = literal(kb_id, UUID(as_uuid=True))
        row = select(
            literal(conversation_id, UUID(as_uuid=True)),
            kb,
            literal(user.id, UUID(as_uuid=True)),
            literal(title, Text),
            literal(now, DateTime(timezone=True)),
            literal(now, DateTime(timezone=True)),
        ).where(kb.in_(KnowledgeBaseRepository.visible_kb_ids(user)))
        statement = (
            insert(Conversation)
            .from_select(["id", "kb_id", "user_id", "title", "created_at", "updated_at"], row)
            .returning(Conversation.id)
        )
        try:
            created = await self.session.scalar(statement)
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            if _sqlstate(error) == FOREIGN_KEY_VIOLATION:  # the KB was deleted after the check
                raise KbNotVisible from None
            raise
        if created is None:
            raise KbNotVisible
        conversation = await self.session.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation

    async def start_answer(self, conversation: Conversation, question: str) -> tuple[Message, Message]:
        """Save the question (COMPLETED) and its answer (PENDING, empty) in one transaction, and mark the activity of
        the conversation. `AnswerInProgress` if it already has a PENDING answer: nothing is saved then."""
        now = datetime.now(UTC)
        # The answer is one microsecond after the question, so that the order by `created_at` is the order of the
        # conversation (both rows of one transaction would otherwise have the same `now()`)
        asked = Message(
            conversation_id=conversation.id, role="user", content=question, status="COMPLETED", created_at=now
        )
        answer = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="PENDING",
            created_at=now + timedelta(microseconds=1),
        )
        try:
            self.session.add_all([asked, answer])
            await self.session.execute(
                update(Conversation).where(Conversation.id == conversation.id).values(updated_at=now)
            )
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            state = _sqlstate(error)
            if state == UNIQUE_VIOLATION:
                raise AnswerInProgress from None
            if state == FOREIGN_KEY_VIOLATION:
                raise ConversationGone from None
            raise
        return asked, answer

    async def complete_answer(
        self, message_id: uuid.UUID, content: str, notice: str | None, sources: Sequence[tuple[int, Candidate, bool]]
    ) -> Message | None:
        """End a PENDING answer as COMPLETED with its text, its notice and all the sources of its context, in one
        transaction. None, and nothing written, if the answer is not PENDING (conversation deleted, answer ended)."""
        message: Message | None = await self.session.scalar(
            update(Message)
            .where(Message.id == message_id, Message.status == "PENDING")
            .values(content=content, status="COMPLETED", error_code=None, notice=notice)
            .returning(Message)
            .execution_options(populate_existing=True)
        )
        if message is None:
            await (
                self.session.commit()
            )  # nothing was written: it only ends the transaction (a rollback would expire the caller's objects)
            return None
        self.session.add_all(
            MessageSource(
                message_id=message_id,
                number=number,
                chunk_id=candidate.chunk_id,
                document_name=candidate.document_name,
                section_title=candidate.section_title,
                page_start=candidate.page_start,
                page_end=candidate.page_end,
                dense_rank=candidate.dense_rank,
                bm25_rank=candidate.bm25_rank,
                score=candidate.score,
                cited=cited,
            )
            for number, candidate, cited in sources
        )
        await self.session.execute(
            update(Conversation).where(Conversation.id == message.conversation_id).values(updated_at=func.now())
        )
        await self.session.commit()
        return message

    async def fail_answer(self, message_id: uuid.UUID, code: str) -> bool:
        """End a PENDING answer as FAILED with the code and no text. True if it did; an answer that is not PENDING is
        left as it is (False): a call after the answer ended, or a second call, changes nothing."""
        result = await self.session.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == "PENDING")
            .values(content="", status="FAILED", error_code=code, notice=None)
            .returning(Message.id)
        )
        changed = result.first() is not None
        await self.session.commit()
        return changed

    # Skeleton of T-04 (stubs: the tests of T-04 fail on their assertions until the next commits) ---------------------

    async def restart_answer(self, conversation: Conversation, message_id: uuid.UUID) -> Message:
        raise NotRetryable

    async def question_of(self, answer: Message) -> Message | None:
        return None

    async def mark_interrupted(self) -> int:
        return 0
