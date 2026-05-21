"""解析关注列表、每日对局、详情页和 replay 页面的原始内容。"""

from __future__ import annotations

import ast
import csv
import json
import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from eiketsu_env.utils import (
    clean_text,
    extract_follow_id,
    extract_replay_id,
    infer_played_at,
    infer_result,
    m3u8_url_for_replay,
    normalize_url,
)


def _text(node: Tag | None) -> str:
    if not node:
        return ""
    text = node.get_text(" ", strip=True)
    if not text and node.string:
        text = str(node.string)
    if not text:
        text = " ".join(str(child) for child in node.descendants if isinstance(child, NavigableString))
    return clean_text(text)


def _classes(node: Tag | None) -> str:
    if not node:
        return ""
    return " ".join(str(item) for item in node.get("class", []))


def parse_follow_html(html: str, source_url: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    players: list[dict[str, str]] = []
    seen: set[str] = set()
    for container in soup.select("li.c-name-plate__wrap, .c-name-plate__wrap"):
        link = container.select_one("a[href*='history/daily?f=']")
        if not link:
            continue
        daily_url = normalize_url(str(link.get("href") or ""), source_url or base_url)
        follow_id = extract_follow_id(daily_url)
        if not follow_id or follow_id in seen:
            continue
        seen.add(follow_id)
        players.append(
            {
                "follow_id": follow_id,
                "name": _text(container.select_one(".c-name-plate__name-text")) or f"f-{follow_id}",
                "state": _text(container.select_one(".c-name-plate__state-text")),
                "daily_url": daily_url,
            }
        )

    # 有些 HTML 快照只保留了链接，没有完整 name plate，兜底从链接直接建玩家。
    for link in soup.select("a[href*='history/daily?f=']"):
        daily_url = normalize_url(str(link.get("href") or ""), source_url or base_url)
        follow_id = extract_follow_id(daily_url)
        if follow_id and follow_id not in seen:
            seen.add(follow_id)
            players.append({"follow_id": follow_id, "name": f"f-{follow_id}", "state": "", "daily_url": daily_url})
    return players


FOLLOW_API_FIELDS = [
    "name",
    "league",
    "leagueimg",
    "title_0",
    "titleimg_0",
    "title_1",
    "titleimg_1",
    "title_2",
    "titleimg_2",
    "avatar_back_0",
    "avatar_back_img_0",
    "avatar_0",
    "avatarimg_0",
    "avatar_frame_0",
    "avatar_frame_img_0",
    "avatar_back_1",
    "avatar_back_img_1",
    "avatar_1",
    "avatarimg_1",
    "avatar_frame_1",
    "avatar_frame_img_1",
    "avatar_back_2",
    "avatar_back_img_2",
    "avatar_2",
    "avatarimg_2",
    "avatar_frame_2",
    "avatar_frame_img_2",
    "condition",
    "idx",
    "code",
    "followtime",
    "lastplaytime",
    "ranking",
    "now",
    "today",
    "store",
    "follower",
]


def parse_follow_api_json(payload: str, base_url: str) -> list[dict[str, str]]:
    """解析 follow.js 使用的 CSV 压缩接口。

    `follow` 数组里同时混有“已关注”和“申请中”；condition=2 才是真正的关注列表。
    """

    doc = json.loads(payload)
    rows = doc.get("follow") or []
    players: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_row in rows:
        parsed = next(csv.reader([str(raw_row)]))
        item = {name: parsed[index] if index < len(parsed) else "" for index, name in enumerate(FOLLOW_API_FIELDS)}
        if item.get("condition") != "2":
            continue
        follow_id = item.get("idx", "").strip()
        if not follow_id or follow_id in seen:
            continue
        seen.add(follow_id)
        players.append(
            {
                "follow_id": follow_id,
                "name": clean_text(item.get("name") or f"f-{follow_id}"),
                "state": clean_text(item.get("league") or ""),
                "daily_url": normalize_url(f"/members/history/daily?f={follow_id}", base_url),
                "today": item.get("today", "0"),
                "now": item.get("now", "0"),
                "lastplaytime": item.get("lastplaytime", ""),
            }
        )
    return players


def parse_daily_html(html: str, source_url: str, base_url: str, iso_date: str, player: dict[str, str]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("a.p-daily-log__detail-nav[href], a[href*='/members/history/detail']")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    resolved_name = clean_text(player.get("name") or "")
    page_name = _text(soup.select_one(".c-name-plate__name-text"))
    if page_name:
        resolved_name = page_name
    page_date = _parse_daily_page_date(soup) or iso_date

    # daily?f=... 会跳到“最近一个游玩日”，不一定是请求日期；日期不一致时跳过，避免把 5/10 误标成 5/11。
    if page_date != iso_date:
        return []

    for anchor in anchors:
        detail_url = normalize_url(str(anchor.get("href") or ""), source_url or base_url)
        if "/members/history/detail" not in detail_url or detail_url in seen:
            continue
        seen.add(detail_url)
        labels = [_text(node) for node in anchor.select(".c-mode-time__label") if _text(node)]
        names = [_text(node) for node in anchor.select(".c-match-up__name-box") if _text(node)]
        opponent = next((name for name in names if name and name != resolved_name), names[-1] if names else "")
        mode_block = anchor.select_one(".c-mode-time")
        rows.append(
            {
                "detail_url": detail_url,
                "raw_text": _text(anchor),
                "mode": labels[0] if labels else "",
                "played_at": infer_played_at(labels[1] if len(labels) > 1 else _text(anchor), page_date),
                "result": infer_result(_classes(mode_block) + " " + _classes(anchor)),
                "player_name": resolved_name,
                "opponent_name": opponent,
                "castle_rates": [_text(node) for node in anchor.select(".c-v2-castle-data__wrap") if _text(node)],
                "follow_id": player.get("follow_id", ""),
                "page_date": page_date,
            }
        )
    return rows


def _parse_daily_page_date(soup: BeautifulSoup) -> str:
    raw_date = _text(soup.select_one(".c-btl-record__rec-ttl--total"))
    match = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", raw_date)
    if not match:
        return ""
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()


def _parse_deck_ids_from_onclick(value: str) -> list[str]:
    match = re.search(r"overwriteCheck\('([^']+)'\)", value or "")
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _parse_score_box(scope: Tag | None) -> dict[str, str]:
    if not scope:
        return {}
    result: dict[str, str] = {}
    selectors = ".p-detail__score-box, .p-detail__score-box--dbl, .p-detail__score-box--3prts, .p-detail-skill"
    for box in scope.select(selectors):
        label = _text(box.select_one("dt"))
        value = _text(box.select_one("dd")) or _text(box)
        if label and value.startswith(label):
            value = value[len(label) :].strip()
        if label:
            result[label] = value
    return result


def _parse_detail_rank_labels(soup: BeautifulSoup) -> list[str]:
    labels: list[str] = []
    for selector in ("#detail_head .player", "#detail_head .enemy"):
        scope = soup.select_one(selector)
        labels.append(_rank_label_in_scope(scope))
    if any(labels):
        return labels

    # 旧快照偶尔没有 detail_head 结构，退回读取页面中前两个段位图标。
    return [
        clean_text(str(image.get("alt") or ""))
        for image in soup.select("img.c-name-plate__rank[alt], img[src*='league_v3'][alt]")[:2]
    ]


def _rank_label_in_scope(scope: Tag | None) -> str:
    if scope is None:
        return ""
    image = scope.select_one("img.c-name-plate__rank[alt], img[src*='league_v3'][alt]")
    return clean_text(str(image.get("alt") or "")) if image else ""


def _parse_selected(side: Tag | None) -> dict[str, Any]:
    if not side:
        return {}
    containers = side.select(".c-selected__container")
    weapon = containers[0] if containers else None
    school = containers[1] if len(containers) > 1 else None
    souls = _parse_souls(weapon)
    return {
        "weapon": {
            "name": _text(weapon.select_one(".etfont")) if weapon else "",
            "summary": _text(weapon),
            "souls": souls,
        },
        "school": {
            "name": _text(school.select_one(".etfont")) if school else "",
            "summary": _text(school),
        },
        "souls": souls,
    }


def _parse_souls(container: Tag | None) -> list[dict[str, str]]:
    if not container:
        return []
    nodes = container.select(".c-selected__explain--buff .c-sec__text")
    if not nodes:
        nodes = container.select(".c-selected__explain--buff .c-sec")
    return [{"name": name} for node in nodes if (name := _text(node))]


def _parse_generals(section: Tag | None) -> list[dict[str, Any]]:
    if not section:
        return []
    generals: list[dict[str, Any]] = []
    for index, cell in enumerate(section.select("tr.p-gen-tbl__row-gen td.p-gen-tbl__cel-gen, img.card.c-card-deco__main, .c-card-deco[data-id]"), start=1):
        if cell.name == "img":
            card_hash = str(cell.get("data-id") or "")
            name = ""
        elif cell.name == "div":
            card_hash = str(cell.get("data-id") or "")
            name = _text(cell)
        else:
            image = cell.select_one("img.card.c-card-deco__main, .c-card-deco[data-id]")
            card_hash = str(image.get("data-id") or "") if image else ""
            name = _text(cell.select_one(".c-unit-info__name"))
        if card_hash:
            generals.append({"slot": len(generals) + 1, "hash_id": card_hash, "raw_name": name})
    return generals


GENERAL_SCORE_KEYS = {
    "\u8a08\u7565": "strategy_count",
    "\u6483\u7834": "kill_count",
    "\u64a4\u9000": "death_count",
    "\u7a81\u6483": "charge_count",
    "\u69cd\u6483": "spear_attack_count",
    "\u5c04\u6483": "shoot_count",
    "\u65ac\u6483": "slash_count",
    "\u8fce\u6483": "intercept_count",
    "\u88ab\u7a81\u6483": "taken_charge_count",
    "\u88ab\u69cd\u6483": "taken_spear_attack_count",
    "\u88ab\u5c04\u6483": "taken_shoot_count",
    "\u88ab\u65ac\u6483": "taken_slash_count",
    "\u57ce\u9580": "gate_attack_count",
    "\u57ce\u58c1": "wall_attack_count",
    "\u30c0\u30e1\u30fc\u30b8": "siege_damage",
}


def _parse_general_score_number(value: str) -> float | None:
    normalized = re.sub(r"\s+", "", value or "").replace(",", "").replace("\uff05", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _parse_general_score_totals(section: Tag | None) -> dict[str, dict[str, Any]]:
    if not section:
        return {}
    totals: dict[str, dict[str, Any]] = {}
    for row in section.select("tr"):
        label = _text(row.select_one("th"))
        key = GENERAL_SCORE_KEYS.get(label)
        if not key:
            continue
        values = [
            value
            for value in (_parse_general_score_number(_text(cell)) for cell in row.select("td.p-gen-tbl__cel--score"))
            if value is not None
        ]
        if values:
            totals[key] = {"label": label, "total": sum(values), "by_slot": values}
    return totals


def _parse_deck_section(section: Tag | None) -> dict[str, Any]:
    if not section:
        return {"deck_ids": [], "deck_totals": {}, "battle_stats": {}, "generals": []}
    onclick_link = section.select_one("a[onclick*='overwriteCheck']")
    deck_ids = _parse_deck_ids_from_onclick(str(onclick_link.get("onclick") or "")) if onclick_link else []
    generals = _parse_generals(section)
    if not deck_ids:
        deck_ids = [item["hash_id"] for item in generals if item.get("hash_id")]
    return {
        "deck_ids": deck_ids,
        "deck_totals": _parse_score_box(section),
        "battle_stats": _parse_general_score_totals(section),
        "generals": generals,
    }


def _parse_castle_breakdown(soup: BeautifulSoup) -> dict[str, Any]:
    castle = soup.select_one(".castle")
    if not castle:
        return {"title": "", "rows": []}
    rows = []
    for row in castle.select("ul.castle_damage"):
        rows.append(
            {
                "player": _text(row.select_one("li.player")),
                "label": _text(row.select_one("li.mincho")),
                "enemy": _text(row.select_one("li.enemy")),
            }
        )
    return {"title": _text(castle.select_one(".trapezoid .inner")), "rows": [item for item in rows if any(item.values())]}


def _extract_js_array(html: str, name: str) -> list[Any]:
    pattern = rf"(?:var|let|const)?\s*{re.escape(name)}\s*=\s*(\[[^\]]*\])"
    match = re.search(pattern, html)
    if not match:
        return []
    payload = match.group(1)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(payload)
            return value if isinstance(value, list) else []
        except (SyntaxError, ValueError):
            return []


def parse_detail_html(html: str, source_url: str, base_url: str, seed: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    play_anchor = soup.select_one("a[href*='/members/enbujyo/play?p=']")
    play_url = normalize_url(str(play_anchor.get("href") or ""), source_url or base_url) if play_anchor else ""
    replay_id = extract_replay_id(play_url)
    info_candidates = [_text(node) for node in soup.select(".trapezoid .inner")]
    info_text = next((item for item in info_candidates if "Ver." in item), next((item for item in info_candidates if item), ""))
    deck_sections = soup.select(".p-general-score, section.general_score")
    side_sections = soup.select(".p-detail--player, .p-detail--enemy")
    names = [
        _text(soup.select_one("#detail_head .player .name .etfont")),
        _text(soup.select_one("#detail_head .enemy .name .etfont")),
    ]
    if not any(names):
        names = [seed.get("player_name", ""), seed.get("opponent_name", "")]
    rank_labels = _parse_detail_rank_labels(soup)

    sides = []
    for index in range(2):
        deck = _parse_deck_section(deck_sections[index] if index < len(deck_sections) else None)
        side = side_sections[index] if index < len(side_sections) else None
        profile = _parse_score_box(side)
        if index < len(rank_labels) and rank_labels[index]:
            profile.setdefault("段位", rank_labels[index])
        sides.append(
            {
                "side_index": index + 1,
                "role": "player" if index == 0 else "enemy",
                "player_name": clean_text(names[index] if index < len(names) else ""),
                "follow_id": seed.get("follow_id", "") if index == 0 else "",
                "result": seed.get("result", "unknown") if index == 0 else "unknown",
                "castle_rate": (seed.get("castle_rates") or ["", ""])[index] if len(seed.get("castle_rates") or []) > index else "",
                "profile": profile,
                "selected": _parse_selected(side),
                "deck_ids": deck["deck_ids"],
                "deck_totals": deck["deck_totals"],
                "battle_stats": deck["battle_stats"],
                "generals": deck["generals"],
            }
        )

    mode = info_text.split(" ")[0] if info_text else seed.get("mode", "")
    version = next((token for token in info_text.split() if token.startswith("Ver.")), "")
    detail_result = infer_result(_classes(soup.select_one("#detail_head"))) if soup.select_one("#detail_head") else "unknown"
    return {
        **seed,
        "detail_url": source_url,
        "url": source_url,
        "play_url": play_url,
        "replay_id": replay_id,
        "m3u8_url": m3u8_url_for_replay(replay_id),
        "title": seed.get("raw_text", ""),
        "date": seed.get("played_at", ""),
        "mode": mode,
        "version": version,
        "result": detail_result if detail_result != "unknown" else seed.get("result", "unknown"),
        "detail_error": "",
        "castle_breakdown": _parse_castle_breakdown(soup),
        "timeline_labels": [_text(node) for node in soup.select(".timelines .trapezoid .inner, .timelines .timeline-title, .timelines .heading_3") if _text(node)],
        "timeline_data": {
            "player": {
                "school_counts": _extract_js_array(html, "p_schoolCnt"),
                "wave_calls": _extract_js_array(html, "p_waveCnt"),
                "orange_changes": _extract_js_array(html, "p_orangeCnt"),
            },
            "enemy": {
                "school_counts": _extract_js_array(html, "e_schoolCnt"),
                "wave_calls": _extract_js_array(html, "e_waveCnt"),
                "orange_changes": _extract_js_array(html, "e_orangeCnt"),
            },
            "castle": {
                "player": _extract_js_array(html, "p_castleTL"),
                "enemy": _extract_js_array(html, "e_castleTL"),
            },
        },
        "players": sides,
    }


def parse_replay_html(html: str, source_url: str, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    replay_id = extract_replay_id(source_url)
    m3u8_match = re.search(r"https://dl\.eiketsu-taisen\.net/live/([A-Za-z0-9]+)/master\.m3u8", html)
    if m3u8_match:
        replay_id = replay_id or m3u8_match.group(1)
    button = soup.select_one("[data-p]")
    replay_id = replay_id or (str(button.get("data-p") or "") if button else "")
    deck_sections = soup.select("section.general_score, .p-general-score")
    name_plates = soup.select("li.c-name-plate__wrap, .c-name-plate__wrap")
    player_names: list[str] = []
    follow_ids: list[str] = []
    for plate in name_plates[:2]:
        player_names.append(_text(plate.select_one(".c-name-plate__name-text")))
        history_link = plate.select_one("a[href*='history/daily?f=']")
        follow_ids.append(extract_follow_id(str(history_link.get("href") or "")) if history_link else "")
    return {
        "replay_id": replay_id,
        "source_url": source_url,
        "replay_url": normalize_url(f"/members/enbujyo/play?p={replay_id}", base_url) if replay_id else "",
        "m3u8_url": m3u8_url_for_replay(replay_id),
        "player1_deck_ids": _parse_deck_section(deck_sections[0] if deck_sections else None)["deck_ids"],
        "player2_deck_ids": _parse_deck_section(deck_sections[1] if len(deck_sections) > 1 else None)["deck_ids"],
        "player_names": player_names,
        "follow_ids": follow_ids,
    }
