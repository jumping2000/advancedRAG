"""Response bodies of the documents API (REQ-006): never the checksum, the path or the content of a file."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.db.models import Document


class StatusReason(BaseModel):
    code: str
    message: str


class DocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    size_bytes: int
    status: Literal["QUEUED", "PROCESSING", "READY", "FAILED", "UNSUPPORTED"]
    status_reason: StatusReason | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_document(cls, document: Document) -> "DocumentOut":
        reason = None
        if document.status_reason_code is not None and document.status_reason_message is not None:
            reason = StatusReason(code=document.status_reason_code, message=document.status_reason_message)
        return cls(
            id=document.id,
            kb_id=document.kb_id,
            filename=document.filename,
            size_bytes=document.size_bytes,
            status=document.status,  # type: ignore[arg-type]  # the database check allows only these values
            status_reason=reason,
            chunk_count=document.chunk_count,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
