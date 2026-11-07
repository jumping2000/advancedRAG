"""Request dependencies of the API: authenticated user, admin role, JSON-only requests, error bodies."""

from email.message import Message
from typing import Annotated

from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.sessions import SessionStore
from app.db.models import User
from app.db.session import get_session

SESSION_COOKIE = "rag_session"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The only route that takes a multipart body (the file upload of a document) and the header it must carry. The route
# path is the one declared by the documents router: this FastAPI keeps the "/api" prefix of main.py out of it.
UPLOAD_ROUTE_PATH = "/kbs/{kb_id}/documents"
UPLOAD_HEADER = ("x-requested-with", "XMLHttpRequest")


class ApiError(Exception):
    """An error answered with {"error": {"code", "message"}}; the UI translates the code."""

    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra  # more fields for the `error` object, e.g. the document a duplicate upload collides with


def not_authenticated() -> ApiError:
    return ApiError(401, "not_authenticated", "Authentication required")


def error_response(status_code: int, code: str, message: str, extra: dict[str, object] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **(extra or {})}}, status_code=status_code)


async def api_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)
    return error_response(error.status_code, error.code, error.message, error.extra)


async def validation_error_handler(_: Request, __: Exception) -> JSONResponse:
    # The details are not returned: they would echo the input, passwords included.
    return error_response(422, "validation_error", "The request is not valid")


_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


async def http_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    return error_response(error.status_code, _HTTP_CODES.get(error.status_code, "http_error"), str(error.detail))


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.session_store
    return store


async def require_json(request: Request) -> None:
    """Only JSON bodies (CSRF): a form or text/plain request from another site gets 422 before doing anything.

    The one exception is the document upload (POST on UPLOAD_ROUTE_PATH): it takes multipart/form-data, but only with
    the X-Requested-With header, which a form of another site cannot send."""
    content_type = request.headers.get("content-type")
    if request.method in UNSAFE_METHODS and content_type is not None:
        message = Message()
        message["content-type"] = content_type
        media_type = message.get_content_type()
        if media_type == "application/json":
            return
        if media_type == "multipart/form-data" and _is_upload(request):
            header, value = UPLOAD_HEADER
            if request.headers.get(header) == value:
                return
            raise RequestValidationError([{"type": "missing", "loc": ("header", header)}])
        raise RequestValidationError([{"type": "content_type", "loc": ("header", "content-type")}])


def _is_upload(request: Request) -> bool:
    route = request.scope.get("route")
    return request.method == "POST" and getattr(route, "path", None) == UPLOAD_ROUTE_PATH


DbSession = Annotated[AsyncSession, Depends(get_session)]
Sessions = Annotated[SessionStore, Depends(get_session_store)]


async def current_user(request: Request, session: DbSession, store: Sessions) -> User:
    """The user of the session cookie, reloaded from the database: 401 if the session is missing or expired, the
    user is inactive or its session_epoch changed (deactivation and password reset invalidate the sessions)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise not_authenticated()
    data = await store.get(token)
    if data is None:
        raise not_authenticated()
    user = await session.get(User, data.user_id)
    if user is None or not user.is_active or user.session_epoch != data.epoch:
        await store.delete(token)
        raise not_authenticated()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Administrator role required")
    return user
