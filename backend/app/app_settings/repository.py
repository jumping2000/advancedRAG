"""The switches of the two steps, in the single row of `app_settings` (REQ-003)."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


@dataclass(frozen=True)
class Switches:
    rerank_enabled: bool
    rewrite_enabled: bool


class AppSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # Skeleton of T-01: the stubs that the tests run against ---------------------------------------------------------

    async def switches(self) -> Switches:
        raise NotImplementedError

    async def update(self, rerank_enabled: bool | None = None, rewrite_enabled: bool | None = None) -> Switches:
        raise NotImplementedError


def active_steps(settings: Settings, switches: Switches) -> tuple[bool, bool]:
    raise NotImplementedError
