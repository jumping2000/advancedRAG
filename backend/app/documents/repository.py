"""Database access to the documents of a knowledge base.

Every method takes the kb_id and puts it in the query, so a document of another KB is never read or changed. The
routes check the KB first with KnowledgeBaseRepository.get_for (AGENTS.md: access to a KB goes through it).
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document

CREATE_ATTEMPTS = 3


class DuplicateDocument(Exception):
    """The KB already has a document with the same checksum; `existing` is that document."""

    def __init__(self, existing: Document) -> None:
        super().__init__("duplicate document")
        self.existing = existing


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, kb_id: uuid.UUID, document_id: uuid.UUID, filename: str, fmt: str, size_bytes: int, checksum: str
    ) -> Document:
        """Insert the document as QUEUED and commit. The unique index (kb_id, checksum) decides duplicates, also
        between concurrent requests: the loser raises DuplicateDocument with the document that won."""
        statement = (
            insert(Document)
            .values(
                id=document_id, kb_id=kb_id, filename=filename, format=fmt, size_bytes=size_bytes, checksum=checksum
            )
            .on_conflict_do_nothing(index_elements=["kb_id", "checksum"])
            .returning(Document)
            .execution_options(populate_existing=True)
        )
        for _ in range(CREATE_ATTEMPTS):
            created: Document | None = await self.session.scalar(statement)
            if created is not None:
                await self.session.commit()
                return created
            existing: Document | None = await self.session.scalar(
                select(Document).where(Document.kb_id == kb_id, Document.checksum == checksum)
            )
            if existing is not None:
                raise DuplicateDocument(existing)
            # The document that blocked the insert was deleted before it could be read: insert again.
        raise RuntimeError("the document could be neither inserted nor found")

    async def list_for_kb(self, kb_id: uuid.UUID) -> list[Document]:
        """The documents of the KB, the most recent first."""
        query = select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc(), Document.id.desc())
        return list((await self.session.scalars(query)).all())

    async def get(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        document: Document | None = await self.session.scalar(
            select(Document).where(Document.kb_id == kb_id, Document.id == document_id)
        )
        return document

    async def delete(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        deleted = await self.session.scalar(
            delete(Document).where(Document.kb_id == kb_id, Document.id == document_id).returning(Document.id)
        )
        await self.session.commit()
        return deleted is not None

    async def claim(self, document_id: uuid.UUID) -> Document | None:
        return None

    async def mark_ready(self, document_id: uuid.UUID, chunk_count: int) -> bool:
        return False

    async def mark_failed(self, document_id: uuid.UUID, code: str, message: str) -> bool:
        return False
