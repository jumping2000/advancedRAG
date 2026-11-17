"""Conversations, messages and the sources of the answers (SPEC-2026-11-17-fase-3-chat, T-03).

A conversation belongs to one user and one knowledge base and goes with the KB (ON DELETE CASCADE, as its messages and
their sources). A message is a question (`user`, COMPLETED) or an answer (`assistant`): the answer is saved PENDING
before it is generated, then COMPLETED or FAILED. The partial unique index `messages_one_pending` lets one conversation
hold at most one PENDING answer, also between simultaneous requests (REQ-014). `message_sources.chunk_id` has no foreign
key to `chunks`: deleting a document must not write on the sources (ADR-003); the row keeps only metadata (document
name, section, pages, ranks, score), never the content of the chunk. Rollback: the downgrade drops the three tables.

Revision ID: 0005_conversations
Revises: 0004_chunks
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_conversations"
down_revision: str | None = "0004_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # The list of the conversations of a user in a KB, by last activity; it also serves the cascade from the KB
    op.create_index("conversations_kb_user_updated_idx", "conversations", ["kb_id", "user_id", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("notice", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role in ('user', 'assistant')", name="messages_role_check"),
        sa.CheckConstraint("status in ('PENDING', 'COMPLETED', 'FAILED')", name="messages_status_check"),
    )
    # The history of a conversation in order; it also serves the cascade from the conversation
    op.create_index("messages_conversation_created_idx", "messages", ["conversation_id", "created_at"])
    op.create_index(
        "messages_one_pending",
        "messages",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "message_sources",
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        # No foreign key to chunks (ADR-003): the chunk may be gone, the excerpt is then "no longer available"
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_name", sa.Text(), nullable=False),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("dense_rank", sa.Integer(), nullable=True),
        sa.Column("bm25_rank", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("cited", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("message_id", "number", name="message_sources_pkey"),
    )


def downgrade() -> None:
    op.drop_table("message_sources")
    op.drop_table("messages")
    op.drop_table("conversations")
