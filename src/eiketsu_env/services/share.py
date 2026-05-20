"""多人共享贡献包的导出、导入、汇总和 Git 同步流程。"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from eiketsu_env.config import Settings
from eiketsu_env.db.migrations import upgrade_database
from eiketsu_env.db.models import (
    Match,
    MatchDeck,
    SharedContributionMatch,
    SharedContributionPackage,
)
from eiketsu_env.db.session import make_session_factory
from eiketsu_env.services.analysis import export_analysis, refresh_analysis
from eiketsu_env.services.collector import CollectResult, collect_follow
from eiketsu_env.services.mode_filter import is_environment_mode
from eiketsu_env.services.repository import EnvRepository
from eiketsu_env.utils import JST, sha256_text, utc_now, write_json


SHARE_SCHEMA_VERSION = "share_v1"
MANIFEST_RECORD_TYPE = "manifest"
MATCH_RECORD_TYPE = "match"
DEFAULT_REPORTS = ["overview", "deck", "card", "deck-version", "card-version"]
DEFAULT_REPORT_FORMATS = ["md", "csv"]
SAFE_GIT_PATHS = ["shared/share_config.json", "shared/contributions", "shared/reports"]
FORBIDDEN_SHARED_KEYS = {
    "cookie",
    "cookies",
    "firefox_profile",
    "browser_profile",
    "browser_path",
    "profile_path",
    "local_path",
    "raw_html",
    "raw_snapshots",
}

CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class ShareConfig:
    schema_version: str = SHARE_SCHEMA_VERSION
    target_version: str = ""
    date_from: str = ""
    date_to: str = ""
    include_solo: bool = False
    high_ranker_rank: int = 100
    report_formats: list[str] = field(default_factory=lambda: list(DEFAULT_REPORT_FORMATS))
    reports: list[str] = field(default_factory=lambda: list(DEFAULT_REPORTS))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ShareConfig":
        return cls(
            schema_version=str(payload.get("schema_version") or SHARE_SCHEMA_VERSION),
            target_version=str(payload.get("target_version") or ""),
            date_from=str(payload.get("date_from") or ""),
            date_to=str(payload.get("date_to") or ""),
            include_solo=bool(payload.get("include_solo", False)),
            high_ranker_rank=int(payload.get("high_ranker_rank") or 100),
            report_formats=[str(item) for item in payload.get("report_formats") or DEFAULT_REPORT_FORMATS],
            reports=[str(item) for item in payload.get("reports") or DEFAULT_REPORTS],
        )

    def validate(self) -> None:
        if self.schema_version != SHARE_SCHEMA_VERSION:
            raise ValueError(f"不支持的共享 schema_version：{self.schema_version}")
        if not self.target_version:
            raise ValueError("shared/share_config.json 缺少 target_version")
        _validate_date(self.date_from, "date_from")
        _validate_date(self.date_to, "date_to")
        if self.date_to < self.date_from:
            raise ValueError("share_config.json 的 date_to 不能早于 date_from")
        unsupported_formats = [item for item in self.report_formats if item not in {"csv", "md"}]
        if unsupported_formats:
            raise ValueError(f"不支持的报告格式：{unsupported_formats}")
        unsupported_reports = [item for item in self.reports if item not in DEFAULT_REPORTS]
        if unsupported_reports:
            raise ValueError(f"不支持的报告类型：{unsupported_reports}")


@dataclass(slots=True)
class ShareExportResult:
    path: Path
    package_id: str
    match_count: int
    content_hash: str


@dataclass(slots=True)
class ShareImportResult:
    packages_seen: int
    packages_imported: int
    packages_skipped: int
    matches_imported: int
    errors: list[dict[str, Any]]


@dataclass(slots=True)
class ShareAggregateResult:
    import_result: ShareImportResult
    analysis_run_id: int
    report_paths: list[Path]


@dataclass(slots=True)
class ShareSyncResult:
    export_result: ShareExportResult
    aggregate_result: ShareAggregateResult
    collect_result: CollectResult | None
    committed: bool
    pushed: bool


def load_share_config(settings: Settings, path: Path | None = None, *, effective: bool = True) -> ShareConfig:
    config_path = path or share_config_path(settings)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    config = ShareConfig.from_payload(payload)
    config.validate()
    return effective_share_config(config) if effective else config


def effective_share_config(config: ShareConfig, today: date | None = None) -> ShareConfig:
    effective_date_to = _effective_date_to(config, today=today)
    if effective_date_to == config.date_to:
        return config
    effective = ShareConfig(
        schema_version=config.schema_version,
        target_version=config.target_version,
        date_from=config.date_from,
        date_to=effective_date_to,
        include_solo=config.include_solo,
        high_ranker_rank=config.high_ranker_rank,
        report_formats=list(config.report_formats),
        reports=list(config.reports),
    )
    effective.validate()
    return effective


def _effective_date_to(config: ShareConfig, today: date | None = None) -> str:
    latest_collectable = _latest_collectable_game_date(today=today)
    return max(config.date_from, config.date_to, latest_collectable)


def _latest_collectable_game_date(today: date | None = None) -> str:
    current_day = today or datetime.now(JST).date()
    return current_day.isoformat()


def share_config_path(settings: Settings) -> Path:
    return shared_root(settings) / "share_config.json"


def shared_root(settings: Settings) -> Path:
    return settings.root_dir / "shared"


def shared_contributions_dir(settings: Settings) -> Path:
    return shared_root(settings) / "contributions"


def shared_reports_dir(settings: Settings, target_version: str) -> Path:
    return shared_root(settings) / "reports" / _safe_path_component(target_version)


def export_contribution(
    settings: Settings,
    config: ShareConfig,
    contributor_id: str,
    output: Path | None = None,
) -> ShareExportResult:
    contributor_id = _validate_contributor(contributor_id)
    records = _match_records_for_config(settings, config)
    body_text = "".join(_json_line(record) for record in records)
    body_hash = sha256_text(body_text)
    package_id = _build_package_id(config, contributor_id, body_hash)
    manifest = {
        "record_type": MANIFEST_RECORD_TYPE,
        "schema_version": SHARE_SCHEMA_VERSION,
        "package_id": package_id,
        "contributor_id": contributor_id,
        "target_version": config.target_version,
        "date_from": config.date_from,
        "date_to": config.date_to,
        "include_solo": config.include_solo,
        "body_hash": body_hash,
        "match_count": len(records),
        "created_at": utc_now().isoformat(timespec="seconds"),
        "source": "eiketsu-env-db",
    }
    text = _json_line(manifest) + body_text
    content_hash = sha256_text(text)
    if output is None:
        output = (
            shared_contributions_dir(settings)
            / _safe_path_component(contributor_id)
            / _safe_path_component(config.target_version)
            / f"{config.date_from}_{config.date_to}_{body_hash[:12]}.jsonl"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return ShareExportResult(output, package_id, len(records), content_hash)


def parse_contribution_package_text(package_text: str) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]:
    """解析共享包文本；服务端上传和本地导入共用同一套 manifest 校验。"""

    return _parse_package_text(package_text)


def assert_safe_contribution_payload(package_text: str) -> None:
    """拒绝明显越界的本地隐私字段，避免客户端或第三方包把浏览器信息传到共享区。"""

    manifest, records = _parse_package_text(package_text)
    _validate_manifest_body_hash(manifest, records)
    _assert_no_forbidden_shared_keys(manifest, "manifest")
    for line_number, record in records:
        _assert_no_forbidden_shared_keys(record, f"line {line_number}")


def import_contributions(settings: Settings, paths: list[Path] | None = None) -> ShareImportResult:
    expanded_paths = _expand_import_paths(settings, paths)
    result = ShareImportResult(
        packages_seen=len(expanded_paths),
        packages_imported=0,
        packages_skipped=0,
        matches_imported=0,
        errors=[],
    )
    factory = make_session_factory(settings)
    with factory() as session:
        for path in expanded_paths:
            try:
                imported, skipped, match_count = _import_contribution_file(session, settings, path)
                result.packages_imported += imported
                result.packages_skipped += skipped
                result.matches_imported += match_count
                session.commit()
            except Exception as exc:  # noqa: BLE001 - 单个包失败不能阻断其它贡献包导入。
                session.rollback()
                result.errors.append({"path": str(path), "error": str(exc)})
                _record_failed_package(session, path, exc)
                session.commit()
    return result


def aggregate_shared(
    settings: Settings,
    config: ShareConfig | None = None,
    import_paths: list[Path] | None = None,
    reports_dir: Path | None = None,
) -> ShareAggregateResult:
    config = config or load_share_config(settings)
    import_result = import_contributions(settings, import_paths)
    analysis = refresh_analysis(
        settings,
        config.date_from,
        config.date_to,
        high_ranker_rank=config.high_ranker_rank,
        version=config.target_version,
    )
    target_dir = reports_dir or shared_reports_dir(settings, config.target_version)
    target_dir.mkdir(parents=True, exist_ok=True)
    report_paths: list[Path] = []
    for report in config.reports:
        for output_format in config.report_formats:
            output = target_dir / f"analysis_{report}.{output_format}"
            report_paths.append(export_analysis(settings, report, output_format, output))
    summary_path = target_dir / "aggregate_summary.json"
    write_json(
        summary_path,
        {
            "target_version": config.target_version,
            "date_from": config.date_from,
            "date_to": config.date_to,
            "analysis_run_id": analysis.run_id,
            "analysis_counts": analysis.counts,
            "import": _import_result_dict(import_result),
            "reports": [str(path.relative_to(settings.root_dir)) for path in report_paths],
            "generated_at": utc_now().isoformat(timespec="seconds"),
        },
    )
    report_paths.append(summary_path)
    return ShareAggregateResult(import_result, analysis.run_id, report_paths)


def doctor_share(settings: Settings, runner: CommandRunner | None = None) -> dict[str, Any]:
    runner = runner or _run_command
    config_error = ""
    config: ShareConfig | None = None
    config_path = share_config_path(settings)
    if config_path.exists():
        try:
            config = load_share_config(settings, config_path)
        except Exception as exc:  # noqa: BLE001 - doctor 只汇报问题，不中断检查。
            config_error = str(exc)
    else:
        config_error = "shared/share_config.json 不存在"
    return {
        "git_repo": _is_git_repo(settings, runner),
        "shared_config_exists": config_path.exists(),
        "shared_config_valid": config is not None,
        "shared_config_error": config_error,
        "target_version": config.target_version if config else "",
        "date_from": config.date_from if config else "",
        "date_to": config.date_to if config else "",
        "data_ignored": _gitignore_contains_data(settings),
        "contribution_files": len(_expand_import_paths(settings, None)),
    }


def sync_shared(
    settings: Settings,
    contributor_id: str,
    skip_collect: bool = False,
    auth_source: str = "",
    runner: CommandRunner | None = None,
) -> ShareSyncResult:
    runner = runner or _run_command
    contributor_id = _validate_contributor(contributor_id)
    config = load_share_config(settings)
    if not _is_git_repo(settings, runner):
        raise RuntimeError("当前目录不是 Git 仓库；请先初始化私有仓库并配置远端后再运行 share sync")

    _git_required(runner, settings, ["git", "pull", "--rebase"])
    upgrade_database(settings)
    collect_result = None
    if not skip_collect:
        collect_result = collect_follow(
            settings,
            config.date_from,
            config.date_to,
            include_solo=config.include_solo,
            auth_source=auth_source,
            interactive_auth=True,
            save_raw_snapshots=False,
        )
    export_result = export_contribution(settings, config, contributor_id)
    aggregate_result = aggregate_shared(settings, config)
    committed = _commit_shared_changes(settings, runner, f"share: update {config.target_version} samples")
    pushed = False
    if committed:
        push_result = runner(["git", "push"], settings.root_dir)
        if push_result.returncode != 0:
            _git_required(runner, settings, ["git", "pull", "--rebase"])
            aggregate_result = aggregate_shared(settings, config)
            _commit_shared_changes(settings, runner, f"share: refresh {config.target_version} reports")
            retry = runner(["git", "push"], settings.root_dir)
            if retry.returncode != 0:
                raise RuntimeError(f"git push 失败：{retry.stderr or retry.stdout}")
        pushed = True
    return ShareSyncResult(export_result, aggregate_result, collect_result, committed, pushed)


def _match_records_for_config(settings: Settings, config: ShareConfig) -> list[dict[str, Any]]:
    factory = make_session_factory(settings)
    with factory() as session:
        matches = session.scalars(
            select(Match)
            .options(
                selectinload(Match.sides),
                selectinload(Match.battle_summary),
                selectinload(Match.decks).selectinload(MatchDeck.units),
                selectinload(Match.replay_asset),
            )
            .order_by(Match.played_at, Match.id)
        ).all()
        return [
            _match_to_shared_record(match)
            for match in matches
            if _match_in_share_scope(match, config)
        ]


def _match_in_share_scope(match: Match, config: ShareConfig) -> bool:
    played_date = str(match.played_at or "")[:10]
    if not played_date or not (config.date_from <= played_date <= config.date_to):
        return False
    if str(match.version or "") != config.target_version:
        return False
    return config.include_solo or is_environment_mode(match.mode or "", include_solo=False)


def _match_to_shared_record(match: Match) -> dict[str, Any]:
    decks = {deck.side_index: deck for deck in match.decks}
    players = []
    for side in sorted(match.sides, key=lambda item: item.side_index):
        deck = decks.get(side.side_index)
        players.append(
            {
                "side_index": side.side_index,
                "role": side.role,
                "player_name": side.player_name or "",
                "follow_id": side.follow_id or "",
                "result": side.result,
                "castle_rate": side.castle_rate or "",
                "profile": side.profile_json or {},
                "selected": side.selected_json or {},
                "deck_ids": [unit.card_hash for unit in deck.units] if deck else [],
            }
        )
    summary = match.battle_summary
    return {
        "record_type": MATCH_RECORD_TYPE,
        "public_id": match.public_id,
        "replay_id": match.replay_id or "",
        "detail_t": match.detail_t or "",
        "primary_follow_id": match.primary_follow_id or "",
        "played_at": match.played_at or "",
        "mode": match.mode or "",
        "version": match.version or "",
        "result": match.result,
        "detail_url": match.detail_url,
        "play_url": match.play_url or "",
        "m3u8_url": match.m3u8_url or "",
        "source_url": match.source_url or "",
        "players": players,
        "title": summary.raw_title if summary else "",
        "detail_error": summary.detail_error if summary else "",
        "castle_breakdown": summary.castle_breakdown_json if summary else {},
        "timeline_labels": summary.timeline_labels_json if summary else [],
        "timeline_data": summary.timeline_data_json if summary else {},
    }


def _import_contribution_file(session: Session, settings: Settings, path: Path) -> tuple[int, int, int]:
    package_text = path.read_text(encoding="utf-8")
    content_hash = sha256_text(package_text)
    manifest, records = _parse_package_text(package_text)
    _validate_manifest_body_hash(manifest, records)
    _assert_no_forbidden_shared_keys(manifest, "manifest")
    for line_number, record in records:
        _assert_no_forbidden_shared_keys(record, f"line {line_number}")
    package_id = str(manifest["package_id"])

    existing_by_content = session.scalar(
        select(SharedContributionPackage).where(SharedContributionPackage.content_hash == content_hash)
    )
    if existing_by_content and existing_by_content.status == "completed":
        return 0, 1, 0
    package = session.get(SharedContributionPackage, package_id) or existing_by_content
    if package and package.content_hash == content_hash and package.status == "completed":
        return 0, 1, 0
    if package is None:
        package = SharedContributionPackage(package_id=package_id)
        session.add(package)

    _update_package_row(package, manifest, content_hash, path, "running", [])
    session.flush()
    repo = EnvRepository(session, settings)
    errors: list[dict[str, Any]] = []
    imported = 0
    for line_number, record in records:
        try:
            if str(record.get("version") or "") != str(manifest.get("target_version") or ""):
                raise ValueError("match record version 与 manifest target_version 不一致")
            match = repo.upsert_match_detail(_record_to_detail(record))
            _ensure_package_match_link(session, package.package_id, match)
            imported += 1
        except Exception as exc:  # noqa: BLE001 - 保留坏行信息，继续导入同包其它样本。
            errors.append({"line": line_number, "public_id": record.get("public_id", ""), "error": str(exc)})
    package.match_count = int(manifest.get("match_count") or len(records))
    package.imported_match_count = imported
    package.status = "completed_with_errors" if errors else "completed"
    package.error_summary_json = errors
    package.imported_at = utc_now()
    return 1, 0, imported


def _parse_package_text(package_text: str) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]:
    lines = [(index, line) for index, line in enumerate(package_text.splitlines(), start=1) if line.strip()]
    if not lines:
        raise ValueError("贡献包为空")
    manifest_line, manifest_text = lines[0]
    manifest = json.loads(manifest_text)
    if manifest.get("record_type") != MANIFEST_RECORD_TYPE:
        raise ValueError(f"第 {manifest_line} 行不是 manifest")
    if manifest.get("schema_version") != SHARE_SCHEMA_VERSION:
        raise ValueError(f"不支持的共享 schema_version：{manifest.get('schema_version')}")
    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in lines[1:]:
        record = json.loads(line)
        if record.get("record_type") != MATCH_RECORD_TYPE:
            raise ValueError(f"第 {line_number} 行不是 match record")
        records.append((line_number, record))
    return manifest, records


def _assert_no_forbidden_shared_keys(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in FORBIDDEN_SHARED_KEYS:
                raise ValueError(f"共享包包含禁止字段 {key_text}（{location}）")
            _assert_no_forbidden_shared_keys(child, f"{location}.{key_text}")
    elif isinstance(value, list):
        for index, child in enumerate(value, start=1):
            _assert_no_forbidden_shared_keys(child, f"{location}[{index}]")


def _validate_manifest_body_hash(manifest: dict[str, Any], records: list[tuple[int, dict[str, Any]]]) -> None:
    expected = str(manifest.get("body_hash") or "")
    actual = sha256_text("".join(_json_line(record) for _line, record in records))
    if expected and expected != actual:
        raise ValueError("贡献包 body_hash 校验失败")
    expected_count = int(manifest.get("match_count") or 0)
    if expected_count != len(records):
        raise ValueError("贡献包 match_count 与实际记录数不一致")


def _record_to_detail(record: dict[str, Any]) -> dict[str, Any]:
    players = []
    for player in record.get("players") or []:
        players.append(
            {
                "side_index": int(player.get("side_index") or len(players) + 1),
                "role": player.get("role") or "unknown",
                "player_name": player.get("player_name") or "",
                "follow_id": player.get("follow_id") or "",
                "result": player.get("result") or "unknown",
                "castle_rate": player.get("castle_rate") or "",
                "profile": player.get("profile") or {},
                "selected": player.get("selected") or {},
                "deck_ids": player.get("deck_ids") or [],
            }
        )
    source_type = "video_search" if _record_is_lightweight(record) else "shared_package"
    return {
        "source_type": source_type,
        "detail_url": record.get("detail_url") or record.get("source_url") or record.get("play_url") or "",
        "url": record.get("detail_url") or record.get("source_url") or record.get("play_url") or "",
        "source_url": record.get("source_url") or record.get("detail_url") or "",
        "follow_id": record.get("primary_follow_id") or _first_follow_id(players),
        "detail_t": record.get("detail_t") or "",
        "played_at": record.get("played_at") or "",
        "date": record.get("played_at") or "",
        "mode": record.get("mode") or "",
        "version": record.get("version") or "",
        "result": record.get("result") or "unknown",
        "replay_id": record.get("replay_id") or "",
        "play_url": record.get("play_url") or "",
        "m3u8_url": record.get("m3u8_url") or "",
        "title": record.get("title") or "",
        "detail_error": record.get("detail_error") or "",
        "castle_breakdown": record.get("castle_breakdown") or {},
        "timeline_labels": record.get("timeline_labels") or [],
        "timeline_data": record.get("timeline_data") or {},
        "players": players,
    }


def _record_is_lightweight(record: dict[str, Any]) -> bool:
    result = str(record.get("result") or "unknown")
    castle = record.get("castle_breakdown") if isinstance(record.get("castle_breakdown"), dict) else {}
    timeline = record.get("timeline_data") if isinstance(record.get("timeline_data"), dict) else {}
    return result == "unknown" and not castle.get("rows") and not timeline


def _first_follow_id(players: list[dict[str, Any]]) -> str:
    for player in players:
        follow_id = str(player.get("follow_id") or "")
        if follow_id:
            return follow_id
    return ""


def _ensure_package_match_link(session: Session, package_id: str, match: Match) -> None:
    link = session.scalar(
        select(SharedContributionMatch).where(
            SharedContributionMatch.package_id == package_id,
            SharedContributionMatch.match_id == match.id,
        )
    )
    if link is None:
        session.add(
            SharedContributionMatch(
                package_id=package_id,
                match_id=match.id,
                public_id=match.public_id,
                replay_id=match.replay_id,
                detail_t=match.detail_t,
            )
        )
        return
    link.public_id = match.public_id
    link.replay_id = match.replay_id
    link.detail_t = match.detail_t
    link.imported_at = utc_now()


def _record_failed_package(session: Session, path: Path, exc: Exception) -> None:
    content_hash = sha256_text(path.read_text(encoding="utf-8", errors="ignore")) if path.exists() else sha256_text(str(path))
    package_id = f"invalid:{content_hash[:24]}"
    package = session.get(SharedContributionPackage, package_id)
    if package is None:
        package = SharedContributionPackage(package_id=package_id)
        session.add(package)
    _update_package_row(
        package,
        {
            "contributor_id": "unknown",
            "target_version": "unknown",
            "date_from": "0000-00-00",
            "date_to": "0000-00-00",
            "schema_version": SHARE_SCHEMA_VERSION,
            "match_count": 0,
        },
        content_hash,
        path,
        "failed",
        [{"error": str(exc)}],
    )


def _update_package_row(
    package: SharedContributionPackage,
    manifest: dict[str, Any],
    content_hash: str,
    path: Path,
    status: str,
    errors: list[dict[str, Any]],
) -> None:
    package.contributor_id = str(manifest.get("contributor_id") or "unknown")
    package.target_version = str(manifest.get("target_version") or "unknown")
    package.date_from = str(manifest.get("date_from") or "0000-00-00")
    package.date_to = str(manifest.get("date_to") or "0000-00-00")
    package.schema_version = str(manifest.get("schema_version") or SHARE_SCHEMA_VERSION)
    package.content_hash = content_hash
    package.file_path = str(path)
    package.status = status
    package.match_count = int(manifest.get("match_count") or 0)
    package.imported_match_count = 0 if status in {"running", "failed"} or package.imported_match_count is None else package.imported_match_count
    package.error_summary_json = errors
    package.imported_at = utc_now()


def _expand_import_paths(settings: Settings, paths: list[Path] | None) -> list[Path]:
    if not paths:
        root = shared_contributions_dir(settings)
        return sorted(root.rglob("*.jsonl")) if root.exists() else []
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(sorted(path.rglob("*.jsonl")))
        elif path.exists():
            expanded.append(path)
    return expanded


def _commit_shared_changes(settings: Settings, runner: CommandRunner, message: str) -> bool:
    _ensure_only_shared_staged(settings, runner)
    _git_required(runner, settings, ["git", "add", "--", *SAFE_GIT_PATHS])
    _ensure_only_shared_staged(settings, runner)
    diff = runner(["git", "diff", "--cached", "--quiet"], settings.root_dir)
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise RuntimeError(f"git diff --cached 失败：{diff.stderr or diff.stdout}")
    _git_required(runner, settings, ["git", "commit", "-m", message])
    return True


def _ensure_only_shared_staged(settings: Settings, runner: CommandRunner) -> None:
    result = runner(["git", "diff", "--cached", "--name-only"], settings.root_dir)
    if result.returncode != 0:
        raise RuntimeError(f"检查暂存区失败：{result.stderr or result.stdout}")
    bad_paths = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().replace("\\", "/").startswith("shared/")
    ]
    if bad_paths:
        raise RuntimeError(f"暂存区已有非 shared 文件，已停止自动提交：{bad_paths}")


def _is_git_repo(settings: Settings, runner: CommandRunner) -> bool:
    result = runner(["git", "rev-parse", "--is-inside-work-tree"], settings.root_dir)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _git_required(runner: CommandRunner, settings: Settings, args: list[str]) -> subprocess.CompletedProcess[str]:
    result = runner(args, settings.root_dir)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败：{result.stderr or result.stdout}")
    return result


def _run_command(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def _gitignore_contains_data(settings: Settings) -> bool:
    path = settings.root_dir / ".gitignore"
    if not path.exists():
        return False
    return any(line.strip().replace("\\", "/") == "data/" for line in path.read_text(encoding="utf-8").splitlines())


def _validate_contributor(contributor_id: str) -> str:
    value = str(contributor_id or "").strip()
    if not value:
        raise ValueError("请提供贡献者昵称：--contributor 或 EIKETSU_SHARE_CONTRIBUTOR")
    return value


def _validate_date(value: str, field_name: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"share_config.json 的 {field_name} 必须是 YYYY-MM-DD") from exc


def _build_package_id(config: ShareConfig, contributor_id: str, body_hash: str) -> str:
    return ":".join(
        [
            _safe_path_component(contributor_id),
            _safe_path_component(config.target_version),
            config.date_from,
            config.date_to,
            body_hash[:16],
        ]
    )


def _safe_path_component(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip()).strip("._")
    return safe or "unknown"


def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _import_result_dict(result: ShareImportResult) -> dict[str, Any]:
    return {
        "packages_seen": result.packages_seen,
        "packages_imported": result.packages_imported,
        "packages_skipped": result.packages_skipped,
        "matches_imported": result.matches_imported,
        "errors": result.errors,
    }
