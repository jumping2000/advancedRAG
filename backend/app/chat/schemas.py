"""Bodies of the chat API (SPEC-2026-11-17-fase-3-chat): the question, and the message that the `done` event carries.

`MessageOut.sources` holds only the sources the answer cites; the excerpt of a source is never part of a message
(REQ-010): `SourceOut` has the metadata saved with the answer, not the content of the chunk."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.chat.retrieval import Candidate
from app.db.models import Message


class QuestionIn(BaseModel):
    question: str


class SourceOut(BaseModel):
    number: int
    document_name: str
    section_title: str | None
    page_start: int | None
    page_end: int | None

    @classmethod
    def from_candidate(cls, number: int, candidate: Candidate) -> SourceOut:
        return cls(
            number=number,
            document_name=candidate.document_name,
            section_title=candidate.section_title,
            page_start=candidate.page_start,
            page_end=candidate.page_end,
        )


class MessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["PENDING", "COMPLETED", "FAILED"]
    error_code: str | None
    notice: str | None
    created_at: datetime
    sources: list[SourceOut]

    @classmethod
    def from_message(cls, message: Message, sources: Sequence[SourceOut] = ()) -> MessageOut:
        return cls(
            id=message.id,
            role=message.role,  # type: ignore[arg-type]  # the database check allows only these values
            content=message.content,
            status=message.status,  # type: ignore[arg-type]  # the database check allows only these values
            error_code=message.error_code,
            notice=message.notice,
            created_at=message.created_at,
            sources=list(sources),
        )
