"""VPS 服务端管理命令：初始化配置和发放邀请码。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eiketsu_env.config import load_settings
from eiketsu_env.db.migrations import upgrade_database
from eiketsu_env.services.client_update import client_update_payload, publish_client_update
from eiketsu_env.services.leaderboard import prune_legacy_leaderboard_snapshots, refresh_public_leaderboard_materialized
from eiketsu_env.services.server_share import create_invite, get_server_config, list_invites, set_server_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eiketsu-server", description="英杰大战 VPS 服务端管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    admin = subparsers.add_parser("admin", help="服务端管理命令")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    invite = admin_sub.add_parser("create-invite", help="创建一次性邀请码")
    invite.add_argument("--label", default="", help="给你自己看的备注，例如朋友昵称")
    invite.add_argument("--code", default="", help="可选：手动指定邀请码")
    invite_list = admin_sub.add_parser("list-invites", help="查看邀请码列表")
    invite_list.add_argument("--status", default="all", choices=["all", "active", "used"])
    invite_list.add_argument("--limit", type=int, default=100)

    config = admin_sub.add_parser("set-config", help="设置客户端采集版本和日期范围")
    config.add_argument("--target-version", required=True)
    config.add_argument("--date-from", required=True)
    config.add_argument("--date-to", required=True)
    config.add_argument("--include-solo", action="store_true")
    config.add_argument("--high-ranker-rank", type=int, default=100)

    admin_sub.add_parser("show-config", help="查看当前服务端采集配置")

    refresh = admin_sub.add_parser("refresh-leaderboard", help="预生成公开聚合榜物化分页数据")
    refresh.add_argument("--rank-scope", default="all", help="兼容参数；当前一次刷新会生成全部公开段位视图")
    refresh.add_argument("--cluster", default="all", choices=["all", "on", "off"], help="兼容参数；当前会同时生成聚类和非聚类行")
    admin_sub.add_parser("prune-leaderboard-snapshots", help="只清理旧 JSON 榜单快照缓存")

    update = admin_sub.add_parser("publish-client", help="发布 Windows 客户端 exe 更新包")
    update.add_argument("--version", required=True, help="客户端版本号，例如 0.1.2")
    update.add_argument("--file", required=True, help="EiketsuCollector_0.1.8.exe 这类带版本号的文件路径")
    update.add_argument("--notes", default="", help="给用户看的更新说明")
    admin_sub.add_parser("show-client-update", help="查看当前发布的客户端更新包")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = load_settings()
    upgrade_database(settings)

    if args.command == "admin" and args.admin_command == "create-invite":
        result = create_invite(settings, args.label, code=args.code)
        print(json.dumps({"code": result.code, "label": result.label, "status": result.status}, ensure_ascii=False, indent=2))
        return

    if args.command == "admin" and args.admin_command == "list-invites":
        print(json.dumps(list_invites(settings, args.status, args.limit), ensure_ascii=False, indent=2))
        return

    if args.command == "admin" and args.admin_command == "set-config":
        payload = set_server_config(
            settings,
            target_version=args.target_version,
            date_from=args.date_from,
            date_to=args.date_to,
            include_solo=args.include_solo,
            high_ranker_rank=args.high_ranker_rank,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "admin" and args.admin_command == "show-config":
        print(json.dumps(get_server_config(settings), ensure_ascii=False, indent=2))
        return

    if args.command == "admin" and args.admin_command == "refresh-leaderboard":
        result = refresh_public_leaderboard_materialized(settings, rank_scope=args.rank_scope, cluster=args.cluster)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") != "completed":
            raise SystemExit(1)
        return

    if args.command == "admin" and args.admin_command == "prune-leaderboard-snapshots":
        print(json.dumps(prune_legacy_leaderboard_snapshots(settings), ensure_ascii=False, indent=2))
        return

    if args.command == "admin" and args.admin_command == "publish-client":
        result = publish_client_update(settings, Path(args.file), args.version, notes=args.notes)
        print(
            json.dumps(
                {
                    "latest_version": result.latest_version,
                    "stored_path": str(result.stored_path),
                    "size_bytes": result.size_bytes,
                    "sha256": result.sha256,
                    "notes": result.notes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "admin" and args.admin_command == "show-client-update":
        print(json.dumps(client_update_payload(settings), ensure_ascii=False, indent=2))
        return

    raise SystemExit("未知命令")


if __name__ == "__main__":
    main()
