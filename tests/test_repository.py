from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import Match, MatchAlias
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.repository import EnvRepository


def _settings(tmp_path: Path) -> Settings:
    return Settings(root_dir=tmp_path, db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}", firefox_profile=tmp_path / "ff")


def _detail(replay_id: str = "") -> dict:
    play_url = f"https://eiketsu-taisen.net/members/enbujyo/play?p={replay_id}" if replay_id else ""
    return {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "follow_id": "586",
        "played_at": "2026-03-20 23:54",
        "date": "2026-03-20 23:54",
        "mode": "全国対戦",
        "version": "Ver.3.1.0H",
        "result": "win",
        "replay_id": replay_id,
        "play_url": play_url,
        "m3u8_url": f"https://dl.eiketsu-taisen.net/live/{replay_id}/master.m3u8" if replay_id else "",
        "castle_breakdown": {"rows": []},
        "timeline_labels": [],
        "timeline_data": {},
        "players": [
            {"side_index": 1, "role": "player", "player_name": "A", "follow_id": "586", "result": "win", "deck_ids": ["hash-a", "hash-b"]},
            {"side_index": 2, "role": "enemy", "player_name": "B", "deck_ids": ["hash-c"]},
        ],
    }


def test_upsert_match_merges_detail_alias_into_replay_id(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        first = repo.upsert_match_detail(_detail())
        second = repo.upsert_match_detail(_detail("b925ff0584d242fa895d47e03306a4b8"))
        session.commit()

        assert first.id == second.id
        matches = session.scalars(select(Match)).all()
        aliases = session.scalars(select(MatchAlias).order_by(MatchAlias.alias)).all()
        assert len(matches) == 1
        assert matches[0].public_id == "r:b925ff0584d242fa895d47e03306a4b8"
        assert matches[0].id_state == "replay"
        assert matches[0].played_at == "2026-03-19 23:54"
        assert [alias.alias for alias in aliases] == [
            "d:586:1773932045",
            "r:b925ff0584d242fa895d47e03306a4b8",
        ]
        assert matches[0].decks[0].deck_fingerprint == "hash-a,hash-b"
        assert [unit.card_hash for unit in matches[0].decks[0].units] == ["hash-a", "hash-b"]


def test_video_search_duplicate_keeps_follow_detail_fields(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import Session

    replay_id = "b925ff0584d242fa895d47e03306a4b8"
    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        follow_detail = _detail(replay_id)
        follow_detail["castle_breakdown"] = {"rows": [{"player": "82.00%", "label": "castle", "enemy": "0.00%"}]}
        follow_detail["timeline_data"] = {"castle": {"player": [10000, 8200], "enemy": [10000, 0]}}
        follow_detail["players"][0]["profile"] = {"rank": "follow-player"}
        follow_detail["players"][1]["profile"] = {"rank": "follow-enemy"}

        first = repo.upsert_match_detail(follow_detail)
        session.commit()

        play_url = f"https://eiketsu-taisen.net/members/enbujyo/play?p={replay_id}&type=video_search"
        video_detail = {
            **_detail(replay_id),
            "source_type": "video_search",
            "detail_url": play_url,
            "url": play_url,
            "source_url": play_url,
            "follow_id": "",
            "played_at": "2026-03-20 12:00",
            "date": "2026-03-20 12:00",
            "result": "unknown",
            "castle_breakdown": {},
            "timeline_data": {},
            "players": [
                {"side_index": 1, "role": "player", "player_name": "Video A", "result": "unknown", "deck_ids": ["video-a"]},
                {"side_index": 2, "role": "enemy", "player_name": "Video B", "result": "unknown", "deck_ids": ["video-b"]},
            ],
        }
        second = repo.upsert_match_detail(video_detail)
        session.commit()

        assert first.id == second.id
        match = session.get(Match, first.id)
        assert match is not None
        assert match.result == "win"
        assert match.detail_url == "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586"
        assert match.played_at == "2026-03-19 23:54"
        assert [side.player_name for side in match.sides] == ["A", "B"]
        assert match.sides[0].profile_json == {"rank": "follow-player"}
        assert [deck.deck_fingerprint for deck in match.decks] == ["hash-a,hash-b", "hash-c"]
        assert match.battle_summary is not None
        assert match.battle_summary.castle_breakdown_json["rows"][0]["player"] == "82.00%"
