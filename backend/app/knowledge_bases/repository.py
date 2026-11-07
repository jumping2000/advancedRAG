"""Every read of a knowledge base goes through KnowledgeBaseRepository (skeleton, T-06)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeBase, User


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for(self, user: User) -> list[KnowledgeBase]:
        return []

    async def get_for(self, user: User, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return None
