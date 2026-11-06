"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import health
from app.config import Settings, get_settings
from app.db.session import create_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.sqlalchemy_url)
    redis = Redis.from_url(settings.redis_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="advancedRAG", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = redis
    app.include_router(health.router, prefix="/api")
    return app
