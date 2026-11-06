"""Application settings read from the environment (.env)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    # The repository-root .env also works when the backend runs from backend/ (the later file wins).
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    database_url: str
    # When set, it replaces the password of DATABASE_URL: any character is allowed, it is never pasted into the URL.
    postgres_password: str | None = None
    redis_url: str
    ollama_base_url: str
    docling_base_url: str
    health_check_timeout_seconds: float = 2.0
    # First admin, created at startup only when no active admin exists (REQ-004); empty counts as missing.
    admin_username: str | None = None
    admin_password: SecretStr | None = None
    session_idle_timeout_minutes: int = Field(default=480, gt=0)

    @property
    def sqlalchemy_url(self) -> URL:
        url = make_url(self.database_url)
        if self.postgres_password:
            url = url.set(password=self.postgres_password)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
