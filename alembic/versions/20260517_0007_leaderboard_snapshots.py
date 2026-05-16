"""leaderboard snapshot cache

Revision ID: 20260517_0007
Revises: 20260516_0006
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260517_0007"
down_revision = "20260516_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_leaderboard_snapshots",
        sa.Column("snapshot_key", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("rank_scope", sa.String(length=32), nullable=False),
        sa.Column("cluster_enabled", sa.Integer(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=True),
        sa.Column("archetype_limit_value", sa.Integer(), nullable=True),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("upload_watermark", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_key"),
    )
    for column in (
        "cluster_enabled",
        "date_from",
        "date_to",
        "rank_scope",
        "scope",
        "subject",
        "target_version",
        "upload_watermark",
    ):
        op.create_index(op.f(f"ix_server_leaderboard_snapshots_{column}"), "server_leaderboard_snapshots", [column], unique=False)


def downgrade() -> None:
    op.drop_table("server_leaderboard_snapshots")
