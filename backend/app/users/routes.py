"""Users admin API (REQ-007): only for the admin role (403 otherwise, 401 without a session)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.auth.deps import DbSession, require_admin
from app.db.models import User
from app.users import service
from app.users.schemas import PasswordIn, UserCreateIn, UserOut, UserUpdateIn

Admin = Annotated[User, Depends(require_admin)]

# The role is checked before the body: a user gets 403 whatever it sends.
router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])


@router.get("")
async def list_users(session: DbSession) -> list[UserOut]:
    return [UserOut.model_validate(user) for user in await service.list_users(session)]


@router.post("", status_code=201)
async def create_user(body: UserCreateIn, session: DbSession) -> UserOut:
    user = await service.create_user(session, body.username, body.password, body.role)
    return UserOut.model_validate(user)


@router.patch("/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserUpdateIn, admin: Admin, session: DbSession) -> UserOut:
    user = await service.set_active(session, admin, user_id, body.is_active)
    return UserOut.model_validate(user)


@router.put("/{user_id}/password", status_code=204, response_class=Response)
async def reset_password(user_id: uuid.UUID, body: PasswordIn, session: DbSession) -> None:
    await service.reset_password(session, user_id, body.password)
