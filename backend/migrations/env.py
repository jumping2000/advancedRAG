"""Alembic environment: async migrations with the URL object given by app.run (config.attributes)."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    # Running the migrations inside the backend or the worker process must not silence the loggers created before.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(config.attributes["database_url"])
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_async_migrations())
