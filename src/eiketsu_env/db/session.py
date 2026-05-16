"""创建数据库引擎和会话工厂，统一处理 SQLite 目录初始化。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from eiketsu_env.config import Settings


def make_engine(settings: Settings):
    if settings.db_url.startswith("sqlite:///"):
        db_path = Path(settings.db_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.db_url, future=True)


def make_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(make_engine(settings), expire_on_commit=False, future=True)
