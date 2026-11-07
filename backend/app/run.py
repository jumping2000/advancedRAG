"""Container entry point: apply the migrations, create the first admin and the indexing profile, start uvicorn."""

import asyncio
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.bootstrap import MissingAdminConfig, ensure_admin
from app.config import Settings, get_settings
from app.db.session import create_engine
from app.ingestion.profile import ProfileSetupError, ensure_profile

BACKEND_DIR = Path(__file__).resolve().parents[1]


def migrate(database_url: URL) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    # Passed as an object, not through the ini file: a password with "%" would break configparser interpolation.
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


async def bootstrap(settings: Settings) -> None:
    engine = create_engine(settings.sqlalchemy_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await ensure_admin(session, settings)
        await ensure_profile(engine, settings)
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    migrate(settings.sqlalchemy_url)
    try:
        asyncio.run(bootstrap(settings))
    except MissingAdminConfig as error:
        # Exit code 1 and the message (with the variable name) on stderr; uvicorn is not started (REQ-004).
        raise SystemExit(f"Startup stopped: {error}") from None
    except ProfileSetupError as error:
        # Same for the indexing profile (REQ-013): the message names EMBEDDING_DIMENSIONS and EMBEDDING_MODEL and
        # carries the cause (which can also be a database problem); the error is chained, not hidden.
        raise SystemExit(f"Startup stopped: {error}") from error
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        # The backend port is not published: only containers of the Compose network reach it, and Nginx always
        # overwrites X-Forwarded-Proto, so the header seen here comes from Nginx.
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
