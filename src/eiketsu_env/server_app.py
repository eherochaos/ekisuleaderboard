"""FastAPI VPS 应用：接收客户端上传并提供即时查看页面。"""

from __future__ import annotations

import html
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlencode

from eiketsu_env import __version__
from eiketsu_env.config import Settings, load_settings
from eiketsu_env.db.migrations import upgrade_database
from eiketsu_env.services.client_update import (
    CLIENT_EXE_LEGACY_DOWNLOAD_NAME,
    client_update_payload,
    load_client_update_manifest,
    resolve_client_update_file,
)
from eiketsu_env.services.server_share import (
    RANK_SCOPE_ALL,
    RANK_SCOPE_KNIGHT_DOWN,
    RANK_SCOPE_KNIGHT_UP,
    RANK_SCOPE_LABELS,
    RANK_SCOPE_TRAVELER_DOWN,
    ServerAuthError,
    bind_invite,
    contributor_leaderboard,
    create_invite,
    get_server_config,
    import_uploaded_package,
    list_invites,
    list_my_uploads,
    personal_leaderboard,
    public_leaderboard,
    refresh_public_leaderboard_snapshots,
)
from eiketsu_env.services.analysis import (
    _deck_archetype_script,
    _deck_archetype_visual_css,
    _deck_visual_css,
    _visual_sort_script,
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
    ) -> dict[str, Any]:
        include_archetypes = _cluster_enabled(cluster)
        try:
            if scope == "contributor":
                return contributor_leaderboard(settings, contributor, rank_scope=rank_scope, include_archetypes=include_archetypes)
            if scope == "mine":
                return personal_leaderboard(settings, _bearer_token(request), rank_scope=rank_scope, include_archetypes=include_archetypes)
            return public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=include_archetypes)
        except ServerAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        scope: str = "public",
        token: str = "",
        contributor: str = "",
        cluster: str = "on",
        rank_scope: str = RANK_SCOPE_ALL,
    ) -> HTMLResponse:
        personal_requested = scope in {"mine", "contributor"}
        token_value = _user_token_from_request(request, token)
        contributor_value = _contributor_from_request(request, contributor)
        cluster_enabled = _cluster_enabled(cluster)
        filter_error = ""
        try:
            if scope == "contributor":
                if contributor_value:
                    payload = contributor_leaderboard(
                        settings,
                        contributor_value,
                        rank_scope=rank_scope,
                        include_archetypes=cluster_enabled,
                    )
                    if not payload.get("contributor_found"):
                        filter_error = f"还没有找到用户名“{contributor_value}”的上传记录。"
                else:
                    payload = public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=cluster_enabled)
                    filter_error = "请输入绑定用户名后查看我的贡献视角。"
            elif scope == "mine":
                if token_value:
                    payload = personal_leaderboard(
                        settings,
                        token_value,
                        rank_scope=rank_scope,
                        include_archetypes=cluster_enabled,
                    )
                else:
                    payload = public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=cluster_enabled)
                    filter_error = "旧版 token 链接缺少 token；请改用绑定用户名查看贡献。"
            else:
                payload = public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=cluster_enabled)
        except ServerAuthError as exc:
            payload = public_leaderboard(settings, rank_scope=rank_scope, include_archetypes=cluster_enabled)
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
        <pre><code>docker compose run --rm -v /tmp/EiketsuCollector_0.1.8.exe:/tmp/EiketsuCollector_0.1.8.exe:ro api eiketsu-server admin publish-client --version 0.1.8 --file /tmp/EiketsuCollector_0.1.8.exe --notes "更新说明"</code></pre>
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


def _leaderboard_visual_page(
    payload: dict[str, Any],
    *,
    personal_requested: bool = False,
    filter_error: str = "",
    contributor_name: str = "",
    cluster_enabled: bool = True,
) -> str:
    archetypes = list(payload.get("top_archetypes") or [])
    decks = list(payload.get("top_decks") or [])
    is_archetype_view = bool(cluster_enabled and archetypes)
    is_personal_view = payload.get("scope") in {"mine", "contributor"}
    css = _deck_archetype_visual_css() if is_archetype_view else _deck_visual_css()
    sort_target = "archetype-ranking" if is_archetype_view else "deck-ranking"
    board = _archetype_ranking_board(archetypes) if is_archetype_view else _ranking_board(decks)
    extra_script = _deck_archetype_script() if is_archetype_view else ""
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
    return f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>英杰大战环境卡组榜</title>
          <style>
            {css}
          .leaderboard-topbar {{ align-items: center; display: flex; gap: 16px; justify-content: space-between; margin-bottom: 16px; }}
          .site-nav {{ margin: 0; }}
          .site-nav a {{ color: var(--accent); font-size: 13px; font-weight: 800; text-decoration: none; }}
          .site-nav a:hover {{ text-decoration: underline; }}
          .leaderboard-note {{ color: var(--muted); font-size: 13px; margin: 0 0 16px; }}
          .scope-tools {{ align-items: center; display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }}
          .scope-tools form {{ align-items: center; display: flex; gap: 8px; margin: 0; }}
          .scope-tools input {{ background: var(--panel); border: 1px solid var(--line); color: var(--ink); font: inherit; font-size: 13px; height: 32px; padding: 0 10px; width: 220px; }}
          .scope-tools button, .scope-tools a, .scope-mobile-drawer summary {{ align-items: center; background: transparent; border: 1px solid var(--line); color: var(--ink); cursor: pointer; display: inline-flex; font: inherit; font-size: 13px; font-weight: 800; height: 32px; padding: 0 12px; text-decoration: none; }}
          .scope-tools .primary {{ background: var(--ink); border-color: var(--ink); color: var(--panel); }}
          .scope-chip {{ border: 1px solid var(--line); color: var(--muted); font-size: 12px; font-weight: 800; height: 32px; line-height: 30px; padding: 0 10px; }}
          .scope-error {{ color: #b42318; font-size: 13px; margin: -8px 0 14px; text-align: right; }}
          .scope-mobile-drawer {{ display: none; position: relative; }}
          .scope-mobile-drawer summary {{ list-style: none; }}
          .scope-mobile-drawer summary::-webkit-details-marker {{ display: none; }}
          .view-controls {{ align-items: center; display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 0 0 14px; }}
          .view-control-group {{ align-items: center; display: flex; flex-wrap: wrap; gap: 7px; }}
          .view-control-label {{ color: var(--muted); font-size: 12px; font-weight: 800; }}
          .view-control-link {{ border: 1px solid #cfd6e1; border-radius: 999px; background: var(--panel); color: #2c3745; font-size: 12px; font-weight: 800; line-height: 1; padding: 8px 11px; text-decoration: none; }}
          .view-control-link.is-active {{ background: var(--ink); border-color: var(--ink); color: var(--panel); }}
          .view-control-link:hover {{ border-color: var(--accent); }}
          .deck-owner {{ color: var(--muted); font-size: 12px; font-weight: 700; margin: 5px 0 8px; overflow-wrap: anywhere; }}
          .leaderboard-mobile-summary {{ display: none; }}
          @media (max-width: 720px) {{
            body {{ background: var(--paper); }}
            .page {{ width: min(100% - 18px, 1280px); padding-top: 10px; }}
            .leaderboard-topbar {{ align-items: center; flex-direction: row; gap: 8px; margin-bottom: 10px; }}
            .site-nav a {{ font-size: 12px; }}
            .scope-tools {{ flex: 1; justify-content: flex-end; min-width: 0; }}
            .scope-form-desktop {{ display: none !important; }}
            .scope-mobile-drawer {{ display: block; }}
            .scope-mobile-drawer[open] form {{ background: var(--panel); border: 1px solid var(--line); box-shadow: 0 14px 32px rgba(23, 29, 37, 0.16); display: grid; gap: 8px; padding: 10px; position: absolute; right: 0; top: 38px; width: min(86vw, 320px); z-index: 20; }}
            .scope-mobile-drawer input {{ width: 100%; }}
            .scope-mobile-drawer button {{ justify-content: center; margin: 0; width: 100%; }}
            .scope-chip {{ max-width: 58vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .scope-error {{ margin: -4px 0 8px; text-align: left; }}
            .report-header {{ border-bottom-width: 1px; margin-bottom: 8px; padding-bottom: 8px; }}
            .title-block p {{ display: none; }}
            h1 {{ font-size: 25px; line-height: 1.15; }}
            .summary {{ display: none; }}
            .leaderboard-mobile-summary {{ color: var(--muted); display: flex; flex-wrap: wrap; font-size: 13px; font-weight: 800; gap: 4px 10px; margin: 8px 0 0; }}
            .leaderboard-note {{ display: none; }}
            .view-controls {{ gap: 6px 10px; margin: 8px 0 10px; }}
            .view-control-group {{ gap: 6px; }}
            .view-control-link {{ padding: 7px 9px; }}
            .deck-owner {{ font-size: 12px; margin: 4px 0 7px; }}
            .sort-toolbar {{ gap: 6px; margin: 8px 0 10px; }}
            .sort-toolbar button {{ padding: 7px 10px; }}
            .archetype-board, .ranking-board {{ border-radius: 6px; }}
            .archetype-row, .rank-row {{ gap: 8px; padding: 10px; }}
            .row-rank strong {{ font-size: 26px; }}
            .row-rank span {{ font-size: 12px; }}
            h3 {{ font-size: 16px; line-height: 1.3; }}
            .variant-viewer {{ margin: 8px 0 0; }}
            .variant-toolbar {{ margin-bottom: 8px; }}
            .variant-name, .variant-statline {{ display: none; }}
          }}
        </style>
      </head>
      <body>
        <main class="page">
          <div class="leaderboard-topbar">
            <nav class="site-nav"><a href="/">← 返回首页</a></nav>
            {_leaderboard_scope_tools(personal_requested, is_personal_view, contributor_name or str(payload.get("contributor_name") or ""))}
          </div>
          {_leaderboard_filter_error(filter_error)}
          <header class="report-header">
            <div class="title-block">
              <p>{eyebrow}</p>
              <h1>{title}</h1>
            </div>
            <dl class="summary">
              {_summary_item("目标版本", payload.get("target_version", ""))}
              {_summary_item("采集日期", f"{payload.get('date_from', '')} 至 {payload.get('date_to', '')}")}
              {_summary_item(upload_label, payload.get("upload_count", 0))}
              {_summary_item("对局数", payload.get("match_count", 0))}
              {_summary_item("双方样本", payload.get("side_sample_count", 0))}
              {_summary_item("生成时间", payload.get("generated_at", ""))}
            </dl>
            <p class="leaderboard-mobile-summary">{mobile_summary}</p>
          </header>
          <p class="leaderboard-note">{privacy_note}</p>
          {view_controls}
          {"" if is_archetype_view else _feature_grid(decks[:3])}
          <div class="sort-toolbar" data-sort-toolbar data-sort-target="{sort_target}">
            <span>排序</span>
            <button type="button" data-sort-button data-sort-key="wilson" aria-pressed="true">Wilson 下限</button>
            <button type="button" data-sort-button data-sort-key="sample" aria-pressed="false">样本数</button>
          </div>
          {board}
        </main>
        {extra_script}
        {_visual_sort_script()}
      </body>
    </html>
    """


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
                    '<div class="feature-stats">',
                    _score_pill("Wilson", _fmt_rate(archetype.get("wilson_lower_bound"))),
                    _score_pill("胜率", _fmt_rate(archetype.get("win_rate"))),
                    _score_pill("样本", archetype.get("sample_count", 0)),
                    _score_pill("构筑", archetype.get("member_count", 0)),
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
    rows = "".join(_archetype_rank_row(index, archetype) for index, archetype in enumerate(archetypes, start=1))
    return "\n".join(
        [
            '<section class="archetype-board" id="archetype-ranking" data-sort-root>',
            '<div class="board-head"><span>Rank</span><span>Archetype</span><span>Signal</span></div>',
            rows,
            "</section>",
        ]
    )


def _archetype_rank_row(index: int, archetype: dict[str, Any]) -> str:
    title = str(archetype.get("title") or archetype.get("archetype_id") or "")
    return "\n".join(
        [
            f'<article class="archetype-row" {_sort_item_attrs(title, archetype.get("wilson_lower_bound"), archetype.get("sample_count"))}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span>{_html(archetype.get("member_count", 0))} 个构筑</span>',
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(title)}</h3>",
            _player_summary(archetype),
            _archetype_variant_viewer(archetype),
            "</div>",
            '<div class="row-signals">',
            _score_pill("Wilson", _fmt_rate(archetype.get("wilson_lower_bound"))),
            _score_pill("胜率", _fmt_rate(archetype.get("win_rate"))),
            _score_pill("样本", archetype.get("sample_count", 0)),
            _score_pill("构筑", archetype.get("member_count", 0)),
            _score_pill("战绩", _record_label(archetype)),
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
    button = (
        '<button type="button" class="variant-button" data-variant-button>Change</button>'
        if len(variants) > 1
        else '<span class="variant-single">Single</span>'
    )
    return "\n".join(
        [
            '<div class="variant-viewer" data-variant-root>',
            '<div class="variant-toolbar">',
            '<span class="variant-label" data-variant-label>代表构筑</span>',
            button,
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
                    '<div class="feature-stats">',
                    _score_pill("Wilson", _fmt_rate(deck.get("wilson_lower_bound"))),
                    _score_pill("胜率", _fmt_rate(deck.get("win_rate"))),
                    _score_pill("样本", deck.get("sample_count", 0)),
                    "</div>",
                    "</article>",
                ]
            )
        )
    return f'<section class="feature-grid">{"".join(cards)}</section>'


def _ranking_board(decks: list[dict[str, Any]]) -> str:
    if not decks:
        return ""
    rows = "".join(_rank_row(index, deck) for index, deck in enumerate(decks, start=1))
    return "\n".join(
        [
            '<section class="ranking-board" id="deck-ranking">',
            '<div class="board-head"><span>Rank</span><span>Deck</span><span>Signals</span></div>',
            rows,
            "</section>",
        ]
    )


def _rank_row(index: int, deck: dict[str, Any]) -> str:
    title = str(deck.get("deck_name") or deck.get("deck_fingerprint") or "")
    return "\n".join(
        [
            f'<article class="rank-row" {_sort_item_attrs(title, deck.get("wilson_lower_bound"), deck.get("sample_count"))}>',
            '<div class="row-rank">',
            f'<strong data-rank-value>{index:02d}</strong>',
            f'<span>{_record_label(deck)}</span>',
            "</div>",
            '<div class="row-deck">',
            f"<h3>{_html(title)}</h3>",
            _player_summary(deck),
            f'<div class="card-strip">{_card_strip(deck.get("cards") or [])}</div>',
            "</div>",
            '<div class="row-signals">',
            _score_pill("Wilson", _fmt_rate(deck.get("wilson_lower_bound"))),
            _score_pill("胜率", _fmt_rate(deck.get("win_rate"))),
            _score_pill("样本", deck.get("sample_count", 0)),
            _score_pill("平局", deck.get("draw_count", 0)),
            "</div>",
            "</article>",
        ]
    )


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


def _score_pill(label: str, value: Any) -> str:
    text = str(value) if value not in {None, ""} else "-"
    return f'<span class="score-pill"><strong>{_html(label)}</strong>{_html(text)}</span>'


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


def _load_app():
    try:
        return create_app()
    except RuntimeError:
        return None


app = _load_app()
