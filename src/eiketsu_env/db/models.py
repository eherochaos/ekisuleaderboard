"""定义采集、对局、卡组、原始快照和分析结果的数据库模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eiketsu_env.utils import utc_now

from .base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CollectionRun(Base):
    """一次采集任务的总览，便于断点排查和统计。"""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    date_from: Mapped[str | None] = mapped_column(String(10), index=True)
    date_to: Mapped[str | None] = mapped_column(String(10), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    counts_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    raw_snapshots: Mapped[list["RawSnapshot"]] = relationship(back_populates="collection_run")


class FollowPlayer(TimestampMixin, Base):
    """关注列表中的主君。"""

    __tablename__ = "follow_players"

    follow_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    state: Mapped[str | None] = mapped_column(String(255))
    daily_url: Mapped[str] = mapped_column(String(500), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class Match(TimestampMixin, Base):
    """对局主表；public_id 会在发现 replay_id 后升级为 r: 前缀。"""

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    replay_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    detail_t: Mapped[str | None] = mapped_column(String(32), index=True)
    primary_follow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    played_at: Mapped[str | None] = mapped_column(String(32), index=True)
    mode: Mapped[str | None] = mapped_column(String(64), index=True)
    version: Mapped[str | None] = mapped_column(String(64), index=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", index=True)
    detail_url: Mapped[str] = mapped_column(String(500), nullable=False)
    play_url: Mapped[str | None] = mapped_column(String(500))
    m3u8_url: Mapped[str | None] = mapped_column(String(500))
    id_state: Mapped[str] = mapped_column(String(32), nullable=False, default="detail_only", index=True)
    source_url: Mapped[str | None] = mapped_column(String(500))
    last_collected_run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_runs.id", ondelete="SET NULL"))

    aliases: Mapped[list["MatchAlias"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        order_by="MatchAlias.alias",
    )
    sides: Mapped[list["MatchSide"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        order_by="MatchSide.side_index",
    )
    decks: Mapped[list["MatchDeck"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        order_by="MatchDeck.side_index",
    )
    battle_summary: Mapped["BattleSummary | None"] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        uselist=False,
    )
    replay_asset: Mapped["ReplayAsset | None"] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        uselist=False,
    )
    raw_snapshots: Mapped[list["RawSnapshot"]] = relationship(back_populates="match")
    shared_package_links: Mapped[list["SharedContributionMatch"]] = relationship(back_populates="match")


class MatchAlias(Base):
    """一个对局可能先以详情页 ID 出现，之后再补到 replay ID。"""

    __tablename__ = "match_aliases"
    __table_args__ = (UniqueConstraint("alias", name="uq_match_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    alias_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    match: Mapped[Match] = relationship(back_populates="aliases")


class MatchSide(TimestampMixin, Base):
    __tablename__ = "match_sides"
    __table_args__ = (UniqueConstraint("match_id", "side_index", name="uq_match_side"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    side_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    player_name: Mapped[str | None] = mapped_column(String(255), index=True)
    follow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", index=True)
    castle_rate: Mapped[str | None] = mapped_column(String(64))
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    selected_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    match: Mapped[Match] = relationship(back_populates="sides")


class MatchDeck(TimestampMixin, Base):
    __tablename__ = "match_decks"
    __table_args__ = (UniqueConstraint("match_id", "side_index", name="uq_match_deck"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    side_index: Mapped[int] = mapped_column(Integer, nullable=False)
    deck_fingerprint: Mapped[str] = mapped_column(String(500), nullable=False, index=True)

    match: Mapped[Match] = relationship(back_populates="decks")
    units: Mapped[list["MatchDeckUnit"]] = relationship(
        back_populates="deck",
        cascade="all, delete-orphan",
        order_by="MatchDeckUnit.slot",
    )


class MatchDeckUnit(Base):
    __tablename__ = "match_deck_units"
    __table_args__ = (UniqueConstraint("deck_id", "slot", name="uq_match_deck_unit_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("match_decks.id", ondelete="CASCADE"), nullable=False, index=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    card_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    deck: Mapped[MatchDeck] = relationship(back_populates="units")


class BattleSummary(TimestampMixin, Base):
    __tablename__ = "battle_summaries"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), primary_key=True)
    raw_title: Mapped[str | None] = mapped_column(Text)
    detail_error: Mapped[str | None] = mapped_column(Text)
    castle_breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    timeline_labels_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    timeline_data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    match: Mapped[Match] = relationship(back_populates="battle_summary")


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id", ondelete="SET NULL"), index=True)
    collection_run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_runs.id", ondelete="SET NULL"), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    local_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)

    match: Mapped[Match | None] = relationship(back_populates="raw_snapshots")
    collection_run: Mapped[CollectionRun | None] = relationship(back_populates="raw_snapshots")


class ReplayAsset(TimestampMixin, Base):
    __tablename__ = "replay_assets"

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), primary_key=True)
    replay_id: Mapped[str | None] = mapped_column(String(64), index=True)
    play_url: Mapped[str | None] = mapped_column(String(500))
    m3u8_url: Mapped[str | None] = mapped_column(String(500))
    download_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_requested", index=True)
    video_path: Mapped[str | None] = mapped_column(String(500))
    frame_dir: Mapped[str | None] = mapped_column(String(500))
    auth_state: Mapped[str] = mapped_column(String(32), nullable=False, default="not_checked", index=True)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    match: Mapped[Match] = relationship(back_populates="replay_asset")


class AnalysisRun(Base):
    """一次分析快照；重复 refresh 会新增批次，导出默认读取最新成功批次。"""

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    mode_scope_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    thresholds_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    counts_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    deck_stats: Mapped[list["AnalysisDeckStat"]] = relationship(
        back_populates="analysis_run",
        cascade="all, delete-orphan",
    )
    card_stats: Mapped[list["AnalysisCardStat"]] = relationship(
        back_populates="analysis_run",
        cascade="all, delete-orphan",
    )


class AnalysisDeckStat(Base):
    __tablename__ = "analysis_deck_stats"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id",
            "sample_scope",
            "version_scope",
            "deck_fingerprint",
            name="uq_analysis_deck_stat",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sample_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="all_players", index=True)
    version_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="all_versions", index=True)
    deck_fingerprint: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False)
    draw_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_rate: Mapped[float | None] = mapped_column(Float)
    avg_castle_diff: Mapped[float | None] = mapped_column(Float)
    avg_own_castle_rate: Mapped[float | None] = mapped_column(Float)
    avg_castle_damage_dealt: Mapped[float | None] = mapped_column(Float)
    avg_castle_damage_taken: Mapped[float | None] = mapped_column(Float)
    avg_kill_count: Mapped[float | None] = mapped_column(Float)
    avg_death_count: Mapped[float | None] = mapped_column(Float)
    castle_crash_count: Mapped[int] = mapped_column(Integer, nullable=False)
    castle_crashed_count: Mapped[int] = mapped_column(Integer, nullable=False)

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="deck_stats")


class AnalysisCardStat(Base):
    __tablename__ = "analysis_card_stats"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id",
            "sample_scope",
            "version_scope",
            "card_hash",
            name="uq_analysis_card_stat",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sample_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="all_players", index=True)
    version_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="all_versions", index=True)
    card_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False)
    draw_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_rate: Mapped[float | None] = mapped_column(Float)
    avg_castle_diff: Mapped[float | None] = mapped_column(Float)
    avg_own_castle_rate: Mapped[float | None] = mapped_column(Float)
    avg_castle_damage_dealt: Mapped[float | None] = mapped_column(Float)
    avg_castle_damage_taken: Mapped[float | None] = mapped_column(Float)
    avg_kill_count: Mapped[float | None] = mapped_column(Float)
    avg_death_count: Mapped[float | None] = mapped_column(Float)
    high_win_deck_count: Mapped[int] = mapped_column(Integer, nullable=False)

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="card_stats")


class SharedContributionPackage(Base):
    """记录已导入的共享贡献包，用 content_hash 保证重复导入不会放大样本。"""

    __tablename__ = "shared_contribution_packages"

    package_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    contributor_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)

    matches: Mapped[list["SharedContributionMatch"]] = relationship(
        back_populates="package",
        cascade="all, delete-orphan",
    )


class SharedContributionMatch(Base):
    """贡献包与本地规范化 match 的关联，便于追踪来源但不参与样本去重。"""

    __tablename__ = "shared_contribution_matches"
    __table_args__ = (
        UniqueConstraint("package_id", "match_id", name="uq_shared_contribution_match"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("shared_contribution_packages.package_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    public_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    replay_id: Mapped[str | None] = mapped_column(String(64), index=True)
    detail_t: Mapped[str | None] = mapped_column(String(32), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)

    package: Mapped[SharedContributionPackage] = relationship(back_populates="matches")
    match: Mapped[Match] = relationship(back_populates="shared_package_links")


class ServerShareConfig(TimestampMixin, Base):
    """VPS 下发给客户端的采集范围；用单行配置避免朋友手动输入版本号。"""

    __tablename__ = "server_share_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="share_v1")
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    include_solo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_ranker_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    report_formats_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reports_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ServerLeaderboardSnapshot(TimestampMixin, Base):
    """排行榜预计算快照；页面筛选优先读这里，避免每次请求都重跑聚合。"""

    __tablename__ = "server_leaderboard_snapshots"

    snapshot_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    rank_scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cluster_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    limit_value: Mapped[int | None] = mapped_column(Integer)
    archetype_limit_value: Mapped[int | None] = mapped_column(Integer)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    upload_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)


class ServerLeaderboardRun(TimestampMixin, Base):
    """一次公开排行榜物化生成记录。"""

    __tablename__ = "server_leaderboard_runs"
    __table_args__ = (
        Index(
            "ix_server_leaderboard_runs_current",
            "scope",
            "status",
            "payload_version",
            "target_version",
            "date_from",
            "date_to",
            "include_solo",
            "upload_watermark",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="public", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="building", index=True)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    include_solo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upload_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    upload_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    side_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=utc_now)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    rows: Mapped[list["ServerLeaderboardRow"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class ServerLeaderboardRow(Base):
    """公开排行榜物化行；页面分页只读取这里。"""

    __tablename__ = "server_leaderboard_rows"
    __table_args__ = (
        UniqueConstraint("run_id", "row_type", "rank_scope", "cluster_enabled", "rank", name="uq_server_leaderboard_row_rank"),
        Index("ix_server_leaderboard_rows_rank", "run_id", "row_type", "rank_scope", "cluster_enabled", "rank"),
        Index(
            "ix_server_leaderboard_rows_wilson",
            "run_id",
            "row_type",
            "rank_scope",
            "cluster_enabled",
            "wilson_lower_bound",
            "sample_count",
        ),
        Index(
            "ix_server_leaderboard_rows_sample",
            "run_id",
            "row_type",
            "rank_scope",
            "cluster_enabled",
            "sample_count",
            "wilson_lower_bound",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("server_leaderboard_runs.id", ondelete="CASCADE"), nullable=False)
    row_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rank_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wilson_lower_bound: Mapped[float | None] = mapped_column(Float)
    row_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    run: Mapped[ServerLeaderboardRun] = relationship(back_populates="rows")


class ServerUser(TimestampMixin, Base):
    """上传用户；公开页面只使用 public_id，不展示 contributor_name。"""

    __tablename__ = "server_users"
    __table_args__ = (UniqueConstraint("public_id", name="uq_server_user_public_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    contributor_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    tokens: Mapped[list["ServerApiToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    uploads: Mapped[list["ServerUpload"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class ServerInvite(TimestampMixin, Base):
    """一次性邀请码；绑定后换发本机 API token。"""

    __tablename__ = "server_invites"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    used_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("server_users.id", ondelete="SET NULL"), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))


class ServerApiToken(TimestampMixin, Base):
    """只保存 token hash，避免数据库泄露后可直接冒用客户端身份。"""

    __tablename__ = "server_api_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_server_api_token_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("server_users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), index=True)

    user: Mapped[ServerUser] = relationship(back_populates="tokens")


class ServerUpload(TimestampMixin, Base):
    """一次客户端上传与共享贡献包的关联；用于 /me 展示和重复上传幂等。"""

    __tablename__ = "server_uploads"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_server_upload_user_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("server_users.id", ondelete="CASCADE"), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("shared_contribution_packages.package_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date_from: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    date_to: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received", index=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    user: Mapped[ServerUser] = relationship(back_populates="uploads")
