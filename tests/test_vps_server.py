from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import Match, ServerApiToken, ServerInvite, ServerLeaderboardSnapshot, ServerUpload, SharedContributionPackage
from eiketsu_env.db.session import make_engine
from eiketsu_env.server_app import (
    LEADERBOARD_HTML_DEFAULT_LIMIT,
    LEADERBOARD_HTML_MAX_LIMIT,
    _admin_invites_response,
    _admin_updates_response,
    _leaderboard_display_limit,
    _leaderboard_rows_response,
    _leaderboard_visual_page,
)
from eiketsu_env.services.client_update import publish_client_update
from eiketsu_env.services.repository import EnvRepository
from eiketsu_env.services.server_share import (
    bind_invite,
    contributor_leaderboard,
    create_invite,
    get_server_config,
    import_uploaded_package,
    list_invites,
    personal_leaderboard,
    public_leaderboard,
    refresh_public_leaderboard_snapshots,
    _clear_leaderboard_cache,
    _effective_share_config,
    set_server_config,
)
from eiketsu_env.services.share import ShareConfig, export_contribution


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=tmp_path / "cards.json",
    )


class _FakeRequest:
    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}


def _write_card_catalog(settings: Settings) -> None:
    settings.card_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    settings.card_catalog_path.write_text(
        json.dumps(
            {
                "cards": [
                    {"hash_id": "card-a", "card_code": "A001", "name": "Card A", "cost": "1.0", "unitType": "槍兵"},
                    {"hash_id": "card-b", "card_code": "B001", "name": "Card B", "cost": "2.0", "unitType": "騎兵"},
                    {"hash_id": "card-c", "card_code": "C001", "name": "Card C", "cost": "3.0", "unitType": "弓兵"},
                    {"hash_id": "card-d", "card_code": "D001", "name": "Card D", "cost": "1.0", "unitType": "槍兵"},
                    {"hash_id": "card-e", "card_code": "E001", "name": "Card E", "cost": "2.5", "unitType": "騎兵"},
                    {"hash_id": "card-f", "card_code": "F001", "name": "Card F", "cost": "2.5", "unitType": "弓兵"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _init_db(settings: Settings):
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return engine


def _detail(
    replay_id: str,
    contributor: str = "586",
    player_deck: list[str] | None = None,
    enemy_deck: list[str] | None = None,
    result: str = "win",
    played_at: str = "2026-05-11 12:34",
    player_name: str = "A",
    enemy_name: str = "B",
    player_profile: dict | None = None,
    enemy_profile: dict | None = None,
) -> dict:
    player_deck = player_deck or ["card-a", "card-b"]
    enemy_deck = enemy_deck or ["card-c"]
    player_profile = player_profile or {"全国主君ランキング": "50 位"}
    enemy_profile = enemy_profile or {"全国主君ランキング": "120 位"}
    return {
        "detail_url": f"https://eiketsu-taisen.net/members/history/detail?f={contributor}",
        "url": f"https://eiketsu-taisen.net/members/history/detail?f={contributor}",
        "follow_id": contributor,
        "played_at": played_at,
        "date": played_at,
        "mode": "全国対戦",
        "version": "Ver.vps",
        "result": result,
        "replay_id": replay_id,
        "castle_breakdown": {"rows": [{"player": "20.00%", "enemy": "100.00%"}]},
        "timeline_labels": ["開幕", "終了"],
        "timeline_data": {"castle": {"player": [100, 80], "enemy": [100, 0]}},
        "players": [
            {
                "side_index": 1,
                "role": "player",
                "player_name": player_name,
                "follow_id": contributor,
                "result": result,
                "castle_rate": "80.00%",
                "deck_ids": player_deck,
                "profile": player_profile,
            },
            {
                "side_index": 2,
                "role": "enemy",
                "player_name": enemy_name,
                "result": "loss",
                "castle_rate": "0.00%",
                "deck_ids": enemy_deck,
                "profile": enemy_profile,
            },
        ],
    }


def _package_text(tmp_path: Path, contributor: str, replay_id: str = "same-replay") -> str:
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail(replay_id))
        session.commit()
    result = export_contribution(
        settings,
        ShareConfig(target_version="Ver.vps", date_from="2026-05-10", date_to="2026-05-12"),
        contributor,
    )
    return result.path.read_text(encoding="utf-8")


def _insert_match(
    settings: Settings,
    replay_id: str,
    player_deck: list[str],
    enemy_deck: list[str],
    played_at: str = "2026-05-11 12:34",
    player_name: str = "A",
    enemy_name: str = "B",
    player_profile: dict | None = None,
    enemy_profile: dict | None = None,
) -> None:
    engine = make_engine(settings)
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(
            _detail(
                replay_id,
                player_deck=player_deck,
                enemy_deck=enemy_deck,
                played_at=played_at,
                player_name=player_name,
                enemy_name=enemy_name,
                player_profile=player_profile,
                enemy_profile=enemy_profile,
            )
        )
        session.commit()


def test_invite_binds_once_and_server_stores_only_token_hash(tmp_path):
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    invite = create_invite(settings, "friend-a", code="INVITE1")

    bound = bind_invite(settings, invite.code, "alice")

    assert bound.api_token
    with pytest.raises(ValueError):
        bind_invite(settings, invite.code, "alice-again")

    with Session(engine) as session:
        [token_row] = session.scalars(select(ServerApiToken)).all()
        assert token_row.token_prefix == bound.api_token[:8]
        assert token_row.token_hash != bound.api_token


def test_list_invites_reports_status_and_bound_user(tmp_path):
    settings = _settings(tmp_path)
    engine = _init_db(settings)
    create_invite(settings, "unused", code="INVITE-A")
    bind_invite(settings, create_invite(settings, "used", code="INVITE-B").code, "alice")

    payload = list_invites(settings, status="all")

    assert payload["counts"]["all"] == 2
    assert payload["counts"]["active"] == 1
    assert payload["counts"]["used"] == 1
    by_code = {item["code"]: item for item in payload["items"]}
    assert by_code["INVITE-A"]["status"] == "active"
    assert by_code["INVITE-B"]["status"] == "used"
    assert by_code["INVITE-B"]["used_by"] == "alice"
    with Session(engine) as session:
        assert len(session.scalars(select(ServerInvite)).all()) == 2


def test_admin_invite_page_requires_token_and_can_create_invite(tmp_path):
    settings = _settings(tmp_path)
    settings.admin_token = "admin-secret"
    _init_db(settings)

    locked = _admin_invites_response(settings, _FakeRequest(), query_token="wrong")
    assert locked.status_code == 401
    assert "输入管理口令" in locked.body.decode("utf-8")

    create_invite(settings, "friend-a", code="INVITE-WEB")
    page = _admin_invites_response(settings, _FakeRequest(), query_token="admin-secret", status="active")
    html = page.body.decode("utf-8")

    assert page.status_code == 200
    assert "INVITE-WEB" in html
    assert "friend-a" in html
    assert "可用" in html
    assert "复制" in html


def test_admin_update_page_shows_published_client(tmp_path):
    settings = _settings(tmp_path)
    settings.admin_token = "admin-secret"
    _init_db(settings)
    exe = tmp_path / "EiketsuCollector.exe"
    exe.write_bytes(b"fake-exe")
    publish_client_update(settings, exe, "0.1.2", notes="test update")

    page = _admin_updates_response(settings, _FakeRequest(), query_token="admin-secret")
    html = page.body.decode("utf-8")

    assert page.status_code == 200
    assert "0.1.2" in html
    assert "/downloads/EiketsuCollector_0.1.2.exe" in html
    assert "test update" in html


def test_server_config_auto_extends_stale_date_to_to_current_day(tmp_path):
    settings = _settings(tmp_path / "server")
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-04-22", "2026-05-15")

    effective = _effective_share_config(
        ShareConfig(target_version="Ver.vps", date_from="2026-04-22", date_to="2026-05-15"),
        today=date(2026, 5, 17),
    )

    assert effective.date_to == "2026-05-17"


def test_public_leaderboard_uses_effective_date_to_for_recent_upload(tmp_path, monkeypatch):
    from eiketsu_env.services import server_share as server_share_module

    monkeypatch.setattr(server_share_module, "_latest_collectable_game_date", lambda today=None: "2026-05-16")
    settings = _settings(tmp_path / "server")
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-04-22", "2026-05-15")
    _insert_match(settings, "recent-replay", ["card-a"], ["card-b"], played_at="2026-05-16 12:00")

    config = get_server_config(settings)
    payload = public_leaderboard(settings)

    assert config["date_to"] == "2026-05-16"
    assert payload["date_to"] == "2026-05-16"
    assert payload["match_count"] == 1


def test_uploaded_package_is_idempotent_and_dedupes_matches(tmp_path):
    server_settings = _settings(tmp_path / "server")
    server_engine = _init_db(server_settings)
    invite = create_invite(server_settings, "friend", code="INVITE2")
    token = bind_invite(server_settings, invite.code, "alice").api_token
    text = _package_text(tmp_path / "source-a", "alice")
    other_text = _package_text(tmp_path / "source-b", "bob")

    first = import_uploaded_package(server_settings, token, text)
    second = import_uploaded_package(server_settings, token, text)
    third = import_uploaded_package(server_settings, token, other_text)

    assert first.already_uploaded is False
    assert second.already_uploaded is True
    assert third.already_uploaded is False
    with Session(server_engine) as session:
        assert len(session.scalars(select(ServerUpload)).all()) == 2
        assert len(session.scalars(select(SharedContributionPackage)).all()) == 2
        assert len(session.scalars(select(Match)).all()) == 1


def test_public_leaderboard_is_anonymous_and_uses_server_config(tmp_path):
    settings = _settings(tmp_path / "server")
    _write_card_catalog(settings)
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    invite = create_invite(settings, "friend", code="INVITE3")
    token = bind_invite(settings, invite.code, "alice").api_token
    import_uploaded_package(settings, token, _package_text(tmp_path / "source", "alice"))

    payload = public_leaderboard(settings)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["target_version"] == "Ver.vps"
    assert payload["match_count"] == 1
    assert payload["side_sample_count"] == 2
    assert payload["top_cards"][0]["card_hash"] in {"card-a", "card-b", "card-c"}
    assert payload["top_decks"][0]["deck_name"] == "Card B(2.0 騎兵) / Card A(1.0 槍兵)"
    assert payload["top_decks"][0]["win_count"] == 1
    assert payload["top_decks"][0]["wilson_lower_bound"] is not None
    assert payload["top_decks"][0]["cards"][0]["image_url"] == "https://image.eiketsu-taisen.net/general/card_small/card-b.jpg"
    assert "alice" not in serialized
    assert "friend" not in serialized


def test_public_leaderboard_returns_all_ranked_decks_by_default(tmp_path):
    settings = _settings(tmp_path / "server")
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    for index in range(25):
        _insert_match(
            settings,
            f"full-rank-{index}",
            [f"player-card-{index}"],
            [f"enemy-card-{index}"],
            played_at=f"2026-05-11 10:{index:02d}",
        )

    payload = public_leaderboard(settings)
    limited = public_leaderboard(settings, limit=20)

    assert len(payload["top_decks"]) == 50
    assert len(payload["top_cards"]) == 50
    assert len(limited["top_decks"]) == 20


def test_public_leaderboard_persists_snapshot_for_repeated_filters(tmp_path, monkeypatch):
    settings = _settings(tmp_path / "server")
    engine = _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    _insert_match(settings, "snapshot-1", ["card-a"], ["card-b"])

    payload = public_leaderboard(settings, include_archetypes=False)

    with Session(engine) as session:
        [snapshot] = session.scalars(select(ServerLeaderboardSnapshot)).all()
        assert snapshot.scope == "public"
        assert snapshot.cluster_enabled == 0
        assert snapshot.payload_json["top_decks"] == payload["top_decks"]

    _clear_leaderboard_cache()

    def _fail_load_matches(*_args, **_kwargs):
        raise AssertionError("snapshot should avoid live match loading")

    monkeypatch.setattr("eiketsu_env.services.server_share._load_leaderboard_matches", _fail_load_matches)

    cached = public_leaderboard(settings, include_archetypes=False)

    assert cached["top_decks"] == payload["top_decks"]


def test_refresh_public_leaderboard_snapshots_builds_filter_matrix(tmp_path):
    settings = _settings(tmp_path / "server")
    engine = _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    _insert_match(settings, "snapshot-refresh-1", ["card-a"], ["card-b"])

    result = refresh_public_leaderboard_snapshots(settings)

    assert result["status"] == "completed"
    assert len(result["refreshed"]) == 8
    with Session(engine) as session:
        snapshots = session.scalars(select(ServerLeaderboardSnapshot)).all()
        assert len(snapshots) == 8
        assert {snapshot.rank_scope for snapshot in snapshots} == {"all", "traveler_down", "knight_down", "knight_up"}
        assert {snapshot.cluster_enabled for snapshot in snapshots} == {0, 1}


def test_upload_clears_leaderboard_snapshots(tmp_path):
    settings = _settings(tmp_path / "server")
    engine = _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    token = bind_invite(settings, create_invite(settings, "friend", code="SNAP-CLEAR").code, "alice").api_token
    import_uploaded_package(settings, token, _package_text(tmp_path / "snap-source-a", "alice", replay_id="snap-a"))
    public_leaderboard(settings, include_archetypes=False)

    with Session(engine) as session:
        assert session.scalar(select(ServerLeaderboardSnapshot)) is not None

    import_uploaded_package(settings, token, _package_text(tmp_path / "snap-source-b", "alice", replay_id="snap-b"))

    with Session(engine) as session:
        assert session.scalars(select(ServerLeaderboardSnapshot)).all() == []


def test_leaderboard_rank_scope_filters_sides_and_counts_players(tmp_path):
    settings = _settings(tmp_path / "server")
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    _insert_match(
        settings,
        "rank-low-1",
        ["traveler-card"],
        ["knight-card"],
        player_name="Traveler",
        enemy_name="Knight",
        player_profile={"段位": "旅人", "証": "9"},
        enemy_profile={"段位": "騎士1", "証": "50"},
    )
    _insert_match(
        settings,
        "rank-low-2",
        ["traveler-card"],
        ["baron-card"],
        player_name="Traveler",
        enemy_name="Baron",
        player_profile={"段位": "旅人", "証": "8"},
        enemy_profile={"段位": "男爵1", "証": "100"},
    )
    _insert_match(
        settings,
        "rank-mid-1",
        ["squire-card"],
        ["baron-card"],
        player_name="Squire",
        enemy_name="Baron",
        player_profile={"段位": "従騎士2", "証": "45"},
        enemy_profile={"段位": "男爵1", "証": "100"},
    )

    traveler = public_leaderboard(settings, rank_scope="traveler_down")
    knight_down = public_leaderboard(settings, rank_scope="knight_down")
    knight_up = public_leaderboard(settings, rank_scope="knight_up")

    assert traveler["rank_scope_label"] == "旅人以下"
    assert traveler["side_sample_count"] == 2
    assert traveler["match_count"] == 2
    assert traveler["top_decks"][0]["deck_fingerprint"] == "traveler-card"
    assert traveler["top_decks"][0]["top_player"] == "Traveler"
    assert traveler["top_decks"][0]["top_player_count"] == 2
    assert traveler["top_decks"][0]["player_count"] == 1
    assert knight_down["side_sample_count"] == 4
    assert knight_up["side_sample_count"] == 3


def test_personal_leaderboard_only_counts_current_user_uploads(tmp_path):
    settings = _settings(tmp_path / "server")
    _write_card_catalog(settings)
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    alice_token = bind_invite(settings, create_invite(settings, "alice", code="INVITEA").code, "alice").api_token
    bob_token = bind_invite(settings, create_invite(settings, "bob", code="INVITEB").code, "bob").api_token
    import_uploaded_package(settings, alice_token, _package_text(tmp_path / "alice-source", "alice", replay_id="alice-replay"))
    import_uploaded_package(settings, bob_token, _package_text(tmp_path / "bob-source", "bob", replay_id="bob-replay"))

    public_payload = public_leaderboard(settings)
    alice_payload = personal_leaderboard(settings, alice_token)
    bob_payload = personal_leaderboard(settings, bob_token)

    assert public_payload["scope"] == "public"
    assert public_payload["match_count"] == 2
    assert alice_payload["scope"] == "mine"
    assert alice_payload["contributor_name"] == "alice"
    assert alice_payload["upload_count"] == 1
    assert alice_payload["match_count"] == 1
    assert alice_payload["side_sample_count"] == 2
    assert bob_payload["match_count"] == 1


def test_contributor_leaderboard_uses_binding_name_without_token(tmp_path):
    settings = _settings(tmp_path / "server")
    _write_card_catalog(settings)
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    alice_token = bind_invite(settings, create_invite(settings, "alice", code="NAMEA").code, "same-name").api_token
    bob_token = bind_invite(settings, create_invite(settings, "bob", code="NAMEB").code, "other-name").api_token
    import_uploaded_package(settings, alice_token, _package_text(tmp_path / "name-a", "same-name", replay_id="name-a"))
    import_uploaded_package(settings, bob_token, _package_text(tmp_path / "name-b", "other-name", replay_id="name-b"))

    payload = contributor_leaderboard(settings, "same-name")
    missing = contributor_leaderboard(settings, "nobody")

    assert payload["scope"] == "contributor"
    assert payload["contributor_name"] == "same-name"
    assert payload["contributor_found"] is True
    assert payload["upload_count"] == 1
    assert payload["match_count"] == 1
    assert payload["side_sample_count"] == 2
    assert missing["contributor_found"] is False
    assert missing["match_count"] == 0


def test_leaderboard_filter_form_uses_contributor_name():
    html = _leaderboard_visual_page(
        {
            "scope": "public",
            "scope_label": "公开匿名聚合",
            "target_version": "Ver.vps",
            "date_from": "2026-05-10",
            "date_to": "2026-05-12",
            "upload_count": 0,
            "match_count": 0,
            "side_sample_count": 0,
            "generated_at": "",
            "top_decks": [],
            "top_archetypes": [],
        }
    )

    assert 'name="contributor"' in html
    assert "绑定用户名" in html
    assert "客户端 token" not in html


def test_leaderboard_mobile_layout_keeps_personal_filter_collapsed():
    html = _leaderboard_visual_page(
        {
            "scope": "public",
            "scope_label": "公开匿名聚合",
            "target_version": "Ver.vps",
            "date_from": "2026-05-10",
            "date_to": "2026-05-12",
            "upload_count": 2,
            "match_count": 12,
            "side_sample_count": 24,
            "generated_at": "2026-05-16T16:00:24",
            "top_decks": [],
            "top_archetypes": [],
        }
    )

    assert "scope-mobile-drawer" in html
    assert "leaderboard-mobile-summary" in html
    assert "12 对局" in html
    assert "/web/static/leaderboard.css" in html
    assert "/web/static/leaderboard.js" in html


def test_leaderboard_html_display_limit_defaults_to_lightweight_mode():
    assert _leaderboard_display_limit(None, "") == LEADERBOARD_HTML_DEFAULT_LIMIT
    assert _leaderboard_display_limit(9999, "") == LEADERBOARD_HTML_MAX_LIMIT
    assert _leaderboard_display_limit(0, "") == 1
    assert _leaderboard_display_limit(20, "1") is None


def test_leaderboard_limited_html_links_to_full_page():
    archetype = {
        "title": "Deck A 系",
        "member_count": 1,
        "sample_count": 3,
        "win_count": 2,
        "loss_count": 1,
        "draw_count": 0,
        "top_player": "Player One",
        "top_player_count": 2,
        "player_count": 1,
        "core_cards": [],
        "member_decks": [],
    }
    html = _leaderboard_visual_page(
        {
            "scope": "public",
            "scope_label": "公开匿名聚合",
            "target_version": "Ver.vps",
            "date_from": "2026-05-10",
            "date_to": "2026-05-12",
            "rank_scope": "all",
            "upload_count": 2,
            "match_count": 12,
            "side_sample_count": 24,
            "generated_at": "",
            "top_decks": [],
            "top_archetypes": [
                archetype,
                {**archetype, "title": "Deck B 系"},
                {**archetype, "title": "Deck C 系"},
            ],
        },
        display_limit=2,
    )

    assert "2 / 3" in html
    assert "加载更多" in html
    assert "full=1" in html
    assert "cluster=on" in html
    assert "rank_scope=all" in html


def test_leaderboard_rows_response_returns_next_page_html():
    decks = [
        {
            "deck_name": f"Deck {index}",
            "deck_fingerprint": f"deck-{index}",
            "sample_count": 10 - index,
            "wilson_lower_bound": 0.9 - index * 0.1,
            "win_count": 1,
            "loss_count": 0,
            "draw_count": 0,
            "cards": [],
        }
        for index in range(4)
    ]
    response = _leaderboard_rows_response(
        {
            "scope": "public",
            "rank_scope": "all",
            "top_decks": decks,
            "top_archetypes": [],
        },
        cluster_enabled=False,
        contributor_name="",
        offset=2,
        limit=1,
        sort_key="wilson",
    )

    assert response["next_offset"] == 3
    assert response["has_more"] is True
    assert response["total"] == 4
    assert "Deck 2" in response["html"]
    assert "Deck 1" not in response["html"]


def test_leaderboard_view_controls_can_disable_clustering():
    deck = {
        "deck_name": "Deck A",
        "deck_fingerprint": "deck-a",
        "sample_count": 3,
        "win_count": 2,
        "loss_count": 1,
        "draw_count": 0,
        "top_player": "Player One",
        "top_player_count": 2,
        "player_count": 2,
        "cards": [],
    }
    html = _leaderboard_visual_page(
        {
            "scope": "public",
            "scope_label": "公开匿名聚合",
            "target_version": "Ver.vps",
            "date_from": "2026-05-10",
            "date_to": "2026-05-12",
            "rank_scope": "all",
            "upload_count": 2,
            "match_count": 3,
            "side_sample_count": 6,
            "generated_at": "",
            "top_decks": [deck],
            "top_archetypes": [
                {
                    "title": "Deck A 系",
                    "member_count": 1,
                    "sample_count": 3,
                    "win_count": 2,
                    "loss_count": 1,
                    "draw_count": 0,
                    "top_player": "Player One",
                    "top_player_count": 2,
                    "player_count": 2,
                    "member_decks": [deck],
                }
            ],
        },
        cluster_enabled=False,
    )

    assert "聚类" in html
    assert "段位" in html
    assert "id=\"deck-ranking\"" in html
    assert "id=\"archetype-ranking\"" not in html
    assert "最多玩家：Player One（2次）" in html
    assert "统计玩家：2人" in html
    assert "rank_scope=traveler_down" in html
    assert "cluster=off" in html


def test_leaderboard_css_keeps_disabled_cluster_view_compact():
    css = (
        Path(__file__).parents[1] / "src" / "eiketsu_env" / "web" / "static" / "leaderboard.css"
    ).read_text(encoding="utf-8")
    compact_rule = css[css.index(".ranking-board .unit") : css.index(".variant-cards .unit")]

    assert "width: 44px;" in compact_rule
    assert "min-width: 44px;" in compact_rule


def test_public_leaderboard_groups_deck_archetypes_by_shared_cost(tmp_path):
    settings = _settings(tmp_path / "server")
    _write_card_catalog(settings)
    _init_db(settings)
    set_server_config(settings, "Ver.vps", "2026-05-10", "2026-05-12")
    _insert_match(settings, "archetype-1", ["card-a", "card-b", "card-c"], ["card-d"], "2026-05-11 09:00")
    _insert_match(settings, "archetype-2", ["card-b", "card-c", "card-d"], ["card-e"], "2026-05-11 10:00")
    _insert_match(settings, "archetype-3", ["card-e", "card-f"], ["card-a"], "2026-05-11 11:00")

    payload = public_leaderboard(settings)

    target = next(
        archetype
        for archetype in payload["top_archetypes"]
        if {"card-b", "card-c"} <= {card["card_hash"] for card in archetype["core_cards"]}
    )
    assert target["similar_cost_threshold"] == 5.0
    assert target["member_count"] == 2
    assert target["member_deck_count"] == 2
    assert target["sample_count"] == 2
    assert target["win_count"] == 2
    assert target["loss_count"] == 0
    assert len(target["member_decks"]) == 2
    assert target["representative_deck"]["deck_fingerprint"] in {
        "card-a,card-b,card-c",
        "card-b,card-c,card-d",
    }


def test_upload_rejects_cookie_fields(tmp_path):
    settings = _settings(tmp_path)
    _init_db(settings)
    token = bind_invite(settings, create_invite(settings, "friend", code="INVITE4").code, "alice").api_token
    unsafe = json.dumps(
        {
            "record_type": "manifest",
            "schema_version": "share_v1",
            "package_id": "unsafe",
            "contributor_id": "alice",
            "target_version": "Ver.vps",
            "date_from": "2026-05-10",
            "date_to": "2026-05-12",
            "body_hash": "",
            "match_count": 0,
            "cookies": "secret",
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="禁止字段"):
        import_uploaded_package(settings, token, unsafe)
