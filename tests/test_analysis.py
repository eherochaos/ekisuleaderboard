from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import AnalysisCardStat, AnalysisDeckStat, BattleSummary, Match, MatchDeck, MatchDeckUnit, MatchSide
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.analysis import export_analysis, refresh_analysis
from eiketsu_env.utils import deck_fingerprint


def _settings(tmp_path: Path) -> Settings:
    catalog_path = tmp_path / "cards.json"
    image_dir = tmp_path / "apps" / "web" / "public" / "assets" / "cards" / "card_small"
    image_dir.mkdir(parents=True)
    for card_code in ("A001", "B001", "C001", "D001"):
        (image_dir / f"{card_code}.jpg").write_bytes(b"fake-image")
    catalog_path.write_text(
        json.dumps(
            {
                "cards": [
                    {"hash_id": "card-a", "card_code": "A001", "name": "Card A", "cost": "1.0", "unitType": "槍兵", "image_urls": {"card_small": "https://example.test/card-a.jpg"}},
                    {"hash_id": "card-b", "card_code": "B001", "name": "Card B", "cost": "2.0", "unitType": "騎兵", "image_urls": {"card_small": "https://example.test/card-b.jpg"}},
                    {"hash_id": "card-c", "card_code": "C001", "name": "Card C", "cost": "3.0", "unitType": "弓兵", "image_urls": {"card_small": "https://example.test/card-c.jpg"}},
                    {"hash_id": "card-d", "card_code": "D001", "name": "Card D", "cost": "1.5", "unitType": "鉄砲隊", "image_urls": {"card_small": "https://example.test/card-d.jpg"}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=catalog_path,
    )


def _add_match(
    session: Session,
    index: int,
    result: str,
    mode: str,
    player_deck: list[str],
    enemy_deck: list[str],
    player_castle: str,
    enemy_castle: str,
    player_stats: dict | None = None,
    enemy_stats: dict | None = None,
    castle_breakdown: dict | None = None,
    version: str = "Ver.test",
) -> None:
    match = Match(
        public_id=f"d:test:{index}",
        detail_url=f"https://example.test/detail?t={index}&f=1",
        result=result,
        played_at=f"2026-05-10 10:{index:02d}",
        mode=mode,
        version=version,
    )
    match.sides = [
        MatchSide(side_index=1, role="player", player_name="A", follow_id="1", result=result, castle_rate=player_castle, profile_json=player_stats or {}, selected_json={}),
        MatchSide(side_index=2, role="enemy", player_name="B", follow_id="", result="unknown", castle_rate=enemy_castle, profile_json=enemy_stats or {}, selected_json={}),
    ]
    match.decks = [
        _deck(1, player_deck),
        _deck(2, enemy_deck),
    ]
    if castle_breakdown is not None:
        match.battle_summary = BattleSummary(castle_breakdown_json=castle_breakdown, timeline_labels_json=[], timeline_data_json={})
    session.add(match)


def _rank_profile(rank: int | None, extra: dict | None = None) -> dict:
    profile = dict(extra or {})
    if rank is not None:
        profile["全国主君ランキング"] = f"{rank} 位"
    return profile


def _deck(side_index: int, card_hashes: list[str]) -> MatchDeck:
    deck = MatchDeck(side_index=side_index, deck_fingerprint=deck_fingerprint(card_hashes))
    deck.units = [MatchDeckUnit(slot=index, card_hash=card_hash) for index, card_hash in enumerate(card_hashes, start=1)]
    return deck


def _deck_stat_for(
    session: Session,
    card_hashes: list[str],
    sample_scope: str = "all_players",
    version_scope: str = "all_versions",
) -> AnalysisDeckStat | None:
    return session.scalar(
        select(AnalysisDeckStat).where(
            AnalysisDeckStat.deck_fingerprint == deck_fingerprint(card_hashes),
            AnalysisDeckStat.sample_scope == sample_scope,
            AnalysisDeckStat.version_scope == version_scope,
        )
    )


def _card_stat_for(
    session: Session,
    card_hash: str,
    sample_scope: str = "all_players",
    version_scope: str = "all_versions",
) -> AnalysisCardStat | None:
    return session.scalar(
        select(AnalysisCardStat).where(
            AnalysisCardStat.card_hash == card_hash,
            AnalysisCardStat.sample_scope == sample_scope,
            AnalysisCardStat.version_scope == version_scope,
        )
    )


def test_refresh_analysis_counts_both_sides_and_filters_modes(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(session, 1, "win", "全国対戦", ["card-a", "card-b"], ["card-c"], "80.00%", "0.00%")
        _add_match(session, 2, "loss", "戦友対戦", ["card-a", "card-b"], ["card-c"], "20.00%", "90.00%")
        _add_match(session, 3, "loss", "店内対戦", ["card-a", "card-b"], ["card-c", "card-d"], "10.00%", "100.00%")
        _add_match(session, 4, "win", "戦祭り", ["card-a", "card-b"], ["card-c"], "100.00%", "0.00%")
        session.commit()

    result = refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)

    assert result.status == "completed"
    assert result.counts["matches"] == 3
    assert result.counts["side_samples"] == 6

    with Session(engine) as session:
        deck_ab = _deck_stat_for(session, ["card-a", "card-b"])
        assert deck_ab is not None
        assert deck_ab.sample_count == 3
        assert deck_ab.win_count == 1
        assert deck_ab.loss_count == 2
        assert round(deck_ab.avg_castle_diff or 0, 2) == -26.67
        assert deck_ab.castle_crash_count == 1

        card_c = _card_stat_for(session, "card-c")
        assert card_c is not None
        assert card_c.sample_count == 3
        assert card_c.win_count == 2
        assert card_c.loss_count == 1
        assert card_c.high_win_deck_count == 1


def test_refresh_analysis_ignores_unknown_results_and_missing_metric_values(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(session, 1, "win", "全国対戦", ["card-a", "card-b"], ["card-c"], "", "")
        _add_match(session, 2, "unknown", "全国対戦", ["card-a", "card-b"], ["card-c"], "50.00%", "50.00%")
        session.commit()

    result = refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)

    assert result.counts["raw_side_samples"] == 4
    assert result.counts["unknown_result_side_samples"] == 2
    assert result.counts["side_samples"] == 2
    assert result.counts["result_counts"] == {"win": 1, "loss": 1}

    with Session(engine) as session:
        deck_ab = _deck_stat_for(session, ["card-a", "card-b"])
        assert deck_ab is not None
        assert deck_ab.sample_count == 1
        assert deck_ab.win_count == 1
        assert deck_ab.avg_castle_damage_dealt is None

        card_c = _card_stat_for(session, "card-c")
        assert card_c is not None
        assert card_c.sample_count == 1
        assert card_c.loss_count == 1
        assert card_c.avg_castle_damage_taken is None


def test_refresh_analysis_adds_castle_and_combat_averages(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(
            session,
            10,
            "win",
            "ranked",
            ["card-a", "card-b"],
            ["card-c"],
            "80.00%",
            "0.00%",
            player_stats={"battle_stats": {"kill_count": {"total": 6}, "death_count": {"total": 4}}},
            enemy_stats={"battle_stats": {"kill_count": {"total": 4}, "death_count": {"total": 6}}},
            castle_breakdown={"rows": [{"player": "20.00%", "enemy": "101.50%"}]},
        )
        session.commit()

    refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)

    with Session(engine) as session:
        deck_ab = _deck_stat_for(session, ["card-a", "card-b"])
        assert deck_ab is not None
        assert deck_ab.avg_castle_damage_dealt == 101.5
        assert deck_ab.avg_castle_damage_taken == 20
        assert deck_ab.avg_kill_count == 6
        assert deck_ab.avg_death_count == 4

        card_c = _card_stat_for(session, "card-c")
        assert card_c is not None
        assert card_c.avg_castle_damage_dealt == 20
        assert card_c.avg_castle_damage_taken == 101.5
        assert card_c.avg_kill_count == 4
        assert card_c.avg_death_count == 6


def test_refresh_analysis_splits_samples_by_ranker_scope_and_version(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(
            session,
            1,
            "win",
            "全国対戦",
            ["card-a", "card-b"],
            ["card-c"],
            "80.00%",
            "0.00%",
            player_stats=_rank_profile(None),
            enemy_stats=_rank_profile(40),
            version="Ver.old",
        )
        _add_match(
            session,
            2,
            "loss",
            "全国対戦",
            ["card-a", "card-b"],
            ["card-c"],
            "20.00%",
            "90.00%",
            player_stats=_rank_profile(50),
            enemy_stats=_rank_profile(None),
            version="Ver.new",
        )
        _add_match(
            session,
            3,
            "loss",
            "全国対戦",
            ["card-a", "card-b"],
            ["card-c"],
            "10.00%",
            "100.00%",
            player_stats=_rank_profile(200),
            enemy_stats=_rank_profile(500),
            version="Ver.new",
        )
        session.commit()

    result = refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)

    assert result.counts["current_version"] == "Ver.new"
    assert result.counts["version_counts"] == {"Ver.old": 1, "Ver.new": 2}
    assert result.counts["sample_scope_counts"]["all_players"] == 6
    assert result.counts["sample_scope_counts"]["all_ranker"] == 4
    assert result.counts["sample_scope_counts"]["high_ranker_top100"] == 2
    assert result.counts["high_ranker_rank_limit"] == 100

    with Session(engine) as session:
        deck_ab_all = _deck_stat_for(session, ["card-a", "card-b"])
        deck_ab_ranker = _deck_stat_for(session, ["card-a", "card-b"], "all_ranker")
        deck_ab_top100 = _deck_stat_for(session, ["card-a", "card-b"], "high_ranker_top100")
        deck_ab_current = _deck_stat_for(session, ["card-a", "card-b"], "all_players", "Ver.new")

        assert deck_ab_all is not None
        assert deck_ab_all.sample_count == 3
        assert deck_ab_all.win_count == 1
        assert deck_ab_all.loss_count == 2

        assert deck_ab_ranker is not None
        assert deck_ab_ranker.sample_count == 2
        assert deck_ab_ranker.win_count == 0
        assert deck_ab_ranker.loss_count == 2

        assert deck_ab_top100 is not None
        assert deck_ab_top100.sample_count == 1
        assert deck_ab_top100.loss_count == 1

        assert deck_ab_current is not None
        assert deck_ab_current.sample_count == 2
        assert deck_ab_current.loss_count == 2

        card_a_current = _card_stat_for(session, "card-a", "all_players", "Ver.new")
        assert card_a_current is not None
        assert card_a_current.sample_count == 2


def test_refresh_analysis_can_filter_to_target_version(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(session, 1, "win", "全国対戦", ["card-a"], ["card-c"], "80.00%", "0.00%", version="Ver.old")
        _add_match(session, 2, "loss", "全国対戦", ["card-a"], ["card-c"], "20.00%", "90.00%", version="Ver.new")
        session.commit()

    result = refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1, version="Ver.new")

    assert result.counts["matches"] == 1
    assert result.counts["side_samples"] == 2
    assert result.counts["target_version"] == "Ver.new"
    assert result.counts["version_counts"] == {"Ver.new": 1}


def test_export_analysis_reports_are_readable(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(
            session,
            1,
            "win",
            "全国対戦",
            ["card-a", "card-b"],
            ["card-c"],
            "80.00%",
            "0.00%",
            player_stats=_rank_profile(12),
            enemy_stats=_rank_profile(150),
        )
        session.commit()
    refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)

    deck_md = export_analysis(settings, "deck", "md")
    card_csv = export_analysis(settings, "card", "csv")
    deck_version_md = export_analysis(settings, "deck-version", "md")
    card_version_csv = export_analysis(settings, "card-version", "csv")
    overview_md = export_analysis(settings, "overview", "md")
    deck_visual_html = export_analysis(settings, "deck-visual", "html")
    deck_archetype_html = export_analysis(settings, "deck-archetype-visual", "html")

    assert "Card A(1.0 槍兵)" in deck_md.read_text(encoding="utf-8")
    deck_text = deck_md.read_text(encoding="utf-8")
    card_text = card_csv.read_text(encoding="utf-8-sig")
    deck_version_text = deck_version_md.read_text(encoding="utf-8")
    card_version_text = card_version_csv.read_text(encoding="utf-8-sig")
    deck_visual_text = deck_visual_html.read_text(encoding="utf-8")
    deck_archetype_text = deck_archetype_html.read_text(encoding="utf-8")
    assert "Card C(3.0 弓兵)" in card_text
    assert "side_samples" in overview_md.read_text(encoding="utf-8")
    assert "wilson_lower_bound" in deck_text
    assert "all_ranker_sample_count" in deck_text
    assert "high_ranker_top100_sample_count" in deck_text
    assert "current_version_sample_count" in deck_text
    assert "avg_kill_count" in card_text
    assert "all_ranker_win_rate" in card_text
    assert "Ver.test_sample_count" in deck_version_text
    assert "Ver.test_win_rate" in card_version_text
    assert "卡组图文报告" in deck_visual_text
    assert '<img src="analysis_deck-visual_assets/cards/A001.jpg"' in deck_visual_text
    assert deck_visual_text.index("cards/B001.jpg") < deck_visual_text.index("cards/A001.jpg")
    assert 'data-sort-key="sample"' in deck_visual_text
    assert '<article class="rank-row" data-sort-item' in deck_visual_text
    assert '<section class="feature-grid">' not in deck_visual_text
    assert "Wilson 下限" in deck_visual_text
    assert (tmp_path / "data" / "exports" / "analysis_deck-visual_assets" / "cards" / "A001.jpg").exists()
    assert "卡组分类报告" in deck_archetype_text
    assert "共同 Cost ≥ 5.0" in deck_archetype_text
    assert "代表构筑吸附" in deck_archetype_text
    assert "data-variant-root" in deck_archetype_text
    assert 'data-sort-key="sample"' in deck_archetype_text
    assert '<article class="archetype-row" data-sort-item' in deck_archetype_text
    assert '<section class="archetype-feature-grid">' not in deck_archetype_text
    assert deck_archetype_text.index("cards/B001.jpg") < deck_archetype_text.index("cards/A001.jpg")
    assert "代表构筑" in deck_archetype_text
    assert (tmp_path / "data" / "exports" / "analysis_deck-archetype-visual_assets" / "cards" / "A001.jpg").exists()


def test_deck_archetype_visual_groups_decks_by_shared_cost(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _add_match(session, 1, "win", "全国対戦", ["card-a", "card-b", "card-c"], ["card-d"], "80.00%", "0.00%")
        _add_match(session, 2, "win", "全国対戦", ["card-b", "card-c", "card-d"], ["card-a"], "80.00%", "0.00%")
        session.commit()

    refresh_analysis(settings, "2026-05-10", "2026-05-10", deck_min_samples=1, card_min_samples=1)
    archetype_html = export_analysis(settings, "deck-archetype-visual", "html")
    text = archetype_html.read_text(encoding="utf-8")

    assert "2 个构筑" in text
    assert "Card C / Card B 系" in text
    assert "Change" in text
    assert 'data-variant-index="1"' in text
    first_variant = text.split('data-variant-index="0"', 1)[1].split('data-variant-index="1"', 1)[0]
    assert first_variant.index("cards/C001.jpg") < first_variant.index("cards/B001.jpg")
    assert first_variant.index("cards/B001.jpg") < first_variant.index("cards/A001.jpg")
