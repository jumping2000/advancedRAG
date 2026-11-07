"""Every access to a knowledge base goes through KnowledgeBaseRepository (constitution: isolation in the SQL query).

For any role other than admin the read of knowledge_bases joins kb_members on the user's id in the same query, so a
KB the user is not assigned to is never loaded: "not assigned" and "unknown" are the same empty result. Phases 2-3
reuse list_for/get_for for every access to a KB.
"""

import uuid
from typing import Any

from sqlalchemy import Select, and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KbMember, KnowledgeBase, User

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


class KbNameTaken(Exception):
    """The unique index on lower(name) refused the name, also between concurrent requests."""


def _sqlstate(error: IntegrityError) -> str | None:
    state: str | None = getattr(error.orig, "sqlstate", None)
    return state


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _visible_to(user: User) -> Select[KnowledgeBase]:
        query = select(KnowledgeBase)
        if user.role != "admin":  # fail closed: only the exact admin role reads every KB
            query = query.join(KbMember, and_(KbMember.kb_id == KnowledgeBase.id, KbMember.user_id == user.id))
        return query

    async def list_for(self, user: User) -> list[KnowledgeBase]:
        query = self._visible_to(user).order_by(func.lower(KnowledgeBase.name), KnowledgeBase.id)
        return list((await self.session.scalars(query)).all())

    async def get_for(self, user: User, kb_id: uuid.UUID) -> KnowledgeBase | None:
        kb: KnowledgeBase | None = await self.session.scalar(self._visible_to(user).where(KnowledgeBase.id == kb_id))
        return kb

    # Administration (the routes allow it to the admin only) ---------------------------------------------------------

    async def _commit(self) -> None:
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            if _sqlstate(error) == UNIQUE_VIOLATION:
                raise KbNameTaken from None
            raise

    async def create(self, name: str, description: str | None) -> KnowledgeBase:
        kb = KnowledgeBase(name=name, description=description)
        self.session.add(kb)
        await self._commit()
        await self.session.refresh(kb)
        return kb

    async def update(self, kb_id: uuid.UUID, changes: dict[str, Any]) -> KnowledgeBase | None:
        if not changes:
            return await self.session.get(KnowledgeBase, kb_id)
        try:
            kb: KnowledgeBase | None = await self.session.scalar(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(**changes)
                .returning(KnowledgeBase)
                .execution_options(populate_existing=True)
            )
        except IntegrityError as error:
            await self.session.rollback()
            if _sqlstate(error) == UNIQUE_VIOLATION:
                raise KbNameTaken from None
            raise
        if kb is not None:
            await self._commit()
        return kb

    async def delete(self, kb_id: uuid.UUID) -> bool:
        """Delete the KB; its kb_members rows go with it (ON DELETE CASCADE)."""
        deleted = await self.session.scalar(
            delete(KnowledgeBase).where(KnowledgeBase.id == kb_id).returning(KnowledgeBase.id)
        )
        await self.session.commit()
        return deleted is not None

    async def members(self, kb_id: uuid.UUID) -> list[User]:
        query = (
            select(User)
            .join(KbMember, KbMember.user_id == User.id)
            .where(KbMember.kb_id == kb_id)
            .order_by(func.lower(User.username), User.id)
        )
        return list((await self.session.scalars(query)).all())

    async def add_member(self, kb_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Assign the user; an existing assignment stays the only one. False if the KB or the user is gone."""
        try:
            await self.session.execute(insert(KbMember).values(kb_id=kb_id, user_id=user_id).on_conflict_do_nothing())
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            if _sqlstate(error) == FOREIGN_KEY_VIOLATION:
                return False
            raise
        return True

    async def remove_member(self, kb_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.session.execute(delete(KbMember).where(KbMember.kb_id == kb_id, KbMember.user_id == user_id))
        await self.session.commit()
