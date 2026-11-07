"""The indexing profile of the running configuration and its HNSW index (REQ-013). Skeleton of T-03."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.models import IndexingProfile

PARSER_ID = "docling-serve+text:v1"
BM25_CONFIG_ID = "conservative-v1"


class ProfileSetupError(Exception):
    """The profile of the current configuration could not be created."""


def embedding_expression(profile: IndexingProfile) -> str:
    raise NotImplementedError


async def ensure_profile(engine: AsyncEngine, settings: Settings) -> IndexingProfile:
    raise NotImplementedError
