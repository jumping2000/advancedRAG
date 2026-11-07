"""Application settings read from the environment (.env)."""

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from app.ingestion.chunker import ChunkerConfig


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
    # Uploaded originals live here until the document is processed (compose.yaml fixes it to the `uploads` volume).
    upload_dir: Path = Path("/data/uploads")
    # Largest accepted file, in MiB (1 MiB = 1048576 bytes, the unit of Nginx's `m`).
    max_upload_size_mb: int = Field(default=20, gt=0)
    # Indexing profile (REQ-013): the embedding model and the size of its vectors, and the chunker parameters (REQ-011).
    # pgvector indexes at most 2000 dimensions of a `vector`.
    embedding_model: str = Field(default="bge-m3", min_length=1)
    embedding_dimensions: int = Field(default=1024, ge=1, le=2000)
    chunk_target_words: int = Field(default=300, ge=1)
    chunk_min_words: int = Field(default=50, ge=0)
    chunk_max_words: int = Field(default=450, ge=1)
    chunk_overlap_percent: int = Field(default=15, ge=0, lt=100)

    @model_validator(mode="after")
    def _chunk_sizes_are_consistent(self) -> Self:
        if not self.chunk_min_words <= self.chunk_target_words <= self.chunk_max_words:
            raise ValueError(
                "CHUNK_MIN_WORDS <= CHUNK_TARGET_WORDS <= CHUNK_MAX_WORDS is required, got "
                f"{self.chunk_min_words}, {self.chunk_target_words}, {self.chunk_max_words}"
            )
        return self

    @property
    def chunker_config(self) -> ChunkerConfig:
        return ChunkerConfig(
            target_words=self.chunk_target_words,
            min_words=self.chunk_min_words,
            max_words=self.chunk_max_words,
            overlap_percent=self.chunk_overlap_percent,
        )

    @property
    def sqlalchemy_url(self) -> URL:
        url = make_url(self.database_url)
        if self.postgres_password:
            url = url.set(password=self.postgres_password)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
