from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eiketsu_env.services.version_update import (  # noqa: E402
    PreparedVersionUpdate,
    prepare_version_update,
    vps_refresh_commands,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="准备新版本采集配置和卡表 overlay。")
    parser.add_argument("--version", required=True, help="目标版本，例如 Ver.3.5.0A")
    parser.add_argument("--start-date", required=True, help="版本开始日期，例如 2026-05-20")
    parser.add_argument("--date-to", default="", help="采集结束日期，默认等于 --start-date")
    parser.add_argument(
        "--official-root",
        type=Path,
        default=ROOT.parent / "eki_database_v2",
        help="eki_database_v2 根目录，默认使用仓库相邻目录",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="只打印将要改动的摘要，不写文件")
    args = parser.parse_args()

    result = prepare_version_update(
        root=args.root,
        official_root=args.official_root,
        version=args.version,
        start_date=args.start_date,
        date_to=args.date_to or args.start_date,
        dry_run=args.dry_run,
    )
    print_summary(result, dry_run=args.dry_run)


def print_summary(result: PreparedVersionUpdate, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}目标版本：{result.version}")
    print(f"{prefix}采集日期：{result.start_date} -> {result.date_to}")
    print(f"{prefix}官方 base：{result.latest_base_path}")
    print(f"{prefix}overlay 卡数：{result.overlay_card_count}，新增：{result.added_overlay_card_count}")
    print()
    print("VPS 刷新命令：")
    for command in vps_refresh_commands(result):
        print(command)


if __name__ == "__main__":
    main()
