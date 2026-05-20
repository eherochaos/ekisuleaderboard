from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from eiketsu_env.config import Settings
from eiketsu_env.db.base import Base
from eiketsu_env.db.models import RawSnapshot
from eiketsu_env.db.session import make_engine
from eiketsu_env.services import client_upload
from eiketsu_env.services.client_upload import (
    apply_client_date_override,
    bind_client,
    check_client_update,
    cleanup_raw_snapshots,
    fetch_client_share_config,
    fetch_client_share_config_state,
    minimum_client_date_from,
    save_client_config,
    sync_client,
)
from eiketsu_env.services.collector import CollectResult
from eiketsu_env.services.repository import EnvRepository


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root_dir=tmp_path,
        db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        firefox_profile=tmp_path / "ff",
        card_catalog_path=tmp_path / "cards.json",
    )


def _detail() -> dict:
    return {
        "detail_url": "https://eiketsu-taisen.net/members/history/detail?f=586",
        "url": "https://eiketsu-taisen.net/members/history/detail?f=586",
        "follow_id": "586",
        "played_at": "2026-05-11 12:34",
        "date": "2026-05-11 12:34",
        "mode": "全国対戦",
        "version": "Ver.client",
        "result": "win",
        "replay_id": "client-replay",
        "castle_breakdown": {"rows": [{"player": "20.00%", "enemy": "100.00%"}]},
        "timeline_labels": ["開幕", "終了"],
        "timeline_data": {"castle": {"player": [100, 80], "enemy": [100, 0]}},
        "players": [
            {
                "side_index": 1,
                "role": "player",
                "player_name": "A",
                "follow_id": "586",
                "result": "win",
                "castle_rate": "80.00%",
                "deck_ids": ["card-a", "card-b"],
                "profile": {},
            }
        ],
    }


def test_bind_client_saves_local_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    transport = _FakeTransport()
    settings = _settings(tmp_path)

    result = bind_client(settings, "http://127.0.0.1:8000/", "INVITE", "alice", transport=transport)

    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert result.server_url == "http://127.0.0.1:8000"
    assert payload["api_token"] == "token-secret"
    assert payload["contributor"] == "alice"
    assert transport.calls[0][0] == "POST"


def test_sync_client_collects_exports_safe_jsonl_and_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    settings = _settings(tmp_path)
    save_client_config(
        settings,
        client_upload.ClientConfig(
            server_url="http://127.0.0.1:8000",
            api_token="token-secret",
            contributor="alice",
            user_public_id="u_test",
        ),
    )
    transport = _FakeTransport()

    def fake_collect(settings, date_from, date_to, **kwargs):
        assert kwargs["interactive_auth"] is False
        assert kwargs["save_raw_snapshots"] is False
        assert kwargs["skip_existing"] is True
        assert kwargs["skip_inactive"] is True
        assert kwargs["concurrency_profile"] == "aggressive"
        engine = make_engine(settings)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            repo = EnvRepository(session, settings)
            repo.upsert_match_detail(_detail())
            session.commit()
        return CollectResult(1, "completed", {"matches": 1}, [])

    monkeypatch.setattr(client_upload, "collect_follow", fake_collect)

    result = sync_client(settings, interactive_auth=False, transport=transport)

    upload_payload = transport.upload_payload
    assert result.upload["status"] == "completed"
    assert upload_payload is not None
    assert "package_text" in upload_payload
    serialized = upload_payload["package_text"]
    assert "cookies" not in serialized
    assert "local_path" not in serialized
    assert "raw_html" not in serialized
    assert (settings.raw_dir).exists() is False
    assert result.package_path.exists() is False


def test_sync_client_allows_user_date_override_and_clamps_to_server_start(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    settings = _settings(tmp_path)
    save_client_config(
        settings,
        client_upload.ClientConfig(
            server_url="http://127.0.0.1:8000",
            api_token="token-secret",
            contributor="alice",
            user_public_id="u_test",
        ),
    )
    transport = _FakeTransport()
    seen_dates: list[tuple[str, str]] = []

    def fake_collect(settings, date_from, date_to, **kwargs):
        seen_dates.append((date_from, date_to))
        engine = make_engine(settings)
        Base.metadata.create_all(engine)
        return CollectResult(1, "completed", {"matches": 0}, [])

    monkeypatch.setattr(client_upload, "collect_follow", fake_collect)

    sync_client(settings, interactive_auth=False, transport=transport, date_from="2026-05-01", date_to="2026-05-11")

    assert seen_dates == [("2026-05-10", "2026-05-11")]


def test_sync_client_clamps_user_date_to_server_window(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    settings = _settings(tmp_path)
    save_client_config(
        settings,
        client_upload.ClientConfig(
            server_url="http://127.0.0.1:8000",
            api_token="token-secret",
            contributor="alice",
            user_public_id="u_test",
        ),
    )
    transport = _FakeTransport()
    seen_dates: list[tuple[str, str]] = []

    def fake_collect(settings, date_from, date_to, **kwargs):
        seen_dates.append((date_from, date_to))
        engine = make_engine(settings)
        Base.metadata.create_all(engine)
        return CollectResult(1, "completed", {"matches": 0}, [])

    monkeypatch.setattr(client_upload, "collect_follow", fake_collect)

    sync_client(settings, interactive_auth=False, transport=transport, date_from="2026-05-11", date_to="2026-05-20")

    assert seen_dates == [("2026-05-11", "2026-05-12")]


def test_fetch_client_share_config_can_request_target_version(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    settings = _settings(tmp_path)
    save_client_config(
        settings,
        client_upload.ClientConfig(
            server_url="http://127.0.0.1:8000",
            api_token="token-secret",
            contributor="alice",
            user_public_id="u_test",
        ),
    )
    transport = _FakeTransport()

    config = fetch_client_share_config(settings, transport=transport, target_version="Ver.old")
    state = fetch_client_share_config_state(settings, transport=transport, target_version="Ver.old")

    assert config.target_version == "Ver.old"
    assert config.date_from == "2026-04-22"
    assert config.date_to == "2026-05-19"
    assert state.current_target_version == "Ver.client"
    assert state.available_target_versions == ["Ver.client", "Ver.old"]
    assert transport.calls[-2][1].endswith("/api/v1/config?target_version=Ver.old")


def test_client_date_override_rejects_date_before_effective_start():
    config = client_upload.ShareConfig(target_version="Ver.client", date_from="2026-05-10", date_to="2026-05-12")

    assert minimum_client_date_from(config) == "2026-05-10"
    effective = apply_client_date_override(config, date_from="2026-05-11", date_to="2026-05-12")
    assert effective.date_from == "2026-05-11"
    clamped = apply_client_date_override(config, date_from="2026-05-11", date_to="2026-05-20")
    assert clamped.date_to == "2026-05-12"

    try:
        apply_client_date_override(config, date_from="2026-05-01", date_to="2026-05-09")
    except ValueError as exc:
        assert "结束日期不能早于起始日期 2026-05-10" in str(exc)
    else:
        raise AssertionError("date_to before version start should fail")


def test_cleanup_raw_snapshots_removes_files_and_rows(tmp_path):
    settings = _settings(tmp_path)
    raw_file = settings.raw_dir / "2026-05-10" / "detail" / "sample.html"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_text("<html>raw</html>", encoding="utf-8")
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            RawSnapshot(
                source_kind="detail",
                source_url="https://example.test/detail",
                local_path=str(raw_file),
                content_hash="abc",
                parser_version="test",
            )
        )
        session.commit()

    result = cleanup_raw_snapshots(settings)

    assert result.files_removed == 1
    assert result.bytes_removed > 0
    assert result.rows_removed == 1
    assert settings.raw_dir.exists() is False
    with Session(engine) as session:
        assert session.query(RawSnapshot).count() == 0


def test_check_client_update_uses_server_update_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("EIKETSU_CLIENT_CONFIG_DIR", str(tmp_path / "client-config"))
    settings = _settings(tmp_path)
    save_client_config(
        settings,
        client_upload.ClientConfig(
            server_url="http://127.0.0.1:8000",
            api_token="token-secret",
            contributor="alice",
            user_public_id="u_test",
        ),
    )
    transport = _FakeTransport()

    result = check_client_update(settings, current_version="0.1.1", transport=transport)

    assert result.update_available is True
    assert result.latest_version == "0.1.2"
    assert transport.calls[-1][1].endswith("/api/v1/client/update?current_version=0.1.1")


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []
        self.upload_payload: dict[str, Any] | None = None

    def request_json(self, method: str, url: str, payload: dict[str, Any] | None = None, token: str = "") -> dict[str, Any]:
        self.calls.append((method, url, payload, token))
        if url.endswith("/api/v1/auth/bind-invite"):
            return {"api_token": "token-secret", "token_prefix": "token-se", "user_public_id": "u_test"}
        if url.endswith("/api/v1/config"):
            return {
                "configured": True,
                "schema_version": "share_v1",
                "target_version": "Ver.client",
                "date_from": "2026-05-10",
                "date_to": "2026-05-12",
                "current_target_version": "Ver.client",
                "available_target_versions": ["Ver.client", "Ver.old"],
                "include_solo": False,
                "high_ranker_rank": 100,
                "report_formats": ["md"],
                "reports": ["overview"],
            }
        if url.endswith("/api/v1/config?target_version=Ver.old"):
            return {
                "configured": True,
                "schema_version": "share_v1",
                "target_version": "Ver.old",
                "date_from": "2026-04-22",
                "date_to": "2026-05-19",
                "current_target_version": "Ver.client",
                "available_target_versions": ["Ver.client", "Ver.old"],
                "include_solo": False,
                "high_ranker_rank": 100,
                "report_formats": ["md"],
                "reports": ["overview"],
            }
        if url.endswith("/api/v1/uploads"):
            self.upload_payload = payload
            assert token == "token-secret"
            return {
                "upload_id": 1,
                "package_id": "pkg",
                "content_hash": payload["content_hash"] if payload else "",
                "status": "completed",
                "match_count": 1,
                "imported_match_count": 1,
                "already_uploaded": False,
                "errors": [],
            }
        if "/api/v1/client/update?" in url:
            return {
                "configured": True,
                "current_version": "0.1.1",
                "latest_version": "0.1.2",
                "update_available": True,
                "download_url": "http://127.0.0.1:8000/downloads/EiketsuCollector_0.1.2.exe",
                "download_name": "EiketsuCollector_0.1.2.exe",
                "size_bytes": 123,
                "sha256": "abc",
                "notes": "test update",
                "published_at": "2026-05-16T00:00:00+00:00",
            }
        raise AssertionError(url)
