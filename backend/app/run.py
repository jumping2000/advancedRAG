"""Container entry point: apply the migrations, then start uvicorn."""

from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from app.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def migrate(database_url: str) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def main() -> None:
    settings = get_settings()
    migrate(settings.database_url)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
