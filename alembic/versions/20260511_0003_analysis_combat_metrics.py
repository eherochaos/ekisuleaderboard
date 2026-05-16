"""analysis combat and castle metrics

Revision ID: 20260511_0003
Revises: 20260511_0002
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260511_0003"
down_revision = "20260511_0002"
branch_labels = None
depends_on = None


METRIC_COLUMN_NAMES = [
    "avg_castle_damage_dealt",
    "avg_castle_damage_taken",
    "avg_kill_count",
    "avg_death_count",
]


def _metric_columns() -> list[sa.Column]:
    return [sa.Column(name, sa.Float(), nullable=True) for name in METRIC_COLUMN_NAMES]


def upgrade() -> None:
    for table_name in ("analysis_deck_stats", "analysis_card_stats"):
        with op.batch_alter_table(table_name) as batch_op:
            for column in _metric_columns():
                batch_op.add_column(column)


def downgrade() -> None:
    for table_name in ("analysis_card_stats", "analysis_deck_stats"):
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in reversed(METRIC_COLUMN_NAMES):
                batch_op.drop_column(column_name)
