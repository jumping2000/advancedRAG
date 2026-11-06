"""Users table (SPEC-2026-11-06-fase-1-fondazioni, T-03).

Revision ID: 0001_users
Revises:
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_users"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("session_epoch", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role in ('admin', 'user')", name="users_role_check"),
        sa.CheckConstraint("language is null or language in ('it', 'en')", name="users_language_check"),
    )
    op.create_index("users_username_lower_key", "users", [sa.text("lower(username)")], unique=True)


def downgrade() -> None:
    op.drop_index("users_username_lower_key", table_name="users")
    op.drop_table("users")
