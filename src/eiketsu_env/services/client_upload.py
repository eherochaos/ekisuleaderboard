"""普通用户侧的一键绑定、采集和上传流程。"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from eiketsu_env.config import Settings, version_start_date
from eiketsu_env import __version__
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from eiketsu_env.db.base import Base
from eiketsu_env.db.models import RawSnapshot
from eiketsu_env.db.session import make_engine
from eiketsu_env.services.collector import CollectResult, collect_follow
from eiketsu_env.services.progress import ProgressReporter
from eiketsu_env.services.share import ShareConfig, assert_safe_contribution_payload, export_contribution
from eiketsu_env.utils import sha256_text


CLIENT_CONFIG_FILE = "client_config.json"


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        token: str = "",
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class ClientConfig:
    server_url: str
    api_token: str
    contributor: str
    user_public_id: str = ""


@dataclass(slots=True)
class ClientBindResult:
    server_url: str
    user_public_id: str
    token_prefix: str
    config_path: Path


@dataclass(slots=True)
class ClientSyncResult:
    collect_result: CollectResult
    package_path: Path
    upload: dict[str, Any]
    viewer_url: str


@dataclass(slots=True)
class ClientShareConfigResult:
    config: ShareConfig
    current_target_version: str
    available_target_versions: list[str]


@dataclass(slots=True)
class ClientCleanupResult:
    raw_dir: Path
    files_removed: int
    bytes_removed: int
    rows_removed: int


@dataclass(slots=True)
class ClientUpdateCheck:
    configured: bool
    current_version: str
    latest_version: str
    update_available: bool
    download_url: str = ""
    download_name: str = ""
    size_bytes: int = 0
    sha256: str = ""
    notes: str = ""
    published_at: str = ""
    message: str = ""


class UrllibJsonTransport:
    def __init__(self, timeout_seconds: int = 900) -> None:
        self.timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        token: str = "",
    ) -> dict[str, Any]:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            # 真实贡献包可能有数千场，首版先让客户端等待服务端同步导入完成。
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - 用户显式配置的私有 VPS 地址。
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_error = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {raw_error}") from exc
        return json.loads(raw) if raw else {}


def bind_client(
    settings: Settings,
    server_url: str,
    invite: str,
    contributor: str,
    transport: JsonTransport | None = None,
) -> ClientBindResult:
    transport = transport or UrllibJsonTransport()
    normalized_url = _normalize_server_url(server_url)
    payload = transport.request_json(
        "POST",
        f"{normalized_url}/api/v1/auth/bind-invite",
        {"invite_code": invite, "contributor_name": contributor},
    )
    config = ClientConfig(
        server_url=normalized_url,
        api_token=str(payload["api_token"]),
        contributor=contributor,
        user_public_id=str(payload.get("user_public_id") or ""),
    )
    path = save_client_config(settings, config)
    return ClientBindResult(
        server_url=normalized_url,
        user_public_id=config.user_public_id,
        token_prefix=str(payload.get("token_prefix") or config.api_token[:8]),
        config_path=path,
    )


def sync_client(
    settings: Settings,
    auth_source: str = "",
    interactive_auth: bool = True,
    transport: JsonTransport | None = None,
    progress: ProgressReporter | None = None,
    date_from: str = "",
    date_to: str = "",
    target_version: str = "",
) -> ClientSyncResult:
    config = load_client_config(settings)
    transport = transport or UrllibJsonTransport()
    share_config = _request_share_config(config, transport, target_version=target_version)
    share_config = apply_client_date_override(share_config, date_from=date_from, date_to=date_to)

    _ensure_client_database(settings)
    if progress:
        progress.message("快速同步模式：并发采集详情，自动跳过已完整采集的旧详情")
    collect_result = collect_follow(
        settings,
        share_config.date_from,
        share_config.date_to,
        include_solo=share_config.include_solo,
        auth_source=auth_source,
        interactive_auth=interactive_auth,
        skip_existing=True,
        skip_inactive=True,
        concurrency_profile="aggressive",
        progress=progress,
        save_raw_snapshots=False,
    )
    if progress:
        progress.message("正在打包标准化贡献数据")
    package_path = _client_tmp_dir(settings) / f"{share_config.target_version}_{share_config.date_from}_{share_config.date_to}.jsonl"
    export_result = export_contribution(settings, share_config, config.contributor, package_path)
    package_text = export_result.path.read_text(encoding="utf-8")
    assert_safe_contribution_payload(package_text)
    if progress:
        progress.message("正在上传到服务器")
    upload = transport.request_json(
        "POST",
        f"{config.server_url}/api/v1/uploads",
        {"package_text": package_text, "content_hash": sha256_text(package_text)},
        token=config.api_token,
    )
    try:
        export_result.path.unlink()
    except OSError:
        pass
    return ClientSyncResult(
        collect_result=collect_result,
        package_path=export_result.path,
        upload=upload,
        viewer_url=f"{config.server_url}/me?token={urllib.parse.quote(config.api_token)}",
    )


def fetch_client_share_config(settings: Settings, transport: JsonTransport | None = None, target_version: str = "") -> ShareConfig:
    config = load_client_config(settings)
    return _request_share_config(config, transport or UrllibJsonTransport(), target_version=target_version)


def fetch_client_share_config_state(
    settings: Settings,
    transport: JsonTransport | None = None,
    target_version: str = "",
) -> ClientShareConfigResult:
    config = load_client_config(settings)
    payload = _request_share_config_payload(config, transport or UrllibJsonTransport(), target_version=target_version)
    share_config = ShareConfig.from_payload(payload)
    share_config.validate()
    available_versions = [
        version
        for version in dict.fromkeys(str(item or "").strip() for item in payload.get("available_target_versions") or [])
        if version
    ]
    if share_config.target_version and share_config.target_version not in available_versions:
        available_versions.insert(0, share_config.target_version)
    return ClientShareConfigResult(
        config=share_config,
        current_target_version=str(payload.get("current_target_version") or share_config.target_version),
        available_target_versions=available_versions,
    )


def check_client_update(
    settings: Settings,
    server_url: str = "",
    current_version: str = __version__,
    transport: JsonTransport | None = None,
) -> ClientUpdateCheck:
    server = _normalize_server_url(server_url or _configured_server_url(settings))
    transport = transport or UrllibJsonTransport(timeout_seconds=20)
    query = urllib.parse.urlencode({"current_version": current_version})
    payload = transport.request_json("GET", f"{server}/api/v1/client/update?{query}")
    return ClientUpdateCheck(
        configured=bool(payload.get("configured")),
        current_version=str(payload.get("current_version") or current_version),
        latest_version=str(payload.get("latest_version") or ""),
        update_available=bool(payload.get("update_available")),
        download_url=str(payload.get("download_url") or ""),
        download_name=str(payload.get("download_name") or ""),
        size_bytes=int(payload.get("size_bytes") or 0),
        sha256=str(payload.get("sha256") or ""),
        notes=str(payload.get("notes") or ""),
        published_at=str(payload.get("published_at") or ""),
        message=str(payload.get("message") or ""),
    )


def apply_client_date_override(config: ShareConfig, date_from: str = "", date_to: str = "") -> ShareConfig:
    # 用户可以缩小采集日期，但不能早于服务端配置的版本开始日，避免混入旧版本样本。
    effective_from = (date_from or config.date_from).strip()
    effective_to = (date_to or config.date_to).strip()
    floor = minimum_client_date_from(config)
    if effective_from < floor:
        effective_from = floor
    if effective_to > config.date_to:
        effective_to = config.date_to
    if effective_to < effective_from:
        raise ValueError(f"结束日期不能早于起始日期 {effective_from}")
    overridden = ShareConfig(
        schema_version=config.schema_version,
        target_version=config.target_version,
        date_from=effective_from,
        date_to=effective_to,
        include_solo=config.include_solo,
        high_ranker_rank=config.high_ranker_rank,
        report_formats=list(config.report_formats),
        reports=list(config.reports),
    )
    overridden.validate()
    return overridden


def minimum_client_date_from(config: ShareConfig) -> str:
    known_start = version_start_date(config.target_version)
    candidates = [item for item in (config.date_from, known_start) if item]
    return max(candidates) if candidates else config.date_from


def doctor_client(settings: Settings, transport: JsonTransport | None = None) -> dict[str, Any]:
    transport = transport or UrllibJsonTransport()
    path = client_config_path(settings)
    if not path.exists():
        return {
            "configured": False,
            "config_path": str(path),
            "message": "还没有绑定 VPS；请先运行 eiketsu-client bind",
        }
    config = load_client_config(settings)
    result: dict[str, Any] = {
        "configured": True,
        "config_path": str(path),
        "server_url": config.server_url,
        "contributor": config.contributor,
        "user_public_id": config.user_public_id,
    }
    try:
        result["server_config"] = transport.request_json("GET", f"{config.server_url}/api/v1/config")
        result["message"] = "客户端已绑定，服务端可访问"
    except Exception as exc:  # noqa: BLE001 - doctor 需要把可读诊断带回给非技术用户。
        result["message"] = f"客户端已绑定，但无法访问服务端：{exc}"
    return result


def load_client_config(settings: Settings) -> ClientConfig:
    path = client_config_path(settings)
    if not path.exists():
        raise RuntimeError("还没有绑定 VPS；请先运行 eiketsu-client bind")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ClientConfig(
        server_url=_normalize_server_url(str(payload.get("server_url") or "")),
        api_token=str(payload.get("api_token") or ""),
        contributor=str(payload.get("contributor") or ""),
        user_public_id=str(payload.get("user_public_id") or ""),
    )


def save_client_config(settings: Settings, config: ClientConfig) -> Path:
    path = client_config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "server_url": config.server_url,
                "api_token": config.api_token,
                "contributor": config.contributor,
                "user_public_id": config.user_public_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def client_config_path(settings: Settings) -> Path:
    override = os.environ.get("EIKETSU_CLIENT_CONFIG_DIR")
    if override:
        return Path(override) / CLIENT_CONFIG_FILE
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "EiketsuCollector" / CLIENT_CONFIG_FILE
    return settings.data_dir / CLIENT_CONFIG_FILE


def cleanup_raw_snapshots(settings: Settings) -> ClientCleanupResult:
    raw_dir = settings.raw_dir
    files_removed = 0
    bytes_removed = 0
    if raw_dir.exists():
        for path in raw_dir.rglob("*"):
            if path.is_file():
                files_removed += 1
                try:
                    bytes_removed += path.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(raw_dir, ignore_errors=True)

    _ensure_client_database(settings)
    engine = make_engine(settings)
    with Session(engine) as session:
        rows_removed = len(session.scalars(select(RawSnapshot.id)).all())
        session.execute(delete(RawSnapshot))
        session.commit()

    return ClientCleanupResult(
        raw_dir=raw_dir,
        files_removed=files_removed,
        bytes_removed=bytes_removed,
        rows_removed=rows_removed,
    )


def _request_share_config(config: ClientConfig, transport: JsonTransport, target_version: str = "") -> ShareConfig:
    remote_config = _request_share_config_payload(config, transport, target_version=target_version)
    share_config = ShareConfig.from_payload(remote_config)
    share_config.validate()
    return share_config


def _request_share_config_payload(config: ClientConfig, transport: JsonTransport, target_version: str = "") -> dict[str, Any]:
    url = f"{config.server_url}/api/v1/config"
    requested_version = str(target_version or "").strip()
    if requested_version:
        url += "?" + urllib.parse.urlencode({"target_version": requested_version})
    remote_config = transport.request_json("GET", url)
    if not remote_config.get("configured", True):
        raise RuntimeError("服务端还没有配置采集版本和日期范围")
    return remote_config


def _client_tmp_dir(settings: Settings) -> Path:
    path = settings.root_dir / ".tmp" / "client_upload"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_server_url(server_url: str) -> str:
    value = server_url.strip().rstrip("/")
    if not value:
        raise ValueError("server_url 不能为空")
    return value


def _configured_server_url(settings: Settings) -> str:
    try:
        return load_client_config(settings).server_url
    except RuntimeError:
        return "http://43.128.141.76:8000"


def _ensure_client_database(settings: Settings) -> None:
    # 单文件 exe 不携带 Alembic 脚本，客户端本地库用 create_all 初始化即可。
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
