"""通过演武场视频搜索按卡牌扩展采集样本。"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from eiketsu_env.config import Settings
from eiketsu_env.db.models import Match, MatchDeckUnit, RawSnapshot
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.browser_session import create_member_session
from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.services.parsers import parse_replay_html
from eiketsu_env.services.progress import ProgressReporter
from eiketsu_env.services.requesting import (
    ConsecutiveHttpPause,
    RequestThrottle,
    VideoSearchConcurrencyProfile,
    call_with_retries,
    video_search_concurrency_profile,
)
from eiketsu_env.services.repository import EnvRepository

_THREAD_LOCAL = threading.local()


@dataclass(slots=True)
class VideoSearchCollectResult:
    run_id: int
    status: str
    counts: dict[str, Any]
    errors: list[dict[str, Any]]


@dataclass(slots=True)
class VideoApiResult:
    card_hash: str
    payload: str
    final_url: str
    response: dict[str, Any]


@dataclass(slots=True)
class VideoPlayResult:
    card_hash: str
    item: dict[str, Any]
    replay_id: str
    played_at: str
    html: str
    final_url: str
    replay: dict[str, Any]


def collect_video_search(
    settings: Settings,
    date_from: str,
    date_to: str,
    card_hashes: list[str] | None = None,
    max_cards: int = 20,
    max_results: int = 0,
    include_solo: bool = False,
    auth_source: str = "",
    version: str = "",
    skip_searched_cards: bool = False,
    frontier_rounds: str | int = "1",
    concurrency_profile: str = "default",
    progress: ProgressReporter | None = None,
) -> VideoSearchCollectResult:
    """按卡牌搜索演武场公开视频，把 replay 样本合并进现有对局库。"""

    requested_cards = _unique_items(card_hashes or [])
    profile = video_search_concurrency_profile(concurrency_profile)
    throttle = RequestThrottle(profile.min_request_interval_seconds)
    pause_gate = ConsecutiveHttpPause(503, profile.http_503_pause_threshold, profile.http_503_pause_seconds)
    max_frontier_rounds = _resolve_frontier_rounds(frontier_rounds)
    factory = make_session_factory(settings)
    errors: list[dict[str, Any]] = []
    counts: dict[str, Any] = {
        "cards": 0,
        "cards_searched": 0,
        "cards_skipped_searched": 0,
        "frontier_rounds": 0,
        "frontier_cards_discovered": 0,
        "api_requests": 0,
        "videos_seen": 0,
        "videos_in_range": 0,
        "play_pages": 0,
        "matches": 0,
        "duplicates_in_run": 0,
        "duplicates_existing": 0,
        "skipped_by_date": 0,
        "skipped_by_mode": 0,
        "skipped_by_version": 0,
        "skipped_without_opponent": 0,
        "max_results_reached": False,
    }

    with factory() as session:
        repo = EnvRepository(session, settings)
        cards = requested_cards or _top_card_hashes(session, max_cards)
        searched_cards = _searched_card_hashes(session) if skip_searched_cards else set()
        existing_replay_ids = _existing_replay_ids_with_decks(session)
        counts["cards"] = len(cards)
        counts["cards_skipped_searched"] = sum(1 for card_hash in cards if card_hash in searched_cards)
        run = repo.start_run(
            "video_search",
            date_from,
            date_to,
            {
                "cards": cards,
                "requested_cards": requested_cards,
                "max_cards": max_cards,
                "max_results": max_results,
                "include_solo": include_solo,
                "version": version,
                "skip_searched_cards": skip_searched_cards,
                "frontier_rounds": frontier_rounds,
                "concurrency_profile": profile.name,
                "api_workers": profile.api_workers,
                "play_workers": profile.play_workers,
            },
        )
        session.commit()

        seen_replay_ids: set[str] = set()
        frontier = [card_hash for card_hash in cards if card_hash not in searched_cards]
        try:
            if not cards:
                raise ValueError("没有可用于演武场搜索的卡牌 hash；请先采集 follow 数据，或传入 --card-hash")
            if progress:
                progress.message(
                    f"video-search: {len(cards)} seed cards, skipped={counts['cards_skipped_searched']}, profile={profile.name}"
                )

            while frontier:
                if max_frontier_rounds is not None and counts["frontier_rounds"] >= max_frontier_rounds:
                    break
                if max_results > 0 and counts["matches"] >= max_results:
                    counts["max_results_reached"] = True
                    break
                round_cards = _unique_items([card_hash for card_hash in frontier if card_hash not in searched_cards])
                frontier = []
                if not round_cards:
                    break
                counts["frontier_rounds"] += 1
                counts["cards_searched"] += len(round_cards)
                if progress:
                    progress.message(f"frontier round {counts['frontier_rounds']}: cards={len(round_cards)}")
                api_results, api_errors = _fetch_video_api_results(
                    settings,
                    auth_source,
                    profile,
                    throttle,
                    pause_gate,
                    round_cards,
                    progress=progress,
                    label=f"api round {counts['frontier_rounds']}",
                )
                errors.extend(api_errors)
                play_jobs: list[tuple[str, dict[str, Any], str, str]] = []
                stop_scheduling_play = False

                for api_result in api_results:
                    card_hash = api_result.card_hash
                    searched_cards.add(card_hash)
                    counts["api_requests"] += 1
                    repo.write_raw_snapshot(run, "video_search_api", f"{api_result.final_url}?g={card_hash}", api_result.payload, date_hint=date_from)
                    response = api_result.response
                    if response.get("error_id"):
                        errors.append({"stage": "video_search_api", "card_hash": card_hash, "error_id": response.get("error_id"), "message": response.get("message")})
                        continue
                    for item in response.get("data") or []:
                        counts["videos_seen"] += 1
                        if item.get("thereData") is False:
                            counts["skipped_without_opponent"] += 1
                            continue
                        if not _matches_requested_version(item, version):
                            counts["skipped_by_version"] += 1
                            continue
                        if not is_environment_mode(str(item.get("mode") or ""), include_solo=include_solo):
                            counts["skipped_by_mode"] += 1
                            continue
                        played_at = _resolve_battle_datetime(str(item.get("dispBattleDate") or ""), date_from, date_to)
                        if not played_at:
                            counts["skipped_by_date"] += 1
                            continue
                        counts["videos_in_range"] += 1
                        replay_id = str(item.get("paramId") or "")
                        if not replay_id:
                            continue
                        if replay_id in seen_replay_ids:
                            counts["duplicates_in_run"] += 1
                            continue
                        seen_replay_ids.add(replay_id)
                        if replay_id in existing_replay_ids:
                            counts["duplicates_existing"] += 1
                            continue
                        if max_results > 0 and counts["matches"] + len(play_jobs) >= max_results:
                            stop_scheduling_play = True
                            break
                        play_jobs.append((card_hash, item, replay_id, played_at))
                    if stop_scheduling_play:
                        break
                session.commit()

                play_results, play_errors = _fetch_video_play_results(
                    settings,
                    auth_source,
                    profile,
                    throttle,
                    pause_gate,
                    play_jobs,
                    progress=progress,
                    label=f"play round {counts['frontier_rounds']}",
                )
                errors.extend(play_errors)
                discovered_cards: list[str] = []
                for play_result in play_results:
                    counts["play_pages"] += 1
                    detail = _video_item_to_detail(play_result.item, play_result.replay, play_result.final_url, play_result.played_at)
                    match = repo.upsert_match_detail(detail, run)
                    repo.write_raw_snapshot(run, "video_search_play", play_result.final_url, play_result.html, match=match, date_hint=play_result.played_at[:10])
                    counts["matches"] += 1
                    existing_replay_ids.add(play_result.replay_id)
                    discovered_cards.extend(_card_hashes_from_replay(play_result.replay))
                    session.commit()
                    if max_results > 0 and counts["matches"] >= max_results:
                        counts["max_results_reached"] = True
                        break

                next_frontier = [
                    card_hash
                    for card_hash in _unique_items(discovered_cards)
                    if card_hash not in searched_cards
                ]
                counts["frontier_cards_discovered"] += len(next_frontier)
                frontier = next_frontier
            status = "completed_limited" if counts["max_results_reached"] else "completed"
            if errors:
                status = f"{status}_with_errors"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            errors.append({"stage": "video_search", "error": str(exc)})
        repo.finish_run(run, status, counts, errors)
        session.commit()
        return VideoSearchCollectResult(run.id, status, counts, errors)


def _fetch_video_api_results(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    card_hashes: list[str],
    progress: ProgressReporter | None = None,
    label: str = "api",
) -> tuple[list[VideoApiResult], list[dict[str, Any]]]:
    task = progress.task(label, len(card_hashes)) if progress else None
    if profile.api_workers <= 1:
        results, errors = _run_serial_video_api_results(settings, auth_source, profile, throttle, pause_gate, card_hashes, task)
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
        return results, errors
    results: list[VideoApiResult] = []
    errors: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=profile.api_workers) as executor:
            futures = {
                executor.submit(_fetch_video_api_result, settings, auth_source, profile, throttle, pause_gate, card_hash): card_hash
                for card_hash in card_hashes
            }
            for future in as_completed(futures):
                card_hash = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单卡搜索失败只记录错误，后续卡继续跑。
                    errors.append({"stage": "video_search_api", "card_hash": card_hash, "error": str(exc)})
                if task:
                    task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    finally:
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
    return results, errors


def _run_serial_video_api_results(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    card_hashes: list[str],
    task=None,
) -> tuple[list[VideoApiResult], list[dict[str, Any]]]:
    results: list[VideoApiResult] = []
    errors: list[dict[str, Any]] = []
    for card_hash in card_hashes:
        try:
            results.append(_fetch_video_api_result(settings, auth_source, profile, throttle, pause_gate, card_hash))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "video_search_api", "card_hash": card_hash, "error": str(exc)})
        if task:
            task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    return results, errors


def _fetch_video_api_result(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    card_hash: str,
) -> VideoApiResult:
    member = _thread_member_session(settings, auth_source)
    payload, final_api_url = call_with_retries(
        lambda: member.post_form(
            f"{settings.base_url}/members/enbujyo/api/search_video",
            _video_search_fields([card_hash]),
            referer=f"{settings.base_url}/members/enbujyo/",
        ),
        profile.retry_policy,
        throttle=throttle,
        pause_gate=pause_gate,
    )
    return VideoApiResult(card_hash=card_hash, payload=payload, final_url=final_api_url, response=json.loads(payload))


def _fetch_video_play_results(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    jobs: list[tuple[str, dict[str, Any], str, str]],
    progress: ProgressReporter | None = None,
    label: str = "play",
) -> tuple[list[VideoPlayResult], list[dict[str, Any]]]:
    if not jobs:
        return [], []
    task = progress.task(label, len(jobs)) if progress else None
    if profile.play_workers <= 1:
        results, errors = _run_serial_video_play_results(settings, auth_source, profile, throttle, pause_gate, jobs, task)
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
        return results, errors
    results: list[VideoPlayResult] = []
    errors: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=profile.play_workers) as executor:
            futures = {
                executor.submit(_fetch_video_play_result, settings, auth_source, profile, throttle, pause_gate, card_hash, item, replay_id, played_at): (card_hash, replay_id)
                for card_hash, item, replay_id, played_at in jobs
            }
            for future in as_completed(futures):
                card_hash, replay_id = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单条播放页失败不影响其它播放页。
                    errors.append({"stage": "video_search_play", "card_hash": card_hash, "replay_id": replay_id, "error": str(exc)})
                if task:
                    task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    finally:
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
    return results, errors


def _run_serial_video_play_results(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    jobs: list[tuple[str, dict[str, Any], str, str]],
    task=None,
) -> tuple[list[VideoPlayResult], list[dict[str, Any]]]:
    results: list[VideoPlayResult] = []
    errors: list[dict[str, Any]] = []
    for card_hash, item, replay_id, played_at in jobs:
        try:
            results.append(_fetch_video_play_result(settings, auth_source, profile, throttle, pause_gate, card_hash, item, replay_id, played_at))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "video_search_play", "card_hash": card_hash, "replay_id": replay_id, "error": str(exc)})
        if task:
            task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    return results, errors


def _fetch_video_play_result(
    settings: Settings,
    auth_source: str,
    profile: VideoSearchConcurrencyProfile,
    throttle: RequestThrottle,
    pause_gate: ConsecutiveHttpPause,
    card_hash: str,
    item: dict[str, Any],
    replay_id: str,
    played_at: str,
) -> VideoPlayResult:
    member = _thread_member_session(settings, auth_source)
    play_url = f"{settings.base_url}/members/enbujyo/play?p={replay_id}&type=video_search"
    play_html, final_play_url = call_with_retries(
        lambda: member.fetch_text(play_url, referer=f"{settings.base_url}/members/enbujyo/"),
        profile.retry_policy,
        throttle=throttle,
        pause_gate=pause_gate,
    )
    replay = parse_replay_html(play_html, final_play_url, settings.base_url)
    return VideoPlayResult(card_hash=card_hash, item=item, replay_id=replay_id, played_at=played_at, html=play_html, final_url=final_play_url, replay=replay)


def _thread_member_session(settings: Settings, auth_source: str):
    # 每个 worker 复用自己的 HTTP 会话；数据库写入仍只在主线程执行。
    cache = getattr(_THREAD_LOCAL, "member_sessions", None)
    if cache is None:
        cache = {}
        _THREAD_LOCAL.member_sessions = cache
    key = (id(settings), auth_source or "")
    member = cache.get(key)
    if member is None:
        member = create_member_session(settings, auth_source or None)
        cache[key] = member
    return member


def _video_search_fields(card_hashes: list[str]) -> list[tuple[str, str]]:
    fields = [("g", card_hash) for card_hash in card_hashes]
    # 这些参数来自站内 search_video_easy.js；保持默认范围，避免额外缩窄搜索结果。
    fields.extend([("r", "0"), ("min", "0"), ("max", "0"), ("kmin", "-1"), ("kmax", "-1"), ("srs", "0"), ("sg", "")])
    return fields


def _video_item_to_detail(item: dict[str, Any], replay: dict[str, Any], play_url: str, played_at: str) -> dict[str, Any]:
    names = replay.get("player_names") or []
    follow_ids = replay.get("follow_ids") or []
    player_name = str(item.get("thisName") or (names[0] if len(names) > 0 else ""))
    enemy_name = str(item.get("thereName") or (names[1] if len(names) > 1 else ""))
    return {
        "source_type": "video_search",
        "detail_url": play_url,
        "url": play_url,
        "source_url": play_url,
        "play_url": replay.get("replay_url") or play_url,
        "replay_id": replay.get("replay_id") or item.get("paramId") or "",
        "m3u8_url": replay.get("m3u8_url") or "",
        "played_at": played_at,
        "date": played_at,
        "mode": item.get("mode") or "",
        "version": item.get("package") or "",
        "result": "unknown",
        "title": f"{player_name} vs {enemy_name}",
        "castle_breakdown": {},
        "timeline_labels": [],
        "timeline_data": {},
        "players": [
            {
                "side_index": 1,
                "role": "player",
                "player_name": player_name,
                "follow_id": follow_ids[0] if len(follow_ids) > 0 else "",
                "result": "unknown",
                "deck_ids": replay.get("player1_deck_ids") or [],
                "profile": {"source": "video_search"},
                "selected": {},
            },
            {
                "side_index": 2,
                "role": "enemy",
                "player_name": enemy_name,
                "follow_id": follow_ids[1] if len(follow_ids) > 1 else "",
                "result": "unknown",
                "deck_ids": replay.get("player2_deck_ids") or [],
                "profile": {"source": "video_search"},
                "selected": {},
            },
        ],
    }


def _matches_requested_version(item: dict[str, Any], version: str) -> bool:
    return not version or str(item.get("package") or "") == version


def _top_card_hashes(session, limit: int) -> list[str]:
    query = (
        select(MatchDeckUnit.card_hash, func.count(MatchDeckUnit.id).label("sample_count"))
        .group_by(MatchDeckUnit.card_hash)
        .order_by(func.count(MatchDeckUnit.id).desc(), MatchDeckUnit.card_hash)
    )
    if limit > 0:
        query = query.limit(limit)
    return [str(row.card_hash) for row in session.execute(query).all() if row.card_hash]


def _searched_card_hashes(session) -> set[str]:
    card_hashes: set[str] = set()
    urls = session.scalars(select(RawSnapshot.source_url).where(RawSnapshot.source_kind == "video_search_api")).all()
    for source_url in urls:
        query = parse_qs(urlparse(str(source_url or "")).query)
        for value in query.get("g") or []:
            if value:
                card_hashes.add(str(value))
    return card_hashes


def _existing_replay_ids_with_decks(session) -> set[str]:
    return {
        str(replay_id)
        for replay_id in session.scalars(select(Match.replay_id).where(Match.replay_id.is_not(None), Match.decks.any())).all()
        if replay_id
    }


def _resolve_frontier_rounds(value: str | int) -> int | None:
    normalized = str(value or "1").strip().lower()
    if normalized == "auto":
        return None
    rounds = int(normalized)
    if rounds < 1:
        raise ValueError("--frontier-rounds 必须是正整数或 auto")
    return rounds


def _card_hashes_from_replay(replay: dict[str, Any]) -> list[str]:
    return _unique_items(
        [str(card_hash) for card_hash in replay.get("player1_deck_ids") or []]
        + [str(card_hash) for card_hash in replay.get("player2_deck_ids") or []]
    )


def _resolve_battle_datetime(label: str, date_from: str, date_to: str) -> str:
    match = re.search(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", label)
    if not match:
        return ""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    month, day, hour, minute = (int(item) for item in match.groups())
    for year in range(start.year - 1, end.year + 2):
        try:
            candidate = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        if start <= candidate.date() <= end:
            return candidate.strftime("%Y-%m-%d %H:%M")
    return ""


def _unique_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
