"""客户端更新包发布与检查。

首版不做运行中自我替换，避免 Windows 正在运行的 exe 被锁住导致更新失败。
服务端只负责托管最新版，客户端提示用户下载后手动关闭旧窗口并运行新版。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eiketsu_env.config import Settings
from eiketsu_env.utils import read_json, write_json


CLIENT_UPDATE_SCHEMA_VERSION = "client_update_v1"
CLIENT_EXE_STEM = "EiketsuCollector"
CLIENT_EXE_LEGACY_DOWNLOAD_NAME = f"{CLIENT_EXE_STEM}.exe"


@dataclass(slots=True)
class PublishedClientUpdate:
    latest_version: str
    stored_path: Path
    size_bytes: int
    sha256: str
    notes: str


def publish_client_update(
    settings: Settings,
    executable_path: Path,
    version: str,
    notes: str = "",
) -> PublishedClientUpdate:
    source = executable_path.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"找不到客户端 exe：{source}")
    if source.suffix.lower() != ".exe":
        raise ValueError("客户端更新包必须是 .exe 文件")
    latest_version = str(version or "").strip()
    if not latest_version:
        raise ValueError("version 不能为空")

    update_dir = client_update_dir(settings)
    update_dir.mkdir(parents=True, exist_ok=True)
    download_name = client_exe_download_name(latest_version)
    target = update_dir / download_name
    shutil.copy2(source, target)
    digest = _sha256_file(target)
    size_bytes = target.stat().st_size
    payload = {
        "schema_version": CLIENT_UPDATE_SCHEMA_VERSION,
        "latest_version": latest_version,
        "stored_filename": target.name,
        "download_name": download_name,
        "download_path": f"/downloads/{download_name}",
        "size_bytes": size_bytes,
        "sha256": digest,
        "notes": str(notes or ""),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_json(client_update_manifest_path(settings), payload)
    return PublishedClientUpdate(latest_version, target, size_bytes, digest, str(notes or ""))


def client_update_payload(settings: Settings, current_version: str = "", base_url: str = "") -> dict[str, Any]:
    manifest = load_client_update_manifest(settings)
    if not manifest:
        return {
            "configured": False,
            "current_version": str(current_version or ""),
            "latest_version": "",
            "update_available": False,
            "message": "服务端还没有发布客户端更新包",
        }
    current = str(current_version or "").strip()
    latest = str(manifest.get("latest_version") or "").strip()
    download_name = str(manifest.get("download_name") or client_exe_download_name(latest))
    download_path = str(manifest.get("download_path") or f"/downloads/{download_name}")
    return {
        "configured": True,
        "current_version": current,
        "latest_version": latest,
        "update_available": bool(current and version_is_newer(latest, current)),
        "download_url": _absolute_url(base_url, download_path),
        "download_name": download_name,
        "size_bytes": int(manifest.get("size_bytes") or 0),
        "sha256": str(manifest.get("sha256") or ""),
        "notes": str(manifest.get("notes") or ""),
        "published_at": str(manifest.get("published_at") or ""),
    }


def load_client_update_manifest(settings: Settings) -> dict[str, Any] | None:
    path = client_update_manifest_path(settings)
    if not path.exists():
        return None
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != CLIENT_UPDATE_SCHEMA_VERSION:
        raise ValueError("客户端更新 manifest 格式不正确")
    return payload


def resolve_client_update_file(settings: Settings) -> tuple[Path, dict[str, Any]]:
    manifest = load_client_update_manifest(settings)
    if not manifest:
        raise FileNotFoundError("服务端还没有发布客户端更新包")
    update_dir = client_update_dir(settings).resolve()
    target = (update_dir / str(manifest.get("stored_filename") or "")).resolve()
    if update_dir not in [target, *target.parents]:
        raise ValueError("客户端更新文件路径不安全")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"客户端更新文件不存在：{target.name}")
    return target, manifest


def version_is_newer(latest: str, current: str) -> bool:
    latest_key = _version_key(latest)
    current_key = _version_key(current)
    if latest_key != current_key:
        return latest_key > current_key
    return latest.strip() > current.strip()


def client_update_dir(settings: Settings) -> Path:
    return settings.data_dir / "client_updates"


def client_update_manifest_path(settings: Settings) -> Path:
    return client_update_dir(settings) / "manifest.json"


def client_exe_download_name(version: str) -> str:
    # 发布包名字里带版本号，方便人工测试更新，也避免浏览器把不同版本的 exe 混在一起。
    return f"{CLIENT_EXE_STEM}_{_safe_filename_component(version)}.exe"


def _version_key(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", str(value or ""))]
    return tuple(numbers or [0])


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "latest"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = str(base_url or "").rstrip("/")
    if not base:
        return path
    return f"{base}/{path.lstrip('/')}"
