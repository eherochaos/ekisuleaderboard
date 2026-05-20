"""按日期范围聚合对局双方样本，生成卡组和卡牌环境分析。"""

from __future__ import annotations

import csv
import html
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from eiketsu_env.config import Settings
from eiketsu_env.db.models import AnalysisCardStat, AnalysisDeckStat, AnalysisRun, Match, MatchDeck
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.card_lookup import load_card_lookup
from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.utils import utc_now

DEFAULT_DECK_MIN_SAMPLES = 3
DEFAULT_CARD_MIN_SAMPLES = 10
DEFAULT_HIGH_RANKER_RANK = 100
DEFAULT_VISUAL_DECK_LIMIT = 30
DEFAULT_ARCHETYPE_LIMIT = 30
DEFAULT_ARCHETYPE_SIMILAR_COST = 5.0
HIGH_WIN_RATE = 0.55
SCOPE_ALL_PLAYERS = "all_players"
SCOPE_ALL_RANKER = "all_ranker"
VERSION_ALL = "all_versions"


@dataclass(slots=True)
class AnalysisResult:
    run_id: int
    status: str
    counts: dict[str, Any]


@dataclass(slots=True)
class SideSample:
    match_id: int
    mode: str
    version: str
    result: str
    rank: int | None
    deck_fingerprint: str
    card_hashes: list[str]
    own_castle_rate: float | None
    opponent_castle_rate: float | None
    castle_damage_dealt: float | None
    castle_damage_taken: float | None
    kill_count: float | None
    death_count: float | None

    @property
    def castle_diff(self) -> float | None:
        if self.own_castle_rate is None or self.opponent_castle_rate is None:
            return None
        return self.own_castle_rate - self.opponent_castle_rate

    @property
    def castle_crash(self) -> bool:
        return self.opponent_castle_rate is not None and self.opponent_castle_rate <= 0

    @property
    def castle_crashed(self) -> bool:
        return self.own_castle_rate is not None and self.own_castle_rate <= 0


@dataclass(slots=True)
class StatBucket:
    sample_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    draw_count: int = 0
    castle_diffs: list[float] = field(default_factory=list)
    own_castle_rates: list[float] = field(default_factory=list)
    castle_damage_dealt_values: list[float] = field(default_factory=list)
    castle_damage_taken_values: list[float] = field(default_factory=list)
    kill_counts: list[float] = field(default_factory=list)
    death_counts: list[float] = field(default_factory=list)
    castle_crash_count: int = 0
    castle_crashed_count: int = 0

    def add(self, sample: SideSample) -> None:
        self.sample_count += 1
        if sample.result == "win":
            self.win_count += 1
        elif sample.result == "loss":
            self.loss_count += 1
        elif sample.result == "draw":
            self.draw_count += 1
        if sample.castle_diff is not None:
            self.castle_diffs.append(sample.castle_diff)
        if sample.own_castle_rate is not None:
            self.own_castle_rates.append(sample.own_castle_rate)
        if sample.castle_damage_dealt is not None:
            self.castle_damage_dealt_values.append(sample.castle_damage_dealt)
        if sample.castle_damage_taken is not None:
            self.castle_damage_taken_values.append(sample.castle_damage_taken)
        if sample.kill_count is not None:
            self.kill_counts.append(sample.kill_count)
        if sample.death_count is not None:
            self.death_counts.append(sample.death_count)
        if sample.castle_crash:
            self.castle_crash_count += 1
        if sample.castle_crashed:
            self.castle_crashed_count += 1

    @property
    def win_rate(self) -> float | None:
        denominator = self.win_count + self.loss_count
        return self.win_count / denominator if denominator else None

    @property
    def avg_castle_diff(self) -> float | None:
        return _avg(self.castle_diffs)

    @property
    def avg_own_castle_rate(self) -> float | None:
        return _avg(self.own_castle_rates)

    @property
    def avg_castle_damage_dealt(self) -> float | None:
        return _avg(self.castle_damage_dealt_values)

    @property
    def avg_castle_damage_taken(self) -> float | None:
        return _avg(self.castle_damage_taken_values)

    @property
    def avg_kill_count(self) -> float | None:
        return _avg(self.kill_counts)

    @property
    def avg_death_count(self) -> float | None:
        return _avg(self.death_counts)


@dataclass(slots=True)
class DeckStatSnapshot:
    sample_count: int
    win_count: int
    loss_count: int
    draw_count: int
    avg_castle_diff: float | None = None
    castle_crash_count: int = 0
    castle_crashed_count: int = 0

    @property
    def win_rate(self) -> float | None:
        denominator = self.win_count + self.loss_count
        return self.win_count / denominator if denominator else None


@dataclass(slots=True)
class DeckArchetype:
    representative: AnalysisDeckStat
    members: list[AnalysisDeckStat]
    summary: DeckStatSnapshot
    core_hashes: list[str]


def refresh_analysis(
    settings: Settings,
    date_from: str,
    date_to: str,
    deck_min_samples: int = DEFAULT_DECK_MIN_SAMPLES,
    card_min_samples: int = DEFAULT_CARD_MIN_SAMPLES,
    high_ranker_rank: int = DEFAULT_HIGH_RANKER_RANK,
    version: str = "",
) -> AnalysisResult:
    factory = make_session_factory(settings)
    with factory() as session:
        run = AnalysisRun(
            status="running",
            date_from=date_from,
            date_to=date_to,
            mode_scope_json=["全国対戦", "戦友対戦", "店内対戦"],
            thresholds_json={
                "deck_min_samples": deck_min_samples,
                "card_min_samples": card_min_samples,
                "high_ranker_rank": high_ranker_rank,
                "high_win_rate": HIGH_WIN_RATE,
                "target_version": version,
            },
            counts_json={},
            error_summary_json=[],
        )
        session.add(run)
        session.commit()

        matches = session.scalars(
            select(Match)
            .options(
                selectinload(Match.sides),
                selectinload(Match.battle_summary),
                selectinload(Match.decks).selectinload(MatchDeck.units),
            )
            .order_by(Match.played_at, Match.id)
        ).all()
        scoped = [
            match
            for match in matches
            if _in_date_range(match.played_at, date_from, date_to)
            and is_environment_mode(match.mode or "")
            and (not version or (match.version or "") == version)
        ]
        raw_samples = [_sample for match in scoped for _sample in _side_samples(match)]
        # 演武场等轻量来源可能只有卡组没有胜负；这些样本保留在库里，但不进入胜率/门槛统计。
        samples = [_sample for _sample in raw_samples if _sample.result != "unknown"]
        current_version = _current_version(scoped)
        deck_buckets = _deck_buckets(samples, high_ranker_rank)
        card_buckets = _card_buckets(samples, high_ranker_rank)
        base_scope = (SCOPE_ALL_PLAYERS, VERSION_ALL)
        base_deck_buckets = deck_buckets.get(base_scope, {})
        base_card_buckets = card_buckets.get(base_scope, {})
        high_win_decks_by_scope = {
            scope_key: {
                fingerprint
                for fingerprint, bucket in scoped_buckets.items()
                if bucket.sample_count >= deck_min_samples and bucket.win_rate is not None and bucket.win_rate >= HIGH_WIN_RATE
            }
            for scope_key, scoped_buckets in deck_buckets.items()
        }

        for (sample_scope, version_scope), scoped_buckets in deck_buckets.items():
            for fingerprint, bucket in scoped_buckets.items():
                if bucket.sample_count < deck_min_samples:
                    continue
                session.add(_deck_stat(run, sample_scope, version_scope, fingerprint, bucket))

        for scope_key, scoped_buckets in card_buckets.items():
            sample_scope, version_scope = scope_key
            high_win_decks = high_win_decks_by_scope.get(scope_key, set())
            for card_hash, bucket in scoped_buckets.items():
                if bucket.sample_count < card_min_samples:
                    continue
                session.add(_card_stat(run, sample_scope, version_scope, card_hash, bucket, _high_win_deck_count(card_hash, high_win_decks)))

        run.status = "completed"
        run.finished_at = utc_now()
        run.counts_json = {
            "matches": len(scoped),
            "side_samples": len(samples),
            "raw_side_samples": len(raw_samples),
            "unknown_result_side_samples": len(raw_samples) - len(samples),
            "current_version": current_version,
            "deck_groups": len(base_deck_buckets),
            "deck_groups_exported": sum(1 for bucket in base_deck_buckets.values() if bucket.sample_count >= deck_min_samples),
            "card_groups": len(base_card_buckets),
            "card_groups_exported": sum(1 for bucket in base_card_buckets.values() if bucket.sample_count >= card_min_samples),
            "scoped_deck_groups": sum(len(scoped_buckets) for scoped_buckets in deck_buckets.values()),
            "scoped_card_groups": sum(len(scoped_buckets) for scoped_buckets in card_buckets.values()),
            "mode_counts": dict(Counter(match.mode or "" for match in scoped)),
            "version_counts": dict(Counter(match.version or "" for match in scoped if match.version)),
            "target_version": version,
            "sample_scope_counts": _sample_scope_counts(samples, high_ranker_rank),
            "high_ranker_rank_limit": high_ranker_rank,
            "result_counts": dict(Counter(sample.result for sample in samples)),
        }
        session.commit()
        return AnalysisResult(run.id, run.status, run.counts_json)


def export_analysis(settings: Settings, report: str, output_format: str, output: Path | None = None) -> Path:
    factory = make_session_factory(settings)
    with factory() as session:
        run = _latest_run(session)
        if run is None:
            raise RuntimeError("没有可导出的分析批次，请先运行 analyze refresh")
        settings.exports_dir.mkdir(parents=True, exist_ok=True)
        if output is None:
            output = settings.exports_dir / f"analysis_{report}.{output_format}"
        lookup = load_card_lookup(settings)

        if report == "overview":
            rows = _overview_rows(run)
            return _write_rows_or_markdown(output, output_format, rows, f"环境分析概览 run #{run.id}")
        if report == "deck":
            rows = _deck_rows(run, lookup)
            return _write_rows_or_markdown(output, output_format, rows, f"卡组统计 run #{run.id}")
        if report == "deck-visual":
            return _write_deck_visual_html(output, output_format, run, lookup)
        if report == "deck-archetype-visual":
            return _write_deck_archetype_visual_html(output, output_format, run, lookup)
        if report == "card":
            rows = _card_rows(run, lookup)
            return _write_rows_or_markdown(output, output_format, rows, f"卡牌随组表现 run #{run.id}")
        if report == "deck-version":
            rows = _deck_version_rows(run, lookup)
            return _write_rows_or_markdown(output, output_format, rows, f"卡组版本历史 run #{run.id}")
        if report == "card-version":
            rows = _card_version_rows(run, lookup)
            return _write_rows_or_markdown(output, output_format, rows, f"卡牌版本历史 run #{run.id}")
    raise ValueError(f"不支持的分析报告：{report}")


def _side_samples(match: Match) -> list[SideSample]:
    sides = {side.side_index: side for side in match.sides}
    decks = {deck.side_index: deck for deck in match.decks}
    samples: list[SideSample] = []
    for side_index in (1, 2):
        side = sides.get(side_index)
        deck = decks.get(side_index)
        opponent = sides.get(2 if side_index == 1 else 1)
        if side is None or deck is None or not deck.deck_fingerprint:
            continue
        result = _normalize_result(match.result if side_index == 1 else _reverse_result(match.result))
        card_hashes = [unit.card_hash for unit in deck.units if unit.card_hash]
        own_castle_rate = _parse_rate(side.castle_rate)
        opponent_castle_rate = _parse_rate(opponent.castle_rate if opponent else "")
        castle_damage_dealt, castle_damage_taken = _castle_damage_for_side(
            match.battle_summary.castle_breakdown_json if match.battle_summary else {},
            side_index,
        )
        castle_damage_dealt = castle_damage_dealt if castle_damage_dealt is not None else _damage_from_remaining_rate(opponent_castle_rate)
        castle_damage_taken = castle_damage_taken if castle_damage_taken is not None else _damage_from_remaining_rate(own_castle_rate)
        samples.append(
            SideSample(
                match_id=match.id,
                mode=match.mode or "",
                version=match.version or "",
                result=result,
                rank=_parse_rank(side.profile_json),
                deck_fingerprint=deck.deck_fingerprint,
                card_hashes=card_hashes,
                own_castle_rate=own_castle_rate,
                opponent_castle_rate=opponent_castle_rate,
                castle_damage_dealt=castle_damage_dealt,
                castle_damage_taken=castle_damage_taken,
                kill_count=_battle_stat_total(side.profile_json, "kill_count"),
                death_count=_battle_stat_total(side.profile_json, "death_count"),
            )
        )
    return samples


def _deck_buckets(samples: list[SideSample], high_ranker_rank: int) -> dict[tuple[str, str], dict[str, StatBucket]]:
    buckets: dict[tuple[str, str], dict[str, StatBucket]] = defaultdict(lambda: defaultdict(StatBucket))
    for sample in samples:
        for sample_scope in _sample_scopes(sample, high_ranker_rank):
            for version_scope in _version_scopes(sample):
                buckets[(sample_scope, version_scope)][sample.deck_fingerprint].add(sample)
    return {scope_key: dict(scoped_buckets) for scope_key, scoped_buckets in buckets.items()}


def _card_buckets(samples: list[SideSample], high_ranker_rank: int) -> dict[tuple[str, str], dict[str, StatBucket]]:
    buckets: dict[tuple[str, str], dict[str, StatBucket]] = defaultdict(lambda: defaultdict(StatBucket))
    for sample in samples:
        # 同一卡同 side 只计一次，避免异常重复 slot 放大卡牌样本。
        for card_hash in set(sample.card_hashes):
            for sample_scope in _sample_scopes(sample, high_ranker_rank):
                for version_scope in _version_scopes(sample):
                    buckets[(sample_scope, version_scope)][card_hash].add(sample)
    return {scope_key: dict(scoped_buckets) for scope_key, scoped_buckets in buckets.items()}


def _deck_stat(run: AnalysisRun, sample_scope: str, version_scope: str, fingerprint: str, bucket: StatBucket) -> AnalysisDeckStat:
    return AnalysisDeckStat(
        analysis_run=run,
        sample_scope=sample_scope,
        version_scope=version_scope,
        deck_fingerprint=fingerprint,
        sample_count=bucket.sample_count,
        win_count=bucket.win_count,
        loss_count=bucket.loss_count,
        draw_count=bucket.draw_count,
        win_rate=bucket.win_rate,
        avg_castle_diff=bucket.avg_castle_diff,
        avg_own_castle_rate=bucket.avg_own_castle_rate,
        avg_castle_damage_dealt=bucket.avg_castle_damage_dealt,
        avg_castle_damage_taken=bucket.avg_castle_damage_taken,
        avg_kill_count=bucket.avg_kill_count,
        avg_death_count=bucket.avg_death_count,
        castle_crash_count=bucket.castle_crash_count,
        castle_crashed_count=bucket.castle_crashed_count,
    )


def _card_stat(
    run: AnalysisRun,
    sample_scope: str,
    version_scope: str,
    card_hash: str,
    bucket: StatBucket,
    high_win_deck_count: int,
) -> AnalysisCardStat:
    return AnalysisCardStat(
        analysis_run=run,
        sample_scope=sample_scope,
        version_scope=version_scope,
        card_hash=card_hash,
        sample_count=bucket.sample_count,
        win_count=bucket.win_count,
        loss_count=bucket.loss_count,
        draw_count=bucket.draw_count,
        win_rate=bucket.win_rate,
        avg_castle_diff=bucket.avg_castle_diff,
        avg_own_castle_rate=bucket.avg_own_castle_rate,
        avg_castle_damage_dealt=bucket.avg_castle_damage_dealt,
        avg_castle_damage_taken=bucket.avg_castle_damage_taken,
        avg_kill_count=bucket.avg_kill_count,
        avg_death_count=bucket.avg_death_count,
        high_win_deck_count=high_win_deck_count,
    )


def _high_win_deck_count(card_hash: str, high_win_decks: set[str]) -> int:
    return sum(1 for fingerprint in high_win_decks if card_hash in fingerprint.split(","))


def _high_ranker_scope(high_ranker_rank: int) -> str:
    return f"high_ranker_top{high_ranker_rank}"


def _sample_scopes(sample: SideSample, high_ranker_rank: int) -> list[str]:
    scopes = [SCOPE_ALL_PLAYERS]
    if sample.rank is not None:
        scopes.append(SCOPE_ALL_RANKER)
        if sample.rank <= high_ranker_rank:
            scopes.append(_high_ranker_scope(high_ranker_rank))
    return scopes


def _version_scopes(sample: SideSample) -> list[str]:
    scopes = [VERSION_ALL]
    if sample.version:
        scopes.append(sample.version)
    return scopes


def _sample_scope_counts(samples: list[SideSample], high_ranker_rank: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in samples:
        for sample_scope in _sample_scopes(sample, high_ranker_rank):
            counts[sample_scope] += 1
    return dict(counts)


def _current_version(matches: list[Match]) -> str:
    for match in sorted(matches, key=lambda item: (item.played_at or "", item.id or 0), reverse=True):
        if match.version:
            return match.version
    return ""


def _latest_run(session) -> AnalysisRun | None:
    return session.scalar(select(AnalysisRun).where(AnalysisRun.status == "completed").order_by(AnalysisRun.id.desc()))


def _overview_rows(run: AnalysisRun) -> list[dict[str, str]]:
    rows = [
        {"metric": "run_id", "value": str(run.id)},
        {"metric": "date_from", "value": run.date_from},
        {"metric": "date_to", "value": run.date_to},
    ]
    for key, value in (run.counts_json or {}).items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                rows.append({"metric": f"{key}.{sub_key}", "value": str(sub_value)})
        else:
            rows.append({"metric": key, "value": str(value)})
    return rows


def _deck_rows(run: AnalysisRun, lookup) -> list[dict[str, str]]:
    rows = []
    stats_by_key = _deck_stat_lookup(run.deck_stats)
    stats = _sorted_base_stats(run.deck_stats)
    current_version = str((run.counts_json or {}).get("current_version") or "")
    high_ranker_scope = _run_high_ranker_scope(run)
    for stat in stats:
        row = _deck_base_row(stat, lookup)
        row.update(_comparison_columns(SCOPE_ALL_RANKER, stats_by_key.get((SCOPE_ALL_RANKER, VERSION_ALL, stat.deck_fingerprint))))
        row.update(_comparison_columns(high_ranker_scope, stats_by_key.get((high_ranker_scope, VERSION_ALL, stat.deck_fingerprint))))
        row["current_version"] = current_version
        current_stat = stats_by_key.get((SCOPE_ALL_PLAYERS, current_version, stat.deck_fingerprint)) if current_version else None
        row.update(_comparison_columns("current_version", current_stat))
        rows.append(row)
    return rows


def _card_rows(run: AnalysisRun, lookup) -> list[dict[str, str]]:
    rows = []
    stats_by_key = _card_stat_lookup(run.card_stats)
    stats = _sorted_base_stats(run.card_stats)
    current_version = str((run.counts_json or {}).get("current_version") or "")
    high_ranker_scope = _run_high_ranker_scope(run)
    for stat in stats:
        row = _card_base_row(stat, lookup)
        row.update(_comparison_columns(SCOPE_ALL_RANKER, stats_by_key.get((SCOPE_ALL_RANKER, VERSION_ALL, stat.card_hash))))
        row.update(_comparison_columns(high_ranker_scope, stats_by_key.get((high_ranker_scope, VERSION_ALL, stat.card_hash))))
        row["current_version"] = current_version
        current_stat = stats_by_key.get((SCOPE_ALL_PLAYERS, current_version, stat.card_hash)) if current_version else None
        row.update(_comparison_columns("current_version", current_stat))
        rows.append(row)
    return rows


def _deck_version_rows(run: AnalysisRun, lookup) -> list[dict[str, str]]:
    rows = []
    stats_by_key = _deck_stat_lookup(run.deck_stats)
    versions = _versions_for_stats(run, run.deck_stats)
    for stat in _sorted_base_stats(run.deck_stats):
        row = _deck_base_row(stat, lookup)
        for version in versions:
            row.update(_comparison_columns(version, stats_by_key.get((SCOPE_ALL_PLAYERS, version, stat.deck_fingerprint))))
        rows.append(row)
    return rows


def _card_version_rows(run: AnalysisRun, lookup) -> list[dict[str, str]]:
    rows = []
    stats_by_key = _card_stat_lookup(run.card_stats)
    versions = _versions_for_stats(run, run.card_stats)
    for stat in _sorted_base_stats(run.card_stats):
        row = _card_base_row(stat, lookup)
        for version in versions:
            row.update(_comparison_columns(version, stats_by_key.get((SCOPE_ALL_PLAYERS, version, stat.card_hash))))
        rows.append(row)
    return rows


def _write_deck_visual_html(output: Path, output_format: str, run: AnalysisRun, lookup) -> Path:
    if output_format != "html":
        raise ValueError("deck-visual 只支持 html 导出格式")

    output.parent.mkdir(parents=True, exist_ok=True)
    asset_dir = output.parent / f"{output.stem}_assets" / "cards"
    stats = _sorted_base_stats(run.deck_stats)[:DEFAULT_VISUAL_DECK_LIMIT]
    stats_by_key = _deck_stat_lookup(run.deck_stats)
    current_version = str((run.counts_json or {}).get("current_version") or "")
    high_ranker_scope = _run_high_ranker_scope(run)
    title = f"卡组图文报告 run #{run.id}"
    html_text = "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-Hans">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_html(title)}</title>",
            "<style>",
            _deck_visual_css(),
            "</style>",
            "</head>",
            "<body>",
            '<main class="page">',
            _deck_visual_header(run, title, current_version),
            _deck_visual_sections(stats, stats_by_key, lookup, high_ranker_scope, current_version, output.parent, asset_dir),
            _visual_sort_script(),
            "</main>",
            "</body>",
            "</html>",
        ]
    )
    output.write_text(html_text + "\n", encoding="utf-8")
    return output


def _write_deck_archetype_visual_html(output: Path, output_format: str, run: AnalysisRun, lookup) -> Path:
    if output_format != "html":
        raise ValueError("deck-archetype-visual 只支持 html 导出格式")

    output.parent.mkdir(parents=True, exist_ok=True)
    asset_dir = output.parent / f"{output.stem}_assets" / "cards"
    stats_by_key = _deck_stat_lookup(run.deck_stats)
    base_stats = _sorted_base_stats(run.deck_stats)
    archetypes = _deck_archetypes(base_stats, lookup, DEFAULT_ARCHETYPE_SIMILAR_COST)[:DEFAULT_ARCHETYPE_LIMIT]
    current_version = str((run.counts_json or {}).get("current_version") or "")
    high_ranker_scope = _run_high_ranker_scope(run)
    title = f"卡组分类报告 run #{run.id}"
    html_text = "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-Hans">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_html(title)}</title>",
            "<style>",
            _deck_archetype_visual_css(),
            "</style>",
            "</head>",
            "<body>",
            '<main class="page archetype-page">',
            _deck_archetype_header(run, title, len(base_stats), len(archetypes), current_version),
            _deck_archetype_sections(archetypes, stats_by_key, lookup, high_ranker_scope, current_version, output.parent, asset_dir),
            _deck_archetype_script(),
            _visual_sort_script(),
            "</main>",
            "</body>",
            "</html>",
        ]
    )
    output.write_text(html_text + "\n", encoding="utf-8")
    return output


def _deck_archetypes(stats: list[AnalysisDeckStat], lookup, similar_cost_threshold: float) -> list[DeckArchetype]:
    clusters: list[list[AnalysisDeckStat]] = []
    representatives: list[AnalysisDeckStat] = []
    cost_maps: dict[str, dict[str, float]] = {}

    for stat in stats:
        cost_maps[stat.deck_fingerprint] = _deck_cost_map(stat.deck_fingerprint, lookup)
        target_index: int | None = None
        # 用代表构筑吸附，避免“链式相似”把边缘构筑一路并成过大的类。
        for index, representative in enumerate(representatives):
            if _similar_cost(cost_maps[stat.deck_fingerprint], cost_maps[representative.deck_fingerprint]) >= similar_cost_threshold:
                target_index = index
                break
        if target_index is None:
            representatives.append(stat)
            clusters.append([stat])
        else:
            clusters[target_index].append(stat)

    archetypes = [
        DeckArchetype(
            representative=members[0],
            members=members,
            summary=_aggregate_deck_stat_snapshot(members),
            core_hashes=_archetype_core_hashes(members, lookup, similar_cost_threshold),
        )
        for members in clusters
        if members
    ]
    return sorted(
        archetypes,
        key=lambda item: (_wilson_lower_bound(item.summary.win_count, item.summary.loss_count) or 0, item.summary.sample_count),
        reverse=True,
    )


def _deck_cost_map(fingerprint: str, lookup) -> dict[str, float]:
    return {card_hash: lookup.cost_value(card_hash) for card_hash in fingerprint.split(",") if card_hash}


def _similar_cost(left: dict[str, float], right: dict[str, float]) -> float:
    return sum(max(left.get(card_hash, 0.0), right.get(card_hash, 0.0)) for card_hash in left.keys() & right.keys())


def _aggregate_deck_stat_snapshot(stats: list[AnalysisDeckStat]) -> DeckStatSnapshot:
    sample_count = sum(stat.sample_count for stat in stats)
    return DeckStatSnapshot(
        sample_count=sample_count,
        win_count=sum(stat.win_count for stat in stats),
        loss_count=sum(stat.loss_count for stat in stats),
        draw_count=sum(stat.draw_count for stat in stats),
        avg_castle_diff=_weighted_stat_avg(stats, "avg_castle_diff"),
        castle_crash_count=sum(stat.castle_crash_count for stat in stats),
        castle_crashed_count=sum(stat.castle_crashed_count for stat in stats),
    )


def _aggregate_scoped_deck_stats(
    members: list[AnalysisDeckStat],
    stats_by_key: dict[tuple[str, str, str], AnalysisDeckStat],
    sample_scope: str,
    version_scope: str,
) -> DeckStatSnapshot | None:
    scoped_stats = [
        stat
        for member in members
        if (stat := stats_by_key.get((sample_scope, version_scope, member.deck_fingerprint))) is not None
    ]
    return _aggregate_deck_stat_snapshot(scoped_stats) if scoped_stats else None


def _weighted_stat_avg(stats: list[AnalysisDeckStat], attr_name: str) -> float | None:
    weighted_values = [
        (value, stat.sample_count)
        for stat in stats
        if (value := getattr(stat, attr_name, None)) is not None and stat.sample_count > 0
    ]
    total_weight = sum(weight for _, weight in weighted_values)
    return sum(value * weight for value, weight in weighted_values) / total_weight if total_weight else None


def _archetype_core_hashes(stats: list[AnalysisDeckStat], lookup, target_cost: float) -> list[str]:
    weighted_counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for stat in stats:
        for card_hash in stat.deck_fingerprint.split(","):
            if not card_hash:
                continue
            weighted_counts[card_hash] += stat.sample_count
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


def _deck_archetype_header(run: AnalysisRun, title: str, deck_count: int, archetype_count: int, current_version: str) -> str:
    counts = run.counts_json or {}
    return "\n".join(
        [
            '<header class="report-header">',
            '<div class="title-block">',
            f"<p>Archetype / { _html(run.date_from) } - { _html(run.date_to) }</p>",
            f"<h1>{_html(title)}</h1>",
            "</div>",
            '<dl class="summary">',
            _summary_item("有效 side 样本", counts.get("side_samples", "")),
            _summary_item("完整卡组", deck_count),
            _summary_item("输出分类", archetype_count),
            _summary_item("阈值", f"共同 Cost ≥ {DEFAULT_ARCHETYPE_SIMILAR_COST:.1f}"),
            _summary_item("当前版本", current_version or "未识别"),
            _summary_item("聚类", "代表构筑吸附"),
            "</dl>",
            "</header>",
        ]
    )


def _visual_sort_toolbar(target_id: str) -> str:
    return "\n".join(
        [
            f'<div class="sort-toolbar" data-sort-toolbar data-sort-target="{_html(target_id)}">',
            '<span>排序规则</span>',
            '<button type="button" data-sort-button data-sort-key="wilson" aria-pressed="true">Wilson 下限</button>',
            '<button type="button" data-sort-button data-sort-key="sample" aria-pressed="false">样本量</button>',
            "</div>",
        ]
    )


def _sort_item_attrs(title: str, wilson: float | None, sample_count: int) -> str:
    # 静态 HTML 没有后端参与，排序所需指标直接写进每个排行项的 data 属性。
    wilson_value = 0.0 if wilson is None else wilson
    return (
        "data-sort-item "
        f'data-sort-wilson="{wilson_value:.8f}" '
        f'data-sort-sample="{sample_count}" '
        f'data-sort-title="{_html(title)}"'
    )


def _deck_archetype_sections(
    archetypes: list[DeckArchetype],
    stats_by_key: dict[tuple[str, str, str], AnalysisDeckStat],
    lookup,
    high_ranker_scope: str,
    current_version: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    if not archetypes:
        return '<section class="empty">暂无可展示卡组分类。</section>'

    # 所有名次都使用同一种 row 样式，避免 Top3 在视觉上被额外放大。
    rows = []
    for index, archetype in enumerate(archetypes, start=1):
        current_stat = (
            _aggregate_scoped_deck_stats(archetype.members, stats_by_key, SCOPE_ALL_PLAYERS, current_version)
            if current_version
            else None
        )
        high_ranker_stat = _aggregate_scoped_deck_stats(archetype.members, stats_by_key, high_ranker_scope, VERSION_ALL)
        all_ranker_stat = _aggregate_scoped_deck_stats(archetype.members, stats_by_key, SCOPE_ALL_RANKER, VERSION_ALL)
        rows.append(
            _deck_archetype_rank_row(
                index,
                archetype,
                lookup,
                all_ranker_stat,
                high_ranker_stat,
                current_stat,
                high_ranker_scope,
                output_dir,
                asset_dir,
            )
        )

    blocks = [
        _visual_sort_toolbar("archetype-ranking"),
        '<section class="archetype-board" id="archetype-ranking" data-sort-root>',
        '<div class="board-head">',
        "<span>Rank</span><span>Archetype</span><span>Signal</span>",
        "</div>",
        *rows,
        "</section>",
    ]
    return "\n".join(blocks)


def _deck_archetype_feature_card(
    index: int,
    archetype: DeckArchetype,
    lookup,
    high_ranker_stat: DeckStatSnapshot | None,
    current_stat: DeckStatSnapshot | None,
    current_version: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    summary = archetype.summary
    return "\n".join(
        [
            f'<article class="archetype-card archetype-card-{index}">',
            '<div class="archetype-meta">',
            f'<span class="rank-badge">#{index}</span>',
            f'<span>{len(archetype.members)} 个构筑</span>',
            f'<span>{summary.win_count}W / {summary.loss_count}L</span>',
            "</div>",
            f"<h2>{_html(_archetype_title(archetype, lookup))}</h2>",
            _archetype_variant_viewer(archetype, lookup, output_dir, asset_dir),
            '<div class="feature-stats">',
            _score_pill("Wilson", _fmt_rate(_wilson_lower_bound(summary.win_count, summary.loss_count))),
            _score_pill("胜率", _fmt_rate(summary.win_rate)),
            _score_pill("样本", summary.sample_count),
            _score_pill("Top100", _compact_scope_value(high_ranker_stat)),
            _score_pill("当前", _compact_scope_value(current_stat)),
            "</div>",
            f"<p class=\"commentary\">{_html(_archetype_commentary(archetype, high_ranker_stat, current_stat, current_version))}</p>",
            "</article>",
        ]
    )


def _deck_archetype_rank_row(
    index: int,
    archetype: DeckArchetype,
    lookup,
    all_ranker_stat: DeckStatSnapshot | None,
    high_ranker_stat: DeckStatSnapshot | None,
    current_stat: DeckStatSnapshot | None,
    high_ranker_scope: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    summary = archetype.summary
    title = _archetype_title(archetype, lookup)
    sort_attrs = _sort_item_attrs(title, _wilson_lower_bound(summary.win_count, summary.loss_count), summary.sample_count)
    high_ranker_label = high_ranker_scope.replace("high_ranker_top", "Top")
    return "\n".join(
        [
            f'<article class="archetype-row" {sort_attrs}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f"<span>{len(archetype.members)} 个构筑</span>",
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(title)}</h3>",
            _archetype_variant_viewer(archetype, lookup, output_dir, asset_dir),
            "</div>",
            '<div class="row-signals">',
            _score_pill("Wilson", _fmt_rate(_wilson_lower_bound(summary.win_count, summary.loss_count))),
            _score_pill("胜率", _fmt_rate(summary.win_rate)),
            _score_pill("样本", summary.sample_count),
            _score_pill("Ranker", _compact_scope_value(all_ranker_stat)),
            _score_pill(high_ranker_label, _compact_scope_value(high_ranker_stat)),
            _score_pill("当前", _compact_scope_value(current_stat)),
            "</div>",
            "</article>",
        ]
    )


def _archetype_title(archetype: DeckArchetype, lookup) -> str:
    names = [lookup.label(card_hash).split("(", 1)[0] for card_hash in archetype.core_hashes[:3]]
    return " / ".join(names) + " 系" if names else "未识别卡组系"


def _archetype_variant_viewer(archetype: DeckArchetype, lookup, output_dir: Path, asset_dir: Path) -> str:
    variants = [_archetype_variant(member, lookup, output_dir, asset_dir, index) for index, member in enumerate(archetype.members)]
    button = (
        '<button type="button" class="variant-button" data-variant-button>Change</button>'
        if len(variants) > 1
        else '<span class="variant-single">Single</span>'
    )
    return "\n".join(
        [
            '<div class="variant-viewer" data-variant-root>',
            '<div class="variant-toolbar">',
            '<span class="variant-label" data-variant-label>代表构筑</span>',
            button,
            "</div>",
            '<div class="variant-stage">',
            *variants,
            "</div>",
            "</div>",
        ]
    )


def _archetype_variant(member: AnalysisDeckStat, lookup, output_dir: Path, asset_dir: Path, index: int) -> str:
    hashes = _card_hashes_by_cost_desc(member.deck_fingerprint.split(",") if member.deck_fingerprint else [], lookup)
    active_class = " is-active" if index == 0 else ""
    return "\n".join(
        [
            f'<div class="variant{active_class}" data-variant data-variant-index="{index}">',
            f'<div class="variant-cards">{"".join(_unit_figure(card_hash, lookup, output_dir, asset_dir) for card_hash in hashes)}</div>',
            f'<p class="variant-name">{_html(" / ".join(lookup.names(hashes)))}</p>',
            '<div class="variant-statline">',
            f"<span>{member.sample_count} 样本</span>",
            f"<span>{_fmt_rate(member.win_rate) or '-'} 胜率</span>",
            f"<span>{_fmt_rate(_wilson_lower_bound(member.win_count, member.loss_count)) or '-'} Wilson</span>",
            "</div>",
            "</div>",
        ]
    )


def _archetype_member_list(archetype: DeckArchetype, lookup, limit: int) -> str:
    rows = []
    for member in archetype.members[:limit]:
        hashes = _card_hashes_by_cost_desc(member.deck_fingerprint.split(",") if member.deck_fingerprint else [], lookup)
        rows.append(
            "<li>"
            f"<span>{_html(' / '.join(lookup.names(hashes)))}</span>"
            f"<strong>{member.sample_count} / {_fmt_rate(member.win_rate)}</strong>"
            "</li>"
        )
    if len(archetype.members) > limit:
        rows.append(f"<li><span>其余 {len(archetype.members) - limit} 个构筑</span><strong>继续合并统计</strong></li>")
    return '<ul class="member-list">' + "".join(rows) + "</ul>"


def _archetype_commentary(
    archetype: DeckArchetype,
    high_ranker_stat: DeckStatSnapshot | None,
    current_stat: DeckStatSnapshot | None,
    current_version: str,
) -> str:
    summary = archetype.summary
    notes = [f"以共同 Cost ≥ {DEFAULT_ARCHETYPE_SIMILAR_COST:.1f} 聚合，包含 {len(archetype.members)} 个完整构筑。"]
    if summary.sample_count >= 80:
        notes.append("样本量已经比较可读，适合作为环境主轴观察。")
    elif summary.sample_count >= 30:
        notes.append("样本量中等，能降低单一替换卡造成的碎片化。")
    else:
        notes.append("样本仍偏少，先看趋势，不宜过度下结论。")
    wilson = _wilson_lower_bound(summary.win_count, summary.loss_count) or 0
    if wilson >= 0.6:
        notes.append("Wilson 下限较高，保守估计也有竞争力。")
    if high_ranker_stat and high_ranker_stat.sample_count >= 5:
        notes.append(f"Top100 口径 {high_ranker_stat.sample_count} 样本，胜率 {_fmt_rate(high_ranker_stat.win_rate) or '暂无'}。")
    if current_stat and current_stat.sample_count >= 3:
        notes.append(f"{current_version} 内 {current_stat.sample_count} 样本，胜率 {_fmt_rate(current_stat.win_rate) or '暂无'}。")
    return " ".join(notes)


def _deck_visual_header(run: AnalysisRun, title: str, current_version: str) -> str:
    counts = run.counts_json or {}
    return "\n".join(
        [
            '<header class="report-header">',
            '<div class="title-block">',
            f"<p>环境分析 / { _html(run.date_from) } - { _html(run.date_to) }</p>",
            f"<h1>{_html(title)}</h1>",
            "</div>",
            '<dl class="summary">',
            _summary_item("有效 side 样本", counts.get("side_samples", "")),
            _summary_item("对局数", counts.get("matches", "")),
            _summary_item("当前版本", current_version or "未识别"),
            _summary_item("展示", f"Top {DEFAULT_VISUAL_DECK_LIMIT}"),
            _summary_item("排序", "Wilson 下限"),
            "</dl>",
            "</header>",
        ]
    )


def _deck_visual_sections(
    stats: list[AnalysisDeckStat],
    stats_by_key: dict[tuple[str, str, str], AnalysisDeckStat],
    lookup,
    high_ranker_scope: str,
    current_version: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    if not stats:
        return '<section class="empty">暂无可展示卡组。</section>'

    # 排榜保持统一密度，排序切换时只需要重排同一组 row。
    rows = []
    for index, stat in enumerate(stats, start=1):
        all_ranker_stat = stats_by_key.get((SCOPE_ALL_RANKER, VERSION_ALL, stat.deck_fingerprint))
        high_ranker_stat = stats_by_key.get((high_ranker_scope, VERSION_ALL, stat.deck_fingerprint))
        current_stat = (
            stats_by_key.get((SCOPE_ALL_PLAYERS, current_version, stat.deck_fingerprint))
            if current_version
            else None
        )
        rows.append(
            _deck_visual_rank_row(
                index,
                stat,
                lookup,
                all_ranker_stat,
                high_ranker_stat,
                current_stat,
                high_ranker_scope,
                output_dir,
                asset_dir,
            )
        )

    blocks = [
        _visual_sort_toolbar("deck-ranking"),
        '<section class="ranking-board" id="deck-ranking" data-sort-root>',
        '<div class="board-head">',
        "<span>Rank</span><span>Deck</span><span>Signal</span>",
        "</div>",
        *rows,
        "</section>",
    ]
    return "\n".join(blocks)


def _deck_visual_item(
    index: int,
    stat: AnalysisDeckStat,
    lookup,
    all_ranker_stat: AnalysisDeckStat | None,
    high_ranker_stat: AnalysisDeckStat | None,
    current_stat: AnalysisDeckStat | None,
    high_ranker_scope: str,
    current_version: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    return (
        _deck_visual_feature_card(
            index,
            stat,
            lookup,
            high_ranker_stat,
            current_stat,
            current_version,
            output_dir,
            asset_dir,
        )
        if index <= 3
        else _deck_visual_rank_row(
            index,
            stat,
            lookup,
            all_ranker_stat,
            high_ranker_stat,
            current_stat,
            high_ranker_scope,
            output_dir,
            asset_dir,
        )
    )


def _deck_visual_feature_card(
    index: int,
    stat: AnalysisDeckStat,
    lookup,
    high_ranker_stat: AnalysisDeckStat | None,
    current_stat: AnalysisDeckStat | None,
    current_version: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    hashes = _card_hashes_by_cost_desc(stat.deck_fingerprint.split(",") if stat.deck_fingerprint else [], lookup)
    deck_name = " / ".join(lookup.names(hashes))
    return "\n".join(
        [
            f'<article class="feature-card feature-card-{index}">',
            '<div class="feature-meta">',
            f'<span class="rank-badge">#{index}</span>',
            f'<span>{stat.win_count}W / {stat.loss_count}L</span>',
            "</div>",
            f"<h2>{_html(deck_name)}</h2>",
            f'<div class="feature-cards">{"".join(_unit_figure(card_hash, lookup, output_dir, asset_dir) for card_hash in hashes)}</div>',
            '<div class="feature-stats">',
            _score_pill("Wilson", _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count))),
            _score_pill("胜率", _fmt_rate(stat.win_rate)),
            _score_pill("样本", stat.sample_count),
            _score_pill("Top100", _compact_scope_value(high_ranker_stat)),
            "</div>",
            f"<p class=\"commentary\">{_html(_deck_visual_commentary(stat, high_ranker_stat, current_stat, current_version))}</p>",
            "</article>",
        ]
    )


def _deck_visual_rank_row(
    index: int,
    stat: AnalysisDeckStat,
    lookup,
    all_ranker_stat: AnalysisDeckStat | None,
    high_ranker_stat: AnalysisDeckStat | None,
    current_stat: AnalysisDeckStat | None,
    high_ranker_scope: str,
    output_dir: Path,
    asset_dir: Path,
) -> str:
    hashes = _card_hashes_by_cost_desc(stat.deck_fingerprint.split(",") if stat.deck_fingerprint else [], lookup)
    deck_name = " / ".join(lookup.names(hashes))
    sort_attrs = _sort_item_attrs(deck_name, _wilson_lower_bound(stat.win_count, stat.loss_count), stat.sample_count)
    high_ranker_label = high_ranker_scope.replace("high_ranker_top", "Top")
    return "\n".join(
        [
            f'<article class="rank-row" {sort_attrs}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span>{stat.win_count}W {stat.loss_count}L</span>',
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(deck_name)}</h3>",
            f'<div class="card-strip">{"".join(_unit_figure(card_hash, lookup, output_dir, asset_dir) for card_hash in hashes)}</div>',
            "</div>",
            '<div class="row-signals">',
            _score_pill("Wilson", _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count))),
            _score_pill("胜率", _fmt_rate(stat.win_rate)),
            _score_pill("样本", stat.sample_count),
            _score_pill("Ranker", _compact_scope_value(all_ranker_stat)),
            _score_pill(high_ranker_label, _compact_scope_value(high_ranker_stat)),
            _score_pill("当前", _compact_scope_value(current_stat)),
            "</div>",
            "</article>",
        ]
    )


def _card_hashes_by_cost_desc(card_hashes: list[str], lookup) -> list[str]:
    # 卡组展示必须按 Cost 从高到低，便于一眼看出主力武将与低 Cost 拼图。
    indexed_hashes = [(index, card_hash) for index, card_hash in enumerate(card_hashes) if card_hash]
    return [
        card_hash
        for index, card_hash in sorted(
            indexed_hashes,
            key=lambda item: (-lookup.cost_value(item[1]), item[0]),
        )
    ]


def _unit_figure(card_hash: str, lookup, output_dir: Path, asset_dir: Path) -> str:
    label = lookup.label(card_hash)
    image_src = _unit_image_src(card_hash, lookup, output_dir, asset_dir)
    if image_src:
        media = f'<img src="{_html(image_src)}" alt="{_html(label)}" loading="lazy">'
    else:
        media = f'<div class="image-placeholder">{_html(_short_card_label(label))}</div>'
    return "\n".join(
        [
            '<figure class="unit">',
            media,
            f"<figcaption>{_html(label)}</figcaption>",
            "</figure>",
        ]
    )


def _unit_image_src(card_hash: str, lookup, output_dir: Path, asset_dir: Path) -> str:
    local_path = lookup.local_image_path(card_hash)
    if local_path is not None:
        asset_dir.mkdir(parents=True, exist_ok=True)
        target = asset_dir / local_path.name
        # 官网图片链接会随 base 快照失效；图文报告优先固化本地缓存，打开文件时也能稳定显示。
        if not target.exists() or target.stat().st_size != local_path.stat().st_size:
            shutil.copy2(local_path, target)
        return _relative_url(target, output_dir)
    return _safe_remote_image_url(lookup.image_url(card_hash))


def _safe_remote_image_url(image_url: str) -> str:
    # base 快照里的官网图片 URL 可能随时间 404；没有本地缓存时宁可显示占位，避免报告里出现破图。
    if "eiketsu-taisen.net/general/" in image_url:
        return ""
    return image_url


def _relative_url(path: Path, base_dir: Path) -> str:
    relative = path.resolve().relative_to(base_dir.resolve())
    return quote(relative.as_posix(), safe="/._-~")


def _deck_visual_commentary(
    stat: AnalysisDeckStat,
    high_ranker_stat: AnalysisDeckStat | None,
    current_stat: AnalysisDeckStat | None,
    current_version: str,
) -> str:
    notes: list[str] = []
    if stat.sample_count >= 30:
        notes.append("样本量较充足，优先看 Wilson 下限而不是裸胜率。")
    elif stat.sample_count >= 10:
        notes.append("样本量中等，结论可参考，但还需要继续观察。")
    else:
        notes.append("样本偏小，连胜卡组会被 Wilson 明显压分。")

    wilson = _wilson_lower_bound(stat.win_count, stat.loss_count) or 0
    if stat.win_rate is not None and stat.win_rate >= 0.8 and wilson >= 0.6:
        notes.append("胜率和保守下限都高，属于本批次里比较硬的候选。")
    elif wilson >= 0.55:
        notes.append("保守下限超过 55%，说明不是纯粹靠一两场胜利撑起来。")

    if high_ranker_stat and high_ranker_stat.win_rate is not None and stat.win_rate is not None:
        diff = high_ranker_stat.win_rate - stat.win_rate
        if diff >= 0.05:
            notes.append("高 Ranker 口径更强，可能偏向熟练度或细节运营收益。")
        elif diff <= -0.05:
            notes.append("高 Ranker 口径回落，泛用强度可能比高手局表现更亮。")
        elif high_ranker_stat.sample_count >= 5:
            notes.append("高 Ranker 与全玩家表现接近，稳定性相对更好。")

    if current_stat and current_stat.sample_count >= 3:
        notes.append(
            f"{current_version} 内样本 {current_stat.sample_count}，胜率 {_fmt_rate(current_stat.win_rate) or '暂无'}。"
        )

    if stat.sample_count and stat.castle_crash_count / stat.sample_count >= 0.25:
        notes.append("落城占比不低，压制力或终盘收束能力值得重点看录像。")
    return " ".join(notes)


def _deck_archetype_script() -> str:
    return """
<script>
(() => {
  document.querySelectorAll('[data-variant-root]').forEach((root) => {
    const variants = Array.from(root.querySelectorAll('[data-variant]'));
    const button = root.querySelector('[data-variant-button]');
    const label = root.querySelector('[data-variant-label]');
    let current = 0;
    const render = () => {
      variants.forEach((variant, index) => {
        variant.classList.toggle('is-active', index === current);
      });
      if (label) {
        const prefix = current === 0 ? '代表构筑' : '构筑式样';
        label.textContent = `${prefix} ${current + 1}/${variants.length}`;
      }
    };
    if (button) {
      button.addEventListener('click', () => {
        current = (current + 1) % variants.length;
        render();
      });
    }
    render();
  });
})();
</script>
""".strip()


def _visual_sort_script() -> str:
    return """
<script>
(() => {
  const padRank = (index) => String(index + 1).padStart(2, '0');
  document.querySelectorAll('[data-sort-toolbar]').forEach((toolbar) => {
    const targetId = toolbar.getAttribute('data-sort-target');
    const root = targetId ? document.getElementById(targetId) : null;
    if (!root) {
      return;
    }
    const buttons = Array.from(toolbar.querySelectorAll('[data-sort-button]'));
    const items = Array.from(root.querySelectorAll('[data-sort-item]'));
    items.forEach((item, index) => {
      item.dataset.sortOriginal = String(index);
    });
    const metricValue = (item, key) => Number(item.getAttribute(`data-sort-${key}`) || 0);
    const applySort = (key) => {
      const secondaryKey = key === 'wilson' ? 'sample' : 'wilson';
      const sortedItems = Array.from(root.querySelectorAll('[data-sort-item]')).sort((left, right) => {
        const primaryDiff = metricValue(right, key) - metricValue(left, key);
        if (primaryDiff !== 0) {
          return primaryDiff;
        }
        const secondaryDiff = metricValue(right, secondaryKey) - metricValue(left, secondaryKey);
        if (secondaryDiff !== 0) {
          return secondaryDiff;
        }
        return Number(left.dataset.sortOriginal || 0) - Number(right.dataset.sortOriginal || 0);
      });
      sortedItems.forEach((item, index) => {
        const rank = item.querySelector('[data-rank-value]');
        if (rank) {
          rank.textContent = padRank(index);
        }
        root.appendChild(item);
      });
      buttons.forEach((button) => {
        button.setAttribute('aria-pressed', String(button.getAttribute('data-sort-key') === key));
      });
    };
    toolbar.addEventListener('click', (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target ? target.closest('[data-sort-button]') : null;
      if (!button) {
        return;
      }
      applySort(button.getAttribute('data-sort-key') || 'wilson');
    });
  });
})();
</script>
""".strip()


def _deck_archetype_visual_css() -> str:
    return """
:root {
  color-scheme: light;
  --paper: #f4f5f7;
  --panel: #ffffff;
  --ink: #171d25;
  --muted: #667386;
  --line: #d7dde7;
  --accent: #b4282e;
  --teal: #146b62;
  --gold: #af8330;
  --wash: #fbfcfe;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: linear-gradient(180deg, rgba(180, 40, 46, 0.055) 0, rgba(20, 107, 98, 0.035) 260px, var(--paper) 560px);
  color: var(--ink);
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  line-height: 1.5;
}
.page {
  width: min(1280px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 44px;
}
.report-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: end;
  border-bottom: 2px solid #242b35;
  padding-bottom: 18px;
  margin-bottom: 18px;
}
.title-block p {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: 36px;
  line-height: 1.1;
  letter-spacing: 0;
}
.summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(104px, 1fr));
  gap: 10px 16px;
  margin: 0;
}
.summary dt {
  color: var(--muted);
  font-size: 12px;
}
.summary dd {
  margin: 2px 0 0;
  font-weight: 700;
}
.sort-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 14px;
}
.sort-toolbar span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
}
.sort-toolbar button {
  border: 1px solid #cfd6e1;
  border-radius: 999px;
  background: var(--panel);
  color: #2c3745;
  cursor: pointer;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
  padding: 8px 11px;
}
.sort-toolbar button[aria-pressed="true"] {
  border-color: #242b35;
  background: #242b35;
  color: #fff;
}
.sort-toolbar button:hover {
  border-color: var(--accent);
}
.archetype-feature-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.18fr) minmax(0, 0.82fr);
  gap: 14px;
  align-items: stretch;
  margin-bottom: 18px;
}
.archetype-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  box-shadow: 0 14px 34px rgba(23, 29, 37, 0.07);
}
.archetype-card-1 {
  grid-row: span 2;
  border-top: 4px solid var(--accent);
}
.archetype-card-2,
.archetype-card-3 {
  padding: 16px;
}
.archetype-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
.rank-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  height: 30px;
  border-radius: 999px;
  background: #242b35;
  color: #fff;
  font-weight: 800;
}
.archetype-card-1 .rank-badge {
  background: var(--accent);
}
h2 {
  margin: 0;
  font-size: 22px;
  line-height: 1.25;
  letter-spacing: 0;
}
.archetype-card-2 h2,
.archetype-card-3 h2 {
  font-size: 17px;
}
.variant-viewer {
  margin: 14px 0;
}
.variant-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.variant-label,
.variant-single {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
.variant-button {
  border: 1px solid #cfd6e1;
  border-radius: 999px;
  background: #242b35;
  color: #fff;
  cursor: pointer;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
  padding: 7px 10px;
}
.variant-button:hover {
  background: var(--accent);
}
.variant {
  display: none;
}
.variant.is-active {
  display: block;
}
.variant-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.variant-name {
  margin: 8px 0 0;
  color: #354052;
  font-size: 12px;
  line-height: 1.45;
}
.variant-statline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.variant-statline span {
  border: 1px solid #e0e5ee;
  border-radius: 999px;
  background: var(--wash);
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  padding: 5px 7px;
}
.archetype-card-1 .variant-cards .unit {
  width: 125px;
  min-width: 125px;
}
.archetype-card-2 .variant-cards .unit,
.archetype-card-3 .variant-cards .unit {
  width: 94px;
  min-width: 94px;
}
.unit {
  margin: 0;
  width: 73px;
  min-width: 73px;
}
.unit img,
.image-placeholder {
  width: 100%;
  aspect-ratio: 0.72;
  border: 1px solid #c8d0dc;
  border-radius: 4px;
  background: #eef1f5;
}
.unit img {
  display: block;
  object-fit: cover;
  transition: transform 160ms ease;
}
.unit img:hover {
  transform: scale(1.08);
}
.image-placeholder {
  display: grid;
  place-items: center;
  color: var(--muted);
  padding: 4px;
  text-align: center;
  font-size: 9px;
  font-weight: 700;
}
figcaption {
  display: none;
}
.feature-stats,
.row-signals {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.score-pill {
  border: 1px solid #cfd6e1;
  border-radius: 999px;
  background: var(--wash);
  color: #2c3745;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  padding: 6px 8px;
  white-space: nowrap;
}
.score-pill strong {
  color: var(--muted);
  font-weight: 700;
  margin-right: 4px;
}
.score-pill:first-child {
  border-color: #e0c982;
  background: #fff8df;
  color: #6f5319;
}
.member-list {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid var(--line);
}
.member-list li {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid #edf0f5;
  color: #354052;
  font-size: 12px;
}
.member-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.member-list strong {
  color: var(--muted);
  font-weight: 700;
  white-space: nowrap;
}
.commentary {
  margin: 12px 0 0;
  color: #2b3441;
  font-size: 13px;
  line-height: 1.55;
}
.archetype-board {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.board-head {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) 430px;
  gap: 14px;
  padding: 10px 14px;
  background: #242b35;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.archetype-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) 430px;
  gap: 14px;
  align-items: center;
  padding: 12px 14px;
  border-top: 1px solid var(--line);
}
.archetype-row:hover {
  background: #fbfcfe;
}
.row-rank {
  display: grid;
  gap: 4px;
  align-content: center;
  min-height: 62px;
}
.row-rank strong {
  color: var(--ink);
  font-size: 24px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.row-rank span {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.row-deck {
  min-width: 0;
}
h3 {
  margin: 0;
  font-size: 15px;
  line-height: 1.35;
  letter-spacing: 0;
}
.row-deck .variant-viewer {
  margin: 8px 0;
}
.row-deck .variant-cards {
  gap: 6px;
}
.row-deck .member-list {
  margin-top: 8px;
}
.row-deck .member-list li {
  padding: 5px 0;
}
.empty {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
}
@media (max-width: 1040px) {
  .archetype-feature-grid {
    grid-template-columns: 1fr;
  }
  .archetype-card-1 {
    grid-row: auto;
  }
  .board-head {
    display: none;
  }
  .archetype-row {
    grid-template-columns: 58px minmax(0, 1fr);
  }
  .row-signals {
    grid-column: 2;
  }
}
@media (max-width: 720px) {
  .page {
    width: min(100% - 18px, 1280px);
    padding-top: 18px;
  }
  .report-header {
    display: block;
  }
  h1 {
    font-size: 28px;
  }
  .summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-top: 14px;
  }
  .archetype-card,
  .archetype-card-2,
  .archetype-card-3 {
    padding: 14px;
  }
  .archetype-row {
    grid-template-columns: 1fr;
  }
  .row-signals {
    grid-column: auto;
  }
  .row-rank {
    display: flex;
    justify-content: space-between;
    min-height: 0;
  }
  .archetype-card-1 .variant-cards .unit,
  .archetype-card-2 .variant-cards .unit,
  .archetype-card-3 .variant-cards .unit,
  .unit {
    width: 75px;
    min-width: 75px;
  }
  .score-pill {
    font-size: 11px;
  }
}
""".strip()


def _deck_visual_css() -> str:
    return """
:root {
  color-scheme: light;
  --paper: #f3f5f7;
  --panel: #ffffff;
  --ink: #171d25;
  --muted: #667386;
  --line: #d8dee7;
  --accent: #b4282e;
  --teal: #146b62;
  --gold: #af8330;
  --wash: #fbfcfe;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    linear-gradient(180deg, rgba(180, 40, 46, 0.05) 0, rgba(20, 107, 98, 0.03) 230px, var(--paper) 520px);
  color: var(--ink);
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  line-height: 1.5;
}
.page {
  width: min(1280px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 44px;
}
.report-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: end;
  border-bottom: 2px solid #242b35;
  padding-bottom: 18px;
  margin-bottom: 18px;
}
.title-block p {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: 36px;
  line-height: 1.1;
  letter-spacing: 0;
}
.summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(104px, 1fr));
  gap: 10px 16px;
  margin: 0;
}
.summary div {
  min-width: 0;
}
.summary dt {
  color: var(--muted);
  font-size: 12px;
}
.summary dd {
  margin: 2px 0 0;
  font-weight: 700;
}
.sort-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 14px;
}
.sort-toolbar span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
}
.sort-toolbar button {
  border: 1px solid #cfd6e1;
  border-radius: 999px;
  background: var(--panel);
  color: #2c3745;
  cursor: pointer;
  font-size: 12px;
  font-weight: 800;
  line-height: 1;
  padding: 8px 11px;
}
.sort-toolbar button[aria-pressed="true"] {
  border-color: #242b35;
  background: #242b35;
  color: #fff;
}
.sort-toolbar button:hover {
  border-color: var(--accent);
}
.feature-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.18fr) minmax(0, 0.82fr);
  gap: 14px;
  align-items: stretch;
  margin-bottom: 18px;
}
.feature-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  box-shadow: 0 14px 34px rgba(23, 29, 37, 0.07);
}
.feature-card-1 {
  grid-row: span 2;
  border-top: 4px solid var(--accent);
}
.feature-card-2,
.feature-card-3 {
  padding: 16px;
}
.feature-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
.rank-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  height: 30px;
  border-radius: 999px;
  background: #242b35;
  color: #fff;
  font-weight: 800;
}
.feature-card-1 .rank-badge {
  background: var(--accent);
}
h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.3;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}
.feature-card-2 h2,
.feature-card-3 h2 {
  font-size: 16px;
}
.feature-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 14px 0;
}
.feature-card-1 .unit {
  width: 78px;
  min-width: 78px;
}
.feature-card-2 .unit,
.feature-card-3 .unit {
  width: 56px;
  min-width: 56px;
}
.feature-stats,
.row-signals {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.score-pill {
  border: 1px solid #cfd6e1;
  border-radius: 999px;
  background: var(--wash);
  color: #2c3745;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  padding: 6px 8px;
  white-space: nowrap;
}
.score-pill strong {
  color: var(--muted);
  font-weight: 700;
  margin-right: 4px;
}
.score-pill:first-child {
  border-color: #e0c982;
  background: #fff8df;
  color: #6f5319;
}
.commentary {
  margin: 12px 0 0;
  color: #2b3441;
  font-size: 13px;
  line-height: 1.55;
}
.ranking-board {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.board-head {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) 430px;
  gap: 14px;
  padding: 10px 14px;
  background: #242b35;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.rank-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr) 430px;
  gap: 14px;
  align-items: center;
  padding: 12px 14px;
  border-top: 1px solid var(--line);
}
.rank-row:hover {
  background: #fbfcfe;
}
.row-rank {
  display: grid;
  gap: 4px;
  align-content: center;
  min-height: 62px;
}
.row-rank strong {
  color: var(--ink);
  font-size: 24px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.row-rank span {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.row-deck {
  min-width: 0;
}
h3 {
  margin: 0;
  font-size: 14px;
  line-height: 1.35;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}
.card-strip {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 6px;
  margin: 8px 0 0;
}
.unit {
  margin: 0;
  width: 44px;
  min-width: 44px;
}
.unit img,
.image-placeholder {
  width: 100%;
  aspect-ratio: 0.72;
  border: 1px solid #c8d0dc;
  border-radius: 4px;
  background: #eef1f5;
}
.unit img {
  display: block;
  object-fit: cover;
  transition: transform 160ms ease;
}
.unit img:hover {
  transform: scale(1.08);
}
.image-placeholder {
  display: grid;
  place-items: center;
  color: var(--muted);
  padding: 4px;
  text-align: center;
  font-size: 9px;
  font-weight: 700;
}
figcaption {
  display: none;
  margin-top: 4px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.empty {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
}
@media (max-width: 1040px) {
  .feature-grid {
    grid-template-columns: 1fr;
  }
  .feature-card-1 {
    grid-row: auto;
  }
  .board-head {
    display: none;
  }
  .rank-row {
    grid-template-columns: 58px minmax(0, 1fr);
  }
  .row-signals {
    grid-column: 2;
  }
}
@media (max-width: 720px) {
  .page {
    width: min(100% - 18px, 1280px);
    padding-top: 18px;
  }
  .report-header {
    display: block;
  }
  h1 {
    font-size: 28px;
  }
  .summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-top: 14px;
  }
  .feature-card,
  .feature-card-2,
  .feature-card-3 {
    padding: 14px;
  }
  .rank-row {
    grid-template-columns: 1fr;
  }
  .row-signals {
    grid-column: auto;
  }
  .row-rank {
    display: flex;
    justify-content: space-between;
    min-height: 0;
  }
  .feature-card-1 .unit,
  .feature-card-2 .unit,
  .feature-card-3 .unit,
  .unit {
    width: 48px;
    min-width: 48px;
  }
  .score-pill {
    font-size: 11px;
  }
}
""".strip()


def _summary_item(label: str, value: Any) -> str:
    return f"<div><dt>{_html(label)}</dt><dd>{_html(value)}</dd></div>"


def _metric_item(label: str, value: Any) -> str:
    text = str(value) if value not in {None, ""} else "-"
    return f"<div><dt>{_html(label)}</dt><dd>{_html(text)}</dd></div>"


def _score_pill(label: str, value: Any) -> str:
    text = str(value) if value not in {None, ""} else "-"
    return f'<span class="score-pill"><strong>{_html(label)}</strong>{_html(text)}</span>'


def _compact_scope_value(stat: AnalysisDeckStat | AnalysisCardStat | None) -> str:
    if stat is None:
        return "-"
    wilson = _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count))
    return f"{stat.sample_count} / {_fmt_rate(stat.win_rate) or '-'} / {wilson or '-'}"


def _short_card_label(label: str) -> str:
    name = label.split("(", 1)[0].strip() or label
    return name if len(name) <= 8 else f"{name[:8]}…"


def _html(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _deck_base_row(stat: AnalysisDeckStat, lookup) -> dict[str, str]:
    hashes = stat.deck_fingerprint.split(",") if stat.deck_fingerprint else []
    return {
        "deck": " / ".join(lookup.names(hashes)),
        "deck_fingerprint": stat.deck_fingerprint,
        "sample_count": str(stat.sample_count),
        "win_count": str(stat.win_count),
        "loss_count": str(stat.loss_count),
        "draw_count": str(stat.draw_count),
        "win_rate": _fmt_rate(stat.win_rate),
        "wilson_lower_bound": _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count)),
        "avg_castle_diff": _fmt_number(stat.avg_castle_diff),
        "avg_own_castle_rate": _fmt_number(stat.avg_own_castle_rate),
        "avg_castle_damage_dealt": _fmt_number(stat.avg_castle_damage_dealt),
        "avg_castle_damage_taken": _fmt_number(stat.avg_castle_damage_taken),
        "avg_kill_count": _fmt_number(stat.avg_kill_count),
        "avg_death_count": _fmt_number(stat.avg_death_count),
        "castle_crash_count": str(stat.castle_crash_count),
        "castle_crashed_count": str(stat.castle_crashed_count),
    }


def _card_base_row(stat: AnalysisCardStat, lookup) -> dict[str, str]:
    return {
        "card": lookup.label(stat.card_hash),
        "card_hash": stat.card_hash,
        "sample_count": str(stat.sample_count),
        "win_count": str(stat.win_count),
        "loss_count": str(stat.loss_count),
        "draw_count": str(stat.draw_count),
        "win_rate": _fmt_rate(stat.win_rate),
        "wilson_lower_bound": _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count)),
        "avg_castle_diff": _fmt_number(stat.avg_castle_diff),
        "avg_own_castle_rate": _fmt_number(stat.avg_own_castle_rate),
        "avg_castle_damage_dealt": _fmt_number(stat.avg_castle_damage_dealt),
        "avg_castle_damage_taken": _fmt_number(stat.avg_castle_damage_taken),
        "avg_kill_count": _fmt_number(stat.avg_kill_count),
        "avg_death_count": _fmt_number(stat.avg_death_count),
        "high_win_deck_count": str(stat.high_win_deck_count),
    }


def _comparison_columns(prefix: str, stat: AnalysisDeckStat | AnalysisCardStat | None) -> dict[str, str]:
    if stat is None:
        return {
            f"{prefix}_sample_count": "",
            f"{prefix}_win_rate": "",
            f"{prefix}_wilson_lower_bound": "",
        }
    return {
        f"{prefix}_sample_count": str(stat.sample_count),
        f"{prefix}_win_rate": _fmt_rate(stat.win_rate),
        f"{prefix}_wilson_lower_bound": _fmt_rate(_wilson_lower_bound(stat.win_count, stat.loss_count)),
    }


def _deck_stat_lookup(stats: list[AnalysisDeckStat]) -> dict[tuple[str, str, str], AnalysisDeckStat]:
    return {(stat.sample_scope, stat.version_scope, stat.deck_fingerprint): stat for stat in stats}


def _card_stat_lookup(stats: list[AnalysisCardStat]) -> dict[tuple[str, str, str], AnalysisCardStat]:
    return {(stat.sample_scope, stat.version_scope, stat.card_hash): stat for stat in stats}


def _sorted_base_stats(stats):
    base_stats = [stat for stat in stats if stat.sample_scope == SCOPE_ALL_PLAYERS and stat.version_scope == VERSION_ALL]
    return sorted(base_stats, key=lambda item: (_wilson_lower_bound(item.win_count, item.loss_count) or 0, item.sample_count), reverse=True)


def _run_high_ranker_scope(run: AnalysisRun) -> str:
    try:
        high_ranker_rank = int((run.thresholds_json or {}).get("high_ranker_rank") or DEFAULT_HIGH_RANKER_RANK)
    except (TypeError, ValueError):
        high_ranker_rank = DEFAULT_HIGH_RANKER_RANK
    return _high_ranker_scope(high_ranker_rank)


def _versions_for_stats(run: AnalysisRun, stats) -> list[str]:
    versions = set()
    counts = (run.counts_json or {}).get("version_counts")
    if isinstance(counts, dict):
        versions.update(str(version) for version in counts if version)
    versions.update(stat.version_scope for stat in stats if stat.version_scope != VERSION_ALL)
    return sorted(version for version in versions if version)


def _write_rows_or_markdown(output: Path, output_format: str, rows: list[dict[str, str]], title: str) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "csv":
        fieldnames = list(rows[0].keys()) if rows else ["metric", "value"]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return output
    if output_format == "md":
        lines = [f"# {title}", ""]
        if rows:
            fieldnames = list(rows[0].keys())
            lines.append("| " + " | ".join(fieldnames) + " |")
            lines.append("|" + "|".join("---" for _ in fieldnames) + "|")
            for row in rows:
                lines.append("| " + " | ".join(str(row.get(field, "")).replace("|", "/") for field in fieldnames) + " |")
        else:
            lines.append("暂无数据。")
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output
    raise ValueError(f"不支持的导出格式：{output_format}")


def _in_date_range(played_at: str | None, date_from: str, date_to: str) -> bool:
    match_date = str(played_at or "")[:10]
    return bool(match_date) and date_from <= match_date <= date_to


def _castle_damage_for_side(castle_breakdown: dict[str, Any], side_index: int) -> tuple[float | None, float | None]:
    rows = castle_breakdown.get("rows") if isinstance(castle_breakdown, dict) else []
    if not isinstance(rows, list):
        return None, None
    player_side_damage = _sum_rates(row.get("player") for row in rows if isinstance(row, dict))
    enemy_side_damage = _sum_rates(row.get("enemy") for row in rows if isinstance(row, dict))
    if side_index == 1:
        return enemy_side_damage, player_side_damage
    return player_side_damage, enemy_side_damage


def _sum_rates(values) -> float | None:
    parsed = [rate for rate in (_parse_rate(value) for value in values) if rate is not None]
    return sum(parsed) if parsed else None


def _damage_from_remaining_rate(rate: float | None) -> float | None:
    return max(0.0, 100.0 - rate) if rate is not None else None


def _battle_stat_total(profile: dict[str, Any], key: str) -> float | None:
    if not isinstance(profile, dict):
        return None
    stats = profile.get("battle_stats")
    if not isinstance(stats, dict):
        return None
    value = stats.get(key)
    if isinstance(value, dict):
        value = value.get("total")
    return _coerce_number(value)


def _parse_rank(profile: dict[str, Any]) -> int | None:
    if not isinstance(profile, dict):
        return None
    # 详情页双方资料里已有全国排名，优先用这里给 side 样本打 Ranker 标签。
    rank_text = str(profile.get("全国主君ランキング") or "").replace(",", "")
    match = re.search(r"\d+", rank_text)
    return int(match.group(0)) if match else None


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def _wilson_lower_bound(win_count: int, loss_count: int, z: float = 1.96) -> float | None:
    total = win_count + loss_count
    if total <= 0:
        return None
    phat = win_count / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return (centre - margin) / denominator


def _normalize_result(result: str) -> str:
    result = str(result or "unknown")
    return result if result in {"win", "loss", "draw"} else "unknown"


def _reverse_result(result: str) -> str:
    if result == "win":
        return "loss"
    if result == "loss":
        return "win"
    return result if result == "draw" else "unknown"


def _parse_rate(value: str | None) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt_rate(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.2f}%"


def _fmt_number(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"
