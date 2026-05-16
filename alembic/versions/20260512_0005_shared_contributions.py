"""shared contribution packages

Revision ID: 20260512_0005
Revises: 20260512_0004
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260512_0005"
down_revision = "20260512_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shared_contribution_packages",
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("contributor_id", sa.String(length=128), nullable=False),
        sa.Column("target_version", sa.String(length=64), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=False),
        sa.Column("date_to", sa.String(length=10), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("imported_match_count", sa.Integer(), nullable=False),
        sa.Column("error_summary_json", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=False), nullable=False),
        sa.PrimaryKeyConstraint("package_id"),
        sa.UniqueConstraint("content_hash"),
    )
    for column in ("content_hash", "contributor_id", "date_from", "date_to", "status", "target_version"):
        op.create_index(op.f(f"ix_shared_contribution_packages_{column}"), "shared_contribution_packages", [column], unique=False)

    op.create_table(
        "shared_contribution_matches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=80), nullable=False),
        sa.Column("replay_id", sa.String(length=64), nullable=True),
        sa.Column("detail_t", sa.String(length=32), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["package_id"], ["shared_contribution_packages.package_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "match_id", name="uq_shared_contribution_match"),
    )
    for column in ("detail_t", "match_id", "package_id", "public_id", "replay_id"):
        op.create_index(op.f(f"ix_shared_contribution_matches_{column}"), "shared_contribution_matches", [column], unique=False)


def downgrade() -> None:
    op.drop_table("shared_contribution_matches")
    op.drop_table("shared_contribution_packages")
