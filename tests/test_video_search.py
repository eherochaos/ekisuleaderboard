from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from eiketsu_env.config import Settings, version_start_date
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import RawSnapshot
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.video_search import (
    _card_hashes_from_replay,
    _matches_requested_version,
    _resolve_battle_datetime,
    _resolve_frontier_rounds,
    _searched_card_hashes,
    _video_item_to_detail,
    _video_search_fields,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(root_dir=tmp_path, db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}", firefox_profile=tmp_path / "ff")


def test_video_search_fields_keep_repeated_card_hashes_and_default_range():
    fields = _video_search_fields(["card-a", "card-b"])

    assert fields[:2] == [("g", "card-a"), ("g", "card-b")]
    assert ("r", "0") in fields
    assert ("kmin", "-1") in fields
    assert ("sg", "") in fields


def test_resolve_battle_datetime_handles_year_boundary():
    assert _resolve_battle_datetime("12/31 23:59", "2025-12-31", "2026-01-01") == "2025-12-31 23:59"
    assert _resolve_battle_datetime("1/1 00:10", "2025-12-31", "2026-01-01") == "2026-01-01 00:10"
    assert _resolve_battle_datetime("5/10 12:00", "2026-05-11", "2026-05-11") == ""


def test_video_item_to_detail_marks_lightweight_source():
    detail = _video_item_to_detail(
        {
            "paramId": "replay-1",
            "thisName": "",
            "thereName": "API Enemy",
            "mode": "national",
            "package": "Ver.3.1.0H",
        },
        {
            "replay_id": "replay-1",
            "replay_url": "https://eiketsu-taisen.net/members/enbujyo/play?p=replay-1",
            "m3u8_url": "https://dl.eiketsu-taisen.net/live/replay-1/master.m3u8",
            "player_names": ["Replay Player", "Replay Enemy"],
            "follow_ids": ["586", ""],
            "player1_deck_ids": ["hash-a"],
            "player2_deck_ids": ["hash-b"],
        },
        "https://eiketsu-taisen.net/members/enbujyo/play?p=replay-1&type=video_search",
        "2026-05-11 12:34",
    )

    assert detail["source_type"] == "video_search"
    assert detail["result"] == "unknown"
    assert detail["players"][0]["player_name"] == "Replay Player"
    assert detail["players"][0]["follow_id"] == "586"
    assert detail["players"][1]["player_name"] == "API Enemy"
    assert detail["players"][1]["deck_ids"] == ["hash-b"]


def test_version_start_date_includes_current_and_previous_windows():
    assert version_start_date("Ver.3.5.0A") == "2026-05-20"
    assert version_start_date("Ver.3.1.0H") == "2026-04-22"


def test_video_search_version_filter_matches_api_package():
    assert _matches_requested_version({"package": "Ver.3.1.0H"}, "Ver.3.1.0H")
    assert not _matches_requested_version({"package": "Ver.old"}, "Ver.3.1.0H")
    assert _matches_requested_version({"package": "Ver.old"}, "")


def test_searched_card_hashes_are_restored_from_raw_snapshot_urls(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                RawSnapshot(source_kind="video_search_api", source_url="https://example.test/search_video?g=card-a", local_path="a.html", content_hash="a", parser_version="test"),
                RawSnapshot(source_kind="video_search_api", source_url="https://example.test/search_video?x=1&g=card-b&g=card-c", local_path="b.html", content_hash="b", parser_version="test"),
                RawSnapshot(source_kind="daily", source_url="https://example.test/search_video?g=ignored", local_path="c.html", content_hash="c", parser_version="test"),
            ]
        )
        session.commit()

        assert _searched_card_hashes(session) == {"card-a", "card-b", "card-c"}


def test_frontier_helpers_expand_deck_hashes_and_parse_auto_rounds():
    replay = {"player1_deck_ids": ["card-a", "card-b"], "player2_deck_ids": ["card-b", "card-c"]}

    assert _card_hashes_from_replay(replay) == ["card-a", "card-b", "card-c"]
    assert _resolve_frontier_rounds("auto") is None
    assert _resolve_frontier_rounds("2") == 2
