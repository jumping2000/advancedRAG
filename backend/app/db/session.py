"""Database engine and sessions (SQLAlchemy async, asyncpg)."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str | URL) -> AsyncEngine:
    # hide_parameters: database errors and statement logs never show the bound values (password hashes, REQ-006).
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
