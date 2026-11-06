"""Creation of the first admin at startup (REQ-004)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import User


class MissingAdminConfig(Exception):
    """The first admin has to be created but the variable var_name does not allow it. The message never contains
    the password."""

    def __init__(self, var_name: str, reason: str = "is not set") -> None:
        super().__init__(f"{var_name} {reason}: it is needed to create the first admin (no active admin exists)")
        self.var_name = var_name


async def ensure_admin(session: AsyncSession, settings: Settings) -> None:
    """Create ADMIN_USERNAME with role admin if no active admin exists; otherwise change nothing."""
    active_admin = await session.scalar(select(User.id).where(User.role == "admin", User.is_active.is_(True)).limit(1))
    if active_admin is not None:
        return
    username = (settings.admin_username or "").strip()
    if not username:
        raise MissingAdminConfig("ADMIN_USERNAME")
    password = settings.admin_password.get_secret_value() if settings.admin_password else ""
    if not password:
        raise MissingAdminConfig("ADMIN_PASSWORD")
    taken = await session.scalar(select(User.id).where(func.lower(User.username) == username.lower()))
    if taken is not None:
        # An existing account is never changed at startup: the admin has to choose another name.
        raise MissingAdminConfig("ADMIN_USERNAME", "names an existing account that is not an active admin")
    session.add(User(username=username, password_hash=hash_password(password), role="admin", is_active=True))
    await session.commit()
