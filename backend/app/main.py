"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import health
from app.auth import routes as auth
from app.auth.deps import ApiError, api_error_handler, http_error_handler, require_json, validation_error_handler
from app.auth.sessions import SessionStore
from app.config import Settings, get_settings
from app.db.session import create_engine
from app.documents import routes as documents
from app.knowledge_bases import routes as knowledge_bases
from app.users import routes as users


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    redis = Redis.from_url(settings.redis_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="advancedRAG", lifespan=lifespan, dependencies=[Depends(require_json)])
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = redis
    app.state.session_store = SessionStore(redis, settings.session_idle_timeout_minutes * 60)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(knowledge_bases.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    return app
