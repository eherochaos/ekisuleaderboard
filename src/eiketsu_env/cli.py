"""定义命令行入口，把采集、导出、分析等服务串成可执行命令。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from eiketsu_env.config import load_settings
from eiketsu_env.db.migrations import upgrade_database
from eiketsu_env.services.analysis import export_analysis, refresh_analysis
from eiketsu_env.services.browser_session import doctor_browser
from eiketsu_env.services.collector import collect_follow, parse_collect_dates
from eiketsu_env.services.exporter import export_matches
from eiketsu_env.services.firefox_session import doctor_firefox
from eiketsu_env.services.progress import ProgressReporter
from eiketsu_env.services.share import (
    aggregate_shared,
    doctor_share,
    export_contribution,
    import_contributions,
    load_share_config,
    sync_shared,
)
from eiketsu_env.services.video_search import collect_video_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eiketsu-env", description="英杰大战环境对局采集 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="初始化或迁移 SQLite 数据库")

    doctor_parser = subparsers.add_parser("doctor", help="检查本地环境")
    doctor_sub = doctor_parser.add_subparsers(dest="doctor_command", required=True)
    doctor_sub.add_parser("firefox", help="检查 Firefox profile 与 cookies")
    browser_doctor = doctor_sub.add_parser("browser", help="检查默认浏览器/Chrome/Edge/Brave/Firefox 登录态")
    browser_doctor.add_argument("--auth-source", default="", help="认证来源：auto/default-browser/chrome/edge/brave/firefox/firefox-profile")

    collect_parser = subparsers.add_parser("collect", help="采集会员区数据")
    collect_sub = collect_parser.add_subparsers(dest="collect_command", required=True)
    follow_parser = collect_sub.add_parser("follow", help="从关注列表采集每日对局")
    follow_parser.add_argument("--date", dest="date_value", help="采集单日，格式 YYYY-MM-DD")
    follow_parser.add_argument("--from", dest="from_value", help="采集起始日期，格式 YYYY-MM-DD")
    follow_parser.add_argument("--to", dest="to_value", help="采集结束日期，格式 YYYY-MM-DD")
    follow_parser.add_argument("--max-players", type=int, default=0, help="最多采集前 N 个关注主君，0 表示不限")
    follow_parser.add_argument("--max-matches", type=int, default=0, help="最多深抓 N 场详情，0 表示不限")
    follow_parser.add_argument("--player-id", default="", help="只采集指定 follow_id 的主君")
    follow_parser.add_argument("--player-name", default="", help="按主君名包含关系过滤后采集")
    follow_parser.add_argument("--include-solo", action="store_true", help="包含群雄传/练习场/战祭等默认排除模式")
    follow_parser.add_argument("--skip-existing", action="store_true", help="跳过已经有完整详情的对局，适合补采历史版本")
    follow_parser.add_argument("--skip-inactive", action="store_true", help="根据 follow API 的 lastplaytime 跳过范围开始前已不活跃的主君")
    follow_parser.add_argument("--concurrency-profile", choices=["default", "aggressive"], default="default", help="采集并发 profile；aggressive=8 daily/12 detail + 重试")
    follow_parser.add_argument("--no-progress", action="store_true", help="不显示 stderr 进度条")
    follow_parser.add_argument("--no-raw-snapshots", action="store_true", help="不保存原始 HTML 快照，只保留结构化对局数据")
    follow_parser.add_argument("--auth-source", default="", help="认证来源：auto/default-browser/chrome/edge/brave/firefox/firefox-profile")
    video_parser = collect_sub.add_parser("video-search", help="从演武场视频搜索扩展采集样本")
    video_parser.add_argument("--date", dest="date_value", help="采集单日，格式 YYYY-MM-DD")
    video_parser.add_argument("--from", dest="from_value", help="采集起始日期，格式 YYYY-MM-DD")
    video_parser.add_argument("--to", dest="to_value", help="采集结束日期，格式 YYYY-MM-DD")
    video_parser.add_argument("--card-hash", action="append", default=[], help="指定用于演武场搜索的卡牌 hash；可重复传入")
    video_parser.add_argument("--max-cards", type=int, default=20, help="未指定 --card-hash 时，最多使用库内高频前 N 张卡")
    video_parser.add_argument("--max-results", type=int, default=0, help="最多新增 N 条演武场样本，0 表示不限")
    video_parser.add_argument("--version", default="", help="只保留 API package 等于指定版本的演武场结果")
    video_parser.add_argument("--skip-searched-cards", action="store_true", help="跳过 raw_snapshots 中已有 video_search_api 记录的卡牌 hash")
    video_parser.add_argument("--frontier-rounds", default="1", help="frontier 扩展轮数，支持正整数或 auto")
    video_parser.add_argument("--concurrency-profile", choices=["default", "aggressive"], default="default", help="采集并发 profile；aggressive=8 API/12 play + 限速重试")
    video_parser.add_argument("--no-progress", action="store_true", help="不显示 stderr 进度条")
    video_parser.add_argument("--include-solo", action="store_true", help="包含群雄传/练习场/战祭等默认排除模式")
    video_parser.add_argument("--auth-source", default="", help="认证来源：auto/default-browser/chrome/edge/brave/firefox/firefox-profile")

    export_parser = subparsers.add_parser("export", help="导出标准化数据")
    export_sub = export_parser.add_subparsers(dest="export_command", required=True)
    matches_parser = export_sub.add_parser("matches", help="导出对局宽表")
    matches_parser.add_argument("--format", choices=["csv", "parquet", "md"], default="csv")
    matches_parser.add_argument("--output", type=Path)

    analyze_parser = subparsers.add_parser("analyze", help="生成和导出环境分析")
    analyze_sub = analyze_parser.add_subparsers(dest="analyze_command", required=True)
    refresh_parser = analyze_sub.add_parser("refresh", help="刷新分析快照")
    refresh_parser.add_argument("--from", dest="from_value", required=True, help="分析起始日期，格式 YYYY-MM-DD")
    refresh_parser.add_argument("--to", dest="to_value", required=True, help="分析结束日期，格式 YYYY-MM-DD")
    refresh_parser.add_argument("--high-ranker-rank", type=int, default=100, help="高 Ranker 口径的全国排名上限，默认 Top100")
    refresh_parser.add_argument("--version", default="", help="只统计指定版本，留空表示按日期范围统计所有版本")
    analysis_export = analyze_sub.add_parser("export", help="导出分析报告")
    analysis_export.add_argument("--report", choices=["deck", "deck-visual", "deck-archetype-visual", "card", "deck-version", "card-version", "overview"], required=True)
    analysis_export.add_argument("--format", choices=["csv", "md", "html"], default="md")
    analysis_export.add_argument("--output", type=Path)

    share_parser = subparsers.add_parser("share", help="多人贡献包共享与自动汇总")
    share_sub = share_parser.add_subparsers(dest="share_command", required=True)
    share_sub.add_parser("doctor", help="检查共享配置、Git 状态和贡献包数量")
    share_export = share_sub.add_parser("export", help="导出当前本地库的目标版本贡献包")
    share_export.add_argument("--contributor", default="", help="贡献者昵称；也可用 EIKETSU_SHARE_CONTRIBUTOR")
    share_export.add_argument("--output", type=Path, help="自定义 JSONL 输出路径")
    share_import = share_sub.add_parser("import", help="导入贡献包；不传路径时扫描 shared/contributions")
    share_import.add_argument("paths", nargs="*", type=Path, help="贡献包文件或目录")
    share_aggregate = share_sub.add_parser("aggregate", help="导入贡献包、刷新目标版本分析并导出共享报告")
    share_aggregate.add_argument("paths", nargs="*", type=Path, help="可选：只导入指定贡献包文件或目录")
    share_sync = share_sub.add_parser("sync", help="一键 Git 同步、采集、导出贡献包、汇总报告并推送")
    share_sync.add_argument("--contributor", default="", help="贡献者昵称；也可用 EIKETSU_SHARE_CONTRIBUTOR")
    share_sync.add_argument("--auth-source", default="", help="认证来源：auto/default-browser/chrome/edge/brave/firefox/firefox-profile")
    share_sync.add_argument("--skip-collect", action="store_true", help="跳过联网采集，只导出/导入/汇总本地已有数据")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = load_settings()

    if args.command == "init-db":
        upgrade_database(settings)
        print(f"数据库已初始化：{settings.db_url}")
        return

    if args.command == "doctor" and args.doctor_command == "firefox":
        result = doctor_firefox(settings)
        print(
            json.dumps(
                {
                    "profile_exists": result.profile_exists,
                    "cookie_db_exists": result.cookie_db_exists,
                    "loaded_cookie_count": result.loaded_cookie_count,
                    "message": result.message,
                    "profile": str(settings.firefox_profile or ""),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "doctor" and args.doctor_command == "browser":
        print(json.dumps(doctor_browser(settings, args.auth_source or None), ensure_ascii=False, indent=2))
        return

    if args.command == "collect" and args.collect_command == "follow":
        date_from, date_to = parse_collect_dates(args.date_value, args.from_value, args.to_value)
        result = collect_follow(
            settings,
            date_from,
            date_to,
            max_players=args.max_players,
            max_matches=args.max_matches,
            player_id=args.player_id,
            player_name=args.player_name,
            include_solo=args.include_solo,
            auth_source=args.auth_source,
            skip_existing=args.skip_existing,
            skip_inactive=args.skip_inactive,
            concurrency_profile=args.concurrency_profile,
            progress=ProgressReporter(enabled=not args.no_progress),
            save_raw_snapshots=not args.no_raw_snapshots,
        )
        print(json.dumps({"run_id": result.run_id, "status": result.status, "counts": result.counts, "errors": result.errors}, ensure_ascii=False, indent=2))
        return

    if args.command == "collect" and args.collect_command == "video-search":
        date_from, date_to = parse_collect_dates(args.date_value, args.from_value, args.to_value)
        result = collect_video_search(
            settings,
            date_from,
            date_to,
            card_hashes=args.card_hash,
            max_cards=args.max_cards,
            max_results=args.max_results,
            include_solo=args.include_solo,
            auth_source=args.auth_source,
            version=args.version,
            skip_searched_cards=args.skip_searched_cards,
            frontier_rounds=args.frontier_rounds,
            concurrency_profile=args.concurrency_profile,
            progress=ProgressReporter(enabled=not args.no_progress),
        )
        print(json.dumps({"run_id": result.run_id, "status": result.status, "counts": result.counts, "errors": result.errors}, ensure_ascii=False, indent=2))
        return

    if args.command == "export" and args.export_command == "matches":
        output = export_matches(settings, args.format, args.output)
        print(f"已导出：{output}")
        return

    if args.command == "analyze" and args.analyze_command == "refresh":
        result = refresh_analysis(settings, args.from_value, args.to_value, high_ranker_rank=args.high_ranker_rank, version=args.version)
        print(json.dumps({"run_id": result.run_id, "status": result.status, "counts": result.counts}, ensure_ascii=False, indent=2))
        return

    if args.command == "analyze" and args.analyze_command == "export":
        output = export_analysis(settings, args.report, args.format, args.output)
        print(f"已导出：{output}")
        return

    if args.command == "share" and args.share_command == "doctor":
        print(json.dumps(doctor_share(settings), ensure_ascii=False, indent=2))
        return

    if args.command == "share" and args.share_command == "export":
        config = load_share_config(settings)
        result = export_contribution(settings, config, _contributor_arg(args.contributor), args.output)
        print(
            json.dumps(
                {
                    "path": str(result.path),
                    "package_id": result.package_id,
                    "match_count": result.match_count,
                    "content_hash": result.content_hash,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "share" and args.share_command == "import":
        result = import_contributions(settings, args.paths or None)
        print(json.dumps(_share_import_payload(result), ensure_ascii=False, indent=2))
        return

    if args.command == "share" and args.share_command == "aggregate":
        result = aggregate_shared(settings, import_paths=args.paths or None)
        print(
            json.dumps(
                {
                    "analysis_run_id": result.analysis_run_id,
                    "reports": [str(path) for path in result.report_paths],
                    "import": _share_import_payload(result.import_result),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "share" and args.share_command == "sync":
        result = sync_shared(settings, _contributor_arg(args.contributor), skip_collect=args.skip_collect, auth_source=args.auth_source)
        print(
            json.dumps(
                {
                    "package": str(result.export_result.path),
                    "analysis_run_id": result.aggregate_result.analysis_run_id,
                    "reports": [str(path) for path in result.aggregate_result.report_paths],
                    "committed": result.committed,
                    "pushed": result.pushed,
                    "collect": {
                        "run_id": result.collect_result.run_id,
                        "status": result.collect_result.status,
                        "counts": result.collect_result.counts,
                        "errors": result.collect_result.errors,
                    }
                    if result.collect_result
                    else None,
                    "import": _share_import_payload(result.aggregate_result.import_result),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    raise SystemExit("未知命令")


def _contributor_arg(value: str) -> str:
    return value or os.environ.get("EIKETSU_SHARE_CONTRIBUTOR", "")


def _share_import_payload(result) -> dict:
    return {
        "packages_seen": result.packages_seen,
        "packages_imported": result.packages_imported,
        "packages_skipped": result.packages_skipped,
        "matches_imported": result.matches_imported,
        "errors": result.errors,
    }
