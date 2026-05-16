"""vps upload server tables

Revision ID: 20260516_0006
Revises: 20260512_0005
Create Date: 2026-05-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260516_0006"
down_revision = "20260512_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_share_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("include_solo", sa.Integer(), nullable=False),
        sa.Column("high_ranker_rank", sa.Integer(), nullable=False),
        sa.Column("report_formats_json", sa.JSON(), nullable=False),
        sa.Column("reports_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "server_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("contributor_name", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_server_user_public_id"),
    )
    op.create_index(op.f("ix_server_users_public_id"), "server_users", ["public_id"], unique=False)
    op.create_table(
        "server_invites",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("used_by_user_id", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["used_by_user_id"], ["server_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(op.f("ix_server_invites_status"), "server_invites", ["status"], unique=False)
    op.create_index(op.f("ix_server_invites_used_by_user_id"), "server_invites", ["used_by_user_id"], unique=False)
    op.create_table(
        "server_api_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["server_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_server_api_token_hash"),
    )
    for column in ("revoked_at", "token_hash", "user_id"):
        op.create_index(op.f(f"ix_server_api_tokens_{column}"), "server_api_tokens", [column], unique=False)
    op.create_table(
        "server_uploads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("imported_match_count", sa.Integer(), nullable=False),
        sa.Column("error_summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["shared_contribution_packages.package_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["server_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_server_upload_user_hash"),
    )
    for column in ("content_hash", "date_from", "date_to", "package_id", "status", "target_version", "user_id"):
        op.create_index(op.f(f"ix_server_uploads_{column}"), "server_uploads", [column], unique=False)


def downgrade() -> None:
    op.drop_table("server_uploads")
    op.drop_table("server_api_tokens")
    op.drop_table("server_invites")
    op.drop_table("server_users")
    op.drop_table("server_share_config")
