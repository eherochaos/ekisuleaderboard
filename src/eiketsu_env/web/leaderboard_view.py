"""排行榜页面 HTML 渲染与静态资源定位。"""

from __future__ import annotations

import html
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlencode

from eiketsu_env import __version__
from eiketsu_env.services.leaderboard import (
    RANK_SCOPE_ALL,
    RANK_SCOPE_KNIGHT_DOWN,
    RANK_SCOPE_KNIGHT_UP,
    RANK_SCOPE_LABELS,
    RANK_SCOPE_TRAVELER_DOWN,
)


def _leaderboard_root() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    frontend_root = project_root / "frontend" / "leaderboard"
    if frontend_root.is_dir():
        return frontend_root
    return Path(__file__).resolve().parent


WEB_ROOT = _leaderboard_root()
WEB_TEMPLATE_ROOT = WEB_ROOT / "templates"
WEB_STATIC_ROOT = WEB_ROOT / "static"
LEADERBOARD_STATIC_FILES = {"leaderboard.css", "leaderboard.js"}

LEADERBOARD_HTML_DEFAULT_LIMIT = 80
LEADERBOARD_HTML_MAX_LIMIT = 500


def _leaderboard_display_limit(limit: int | None, full: str = "") -> int | None:
    if str(full or "").strip().lower() in {"1", "true", "yes", "all"}:
        return None
    if limit is None:
        return LEADERBOARD_HTML_DEFAULT_LIMIT
    return max(1, min(int(limit), LEADERBOARD_HTML_MAX_LIMIT))


def _leaderboard_visual_page(
    payload: dict[str, Any],
    *,
    personal_requested: bool = False,
    filter_error: str = "",
    contributor_name: str = "",
    cluster_enabled: bool = True,
    display_limit: int | None = None,
) -> str:
    archetypes = list(payload.get("top_archetypes") or [])
    decks = list(payload.get("top_decks") or [])
    is_archetype_view = bool(cluster_enabled and archetypes)
    is_personal_view = payload.get("scope") in {"mine", "contributor"}
    sort_target = "archetype-ranking" if is_archetype_view else "deck-ranking"
    all_items = archetypes if is_archetype_view else decks
    visible_items = _leaderboard_visible_items(all_items, display_limit)
    board = _archetype_ranking_board(visible_items) if is_archetype_view else _ranking_board(visible_items)
    eyebrow = "MY CONTRIBUTION LEADERBOARD" if is_personal_view else "PUBLIC ANONYMOUS LEADERBOARD"
    title = "我的贡献卡组榜" if is_personal_view else "英杰大战环境卡组榜"
    upload_label = "我的上传批次" if is_personal_view else "上传批次"
    privacy_note = (
        "当前只统计这个用户名上传包关联的对局；公开榜仍不会列出贡献者名单。"
        if is_personal_view
        else "公开页只展示匿名聚合结果，不展示贡献者昵称、token、浏览器信息或本地路径。"
    )
    mobile_summary = _leaderboard_mobile_summary(payload, upload_label)
    view_controls = _leaderboard_view_controls(payload, cluster_enabled, contributor_name)
    display_notice = _leaderboard_display_notice(
        payload,
        cluster_enabled=cluster_enabled,
        contributor_name=contributor_name,
        display_limit=display_limit,
        shown_count=len(visible_items),
        total_count=len(all_items),
    )
    summary_items = "".join(
        [
            _summary_item("目标版本", payload.get("target_version", "")),
            _summary_item("采集日期", f"{payload.get('date_from', '')} 至 {payload.get('date_to', '')}"),
            _summary_item(upload_label, payload.get("upload_count", 0)),
            _summary_item("对局数", payload.get("match_count", 0)),
            _summary_item("双方样本", payload.get("side_sample_count", 0)),
            _summary_item("生成时间", payload.get("generated_at", "")),
        ]
    )
    return _render_web_template(
        "leaderboard.html",
        {
            "page_title": _html(title),
            "asset_version": _html(__version__),
            "scope_tools": _leaderboard_scope_tools(personal_requested, is_personal_view, contributor_name or str(payload.get("contributor_name") or "")),
            "filter_error": _leaderboard_filter_error(filter_error),
            "eyebrow": _html(eyebrow),
            "title": _html(title),
            "summary_items": summary_items,
            "mobile_summary": mobile_summary,
            "privacy_note": _html(privacy_note),
            "view_controls": view_controls,
            "display_notice": display_notice,
            "feature_grid": "",
            "sort_target": _html(sort_target),
            "board": board,
            "load_more": _leaderboard_load_more_control(
                payload,
                cluster_enabled=cluster_enabled,
                contributor_name=contributor_name,
                target_id=sort_target,
                visible_count=len(visible_items),
                total_count=len(all_items),
                page_size=display_limit,
            ),
        },
    )


def _render_web_template(name: str, context: dict[str, Any]) -> str:
    template_path = WEB_TEMPLATE_ROOT / name
    if not template_path.is_file():
        raise RuntimeError(f"web template not found: {name}")
    return Template(template_path.read_text(encoding="utf-8")).safe_substitute(context)


def _leaderboard_visible_items(items: list[dict[str, Any]], display_limit: int | None) -> list[dict[str, Any]]:
    if display_limit is None:
        return items
    return items[:display_limit]


def _leaderboard_rows_response(
    payload: dict[str, Any],
    *,
    cluster_enabled: bool,
    contributor_name: str,
    offset: int,
    limit: int,
    sort_key: str,
) -> dict[str, Any]:
    is_archetype_view = bool(cluster_enabled and payload.get("top_archetypes"))
    items = list(payload.get("top_archetypes") or []) if is_archetype_view else list(payload.get("top_decks") or [])
    sorted_items = _sort_leaderboard_items(items, sort_key)
    safe_offset = max(0, int(offset or 0))
    page_size = max(1, min(int(limit or LEADERBOARD_HTML_DEFAULT_LIMIT), LEADERBOARD_HTML_MAX_LIMIT))
    page_items = sorted_items[safe_offset : safe_offset + page_size]
    next_offset = safe_offset + len(page_items)
    html_rows = (
        _archetype_rows(page_items, start=safe_offset + 1)
        if is_archetype_view
        else _deck_rows(page_items, start=safe_offset + 1)
    )
    return {
        "html": html_rows,
        "offset": safe_offset,
        "next_offset": next_offset,
        "limit": page_size,
        "total": len(sorted_items),
        "has_more": next_offset < len(sorted_items),
        "scope": payload.get("scope", "public"),
        "contributor_name": contributor_name,
    }


def _sort_leaderboard_items(items: list[dict[str, Any]], sort_key: str) -> list[dict[str, Any]]:
    primary = "sample_count" if str(sort_key or "").lower() == "sample" else "wilson_lower_bound"
    secondary = "wilson_lower_bound" if primary == "sample_count" else "sample_count"

    def _metric(item: dict[str, Any], key: str) -> float:
        try:
            return float(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: (-_metric(pair[1], primary), -_metric(pair[1], secondary), pair[0]),
        )
    ]


def _leaderboard_load_more_control(
    payload: dict[str, Any],
    *,
    cluster_enabled: bool,
    contributor_name: str,
    target_id: str,
    visible_count: int,
    total_count: int,
    page_size: int | None,
) -> str:
    if page_size is None or visible_count >= total_count:
        return ""
    rank_scope = str(payload.get("rank_scope") or RANK_SCOPE_ALL)
    if rank_scope not in RANK_SCOPE_LABELS:
        rank_scope = RANK_SCOPE_ALL
    endpoint = "/api/v1/leaderboard/rows"
    endpoint += "?" + urlencode(
        {
            **_leaderboard_base_query(payload, contributor_name),
            "cluster": "on" if cluster_enabled else "off",
            "rank_scope": rank_scope,
        }
    )
    full_url = _leaderboard_query_url(
        _leaderboard_base_query(payload, contributor_name),
        cluster="on" if cluster_enabled else "off",
        rank_scope=rank_scope,
        full="1",
    )
    return "\n".join(
        [
            f'<div class="leaderboard-loadmore" data-load-more data-target="{_html(target_id)}" data-endpoint="{_html(endpoint)}" data-next-offset="{visible_count}" data-page-size="{page_size}">',
            f'<button type="button" data-load-more-button>加载更多</button>',
            f'<span data-load-more-status>已显示 {visible_count} / {total_count}</span>',
            f'<noscript><a href="{_html(full_url)}">查看完整榜</a></noscript>',
            "</div>",
        ]
    )


def _leaderboard_scope_tools(
    personal_requested: bool,
    is_personal_view: bool,
    contributor_name: str = "",
) -> str:
    if is_personal_view:
        label = f"用户：{_html(contributor_name)}" if contributor_name else "我的贡献视角"
        return f"""
        <div class="scope-tools">
          <span class="scope-chip">{label}</span>
          <form method="post" action="/leaderboard/filter/clear">
            <button type="submit">公开聚合榜</button>
          </form>
        </div>
        """
    if personal_requested:
        return """
        <div class="scope-tools">
        <form method="post" action="/leaderboard/filter/clear">
          <button type="submit">公开聚合榜</button>
        </form>
        </div>
        """
    return """
        <div class="scope-tools">
          <form class="scope-form-desktop" method="post" action="/leaderboard/filter">
            <input name="contributor" type="text" autocomplete="off" placeholder="绑定用户名">
            <button class="primary" type="submit">我的贡献</button>
          </form>
          <details class="scope-mobile-drawer">
            <summary>我的贡献</summary>
            <form method="post" action="/leaderboard/filter">
              <input name="contributor" type="text" autocomplete="off" placeholder="绑定用户名">
              <button class="primary" type="submit">查看</button>
            </form>
          </details>
        </div>
    """


def _leaderboard_filter_error(filter_error: str) -> str:
    return f'<p class="scope-error">{_html(filter_error)}</p>' if filter_error else ""


def _leaderboard_mobile_summary(payload: dict[str, Any], upload_label: str) -> str:
    date_from = str(payload.get("date_from") or "")
    date_to = str(payload.get("date_to") or "")
    parts = [
        str(payload.get("target_version") or ""),
        f"{date_from} 至 {date_to}" if date_from or date_to else "",
        f"{payload.get('match_count', 0)} 对局",
        f"{payload.get('side_sample_count', 0)} 样本",
        f"{upload_label} {payload.get('upload_count', 0)}",
    ]
    return " · ".join(_html(part) for part in parts if part)


def _leaderboard_display_notice(
    payload: dict[str, Any],
    *,
    cluster_enabled: bool,
    contributor_name: str,
    display_limit: int | None,
    shown_count: int,
    total_count: int,
) -> str:
    return ""


def _leaderboard_view_controls(payload: dict[str, Any], cluster_enabled: bool, contributor_name: str = "") -> str:
    rank_scope = str(payload.get("rank_scope") or RANK_SCOPE_ALL)
    if rank_scope not in RANK_SCOPE_LABELS:
        rank_scope = RANK_SCOPE_ALL
    base_params = _leaderboard_base_query(payload, contributor_name)
    cluster_links = [
        _view_control_link("开", _leaderboard_query_url(base_params, cluster="on", rank_scope=rank_scope), cluster_enabled),
        _view_control_link("关", _leaderboard_query_url(base_params, cluster="off", rank_scope=rank_scope), not cluster_enabled),
    ]
    rank_links = [
        _view_control_link(label, _leaderboard_query_url(base_params, cluster="on" if cluster_enabled else "off", rank_scope=value), rank_scope == value)
        for value, label in (
            (RANK_SCOPE_ALL, RANK_SCOPE_LABELS[RANK_SCOPE_ALL]),
            (RANK_SCOPE_TRAVELER_DOWN, RANK_SCOPE_LABELS[RANK_SCOPE_TRAVELER_DOWN]),
            (RANK_SCOPE_KNIGHT_DOWN, RANK_SCOPE_LABELS[RANK_SCOPE_KNIGHT_DOWN]),
            (RANK_SCOPE_KNIGHT_UP, RANK_SCOPE_LABELS[RANK_SCOPE_KNIGHT_UP]),
        )
    ]
    return "\n".join(
        [
            '<section class="view-controls" aria-label="榜单视图设置">',
            '<div class="view-control-group"><span class="view-control-label">聚类</span>',
            *cluster_links,
            "</div>",
            '<div class="view-control-group"><span class="view-control-label">段位</span>',
            *rank_links,
            "</div>",
            "</section>",
        ]
    )


def _leaderboard_base_query(payload: dict[str, Any], contributor_name: str = "") -> dict[str, str]:
    scope = str(payload.get("scope") or "public")
    if scope == "contributor":
        contributor = str(contributor_name or payload.get("contributor_name") or "").strip()
        return {"scope": "contributor", "contributor": contributor} if contributor else {"scope": "contributor"}
    if scope == "mine":
        return {"scope": "mine"}
    return {}


def _leaderboard_query_url(base_params: dict[str, str], **updates: str) -> str:
    params = {key: value for key, value in {**base_params, **updates}.items() if value}
    return "/leaderboard" + (f"?{urlencode(params)}" if params else "")


def _view_control_link(label: str, href: str, active: bool) -> str:
    css_class = "view-control-link is-active" if active else "view-control-link"
    return f'<a class="{css_class}" href="{_html(href)}">{_html(label)}</a>'


def _archetype_feature_grid(archetypes: list[dict[str, Any]]) -> str:
    if not archetypes:
        return '<section class="empty">当前筛选范围内还没有可展示的卡组分类。</section>'
    cards = []
    for index, archetype in enumerate(archetypes, start=1):
        css_class = "feature-card-1" if index == 1 else f"feature-card-{index}"
        cards.append(
            "\n".join(
                [
                    f'<article class="feature-card {css_class}">',
                    '<div class="feature-meta">',
                    f'<span class="rank-badge">{index:02d}</span>',
                    f'<span>{_record_label(archetype)}</span>',
                    "</div>",
                    f'<h2>{_html(archetype.get("title") or "-")}</h2>',
                    _player_summary(archetype),
                    f'<p class="archetype-subnote">共同 Cost >= {_html(archetype.get("similar_cost_threshold", ""))}，{_html(archetype.get("member_count", 0))} 个构筑合并统计</p>',
                    f'<div class="feature-cards">{_card_strip(archetype.get("core_cards") or [])}</div>',
                    '<div class="feature-stats signal-panel">',
                    _signal_groups(archetype),
                    "</div>",
                    _variant_stack(archetype.get("member_decks") or [], limit=2),
                    "</article>",
                ]
            )
        )
    return f'<section class="feature-grid">{"".join(cards)}</section>'


def _archetype_ranking_board(archetypes: list[dict[str, Any]]) -> str:
    if not archetypes:
        return ""
    return "\n".join(
        [
            '<section class="archetype-board" id="archetype-ranking" data-sort-root>',
            '<div class="board-head"><span>Rank</span><span>Archetype</span><span>Signal</span></div>',
            _archetype_rows(archetypes),
            "</section>",
        ]
    )


def _archetype_rows(archetypes: list[dict[str, Any]], start: int = 1) -> str:
    return "".join(_archetype_rank_row(index, archetype) for index, archetype in enumerate(archetypes, start=start))


def _archetype_rank_row(index: int, archetype: dict[str, Any]) -> str:
    title = str(archetype.get("title") or archetype.get("archetype_id") or "")
    return "\n".join(
        [
            f'<article class="archetype-row" {_sort_item_attrs(title, archetype.get("wilson_lower_bound"), archetype.get("sample_count"))}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span>{_record_label(archetype)}</span>',
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(title)}</h3>",
            _player_summary(archetype),
            _archetype_variant_viewer(archetype),
            "</div>",
            '<div class="row-signals signal-panel">',
            _signal_groups(archetype),
            "</div>",
            "</article>",
        ]
    )


def _archetype_variant_viewer(archetype: dict[str, Any]) -> str:
    variants = [
        _archetype_variant(deck, index)
        for index, deck in enumerate(archetype.get("member_decks") or [])
        if isinstance(deck, dict)
    ]
    if not variants:
        representative = archetype.get("representative_deck")
        if isinstance(representative, dict):
            variants = [_archetype_variant(representative, 0)]
    variant_count = len(variants)
    return "\n".join(
        [
            '<div class="variant-viewer" data-variant-root>',
            '<div class="variant-toolbar">',
            f'<span class="variant-label" data-variant-label>构筑 1/{max(variant_count, 1)}</span>',
            _variant_control(variant_count),
            "</div>",
            '<div class="variant-stage">',
            *variants,
            "</div>",
            "</div>",
        ]
    )


def _archetype_variant(deck: dict[str, Any], index: int) -> str:
    active_class = " is-active" if index == 0 else ""
    cards = list(deck.get("cards") or [])
    return "\n".join(
        [
            f'<div class="variant{active_class}" data-variant data-variant-index="{index}">',
            f'<div class="variant-cards">{_card_strip(cards)}</div>',
            f'<p class="variant-name">{_html(deck.get("deck_name") or deck.get("deck_fingerprint") or "-")}</p>',
            '<div class="variant-statline">',
            f'<span>{_html(deck.get("sample_count", 0))} 样本</span>',
            f'<span>{_player_summary_text(deck)}</span>',
            f'<span>{_fmt_rate(deck.get("win_rate")) or "-"} 胜率</span>',
            f'<span>{_fmt_rate(deck.get("wilson_lower_bound")) or "-"} Wilson</span>',
            f'<span>{_record_label(deck)}</span>',
            "</div>",
            "</div>",
        ]
    )


def _variant_stack(decks: list[dict[str, Any]], limit: int) -> str:
    if not decks:
        return ""
    rows = []
    for index, deck in enumerate(decks[:limit], start=1):
        rows.append(
            "\n".join(
                [
                    '<div class="variant-row">',
                    '<div class="variant-row-head">',
                    f'<strong>构筑 {index}</strong>',
                    f'<span>{_record_label(deck)}</span>',
                    "</div>",
                    _player_summary(deck),
                    f'<div class="card-strip">{_card_strip(deck.get("cards") or [])}</div>',
                    "</div>",
                ]
            )
        )
    if len(decks) > limit:
        rows.append(f'<p class="archetype-subnote">其余 {len(decks) - limit} 个构筑继续合并统计</p>')
    return f'<div class="variant-stack">{"".join(rows)}</div>'


def _feature_grid(decks: list[dict[str, Any]]) -> str:
    if not decks:
        return '<section class="empty">暂无符合当前版本与日期范围的上传样本。</section>'
    cards = []
    for index, deck in enumerate(decks, start=1):
        css_class = "feature-card-1" if index == 1 else f"feature-card-{index}"
        cards.append(
            "\n".join(
                [
                    f'<article class="feature-card {css_class}">',
                    '<div class="feature-meta">',
                    f'<span class="rank-badge">{index:02d}</span>',
                    f'<span>{_record_label(deck)}</span>',
                    "</div>",
                    f'<h2>{_html(deck.get("deck_name") or deck.get("deck_fingerprint") or "-")}</h2>',
                    _player_summary(deck),
                    f'<div class="feature-cards">{_card_strip(deck.get("cards") or [])}</div>',
                    '<div class="feature-stats signal-panel">',
                    _signal_groups(deck),
                    "</div>",
                    "</article>",
                ]
            )
        )
    return f'<section class="feature-grid">{"".join(cards)}</section>'


def _ranking_board(decks: list[dict[str, Any]]) -> str:
    if not decks:
        return ""
    return "\n".join(
        [
            '<section class="archetype-board" id="deck-ranking" data-sort-root>',
            '<div class="board-head"><span>Rank</span><span>Deck</span><span>Signals</span></div>',
            _deck_rows(decks),
            "</section>",
        ]
    )


def _deck_rows(decks: list[dict[str, Any]], start: int = 1) -> str:
    return "".join(_rank_row(index, deck) for index, deck in enumerate(decks, start=start))


def _rank_row(index: int, deck: dict[str, Any]) -> str:
    title = str(deck.get("deck_name") or deck.get("deck_fingerprint") or "")
    return "\n".join(
        [
            f'<article class="archetype-row" {_sort_item_attrs(title, deck.get("wilson_lower_bound"), deck.get("sample_count"))}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span>{_record_label(deck)}</span>',
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(title)}</h3>",
            _player_summary(deck),
            _deck_variant_viewer(deck),
            "</div>",
            '<div class="row-signals signal-panel">',
            _signal_groups(deck),
            "</div>",
            "</article>",
        ]
    )


def _deck_variant_viewer(deck: dict[str, Any]) -> str:
    return "\n".join(
        [
            '<div class="variant-viewer" data-variant-root>',
            '<div class="variant-toolbar">',
            '<span class="variant-label" data-variant-label>构筑 1/1</span>',
            _variant_control(1),
            "</div>",
            '<div class="variant-stage">',
            _archetype_variant(deck, 0),
            "</div>",
            "</div>",
        ]
    )


def _variant_control(variant_count: int) -> str:
    safe_count = max(1, int(variant_count or 0))
    label = f"{'Change' if safe_count > 1 else 'Single'} · {safe_count} 构筑"
    if safe_count > 1:
        return f'<button type="button" class="variant-button" data-variant-button>{_html(label)}</button>'
    return f'<span class="variant-single">{_html(label)}</span>'


def _signal_groups(item: dict[str, Any]) -> str:
    behavior = item.get("behavior_stats") if isinstance(item.get("behavior_stats"), dict) else {}
    metric_pills = [
        _score_pill("胜率", _fmt_rate(item.get("win_rate"))),
        _score_pill("Wilson", _fmt_rate(item.get("wilson_lower_bound"))),
        _player_metric_pill(item),
        _score_pill("样本", item.get("sample_count", 0)),
    ]
    metric_pills.extend(_dynamic_signal_pills(behavior))
    return "\n".join(
        [
            f'<div class="signal-group" title="战绩 {_record_label(item)}">',
            *metric_pills,
            "</div>",
            _behavior_panel(behavior),
        ]
    )


def _dynamic_signal_pills(behavior: dict[str, Any]) -> list[str]:
    if not behavior:
        return []
    pills: list[str] = []
    trend = behavior.get("trend") if isinstance(behavior.get("trend"), dict) else {}
    if trend:
        pills.append(_score_pill("近7日", _trend_compact_label(trend), title=_trend_label(trend)))
    credibility = behavior.get("credibility") if isinstance(behavior.get("credibility"), dict) else {}
    if credibility:
        pills.append(_score_pill("可信度", _credibility_label(credibility), title=_credibility_title(credibility)))
    return pills


def _behavior_panel(behavior: dict[str, Any]) -> str:
    if not behavior:
        return ""
    blocks = [
        _behavior_block("胜利局战器", behavior.get("weapons") or []),
        _behavior_block("流派统计", behavior.get("styles") or []),
        _soul_block(behavior.get("souls") or []),
    ]
    blocks = [block for block in blocks if block]
    return f'<div class="behavior-panel">{"".join(blocks)}</div>' if blocks else ""


def _behavior_block(title: str, rows: list[dict[str, Any]]) -> str:
    # 右侧信号区要保持 A 款短卡片高度，列表只露出 Top3。
    valid_rows = [row for row in rows if isinstance(row, dict)][:3]
    if not valid_rows:
        return ""
    return "\n".join(
        [
            '<section class="behavior-block">',
            f'<span class="behavior-title">{_html(title)}</span>',
            *(_behavior_row(row) for row in valid_rows),
            "</section>",
        ]
    )


def _behavior_row(row: dict[str, Any]) -> str:
    win_usage = row.get("win_usage_rate")
    width = _bar_width(win_usage if win_usage not in {None, ""} else row.get("usage_rate"))
    usage = _fmt_compact_rate(win_usage) or _fmt_compact_rate(row.get("usage_rate")) or "-"
    conditional = "样本不足" if row.get("low_sample") else f"胜{_fmt_compact_rate(row.get('conditional_win_rate')) or '-'}"
    meta = f"{usage} · {conditional}"
    detail = " / ".join(
        part
        for part in (
            f"{row.get('sample_count', 0)} 样本",
            f"赢局占比 {_fmt_rate(win_usage) or '-'}",
            f"总占比 {_fmt_rate(row.get('usage_rate')) or '-'}",
            "样本不足" if row.get("low_sample") else f"条件胜率 {_fmt_rate(row.get('conditional_win_rate')) or '-'}",
        )
        if part
    )
    return "\n".join(
        [
            f'<div class="behavior-row" title="{_html(detail)}">',
            f'<span class="behavior-name" title="{_html(row.get("name") or "-")}">{_html(row.get("name") or "-")}</span>',
            f'<span class="behavior-bar" aria-hidden="true"><span style="width: {width}"></span></span>',
            f'<span class="behavior-meta">{_html(meta)}</span>',
            "</div>",
        ]
    )


def _soul_block(rows: list[dict[str, Any]]) -> str:
    valid_rows = [row for row in rows if isinstance(row, dict)]
    if not valid_rows:
        return ""
    chips = "".join(
        f'<span class="score-pill"><strong>{_html(row.get("name") or "英魂")}</strong>{_fmt_rate(row.get("usage_rate")) or "-"}</span>'
        for row in valid_rows
    )
    return f'<section class="behavior-block"><span class="behavior-title">英魂配置</span><div class="signal-group">{chips}</div></section>'


def _trend_label(trend: dict[str, Any]) -> str:
    sample_count = _safe_int(trend.get("last_7d_sample_count"))
    rate = _fmt_rate(trend.get("last_7d_win_rate")) or "-"
    delta = _fmt_delta_points(trend.get("delta_7d"))
    return f"{sample_count} 样本 / {rate} / {delta}"


def _trend_compact_label(trend: dict[str, Any]) -> str:
    delta = _fmt_delta_points(trend.get("delta_7d"))
    if delta != "-":
        return delta
    return _fmt_rate(trend.get("last_7d_win_rate")) or "-"


def _credibility_label(credibility: dict[str, Any]) -> str:
    labels = {"high": "高", "medium": "中", "low": "低"}
    return labels.get(str(credibility.get("label") or ""), "低")


def _credibility_title(credibility: dict[str, Any]) -> str:
    return " / ".join(
        part
        for part in (
            f"样本 {_safe_int(credibility.get('sample_count'))}",
            f"玩家 {_safe_int(credibility.get('player_count'))}",
            f"Top3玩家贡献 {_fmt_rate(credibility.get('top3_player_share')) or '-'}",
        )
        if part
    )


def _fmt_delta_points(value: Any) -> str:
    if value in {None, ""}:
        return "-"
    try:
        return f"{float(value) * 100:+.1f}pt"
    except (TypeError, ValueError):
        return "-"


def _bar_width(value: Any) -> str:
    try:
        percent = max(0.0, min(float(value), 1.0)) * 100
    except (TypeError, ValueError):
        percent = 0.0
    return f"{percent:.1f}%"


def _fmt_compact_rate(value: Any) -> str:
    if value in {None, ""}:
        return ""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return ""


def _card_strip(cards: list[dict[str, Any]]) -> str:
    return "".join(_unit_figure(card) for card in cards)


def _record_label(deck: dict[str, Any]) -> str:
    return f'{_html(deck.get("win_count", 0))} win {_html(deck.get("loss_count", 0))} lose'


def _player_summary(item: dict[str, Any]) -> str:
    text = _player_summary_text(item)
    return f'<p class="deck-owner">{text}</p>' if text else ""


def _player_summary_text(item: dict[str, Any]) -> str:
    player_count = _safe_int(item.get("player_count"))
    top_player = str(item.get("top_player") or "").strip()
    top_player_count = _safe_int(item.get("top_player_count"))
    parts: list[str] = []
    if top_player:
        parts.append(f"最多玩家：{_html(top_player)}（{_html(top_player_count)}次）")
    parts.append(f"统计玩家：{_html(player_count)}人")
    return " · ".join(parts)


def _player_metric_pill(item: dict[str, Any]) -> str:
    for key in ("player_normalized_win_rate", "player_normalized_rate", "player_normalized"):
        rate = _fmt_rate(item.get(key))
        if rate:
            return _score_pill("玩家归一化", rate)
    player_count = _safe_int(item.get("player_count"))
    return _score_pill("玩家数", f"{player_count}人" if player_count else "-")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _unit_figure(card: dict[str, Any]) -> str:
    label = str(card.get("label") or card.get("card_hash") or "")
    image_url = str(card.get("image_url") or "")
    if image_url:
        media = f'<img src="{_html(image_url)}" alt="{_html(label)}" loading="lazy">'
    else:
        media = f'<div class="image-placeholder">{_html(_short_card_label(label))}</div>'
    return "\n".join(
        [
            '<figure class="unit">',
            media,
            f"<figcaption>{_html(label)}</figcaption>",
            "</figure>",
        ]
    )


def _sort_item_attrs(title: str, wilson: Any, sample_count: Any) -> str:
    return (
        "data-sort-item "
        f'data-sort-title="{_html(title.lower())}" '
        f'data-sort-wilson="{_sort_number(wilson)}" '
        f'data-sort-sample="{_sort_number(sample_count)}"'
    )


def _summary_item(label: str, value: Any) -> str:
    return f"<div><dt>{_html(label)}</dt><dd>{_html(value)}</dd></div>"


def _score_pill(label: str, value: Any, title: str = "") -> str:
    text = str(value) if value not in {None, ""} else "-"
    title_attr = f' title="{_html(title)}"' if title else ""
    return f'<span class="score-pill"{title_attr}><strong>{_html(label)}</strong>{_html(text)}</span>'


def _fmt_rate(value: Any) -> str:
    if value in {None, ""}:
        return ""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return ""


def _sort_number(value: Any) -> str:
    try:
        return f"{float(value):.8f}"
    except (TypeError, ValueError):
        return "0"


def _short_card_label(label: str) -> str:
    name = label.split("(", 1)[0].strip() or label
    return name if len(name) <= 8 else f"{name[:8]}..."


def _html(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
