"""Documents uploaded to the knowledge bases (SPEC-2026-11-07-fase-2-ingestion, T-01).

The profile column is added with the indexing profiles by the next migration.

Revision ID: 0003_documents
Revises: 0002_knowledge_bases
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_documents"
down_revision: str | None = "0002_knowledge_bases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'QUEUED'")),
        sa.Column("status_reason_code", sa.Text(), nullable=True),
        sa.Column("status_reason_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status in ('QUEUED', 'PROCESSING', 'READY', 'FAILED', 'UNSUPPORTED')", name="documents_status_check"
        ),
        sa.CheckConstraint(
            "(status in ('FAILED', 'UNSUPPORTED')"
            " and coalesce(char_length(status_reason_code), 0) > 0"
            " and coalesce(char_length(status_reason_message), 0) > 0)"
            " or (status not in ('FAILED', 'UNSUPPORTED')"
            " and status_reason_code is null and status_reason_message is null)",
            name="documents_status_reason_check",
        ),
        sa.CheckConstraint("status <> 'READY' or chunk_count > 0", name="documents_ready_chunks_check"),
    )
    # The same content (SHA-256) is stored once per KB, in any status; it also serves the reads by kb_id.
    op.create_index("documents_kb_id_checksum_key", "documents", ["kb_id", "checksum"], unique=True)


def downgrade() -> None:
    op.drop_index("documents_kb_id_checksum_key", table_name="documents")
    op.drop_table("documents")
