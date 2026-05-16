"""initial environment match schema

Revision ID: 20260511_0001
Revises:
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260511_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("date_from", sa.String(length=10), nullable=True),
        sa.Column("date_to", sa.String(length=10), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("error_summary_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_collection_runs_date_from"), "collection_runs", ["date_from"], unique=False)
    op.create_index(op.f("ix_collection_runs_date_to"), "collection_runs", ["date_to"], unique=False)
    op.create_index(op.f("ix_collection_runs_source_type"), "collection_runs", ["source_type"], unique=False)
    op.create_index(op.f("ix_collection_runs_status"), "collection_runs", ["status"], unique=False)

    op.create_table(
        "follow_players",
        sa.Column("follow_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=255), nullable=True),
        sa.Column("daily_url", sa.String(length=500), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("follow_id"),
    )
    op.create_index(op.f("ix_follow_players_name"), "follow_players", ["name"], unique=False)

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=80), nullable=False),
        sa.Column("replay_id", sa.String(length=64), nullable=True),
        sa.Column("detail_t", sa.String(length=32), nullable=True),
        sa.Column("primary_follow_id", sa.String(length=32), nullable=True),
        sa.Column("played_at", sa.String(length=32), nullable=True),
        sa.Column("mode", sa.String(length=64), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("detail_url", sa.String(length=500), nullable=False),
        sa.Column("play_url", sa.String(length=500), nullable=True),
        sa.Column("m3u8_url", sa.String(length=500), nullable=True),
        sa.Column("id_state", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("last_collected_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["last_collected_run_id"], ["collection_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("replay_id"),
    )
    for column in ("detail_t", "id_state", "mode", "played_at", "primary_follow_id", "public_id", "replay_id", "result", "version"):
        op.create_index(op.f(f"ix_matches_{column}"), "matches", [column], unique=False)

    op.create_table(
        "match_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=80), nullable=False),
        sa.Column("alias_type", sa.String(length=24), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias", name="uq_match_alias"),
    )
    op.create_index(op.f("ix_match_aliases_alias"), "match_aliases", ["alias"], unique=False)
    op.create_index(op.f("ix_match_aliases_alias_type"), "match_aliases", ["alias_type"], unique=False)
    op.create_index(op.f("ix_match_aliases_match_id"), "match_aliases", ["match_id"], unique=False)

    op.create_table(
        "match_sides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("side_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("player_name", sa.String(length=255), nullable=True),
        sa.Column("follow_id", sa.String(length=32), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("castle_rate", sa.String(length=64), nullable=True),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("selected_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "side_index", name="uq_match_side"),
    )
    for column in ("follow_id", "match_id", "player_name", "result"):
        op.create_index(op.f(f"ix_match_sides_{column}"), "match_sides", [column], unique=False)

    op.create_table(
        "match_decks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("side_index", sa.Integer(), nullable=False),
        sa.Column("deck_fingerprint", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "side_index", name="uq_match_deck"),
    )
    op.create_index(op.f("ix_match_decks_deck_fingerprint"), "match_decks", ["deck_fingerprint"], unique=False)
    op.create_index(op.f("ix_match_decks_match_id"), "match_decks", ["match_id"], unique=False)

    op.create_table(
        "battle_summaries",
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("raw_title", sa.Text(), nullable=True),
        sa.Column("detail_error", sa.Text(), nullable=True),
        sa.Column("castle_breakdown_json", sa.JSON(), nullable=False),
        sa.Column("timeline_labels_json", sa.JSON(), nullable=False),
        sa.Column("timeline_data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("match_id"),
    )

    op.create_table(
        "replay_assets",
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("replay_id", sa.String(length=64), nullable=True),
        sa.Column("play_url", sa.String(length=500), nullable=True),
        sa.Column("m3u8_url", sa.String(length=500), nullable=True),
        sa.Column("download_status", sa.String(length=32), nullable=False),
        sa.Column("video_path", sa.String(length=500), nullable=True),
        sa.Column("frame_dir", sa.String(length=500), nullable=True),
        sa.Column("auth_state", sa.String(length=32), nullable=False),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("match_id"),
    )
    op.create_index(op.f("ix_replay_assets_auth_state"), "replay_assets", ["auth_state"], unique=False)
    op.create_index(op.f("ix_replay_assets_download_status"), "replay_assets", ["download_status"], unique=False)
    op.create_index(op.f("ix_replay_assets_replay_id"), "replay_assets", ["replay_id"], unique=False)

    op.create_table(
        "match_deck_units",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("card_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["deck_id"], ["match_decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deck_id", "slot", name="uq_match_deck_unit_slot"),
    )
    op.create_index(op.f("ix_match_deck_units_card_hash"), "match_deck_units", ["card_hash"], unique=False)
    op.create_index(op.f("ix_match_deck_units_deck_id"), "match_deck_units", ["deck_id"], unique=False)

    op.create_table(
        "raw_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=True),
        sa.Column("collection_run_id", sa.Integer(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("local_path", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(["collection_run_id"], ["collection_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_raw_snapshots_collection_run_id"), "raw_snapshots", ["collection_run_id"], unique=False)
    op.create_index(op.f("ix_raw_snapshots_content_hash"), "raw_snapshots", ["content_hash"], unique=False)
    op.create_index(op.f("ix_raw_snapshots_match_id"), "raw_snapshots", ["match_id"], unique=False)
    op.create_index(op.f("ix_raw_snapshots_source_kind"), "raw_snapshots", ["source_kind"], unique=False)


def downgrade() -> None:
    op.drop_table("raw_snapshots")
    op.drop_table("match_deck_units")
    op.drop_table("replay_assets")
    op.drop_table("battle_summaries")
    op.drop_table("match_decks")
    op.drop_table("match_sides")
    op.drop_table("match_aliases")
    op.drop_table("matches")
    op.drop_table("follow_players")
    op.drop_table("collection_runs")
