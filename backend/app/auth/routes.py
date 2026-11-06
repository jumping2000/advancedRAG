"""Authentication API: login, logout, current user (REQ-005)."""

import uuid
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.auth.deps import SESSION_COOKIE, ApiError, CurrentUser, DbSession, Sessions
from app.auth.passwords import dummy_hash, verify_password
from app.db.models import User

router = APIRouter(prefix="/auth")


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: Literal["admin", "user"]
    is_active: bool
    language: Literal["it", "en"] | None


def invalid_credentials() -> ApiError:
    # One answer for wrong password, unknown user and deactivated user: nothing tells them apart.
    return ApiError(401, "invalid_credentials", "Invalid username or password")


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    # Secure only in HTTPS: the scheme comes from the ASGI server, which applies X-Forwarded-Proto from Nginx.
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/"
    )


@router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: DbSession,
    store: Sessions,
) -> UserOut:
    # Exact match (owner's decision): no case folding, no trimming.
    user = await session.scalar(select(User).where(User.username == body.username))
    password_ok = verify_password(body.password, user.password_hash if user else dummy_hash())
    if user is None or not password_ok or not user.is_active:
        raise invalid_credentials()
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await store.delete(previous)
    token = await store.create(user.id, user.session_epoch)
    set_session_cookie(response, request, token)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    request: Request,
    response: Response,
    _: CurrentUser,
    store: Sessions,
) -> None:
    await store.delete(request.cookies[SESSION_COOKIE])
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/"
    )


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
