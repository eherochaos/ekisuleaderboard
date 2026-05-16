from __future__ import annotations

from pathlib import Path

from eiketsu_env.config import Settings
from eiketsu_env.services.client_update import (
    client_exe_download_name,
    client_update_payload,
    publish_client_update,
    resolve_client_update_file,
    version_is_newer,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(root_dir=tmp_path, db_url=f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}")


def test_publish_client_update_writes_manifest_and_download_payload(tmp_path):
    settings = _settings(tmp_path)
    exe = tmp_path / "EiketsuCollector.exe"
    exe.write_bytes(b"fake-exe")

    result = publish_client_update(settings, exe, "0.1.2", notes="修复登录提示")
    payload = client_update_payload(settings, current_version="0.1.1", base_url="http://127.0.0.1:8000")
    resolved_path, manifest = resolve_client_update_file(settings)

    assert result.latest_version == "0.1.2"
    assert result.stored_path.name == "EiketsuCollector_0.1.2.exe"
    assert resolved_path == result.stored_path
    assert manifest["sha256"] == result.sha256
    assert manifest["download_name"] == "EiketsuCollector_0.1.2.exe"
    assert payload["configured"] is True
    assert payload["update_available"] is True
    assert payload["download_url"] == "http://127.0.0.1:8000/downloads/EiketsuCollector_0.1.2.exe"
    assert payload["download_name"] == "EiketsuCollector_0.1.2.exe"
    assert payload["notes"] == "修复登录提示"


def test_client_update_reports_no_manifest(tmp_path):
    payload = client_update_payload(_settings(tmp_path), current_version="0.1.1")

    assert payload["configured"] is False
    assert payload["update_available"] is False


def test_publish_client_update_rejects_non_exe(tmp_path):
    source = tmp_path / "readme.txt"
    source.write_text("not exe", encoding="utf-8")

    try:
        publish_client_update(_settings(tmp_path), source, "0.1.2")
    except ValueError as exc:
        assert ".exe" in str(exc)
    else:
        raise AssertionError("non-exe update should fail")


def test_client_exe_download_name_sanitizes_version():
    assert client_exe_download_name("0.1.2 beta") == "EiketsuCollector_0.1.2_beta.exe"


def test_version_compare_handles_multi_digit_versions():
    assert version_is_newer("0.1.10", "0.1.2") is True
    assert version_is_newer("0.1.2", "0.1.10") is False
    assert version_is_newer("0.1.2", "0.1.2") is False
