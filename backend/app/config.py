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
    # The ranges are checked by `_check_profile_settings`, so that the error names the environment variable.
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = 1024
    # The embedding texts go to Ollama in groups of this size, each request with this timeout (REQ-012).
    embedding_batch_size: int = Field(default=16, ge=1)
    embedding_timeout_seconds: float = Field(default=120, gt=0)
    # Docling (REQ-008): the key is sent as X-Api-Key when set; one conversion waits at most this long.
    docling_api_key: SecretStr | None = None
    docling_timeout_seconds: float = Field(default=600, gt=0)
    # Ask Docling to extract the images of the document (form field include_images); off: only text is wanted.
    docling_include_images: bool = False
    # Temporary errors of Docling or Ollama repeat the step up to this many attempts in all, waiting this long (then
    # double) between them (REQ-016).
    ingestion_max_attempts: int = Field(default=3, ge=1)
    ingestion_retry_backoff_seconds: float = Field(default=10, ge=0)
    # A document with fewer non-white characters than this is UNSUPPORTED (REQ-009).
    min_extracted_chars: int = Field(default=50, ge=0)
    # The original of a FAILED document is kept this many days after it failed, for a retry (REQ-017); only the worker
    # reads it.
    failed_original_retention_days: int = Field(default=7, ge=1)
    chunk_target_words: int = 300
    chunk_min_words: int = 50
    chunk_max_words: int = 450
    chunk_overlap_percent: int = 15

    @model_validator(mode="after")
    def _check_profile_settings(self) -> Self:
        problems: list[str] = []
        if not self.embedding_model.strip():
            problems.append("EMBEDDING_MODEL must not be empty")
        if not 1 <= self.embedding_dimensions <= 2000:
            problems.append(f"EMBEDDING_DIMENSIONS must be from 1 to 2000, got {self.embedding_dimensions}")
        if self.chunk_target_words < 1:
            problems.append(f"CHUNK_TARGET_WORDS must be at least 1, got {self.chunk_target_words}")
        if self.chunk_min_words < 0:
            problems.append(f"CHUNK_MIN_WORDS must be at least 0, got {self.chunk_min_words}")
        if not 0 <= self.chunk_overlap_percent < 100:
            problems.append(f"CHUNK_OVERLAP_PERCENT must be from 0 to 99, got {self.chunk_overlap_percent}")
        if not problems and not self.chunk_min_words <= self.chunk_target_words <= self.chunk_max_words:
            problems.append(
                "CHUNK_MIN_WORDS <= CHUNK_TARGET_WORDS <= CHUNK_MAX_WORDS is required, got "
                f"{self.chunk_min_words}, {self.chunk_target_words}, {self.chunk_max_words}"
            )
        if problems:
            raise ValueError("; ".join(problems))
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
