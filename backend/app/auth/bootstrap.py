"""Creation of the first admin at startup (skeleton)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


class MissingAdminConfig(Exception):
    def __init__(self, var_name: str) -> None:
        super().__init__(var_name)
        self.var_name = var_name


async def ensure_admin(session: AsyncSession, settings: Settings) -> None:
    return None
