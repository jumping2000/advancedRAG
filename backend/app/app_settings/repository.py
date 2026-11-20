"""The switches of the two steps, in the single row of `app_settings` (REQ-003).

The migration 0006 inserts the row (`id = 1`) with both switches off. An answer reads the switches once, when it starts
(`switches`), and keeps them until it ends; `update` commits at once, so the next question reads the new values without
a restart. A step is active only when it is switched on and its service is configured (`active_steps`).
"""

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AppSetting

ROW_ID = 1  # the only row (CHECK (id = 1))


@dataclass(frozen=True)
class Switches:
    rerank_enabled: bool
    rewrite_enabled: bool


class AppSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def switches(self) -> Switches:
        row = (
            await self.session.execute(
                select(AppSetting.rerank_enabled, AppSetting.rewrite_enabled).where(AppSetting.id == ROW_ID)
            )
        ).one()
        return Switches(rerank_enabled=row.rerank_enabled, rewrite_enabled=row.rewrite_enabled)

    async def update(self, rerank_enabled: bool | None = None, rewrite_enabled: bool | None = None) -> Switches:
        """Save the switches given (None leaves one as it is) and return the saved state, committed."""
        values: dict[str, bool] = {}
        if rerank_enabled is not None:
            values["rerank_enabled"] = rerank_enabled
        if rewrite_enabled is not None:
            values["rewrite_enabled"] = rewrite_enabled
        if not values:
            return await self.switches()
        row = (
            await self.session.execute(
                update(AppSetting)
                .where(AppSetting.id == ROW_ID)
                .values(**values, updated_at=func.now())
                .returning(AppSetting.rerank_enabled, AppSetting.rewrite_enabled)
            )
        ).one()
        await self.session.commit()
        return Switches(rerank_enabled=row.rerank_enabled, rewrite_enabled=row.rewrite_enabled)


def active_steps(settings: Settings, switches: Switches) -> tuple[bool, bool]:
    """(rerank active, rewrite active): a step is active when it is switched on and its service is configured."""
    return (
        switches.rerank_enabled and settings.rerank_configured,
        switches.rewrite_enabled and settings.rewrite_configured,
    )
