from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.exporter import export_matches
from eiketsu_env.services.repository import EnvRepository


def _settings(tmp_path: Path) -> Settings:
    catalog_path = tmp_path / "cards.json"
    catalog_path.write_text(
        json.dumps(
            {
                "cards": [
                    {"hash_id": "hash-a", "name": "Card A", "cost": "1.5", "unitType": "Spear"},
                    {"hash_id": "hash-b", "name": "Card B", "cost": "2.0", "unitType": "Archer"},
                    {"hash_id": "hash-c", "name": "Card C", "cost": "3.0", "unitType": "Cavalry"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=catalog_path,
    )


def _detail() -> dict:
    return {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "url": "https://eiketsu-taisen.net/members/history/detail?t=1773932045&f=586",
        "follow_id": "586",
        "played_at": "2026-03-20 23:54",
        "date": "2026-03-20 23:54",
        "mode": "全国対戦",
        "version": "Ver.3.1.0H",
        "result": "win",
        "replay_id": "b925ff0584d242fa895d47e03306a4b8",
        "play_url": "https://eiketsu-taisen.net/members/enbujyo/play?p=b925ff0584d242fa895d47e03306a4b8",
        "m3u8_url": "https://dl.eiketsu-taisen.net/live/b925ff0584d242fa895d47e03306a4b8/master.m3u8",
        "castle_breakdown": {"rows": []},
        "timeline_labels": [],
        "timeline_data": {},
        "players": [
            {"side_index": 1, "role": "player", "player_name": "A", "follow_id": "586", "result": "win", "deck_ids": ["hash-a", "hash-b", "hash-missing"]},
            {"side_index": 2, "role": "enemy", "player_name": "B", "deck_ids": ["hash-c"]},
        ],
    }


def test_export_matches_adds_readable_deck_names(tmp_path):
    settings = _settings(tmp_path)
    engine = make_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repo = EnvRepository(session, settings)
        repo.upsert_match_detail(_detail())
        session.commit()

    csv_path = export_matches(settings, "csv")
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "Card A(1.5 Spear) / Card B(2.0 Archer) / 未识别卡(hash-mis)" in csv_text
    assert "Card C(3.0 Cavalry)" in csv_text

    md_path = export_matches(settings, "md")
    md_text = md_path.read_text(encoding="utf-8")
    assert "# 英杰大战环境对局导出" in md_text
    assert "卡名映射：已加载" in md_text
    assert "Card A(1.5 Spear) / Card B(2.0 Archer) / 未识别卡(hash-mis)" in md_text
