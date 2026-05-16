from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from eiketsu_env.config import Settings
from eiketsu_env.services.card_lookup import load_card_lookup


def _settings(tmp_path: Path, card_catalog_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=card_catalog_path,
    )


def test_load_card_lookup_from_eki_database_sqlite(tmp_path: Path):
    catalog_root = tmp_path / "eki_database_v2"
    sqlite_path = catalog_root / "data" / "db" / "cards.sqlite3"
    sqlite_path.parent.mkdir(parents=True)
    image_dir = catalog_root / "apps" / "web" / "public" / "assets" / "cards" / "card_small"
    image_dir.mkdir(parents=True)
    local_image = image_dir / "蒼001.jpg"
    local_image.write_bytes(b"fake-image")
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            """
            CREATE TABLE cards (
                card_code TEXT PRIMARY KEY,
                name TEXT,
                cost_label TEXT,
                unit_type TEXT,
                image_keys_json TEXT,
                image_urls_json TEXT,
                wiki_url TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO cards
                (card_code, name, cost_label, unit_type, image_keys_json, image_urls_json, wiki_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "蒼001",
                "池内蔵太",
                "1.0",
                "槍兵",
                json.dumps({"card_small_code": "hash-a", "card_face_code": "face-a"}),
                json.dumps({"card_small": "https://example.test/card-small.jpg"}),
                "https://example.test/wiki",
            ),
        )

    lookup = load_card_lookup(_settings(tmp_path, catalog_root))

    assert lookup.label("hash-a") == "池内蔵太(1.0 槍兵)"
    assert lookup.label("蒼001") == "池内蔵太(1.0 槍兵)"
    assert lookup.image_url("hash-a") == "https://example.test/card-small.jpg"
    assert lookup.image_url("face-a") == "https://example.test/card-small.jpg"
    assert lookup.local_image_path("hash-a") == local_image
    assert lookup.cost_value("hash-a") == 1.0


def test_load_card_lookup_from_official_base_snapshot(tmp_path: Path):
    catalog_root = tmp_path / "eki_database_v2"
    base_dir = catalog_root / "data" / "raw" / "official" / "base"
    base_dir.mkdir(parents=True)
    (base_dir / "official-base.json").write_text(
        json.dumps(
            {
                "general": [
                    "hash-a,ds,face,池内蔵太,いけくらた,0,0,1,1,0,0,0,1,0,0,0,0,3,1,-1,-1,-1,0,0,0,0",
                ],
                "indexInitial": ["蒼"],
                "color": ["blue,蒼,0,0,0"],
                "cardType": ["normal,通常,通"],
                "cost": ["c1,1.0,10"],
                "unitType": ["spear,槍兵"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    lookup = load_card_lookup(_settings(tmp_path, catalog_root))

    assert lookup.label("hash-a") == "池内蔵太(1.0 槍兵)"
