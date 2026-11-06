"""Request and response bodies of the users admin API (REQ-007)."""

from typing import Literal

from pydantic import BaseModel, StrictBool

from app.auth.routes import UserOut

__all__ = ["PasswordIn", "UserCreateIn", "UserOut", "UserUpdateIn"]


class UserCreateIn(BaseModel):
    # Name and password are checked by the service, which answers invalid_username and password_too_short.
    username: str
    password: str
    role: Literal["admin", "user"]


class UserUpdateIn(BaseModel):
    is_active: StrictBool


class PasswordIn(BaseModel):
    password: str
