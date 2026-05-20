"""集中读取运行配置，决定数据库、缓存目录和外部卡牌库位置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


DEFAULT_AUTH_SOURCE = "auto"
VERSION_START_DATES = {
    "Ver.3.5.0A": "2026-05-20",
    "Ver.3.1.0H": "2026-04-22",
}


def known_target_versions() -> list[str]:
    return [
        version
        for version, _start in sorted(
            VERSION_START_DATES.items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
    ]


def latest_target_version() -> str:
    versions = known_target_versions()
    return versions[0] if versions else ""


def version_start_date(version: str) -> str:
    return VERSION_START_DATES.get(str(version or "").strip(), "")


@dataclass(slots=True)
class Settings:
    root_dir: Path
    db_url: str
    base_url: str = "https://eiketsu-taisen.net"
    firefox_profile: Path | None = None
    auth_source: str = DEFAULT_AUTH_SOURCE
    browser_profile: Path | None = None
    login_url: str = "https://eiketsu-taisen.net/members/"
    card_catalog_path: Path | None = None
    admin_token: str = ""
    cookie_domains: list[str] = field(
        default_factory=lambda: [
            "eiketsu-taisen.net",
            "sega.jp",
            "tgk-aime-gw.sega.jp",
        ]
    )

    @property
    def data_dir(self) -> Path:
        return self.root_dir / "data"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_settings(root_dir: Path | None = None) -> Settings:
    resolved_root = Path(
        os.environ.get("EIKETSU_ENV_ROOT")
        or (str(root_dir) if root_dir is not None else os.getcwd())
    ).resolve()
    db_path = resolved_root / "data" / "eiketsu_env.db"
    db_url = os.environ.get("EIKETSU_ENV_DB_URL") or f"sqlite:///{db_path.as_posix()}"
    firefox_profile_value = os.environ.get("EIKETSU_FIREFOX_PROFILE")
    firefox_profile = Path(firefox_profile_value) if firefox_profile_value else None
    browser_profile_value = os.environ.get("EIKETSU_BROWSER_PROFILE")
    auth_source = os.environ.get("EIKETSU_AUTH_SOURCE") or DEFAULT_AUTH_SOURCE
    base_url = os.environ.get("EIKETSU_BASE_URL") or "https://eiketsu-taisen.net"
    login_url = os.environ.get("EIKETSU_LOGIN_URL") or f"{base_url.rstrip('/')}/members/"
    catalog_path = os.environ.get("EIKETSU_CARD_CATALOG_PATH")
    admin_token = os.environ.get("EIKETSU_ADMIN_TOKEN") or ""
    # 卡牌主数据以相邻的 eki_database_v2 为准；lookup 层会优先读 SQLite，必要时退回 raw official base。
    default_catalog = resolved_root.parent / "eki_database_v2"
    project_catalog = resolved_root / "assets" / "card_catalog.json"
    packaged_catalog = _packaged_card_catalog_path()
    return Settings(
        root_dir=resolved_root,
        db_url=db_url,
        base_url=base_url.rstrip("/"),
        firefox_profile=firefox_profile,
        auth_source=auth_source,
        browser_profile=Path(browser_profile_value) if browser_profile_value else None,
        login_url=login_url,
        card_catalog_path=Path(catalog_path) if catalog_path else _first_existing_path(default_catalog, project_catalog, packaged_catalog),
        admin_token=admin_token,
    )


def _first_existing_path(*paths: Path | None) -> Path | None:
    for path in paths:
        if path is not None and path.exists():
            return path
    return paths[0] if paths else None


def _packaged_card_catalog_path() -> Path | None:
    try:
        path = resources.files("eiketsu_env").joinpath("assets/card_catalog.json")
    except (ModuleNotFoundError, OSError):
        return None
    try:
        return Path(str(path))
    except TypeError:
        return None
