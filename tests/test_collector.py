from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import RawSnapshot
from eiketsu_env.db.session import make_engine
from eiketsu_env.services import collector
from eiketsu_env.services.collector import _existing_detail_is_complete, _filter_active_players, collect_follow
from eiketsu_env.services.repository import EnvRepository
from eiketsu_env.utils import JST


def _settings(tmp_path: Path) -> Settings:
    return Settings(root_dir=tmp_path, db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}", firefox_profile=tmp_path / "ff")


def _detail() -> dict:
    return {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "follow_id": "586",
        "played_at": "2026-05-10 23:54",
        "date": "2026-05-10 23:54",
        "mode": "全国対戦",
        "version": "Ver.3.1.0H",
        "result": "win",
        "castle_breakdown": {"rows": [{"player": "82.00%", "label": "castle", "enemy": "0.00%"}]},
        "timeline_labels": [],
        "timeline_data": {},
        "players": [
            {
                "side_index": 1,
                "role": "player",
                "player_name": "A",
                "follow_id": "586",
                "result": "win",
                "castle_rate": "82.00%",
                "deck_ids": ["hash-a", "hash-b"],
                "profile": {"全国主君ランキング": "12 位"},
            },
            {
                "side_index": 2,
                "role": "enemy",
                "player_name": "B",
                "result": "loss",
                "castle_rate": "0.00%",
                "deck_ids": ["hash-c"],
                "profile": {"全国主君ランキング": "80 位"},
            },
        ],
    }


def test_filter_active_players_skips_players_inactive_before_range():
    old_timestamp = int(datetime(2026, 5, 4, 23, 59, tzinfo=JST).timestamp())
    fresh_timestamp = int(datetime(2026, 5, 5, 0, 1, tzinfo=JST).timestamp())

    players, skipped = _filter_active_players(
        [
            {"follow_id": "old", "lastplaytime": str(old_timestamp)},
            {"follow_id": "fresh", "lastplaytime": str(fresh_timestamp)},
            {"follow_id": "unknown"},
        ],
        "2026-05-05",
    )

    assert skipped == 1
    assert [player["follow_id"] for player in players] == ["fresh", "unknown"]


def test_existing_detail_is_complete_detects_reusable_follow_detail(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    seed = {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "follow_id": "586",
    }

    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail())
        session.commit()

        assert _existing_detail_is_complete(session, seed)


def test_collect_follow_can_skip_raw_html_snapshots(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    class FakeMember:
        def fetch_text(self, url, referer=None):
            return f"<html>{url}</html>", url

    monkeypatch.setattr(collector, "create_member_session", lambda *args, **kwargs: FakeMember())
    monkeypatch.setattr(collector, "parse_follow_html", lambda html, url, base_url: [{"follow_id": "586", "name": "A"}])
    monkeypatch.setattr(collector, "parse_follow_api_json", lambda payload, base_url: [])
    monkeypatch.setattr(
        collector,
        "parse_daily_html",
        lambda html, url, base_url, iso_date, player: [
            {
                "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
                "follow_id": "586",
                "mode": "全国対戦",
            }
        ],
    )
    monkeypatch.setattr(collector, "parse_detail_html", lambda html, url, base_url, seed: _detail())

    result = collect_follow(settings, "2026-05-10", "2026-05-10", save_raw_snapshots=False)

    assert result.status == "completed"
    assert settings.raw_dir.exists() is False
    with Session(engine) as session:
        assert session.query(RawSnapshot).count() == 0
