"""提供 URL、时间、文本清洗和 ID 生成等跨模块通用工具。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

BASE_URL = "https://eiketsu-taisen.net"
PARSER_VERSION = "env_v1"
JST = timezone(timedelta(hours=9))


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: Any) -> str:
    """清理页面文本里的私有区图标、重复空白和尾部装饰。"""

    text = re.sub(r"[\uE000-\uF8FF]", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*---$", "", text).strip()


def normalize_url(href: str, base_url: str = BASE_URL) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base_url, href)


def infer_result(value: str) -> str:
    source = str(value or "")
    lowered = source.lower()
    if "win" in lowered or "勝利" in source:
        return "win"
    if "lose" in lowered or "loss" in lowered or "敗北" in source:
        return "loss"
    if "draw" in lowered or "引き分け" in source or "引分" in source:
        return "draw"
    return "unknown"


def infer_played_at(value: str, iso_date: str) -> str:
    match = re.search(r"(\d{2}:\d{2})", str(value or ""))
    return f"{iso_date} {match.group(1)}" if match else ""


def deck_fingerprint(card_hashes: list[str]) -> str:
    """卡组指纹只反映集合，不反映展示顺序，方便同卡组聚合。"""

    return ",".join(sorted(item for item in card_hashes if item))


def extract_replay_id(url: str) -> str:
    query = parse_qs(urlparse(str(url or "")).query)
    replay_id = (query.get("p") or [""])[0]
    if replay_id:
        return replay_id
    match = re.search(r"/live/([A-Za-z0-9]+)/master\.m3u8", str(url or ""))
    return match.group(1) if match else ""


def extract_detail_t(url: str) -> str:
    query = parse_qs(urlparse(str(url or "")).query)
    return (query.get("t") or [""])[0]


def played_at_from_detail_t(detail_t: str) -> str:
    try:
        timestamp = int(str(detail_t or ""))
    except ValueError:
        return ""
    # 英杰大战.NET 的 history/detail?t=... 是日本本地站点时间戳，用 JST 还原日期最稳。
    return datetime.fromtimestamp(timestamp, JST).strftime("%Y-%m-%d %H:%M")


def extract_follow_id(url: str) -> str:
    query = parse_qs(urlparse(str(url or "")).query)
    return (query.get("f") or [""])[0]


def m3u8_url_for_replay(replay_id: str) -> str:
    return f"https://dl.eiketsu-taisen.net/live/{replay_id}/master.m3u8" if replay_id else ""


def public_id_for_match(replay_id: str, follow_id: str, detail_t: str) -> str:
    if replay_id:
        return f"r:{replay_id}"
    if follow_id and detail_t:
        return f"d:{follow_id}:{detail_t}"
    raw = "|".join([follow_id, detail_t])
    return "d:unknown:" + sha256_text(raw)[:16]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
