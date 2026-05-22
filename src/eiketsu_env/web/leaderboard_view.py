"""排行榜页面 HTML 渲染与静态资源定位。"""

from __future__ import annotations

import html
import hashlib
import os
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
    configured_root = os.environ.get("EIKETSU_ENV_ROOT")
    candidates = []
    if configured_root:
        candidates.append(Path(configured_root).expanduser().resolve() / "frontend" / "leaderboard")
    candidates.append(Path.cwd().resolve() / "frontend" / "leaderboard")
    project_root = Path(__file__).resolve().parents[3]
    candidates.append(project_root / "frontend" / "leaderboard")
    for frontend_root in candidates:
        if frontend_root.is_dir():
            return frontend_root
    return Path(__file__).resolve().parent


WEB_ROOT = _leaderboard_root()
WEB_TEMPLATE_ROOT = WEB_ROOT / "templates"
WEB_STATIC_ROOT = WEB_ROOT / "static"
LEADERBOARD_STATIC_FILES = {"leaderboard.css", "leaderboard.js"}

LEADERBOARD_HTML_DEFAULT_LIMIT = 80
LEADERBOARD_HTML_MAX_LIMIT = 500


def _leaderboard_asset_version() -> str:
    digest = hashlib.sha256()
    digest.update(__version__.encode("utf-8"))
    has_static_asset = False
    for filename in sorted(LEADERBOARD_STATIC_FILES):
        path = WEB_STATIC_ROOT / filename
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        has_static_asset = True
        digest.update(filename.encode("utf-8"))
        digest.update(content)
    if not has_static_asset:
        return __version__
    return f"{__version__}-{digest.hexdigest()[:12]}"


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
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
    pagination_total = _safe_int(pagination.get("total")) if pagination else len(all_items)
    total_count = pagination_total if pagination_total > 0 else len(all_items)
    shown_count = _safe_int(pagination.get("offset")) + len(visible_items) if pagination else len(visible_items)
    page_size = _safe_int(pagination.get("limit")) if pagination else display_limit
    if page_size is not None and page_size <= 0:
        page_size = display_limit
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
        shown_count=shown_count,
        total_count=total_count,
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
            "asset_version": _html(_leaderboard_asset_version()),
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
                visible_count=shown_count,
                total_count=total_count,
                page_size=page_size,
            ),
        },
    )


def _leaderboard_matchup_matrix_page(payload: dict[str, Any]) -> str:
    title = "公开卡组对局矩阵"
    matrix = payload.get("matchup_matrix") if isinstance(payload.get("matchup_matrix"), dict) else {}
    matrix_limit = _safe_int(matrix.get("limit")) if matrix else 0
    min_sample = _safe_int(matrix.get("min_sample_count")) if matrix else 0
    summary_items = "".join(
        [
            _summary_item("目标版本", payload.get("target_version", "")),
            _summary_item("采集日期", f"{payload.get('date_from', '')} 至 {payload.get('date_to', '')}"),
            _summary_item("对局数", payload.get("match_count", 0)),
            _summary_item("双方样本", payload.get("side_sample_count", 0)),
            _summary_item("矩阵规模", f"前 {matrix_limit or '-'} 卡组"),
            _summary_item("最低样本", min_sample or "-"),
        ]
    )
    notice = _leaderboard_display_notice(
        payload,
        cluster_enabled=False,
        contributor_name="",
        display_limit=None,
        shown_count=0,
        total_count=0,
    )
    matrix_html = _matchup_matrix_table(payload) if str(payload.get("leaderboard_status") or "") == "ready" else ""
    return _render_web_template(
        "leaderboard_matchups.html",
        {
            "page_title": _html(title),
            "asset_version": _html(_leaderboard_asset_version()),
            "title": _html(title),
            "eyebrow": "PUBLIC MATCHUP MATRIX",
            "summary_items": summary_items,
            "mobile_summary": _leaderboard_mobile_summary(payload, "上传批次"),
            "privacy_note": _html("公开矩阵只展示匿名聚合结果，不展示贡献者、token、浏览器信息或本地路径。"),
            "view_controls": _leaderboard_matchup_view_controls(payload),
            "display_notice": notice,
            "matrix": matrix_html,
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
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
    if pagination:
        safe_offset = max(0, _safe_int(pagination.get("offset")))
        page_size = max(1, min(_safe_int(pagination.get("limit")) or LEADERBOARD_HTML_DEFAULT_LIMIT, LEADERBOARD_HTML_MAX_LIMIT))
        page_items = items
        total = max(0, _safe_int(pagination.get("total")))
        next_offset = safe_offset + len(page_items)
        has_more = bool(pagination.get("has_more")) and next_offset < total
    else:
        sorted_items = _sort_leaderboard_items(items, sort_key)
        safe_offset = max(0, int(offset or 0))
        page_size = max(1, min(int(limit or LEADERBOARD_HTML_DEFAULT_LIMIT), LEADERBOARD_HTML_MAX_LIMIT))
        page_items = sorted_items[safe_offset : safe_offset + page_size]
        next_offset = safe_offset + len(page_items)
        total = len(sorted_items)
        has_more = next_offset < total
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
        "total": total,
        "has_more": has_more,
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
            "row_type": str(payload.get("row_type") or ("archetype" if cluster_enabled else "deck")),
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
    status = str(payload.get("leaderboard_status") or "")
    if status == "missing":
        target_version = str(payload.get("target_version") or "").strip()
        available_versions = [
            version
            for version in dict.fromkeys(str(item or "").strip() for item in payload.get("available_target_versions") or [])
            if version and version != target_version
        ]
        suffix = "旧版本可在上方“目标版本”切换查看。" if available_versions else "等待上传样本后刷新榜单。"
        return f'<section class="empty">当前目标版本 {_html(target_version)} 暂无公开榜单数据。{_html(suffix)}</section>'
    if status in {"building", "running"}:
        return '<section class="empty">榜单生成中，请稍后刷新。当前不会在页面请求里同步重算。</section>'
    if status == "failed":
        return '<section class="empty">榜单生成失败，请在服务端重新执行刷新命令。</section>'
    return ""


def _leaderboard_view_controls(payload: dict[str, Any], cluster_enabled: bool, contributor_name: str = "") -> str:
    rank_scope = str(payload.get("rank_scope") or RANK_SCOPE_ALL)
    if rank_scope not in RANK_SCOPE_LABELS:
        rank_scope = RANK_SCOPE_ALL
    base_params = _leaderboard_base_query(payload, contributor_name)
    is_public = str(payload.get("scope") or "public") == "public"
    active_version = str(payload.get("target_version") or "").strip()
    available_versions = [
        version
        for version in dict.fromkeys(str(item or "").strip() for item in payload.get("available_target_versions") or [])
        if version
    ]
    if active_version and active_version not in available_versions:
        available_versions.insert(0, active_version)
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
    matchup_group = []
    if is_public:
        matchup_group = [
            '<div class="view-control-group"><span class="view-control-label">分析</span>',
            _view_control_link("对局矩阵", _leaderboard_path_query_url("/leaderboard/matchups", base_params, rank_scope=rank_scope), False),
            "</div>",
        ]
    version_group = []
    if str(payload.get("scope") or "public") == "public" and available_versions:
        version_params = {
            **base_params,
            "cluster": "on" if cluster_enabled else "off",
            "rank_scope": rank_scope,
        }
        version_params.pop("version", None)
        version_group = [
            '<form class="view-control-group view-control-version-form" method="get" action="/leaderboard">',
            '<label class="view-control-label" for="leaderboard-version-select">目标版本</label>',
            *_hidden_query_inputs(version_params),
            '<select id="leaderboard-version-select" class="view-control-select" name="version" onchange="this.form.submit()">',
            *[
                f'<option value="{_html(version)}"{" selected" if version == active_version else ""}>{_html(version)}</option>'
                for version in available_versions
            ],
            "</select>",
            '<noscript><button class="view-control-submit" type="submit">切换</button></noscript>',
            "</form>",
        ]
    return "\n".join(
        [
            '<section class="view-controls" aria-label="榜单视图设置">',
            '<div class="view-control-group"><span class="view-control-label">聚类</span>',
            *cluster_links,
            "</div>",
            *version_group,
            '<div class="view-control-group"><span class="view-control-label">段位</span>',
            *rank_links,
            "</div>",
            *matchup_group,
            "</section>",
        ]
    )


def _leaderboard_matchup_view_controls(payload: dict[str, Any]) -> str:
    rank_scope = str(payload.get("rank_scope") or RANK_SCOPE_ALL)
    if rank_scope not in RANK_SCOPE_LABELS:
        rank_scope = RANK_SCOPE_ALL
    base_params = _leaderboard_base_query(payload)
    active_version = str(payload.get("target_version") or "").strip()
    available_versions = [
        version
        for version in dict.fromkeys(str(item or "").strip() for item in payload.get("available_target_versions") or [])
        if version
    ]
    if active_version and active_version not in available_versions:
        available_versions.insert(0, active_version)
    rank_links = [
        _view_control_link(label, _leaderboard_path_query_url("/leaderboard/matchups", base_params, rank_scope=value), rank_scope == value)
        for value, label in (
            (RANK_SCOPE_ALL, RANK_SCOPE_LABELS[RANK_SCOPE_ALL]),
            (RANK_SCOPE_TRAVELER_DOWN, RANK_SCOPE_LABELS[RANK_SCOPE_TRAVELER_DOWN]),
            (RANK_SCOPE_KNIGHT_DOWN, RANK_SCOPE_LABELS[RANK_SCOPE_KNIGHT_DOWN]),
            (RANK_SCOPE_KNIGHT_UP, RANK_SCOPE_LABELS[RANK_SCOPE_KNIGHT_UP]),
        )
    ]
    version_group = []
    if available_versions:
        version_params = {**base_params, "rank_scope": rank_scope}
        version_params.pop("version", None)
        version_group = [
            '<form class="view-control-group view-control-version-form" method="get" action="/leaderboard/matchups">',
            '<label class="view-control-label" for="matchup-version-select">目标版本</label>',
            *_hidden_query_inputs(version_params),
            '<select id="matchup-version-select" class="view-control-select" name="version" onchange="this.form.submit()">',
            *[
                f'<option value="{_html(version)}"{" selected" if version == active_version else ""}>{_html(version)}</option>'
                for version in available_versions
            ],
            "</select>",
            '<noscript><button class="view-control-submit" type="submit">切换</button></noscript>',
            "</form>",
        ]
    return "\n".join(
        [
            '<section class="view-controls" aria-label="对局矩阵筛选">',
            '<div class="view-control-group"><span class="view-control-label">页面</span>',
            _view_control_link("返回榜单", _leaderboard_path_query_url("/leaderboard", base_params, cluster="off", rank_scope=rank_scope), False),
            "</div>",
            *version_group,
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
    version = str(payload.get("target_version") or "").strip()
    if version:
        return {"version": version}
    return {}


def _leaderboard_query_url(base_params: dict[str, str], **updates: str) -> str:
    return _leaderboard_path_query_url("/leaderboard", base_params, **updates)


def _leaderboard_path_query_url(path: str, base_params: dict[str, str], **updates: str) -> str:
    params = {key: value for key, value in {**base_params, **updates}.items() if value}
    return path + (f"?{urlencode(params)}" if params else "")


def _hidden_query_inputs(params: dict[str, str]) -> list[str]:
    return [
        f'<input type="hidden" name="{_html(key)}" value="{_html(value)}">'
        for key, value in params.items()
        if value
    ]


def _view_control_link(label: str, href: str, active: bool) -> str:
    css_class = "view-control-link is-active" if active else "view-control-link"
    return f'<a class="{css_class}" href="{_html(href)}">{_html(label)}</a>'


def _matchup_matrix_table(payload: dict[str, Any]) -> str:
    matrix = payload.get("matchup_matrix") if isinstance(payload.get("matchup_matrix"), dict) else {}
    columns = [item for item in matrix.get("columns") or [] if isinstance(item, dict)]
    rows = [item for item in matrix.get("rows") or [] if isinstance(item, dict)]
    if not columns or not rows:
        return '<section class="empty">当前公开榜还没有可生成矩阵的卡组数据。</section>'
    header_cells = [
        '<th class="matchup-matrix-corner" scope="col">卡组</th>',
        *[
            f'<th class="matchup-matrix-column" scope="col">{_matchup_matrix_deck_heading(column)}</th>'
            for column in columns
        ],
    ]
    body_rows = []
    for row in rows:
        deck = row.get("deck") if isinstance(row.get("deck"), dict) else {}
        cells = [cell if isinstance(cell, dict) else {} for cell in row.get("cells") or []]
        body_rows.append(
            "\n".join(
                [
                    "<tr>",
                    f'<th class="matchup-matrix-row-head" scope="row">{_matchup_matrix_deck_heading(deck)}</th>',
                    *[_matchup_matrix_cell(cell) for cell in cells[: len(columns)]],
                    "</tr>",
                ]
            )
        )
    min_sample = _safe_int(matrix.get("min_sample_count"))
    return "\n".join(
        [
            '<section class="matchup-matrix-panel" aria-label="卡组对局矩阵">',
            '<div class="matchup-matrix-meta">',
            f'<span>胜率优先前 {_safe_int(matrix.get("limit")) or len(columns)} 卡组</span>',
            f'<span>低于 {min_sample or 0} 场仅显示样本数</span>',
            "</div>",
            '<div class="matchup-matrix-scroll">',
            '<table class="matchup-matrix-table">',
            f"<thead><tr>{''.join(header_cells)}</tr></thead>",
            f"<tbody>{''.join(body_rows)}</tbody>",
            "</table>",
            "</div>",
            "</section>",
        ]
    )


def _matchup_matrix_deck_heading(deck: dict[str, Any]) -> str:
    index = _safe_int(deck.get("matrix_index")) or _safe_int(deck.get("rank"))
    index_label = f"#{index:02d}" if index else "#--"
    name = str(deck.get("deck_name") or deck.get("deck_fingerprint") or "-")
    cards = _matchup_matrix_cards(deck)
    return "\n".join(
        [
            '<span class="matchup-matrix-deck">',
            _matchup_matrix_avatar(cards, name),
            '<span class="matchup-matrix-copy">',
            f'<span class="matchup-matrix-rank">{_html(index_label)}</span>',
            f'<span class="matchup-matrix-name" title="{_html(name)}">{_html(_short_deck_label(name))}</span>',
            _matchup_matrix_mini_strip(cards),
            "</span>",
            "</span>",
        ]
    )


def _matchup_matrix_cards(deck: dict[str, Any]) -> list[dict[str, Any]]:
    cards = deck.get("cards")
    if not isinstance(cards, list):
        return []
    return [card for card in cards if isinstance(card, dict)]


def _matchup_matrix_avatar(cards: list[dict[str, Any]], deck_name: str) -> str:
    representative = cards[0] if cards else {}
    label = str(representative.get("label") or deck_name or "-")
    image_url = str(representative.get("image_url") or "")
    if image_url:
        return (
            '<span class="matchup-matrix-avatar">'
            f'<img src="{_html(image_url)}" alt="{_html(label)}" loading="lazy">'
            "</span>"
        )
    return f'<span class="matchup-matrix-avatar is-placeholder">{_html(_short_card_label(label))}</span>'


def _matchup_matrix_mini_strip(cards: list[dict[str, Any]]) -> str:
    if not cards:
        return ""
    chips = []
    for card in cards[:4]:
        label = str(card.get("label") or card.get("card_hash") or "")
        image_url = str(card.get("image_url") or "")
        if image_url:
            chips.append(
                '<span class="matchup-matrix-mini-card">'
                f'<img src="{_html(image_url)}" alt="{_html(label)}" loading="lazy">'
                "</span>"
            )
        else:
            chips.append(
                f'<span class="matchup-matrix-mini-card is-placeholder">{_html(_short_card_label(label))}</span>'
            )
    return f'<span class="matchup-matrix-mini-strip">{"".join(chips)}</span>'


def _matchup_matrix_cell(cell: dict[str, Any]) -> str:
    sample = _safe_int(cell.get("sample_count"))
    if not cell.get("visible"):
        if sample > 0:
            return f'<td class="matchup-matrix-cell is-low-sample" title="样本不足"><small>n={sample}</small></td>'
        return '<td class="matchup-matrix-cell is-empty" aria-label="样本不足"></td>'
    tone = str(cell.get("tone") or "even")
    if tone not in {"advantage", "even", "disadvantage"}:
        tone = "even"
    rate = _fmt_rate(cell.get("win_rate")) or "-"
    title = f"{_safe_int(cell.get('win_count'))}胜 / {_safe_int(cell.get('loss_count'))}负 / {_safe_int(cell.get('draw_count'))}平"
    return (
        f'<td class="matchup-matrix-cell is-{_html(tone)}" title="{_html(title)}">'
        f'<span>{_html(rate)}</span><small> · n={sample}</small></td>'
    )


def _short_deck_label(label: str) -> str:
    text = " / ".join(part.strip() for part in str(label or "").split("/")[:2] if part.strip())
    text = text or str(label or "")
    return text if len(text) <= 20 else f"{text[:20]}..."


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
    return _report_card_board(archetypes, board_id="archetype-ranking", item_type="archetype")


def _archetype_rows(archetypes: list[dict[str, Any]], start: int = 1) -> str:
    return "".join(
        _deck_report_card(index, archetype, item_type="archetype")
        for index, archetype in enumerate(archetypes, start=start)
    )


def _report_card_board(items: list[dict[str, Any]], *, board_id: str, item_type: str) -> str:
    rows = _archetype_rows(items) if item_type == "archetype" else _deck_rows(items)
    return "\n".join(
        [
            f'<section class="archetype-board report-card-board" id="{_html(board_id)}" data-sort-root>',
            rows,
            "</section>",
        ]
    )


def _deck_report_card(index: int, item: dict[str, Any], *, item_type: str) -> str:
    title = _report_card_title(item, item_type)
    return "\n".join(
        [
            f'<article class="deck-report-card archetype-row" {_sort_item_attrs(title, item.get("wilson_lower_bound"), item.get("sample_count"))}>',
            _rank_block(index, item),
            _deck_identity_block(item, item_type=item_type),
            _strength_metrics_block(item),
            _equipment_style_block(item),
            _matchup_summary_block(item),
            "</article>",
        ]
    )


def _rank_block(index: int, item: dict[str, Any]) -> str:
    trend_label, trend_class, trend_title = _rank_trend(item)
    return "\n".join(
        [
            '<section class="rank-block report-card-block" aria-label="排名">',
            '<span class="rank-kicker">Rank</span>',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span class="rank-record">{_record_label(item)}</span>',
            f'<span class="rank-trend {trend_class}" title="{_html(trend_title)}">{_html(trend_label)}</span>',
            "</section>",
        ]
    )


def _deck_identity_block(item: dict[str, Any], *, item_type: str) -> str:
    title = _report_card_title(item, item_type)
    variant_viewer = _archetype_variant_viewer(item) if item_type == "archetype" else _deck_variant_viewer(item)
    return "\n".join(
        [
            '<section class="deck-identity-block report-card-block" aria-label="卡组身份">',
            '<div class="identity-heading">',
            f"<h3>{_html(title)}</h3>",
            f'<span class="identity-archetype">{_html(_archetype_label(item, item_type))}</span>',
            "</div>",
            '<div class="identity-meta">',
            _identity_pill("最多玩家", _top_player_label(item)),
            _identity_pill("统计玩家", f"{_safe_int(item.get('player_count'))}人"),
            _identity_pill("构筑", _variant_count_label(item, item_type)),
            "</div>",
            variant_viewer,
            "</section>",
        ]
    )


def _strength_metrics_block(item: dict[str, Any]) -> str:
    behavior = _behavior_stats_from_item(item)
    trend = behavior.get("trend") if isinstance(behavior.get("trend"), dict) else {}
    credibility = behavior.get("credibility") if isinstance(behavior.get("credibility"), dict) else {}
    trend_value = _trend_compact_label(trend) if trend else "-"
    return "\n".join(
        [
            '<section class="strength-metrics-block report-card-block" aria-label="强度指标">',
            '<div class="block-title">强度指标</div>',
            '<div class="metric-card-grid">',
            _metric_card("胜率", _fmt_rate(item.get("win_rate")) or "-", "is-primary"),
            _metric_card("Wilson", _fmt_rate(item.get("wilson_lower_bound")) or "-", ""),
            _metric_card("玩家归一化", _player_normalized_rate(item), ""),
            _metric_card("样本数", str(_safe_int(item.get("sample_count"))), ""),
            _metric_card("玩家数", f"{_safe_int(item.get('player_count'))}人", ""),
            _metric_card("近7日趋势", trend_value if trend_value != "-" else "暂无", "is-trend", title=_trend_label(trend) if trend else ""),
            _metric_card("可信度", _credibility_label(credibility), f"is-credibility {_credibility_css_class(credibility)}", title=_credibility_title(credibility) if credibility else ""),
            "</div>",
            "</section>",
        ]
    )


def _equipment_style_block(item: dict[str, Any]) -> str:
    behavior = _behavior_stats_from_item(item)
    weapon = _first_behavior_row(behavior.get("weapons"))
    style = _first_behavior_row(behavior.get("styles"))
    souls = [row for row in behavior.get("souls") or [] if isinstance(row, dict)]
    return "\n".join(
        [
            '<section class="equipment-style-block report-card-block" aria-label="战器与流派">',
            '<div class="block-title">主流战器 / 流派</div>',
            '<div class="equipment-grid">',
            _equipment_rows(weapon, empty_text="暂无战器统计", kind="weapon"),
            _soul_rows(souls),
            _equipment_rows(style, empty_text="暂无流派统计", kind="style"),
            "</div>",
            "</section>",
        ]
    )


def _matchup_summary_block(item: dict[str, Any]) -> str:
    advantages, disadvantages = _matchup_groups(item)
    if not advantages and not disadvantages:
        return '<section class="matchup-summary-block" aria-label="对局摘要"><span class="matchup-empty">暂无对局矩阵</span></section>'
    return "\n".join(
        [
            '<section class="matchup-summary-block" aria-label="对局摘要">',
            _matchup_group("优势对局", advantages, "advantage"),
            _matchup_group("劣势对局", disadvantages, "disadvantage"),
            "</section>",
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
            '<div class="variant-scroll" data-card-scroll>',
            '<button type="button" class="variant-scroll-button is-left" data-card-scroll-left aria-label="向左滚动卡组">‹</button>',
            f'<div class="variant-cards" data-card-scroll-strip>{_card_strip(cards)}</div>',
            '<button type="button" class="variant-scroll-button is-right" data-card-scroll-right aria-label="向右滚动卡组">›</button>',
            "</div>",
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
    return _report_card_board(decks, board_id="deck-ranking", item_type="deck")


def _deck_rows(decks: list[dict[str, Any]], start: int = 1) -> str:
    return "".join(_deck_report_card(index, deck, item_type="deck") for index, deck in enumerate(decks, start=start))


def _rank_row(index: int, deck: dict[str, Any]) -> str:
    return _deck_report_card(index, deck, item_type="deck")


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


def _report_card_title(item: dict[str, Any], item_type: str) -> str:
    if item_type == "archetype":
        return str(item.get("title") or item.get("archetype_name") or item.get("archetype_id") or "-")
    return str(item.get("deck_name") or item.get("deck_fingerprint") or item.get("title") or "-")


def _archetype_label(item: dict[str, Any], item_type: str) -> str:
    if item_type == "archetype":
        title = str(item.get("title") or "").strip()
        return title if title else "未识别分类"
    for key in ("archetype", "archetype_name", "faction", "school"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return "未归类"


def _identity_pill(label: str, value: Any) -> str:
    text = str(value) if value not in {None, ""} else "-"
    return f'<span class="identity-pill"><strong>{_html(label)}</strong>{_html(text)}</span>'


def _top_player_label(item: dict[str, Any]) -> str:
    top_player = str(item.get("top_player") or "").strip()
    if not top_player:
        return "暂无"
    return f"{top_player}（{_safe_int(item.get('top_player_count'))}次）"


def _variant_count_label(item: dict[str, Any], item_type: str) -> str:
    if item_type != "archetype":
        return "1/1"
    variants = [deck for deck in item.get("member_decks") or [] if isinstance(deck, dict)]
    if variants:
        return f"1/{len(variants)}"
    if isinstance(item.get("representative_deck"), dict):
        return "1/1"
    member_count = _safe_int(item.get("member_deck_count") or item.get("member_count"))
    return f"1/{max(member_count, 1)}"


def _rank_trend(item: dict[str, Any]) -> tuple[str, str, str]:
    for key in ("rank_delta", "rank_change", "trend_rank_change"):
        value = _float_or_none(item.get(key))
        if value is None:
            continue
        if value > 0:
            return f"▲ {abs(value):.0f}", "trend-up", f"排名变化 +{value:.0f}"
        if value < 0:
            return f"▼ {abs(value):.0f}", "trend-down", f"排名变化 {value:.0f}"
        return "→ 0", "trend-neutral", "排名无变化"

    behavior = _behavior_stats_from_item(item)
    trend = behavior.get("trend") if isinstance(behavior.get("trend"), dict) else {}
    delta = _float_or_none(trend.get("delta_7d")) if trend else None
    if delta is not None:
        if delta > 0:
            return _fmt_delta_points(delta), "trend-up", _trend_label(trend)
        if delta < 0:
            return _fmt_delta_points(delta), "trend-down", _trend_label(trend)
        return "±0.0pt", "trend-neutral", _trend_label(trend)
    return "暂无变化", "trend-unknown", "暂无排名变化或近7日趋势"


def _behavior_stats_from_item(item: dict[str, Any]) -> dict[str, Any]:
    behavior = item.get("behavior_stats")
    return behavior if isinstance(behavior, dict) else {}


def _metric_card(label: str, value: Any, css_class: str = "", title: str = "") -> str:
    class_attr = f"metric-card {css_class}".strip()
    title_attr = f' title="{_html(title)}"' if title else ""
    text = str(value) if value not in {None, ""} else "-"
    return "\n".join(
        [
            f'<div class="{_html(class_attr)}"{title_attr}>',
            f'<span>{_html(label)}</span>',
            f'<strong>{_html(text)}</strong>',
            "</div>",
        ]
    )


def _player_normalized_rate(item: dict[str, Any]) -> str:
    for key in ("player_normalized_win_rate", "player_normalized_rate", "player_normalized"):
        rate = _fmt_rate(item.get(key))
        if rate:
            return rate
    return "-"


def _credibility_css_class(credibility: dict[str, Any]) -> str:
    label = str(credibility.get("label") or "low")
    return {
        "high": "credibility-high",
        "medium": "credibility-medium",
        "low": "credibility-low",
    }.get(label, "credibility-low")


def _first_behavior_row(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if isinstance(row, dict):
            return row
    return {}


def _equipment_rows(row: dict[str, Any], *, empty_text: str, kind: str) -> str:
    if not row:
        return f'<div class="equipment-section is-empty"><span>{_html(empty_text)}</span></div>'
    if kind == "weapon":
        title = "战器统计"
        name_label = "主流战器"
        usage_label = "战器采用率"
        win_label = "战器胜率"
    else:
        title = "流派统计"
        name_label = "主流流派"
        usage_label = "流派采用率"
        win_label = "流派胜率"
    win_rate = "样本不足" if row.get("low_sample") else (_fmt_rate(row.get("conditional_win_rate")) or "-")
    return "\n".join(
        [
            '<div class="equipment-section">',
            f'<span class="equipment-title">{_html(title)}</span>',
            '<div class="equipment-row-grid">',
            _equipment_cell(name_label, row.get("name") or "-"),
            _equipment_cell(usage_label, _fmt_rate(row.get("usage_rate")) or "-"),
            _equipment_cell(win_label, win_rate),
            "</div>",
            "</div>",
        ]
    )


def _equipment_cell(label: str, value: Any) -> str:
    text = str(value) if value not in {None, ""} else "-"
    return f'<span class="equipment-cell"><strong>{_html(label)}</strong>{_html(text)}</span>'


def _soul_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="equipment-section is-empty"><span>暂无英魂配置</span></div>'
    chips = "".join(
        f'<span class="soul-chip"><strong>{_html(row.get("name") or "英魂")}</strong>{_fmt_rate(row.get("usage_rate")) or "-"}</span>'
        for row in rows[:3]
    )
    return "\n".join(
        [
            '<div class="equipment-section">',
            '<span class="equipment-title">英魂配置</span>',
            f'<div class="soul-chip-row">{chips}</div>',
            "</div>",
        ]
    )


def _matchup_groups(item: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary = _matchup_summary(item)
    if not summary:
        return [], []
    advantages = _matchup_rows(summary, ("advantages", "advantage_matchups", "favorable", "good"))
    disadvantages = _matchup_rows(summary, ("disadvantages", "disadvantage_matchups", "unfavorable", "bad"))
    return advantages[:4], disadvantages[:4]


def _matchup_summary(item: dict[str, Any]) -> dict[str, Any]:
    # 只消费后端显式给出的 matchup 结构；当前页面不从卡组胜率反推对局矩阵。
    for key in ("matchup_summary", "matchups", "matchup_matrix"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _matchup_rows(summary: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = summary.get(key)
        if isinstance(value, list):
            return [row if isinstance(row, dict) else {"name": row} for row in value]
    return []


def _matchup_group(title: str, rows: list[dict[str, Any]], tone: str) -> str:
    chips = "".join(_matchup_chip(row, tone) for row in rows)
    if not chips:
        chips = '<span class="matchup-chip is-unknown">暂无</span>'
    return "\n".join(
        [
            f'<div class="matchup-group is-{_html(tone)}">',
            f'<span class="matchup-title">{_html(title)}</span>',
            f'<div class="matchup-chip-row">{chips}</div>',
            "</div>",
        ]
    )


def _matchup_chip(row: dict[str, Any], tone: str) -> str:
    name = str(_first_present(row, "name", "matchup_name", "opponent_name", "title", "opponent_id") or "未知对局")
    rate = _fmt_rate(_first_present(row, "win_rate", "rate", "value"))
    text = f"{name} {rate}" if rate else name
    return f'<span class="matchup-chip is-{_html(tone)}">{_html(text)}</span>'


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return ""


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
    draw_count = _safe_int(deck.get("draw_count"))
    draw_text = f' / {_html(draw_count)} draw' if draw_count else ""
    return f'{_html(deck.get("win_count", 0))} win / {_html(deck.get("loss_count", 0))} lose{draw_text}'


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


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unit_figure(card: dict[str, Any]) -> str:
    label = str(card.get("label") or card.get("card_hash") or "")
    image_url = str(card.get("image_url") or "")
    if image_url:
        media = f'<img src="{_html(image_url)}" alt="{_html(label)}" loading="lazy">'
    else:
        media = f'<div class="image-placeholder">{_html(_short_card_label(label))}</div>'
    caption = _card_caption(label)
    return "\n".join(
        [
            f'<figure class="unit" title="{_html(label)}">',
            media,
            f"<figcaption>{caption}</figcaption>",
            "</figure>",
        ]
    )


def _card_caption(label: str) -> str:
    name, detail = _split_card_label(label)
    if not detail:
        return f'<span class="unit-name">{_html(name)}</span>'
    return f'<span class="unit-name">{_html(name)}</span><span class="unit-meta">{_html(detail)}</span>'


def _split_card_label(label: str) -> tuple[str, str]:
    text = str(label or "").strip()
    if text.endswith(")") and "(" in text:
        name, detail = text.rsplit("(", 1)
        return name.strip() or text, detail[:-1].strip()
    return text, ""


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
