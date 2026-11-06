"""User management by the admin (REQ-006, REQ-007): create, list, deactivate or reactivate, reset the password.

Deactivation and password reset increment session_epoch: every session of the user stops working at its next request
(current_user compares the epoch), without searching the sessions in Redis.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.bootstrap import USERNAME_PATTERN
from app.auth.deps import ApiError
from app.auth.passwords import MIN_PASSWORD_LENGTH, hash_password
from app.db.models import User

UNIQUE_VIOLATION = "23505"


def valid_username(raw: str) -> str:
    """The name without leading or trailing spaces, if it follows the rule shared with the first admin."""
    username = raw.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ApiError(
            422, "invalid_username", 'The username must have 3-64 characters among letters, digits, ".", "_", "-"'
        )
    return username


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(
            422, "password_too_short", f"The password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )


def user_not_found() -> ApiError:
    return ApiError(404, "user_not_found", "User not found")


async def list_users(session: AsyncSession) -> Sequence[User]:
    result = await session.scalars(select(User).order_by(func.lower(User.username)))
    return result.all()


async def create_user(session: AsyncSession, username: str, password: str, role: str) -> User:
    name = valid_username(username)
    check_password(password)
    password_hash = await run_in_threadpool(hash_password, password)  # Argon2id is slow on purpose: off the loop
    user = User(username=name, password_hash=password_hash, role=role, is_active=True)
    session.add(user)
    try:
        # The unique index on lower(username) decides, also between concurrent requests.
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if getattr(error.orig, "sqlstate", None) == UNIQUE_VIOLATION:
            raise ApiError(409, "username_taken", "The username is already taken") from None
        raise
    await session.refresh(user)
    return user


async def set_active(session: AsyncSession, admin: User, user_id: uuid.UUID, is_active: bool) -> User:
    if not is_active and user_id == admin.id:
        raise ApiError(409, "cannot_deactivate_self", "Administrators cannot deactivate themselves")
    values: dict[str, object] = {"is_active": is_active}
    if not is_active:
        values["session_epoch"] = User.session_epoch + 1
    user = await session.scalar(
        update(User)
        .where(User.id == user_id)
        .values(**values)
        .returning(User)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise user_not_found()
    await session.commit()
    return user


async def reset_password(session: AsyncSession, user_id: uuid.UUID, password: str) -> None:
    check_password(password)
    password_hash = await run_in_threadpool(hash_password, password)
    updated = await session.scalar(
        update(User)
        .where(User.id == user_id)
        .values(password_hash=password_hash, session_epoch=User.session_epoch + 1)
        .returning(User.id)
    )
    if updated is None:
        raise user_not_found()
    await session.commit()
