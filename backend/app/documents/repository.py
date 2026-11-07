"""Database access to the documents (skeleton). The KB is checked before, with KnowledgeBaseRepository.get_for."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document


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
        raise NotImplementedError

    async def list_for_kb(self, kb_id: uuid.UUID) -> list[Document]:
        raise NotImplementedError

    async def get(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        raise NotImplementedError

    async def delete(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        raise NotImplementedError
