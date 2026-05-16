"""analysis sample and version scopes

Revision ID: 20260512_0004
Revises: 20260511_0003
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260512_0004"
down_revision = "20260511_0003"
branch_labels = None
depends_on = None


STAT_TABLES = [
    ("analysis_deck_stats", "deck_fingerprint", "uq_analysis_deck_stat"),
    ("analysis_card_stats", "card_hash", "uq_analysis_card_stat"),
]


def upgrade() -> None:
    for table_name, identity_column, unique_name in STAT_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("sample_scope", sa.String(length=32), nullable=False, server_default="all_players"))
            batch_op.add_column(sa.Column("version_scope", sa.String(length=64), nullable=False, server_default="all_versions"))
            batch_op.drop_constraint(unique_name, type_="unique")
            batch_op.create_unique_constraint(
                unique_name,
                ["analysis_run_id", "sample_scope", "version_scope", identity_column],
            )
            batch_op.create_index(op.f(f"ix_{table_name}_sample_scope"), ["sample_scope"], unique=False)
            batch_op.create_index(op.f(f"ix_{table_name}_version_scope"), ["version_scope"], unique=False)


def downgrade() -> None:
    for table_name, identity_column, unique_name in STAT_TABLES:
        # 降级回旧唯一键前，只保留旧口径，避免同一分析批次内多 scope/version 行冲突。
        op.execute(
            sa.text(
                f"DELETE FROM {table_name} "
                "WHERE sample_scope != 'all_players' OR version_scope != 'all_versions'"
            )
        )
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table_name}_version_scope"))
            batch_op.drop_index(op.f(f"ix_{table_name}_sample_scope"))
            batch_op.drop_constraint(unique_name, type_="unique")
            batch_op.create_unique_constraint(unique_name, ["analysis_run_id", identity_column])
            batch_op.drop_column("version_scope")
            batch_op.drop_column("sample_scope")
