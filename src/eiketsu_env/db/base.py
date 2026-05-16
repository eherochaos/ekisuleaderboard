"""声明 SQLAlchemy 基类，供所有 ORM 模型统一继承。"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
