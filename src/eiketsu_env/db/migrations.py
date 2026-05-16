"""封装 Alembic 配置和数据库升级入口。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from eiketsu_env.config import Settings


def alembic_config(settings: Settings) -> Config:
    root = settings.root_dir
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    return cfg


def upgrade_database(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")
