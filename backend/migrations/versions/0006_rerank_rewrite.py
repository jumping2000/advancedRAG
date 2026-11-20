"""The switches of the reranker and of the question rewriting, and the question searched by an answer
(SPEC-2026-11-20-fase-4-rerank-rewrite, T-01).

`app_settings` holds one row only (`id = 1`, CHECK): the global switches `rerank_enabled` and `rewrite_enabled`, both
off; the migration inserts the row, so the switches exist before any admin saves them. `messages.search_query` is the
question searched for an answer when the rewriting changed it; it is NULL for the answers saved before this phase, which
are not changed. Rollback: the downgrade drops the column and the table.

Revision ID: 0006_rerank_rewrite
Revises: 0005_conversations
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_rerank_rewrite"
down_revision: str | None = "0005_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True, autoincrement=False),
        sa.Column("rerank_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rewrite_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id = 1", name="app_settings_single_row_check"),
    )
    op.execute("insert into app_settings (id) values (1)")
    op.add_column("messages", sa.Column("search_query", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "search_query")
    op.drop_table("app_settings")
