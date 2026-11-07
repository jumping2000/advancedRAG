"""Indexing profiles and chunks with their vectors (SPEC-2026-11-07-fase-2-ingestion, T-03).

`chunks.embedding` is a `vector` without a fixed dimension: the profile of a chunk fixes it, and the database refuses a
vector whose dimension differs (CHECK) or a `dimensions` that is not the profile's (composite foreign key). The HNSW
index is created per profile at runtime (`app.ingestion.profile.ensure_profile`), not here. The BM25 index
`chunks_bm25` is the profile configuration `conservative-v1`: no stemming, lowercase and ASCII folding, `kb_id` and
`document_id` as filter fields.

Revision ID: 0004_chunks
Revises: 0003_documents
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_chunks"
down_revision: str | None = "0003_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")

    op.create_table(
        "indexing_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("parser", sa.Text(), nullable=False),
        sa.Column("chunker", postgresql.JSONB(), nullable=False),
        sa.Column("bm25_config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("dimensions between 1 and 2000", name="indexing_profiles_dimensions_check"),
        # One profile per configuration; (id, dimensions) is the target of the foreign key of the chunks.
        sa.UniqueConstraint(
            "embedding_model",
            "dimensions",
            "parser",
            "chunker",
            "bm25_config",
            name="indexing_profiles_configuration_key",
        ),
        sa.UniqueConstraint("id", "dimensions", name="indexing_profiles_id_dimensions_key"),
    )

    op.add_column(
        "documents",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("indexing_profiles.id"), nullable=True),
    )
    # Target of the composite foreign key of the chunks.
    op.create_unique_constraint("documents_id_kb_id_key", "documents", ["id", "kb_id"])

    op.execute(
        """
        CREATE TABLE chunks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kb_id uuid NOT NULL,
            document_id uuid NOT NULL,
            profile_id uuid NOT NULL,
            dimensions integer NOT NULL,
            position integer NOT NULL,
            section_title text,
            page_start integer,
            page_end integer,
            content text NOT NULL,
            word_count integer NOT NULL,
            embedding vector NOT NULL,
            CONSTRAINT chunks_document_kb_fkey FOREIGN KEY (document_id, kb_id)
                REFERENCES documents (id, kb_id) ON DELETE CASCADE,
            CONSTRAINT chunks_profile_fkey FOREIGN KEY (profile_id, dimensions)
                REFERENCES indexing_profiles (id, dimensions),
            CONSTRAINT chunks_position_check CHECK (position >= 0),
            CONSTRAINT chunks_word_count_check CHECK (word_count >= 0),
            CONSTRAINT chunks_embedding_dimensions_check CHECK (vector_dims(embedding) = dimensions),
            CONSTRAINT chunks_document_position_key UNIQUE (document_id, position)
        )
        """
    )
    op.execute("CREATE INDEX chunks_kb_id_idx ON chunks (kb_id)")
    # Lexical index (pg_search): the key field and the two filter fields are indexed with the text fields.
    op.execute(
        """
        CREATE INDEX chunks_bm25 ON chunks USING bm25 (
            id,
            kb_id,
            document_id,
            (content::pdb.simple('lowercase=true', 'ascii_folding=true')),
            (section_title::pdb.simple('lowercase=true', 'ascii_folding=true'))
        ) WITH (key_field = 'id')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX chunks_bm25")
    op.execute("DROP TABLE chunks")  # with its other indexes
    op.drop_constraint("documents_id_kb_id_key", "documents", type_="unique")
    op.drop_column("documents", "profile_id")
    op.drop_table("indexing_profiles")
    # The extensions stay: they are created idempotently and other objects may use them.
