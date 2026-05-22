"""VPS 端的邀请码、上传导入和服务端配置服务。"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from eiketsu_env.config import Settings, known_target_versions, latest_target_version, version_start_date
from eiketsu_env.db.models import (
    ServerApiToken,
    ServerInvite,
    ServerShareConfig,
    ServerUpload,
    ServerUser,
    SharedContributionPackage,
)
from eiketsu_env.db.session import make_session_factory
# 排行榜实现已拆到独立模块；这里保留旧导入路径，方便现有调用方平滑过渡。
from eiketsu_env.services.leaderboard import (
    RANK_SCOPE_ALL,
    RANK_SCOPE_KNIGHT_DOWN,
    RANK_SCOPE_KNIGHT_UP,
    RANK_SCOPE_LABELS,
    RANK_SCOPE_TRAVELER_DOWN,
    _clear_leaderboard_cache,
    _clear_leaderboard_snapshots,
    _load_leaderboard_matches,
    contributor_leaderboard,
    personal_leaderboard,
    prune_legacy_leaderboard_snapshots,
    public_leaderboard,
    public_leaderboard_matchup_matrix,
    public_leaderboard_page,
    refresh_public_leaderboard_materialized,
    refresh_public_leaderboard_snapshots,
)
from eiketsu_env.services.share import (
    DEFAULT_REPORT_FORMATS,
    DEFAULT_REPORTS,
    SHARE_SCHEMA_VERSION,
    ShareConfig,
    assert_safe_contribution_payload,
    import_contributions,
    parse_contribution_package_text,
)
from eiketsu_env.utils import JST, sha256_text, utc_now


CONFIG_ROW_ID = 1


@dataclass(slots=True)
class InviteResult:
    code: str
    label: str
    status: str


@dataclass(slots=True)
class BindInviteResult:
    api_token: str
    token_prefix: str
    user_public_id: str
    contributor_name: str


@dataclass(slots=True)
class UploadResult:
    upload_id: int
    package_id: str
    content_hash: str
    status: str
    match_count: int
    imported_match_count: int
    already_uploaded: bool
    errors: list[dict[str, Any]]




class ServerAuthError(PermissionError):
    """客户端 token 无效或已撤销。"""


def get_server_config(settings: Settings, target_version: str = "") -> dict[str, Any]:
    factory = make_session_factory(settings)
    with factory() as session:
        row = session.get(ServerShareConfig, CONFIG_ROW_ID)
        if row is None:
            return {"configured": False, "schema_version": SHARE_SCHEMA_VERSION}
        stored_config = _row_to_share_config(row)
        current_config = _effective_share_config(_default_client_share_config(stored_config))
        available_versions = _available_client_target_versions(session, current_config.target_version, stored_config.target_version)
        requested_version = str(target_version or "").strip()
        config = (
            _client_share_config_for_version(session, stored_config, current_config, requested_version)
            if requested_version
            else current_config
        )
        return {
            "configured": True,
            **_config_to_payload(config),
            "current_target_version": current_config.target_version,
            "available_target_versions": available_versions,
        }


def set_server_config(
    settings: Settings,
    target_version: str,
    date_from: str,
    date_to: str,
    include_solo: bool = False,
    high_ranker_rank: int = 100,
    report_formats: list[str] | None = None,
    reports: list[str] | None = None,
) -> dict[str, Any]:
    config = ShareConfig(
        target_version=target_version,
        date_from=date_from,
        date_to=date_to,
        include_solo=include_solo,
        high_ranker_rank=high_ranker_rank,
        report_formats=report_formats or list(DEFAULT_REPORT_FORMATS),
        reports=reports or list(DEFAULT_REPORTS),
    )
    config.validate()
    factory = make_session_factory(settings)
    with factory() as session:
        row = session.get(ServerShareConfig, CONFIG_ROW_ID)
        if row is None:
            row = ServerShareConfig(id=CONFIG_ROW_ID)
            session.add(row)
        _apply_config_to_row(row, config)
        session.commit()
    _clear_leaderboard_snapshots(settings, target_version=config.target_version)
    return {"configured": True, **_config_to_payload(_effective_share_config(config))}


def _effective_share_config(config: ShareConfig, today: date | None = None) -> ShareConfig:
    effective_date_to = _effective_date_to(config, today=today)
    if effective_date_to == config.date_to:
        return config
    # date_to 以前需要手动每天推进；这里按日本时间自动扩到当天，避免客户端和榜单卡在旧日期。
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


def _default_client_share_config(config: ShareConfig) -> ShareConfig:
    latest_version = latest_target_version()
    latest_start = version_start_date(latest_version)
    current_start = version_start_date(config.target_version)
    if not latest_version or not latest_start or not current_start:
        return config
    if latest_version == config.target_version or latest_start <= current_start:
        return config
    return ShareConfig(
        schema_version=config.schema_version,
        target_version=latest_version,
        date_from=latest_start,
        date_to=latest_start,
        include_solo=config.include_solo,
        high_ranker_rank=config.high_ranker_rank,
        report_formats=list(config.report_formats),
        reports=list(config.reports),
    )


def _available_client_target_versions(session, current_version: str, stored_version: str) -> list[str]:
    versions = {version for version in known_target_versions() if version}
    versions.update(version for version in (current_version, stored_version) if version)
    uploaded_versions = session.scalars(select(ServerUpload.target_version).where(ServerUpload.target_version != "")).all()
    versions.update(str(version or "").strip() for version in uploaded_versions if str(version or "").strip())
    return sorted(versions, key=_client_version_sort_key, reverse=True)


def _client_version_sort_key(version: str) -> tuple[int, str, str]:
    start = version_start_date(version)
    return (1 if start else 0, start, version)


def _client_share_config_for_version(
    session,
    stored_config: ShareConfig,
    current_config: ShareConfig,
    target_version: str,
) -> ShareConfig:
    requested = str(target_version or "").strip()
    if requested == current_config.target_version:
        return current_config
    start = version_start_date(requested)
    if start:
        date_to = _known_version_date_to(requested, current_config.date_to)
        return _clone_share_config(current_config, requested, start, date_to)
    upload_range = _uploaded_version_date_range(session, requested)
    if upload_range is not None:
        return _clone_share_config(current_config, requested, upload_range[0], upload_range[1])
    if requested == stored_config.target_version:
        return _effective_share_config(stored_config)
    raise ValueError(f"未知目标版本：{requested}")


def _known_version_date_to(target_version: str, fallback_date_to: str) -> str:
    start = version_start_date(target_version)
    later_starts = [
        candidate_start
        for candidate in known_target_versions()
        for candidate_start in [version_start_date(candidate)]
        if candidate != target_version and candidate_start and start and candidate_start > start
    ]
    if later_starts:
        next_start = min(later_starts)
        return (date.fromisoformat(next_start) - timedelta(days=1)).isoformat()
    return max(start, fallback_date_to, _latest_collectable_game_date())


def _uploaded_version_date_range(session, target_version: str) -> tuple[str, str] | None:
    row = session.execute(
        select(func.min(ServerUpload.date_from), func.max(ServerUpload.date_to)).where(ServerUpload.target_version == target_version)
    ).one()
    date_from, date_to = str(row[0] or ""), str(row[1] or "")
    if not date_from or not date_to:
        return None
    return date_from, date_to


def _clone_share_config(config: ShareConfig, target_version: str, date_from: str, date_to: str) -> ShareConfig:
    selected = ShareConfig(
        schema_version=config.schema_version,
        target_version=target_version,
        date_from=date_from,
        date_to=date_to,
        include_solo=config.include_solo,
        high_ranker_rank=config.high_ranker_rank,
        report_formats=list(config.report_formats),
        reports=list(config.reports),
    )
    selected.validate()
    return selected


def create_invite(settings: Settings, label: str, code: str = "") -> InviteResult:
    factory = make_session_factory(settings)
    with factory() as session:
        requested_code = code.strip()
        if requested_code:
            if session.get(ServerInvite, requested_code) is not None:
                raise ValueError("邀请码已存在，请换一个自定义邀请码。")
            invite_code = requested_code
        else:
            invite_code = _new_invite_code()
            while session.get(ServerInvite, invite_code) is not None:
                invite_code = _new_invite_code()
        invite = ServerInvite(code=invite_code, label=label or "", status="active")
        session.add(invite)
        session.commit()
        return InviteResult(code=invite.code, label=invite.label, status=invite.status)


def list_invites(settings: Settings, status: str = "all", limit: int = 100) -> dict[str, Any]:
    normalized_status = str(status or "all").strip().lower()
    if normalized_status not in {"all", "active", "used"}:
        normalized_status = "all"
    bounded_limit = max(1, min(int(limit or 100), 500))
    factory = make_session_factory(settings)
    with factory() as session:
        statement = (
            select(ServerInvite, ServerUser.contributor_name)
            .outerjoin(ServerUser, ServerInvite.used_by_user_id == ServerUser.id)
            .order_by(ServerInvite.created_at.desc(), ServerInvite.code.desc())
            .limit(bounded_limit)
        )
        if normalized_status != "all":
            statement = statement.where(ServerInvite.status == normalized_status)
        rows = session.execute(statement).all()
        items = [
            {
                "code": invite.code,
                "label": invite.label,
                "status": invite.status,
                "created_at": _datetime_to_text(invite.created_at),
                "used_at": _datetime_to_text(invite.used_at),
                "used_by": contributor_name or "",
            }
            for invite, contributor_name in rows
        ]
        counts = dict(session.execute(select(ServerInvite.status, func.count()).group_by(ServerInvite.status)).all())
        return {
            "status": normalized_status,
            "limit": bounded_limit,
            "items": items,
            "counts": {
                "all": sum(int(value) for value in counts.values()),
                "active": int(counts.get("active") or 0),
                "used": int(counts.get("used") or 0),
            },
        }


def bind_invite(settings: Settings, invite_code: str, contributor_name: str) -> BindInviteResult:
    factory = make_session_factory(settings)
    with factory() as session:
        invite = session.get(ServerInvite, invite_code.strip())
        if invite is None or invite.status != "active" or invite.used_at is not None:
            raise ValueError("邀请码无效或已被使用")

        # 用户昵称只用于本人页面和你的后台识别，公开聚合页不展示。
        user = ServerUser(
            public_id=f"u_{secrets.token_hex(8)}",
            contributor_name=contributor_name.strip() or "anonymous",
            label=invite.label,
            last_seen_at=utc_now(),
        )
        token = secrets.token_urlsafe(32)
        token_row = ServerApiToken(
            user=user,
            token_hash=_token_hash(token),
            token_prefix=token[:8],
        )
        invite.status = "used"
        invite.used_at = utc_now()
        invite.used_by_user_id = user.id
        session.add_all([user, token_row])
        session.flush()
        invite.used_by_user_id = user.id
        session.commit()
        return BindInviteResult(token, token[:8], user.public_id, user.contributor_name)


def import_uploaded_package(settings: Settings, api_token: str, package_text: str) -> UploadResult:
    assert_safe_contribution_payload(package_text)
    manifest, _records = parse_contribution_package_text(package_text)
    content_hash = sha256_text(package_text)
    package_id = str(manifest["package_id"])

    factory = make_session_factory(settings)
    with factory() as session:
        user = authenticate_token(session, api_token)
        existing = session.scalar(
            select(ServerUpload).where(
                ServerUpload.user_id == user.id,
                ServerUpload.content_hash == content_hash,
            )
        )
        if existing is not None:
            user.last_seen_at = utc_now()
            session.commit()
            return _upload_result(existing, already_uploaded=True)

    upload_path = _upload_storage_path(settings, content_hash)
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_text(package_text, encoding="utf-8")
    import_result = import_contributions(settings, [upload_path])

    with factory() as session:
        user = authenticate_token(session, api_token)
        package = session.get(SharedContributionPackage, package_id)
        status = package.status if package else ("failed" if import_result.errors else "completed")
        errors = package.error_summary_json if package else import_result.errors
        upload = ServerUpload(
            user_id=user.id,
            package_id=package_id if package else None,
            content_hash=content_hash,
            target_version=str(manifest.get("target_version") or ""),
            date_from=str(manifest.get("date_from") or ""),
            date_to=str(manifest.get("date_to") or ""),
            status=status,
            match_count=package.match_count if package else int(manifest.get("match_count") or 0),
            imported_match_count=package.imported_match_count if package else import_result.matches_imported,
            error_summary_json=errors,
        )
        user.last_seen_at = utc_now()
        session.add(upload)
        session.commit()
        result = _upload_result(upload, already_uploaded=False)
    _clear_leaderboard_snapshots(settings, target_version=str(manifest.get("target_version") or ""), clear_runs=False)
    return result


def list_my_uploads(settings: Settings, api_token: str) -> dict[str, Any]:
    factory = make_session_factory(settings)
    with factory() as session:
        user = authenticate_token(session, api_token)
        uploads = session.scalars(
            select(ServerUpload)
            .where(ServerUpload.user_id == user.id)
            .order_by(ServerUpload.created_at.desc(), ServerUpload.id.desc())
        ).all()
        session.commit()
        return {
            "user_public_id": user.public_id,
            "contributor_name": user.contributor_name,
            "uploads": [_upload_to_payload(upload) for upload in uploads],
        }




def authenticate_token(session, api_token: str) -> ServerUser:
    token_value = api_token.strip()
    if not token_value:
        raise ServerAuthError("缺少 API token")
    token_row = session.scalar(
        select(ServerApiToken)
        .options(selectinload(ServerApiToken.user))
        .where(ServerApiToken.token_hash == _token_hash(token_value))
        .where(ServerApiToken.revoked_at.is_(None))
    )
    if token_row is None:
        raise ServerAuthError("API token 无效")
    token_row.last_used_at = utc_now()
    token_row.user.last_seen_at = utc_now()
    return token_row.user


def _require_config(session) -> ShareConfig:
    row = session.get(ServerShareConfig, CONFIG_ROW_ID)
    if row is None:
        raise ValueError("服务端还没有配置 target_version/date_from/date_to")
    config = _row_to_share_config(row)
    config.validate()
    return config


def _row_to_share_config(row: ServerShareConfig) -> ShareConfig:
    return ShareConfig(
        schema_version=row.schema_version,
        target_version=row.target_version,
        date_from=row.date_from,
        date_to=row.date_to,
        include_solo=bool(row.include_solo),
        high_ranker_rank=row.high_ranker_rank,
        report_formats=list(row.report_formats_json or DEFAULT_REPORT_FORMATS),
        reports=list(row.reports_json or DEFAULT_REPORTS),
    )


def _apply_config_to_row(row: ServerShareConfig, config: ShareConfig) -> None:
    row.schema_version = config.schema_version
    row.target_version = config.target_version
    row.date_from = config.date_from
    row.date_to = config.date_to
    row.include_solo = 1 if config.include_solo else 0
    row.high_ranker_rank = config.high_ranker_rank
    row.report_formats_json = config.report_formats
    row.reports_json = config.reports


def _config_to_payload(config: ShareConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "target_version": config.target_version,
        "date_from": config.date_from,
        "date_to": config.date_to,
        "include_solo": config.include_solo,
        "high_ranker_rank": config.high_ranker_rank,
        "report_formats": config.report_formats,
        "reports": config.reports,
    }


def _upload_result(upload: ServerUpload, already_uploaded: bool) -> UploadResult:
    return UploadResult(
        upload_id=upload.id,
        package_id=upload.package_id or "",
        content_hash=upload.content_hash,
        status=upload.status,
        match_count=upload.match_count,
        imported_match_count=upload.imported_match_count,
        already_uploaded=already_uploaded,
        errors=list(upload.error_summary_json or []),
    )


def _upload_to_payload(upload: ServerUpload) -> dict[str, Any]:
    return {
        "id": upload.id,
        "package_id": upload.package_id,
        "content_hash": upload.content_hash,
        "target_version": upload.target_version,
        "date_from": upload.date_from,
        "date_to": upload.date_to,
        "status": upload.status,
        "match_count": upload.match_count,
        "imported_match_count": upload.imported_match_count,
        "errors": upload.error_summary_json,
        "created_at": upload.created_at.isoformat(timespec="seconds") if upload.created_at else "",
    }


def _upload_storage_path(settings: Settings, content_hash: str) -> Path:
    return settings.data_dir / "server_uploads" / content_hash[:2] / f"{content_hash}.jsonl"




def _token_hash(token: str) -> str:
    return sha256_text(f"server-token:{token}")


def _datetime_to_text(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _new_invite_code() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
