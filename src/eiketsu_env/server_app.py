"""FastAPI VPS 应用：接收客户端上传并提供即时查看页面。"""

from __future__ import annotations

import html
import mimetypes
import secrets
from typing import Any
from urllib.parse import parse_qs, quote

from eiketsu_env import __version__
from eiketsu_env.config import Settings, load_settings
from eiketsu_env.db.migrations import upgrade_database
from eiketsu_env.services.client_update import (
    CLIENT_EXE_LEGACY_DOWNLOAD_NAME,
    client_update_payload,
    load_client_update_manifest,
    resolve_client_update_file,
)
from eiketsu_env.services.leaderboard import (
    LEADERBOARD_DEFAULT_PAGE_LIMIT,
    LEADERBOARD_SNAPSHOT_LIMIT,
    RANK_SCOPE_ALL,
    contributor_leaderboard,
    personal_leaderboard,
    public_leaderboard_page,
    refresh_public_leaderboard_snapshots,
)
from eiketsu_env.services.server_share import (
    ServerAuthError,
    bind_invite,
    create_invite,
    get_server_config,
    import_uploaded_package,
    list_invites,
    list_my_uploads,
)
from eiketsu_env.web.leaderboard_view import (
    LEADERBOARD_HTML_DEFAULT_LIMIT,
    LEADERBOARD_HTML_MAX_LIMIT,
    LEADERBOARD_STATIC_FILES,
    WEB_STATIC_ROOT,
    _leaderboard_display_limit,
    _leaderboard_rows_response,
    _leaderboard_visual_page,
)

try:  # FastAPI 是 server extra；本地只跑采集测试时不强制安装。
    from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    from starlette.middleware.gzip import GZipMiddleware
except ModuleNotFoundError:  # pragma: no cover - 当前开发虚拟环境可能未安装 server extra。
    Body = None  # type: ignore[assignment]
    BackgroundTasks = None  # type: ignore[assignment]
    FastAPI = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    FileResponse = None  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]
    RedirectResponse = None  # type: ignore[assignment]
    GZipMiddleware = None  # type: ignore[assignment]

def create_app(settings: Settings | None = None):
    if FastAPI is None:
        raise RuntimeError("缺少 FastAPI 依赖；请安装 `pip install .[server]` 后再启动服务端")
    settings = settings or load_settings()
    upgrade_database(settings)
    app = FastAPI(title="Eiketsu Upload Server", version=__version__)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/config")
    def api_config() -> dict[str, Any]:
        return get_server_config(settings)

    @app.get("/api/v1/client/update")
    def api_client_update(request: Request, current_version: str = "") -> dict[str, Any]:
        return client_update_payload(settings, current_version=current_version, base_url=str(request.base_url))

    @app.post("/api/v1/auth/bind-invite")
    async def api_bind_invite(request: Request) -> dict[str, str]:
        payload = await request.json()
        try:
            result = bind_invite(
                settings,
                str(payload.get("invite_code") or ""),
                str(payload.get("contributor_name") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "api_token": result.api_token,
            "token_prefix": result.token_prefix,
            "user_public_id": result.user_public_id,
            "contributor_name": result.contributor_name,
        }

    @app.post("/api/v1/uploads")
    def api_upload(
        request: Request,
        background_tasks: BackgroundTasks,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        token = _bearer_token(request)
        package_text = str(payload.get("package_text") or "")
        if not package_text:
            raise HTTPException(status_code=400, detail="package_text 不能为空")
        try:
            result = import_uploaded_package(settings, token, package_text)
        except ServerAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not result.already_uploaded:
            background_tasks.add_task(refresh_public_leaderboard_snapshots, settings)
        return {
            "upload_id": result.upload_id,
            "package_id": result.package_id,
            "content_hash": result.content_hash,
            "status": result.status,
            "match_count": result.match_count,
            "imported_match_count": result.imported_match_count,
            "already_uploaded": result.already_uploaded,
            "errors": result.errors,
        }

    @app.get("/api/v1/me/uploads")
    def api_my_uploads(request: Request) -> dict[str, Any]:
        try:
            return list_my_uploads(settings, _bearer_token(request))
        except ServerAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/v1/leaderboard")
    def api_leaderboard(
        request: Request,
        scope: str = "public",
        contributor: str = "",
        rank_scope: str = RANK_SCOPE_ALL,
        cluster: str = "on",
        row_type: str = "",
        offset: int = 0,
        limit: int | None = LEADERBOARD_DEFAULT_PAGE_LIMIT,
        sort: str = "wilson",
        full: str = "",
    ) -> dict[str, Any]:
        include_archetypes = _cluster_enabled(cluster)
        service_limit = _leaderboard_service_limit(limit, full)
        leaderboard_kwargs = {
            "limit": service_limit,
            "archetype_limit": service_limit,
            "rank_scope": rank_scope,
            "include_archetypes": include_archetypes,
        }
        try:
            if scope == "contributor":
                return contributor_leaderboard(settings, contributor, **leaderboard_kwargs)
            if scope == "mine":
                return personal_leaderboard(settings, _bearer_token(request), **leaderboard_kwargs)
            return public_leaderboard_page(
                settings,
                row_type=row_type,
                offset=offset,
                limit=limit,
                sort_key=sort,
                rank_scope=rank_scope,
                include_archetypes=include_archetypes,
            )
        except ServerAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/leaderboard/rows")
    def api_leaderboard_rows(
        request: Request,
        scope: str = "public",
        token: str = "",
        contributor: str = "",
        rank_scope: str = RANK_SCOPE_ALL,
        cluster: str = "on",
        row_type: str = "",
        offset: int = 0,
        limit: int = LEADERBOARD_HTML_DEFAULT_LIMIT,
        sort: str = "wilson",
    ) -> dict[str, Any]:
        cluster_enabled = _cluster_enabled(cluster)
        try:
            if scope == "public":
                payload = public_leaderboard_page(
                    settings,
                    row_type=row_type,
                    offset=offset,
                    limit=limit,
                    sort_key=sort,
                    rank_scope=rank_scope,
                    include_archetypes=cluster_enabled,
                )
                contributor_value = ""
            else:
                payload, _, _, contributor_value, _ = _leaderboard_payload_for_web_request(
                    settings,
                    request,
                    scope=scope,
                    token=token,
                    contributor=contributor,
                    rank_scope=rank_scope,
                    cluster_enabled=cluster_enabled,
                    service_limit=LEADERBOARD_SNAPSHOT_LIMIT,
                )
        except ServerAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _leaderboard_rows_response(
            payload,
            cluster_enabled=cluster_enabled,
            contributor_name=contributor_value,
            offset=offset,
            limit=limit,
            sort_key=sort,
        )

    @app.get("/downloads/{filename}")
    def download_client_update(filename: str):
        try:
            path, manifest = resolve_client_update_file(settings)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        allowed_names = {
            str(manifest.get("download_name") or ""),
            str(manifest.get("stored_filename") or ""),
            CLIENT_EXE_LEGACY_DOWNLOAD_NAME,
        }
        if filename not in allowed_names:
            raise HTTPException(status_code=404, detail="client update file not found")
        return FileResponse(
            path,
            media_type="application/vnd.microsoft.portable-executable",
            filename=str(manifest.get("download_name") or path.name),
        )

    @app.get("/web/static/{filename}")
    def web_static(filename: str):
        if filename not in LEADERBOARD_STATIC_FILES:
            raise HTTPException(status_code=404, detail="static file not found")
        path = WEB_STATIC_ROOT / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="static file not found")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return _page(
            "英杰大战环境数据库",
            "<p>服务正在运行。打开 <a href=\"/leaderboard\">公开聚合榜</a>，或在 <a href=\"/me\">我的上传</a> 输入 token 查看自己的上传记录。</p>",
        )

    @app.get("/me", response_class=HTMLResponse)
    def me(token: str = "") -> HTMLResponse:
        if not token:
            return HTMLResponse(
                _page(
                    "我的上传",
                    """
                <form method="get" action="/me">
                  <label>API token <input name="token" type="password" autocomplete="off"></label>
                  <button type="submit">查看</button>
                </form>
                """,
                )
            )
        try:
            payload = list_my_uploads(settings, token)
        except ServerAuthError as exc:
            return HTMLResponse(_page("我的上传", f"<p>{html.escape(str(exc))}</p>"))
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(item['created_at']))}</td>"
            f"<td>{html.escape(str(item['target_version']))}</td>"
            f"<td>{html.escape(str(item['date_from']))} 至 {html.escape(str(item['date_to']))}</td>"
            f"<td>{html.escape(str(item['status']))}</td>"
            f"<td>{int(item['match_count'])}</td>"
            f"<td>{int(item['imported_match_count'])}</td>"
            "</tr>"
            for item in payload["uploads"]
        )
        empty_upload_rows = '<tr><td colspan="6">还没有上传记录</td></tr>'
        contributor_link = f"/leaderboard?scope=contributor&contributor={quote(str(payload['contributor_name']))}"
        body = (
            f"<p>用户：{html.escape(payload['contributor_name'])}</p>"
            f'<p><a href="{contributor_link}">查看我的贡献榜</a></p>'
            "<table><thead><tr><th>时间</th><th>版本</th><th>日期</th><th>状态</th><th>包内对局</th><th>导入对局</th></tr></thead>"
            f"<tbody>{rows or empty_upload_rows}</tbody></table>"
        )
        response = HTMLResponse(_page("我的上传", body))
        response.set_cookie("eiketsu_contributor_name", str(payload["contributor_name"]), httponly=True, samesite="lax")
        response.delete_cookie("eiketsu_user_token")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request, token: str = "", status: str = "all") -> HTMLResponse:
        return _admin_invites_response(settings, request, query_token=token, status=status)

    @app.get("/admin/invites", response_class=HTMLResponse)
    def admin_invites(request: Request, token: str = "", status: str = "all") -> HTMLResponse:
        return _admin_invites_response(settings, request, query_token=token, status=status)

    @app.get("/admin/updates", response_class=HTMLResponse)
    def admin_updates(request: Request, token: str = "") -> HTMLResponse:
        return _admin_updates_response(settings, request, query_token=token)

    @app.post("/admin/invites/login", response_class=HTMLResponse)
    async def admin_invites_login(request: Request) -> HTMLResponse:
        form = _parse_urlencoded_form(await request.body())
        token = form.get("admin_token", "")
        if not _admin_authorized(settings, token):
            return HTMLResponse(_admin_login_page(settings, "管理口令不正确。"), status_code=401)
        response = _admin_invites_response(settings, request, query_token=token, notice="已进入邀请码管理页。")
        response.set_cookie("eiketsu_admin_token", token, httponly=True, samesite="lax")
        return response

    @app.post("/admin/invites/create", response_class=HTMLResponse)
    async def admin_invites_create(request: Request) -> HTMLResponse:
        form = _parse_urlencoded_form(await request.body())
        token = _admin_token_from_request(settings, request, form)
        if not _admin_authorized(settings, token):
            return HTMLResponse(_admin_login_page(settings, "请先输入管理口令。"), status_code=401)
        label = form.get("label", "").strip()
        code = form.get("code", "").strip()
        status = form.get("status", "all")
        try:
            result = create_invite(settings, label, code=code)
            notice = f"已创建邀请码：{result.code}"
            error = ""
        except ValueError as exc:
            notice = ""
            error = str(exc)
        response = _admin_invites_response(settings, request, query_token=token, status=status, notice=notice, error=error)
        response.set_cookie("eiketsu_admin_token", token, httponly=True, samesite="lax")
        return response

    @app.post("/admin/invites/logout", response_class=HTMLResponse)
    def admin_invites_logout() -> HTMLResponse:
        response = HTMLResponse(_admin_login_page(settings, "已退出管理页。"))
        response.delete_cookie("eiketsu_admin_token")
        return response

    @app.post("/leaderboard/filter")
    async def leaderboard_filter(request: Request) -> RedirectResponse:
        form = _parse_urlencoded_form(await request.body())
        contributor = form.get("contributor", "").strip()
        response = RedirectResponse("/leaderboard?scope=contributor", status_code=303)
        if contributor:
            response.set_cookie("eiketsu_contributor_name", contributor, httponly=True, samesite="lax")
        else:
            response.delete_cookie("eiketsu_contributor_name")
        response.delete_cookie("eiketsu_user_token")
        return response

    @app.post("/leaderboard/filter/clear")
    def leaderboard_filter_clear() -> RedirectResponse:
        response = RedirectResponse("/leaderboard", status_code=303)
        response.delete_cookie("eiketsu_user_token")
        response.delete_cookie("eiketsu_contributor_name")
        return response

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard(
        request: Request,
        background_tasks: BackgroundTasks,
        scope: str = "public",
        token: str = "",
        contributor: str = "",
        cluster: str = "on",
        rank_scope: str = RANK_SCOPE_ALL,
        limit: int | None = None,
        full: str = "",
    ) -> HTMLResponse:
        cluster_enabled = _cluster_enabled(cluster)
        public_scope = scope not in {"mine", "contributor"}
        display_limit = _leaderboard_display_limit(limit, "" if public_scope else full)
        try:
            if public_scope:
                payload = public_leaderboard_page(
                    settings,
                    limit=display_limit,
                    rank_scope=rank_scope,
                    include_archetypes=cluster_enabled,
                )
                if payload.get("leaderboard_status") != "ready":
                    background_tasks.add_task(refresh_public_leaderboard_snapshots, settings)
                personal_requested = False
                token_value = _user_token_from_request(request, token)
                contributor_value = _contributor_from_request(request, contributor)
                filter_error = ""
            else:
                payload, personal_requested, token_value, contributor_value, filter_error = _leaderboard_payload_for_web_request(
                    settings,
                    request,
                    scope=scope,
                    token=token,
                    contributor=contributor,
                    rank_scope=rank_scope,
                    cluster_enabled=cluster_enabled,
                    service_limit=_leaderboard_page_service_limit(display_limit, full),
                )
                if payload.get("scope") == "public" and payload.get("leaderboard_status") != "ready":
                    background_tasks.add_task(refresh_public_leaderboard_snapshots, settings)
        except ServerAuthError as exc:
            fallback_limit = _leaderboard_page_service_limit(display_limit, full)
            payload = public_leaderboard_page(
                settings,
                limit=fallback_limit,
                rank_scope=rank_scope,
                include_archetypes=cluster_enabled,
            )
            if payload.get("leaderboard_status") != "ready":
                background_tasks.add_task(refresh_public_leaderboard_snapshots, settings)
            personal_requested = scope in {"mine", "contributor"}
            token_value = _user_token_from_request(request, token)
            contributor_value = _contributor_from_request(request, contributor)
            filter_error = "旧版 token 链接不可用；请改用绑定用户名查看贡献。" if scope == "mine" else str(exc)
        except ValueError as exc:
            return HTMLResponse(_page("公开聚合榜", f"<p>{html.escape(str(exc))}</p>"))
        response = HTMLResponse(
            _leaderboard_visual_page(
                payload,
                personal_requested=personal_requested,
                filter_error=filter_error,
                contributor_name=contributor_value,
                cluster_enabled=cluster_enabled,
                display_limit=display_limit,
            )
        )
        if token:
            response.set_cookie("eiketsu_user_token", token_value, httponly=True, samesite="lax")
        if contributor:
            response.set_cookie("eiketsu_contributor_name", contributor_value, httponly=True, samesite="lax")
            response.delete_cookie("eiketsu_user_token")
        return response

    return app


def _bearer_token(request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _user_token_from_request(request, query_token: str = "") -> str:
    return str(query_token or request.cookies.get("eiketsu_user_token") or "").strip()


def _contributor_from_request(request, query_contributor: str = "") -> str:
    return str(query_contributor or request.cookies.get("eiketsu_contributor_name") or "").strip()


def _cluster_enabled(value: str) -> bool:
    return str(value or "on").strip().lower() not in {"0", "false", "off", "deck", "none"}


def _leaderboard_page_service_limit(display_limit: int | None, full: str = "") -> int | None:
    if display_limit is None or str(full or "").strip().lower() in {"1", "true", "yes", "all"}:
        return None
    return LEADERBOARD_SNAPSHOT_LIMIT


def _leaderboard_service_limit(limit: int | None, full: str = "") -> int | None:
    if str(full or "").strip().lower() in {"1", "true", "yes", "all"}:
        return None
    if limit is None:
        return LEADERBOARD_SNAPSHOT_LIMIT
    return max(1, min(int(limit), LEADERBOARD_SNAPSHOT_LIMIT))


def _leaderboard_payload_for_web_request(
    settings: Settings,
    request: Any,
    *,
    scope: str,
    token: str,
    contributor: str,
    rank_scope: str,
    cluster_enabled: bool,
    service_limit: int | None,
) -> tuple[dict[str, Any], bool, str, str, str]:
    personal_requested = scope in {"mine", "contributor"}
    token_value = _user_token_from_request(request, token)
    contributor_value = _contributor_from_request(request, contributor)
    leaderboard_kwargs = {
        "limit": service_limit,
        "archetype_limit": service_limit,
        "rank_scope": rank_scope,
        "include_archetypes": cluster_enabled,
    }
    filter_error = ""
    if scope == "contributor":
        if contributor_value:
            payload = contributor_leaderboard(
                settings,
                contributor_value,
                **leaderboard_kwargs,
            )
            if not payload.get("contributor_found"):
                filter_error = f"还没有找到用户名“{contributor_value}”的上传记录。"
        else:
            payload = public_leaderboard_page(
                settings,
                limit=service_limit,
                rank_scope=rank_scope,
                include_archetypes=cluster_enabled,
            )
            filter_error = "请输入绑定用户名后查看我的贡献视角。"
    elif scope == "mine":
        if token_value:
            payload = personal_leaderboard(
                settings,
                token_value,
                **leaderboard_kwargs,
            )
        else:
            payload = public_leaderboard_page(
                settings,
                limit=service_limit,
                rank_scope=rank_scope,
                include_archetypes=cluster_enabled,
            )
            filter_error = "旧版 token 链接缺少 token；请改用绑定用户名查看贡献。"
    else:
        payload = public_leaderboard_page(
            settings,
            limit=service_limit,
            rank_scope=rank_scope,
            include_archetypes=cluster_enabled,
        )
    return payload, personal_requested, token_value, contributor_value, filter_error


def _parse_urlencoded_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}


def _admin_authorized(settings: Settings, token: str) -> bool:
    configured = str(settings.admin_token or "").strip()
    candidate = str(token or "").strip()
    return bool(configured and candidate and secrets.compare_digest(candidate, configured))


def _admin_token_from_request(
    settings: Settings,
    request: Request,
    form: dict[str, str] | None = None,
    query_token: str = "",
) -> str:
    if query_token:
        return query_token
    if form and form.get("admin_token"):
        return form["admin_token"]
    return str(request.cookies.get("eiketsu_admin_token") or "")


def _admin_invites_response(
    settings: Settings,
    request: Request,
    query_token: str = "",
    status: str = "all",
    notice: str = "",
    error: str = "",
) -> HTMLResponse:
    token = _admin_token_from_request(settings, request, query_token=query_token)
    if not settings.admin_token.strip():
        return HTMLResponse(_admin_setup_page(), status_code=503)
    if not _admin_authorized(settings, token):
        status_code = 401 if token else 200
        return HTMLResponse(_admin_login_page(settings, error), status_code=status_code)

    payload = list_invites(settings, status=status, limit=200)
    response = HTMLResponse(_admin_shell("邀请码管理", _admin_invites_body(payload, notice=notice, error=error)))
    if query_token:
        response.set_cookie("eiketsu_admin_token", token, httponly=True, samesite="lax")
    return response


def _admin_updates_response(
    settings: Settings,
    request: Request,
    query_token: str = "",
) -> HTMLResponse:
    token = _admin_token_from_request(settings, request, query_token=query_token)
    if not settings.admin_token.strip():
        return HTMLResponse(_admin_setup_page(), status_code=503)
    if not _admin_authorized(settings, token):
        status_code = 401 if token else 200
        return HTMLResponse(_admin_login_page(settings), status_code=status_code)
    try:
        manifest = load_client_update_manifest(settings)
        body = _admin_updates_body(manifest, error="")
    except ValueError as exc:
        body = _admin_updates_body(None, error=str(exc))
    response = HTMLResponse(_admin_shell("客户端更新包", body))
    if query_token:
        response.set_cookie("eiketsu_admin_token", token, httponly=True, samesite="lax")
    return response


def _admin_setup_page() -> str:
    return _admin_shell(
        "邀请码管理",
        """
        <section class="notice error">
          <strong>还没有配置管理口令。</strong>
          <span>请在 VPS 的环境变量里设置 EIKETSU_ADMIN_TOKEN，然后重启服务。</span>
        </section>
        """,
    )


def _admin_login_page(settings: Settings, error: str = "") -> str:
    if not settings.admin_token.strip():
        return _admin_setup_page()
    error_html = f'<section class="notice error">{html.escape(error)}</section>' if error else ""
    return _admin_shell(
        "邀请码管理",
        f"""
        {error_html}
        <section class="admin-login">
          <h2>输入管理口令</h2>
          <form method="post" action="/admin/invites/login">
            <label>
              管理口令
              <input name="admin_token" type="password" autocomplete="current-password" required>
            </label>
            <button type="submit">进入管理页</button>
          </form>
        </section>
        """,
    )


def _admin_invites_body(payload: dict[str, Any], notice: str = "", error: str = "") -> str:
    rows = "".join(_admin_invite_row(index, item) for index, item in enumerate(payload["items"]))
    if not rows:
        rows = '<tr><td colspan="6" class="empty">当前筛选下没有邀请码。</td></tr>'
    notice_html = f'<section class="notice ok">{html.escape(notice)}</section>' if notice else ""
    error_html = f'<section class="notice error">{html.escape(error)}</section>' if error else ""
    status = html.escape(str(payload.get("status") or "all"))
    counts = payload.get("counts") or {}
    return f"""
      {notice_html}
      {error_html}
      <section class="admin-summary" aria-label="邀请码统计">
        <div><span>全部</span><strong>{int(counts.get("all") or 0)}</strong></div>
        <div><span>可用</span><strong>{int(counts.get("active") or 0)}</strong></div>
        <div><span>已使用</span><strong>{int(counts.get("used") or 0)}</strong></div>
      </section>

      <section class="admin-actions">
        <form method="post" action="/admin/invites/create" class="create-invite">
          <input type="hidden" name="status" value="{status}">
          <label>
            备注
            <input name="label" placeholder="例如：朋友昵称 / 测试机" autocomplete="off">
          </label>
          <label>
            自定义邀请码
            <input name="code" placeholder="可留空自动生成" autocomplete="off">
          </label>
          <button type="submit">创建邀请码</button>
        </form>
      </section>

      <section class="admin-table-head">
        <div class="filters">
          {_admin_filter_link("全部", "all", status)}
          {_admin_filter_link("可用", "active", status)}
          {_admin_filter_link("已使用", "used", status)}
        </div>
        <form method="post" action="/admin/invites/logout">
          <button type="submit" class="ghost">退出</button>
        </form>
      </section>

      <table class="admin-table">
        <thead>
          <tr>
            <th>邀请码</th>
            <th>备注</th>
            <th>状态</th>
            <th>创建时间</th>
            <th>使用时间</th>
            <th>绑定用户</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <script>
        document.querySelectorAll('[data-copy]').forEach((button) => {{
          button.addEventListener('click', async () => {{
            const text = button.getAttribute('data-copy') || '';
            await navigator.clipboard.writeText(text);
            button.textContent = '已复制';
            setTimeout(() => button.textContent = '复制', 1200);
          }});
        }});
      </script>
    """


def _admin_updates_body(manifest: dict[str, Any] | None, error: str = "") -> str:
    error_html = f'<section class="notice error">{html.escape(error)}</section>' if error else ""
    if not manifest:
        current = """
        <section class="notice">
          <strong>还没有发布客户端更新包。</strong>
          <span>发布后，客户端启动时会自动检查新版本，并提示用户下载。</span>
        </section>
        """
    else:
        size_mb = int(manifest.get("size_bytes") or 0) / 1024 / 1024
        download_path = str(manifest.get("download_path") or "")
        if not download_path:
            download_name = str(manifest.get("download_name") or manifest.get("stored_filename") or CLIENT_EXE_LEGACY_DOWNLOAD_NAME)
            download_path = f"/downloads/{download_name}"
        download_path_html = html.escape(download_path, quote=True)
        current = f"""
        <section class="admin-summary update-summary" aria-label="客户端更新包">
          <div><span>最新版</span><strong>{html.escape(str(manifest.get("latest_version") or ""))}</strong></div>
          <div><span>文件大小</span><strong>{size_mb:.1f} MB</strong></div>
          <div><span>发布时间</span><strong>{html.escape(str(manifest.get("published_at") or ""))}</strong></div>
        </section>
        <section class="admin-actions">
          <p><strong>下载地址：</strong><a href="{download_path_html}">{download_path_html}</a></p>
          <p><strong>SHA256：</strong><code>{html.escape(str(manifest.get("sha256") or ""))}</code></p>
          <p><strong>说明：</strong>{html.escape(str(manifest.get("notes") or "无"))}</p>
        </section>
        """
    return f"""
      {error_html}
      {current}
      <section class="admin-actions">
        <h2>发布新版客户端</h2>
        <p>把新打包的 exe 放到 VPS 后运行下面的管理命令。发布成功后，不需要逐个把文件发给朋友；他们打开旧客户端时会看到新版提示。</p>
        <pre><code>docker compose -f deploy/docker-compose.yml run --rm -v /tmp/EiketsuCollector_0.1.8.exe:/tmp/EiketsuCollector_0.1.8.exe:ro api eiketsu-server admin publish-client --version 0.1.8 --file /tmp/EiketsuCollector_0.1.8.exe --notes "更新说明"</code></pre>
      </section>
    """


def _admin_invite_row(index: int, item: dict[str, Any]) -> str:
    code = str(item.get("code") or "")
    status = str(item.get("status") or "")
    status_label = "可用" if status == "active" else "已使用" if status == "used" else status
    return (
        "<tr>"
        f'<td><code id="invite-{index}">{html.escape(code)}</code><button type="button" class="copy" data-copy="{html.escape(code, quote=True)}">复制</button></td>'
        f"<td>{html.escape(str(item.get('label') or ''))}</td>"
        f'<td><span class="status {html.escape(status)}">{html.escape(status_label)}</span></td>'
        f"<td>{html.escape(str(item.get('created_at') or ''))}</td>"
        f"<td>{html.escape(str(item.get('used_at') or ''))}</td>"
        f"<td>{html.escape(str(item.get('used_by') or ''))}</td>"
        "</tr>"
    )


def _admin_filter_link(label: str, value: str, current: str) -> str:
    active = " active" if value == current else ""
    return f'<a class="filter{active}" href="/admin/invites?status={html.escape(value)}">{html.escape(label)}</a>'


def _admin_shell(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{html.escape(title)}</title>
        <style>
          :root {{ color-scheme: light; --ink: #17202a; --muted: #667085; --line: #d8dee8; --bg: #f6f7f9; --surface: #ffffff; --accent: #1f6feb; --ok: #16833a; --warn: #a65f00; --danger: #ba1a1a; }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: "Microsoft YaHei UI", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); }}
          .admin-page {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 44px; }}
          .admin-top {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; margin-bottom: 22px; }}
          .admin-top h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
          .admin-top p {{ margin: 6px 0 0; color: var(--muted); font-size: 13px; }}
          .admin-top nav {{ display: flex; gap: 14px; font-size: 13px; }}
          .admin-top a {{ color: var(--accent); font-weight: 700; text-decoration: none; }}
          .admin-summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; border: 1px solid var(--line); background: var(--line); margin-bottom: 18px; }}
          .admin-summary div {{ background: var(--surface); padding: 15px 16px; }}
          .admin-summary span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
          .admin-summary strong {{ font-size: 24px; }}
          .admin-actions {{ border: 1px solid var(--line); background: var(--surface); padding: 16px; margin-bottom: 16px; }}
          .create-invite, .admin-login form, .admin-table-head {{ display: flex; align-items: end; gap: 12px; flex-wrap: wrap; }}
          label {{ display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 700; }}
          input {{ min-width: 260px; height: 38px; border: 1px solid #c8d0dc; border-radius: 4px; padding: 0 10px; font: inherit; background: #fff; }}
          button, .filter {{ height: 38px; border: 1px solid var(--accent); border-radius: 4px; padding: 0 13px; color: #fff; background: var(--accent); font: inherit; font-weight: 700; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; }}
          button.ghost, .filter {{ color: var(--ink); background: #fff; border-color: var(--line); }}
          .filter.active {{ color: #fff; background: var(--ink); border-color: var(--ink); }}
          .admin-table-head {{ justify-content: space-between; margin: 14px 0 8px; }}
          .filters {{ display: flex; gap: 8px; }}
          .admin-table {{ width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--line); }}
          th, td {{ border-bottom: 1px solid var(--line); padding: 10px 11px; text-align: left; font-size: 13px; vertical-align: middle; }}
          th {{ color: var(--muted); font-size: 12px; background: #eef2f6; }}
          code {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 13px; }}
          .copy {{ height: 28px; margin-left: 8px; padding: 0 9px; font-size: 12px; color: var(--ink); background: #fff; border-color: var(--line); }}
          .status {{ display: inline-flex; align-items: center; min-width: 56px; justify-content: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 800; }}
          .status.active {{ color: var(--ok); background: #e8f5ec; }}
          .status.used {{ color: var(--warn); background: #fff4df; }}
          .notice {{ border: 1px solid var(--line); background: #fff; padding: 12px 14px; margin-bottom: 14px; font-size: 14px; }}
          .notice.ok {{ border-color: #b8dec5; color: var(--ok); background: #f0faf3; }}
          .notice.error {{ border-color: #efb7b7; color: var(--danger); background: #fff3f3; }}
          .admin-login {{ max-width: 560px; border: 1px solid var(--line); background: var(--surface); padding: 18px; }}
          .admin-login h2 {{ margin: 0 0 14px; font-size: 18px; }}
          .empty {{ color: var(--muted); text-align: center; padding: 24px; }}
          @media (max-width: 760px) {{
            .admin-top {{ align-items: flex-start; flex-direction: column; }}
            .admin-summary {{ grid-template-columns: 1fr; }}
            input {{ min-width: 100%; }}
            .create-invite, .admin-login form {{ align-items: stretch; flex-direction: column; }}
            button, .filter {{ width: 100%; }}
            .admin-table {{ display: block; overflow-x: auto; white-space: nowrap; }}
          }}
        </style>
      </head>
      <body>
        <main class="admin-page">
          <header class="admin-top">
            <div>
              <h1>{html.escape(title)}</h1>
              <p>创建一次性邀请码，查看谁已经绑定。公开页面不会展示这些备注和昵称。</p>
            </div>
            <nav>
              <a href="/admin/updates">客户端更新</a>
              <a href="/admin/invites">邀请码</a>
              <a href="/leaderboard">公开榜</a>
              <a href="/health">健康检查</a>
            </nav>
          </header>
          {body}
        </main>
      </body>
    </html>
    """


def _page(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{html.escape(title)}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px auto; max-width: 1080px; padding: 0 20px; color: #18202a; background: #f7f8fa; }}
          a {{ color: #1b62a8; }}
          table {{ border-collapse: collapse; width: 100%; background: white; margin: 16px 0 28px; }}
          th, td {{ border: 1px solid #d8dee8; padding: 8px 10px; text-align: left; font-size: 14px; }}
          th {{ background: #eef2f6; }}
          input {{ padding: 8px 10px; min-width: 360px; }}
          button {{ padding: 8px 14px; margin-left: 8px; }}
        </style>
      </head>
      <body>
        <h1>{html.escape(title)}</h1>
        {body}
      </body>
    </html>
    """




def _load_app():
    try:
        return create_app()
    except RuntimeError:
        return None


app = _load_app()
