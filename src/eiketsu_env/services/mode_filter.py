"""判断对局模式是否适合纳入常规环境统计。"""

from __future__ import annotations


EXCLUDED_DEFAULT_MODES = {"群雄伝", "鍛練場", "戦祭り"}


def is_environment_mode(mode: str, include_solo: bool = False) -> bool:
    cleaned = str(mode or "").strip()
    if include_solo:
        return True
    # 默认环境统计只看常规规则；群雄传/练习场不是 PvP，战祭规则特殊，都会污染胜率和卡组使用率。
    return cleaned not in EXCLUDED_DEFAULT_MODES
