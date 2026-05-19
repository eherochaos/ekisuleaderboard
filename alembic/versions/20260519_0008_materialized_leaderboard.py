"""materialized leaderboard rows

Revision ID: 20260519_0008
Revises: 20260517_0007
Create Date: 2026-05-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260519_0008"
down_revision = "20260517_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_leaderboard_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("include_solo", sa.Integer(), nullable=False),
        sa.Column("upload_watermark", sa.Integer(), nullable=False),
        sa.Column("upload_count", sa.Integer(), nullable=False),
        sa.Column("package_count", sa.Integer(), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("side_sample_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("scope", "status", "payload_version", "target_version", "date_from", "date_to", "upload_watermark"):
        op.create_index(op.f(f"ix_server_leaderboard_runs_{column}"), "server_leaderboard_runs", [column], unique=False)
    op.create_index(
        "ix_server_leaderboard_runs_current",
        "server_leaderboard_runs",
        ["scope", "status", "payload_version", "target_version", "date_from", "date_to", "include_solo", "upload_watermark"],
        unique=False,
    )

    op.create_table(
        "server_leaderboard_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("row_type", sa.String(length=32), nullable=False),
        sa.Column("rank_scope", sa.String(length=32), nullable=False),
        sa.Column("cluster_enabled", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("wilson_lower_bound", sa.Float(), nullable=True),
        sa.Column("row_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["server_leaderboard_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "row_type", "rank_scope", "cluster_enabled", "rank", name="uq_server_leaderboard_row_rank"),
    )
    op.create_index(
        "ix_server_leaderboard_rows_rank",
        "server_leaderboard_rows",
        ["run_id", "row_type", "rank_scope", "cluster_enabled", "rank"],
        unique=False,
    )
    op.create_index(
        "ix_server_leaderboard_rows_wilson",
        "server_leaderboard_rows",
        ["run_id", "row_type", "rank_scope", "cluster_enabled", "wilson_lower_bound", "sample_count"],
        unique=False,
    )
    op.create_index(
        "ix_server_leaderboard_rows_sample",
        "server_leaderboard_rows",
        ["run_id", "row_type", "rank_scope", "cluster_enabled", "sample_count", "wilson_lower_bound"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("server_leaderboard_rows")
    op.drop_table("server_leaderboard_runs")
