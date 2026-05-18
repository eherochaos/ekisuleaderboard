"""排行榜查询、聚合、缓存与快照服务。"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from eiketsu_env.config import Settings
from eiketsu_env.db.models import (
    Match,
    MatchDeck,
    MatchSide,
    ServerLeaderboardSnapshot,
    ServerUpload,
    ServerUser,
    SharedContributionMatch,
    SharedContributionPackage,
)
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.card_lookup import load_card_lookup
from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.services.share import ShareConfig
from eiketsu_env.utils import sha256_text


DEFAULT_ARCHETYPE_SIMILAR_COST = 5.0
LEADERBOARD_CACHE_TTL_SECONDS = 300.0
RANK_SCOPE_ALL = "all"
RANK_SCOPE_TRAVELER_DOWN = "traveler_down"
RANK_SCOPE_KNIGHT_DOWN = "knight_down"
RANK_SCOPE_KNIGHT_UP = "knight_up"
RANK_SCOPE_LABELS = {
    RANK_SCOPE_ALL: "全部段位",
    RANK_SCOPE_TRAVELER_DOWN: "旅人以下",
    RANK_SCOPE_KNIGHT_DOWN: "騎士以下",
    RANK_SCOPE_KNIGHT_UP: "騎士以上",
}
RANK_SCOPE_ALIASES = {
    "": RANK_SCOPE_ALL,
    "all": RANK_SCOPE_ALL,
    "traveler": RANK_SCOPE_TRAVELER_DOWN,
    "traveler_down": RANK_SCOPE_TRAVELER_DOWN,
    "traveler_below": RANK_SCOPE_TRAVELER_DOWN,
    "traveler-below": RANK_SCOPE_TRAVELER_DOWN,
    "knight_down": RANK_SCOPE_KNIGHT_DOWN,
    "knight_below": RANK_SCOPE_KNIGHT_DOWN,
    "knight-below": RANK_SCOPE_KNIGHT_DOWN,
    "knight_up": RANK_SCOPE_KNIGHT_UP,
    "knight_above": RANK_SCOPE_KNIGHT_UP,
    "knight-above": RANK_SCOPE_KNIGHT_UP,
}
_LEADERBOARD_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
LEADERBOARD_PAYLOAD_VERSION = 2
BEHAVIOR_TOP_LIMIT = 3
BEHAVIOR_MIN_CONDITIONAL_SAMPLE = 20


@dataclass(slots=True)
class _LeaderboardSideSample:
    result: str
    played_at: str
    player_name: str
    weapon_name: str
    style_name: str


@dataclass(slots=True)
class _LeaderboardBucket:
    sample_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    draw_count: int = 0
    player_counts: Counter[str] = field(default_factory=Counter)
    samples: list[_LeaderboardSideSample] = field(default_factory=list)

    def add(self, result: str, side: MatchSide | None = None, played_at: str = "") -> None:
        self.sample_count += 1
        player_name = _bucket_player_name(side)
        if player_name:
            self.player_counts[player_name] += 1
        self.samples.append(
            _LeaderboardSideSample(
                result=result,
                played_at=str(played_at or ""),
                player_name=player_name,
                weapon_name=_selected_name(side, "weapon"),
                style_name=_selected_name(side, "school"),
            )
        )
        if result == "win":
            self.win_count += 1
        elif result == "loss":
            self.loss_count += 1
        elif result == "draw":
            self.draw_count += 1

    def merge(self, other: "_LeaderboardBucket") -> None:
        self.sample_count += other.sample_count
        self.win_count += other.win_count
        self.loss_count += other.loss_count
        self.draw_count += other.draw_count
        self.player_counts.update(other.player_counts)
        self.samples.extend(other.samples)

    @property
    def win_rate(self) -> float | None:
        denominator = self.win_count + self.loss_count
        return self.win_count / denominator if denominator else None

    @property
    def top_player(self) -> str:
        if not self.player_counts:
            return ""
        return sorted(self.player_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    @property
    def top_player_count(self) -> int:
        top_player = self.top_player
        return int(self.player_counts.get(top_player, 0)) if top_player else 0

    @property
    def player_count(self) -> int:
        return len(self.player_counts)


@dataclass(slots=True)
class _DeckArchetype:
    representative: str
    members: list[str]
    summary: _LeaderboardBucket
    core_hashes: list[str]


def _server_share_config(session) -> ShareConfig:
    # 服务端配置仍由 server_share 管理；排行榜只读取生效后的配置。
    from eiketsu_env.services.server_share import _effective_share_config, _require_config

    return _effective_share_config(_require_config(session))


def _authenticate_token(session, api_token: str):
    from eiketsu_env.services.server_share import authenticate_token

    return authenticate_token(session, api_token)


def _config_to_payload(config: ShareConfig) -> dict[str, Any]:
    from eiketsu_env.services.server_share import _config_to_payload as server_config_to_payload

    return server_config_to_payload(config)


def public_leaderboard(
    settings: Settings,
    limit: int | None = None,
    archetype_limit: int | None = None,
    rank_scope: str = RANK_SCOPE_ALL,
    include_archetypes: bool = True,
) -> dict[str, Any]:
    factory = make_session_factory(settings)
    with factory() as session:
        config = _server_share_config(session)
        upload_watermark = _leaderboard_upload_watermark(session)
        snapshot_key = _leaderboard_snapshot_key("public", "", config, upload_watermark, limit, archetype_limit, rank_scope, include_archetypes)
        cache_key = _leaderboard_cache_key(settings, snapshot_key)
        cached = _leaderboard_cache_get(cache_key)
        if cached is not None:
            return cached
        snapshot = _load_leaderboard_snapshot(session, snapshot_key)
        if snapshot is not None:
            return _leaderboard_cache_set(cache_key, snapshot)
        matches = _load_leaderboard_matches(session)
        scoped = _scope_leaderboard_matches(matches, config)
        upload_count = session.scalar(select(func.count(ServerUpload.id))) or 0
        package_count = session.scalar(select(func.count(SharedContributionPackage.package_id))) or 0
        payload = _leaderboard_payload(
            settings,
            config,
            scoped,
            upload_count=upload_count,
            package_count=package_count,
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
            scope="public",
            scope_label="公开匿名聚合",
        )
        _store_leaderboard_snapshot(
            session,
            snapshot_key,
            payload,
            scope="public",
            subject="",
            config=config,
            upload_watermark=upload_watermark,
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
        )
        session.commit()
        return _leaderboard_cache_set(cache_key, payload)


def personal_leaderboard(
    settings: Settings,
    api_token: str,
    limit: int | None = None,
    archetype_limit: int | None = None,
    rank_scope: str = RANK_SCOPE_ALL,
    include_archetypes: bool = True,
) -> dict[str, Any]:
    factory = make_session_factory(settings)
    with factory() as session:
        user = _authenticate_token(session, api_token)
        config = _server_share_config(session)
        upload_watermark = _leaderboard_upload_watermark(session)
        snapshot_key = _leaderboard_snapshot_key(
            "mine",
            user.public_id,
            config,
            upload_watermark,
            limit,
            archetype_limit,
            rank_scope,
            include_archetypes,
        )
        cache_key = _leaderboard_cache_key(settings, snapshot_key)
        cached = _leaderboard_cache_get(cache_key)
        if cached is not None:
            session.commit()
            return cached
        snapshot = _load_leaderboard_snapshot(session, snapshot_key)
        if snapshot is not None:
            session.commit()
            return _leaderboard_cache_set(cache_key, snapshot)
        uploads = session.scalars(select(ServerUpload).where(ServerUpload.user_id == user.id)).all()
        package_ids = {str(upload.package_id or "") for upload in uploads if upload.package_id}
        match_ids = set(
            session.scalars(
                select(SharedContributionMatch.match_id).where(SharedContributionMatch.package_id.in_(package_ids))
            ).all()
            if package_ids
            else []
        )
        matches = _load_leaderboard_matches(session, match_ids)
        scoped = _scope_leaderboard_matches(matches, config)
        payload = _leaderboard_payload(
            settings,
            config,
            scoped,
            upload_count=len(uploads),
            package_count=len(package_ids),
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
            scope="mine",
            scope_label=f"我的贡献：{user.contributor_name}",
        )
        payload["user_public_id"] = user.public_id
        payload["contributor_name"] = user.contributor_name
        _store_leaderboard_snapshot(
            session,
            snapshot_key,
            payload,
            scope="mine",
            subject=user.public_id,
            config=config,
            upload_watermark=upload_watermark,
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
        )
        session.commit()
        return _leaderboard_cache_set(cache_key, payload)


def contributor_leaderboard(
    settings: Settings,
    contributor_name: str,
    limit: int | None = None,
    archetype_limit: int | None = None,
    rank_scope: str = RANK_SCOPE_ALL,
    include_archetypes: bool = True,
) -> dict[str, Any]:
    contributor = contributor_name.strip()
    if not contributor:
        raise ValueError("请输入绑定用户名")
    factory = make_session_factory(settings)
    with factory() as session:
        config = _server_share_config(session)
        upload_watermark = _leaderboard_upload_watermark(session)
        snapshot_key = _leaderboard_snapshot_key(
            "contributor",
            contributor,
            config,
            upload_watermark,
            limit,
            archetype_limit,
            rank_scope,
            include_archetypes,
        )
        cache_key = _leaderboard_cache_key(settings, snapshot_key)
        cached = _leaderboard_cache_get(cache_key)
        if cached is not None:
            return cached
        snapshot = _load_leaderboard_snapshot(session, snapshot_key)
        if snapshot is not None:
            return _leaderboard_cache_set(cache_key, snapshot)
        users = session.scalars(select(ServerUser).where(ServerUser.contributor_name == contributor)).all()
        user_ids = {user.id for user in users}
        uploads = (
            session.scalars(select(ServerUpload).where(ServerUpload.user_id.in_(user_ids))).all()
            if user_ids
            else []
        )
        package_ids = {str(upload.package_id or "") for upload in uploads if upload.package_id}
        match_ids = set(
            session.scalars(
                select(SharedContributionMatch.match_id).where(SharedContributionMatch.package_id.in_(package_ids))
            ).all()
            if package_ids
            else []
        )
        matches = _load_leaderboard_matches(session, match_ids)
        scoped = _scope_leaderboard_matches(matches, config)
        payload = _leaderboard_payload(
            settings,
            config,
            scoped,
            upload_count=len(uploads),
            package_count=len(package_ids),
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
            scope="contributor",
            scope_label=f"用户贡献：{contributor}",
        )
        payload["contributor_name"] = contributor
        payload["user_count"] = len(users)
        payload["contributor_found"] = bool(users)
        _store_leaderboard_snapshot(
            session,
            snapshot_key,
            payload,
            scope="contributor",
            subject=contributor,
            config=config,
            upload_watermark=upload_watermark,
            limit=limit,
            archetype_limit=archetype_limit,
            rank_scope=rank_scope,
            include_archetypes=include_archetypes,
        )
        session.commit()
        return _leaderboard_cache_set(cache_key, payload)


def refresh_public_leaderboard_snapshots(settings: Settings) -> dict[str, Any]:
    """后台预热公共榜单筛选组合，避免用户点击时现场重算。"""

    refreshed: list[str] = []
    for rank_scope in RANK_SCOPE_LABELS:
        for include_archetypes in (True, False):
            try:
                public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=include_archetypes)
            except ValueError as exc:
                return {"status": "skipped", "reason": str(exc), "refreshed": refreshed}
            cluster_label = "cluster" if include_archetypes else "deck"
            refreshed.append(f"{rank_scope}:{cluster_label}")
    return {"status": "completed", "refreshed": refreshed}


def _leaderboard_snapshot_key(
    scope: str,
    subject: str,
    config: ShareConfig,
    upload_watermark: int,
    limit: int | None,
    archetype_limit: int | None,
    rank_scope: str,
    include_archetypes: bool,
) -> str:
    identity = {
        # 排行榜 payload 口径升级时必须变更版本，避免部署后继续命中旧快照。
        "payload_version": LEADERBOARD_PAYLOAD_VERSION,
        "scope": scope,
        "subject": subject,
        "target_version": config.target_version,
        "date_from": config.date_from,
        "date_to": config.date_to,
        "include_solo": bool(config.include_solo),
        "upload_watermark": upload_watermark,
        "limit": limit,
        "archetype_limit": archetype_limit,
        "rank_scope": _normalize_rank_scope(rank_scope),
        "cluster_enabled": bool(include_archetypes),
    }
    digest = sha256_text(json.dumps(identity, ensure_ascii=False, sort_keys=True))[:32]
    return f"lb:{scope}:{digest}"


def _leaderboard_cache_key(settings: Settings, snapshot_key: str) -> tuple[Any, ...]:
    return (str(settings.db_url), snapshot_key)


def _leaderboard_upload_watermark(session) -> int:
    return int(session.scalar(select(func.max(ServerUpload.id))) or 0)


def _load_leaderboard_snapshot(session, snapshot_key: str) -> dict[str, Any] | None:
    row = session.get(ServerLeaderboardSnapshot, snapshot_key)
    if row is None:
        return None
    return dict(row.payload_json or {})


def _store_leaderboard_snapshot(
    session,
    snapshot_key: str,
    payload: dict[str, Any],
    *,
    scope: str,
    subject: str,
    config: ShareConfig,
    upload_watermark: int,
    limit: int | None,
    archetype_limit: int | None,
    rank_scope: str,
    include_archetypes: bool,
) -> None:
    row = session.get(ServerLeaderboardSnapshot, snapshot_key)
    if row is None:
        row = ServerLeaderboardSnapshot(snapshot_key=snapshot_key)
        session.add(row)
    row.scope = scope
    row.subject = subject
    row.rank_scope = _normalize_rank_scope(rank_scope)
    row.cluster_enabled = 1 if include_archetypes else 0
    row.limit_value = limit
    row.archetype_limit_value = archetype_limit
    row.target_version = config.target_version
    row.date_from = config.date_from
    row.date_to = config.date_to
    row.upload_watermark = upload_watermark
    row.payload_json = payload
    row.generated_at = datetime.utcnow()


def _leaderboard_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    cached = _LEADERBOARD_CACHE.get(key)
    if cached is None:
        return None
    created_at, payload = cached
    if time.monotonic() - created_at > LEADERBOARD_CACHE_TTL_SECONDS:
        _LEADERBOARD_CACHE.pop(key, None)
        return None
    return payload


def _leaderboard_cache_set(key: tuple[Any, ...], payload: dict[str, Any]) -> dict[str, Any]:
    _LEADERBOARD_CACHE[key] = (time.monotonic(), payload)
    return payload


def _clear_leaderboard_cache() -> None:
    _LEADERBOARD_CACHE.clear()


def _clear_leaderboard_snapshots(settings: Settings) -> None:
    _clear_leaderboard_cache()
    factory = make_session_factory(settings)
    with factory() as session:
        session.execute(delete(ServerLeaderboardSnapshot))
        session.commit()


def _load_leaderboard_matches(session, match_ids: set[int] | None = None) -> list[Match]:
    if match_ids is not None and not match_ids:
        return []
    query = (
        select(Match)
        .options(
            selectinload(Match.sides),
            selectinload(Match.decks).selectinload(MatchDeck.units),
        )
        .order_by(Match.played_at, Match.id)
    )
    if match_ids is not None:
        query = query.where(Match.id.in_(match_ids))
    return list(session.scalars(query).all())


def _scope_leaderboard_matches(matches: list[Match], config: ShareConfig) -> list[Match]:
    return [match for match in matches if _match_in_server_scope(match, config)]


def _leaderboard_payload(
    settings: Settings,
    config: ShareConfig,
    scoped: list[Match],
    *,
    upload_count: int,
    package_count: int,
    limit: int | None,
    archetype_limit: int | None,
    rank_scope: str,
    include_archetypes: bool,
    scope: str,
    scope_label: str,
) -> dict[str, Any]:
    lookup = load_card_lookup(settings)
    seen_names = _card_names_from_matches(scoped)
    normalized_rank_scope = _normalize_rank_scope(rank_scope)
    deck_buckets: dict[str, _LeaderboardBucket] = {}
    card_buckets: dict[str, _LeaderboardBucket] = {}
    included_match_ids: set[int] = set()
    for match in scoped:
        sides_by_index = _sides_by_index(match)
        for deck in match.decks:
            if not deck.deck_fingerprint:
                continue
            side = sides_by_index.get(int(deck.side_index or 0))
            if not _side_matches_rank_scope(side, normalized_rank_scope):
                continue
            result = _result_for_side(match, deck.side_index)
            included_match_ids.add(int(match.id or 0))
            deck_buckets.setdefault(deck.deck_fingerprint, _LeaderboardBucket()).add(result, side, match.played_at or "")
            # 同一侧同一卡只计一次，避免异常重复 slot 放大卡牌使用率。
            for card_hash in {unit.card_hash for unit in deck.units if unit.card_hash}:
                card_buckets.setdefault(card_hash, _LeaderboardBucket()).add(result, side, match.played_at or "")
    return {
        **_config_to_payload(config),
        "payload_version": LEADERBOARD_PAYLOAD_VERSION,
        "scope": scope,
        "scope_label": scope_label,
        "rank_scope": normalized_rank_scope,
        "rank_scope_label": RANK_SCOPE_LABELS[normalized_rank_scope],
        "upload_count": upload_count,
        "package_count": package_count,
        "match_count": len(scoped) if normalized_rank_scope == RANK_SCOPE_ALL else len(included_match_ids),
        "side_sample_count": sum(bucket.sample_count for bucket in deck_buckets.values()),
        "top_decks": _top_decks(deck_buckets, lookup, seen_names, limit, config.date_to),
        "top_cards": _top_cards(card_buckets, lookup, seen_names, limit),
        "top_archetypes": (
            _top_archetypes(deck_buckets, lookup, seen_names, archetype_limit if archetype_limit is not None else limit, config.date_to)
            if include_archetypes
            else []
        ),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def _match_in_server_scope(match: Match, config: ShareConfig) -> bool:
    played_date = str(match.played_at or "")[:10]
    return (
        bool(played_date)
        and config.date_from <= played_date <= config.date_to
        and str(match.version or "") == config.target_version
        and is_environment_mode(match.mode or "", include_solo=config.include_solo)
    )


def _sides_by_index(match: Match) -> dict[int, MatchSide]:
    return {int(side.side_index or 0): side for side in match.sides}


def _bucket_player_name(side: MatchSide | None) -> str:
    return str(getattr(side, "player_name", "") or "").strip()


def _normalize_rank_scope(rank_scope: str) -> str:
    key = str(rank_scope or RANK_SCOPE_ALL).strip().lower().replace("-", "_")
    return RANK_SCOPE_ALIASES.get(key, RANK_SCOPE_ALL)


def _side_matches_rank_scope(side: MatchSide | None, rank_scope: str) -> bool:
    if rank_scope == RANK_SCOPE_ALL:
        return True
    order = _side_rank_order(side)
    if order is None:
        return False
    if rank_scope == RANK_SCOPE_TRAVELER_DOWN:
        return order <= 20
    if rank_scope == RANK_SCOPE_KNIGHT_DOWN:
        return order <= 50
    if rank_scope == RANK_SCOPE_KNIGHT_UP:
        return order >= 50
    return True


def _side_rank_order(side: MatchSide | None) -> int | None:
    if side is None:
        return None
    profile = side.profile_json if isinstance(side.profile_json, dict) else {}
    label = _rank_label_from_profile(profile)
    label_order = _rank_order_from_label(label)
    if label_order is not None:
        return label_order
    certificate = _rank_certificate(profile)
    if certificate is None:
        return None
    # 旧上传包没有保存段位图标 alt，只能用“証”兜底近似：
    # 0-9 基本对应旅人以下，50 起进入騎士附近，100 以上进入更高石高/爵位段。
    if certificate <= 9:
        return 20
    if certificate < 50:
        return 40
    if certificate < 100:
        return 50
    return 60


def _rank_label_from_profile(profile: dict[str, Any]) -> str:
    for key in ("段位", "位階", "リーグ", "league", "rank_label", "rank"):
        value = profile.get(key)
        if value:
            return str(value).strip()
    return ""


def _rank_order_from_label(label: str) -> int | None:
    text = str(label or "")
    if not text:
        return None
    if "風来坊" in text:
        return 10
    if "旅人" in text:
        return 20
    if "食客" in text:
        return 30
    if "従騎士" in text or "從騎士" in text or "从骑士" in text:
        return 40
    if "騎士" in text or "骑士" in text:
        return 50
    if "万石" in text or "萬石" in text:
        return 60
    higher_tiers = ("男爵", "子爵", "伯爵", "侯爵", "公爵", "海賊", "海贼", "団長", "团长", "頭領", "头领")
    if any(tier in text for tier in higher_tiers):
        return 70
    return None


def _rank_certificate(profile: dict[str, Any]) -> int | None:
    raw = str(profile.get("証") or profile.get("证") or "").strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def _top_decks(
    buckets: dict[str, _LeaderboardBucket],
    lookup,
    seen_names: dict[str, str],
    limit: int | None,
    trend_anchor: str,
) -> list[dict[str, Any]]:
    return [
        _deck_payload(fingerprint, bucket, lookup, seen_names, trend_anchor)
        for fingerprint, bucket in _sorted_buckets(buckets, limit)
    ]


def _top_cards(
    buckets: dict[str, _LeaderboardBucket],
    lookup,
    seen_names: dict[str, str],
    limit: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card_hash, bucket in _sorted_buckets(buckets, limit):
        rows.append(
            {
                **_card_payload(card_hash, lookup, seen_names),
                "sample_count": bucket.sample_count,
                "win_count": bucket.win_count,
                "loss_count": bucket.loss_count,
                "draw_count": bucket.draw_count,
                "win_rate": bucket.win_rate,
                "wilson_lower_bound": _wilson_lower_bound(bucket.win_count, bucket.loss_count),
            }
        )
    return rows


def _top_archetypes(
    buckets: dict[str, _LeaderboardBucket],
    lookup,
    seen_names: dict[str, str],
    limit: int | None,
    trend_anchor: str,
) -> list[dict[str, Any]]:
    archetypes = _deck_archetypes(buckets, lookup)
    return [
        _archetype_payload(archetype, buckets, lookup, seen_names, trend_anchor)
        for archetype in _apply_optional_limit(archetypes, limit)
    ]


def _deck_archetypes(buckets: dict[str, _LeaderboardBucket], lookup) -> list[_DeckArchetype]:
    clusters: list[list[str]] = []
    representatives: list[str] = []
    cost_maps: dict[str, dict[str, float]] = {}

    for fingerprint, _bucket in _sorted_buckets(buckets, len(buckets)):
        cost_maps[fingerprint] = _deck_cost_map(fingerprint, lookup)
        target_index: int | None = None
        # 和本地 deck-archetype-visual 保持同一口径：只向代表构筑吸附，
        # 避免链式相似把边缘构筑串成过大的分类。
        for index, representative in enumerate(representatives):
            if _similar_cost(cost_maps[fingerprint], cost_maps[representative]) >= DEFAULT_ARCHETYPE_SIMILAR_COST:
                target_index = index
                break
        if target_index is None:
            representatives.append(fingerprint)
            clusters.append([fingerprint])
        else:
            clusters[target_index].append(fingerprint)

    archetypes = [
        _DeckArchetype(
            representative=members[0],
            members=members,
            summary=_aggregate_deck_buckets(members, buckets),
            core_hashes=_archetype_core_hashes(members, buckets, lookup, DEFAULT_ARCHETYPE_SIMILAR_COST),
        )
        for members in clusters
        if members
    ]
    return sorted(
        archetypes,
        key=lambda item: (
            _wilson_lower_bound(item.summary.win_count, item.summary.loss_count) or 0,
            item.summary.sample_count,
            item.representative,
        ),
        reverse=True,
    )


def _deck_cost_map(fingerprint: str, lookup) -> dict[str, float]:
    return {card_hash: lookup.cost_value(card_hash) for card_hash in fingerprint.split(",") if card_hash}


def _similar_cost(left: dict[str, float], right: dict[str, float]) -> float:
    return sum(max(left.get(card_hash, 0.0), right.get(card_hash, 0.0)) for card_hash in left.keys() & right.keys())


def _aggregate_deck_buckets(members: list[str], buckets: dict[str, _LeaderboardBucket]) -> _LeaderboardBucket:
    summary = _LeaderboardBucket()
    for fingerprint in members:
        summary.merge(buckets[fingerprint])
    return summary


def _archetype_core_hashes(
    members: list[str],
    buckets: dict[str, _LeaderboardBucket],
    lookup,
    target_cost: float,
) -> list[str]:
    weighted_counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for fingerprint in members:
        for card_hash in fingerprint.split(","):
            if not card_hash:
                continue
            weighted_counts[card_hash] += buckets[fingerprint].sample_count
            first_seen.setdefault(card_hash, len(first_seen))

    sorted_hashes = sorted(
        weighted_counts,
        key=lambda card_hash: (-weighted_counts[card_hash], -lookup.cost_value(card_hash), first_seen[card_hash]),
    )
    core_hashes: list[str] = []
    total_cost = 0.0
    for card_hash in sorted_hashes:
        core_hashes.append(card_hash)
        total_cost += lookup.cost_value(card_hash)
        if total_cost >= target_cost or len(core_hashes) >= 5:
            break
    return core_hashes


def _archetype_payload(
    archetype: _DeckArchetype,
    buckets: dict[str, _LeaderboardBucket],
    lookup,
    seen_names: dict[str, str],
    trend_anchor: str,
) -> dict[str, Any]:
    summary = archetype.summary
    core_cards = [_card_payload(card_hash, lookup, seen_names) for card_hash in archetype.core_hashes]
    return {
        "archetype_id": sha256_text(f"archetype:{archetype.representative}")[:16],
        "title": _archetype_title(core_cards),
        "similar_cost_threshold": DEFAULT_ARCHETYPE_SIMILAR_COST,
        "representative_deck_fingerprint": archetype.representative,
        "member_count": len(archetype.members),
        "member_deck_count": len(archetype.members),
        "sample_count": summary.sample_count,
        "win_count": summary.win_count,
        "loss_count": summary.loss_count,
        "draw_count": summary.draw_count,
        "top_player": summary.top_player,
        "top_player_count": summary.top_player_count,
        "player_count": summary.player_count,
        "win_rate": summary.win_rate,
        "wilson_lower_bound": _wilson_lower_bound(summary.win_count, summary.loss_count),
        "core_cards": core_cards,
        "representative_deck": _deck_payload(archetype.representative, buckets[archetype.representative], lookup, seen_names, trend_anchor),
        "member_decks": [
            _deck_payload(fingerprint, buckets[fingerprint], lookup, seen_names, trend_anchor)
            for fingerprint in archetype.members[:8]
        ],
        "behavior_stats": _behavior_stats(summary, trend_anchor),
    }


def _archetype_title(core_cards: list[dict[str, str]]) -> str:
    names = [card["label"].split("(", 1)[0] for card in core_cards[:3] if card["label"]]
    return " / ".join(names) + " 系" if names else "未识别卡组分类"


def _deck_payload(
    fingerprint: str,
    bucket: _LeaderboardBucket,
    lookup,
    seen_names: dict[str, str],
    trend_anchor: str,
) -> dict[str, Any]:
    card_hashes = _card_hashes_by_cost_desc(fingerprint.split(",") if fingerprint else [], lookup)
    cards = [_card_payload(card_hash, lookup, seen_names) for card_hash in card_hashes]
    deck_name = " / ".join(card["label"] for card in cards if card["label"]) or fingerprint
    return {
        "deck_fingerprint": fingerprint,
        "deck_name": deck_name,
        "sample_count": bucket.sample_count,
        "win_count": bucket.win_count,
        "loss_count": bucket.loss_count,
        "draw_count": bucket.draw_count,
        "top_player": bucket.top_player,
        "top_player_count": bucket.top_player_count,
        "player_count": bucket.player_count,
        "win_rate": bucket.win_rate,
        "wilson_lower_bound": _wilson_lower_bound(bucket.win_count, bucket.loss_count),
        "cards": cards,
        "behavior_stats": _behavior_stats(bucket, trend_anchor),
    }


def _behavior_stats(bucket: _LeaderboardBucket, trend_anchor: str) -> dict[str, Any]:
    return {
        "weapons": _behavior_category_rows(bucket.samples, "weapon_name"),
        "styles": _behavior_category_rows(bucket.samples, "style_name"),
        "trend": _trend_stats(bucket.samples, trend_anchor),
        "credibility": _credibility_stats(bucket),
        "souls": [],
    }


def _behavior_category_rows(samples: list[_LeaderboardSideSample], attr: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    for sample in samples:
        name = _normalize_behavior_name(getattr(sample, attr))
        if not name:
            continue
        row = buckets.setdefault(name, {"sample_count": 0, "win_count": 0, "loss_count": 0, "draw_count": 0})
        row["sample_count"] += 1
        if sample.result == "win":
            row["win_count"] += 1
        elif sample.result == "loss":
            row["loss_count"] += 1
        elif sample.result == "draw":
            row["draw_count"] += 1

    total_sample = sum(row["sample_count"] for row in buckets.values())
    total_win = sum(row["win_count"] for row in buckets.values())
    if total_sample <= 0:
        return []

    sorted_rows = sorted(
        buckets.items(),
        key=lambda item: (-item[1]["sample_count"], -item[1]["win_count"], item[0]),
    )
    visible = sorted_rows[:BEHAVIOR_TOP_LIMIT]
    hidden = sorted_rows[BEHAVIOR_TOP_LIMIT:]
    result = [_behavior_category_payload(name, row, total_sample, total_win) for name, row in visible]
    if hidden:
        other = {"sample_count": 0, "win_count": 0, "loss_count": 0, "draw_count": 0}
        for _name, row in hidden:
            other["sample_count"] += row["sample_count"]
            other["win_count"] += row["win_count"]
            other["loss_count"] += row["loss_count"]
            other["draw_count"] += row["draw_count"]
        result.append(_behavior_category_payload("其他", other, total_sample, total_win))
    return result


def _behavior_category_payload(name: str, row: dict[str, int], total_sample: int, total_win: int) -> dict[str, Any]:
    sample_count = int(row["sample_count"])
    win_count = int(row["win_count"])
    loss_count = int(row["loss_count"])
    low_sample = sample_count < BEHAVIOR_MIN_CONDITIONAL_SAMPLE
    # 小样本只展示频率，不给条件胜率，避免把偶然结果包装成强度结论。
    conditional_win_rate = None if low_sample else _win_rate(win_count, loss_count)
    return {
        "name": name,
        "sample_count": sample_count,
        "win_count": win_count,
        "usage_rate": sample_count / total_sample if total_sample else None,
        "win_usage_rate": win_count / total_win if total_win else None,
        "conditional_win_rate": conditional_win_rate,
        "low_sample": low_sample,
    }


def _trend_stats(samples: list[_LeaderboardSideSample], trend_anchor: str) -> dict[str, Any]:
    dated_samples = [(sample, _sample_date(sample.played_at)) for sample in samples]
    dated_samples = [(sample, sample_date) for sample, sample_date in dated_samples if sample_date is not None]
    anchor = _trend_anchor_date(trend_anchor, dated_samples)
    if anchor is None:
        return {
            "last_7d_sample_count": 0,
            "last_7d_win_rate": None,
            "previous_7d_sample_count": 0,
            "previous_7d_win_rate": None,
            "delta_7d": None,
            "last_30_points": [],
        }

    # 趋势以服务端配置 date_to 为锚点，而不是机器当天日期，保证历史快照可复现。
    last_start = anchor - timedelta(days=6)
    previous_start = anchor - timedelta(days=13)
    previous_end = anchor - timedelta(days=7)
    last_samples = [sample for sample, sample_date in dated_samples if last_start <= sample_date <= anchor]
    previous_samples = [sample for sample, sample_date in dated_samples if previous_start <= sample_date <= previous_end]
    last_win_rate = _samples_win_rate(last_samples)
    previous_win_rate = _samples_win_rate(previous_samples)
    return {
        "last_7d_sample_count": len(last_samples),
        "last_7d_win_rate": last_win_rate,
        "previous_7d_sample_count": len(previous_samples),
        "previous_7d_win_rate": previous_win_rate,
        "delta_7d": last_win_rate - previous_win_rate if last_win_rate is not None and previous_win_rate is not None else None,
        "last_30_points": _last_30_trend_points(dated_samples),
    }


def _last_30_trend_points(dated_samples: list[tuple[_LeaderboardSideSample, date]]) -> list[dict[str, Any]]:
    recent = sorted(
        dated_samples,
        key=lambda item: (item[1], item[0].played_at, item[0].player_name, item[0].weapon_name, item[0].style_name),
    )[-30:]
    wins = 0
    losses = 0
    points: list[dict[str, Any]] = []
    for index, (sample, sample_date) in enumerate(recent, start=1):
        if sample.result == "win":
            wins += 1
        elif sample.result == "loss":
            losses += 1
        points.append(
            {
                "index": index,
                "date": sample_date.isoformat(),
                "played_at": sample.played_at,
                "result": sample.result,
                "rolling_win_rate": _win_rate(wins, losses),
            }
        )
    return points


def _credibility_stats(bucket: _LeaderboardBucket) -> dict[str, Any]:
    top3_count = sum(count for _player, count in bucket.player_counts.most_common(3))
    top3_share = top3_count / bucket.sample_count if bucket.sample_count else 0.0
    if bucket.sample_count >= 500 and bucket.player_count >= 30 and top3_share < 0.5:
        label = "high"
    elif bucket.sample_count >= 200 and bucket.player_count >= 15 and top3_share < 0.7:
        label = "medium"
    else:
        label = "low"
    return {
        "label": label,
        "top3_player_share": top3_share,
        "player_count": bucket.player_count,
        "sample_count": bucket.sample_count,
    }


def _selected_name(side: MatchSide | None, key: str) -> str:
    selected = getattr(side, "selected_json", None)
    if not isinstance(selected, dict):
        return ""
    raw = selected.get(key)
    if isinstance(raw, dict):
        return _normalize_behavior_name(raw.get("name") or raw.get("label") or raw.get("summary"))
    return _normalize_behavior_name(raw)


def _normalize_behavior_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return "" if text in {"", "-", "None", "none", "未選択", "未选择"} else text


def _sample_date(value: str) -> date | None:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _trend_anchor_date(trend_anchor: str, dated_samples: list[tuple[_LeaderboardSideSample, date]]) -> date | None:
    anchor = _sample_date(trend_anchor)
    if anchor is not None:
        return anchor
    return max((sample_date for _sample, sample_date in dated_samples), default=None)


def _samples_win_rate(samples: list[_LeaderboardSideSample]) -> float | None:
    win_count = sum(1 for sample in samples if sample.result == "win")
    loss_count = sum(1 for sample in samples if sample.result == "loss")
    return _win_rate(win_count, loss_count)


def _win_rate(win_count: int, loss_count: int) -> float | None:
    total = win_count + loss_count
    return win_count / total if total else None


def _sorted_buckets(buckets: dict[str, _LeaderboardBucket], limit: int | None) -> list[tuple[str, _LeaderboardBucket]]:
    rows = sorted(
        buckets.items(),
        key=lambda item: (
            _wilson_lower_bound(item[1].win_count, item[1].loss_count) or 0,
            item[1].sample_count,
            item[1].win_count,
            item[0],
        ),
        reverse=True,
    )
    return _apply_optional_limit(rows, limit)


def _apply_optional_limit(rows: list[Any], limit: int | None) -> list[Any]:
    # 排行榜页面默认要展示全量；只有显式传入 limit 时才裁剪，便于后续 API 需要分页/截断时复用。
    return rows if limit is None else rows[:limit]


def _card_payload(card_hash: str, lookup, seen_names: dict[str, str]) -> dict[str, str]:
    label = _card_label(card_hash, lookup, seen_names)
    return {
        "card_hash": card_hash,
        "card_code": _card_code(card_hash, lookup),
        "label": label,
        "image_url": _official_card_small_url(card_hash, lookup),
    }


def _card_label(card_hash: str, lookup, seen_names: dict[str, str]) -> str:
    if card_hash in lookup.cards_by_hash:
        return lookup.label(card_hash)
    name = seen_names.get(card_hash, "")
    return name or f"未识别卡({card_hash[:8]})"


def _card_code(card_hash: str, lookup) -> str:
    if card_hash in lookup.cards_by_hash:
        return lookup.card_code(card_hash)
    return card_hash if _looks_like_card_code(card_hash) else ""


def _official_card_small_url(card_hash: str, lookup) -> str:
    if card_hash in lookup.cards_by_hash:
        return lookup.official_card_small_url(card_hash)
    return f"https://image.eiketsu-taisen.net/general/card_small/{quote(card_hash + '.jpg', safe='.')}" if card_hash else ""


def _looks_like_card_code(value: str) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 8:
        return False
    return any(ch.isalpha() or ord(ch) > 127 for ch in text) and any(ch.isdigit() for ch in text)


def _card_hashes_by_cost_desc(card_hashes: list[str], lookup) -> list[str]:
    indexed_hashes = [(index, card_hash) for index, card_hash in enumerate(card_hashes) if card_hash]
    return [
        card_hash
        for index, card_hash in sorted(
            indexed_hashes,
            key=lambda item: (-lookup.cost_value(item[1]), item[0]),
        )
    ]


def _card_names_from_matches(matches: list[Match]) -> dict[str, str]:
    names: dict[str, str] = {}
    for match in matches:
        for side in match.sides:
            selected = side.selected_json or {}
            generals = selected.get("generals") if isinstance(selected, dict) else []
            if not isinstance(generals, list):
                continue
            for general in generals:
                if not isinstance(general, dict):
                    continue
                card_hash = str(general.get("hash_id") or "")
                raw_name = str(general.get("raw_name") or "").strip()
                if card_hash and raw_name:
                    names.setdefault(card_hash, raw_name)
    return names


def _result_for_side(match: Match, side_index: int) -> str:
    result = _normalize_result(match.result if side_index == 1 else _reverse_result(match.result))
    if result != "unknown":
        return result
    for side in match.sides:
        if side.side_index == side_index:
            return _normalize_result(side.result)
    return "unknown"


def _normalize_result(result: str) -> str:
    text = str(result or "unknown")
    return text if text in {"win", "loss", "draw"} else "unknown"


def _reverse_result(result: str) -> str:
    text = _normalize_result(result)
    if text == "win":
        return "loss"
    if text == "loss":
        return "win"
    return text


def _wilson_lower_bound(win_count: int, loss_count: int, z: float = 1.96) -> float | None:
    total = win_count + loss_count
    if total <= 0:
        return None
    phat = win_count / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * ((phat * (1 - phat) + z * z / (4 * total)) / total) ** 0.5
    return (centre - margin) / denominator


def _top_counts(counts: dict[str, int], limit: int, key_name: str) -> list[dict[str, Any]]:
    pairs = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{key_name: key, "sample_count": count} for key, count in pairs]
