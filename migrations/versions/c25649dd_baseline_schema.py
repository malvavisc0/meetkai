"""baseline schema

Revision ID: c25649dd
Revises:
Create Date: 2026-07-24 20:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c25649dd"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("is_disabled", sa.Boolean(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("hmac_key", sa.String(), nullable=False),
        sa.Column("feature_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("kai_slug", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("kai_slug", name="uq_users_kai_slug"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("bot_type", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("desired_state", sa.String(), nullable=False),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("template", sa.String(), nullable=False),
        sa.Column("tool_overrides", sa.JSON(), nullable=False),
        sa.Column("feature_flags", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("brain_mandatory", sa.Boolean(), nullable=True),
        sa.Column("brain_instruction", sa.String(), nullable=True),
        sa.Column("needs_restart", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_deployments_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
        sa.UniqueConstraint("user_id", "bot_type", name="uq_deployments_user_id_bot_type"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("service", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_connections_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_connections"),
        sa.UniqueConstraint("user_id", "service", name="uq_connections_user_id_service"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "login_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("fulfilled_at", sa.String(), nullable=True),
        sa.Column("token_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_login_requests_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_login_requests"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "login_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("consumed_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_login_tokens_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_login_tokens"),
        sa.UniqueConstraint("token", name="uq_login_tokens_token"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("login_tokens")
    op.drop_table("login_requests")
    op.drop_table("connections")
    op.drop_table("deployments")
    op.drop_table("users")
