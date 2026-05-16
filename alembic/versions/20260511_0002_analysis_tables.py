"""analysis snapshot tables

Revision ID: 20260511_0002
Revises: 20260511_0001
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260511_0002"
down_revision = "20260511_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("mode_scope_json", sa.JSON(), nullable=False),
        sa.Column("thresholds_json", sa.JSON(), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("error_summary_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_analysis_runs_date_from"), "analysis_runs", ["date_from"], unique=False)
    op.create_index(op.f("ix_analysis_runs_date_to"), "analysis_runs", ["date_to"], unique=False)
    op.create_index(op.f("ix_analysis_runs_status"), "analysis_runs", ["status"], unique=False)

    op.create_table(
        "analysis_deck_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), nullable=False),
        sa.Column("deck_fingerprint", sa.String(length=500), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("win_count", sa.Integer(), nullable=False),
        sa.Column("loss_count", sa.Integer(), nullable=False),
        sa.Column("draw_count", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("avg_castle_diff", sa.Float(), nullable=True),
        sa.Column("avg_own_castle_rate", sa.Float(), nullable=True),
        sa.Column("castle_crash_count", sa.Integer(), nullable=False),
        sa.Column("castle_crashed_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", "deck_fingerprint", name="uq_analysis_deck_stat"),
    )
    op.create_index(op.f("ix_analysis_deck_stats_analysis_run_id"), "analysis_deck_stats", ["analysis_run_id"], unique=False)
    op.create_index(op.f("ix_analysis_deck_stats_deck_fingerprint"), "analysis_deck_stats", ["deck_fingerprint"], unique=False)

    op.create_table(
        "analysis_card_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), nullable=False),
        sa.Column("card_hash", sa.String(length=64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("win_count", sa.Integer(), nullable=False),
        sa.Column("loss_count", sa.Integer(), nullable=False),
        sa.Column("draw_count", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("avg_castle_diff", sa.Float(), nullable=True),
        sa.Column("avg_own_castle_rate", sa.Float(), nullable=True),
        sa.Column("high_win_deck_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", "card_hash", name="uq_analysis_card_stat"),
    )
    op.create_index(op.f("ix_analysis_card_stats_analysis_run_id"), "analysis_card_stats", ["analysis_run_id"], unique=False)
    op.create_index(op.f("ix_analysis_card_stats_card_hash"), "analysis_card_stats", ["card_hash"], unique=False)


def downgrade() -> None:
    op.drop_table("analysis_card_stats")
    op.drop_table("analysis_deck_stats")
    op.drop_table("analysis_runs")
