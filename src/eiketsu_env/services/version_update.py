from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eiketsu_env.services.card_lookup import (
    _build_official_card_code,
    _catalog_card_hashes,
    _load_from_json,
    _lookup_official_name,
    _parse_int,
)


@dataclass(slots=True)
class PreparedVersionUpdate:
    version: str
    start_date: str
    date_to: str
    latest_base_path: Path
    overlay_card_count: int
    added_overlay_card_count: int


def prepare_version_update(
    *,
    root: Path,
    official_root: Path,
    version: str,
    start_date: str,
    date_to: str,
    dry_run: bool = False,
) -> PreparedVersionUpdate:
    root = root.resolve()
    official_root = official_root.resolve()
    version = version.strip()
    start_date = start_date.strip()
    date_to = date_to.strip()
    if not version or not start_date or not date_to:
        raise ValueError("version/start_date/date_to 不能为空")

    latest_base_path = latest_official_base_snapshot(official_root)
    base_catalog_path = root / "assets" / "card_catalog.json"
    overlay_path = root / "assets" / "card_catalog_overlay.json"
    overlay_cards = build_overlay_cards(base_catalog_path, latest_base_path)
    existing_overlay_hashes = set(_load_from_json(overlay_path)) if overlay_path.exists() else set()
    added_overlay_card_count = sum(
        1
        for card in overlay_cards
        if str(card.get("hash_id") or "") not in existing_overlay_hashes
    )

    if not dry_run:
        update_share_config(root / "shared" / "share_config.json", version, start_date, date_to)
        update_version_start_dates(root / "src" / "eiketsu_env" / "config.py", version, start_date)
        write_overlay(overlay_path, latest_base_path, version, overlay_cards)

    return PreparedVersionUpdate(
        version=version,
        start_date=start_date,
        date_to=date_to,
        latest_base_path=latest_base_path,
        overlay_card_count=len(overlay_cards),
        added_overlay_card_count=added_overlay_card_count,
    )


def latest_official_base_snapshot(official_root: Path) -> Path:
    base_dir = official_root / "data" / "raw" / "official" / "base"
    try:
        paths = [path for path in base_dir.glob("*.json") if path.is_file()]
    except OSError as exc:
        raise FileNotFoundError(f"无法读取官方 base 目录：{base_dir}") from exc
    if not paths:
        raise FileNotFoundError(f"找不到官方 base 快照：{base_dir}")
    return max(paths, key=lambda path: path.stat().st_mtime)


def update_share_config(path: Path, version: str, start_date: str, date_to: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["target_version"] = version
    payload["date_from"] = start_date
    payload["date_to"] = date_to
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_version_start_dates(path: Path, version: str, start_date: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"VERSION_START_DATES = \{\n(?P<body>.*?)\n\}", re.DOTALL)
    match = pattern.search(text)
    if not match:
        raise ValueError(f"找不到 VERSION_START_DATES：{path}")

    versions: dict[str, str] = {}
    for version_text, date_text in re.findall(r'^\s*"([^"]+)":\s*"([^"]+)",\s*$', match.group("body"), re.MULTILINE):
        versions[version_text] = date_text
    versions[version] = start_date

    body = "\n".join(
        f'    "{item_version}": "{item_date}",'
        for item_version, item_date in sorted(
            versions.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
    )
    updated = pattern.sub(f"VERSION_START_DATES = {{\n{body}\n}}", text, count=1)
    path.write_text(updated, encoding="utf-8")


def build_overlay_cards(base_catalog_path: Path, latest_base_path: Path) -> list[dict[str, Any]]:
    existing_hashes = set(_load_from_json(base_catalog_path))
    payload = json.loads(latest_base_path.read_text(encoding="utf-8"))
    cards: list[dict[str, Any]] = []
    for raw_row_text in payload.get("general", []):
        if not isinstance(raw_row_text, str):
            continue
        raw = raw_row_text.split(",")
        if len(raw) < 16:
            continue
        primary_hash = raw[0]
        if primary_hash in existing_hashes:
            continue
        card_number = _parse_int(raw[12])
        card = {
            "hash_id": primary_hash,
            "card_code": _build_official_card_code(payload, raw[10], card_number),
            "name": raw[3],
            "faction": _lookup_official_name(payload, "color", raw[5]),
            "card_type": _lookup_official_name(payload, "cardType", raw[11]),
            "cost": _lookup_official_name(payload, "cost", raw[13]),
            "unitType": _lookup_official_name(payload, "unitType", raw[15]),
            "image_keys": {
                "card_small": raw[0],
                "card_ds": raw[1],
                "card_face": raw[2],
            },
        }
        if _catalog_card_hashes(card):
            cards.append(card)
    return cards


def write_overlay(path: Path, latest_base_path: Path, version: str, cards: list[dict[str, Any]]) -> None:
    payload = {
        "source": latest_base_path.name,
        "generated_for": version,
        "cards": cards,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def vps_refresh_commands(result: PreparedVersionUpdate) -> list[str]:
    return [
        (
            "docker compose -f deploy/docker-compose.yml run --rm api "
            f"eiketsu-server admin set-config --target-version {result.version} "
            f"--date-from {result.start_date} --date-to {result.date_to}"
        ),
        "docker compose -f deploy/docker-compose.yml run --rm api eiketsu-server admin refresh-leaderboard",
    ]
