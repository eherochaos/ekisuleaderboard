"""读取外部卡牌主数据，把采集到的卡牌 hash 转成可读名称。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from eiketsu_env.config import Settings


@dataclass(slots=True)
class CardLookup:
    cards_by_hash: dict[str, dict[str, Any]]
    local_card_small_dirs: tuple[Path, ...] = ()

    def label(self, card_hash: str) -> str:
        card = self.cards_by_hash.get(card_hash)
        if not card:
            return f"未识别卡({card_hash[:8]})"
        name = _first_text(card, "name", "rawName", "card_code") or card_hash
        unit_type = _first_text(card, "unitType", "unit_type")
        cost = _first_text(card, "cost", "cost_label")
        suffix = " ".join(item for item in [cost, unit_type] if item)
        return f"{name}({suffix})" if suffix else name

    def names(self, hashes: list[str]) -> list[str]:
        return [self.label(card_hash) for card_hash in hashes if card_hash]

    def image_url(self, card_hash: str) -> str:
        card = self.cards_by_hash.get(card_hash)
        return _card_image_url(card) if card else ""

    def card_code(self, card_hash: str) -> str:
        card = self.cards_by_hash.get(card_hash)
        return _first_text(card, "card_code", "cardCode", "code") if card else ""

    def official_card_small_url(self, card_hash: str) -> str:
        card = self.cards_by_hash.get(card_hash)
        asset_code = _card_small_asset_code(card, card_hash) if card else card_hash
        if not asset_code:
            return ""
        return f"https://image.eiketsu-taisen.net/general/card_small/{quote(asset_code + '.jpg', safe='.')}"

    def cost_value(self, card_hash: str) -> float:
        card = self.cards_by_hash.get(card_hash)
        return _card_cost_value(card) if card else 0.0

    def local_image_path(self, card_hash: str) -> Path | None:
        card = self.cards_by_hash.get(card_hash)
        if not card:
            return None
        card_code = _first_text(card, "card_code", "cardCode", "code")
        if not card_code:
            return None
        for image_dir in self.local_card_small_dirs:
            for suffix in (".jpg", ".png", ".webp"):
                path = image_dir / f"{card_code}{suffix}"
                if path.exists():
                    return path
        return None


def load_card_lookup(settings: Settings) -> CardLookup:
    path = settings.card_catalog_path
    if not path:
        return CardLookup({})

    return CardLookup(_load_cards_by_hash(path), _local_card_small_dirs(path))


def _load_cards_by_hash(path: Path) -> dict[str, dict[str, Any]]:
    try:
        if not path.exists():
            return {}
    except OSError:
        # 卡牌主数据是外部库；读不到时导出仍继续，只是退回 hash，避免采集成果不可用。
        return {}

    if path.is_dir():
        return _load_from_database_root(path)

    suffix = path.suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return _merge_card_catalog_overlay(path, _load_from_sqlite(path))
    if suffix == ".json":
        return _merge_card_catalog_overlay(path, _load_from_json(path))
    return {}


def _load_from_database_root(root: Path) -> dict[str, dict[str, Any]]:
    sqlite_path = root / "data" / "db" / "cards.sqlite3"
    if sqlite_path.exists():
        by_hash = _load_from_sqlite(sqlite_path)
        if by_hash:
            return by_hash

    # 部分受限环境会禁止 sqlite 读外部库；raw base 同属 eki_database_v2，仍可作为事实来源。
    latest_base = _latest_official_base_snapshot(root)
    if latest_base is not None:
        by_hash = _load_from_json(latest_base)
        if by_hash:
            return by_hash

    legacy_catalog = root / "data" / "catalog" / "official" / "cards.json"
    if legacy_catalog.exists():
        return _load_from_json(legacy_catalog)
    return {}


def _local_card_small_dirs(path: Path) -> tuple[Path, ...]:
    root = path if path.is_dir() else path.parent
    candidates = (
        root / "apps" / "web" / "public" / "assets" / "cards" / "card_small",
        root / "apps" / "web" / "dist" / "assets" / "cards" / "card_small",
    )
    return tuple(candidate for candidate in candidates if candidate.exists())


def _load_from_sqlite(path: Path) -> dict[str, dict[str, Any]]:
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            table_name = _pick_card_table(connection)
            if table_name is None:
                return {}
            columns = _table_columns(connection, table_name)
            wanted_columns = [
                column
                for column in (
                    "card_code",
                    "name",
                    "cost_label",
                    "cost_value",
                    "unit_type",
                    "faction",
                    "rarity",
                    "card_type",
                    "image_keys_json",
                    "image_urls_json",
                    "official_url",
                    "wiki_url",
                )
                if column in columns
            ]
            if not wanted_columns:
                return {}
            rows = connection.execute(
                f"SELECT {', '.join(wanted_columns)} FROM {table_name}"
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return {}

    by_hash: dict[str, dict[str, Any]] = {}
    for row in rows:
        card = _normalize_sqlite_card(dict(row))
        for hash_id in _sqlite_card_hashes(card):
            by_hash[hash_id] = {**card, "hash_id": hash_id}
    return by_hash


def _load_from_json(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if isinstance(payload, dict) and "general" in payload:
        return _load_from_official_base_payload(payload)

    cards = payload.get("cards") if isinstance(payload, dict) else payload
    by_hash: dict[str, dict[str, Any]] = {}
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            for hash_id in _catalog_card_hashes(card):
                by_hash[hash_id] = {**card, "hash_id": hash_id}
    return by_hash


def _load_from_official_base_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("general")
    if not isinstance(rows, list):
        return {}

    by_hash: dict[str, dict[str, Any]] = {}
    for raw_row_text in rows:
        if not isinstance(raw_row_text, str):
            continue
        raw = raw_row_text.split(",")
        if len(raw) < 16:
            continue

        image_keys = {
            "card_small": raw[0],
            "card_ds": raw[1],
            "card_face": raw[2],
        }
        hash_id = raw[0]
        card_number = _parse_int(raw[12])
        card = {
            "hash_id": hash_id,
            "card_code": _build_official_card_code(payload, raw[10], card_number),
            "name": raw[3],
            "faction": _lookup_official_name(payload, "color", raw[5]),
            "card_type": _lookup_official_name(payload, "cardType", raw[11]),
            "cost": _lookup_official_name(payload, "cost", raw[13]),
            "unitType": _lookup_official_name(payload, "unitType", raw[15]),
            "image_keys": image_keys,
        }
        for candidate_hash in _catalog_card_hashes(card):
            by_hash[candidate_hash] = {**card, "hash_id": candidate_hash}
    return by_hash


def _merge_card_catalog_overlay(path: Path, cards_by_hash: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    overlay_path = path.with_name("card_catalog_overlay.json")
    try:
        if not overlay_path.exists():
            return cards_by_hash
    except OSError:
        return cards_by_hash
    overlay_cards = _load_from_json(overlay_path)
    if not overlay_cards:
        return cards_by_hash
    return {**cards_by_hash, **overlay_cards}


def _latest_official_base_snapshot(root: Path) -> Path | None:
    base_dir = root / "data" / "raw" / "official" / "base"
    try:
        paths = [path for path in base_dir.glob("*.json") if path.is_file()]
    except OSError:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime) if paths else None


def _pick_card_table(connection: sqlite3.Connection) -> str | None:
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table_name in ("cards", "official_cards"):
        if table_name in table_names:
            return table_name
    return None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _normalize_sqlite_card(row: dict[str, Any]) -> dict[str, Any]:
    cost_label = _nullable_text(row.get("cost_label"))
    return {
        "card_code": _nullable_text(row.get("card_code")),
        "name": _nullable_text(row.get("name")),
        "cost": cost_label or _nullable_text(row.get("cost_value")),
        "cost_label": cost_label,
        "unitType": _nullable_text(row.get("unit_type")),
        "unit_type": _nullable_text(row.get("unit_type")),
        "faction": _nullable_text(row.get("faction")),
        "rarity": _nullable_text(row.get("rarity")),
        "card_type": _nullable_text(row.get("card_type")),
        "official_url": _nullable_text(row.get("official_url")),
        "wiki_url": _nullable_text(row.get("wiki_url")),
        "image_keys": _json_dict(row.get("image_keys_json")),
        "image_urls": _json_dict(row.get("image_urls_json")),
    }


def _sqlite_card_hashes(card: dict[str, Any]) -> list[str]:
    hashes: list[str] = []
    for value in card.get("image_keys", {}).values():
        text = _nullable_text(value)
        if text:
            hashes.append(text)
    card_code = _nullable_text(card.get("card_code"))
    if card_code:
        hashes.append(card_code)
    return list(dict.fromkeys(hashes))


def _catalog_card_hashes(card: dict[str, Any]) -> list[str]:
    candidates = [
        card.get("hash_id"),
        card.get("hashId"),
        card.get("card_hash"),
        card.get("cardHash"),
        card.get("code"),
        card.get("card_small_code"),
        card.get("cardSmallCode"),
    ]
    for image_keys_key in ("image_keys", "imageKeys", "image_keys_json"):
        image_keys = _json_dict(card.get(image_keys_key))
        candidates.extend(image_keys.values())
    return [
        text
        for text in (_nullable_text(candidate) for candidate in candidates)
        if text
    ]


def _card_image_url(card: dict[str, Any]) -> str:
    # 卡组图优先使用官网小卡图；没有时再退到详情图或头像图。
    for container_key in ("image_urls", "imageUrls", "images", "image_urls_json"):
        image_urls = _json_dict(card.get(container_key))
        for key in (
            "card_small",
            "cardSmall",
            "card_small_url",
            "cardSmallUrl",
            "small",
            "card_ds",
            "cardDs",
            "card_ds_url",
            "cardDsUrl",
            "card_face",
            "cardFace",
            "card_face_url",
            "cardFaceUrl",
            "face",
        ):
            text = _nullable_text(image_urls.get(key))
            if text:
                return text
        for value in image_urls.values():
            text = _nullable_text(value)
            if text:
                return text

    for key in (
        "image_url",
        "imageUrl",
        "card_small_url",
        "cardSmallUrl",
        "card_ds_url",
        "cardDsUrl",
        "card_face_url",
        "cardFaceUrl",
    ):
        text = _nullable_text(card.get(key))
        if text:
            return text
    return ""


def _card_small_asset_code(card: dict[str, Any], fallback: str) -> str:
    for image_keys_key in ("image_keys", "imageKeys", "image_keys_json"):
        image_keys = _json_dict(card.get(image_keys_key))
        for key in ("card_small_code", "cardSmallCode", "card_small", "cardSmall"):
            text = _nullable_text(image_keys.get(key))
            if text:
                return text
    return _first_text(card, "card_small_code", "cardSmallCode", "hash_id", "hashId", "card_hash", "cardHash") or fallback


def _card_cost_value(card: dict[str, Any]) -> float:
    for key in ("cost", "cost_label", "cost_value", "costValue"):
        text = _nullable_text(card.get(key))
        if not text:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return 0.0


def _lookup_official_name(payload: dict[str, Any], table_name: str, raw_index: str) -> str:
    rows = payload.get(table_name)
    index = _parse_int(raw_index)
    if not isinstance(rows, list) or index is None or index < 0 or index >= len(rows):
        return ""
    row = rows[index]
    if not isinstance(row, str):
        return ""
    parts = row.split(",")
    return parts[1] if len(parts) > 1 else parts[0]


def _build_official_card_code(payload: dict[str, Any], raw_index: str, card_number: int | None) -> str:
    index_initial = payload.get("indexInitial")
    index = _parse_int(raw_index)
    if not isinstance(index_initial, list) or index is None or card_number is None:
        return ""
    if index < 0 or index >= len(index_initial):
        return ""
    return f"{index_initial[index]}{card_number:03d}"


def _first_text(card: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _nullable_text(card.get(key))
        if text:
            return text
    return ""


def _nullable_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"", "None"} else text


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
