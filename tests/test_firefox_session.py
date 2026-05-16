from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eiketsu_env.config import Settings
from eiketsu_env.services.firefox_session import doctor_firefox, load_firefox_cookiejar


def _create_cookie_db(profile: Path) -> None:
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / "cookies.sqlite")
    try:
        connection.execute(
            """
            CREATE TABLE moz_cookies (
                host TEXT,
                path TEXT,
                name TEXT,
                value TEXT,
                expiry INTEGER,
                isSecure INTEGER,
                isHttpOnly INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("eiketsu-taisen.net", "/", "sid", "abc", 1770000000000, 1, 1),
        )
        connection.execute(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("example.com", "/", "ignored", "no", 1770000000, 0, 0),
        )
        connection.commit()
    finally:
        connection.close()


def test_load_firefox_cookiejar_filters_domains_and_normalizes_expiry(tmp_path):
    profile = tmp_path / "ff"
    _create_cookie_db(profile)
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:", firefox_profile=profile)

    jar = load_firefox_cookiejar(settings)
    cookies = list(jar)

    assert len(cookies) == 1
    assert cookies[0].name == "sid"
    assert cookies[0].expires == 1770000000


def test_doctor_firefox_reports_cookie_count(tmp_path):
    profile = tmp_path / "ff"
    _create_cookie_db(profile)
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:", firefox_profile=profile)

    result = doctor_firefox(settings)

    assert result.profile_exists is True
    assert result.cookie_db_exists is True
    assert result.loaded_cookie_count == 1


def test_doctor_firefox_without_explicit_profile_is_user_readable(tmp_path):
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:")

    result = doctor_firefox(settings)

    assert result.profile_exists is False
    assert result.cookie_db_exists is False
    assert result.loaded_cookie_count == 0


def test_load_firefox_cookiejar_without_profile_does_not_use_local_fallback(tmp_path):
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:")

    with pytest.raises(FileNotFoundError):
        load_firefox_cookiejar(settings)
