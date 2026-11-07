"""Database access to the documents of a knowledge base.

Every method takes the kb_id and puts it in the query, so a document of another KB is never read or changed. The
routes check the KB first with KnowledgeBaseRepository.get_for (AGENTS.md: access to a KB goes through it).
"""

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document

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
        """QUEUED -> PROCESSING in one statement, for the worker: the document, or None when there is no QUEUED
        document with this id (deleted, already taken, or not in the queue). Commits."""
        statement = (
            update(Document)
            .where(Document.id == document_id, Document.status == "QUEUED")
            .values(status="PROCESSING", updated_at=func.now())
            .returning(Document)
            .execution_options(populate_existing=True)
        )
        claimed: Document | None = await self.session.scalar(statement)
        await self.session.commit()
        return claimed

    async def mark_ready(self, document_id: uuid.UUID, chunk_count: int) -> bool:
        """PROCESSING -> READY with the number of chunks; False if the document is no longer PROCESSING. Commits."""
        return await self._finish(document_id, status="READY", chunk_count=chunk_count)

    async def mark_failed(self, document_id: uuid.UUID, code: str, message: str) -> bool:
        """PROCESSING -> FAILED with the reason, and no chunk of the document left, in one transaction; False if the
        document is no longer PROCESSING (then nothing changes). Commits."""
        return await self._finish(
            document_id,
            status="FAILED",
            chunk_count=0,
            status_reason_code=code,
            status_reason_message=message,
            delete_chunks=True,
        )

    async def mark_unsupported(self, document_id: uuid.UUID, code: str, message: str) -> bool:
        """Skeleton: PROCESSING -> UNSUPPORTED is not written yet."""
        return False

    async def requeue(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        """Skeleton: FAILED -> QUEUED is not written yet."""
        return None

    async def _finish(self, document_id: uuid.UUID, *, delete_chunks: bool = False, **values: str | int) -> bool:
        reason = {"status_reason_code": None, "status_reason_message": None}
        try:
            changed = await self.session.scalar(
                update(Document)
                .where(Document.id == document_id, Document.status == "PROCESSING")
                .values(**{**reason, **values}, updated_at=func.now())
                .returning(Document.id)
            )
            if changed is not None and delete_chunks:
                await self.session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise
        return changed is not None
