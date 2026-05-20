"""面向普通用户的极简客户端 CLI，方便打包成 EiketsuCollector.exe。"""

from __future__ import annotations

import argparse
import json
import sys

from eiketsu_env.config import load_settings
from eiketsu_env.services.client_upload import bind_client, client_config_path, doctor_client, sync_client
from eiketsu_env.services.progress import ProgressReporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eiketsu-client", description="英杰大战数据采集上传客户端")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bind = subparsers.add_parser("bind", help="首次绑定 VPS 邀请码")
    bind.add_argument("--server", default="", help="VPS 地址，例如 http://1.2.3.4:8000")
    bind.add_argument("--invite", default="", help="你提供给朋友的一次性邀请码")
    bind.add_argument("--contributor", default="", help="朋友自己填写的昵称")

    sync = subparsers.add_parser("sync", help="采集本机登录态数据并上传到 VPS")
    sync.add_argument("--auth-source", default="", help="auto/default-browser/chrome/edge/brave/firefox/firefox-profile")
    sync.add_argument("--target-version", "--version", dest="target_version", default="", help="指定采集上传的目标版本；留空默认使用服务端最新版本")
    sync.add_argument("--no-progress", action="store_true", help="不显示采集进度")

    subparsers.add_parser("doctor", help="检查本机绑定和服务端配置")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = load_settings()

    if args.command == "bind":
        result = bind_client(
            settings,
            args.server or _prompt("VPS 地址"),
            args.invite or _prompt("邀请码"),
            args.contributor or _prompt("昵称"),
        )
        print(
            json.dumps(
                {
                    "status": "bound",
                    "server_url": result.server_url,
                    "user_public_id": result.user_public_id,
                    "token_prefix": result.token_prefix,
                    "config_path": str(result.config_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "sync":
        if not client_config_path(settings).exists():
            # 普通朋友双击时最容易漏掉 bind；这里直接进入首次绑定引导。
            bind_client(
                settings,
                _prompt("首次运行：VPS 地址"),
                _prompt("首次运行：邀请码"),
                _prompt("首次运行：昵称"),
            )
        result = sync_client(
            settings,
            auth_source=args.auth_source,
            target_version=args.target_version,
            progress=ProgressReporter(enabled=not args.no_progress),
        )
        print(
            json.dumps(
                {
                    "status": "uploaded",
                    "upload": result.upload,
                    "collect": {
                        "run_id": result.collect_result.run_id,
                        "status": result.collect_result.status,
                        "counts": result.collect_result.counts,
                        "errors": result.collect_result.errors,
                    },
                    "viewer_url": result.viewer_url,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "doctor":
        print(json.dumps(doctor_client(settings), ensure_ascii=False, indent=2))
        return

    raise SystemExit("未知命令")


def _prompt(label: str) -> str:
    return input(f"{label}: ").strip()


if __name__ == "__main__":
    main()
