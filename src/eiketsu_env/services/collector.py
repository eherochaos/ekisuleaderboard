"""编排关注列表、每日列表和详情页采集，并把结果写入仓储。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select

from eiketsu_env.config import Settings
from eiketsu_env.db.models import Match, MatchAlias
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.browser_session import create_member_session
from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.services.parsers import parse_daily_html, parse_detail_html, parse_follow_api_json, parse_follow_html
from eiketsu_env.services.progress import ProgressReporter
from eiketsu_env.services.requesting import FollowConcurrencyProfile, call_with_retries, follow_concurrency_profile
from eiketsu_env.services.repository import EnvRepository
from eiketsu_env.utils import JST, extract_detail_t, extract_follow_id

_THREAD_LOCAL = threading.local()


@dataclass(slots=True)
class CollectResult:
    run_id: int
    status: str
    counts: dict[str, Any]
    errors: list[dict[str, Any]]


@dataclass(slots=True)
class DailyPageResult:
    player: dict[str, str]
    iso_date: str
    html: str
    final_url: str
    seeds: list[dict[str, Any]]


@dataclass(slots=True)
class DetailPageResult:
    player: dict[str, str]
    iso_date: str
    seed: dict[str, Any]
    html: str
    final_url: str
    detail: dict[str, Any]


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("--to 不能早于 --from")
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _daily_url_for_date(settings: Settings, player: dict[str, str], iso_date: str) -> str:
    target = date.fromisoformat(iso_date)
    query = urlencode({"y": target.year, "m": target.month, "d": target.day, "f": str(player.get("follow_id") or "")})
    return f"{settings.base_url}/members/history/daily?{query}"


def _filter_players(players: list[dict[str, str]], player_id: str = "", player_name: str = "") -> list[dict[str, str]]:
    if player_id:
        return [player for player in players if str(player.get("follow_id") or "") == str(player_id)]
    if player_name:
        needle = player_name.casefold()
        return [player for player in players if needle in str(player.get("name") or "").casefold()]
    return players


def _filter_active_players(players: list[dict[str, str]], date_from: str) -> tuple[list[dict[str, str]], int]:
    active_players = []
    skipped = 0
    for player in players:
        if _player_inactive_before_range(player, date_from):
            skipped += 1
        else:
            active_players.append(player)
    return active_players, skipped


def _player_inactive_before_range(player: dict[str, str], date_from: str) -> bool:
    lastplaytime = str(player.get("lastplaytime") or "").strip()
    if not lastplaytime.isdigit():
        return False
    try:
        last_played_date = datetime.fromtimestamp(int(lastplaytime), JST).date()
    except (OSError, ValueError, OverflowError):
        return False
    return last_played_date < date.fromisoformat(date_from)


def collect_follow(
    settings: Settings,
    date_from: str,
    date_to: str,
    max_players: int = 0,
    max_matches: int = 0,
    player_id: str = "",
    player_name: str = "",
    include_solo: bool = False,
    auth_source: str = "",
    interactive_auth: bool = False,
    skip_existing: bool = False,
    skip_inactive: bool = False,
    concurrency_profile: str = "default",
    progress: ProgressReporter | None = None,
    save_raw_snapshots: bool = True,
) -> CollectResult:
    dates = _date_range(date_from, date_to)
    profile = follow_concurrency_profile(concurrency_profile)
    factory = make_session_factory(settings)
    member = create_member_session(settings, auth_source or None, interactive=interactive_auth)
    errors: list[dict[str, Any]] = []
    counts: dict[str, Any] = {
        "dates": len(dates),
        "players": 0,
        "daily_pages": 0,
        "detail_pages": 0,
        "detail_candidates": 0,
        "existing_detail_skipped": 0,
        "matches": 0,
        "players_visited": 0,
        "players_inactive_skipped": 0,
        "max_matches_reached": False,
        "skipped_by_mode": 0,
    }

    with factory() as session:
        repo = EnvRepository(session, settings)
        run = repo.start_run(
            "follow",
            date_from,
            date_to,
            {
                "dates": dates,
                "max_players": max_players,
                "max_matches": max_matches,
                "player_id": player_id,
                "player_name": player_name,
                "include_solo": include_solo,
                "skip_existing": skip_existing,
                "skip_inactive": skip_inactive,
                "save_raw_snapshots": save_raw_snapshots,
                "concurrency_profile": profile.name,
                "daily_workers": profile.daily_workers,
                "detail_workers": profile.detail_workers,
            },
        )
        session.commit()
        try:
            follow_url = f"{settings.base_url}/members/follow/"
            follow_html, final_follow_url = call_with_retries(lambda: member.fetch_text(follow_url), profile.retry_policy)
            if save_raw_snapshots:
                repo.write_raw_snapshot(run, "follow", final_follow_url, follow_html, date_hint=date_from)
            players = parse_follow_html(follow_html, final_follow_url, settings.base_url)
            try:
                api_url = f"{settings.base_url}/members/follow/api/followlist"
                follow_api_payload, final_api_url = call_with_retries(
                    lambda: member.fetch_text(api_url, referer=final_follow_url),
                    profile.retry_policy,
                )
                if save_raw_snapshots:
                    repo.write_raw_snapshot(run, "follow_api", final_api_url, follow_api_payload, date_hint=date_from)
                api_players = parse_follow_api_json(follow_api_payload, settings.base_url)
                if api_players:
                    players = api_players
            except Exception as exc:  # noqa: BLE001 - API 失败时保留 HTML 兜底路径。
                errors.append({"stage": "follow_api", "error": str(exc)})
            counts["players_total"] = len(players)
            players = _filter_players(players, player_id, player_name)
            counts["players_filtered"] = len(players)
            if skip_inactive:
                players, inactive_skipped = _filter_active_players(players, date_from)
                counts["players_inactive_skipped"] = inactive_skipped
            if max_players > 0:
                players = players[:max_players]
            counts["players"] = len(players)
            for player in players:
                repo.upsert_follow_player(player)
            session.commit()
            if progress:
                progress.message(f"follow: {len(dates)} days, {len(players)} players, profile={profile.name}")

            for iso_date in dates:
                if max_matches > 0 and counts["matches"] >= max_matches:
                    counts["max_matches_reached"] = True
                    break

                daily_results, daily_errors = _fetch_daily_pages(settings, auth_source, profile, players, iso_date, final_follow_url, progress=progress)
                errors.extend(daily_errors)
                detail_jobs: list[tuple[dict[str, str], str, dict[str, Any], str]] = []
                stop_scheduling_details = False
                for daily_result in daily_results:
                    if save_raw_snapshots:
                        repo.write_raw_snapshot(run, "daily", daily_result.final_url, daily_result.html, date_hint=iso_date)
                    counts["daily_pages"] += 1
                    counts["players_visited"] += 1
                    for seed in daily_result.seeds:
                        if not is_environment_mode(str(seed.get("mode") or ""), include_solo=include_solo):
                            counts["skipped_by_mode"] += 1
                            continue
                        if max_matches > 0 and counts["matches"] + len(detail_jobs) >= max_matches:
                            stop_scheduling_details = True
                            break
                        counts["detail_candidates"] += 1
                        if skip_existing and _existing_detail_is_complete(session, seed):
                            counts["existing_detail_skipped"] += 1
                            continue
                        detail_jobs.append((daily_result.player, iso_date, seed, daily_result.final_url))
                    if stop_scheduling_details:
                        break
                session.commit()

                detail_results, detail_errors = _fetch_detail_pages(settings, auth_source, profile, detail_jobs, progress=progress, label=f"detail {iso_date}")
                errors.extend(detail_errors)
                for detail_result in detail_results:
                    counts["detail_pages"] += 1
                    detail = detail_result.detail
                    if not is_environment_mode(str(detail.get("mode") or ""), include_solo=include_solo):
                        counts["skipped_by_mode"] += 1
                        continue
                    match = repo.upsert_match_detail(detail, run)
                    if save_raw_snapshots:
                        repo.write_raw_snapshot(run, "detail", detail_result.final_url, detail_result.html, match=match, date_hint=detail_result.iso_date)
                    counts["matches"] += 1
                    session.commit()
                    if max_matches > 0 and counts["matches"] >= max_matches:
                        counts["max_matches_reached"] = True
                        break

                if max_matches > 0 and counts["matches"] >= max_matches:
                    counts["max_matches_reached"] = True
                    break

            status = "completed_limited" if counts["max_matches_reached"] else "completed"
            if errors:
                status = f"{status}_with_errors"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            errors.append({"stage": "follow", "error": str(exc)})
        repo.finish_run(run, status, counts, errors)
        session.commit()
        return CollectResult(run.id, status, counts, errors)


def _fetch_daily_pages(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    players: list[dict[str, str]],
    iso_date: str,
    referer: str,
    progress: ProgressReporter | None = None,
) -> tuple[list[DailyPageResult], list[dict[str, Any]]]:
    task = progress.task(f"daily {iso_date}", len(players)) if progress else None
    if profile.daily_workers <= 1:
        results, errors = _run_serial_daily_pages(settings, auth_source, profile, players, iso_date, referer, task)
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
        return results, errors
    results: list[DailyPageResult] = []
    errors: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=profile.daily_workers) as executor:
            futures = {
                executor.submit(_fetch_daily_page, settings, auth_source, profile, player, iso_date, referer): player
                for player in players
            }
            for future in as_completed(futures):
                player = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单个 daily 失败时保留错误并继续其它玩家。
                    errors.append({"stage": "daily", "player": player, "date": iso_date, "error": str(exc)})
                if task:
                    task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    finally:
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
    return results, errors


def _run_serial_daily_pages(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    players: list[dict[str, str]],
    iso_date: str,
    referer: str,
    task=None,
) -> tuple[list[DailyPageResult], list[dict[str, Any]]]:
    results: list[DailyPageResult] = []
    errors: list[dict[str, Any]] = []
    for player in players:
        try:
            results.append(_fetch_daily_page(settings, auth_source, profile, player, iso_date, referer))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "daily", "player": player, "date": iso_date, "error": str(exc)})
        if task:
            task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    return results, errors


def _fetch_daily_page(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    player: dict[str, str],
    iso_date: str,
    referer: str,
) -> DailyPageResult:
    member = _thread_member_session(settings, auth_source)
    daily_url = _daily_url_for_date(settings, player, iso_date)
    daily_html, final_daily_url = call_with_retries(
        lambda: member.fetch_text(daily_url, referer=referer),
        profile.retry_policy,
    )
    seeds = parse_daily_html(daily_html, final_daily_url, settings.base_url, iso_date, player)
    return DailyPageResult(player=player, iso_date=iso_date, html=daily_html, final_url=final_daily_url, seeds=seeds)


def _fetch_detail_pages(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    jobs: list[tuple[dict[str, str], str, dict[str, Any], str]],
    progress: ProgressReporter | None = None,
    label: str = "detail",
) -> tuple[list[DetailPageResult], list[dict[str, Any]]]:
    if not jobs:
        return [], []
    task = progress.task(label, len(jobs)) if progress else None
    if profile.detail_workers <= 1:
        results, errors = _run_serial_detail_pages(settings, auth_source, profile, jobs, task)
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
        return results, errors
    results: list[DetailPageResult] = []
    errors: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=profile.detail_workers) as executor:
            futures = {
                executor.submit(_fetch_detail_page, settings, auth_source, profile, player, iso_date, seed, referer): (player, seed)
                for player, iso_date, seed, referer in jobs
            }
            for future in as_completed(futures):
                player, seed = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 详情页失败只影响单场，不能中断整轮补采。
                    errors.append({"stage": "detail", "player": player, "seed": seed, "error": str(exc)})
                if task:
                    task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    finally:
        if task:
            task.finish(f"ok={len(results)} err={len(errors)}")
    return results, errors


def _run_serial_detail_pages(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    jobs: list[tuple[dict[str, str], str, dict[str, Any], str]],
    task=None,
) -> tuple[list[DetailPageResult], list[dict[str, Any]]]:
    results: list[DetailPageResult] = []
    errors: list[dict[str, Any]] = []
    for player, iso_date, seed, referer in jobs:
        try:
            results.append(_fetch_detail_page(settings, auth_source, profile, player, iso_date, seed, referer))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "detail", "player": player, "seed": seed, "error": str(exc)})
        if task:
            task.advance(suffix=f"ok={len(results)} err={len(errors)}")
    return results, errors


def _fetch_detail_page(
    settings: Settings,
    auth_source: str,
    profile: FollowConcurrencyProfile,
    player: dict[str, str],
    iso_date: str,
    seed: dict[str, Any],
    referer: str,
) -> DetailPageResult:
    member = _thread_member_session(settings, auth_source)
    detail_html, final_detail_url = call_with_retries(
        lambda: member.fetch_text(seed["detail_url"], referer=referer),
        profile.retry_policy,
    )
    detail = parse_detail_html(detail_html, final_detail_url, settings.base_url, seed)
    return DetailPageResult(player=player, iso_date=iso_date, seed=seed, html=detail_html, final_url=final_detail_url, detail=detail)


def _thread_member_session(settings: Settings, auth_source: str):
    # worker 只负责请求和解析；每个线程独立 HTTP 会话，避免共享 CookieJar 的竞态。
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


def _existing_detail_is_complete(session, seed: dict[str, Any]) -> bool:
    alias = _detail_alias(seed)
    if not alias:
        return False
    alias_row = session.scalar(select(MatchAlias).where(MatchAlias.alias == alias))
    if alias_row is None:
        return False
    return _match_has_complete_follow_detail(alias_row.match)


def _detail_alias(seed: dict[str, Any]) -> str:
    detail_url = str(seed.get("detail_url") or "")
    follow_id = str(seed.get("follow_id") or extract_follow_id(detail_url) or "")
    detail_t = str(seed.get("detail_t") or extract_detail_t(detail_url) or "")
    return f"d:{follow_id}:{detail_t}" if follow_id and detail_t else ""


def _match_has_complete_follow_detail(match: Match | None) -> bool:
    if match is None:
        return False
    if not match.version or not match.sides or not match.decks:
        return False
    if not match.result or match.result == "unknown":
        return False
    if not all(deck.units for deck in match.decks):
        return False
    # 详情页样本保留胜负、profile、城血和卡组；满足这些再跳过，避免把演武场轻量样本误当完整数据。
    return any(side.castle_rate for side in match.sides) and any(side.profile_json for side in match.sides)


def parse_collect_dates(date_value: str | None, from_value: str | None, to_value: str | None) -> tuple[str, str]:
    if date_value:
        datetime.strptime(date_value, "%Y-%m-%d")
        return date_value, date_value
    if not from_value or not to_value:
        raise ValueError("请提供 --date，或者同时提供 --from 和 --to")
    datetime.strptime(from_value, "%Y-%m-%d")
    datetime.strptime(to_value, "%Y-%m-%d")
    return from_value, to_value
