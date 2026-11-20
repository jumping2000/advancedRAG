"""Conversations, messages and the sources of the answers (SPEC-2026-11-17-fase-3-chat, T-03, T-04 and T-05).

A conversation belongs to one user and one KB. Every read of a conversation for a user goes through `get_for`, which is
one SQL statement: the author and a KB the user can see (`KnowledgeBaseRepository.visible_kb_ids`), so a conversation of
another user, of a KB the user is not assigned to (any more) or an unknown id are the same empty result. `get_for_admin`
has no such check: the caller must have checked the admin role.

An answer is saved PENDING by `start_answer` (together with the question, in one transaction) and ends in
`complete_answer` or `fail_answer`, which change it only while it is PENDING: the answer of a conversation that was
deleted, or that ended already, is not touched. `restart_answer` (T-04) puts the last FAILED answer back to PENDING in
place, for the retry; `mark_interrupted` ends the answers that the previous backend left PENDING. The partial unique
index `messages_one_pending` refuses a second PENDING answer in a conversation, also between simultaneous requests
(`AnswerInProgress`). Nothing here logs: questions, answers and titles never reach a log.

T-05 adds what the author does with a conversation and what the admin reads. `list_for` is the same kind of statement as
`get_for` (the author, a KB the user can see). `rename`, `delete` and `excerpt` take the conversation that `get_for`
(or, for the admin, `get_for_admin`) returned, so they cannot be reached for a conversation the caller may not use;
`excerpt` in addition looks the chunk up in the KB of that conversation only, in the same statement as the source, and
only for a cited source of a message of that conversation. `list_all` has no user: the caller has checked the admin
role.

Lock order. Every writer that takes a conversation row takes it BEFORE any of its messages: `complete_answer` and
`restart_answer` lock the conversation row (`FOR NO KEY UPDATE`) as their first statement, then change the message and
touch the conversation; `start_answer` does the same before it inserts the question and the PENDING answer (a plain
insert takes only a key-share lock on the row, so a question that inserted first would hold the unique entry of its
PENDING answer while waiting for the row that a retry holds, and the retry would wait for the entry: a deadlock); the
DELETE of a conversation, and of a KB (the cascade), locks the conversation row and then removes its messages. With the
opposite order an answer saved at the moment of a delete, or a question and a retry together, is a deadlock, which
PostgreSQL resolves by aborting one of the two. The savers that find the conversation gone end quietly
(`complete_answer` gives None, `restart_answer` raises `MessageNotFound`, `start_answer` raises `ConversationGone`).
The writers that take no conversation row (`fail_answer`, `mark_interrupted`) change one message each and wait for
nothing else, so they cannot be part of a cycle: a question that waits for the unique entry of a PENDING answer that
one of them is settling just gets it released.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Text, and_, delete, exists, func, insert, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.chat.retrieval import Candidate
from app.chat.schemas import MessageOut, SourceExcerpt, SourceOut
from app.db.models import Chunk, Conversation, KnowledgeBase, Message, MessageSource, User
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


@dataclass(frozen=True)
class AdminConversationRow:
    id: uuid.UUID
    kb_id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    username: str
    kb_name: str
    message_count: int


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

    async def list_for(self, user: User, kb_id: uuid.UUID) -> list[Conversation]:
        """The user's own conversations of the KB, by last activity (then id) descending, in one statement: the author
        and a KB the user can see. A KB the user cannot see (any more), or an unknown one, is the empty list."""
        query = (
            select(Conversation)
            .where(
                Conversation.kb_id == kb_id,
                Conversation.user_id == user.id,
                Conversation.kb_id.in_(KnowledgeBaseRepository.visible_kb_ids(user)),
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        )
        return list((await self.session.scalars(query)).all())

    async def read_messages(self, conversation: Conversation) -> list[MessageOut]:
        """Every message of the conversation in order, whatever its status, each with the sources its answer cites (the
        context chunks that it does not cite are not shown), in one statement."""
        rows = await self.session.execute(
            select(Message, MessageSource)
            .outerjoin(MessageSource, and_(MessageSource.message_id == Message.id, MessageSource.cited.is_(True)))
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id, MessageSource.number)
            .execution_options(populate_existing=True)
        )
        messages: dict[uuid.UUID, Message] = {}
        shown: dict[uuid.UUID, list[SourceOut]] = {}
        for message, source in rows.all():
            messages[message.id] = message
            shown.setdefault(message.id, [])
            if source is not None:
                shown[message.id].append(
                    SourceOut(
                        number=source.number,
                        document_name=source.document_name,
                        section_title=source.section_title,
                        page_start=source.page_start,
                        page_end=source.page_end,
                    )
                )
        return [MessageOut.from_message(message, shown[message_id]) for message_id, message in messages.items()]

    async def excerpt(self, conversation: Conversation, message_id: uuid.UUID, number: int) -> SourceExcerpt | None:
        """The cited source `number` of the message `message_id` of the conversation, with the content of its chunk read
        now, in one statement: the message must be one of the conversation, the source a cited one of that message, and
        the chunk is looked up in the KB of the conversation only (an outer join: no chunk is `available=False`, with
        the document name saved with the source). None for anything else. Nothing is written."""
        row = (
            await self.session.execute(
                select(
                    MessageSource.number,
                    MessageSource.document_name,
                    MessageSource.section_title,
                    MessageSource.page_start,
                    MessageSource.page_end,
                    Chunk.content,
                )
                .select_from(MessageSource)
                .join(Message, and_(Message.id == MessageSource.message_id, Message.conversation_id == conversation.id))
                .outerjoin(Chunk, and_(Chunk.id == MessageSource.chunk_id, Chunk.kb_id == conversation.kb_id))
                .where(MessageSource.message_id == message_id, MessageSource.number == number, MessageSource.cited)
            )
        ).first()
        if row is None:
            return None
        return SourceExcerpt(
            number=row.number,
            document_name=row.document_name,
            section_title=row.section_title,
            page_start=row.page_start,
            page_end=row.page_end,
            available=row.content is not None,
            content=row.content,
        )

    async def list_all(self, kb_id: uuid.UUID | None, user_id: uuid.UUID | None) -> list[AdminConversationRow]:
        """Every conversation, of any user and KB, optionally of one KB and/or one user, by last activity (then id)
        descending, in one statement, with the user name, the KB name and the number of messages (all of them). Only
        for a caller that has checked the admin role."""
        message_count = (
            select(func.count(Message.id)).where(Message.conversation_id == Conversation.id).scalar_subquery()
        )
        query = (
            select(
                Conversation.id,
                Conversation.kb_id,
                Conversation.title,
                Conversation.created_at,
                Conversation.updated_at,
                User.username,
                KnowledgeBase.name.label("kb_name"),
                message_count.label("message_count"),
            )
            .join(User, User.id == Conversation.user_id)
            .join(KnowledgeBase, KnowledgeBase.id == Conversation.kb_id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        )
        if kb_id is not None:
            query = query.where(Conversation.kb_id == kb_id)
        if user_id is not None:
            query = query.where(Conversation.user_id == user_id)
        return [AdminConversationRow(*row) for row in (await self.session.execute(query)).all()]

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

    async def rename(self, conversation: Conversation, title: str) -> Conversation | None:
        """Set the title (already checked and trimmed by the caller) of the conversation; None if it is gone. A rename
        is not an activity: `updated_at` is not touched, so the list keeps its order."""
        renamed: Conversation | None = await self.session.scalar(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(title=title)
            .returning(Conversation)
            .execution_options(populate_existing=True)
        )
        await self.session.commit()
        return renamed

    async def delete(self, conversation: Conversation) -> bool:
        """Delete the conversation with its messages and sources (cascade); False if it was gone already. The statement
        locks the conversation row and the cascade then the messages: the order of the module (lock order)."""
        deleted = await self.session.scalar(
            delete(Conversation).where(Conversation.id == conversation.id).returning(Conversation.id)
        )
        await self.session.commit()
        return deleted is not None

    async def start_answer(self, conversation: Conversation, question: str) -> tuple[Message, Message]:
        """Save the question (COMPLETED) and its answer (PENDING, empty) in one transaction, and mark the activity of
        the conversation. `AnswerInProgress` if it already has a PENDING answer: nothing is saved then. The conversation
        row is locked first (see the module: lock order); `ConversationGone` if it is gone."""
        locked = await self.session.scalar(
            select(Conversation.id).where(Conversation.id == conversation.id).with_for_update(key_share=True)
        )
        if locked is None:
            await self.session.commit()  # nothing was written: it only ends the transaction
            raise ConversationGone
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
        self,
        message_id: uuid.UUID,
        content: str,
        notice: str | None,
        sources: Sequence[tuple[int, Candidate, bool]],
        search_query: str | None = None,
    ) -> Message | None:
        """End a PENDING answer as COMPLETED with its text, its notice, the question that was searched when the
        rewriting changed it (`search_query`, else None) and all the sources of its context, in one transaction. None,
        and nothing written, if the answer is not PENDING (conversation deleted, answer ended). The conversation row is
        locked first (see the module: lock order)."""
        conversation_id = await self.session.scalar(
            select(Conversation.id)
            .join(Message, Message.conversation_id == Conversation.id)
            .where(Message.id == message_id)
            .with_for_update(key_share=True, of=Conversation)
        )
        if conversation_id is None:  # the conversation is gone (and with it the answer)
            await self.session.commit()  # nothing was written: it only ends the transaction
            return None
        message: Message | None = await self.session.scalar(
            update(Message)
            .where(Message.id == message_id, Message.status == "PENDING")
            .values(content=content, status="COMPLETED", error_code=None, notice=notice, search_query=search_query)
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

    async def restart_answer(self, conversation: Conversation, message_id: uuid.UUID) -> Message:
        """Put the FAILED answer `message_id` back to PENDING, empty (no `search_query` either), in place: the same row,
        so the conversation keeps one question and one answer in the same order (`created_at` is not touched), and the
        activity of the conversation is marked. It must be the last message of the conversation: that is checked, with
        its status and its role, in the UPDATE itself, so nothing can come between the check and the change.
        `MessageNotFound` if the conversation has no such message, `AnswerInProgress` if it has a PENDING answer
        (checked before `NotRetryable`: while an answer is in progress, any retry is refused for that reason; also when
        a simultaneous request got the unique index `messages_one_pending` first), `NotRetryable` otherwise (a question, a COMPLETED answer, a failed
        answer that a later message follows). Nothing is written when it raises. The conversation row is locked first
        (see the module: lock order); if it is gone the message is gone too (`MessageNotFound`)."""
        conversation_id = conversation.id
        locked = await self.session.scalar(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update(key_share=True)
        )
        if locked is None:
            await self.session.commit()  # nothing was written: it only ends the transaction
            raise MessageNotFound
        later = aliased(Message)
        is_last = ~exists().where(
            later.conversation_id == Message.conversation_id,
            tuple_(later.created_at, later.id) > tuple_(Message.created_at, Message.id),
        )
        try:
            restarted: Message | None = await self.session.scalar(
                update(Message)
                .where(
                    Message.id == message_id,
                    Message.conversation_id == conversation_id,
                    Message.role == "assistant",
                    Message.status == "FAILED",
                    is_last,
                )
                .values(content="", status="PENDING", error_code=None, notice=None, search_query=None)
                .returning(Message)
                .execution_options(populate_existing=True)
            )
            if restarted is not None:
                await self.session.execute(
                    update(Conversation).where(Conversation.id == conversation_id).values(updated_at=func.now())
                )
        except IntegrityError as error:
            await self.session.rollback()
            if _sqlstate(error) == UNIQUE_VIOLATION:  # a question or a retry got to be the PENDING answer first
                raise AnswerInProgress from None
            raise
        if restarted is not None:
            await self.session.commit()
            return restarted
        known = await self.session.scalar(
            select(Message.id).where(Message.id == message_id, Message.conversation_id == conversation_id)
        )
        pending = await self.session.scalar(
            select(Message.id).where(Message.conversation_id == conversation_id, Message.status == "PENDING").limit(1)
        )
        await (
            self.session.commit()
        )  # nothing was written: it only ends the transaction (a rollback would expire objects)
        if known is None:
            raise MessageNotFound
        raise AnswerInProgress if pending is not None else NotRetryable

    async def question_of(self, answer: Message) -> Message | None:
        """The question that `answer` answers: the last question before it in the conversation."""
        question: Message | None = await self.session.scalar(
            select(Message)
            .where(
                Message.conversation_id == answer.conversation_id,
                Message.role == "user",
                tuple_(Message.created_at, Message.id) < tuple_(answer.created_at, answer.id),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return question

    async def is_pending(self, message_id: uuid.UUID) -> bool:
        """Whether the message exists and is a PENDING answer: False once the conversation was deleted or the answer
        settled, so an answer that no one waits for any more is not started."""
        found = await self.session.scalar(
            select(Message.id).where(Message.id == message_id, Message.status == "PENDING")
        )
        return found is not None

    async def mark_interrupted(self) -> int:
        """End every PENDING answer as FAILED `interrupted`, without text, and return how many: the answers that the
        backend that generated them did not live to settle (REQ-017). Called once, at the start of the backend, before
        it serves a request: a single process generates the answers (ADR-003), so at that moment no PENDING answer
        has anyone working on it."""
        result = await self.session.execute(
            update(Message)
            .where(Message.status == "PENDING")
            .values(content="", status="FAILED", error_code="interrupted", notice=None)
            .returning(Message.id)
        )
        count = len(result.all())
        await self.session.commit()
        return count
