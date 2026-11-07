"""The indexing profile of the running configuration and its HNSW index (REQ-013).

A profile is one row per configuration (embedding model, dimensions, parser, chunker parameters, BM25 configuration).
Its vectors are searched through a partial HNSW index created when the profile is: phase 3 searches must filter on
`profile_id` and order by the expression `embedding_expression(profile)`, the one the index is built on.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import Settings
from app.db.models import IndexingProfile

PARSER_ID = "docling-serve+text:v1"
BM25_CONFIG_ID = "conservative-v1"

# pgvector builds an HNSW index on at most 2000 dimensions of a `vector`.
MAX_DIMENSIONS = 2000
# Serialises the creation of the index of a profile between processes that start together.
_INDEX_LOCK_KEY = 7_340_321_001


class ProfileSetupError(Exception):
    """The profile of the current configuration could not be created. The message names the variables to check."""


def _checked_dimensions(dimensions: object) -> int:
    # The value is written into DDL, which cannot take parameters: it has to be a plain integer in range.
    if type(dimensions) is not int or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise ProfileSetupError(
            f"EMBEDDING_DIMENSIONS must be an integer from 1 to {MAX_DIMENSIONS} (pgvector indexes at most "
            f"{MAX_DIMENSIONS} dimensions); check EMBEDDING_DIMENSIONS and EMBEDDING_MODEL"
        )
    return dimensions


def embedding_expression(profile: IndexingProfile) -> str:
    """The SQL expression the HNSW index of the profile is built on, for the searches to use (`ORDER BY <it> <=> q`)."""
    return f"embedding::vector({_checked_dimensions(profile.dimensions)})"


def hnsw_index_name(profile_id: uuid.UUID) -> str:
    return f"chunks_embedding_hnsw_{profile_id.hex[:12]}"


async def _create_hnsw_index(engine: AsyncEngine, profile: IndexingProfile) -> None:
    """Cosine HNSW index with the pgvector default parameters, on the rows of this profile only. Autocommit DDL under
    an advisory lock, so that concurrent starts create it once."""
    if not isinstance(profile.id, uuid.UUID):
        raise ProfileSetupError("the profile has no valid id")
    ddl = (
        f"CREATE INDEX IF NOT EXISTS {hnsw_index_name(profile.id)} ON chunks"
        f" USING hnsw (({embedding_expression(profile)}) vector_cosine_ops)"
        f" WHERE profile_id = '{profile.id}'"
    )
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("select pg_advisory_lock(:key)"), {"key": _INDEX_LOCK_KEY})
        try:
            await connection.execute(text(ddl))
        finally:
            await connection.execute(text("select pg_advisory_unlock(:key)"), {"key": _INDEX_LOCK_KEY})


async def ensure_profile(engine: AsyncEngine, settings: Settings) -> IndexingProfile:
    """The profile of the settings: created if no profile has the same configuration, with its HNSW index. Safe when
    several processes call it together. Raises ProfileSetupError, naming the variables to check, when it cannot."""
    dimensions = _checked_dimensions(settings.embedding_dimensions)
    configuration = {
        "embedding_model": settings.embedding_model,
        "dimensions": dimensions,
        "parser": PARSER_ID,
        "chunker": asdict(settings.chunker_config),
        "bm25_config": BM25_CONFIG_ID,
    }
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(insert(IndexingProfile).values(**configuration).on_conflict_do_nothing())
            profile = await session.scalar(
                select(IndexingProfile).where(
                    *(getattr(IndexingProfile, key) == value for key, value in configuration.items())
                )
            )
            await session.commit()
        if profile is None:
            raise ProfileSetupError("the indexing profile was not found after it was created")
        await _create_hnsw_index(engine, profile)
    except (SQLAlchemyError, OSError) as error:
        raise ProfileSetupError(
            f"cannot create the indexing profile ({type(error).__name__}); check EMBEDDING_DIMENSIONS and "
            "EMBEDDING_MODEL"
        ) from error
    return profile
