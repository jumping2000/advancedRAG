"""Knowledge bases and their members (SPEC-2026-11-06-fase-1-fondazioni, T-06).

Revision ID: 0002_knowledge_bases
Revises: 0001_users
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_knowledge_bases"
down_revision: str | None = "0001_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("char_length(name) between 1 and 100", name="knowledge_bases_name_length_check"),
        sa.CheckConstraint(
            "description is null or char_length(description) <= 500", name="knowledge_bases_description_length_check"
        ),
    )
    op.create_index("knowledge_bases_name_lower_key", "knowledge_bases", [sa.text("lower(name)")], unique=True)
    op.create_table(
        "kb_members",
        sa.Column(
            "kb_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    # The primary key starts with kb_id: this index serves the per-user filter of every KB read.
    op.create_index("kb_members_user_id_idx", "kb_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("kb_members_user_id_idx", table_name="kb_members")
    op.drop_table("kb_members")
    op.drop_index("knowledge_bases_name_lower_key", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
