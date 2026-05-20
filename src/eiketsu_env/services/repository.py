"""封装采集数据入库逻辑，负责对局、卡组和原始快照的合并保存。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.models import (
    BattleSummary,
    CollectionRun,
    FollowPlayer,
    Match,
    MatchAlias,
    MatchDeck,
    MatchDeckUnit,
    MatchSide,
    RawSnapshot,
    ReplayAsset,
)
from eiketsu_env.utils import (
    PARSER_VERSION,
    deck_fingerprint,
    extract_detail_t,
    extract_follow_id,
    m3u8_url_for_replay,
    played_at_from_detail_t,
    public_id_for_match,
    sha256_text,
    utc_now,
)


class EnvRepository:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def start_run(self, source_type: str, date_from: str, date_to: str, scope: dict[str, Any]) -> CollectionRun:
        run = CollectionRun(
            source_type=source_type,
            status="running",
            date_from=date_from,
            date_to=date_to,
            scope_json=scope,
            counts_json={},
            error_summary_json=[],
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish_run(self, run: CollectionRun, status: str, counts: dict[str, Any], errors: list[dict[str, Any]]) -> None:
        run.status = status
        run.counts_json = counts
        run.error_summary_json = errors
        run.finished_at = utc_now()
        self.session.flush()

    def upsert_follow_player(self, player: dict[str, str]) -> FollowPlayer:
        follow_id = str(player["follow_id"])
        row = self.session.get(FollowPlayer, follow_id)
        if row is None:
            row = FollowPlayer(follow_id=follow_id, name=player.get("name") or f"f-{follow_id}", daily_url=player.get("daily_url") or "")
            self.session.add(row)
        row.name = player.get("name") or row.name
        row.state = player.get("state") or row.state
        row.daily_url = player.get("daily_url") or row.daily_url
        row.last_seen_at = utc_now()
        self.session.flush()
        return row

    def write_raw_snapshot(
        self,
        run: CollectionRun | None,
        source_kind: str,
        source_url: str,
        html: str,
        match: Match | None = None,
        date_hint: str | None = None,
    ) -> RawSnapshot:
        content_hash = sha256_text(html)
        safe_date = date_hint or utc_now().strftime("%Y-%m-%d")
        path = self.settings.raw_dir / safe_date / source_kind / f"{content_hash[:16]}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        snapshot = RawSnapshot(
            match=match,
            collection_run=run,
            source_kind=source_kind,
            source_url=source_url,
            local_path=str(path),
            content_hash=content_hash,
            parser_version=PARSER_VERSION,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def upsert_match_detail(self, detail: dict[str, Any], run: CollectionRun | None = None) -> Match:
        is_video_search = detail.get("source_type") == "video_search"
        replay_id = str(detail.get("replay_id") or "")
        detail_url = str(detail.get("detail_url") or detail.get("url") or "")
        follow_id = str(detail.get("follow_id") or extract_follow_id(detail_url) or "")
        detail_t = str(detail.get("detail_t") or extract_detail_t(detail_url) or "")
        public_id = public_id_for_match(replay_id, follow_id, detail_t)
        aliases = []
        if replay_id:
            aliases.append((f"r:{replay_id}", "replay"))
        if follow_id and detail_t:
            aliases.append((f"d:{follow_id}:{detail_t}", "detail"))

        match = self._find_match(public_id, aliases, replay_id)
        is_new_match = match is None
        if match is None:
            fallback_url = detail_url or detail.get("play_url") or detail.get("source_url") or ""
            match = Match(public_id=public_id, detail_url=fallback_url, result="unknown")
            self.session.add(match)
            self.session.flush()

        # 先以详情页落库，后来补到 replay_id 时升级 canonical public_id。
        match.public_id = public_id
        match.replay_id = replay_id or None
        match.detail_t = detail_t or match.detail_t
        match.primary_follow_id = follow_id or match.primary_follow_id
        incoming_played_at = played_at_from_detail_t(detail_t) or detail.get("date") or detail.get("played_at")
        if not is_video_search or not match.played_at:
            match.played_at = incoming_played_at or match.played_at
        match.mode = detail.get("mode") or match.mode
        match.version = detail.get("version") or match.version
        incoming_result = detail.get("result") or ""
        if incoming_result and incoming_result != "unknown":
            match.result = incoming_result
        elif not match.result:
            match.result = "unknown"
        if detail_url and (not is_video_search or is_new_match or "/members/history/detail" not in (match.detail_url or "")):
            match.detail_url = detail_url
        match.play_url = detail.get("play_url") or match.play_url
        match.m3u8_url = detail.get("m3u8_url") or (m3u8_url_for_replay(replay_id) if replay_id else match.m3u8_url)
        match.id_state = "replay" if replay_id else "detail_only"
        if not is_video_search or not match.source_url:
            match.source_url = detail.get("source_url") or detail_url
        match.last_collected_run_id = run.id if run else match.last_collected_run_id
        self.session.flush()

        for alias, alias_type in aliases:
            self._ensure_alias(match, alias, alias_type)

        # 演武场搜索是轻量来源：同一 replay 已有详情页数据时，只补缺失项，避免擦掉城血/时间线等高价值字段。
        if not is_video_search or is_new_match or not match.sides:
            self._replace_sides(match, detail)
        if not is_video_search or is_new_match or not match.decks:
            self._replace_decks(match, detail)
        if not is_video_search or is_new_match or self._incoming_has_battle_detail(detail) or match.battle_summary is None:
            self._upsert_battle_summary(match, detail)
        self._upsert_replay_asset(match, detail)
        self.session.flush()
        return match

    def _find_match(self, public_id: str, aliases: list[tuple[str, str]], replay_id: str) -> Match | None:
        for alias, _alias_type in aliases:
            alias_row = self.session.scalar(select(MatchAlias).where(MatchAlias.alias == alias))
            if alias_row:
                return alias_row.match
        if replay_id:
            match = self.session.scalar(select(Match).where(Match.replay_id == replay_id))
            if match:
                return match
        return self.session.scalar(select(Match).where(Match.public_id == public_id))

    def _ensure_alias(self, match: Match, alias: str, alias_type: str) -> None:
        alias_row = self.session.scalar(select(MatchAlias).where(MatchAlias.alias == alias))
        if alias_row is None:
            self.session.add(MatchAlias(match=match, alias=alias, alias_type=alias_type))
        elif alias_row.match_id != match.id:
            alias_row.match = match

    def _replace_sides(self, match: Match, detail: dict[str, Any]) -> None:
        match.sides.clear()
        self.session.flush()
        for player in detail.get("players", []):
            profile = dict(player.get("profile") or player.get("profile_boxes") or {})
            if player.get("deck_totals"):
                profile["deck_totals"] = player.get("deck_totals")
            if player.get("battle_stats"):
                profile["battle_stats"] = player.get("battle_stats")
            match.sides.append(
                MatchSide(
                    side_index=int(player.get("side_index") or len(match.sides) + 1),
                    role=str(player.get("role") or "unknown"),
                    player_name=player.get("player_name") or player.get("name"),
                    follow_id=player.get("follow_id") or "",
                    result=player.get("result") or ("unknown" if player.get("role") != "player" else detail.get("result", "unknown")),
                    castle_rate=player.get("castle_rate") or "",
                    profile_json=profile,
                    selected_json=player.get("selected") or {},
                )
            )

    def _replace_decks(self, match: Match, detail: dict[str, Any]) -> None:
        match.decks.clear()
        self.session.flush()
        for player in detail.get("players", []):
            card_hashes = [str(item) for item in player.get("deck_ids", []) if item]
            deck = MatchDeck(
                side_index=int(player.get("side_index") or len(match.decks) + 1),
                deck_fingerprint=deck_fingerprint(card_hashes),
            )
            deck.units = [MatchDeckUnit(slot=index, card_hash=card_hash) for index, card_hash in enumerate(card_hashes, start=1)]
            match.decks.append(deck)

    def _upsert_battle_summary(self, match: Match, detail: dict[str, Any]) -> None:
        if match.battle_summary is None:
            match.battle_summary = BattleSummary()
        match.battle_summary.raw_title = detail.get("title") or detail.get("raw_text")
        match.battle_summary.detail_error = detail.get("detail_error") or ""
        match.battle_summary.castle_breakdown_json = detail.get("castle_breakdown") or {}
        match.battle_summary.timeline_labels_json = detail.get("timeline_labels") or []
        match.battle_summary.timeline_data_json = detail.get("timeline_data") or {}

    def _upsert_replay_asset(self, match: Match, detail: dict[str, Any]) -> None:
        if match.replay_asset is None:
            match.replay_asset = ReplayAsset(download_status="not_requested", auth_state="not_checked", meta_json={})
        match.replay_asset.replay_id = detail.get("replay_id") or match.replay_asset.replay_id
        match.replay_asset.play_url = detail.get("play_url") or match.replay_asset.play_url
        match.replay_asset.m3u8_url = detail.get("m3u8_url") or match.replay_asset.m3u8_url

    def _incoming_has_battle_detail(self, detail: dict[str, Any]) -> bool:
        castle = detail.get("castle_breakdown") or {}
        timeline = detail.get("timeline_data") or {}
        return bool(castle.get("rows") or timeline)


def remove_path_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
