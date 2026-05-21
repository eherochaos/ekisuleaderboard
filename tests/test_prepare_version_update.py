from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_prepare_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "prepare_version_update.py"
    spec = importlib.util.spec_from_file_location("prepare_version_update", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_prepare_version_update_updates_config_and_overlay(tmp_path: Path):
    prepare = _load_prepare_script()
    root = tmp_path / "repo"
    official_root = tmp_path / "eki_database_v2"
    (root / "shared").mkdir(parents=True)
    (root / "assets").mkdir(parents=True)
    (root / "src" / "eiketsu_env").mkdir(parents=True)
    base_dir = official_root / "data" / "raw" / "official" / "base"
    base_dir.mkdir(parents=True)

    (root / "shared" / "share_config.json").write_text(
        json.dumps({"target_version": "Ver.old", "date_from": "2026-01-01", "date_to": "2026-01-01"}),
        encoding="utf-8",
    )
    (root / "src" / "eiketsu_env" / "config.py").write_text(
        'VERSION_START_DATES = {\n    "Ver.old": "2026-01-01",\n}\n',
        encoding="utf-8",
    )
    (root / "assets" / "card_catalog.json").write_text(
        json.dumps({"cards": [{"hash_id": "old-hash", "name": "Old Card"}]}),
        encoding="utf-8",
    )
    (base_dir / "official-base.json").write_text(
        json.dumps(
            {
                "general": [
                    "old-hash,old-ds,old-face,Old Card,old,0,0,0,0,0,0,0,1,0,0,0",
                    "new-hash,new-ds,new-face,New Card,new,0,0,0,0,0,0,0,2,1,0,1",
                ],
                "indexInitial": ["A"],
                "color": ["blue,Blue"],
                "cardType": ["normal,Normal"],
                "cost": ["c1,1.0", "c2,2.0"],
                "unitType": ["spear,Spear", "bow,Bow"],
            }
        ),
        encoding="utf-8",
    )

    result = prepare.prepare_version_update(
        root=root,
        official_root=official_root,
        version="Ver.new",
        start_date="2026-06-01",
        date_to="2026-06-01",
    )

    share_config = json.loads((root / "shared" / "share_config.json").read_text(encoding="utf-8"))
    config_text = (root / "src" / "eiketsu_env" / "config.py").read_text(encoding="utf-8")
    overlay = json.loads((root / "assets" / "card_catalog_overlay.json").read_text(encoding="utf-8"))

    assert result.overlay_card_count == 1
    assert share_config["target_version"] == "Ver.new"
    assert share_config["date_from"] == "2026-06-01"
    assert '"Ver.new": "2026-06-01"' in config_text
    assert overlay["generated_for"] == "Ver.new"
    assert overlay["cards"][0]["hash_id"] == "new-hash"
    assert overlay["cards"][0]["image_keys"]["card_face"] == "new-face"
