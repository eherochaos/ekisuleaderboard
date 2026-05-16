from __future__ import annotations

import json
import sqlite3
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from eiketsu_env.config import Settings
from eiketsu_env.services import browser_session
from eiketsu_env.services.browser_session import (
    BrowserAuthError,
    BrowserCookieResult,
    BrowserProfileCandidate,
    browser_kind_from_progid,
    create_member_session,
    discover_chromium_profiles,
    discover_firefox_profiles,
    doctor_browser,
    load_browser_cookiejar,
    load_chromium_cookiejar,
    open_login_url,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(root_dir=tmp_path, db_url="sqlite:///:memory:", firefox_profile=tmp_path / "ff")


def _make_cookie(name: str = "sid", value: str = "abc") -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain="eiketsu-taisen.net",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": "True"},
        rfc2109=False,
    )


def _make_devtools_cookie(name: str = "sid", value: str = "abc") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": "eiketsu-taisen.net",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": -1,
    }


class _FakeDevToolsConnection:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _create_firefox_profile(profile: Path, cookie_count: int = 1) -> None:
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
        for index in range(cookie_count):
            connection.execute(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("eiketsu-taisen.net", "/", f"sid{index}", f"abc{index}", 1770000000, 1, 1),
            )
        connection.commit()
    finally:
        connection.close()


def _create_chromium_profile(user_data: Path, profile_name: str, cookie_count: int) -> Path:
    profile = user_data / profile_name
    cookie_db = profile / "Network" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(cookie_db)
    try:
        connection.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                path TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                expires_utc INTEGER,
                is_secure INTEGER,
                is_httponly INTEGER
            )
            """
        )
        for index in range(cookie_count):
            connection.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("eiketsu-taisen.net", "/", f"sid{index}", "", b"encrypted", 13_300_000_000_000_000, 1, 1),
            )
        connection.commit()
    finally:
        connection.close()
    return profile


def test_firefox_profiles_ini_default_profile_is_discovered(tmp_path, monkeypatch):
    firefox_root = tmp_path / "Mozilla" / "Firefox"
    default_profile = firefox_root / "Profiles" / "default-release"
    other_profile = firefox_root / "Profiles" / "other"
    _create_firefox_profile(default_profile)
    _create_firefox_profile(other_profile)
    (firefox_root / "profiles.ini").write_text(
        "\n".join(
            [
                "[Profile0]",
                "Name=default-release",
                "IsRelative=1",
                "Path=Profiles/default-release",
                "Default=1",
                "[Profile1]",
                "Name=other",
                "IsRelative=1",
                "Path=Profiles/other",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))

    profiles = discover_firefox_profiles(_settings(tmp_path))

    assert profiles[0].profile_path == default_profile
    assert profiles[0].is_default is True


def test_firefox_discovery_without_profiles_ini_does_not_use_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-roaming"))
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:")

    profiles = discover_firefox_profiles(settings)

    assert profiles == []


def test_chromium_cookiejar_uses_fake_decryptor(tmp_path):
    user_data = tmp_path / "Chrome" / "User Data"
    profile = _create_chromium_profile(user_data, "Default", 1)
    (user_data / "Local State").write_text(json.dumps({"os_crypt": {}}), encoding="utf-8")
    candidate = BrowserProfileCandidate("chrome", profile, profile / "Network" / "Cookies", user_data / "Local State")

    jar = load_chromium_cookiejar(_settings(tmp_path), candidate, decryptor=lambda _value, _key: "decrypted")

    cookies = list(jar)
    assert len(cookies) == 1
    assert cookies[0].name == "sid0"
    assert cookies[0].value == "decrypted"


def test_chromium_v20_cookie_reports_protected_login_data(tmp_path):
    user_data = tmp_path / "Edge" / "User Data"
    profile = _create_chromium_profile(user_data, "Default", 1)
    connection = sqlite3.connect(profile / "Network" / "Cookies")
    try:
        connection.execute("UPDATE cookies SET encrypted_value = ?", (b"v20-protected",))
        connection.commit()
    finally:
        connection.close()
    (user_data / "Local State").write_text(json.dumps({"os_crypt": {}}), encoding="utf-8")
    candidate = BrowserProfileCandidate("edge", profile, profile / "Network" / "Cookies", user_data / "Local State")

    try:
        load_chromium_cookiejar(_settings(tmp_path), candidate)
    except BrowserAuthError as exc:
        assert "新版登录数据受浏览器保护" in str(exc)
    else:
        raise AssertionError("v20 Chromium login data should not be treated as readable")


def test_auto_chrome_profile_selects_most_domain_cookies(tmp_path, monkeypatch):
    local_app = tmp_path / "LocalAppData"
    user_data = local_app / "Google" / "Chrome" / "User Data"
    _create_chromium_profile(user_data, "Default", 1)
    _create_chromium_profile(user_data, "Profile 1", 2)
    (user_data / "Local State").write_text(json.dumps({"profile": {"last_used": "Default"}}), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    result = load_browser_cookiejar(_settings(tmp_path), "chrome", decryptor=lambda _value, _key: "decrypted")

    assert result.source == "chrome"
    assert result.profile_path.name == "Profile 1"
    assert result.cookie_count == 2


def test_custom_chromium_profile_uses_parent_local_state(tmp_path):
    user_data = tmp_path / "Chrome" / "User Data"
    profile = _create_chromium_profile(user_data, "Default", 1)
    (user_data / "Local State").write_text(json.dumps({"os_crypt": {}}), encoding="utf-8")
    settings = Settings(root_dir=tmp_path, db_url="sqlite:///:memory:", firefox_profile=tmp_path / "ff", browser_profile=profile)

    [candidate] = discover_chromium_profiles(settings, "chrome", tmp_path / "unused")

    assert candidate.profile_path == profile
    assert candidate.local_state == user_data / "Local State"


def test_default_browser_progid_mapping():
    assert browser_kind_from_progid("MSEdgeHTM") == "edge"
    assert browser_kind_from_progid("ChromeHTML") == "chrome"
    assert browser_kind_from_progid("BraveHTML") == "brave"
    assert browser_kind_from_progid("FirefoxURL-308046B0AF4A39CB") == "firefox"
    assert browser_kind_from_progid("UnknownHTML") == ""


def test_doctor_browser_reports_missing_login(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-roaming"))

    result = doctor_browser(_settings(tmp_path), "chrome")

    assert result["ok"] is False
    assert "打开登录页" in result["message"]
    assert "Google Chrome" in result["message"]


def test_create_member_session_opens_login_and_retries(tmp_path, monkeypatch):
    jar = CookieJar()
    jar.set_cookie(_make_cookie())
    calls: list[str] = []

    def fake_load(settings, auth_source=None, decryptor=None):
        calls.append("load")
        if calls.count("load") == 1:
            raise BrowserAuthError("missing")
        return BrowserCookieResult("chrome", tmp_path / "profile", jar, 1, "ok")

    monkeypatch.setattr(browser_session, "detect_default_browser_kind", lambda: "")
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda _browser: [])
    monkeypatch.setattr(browser_session, "load_browser_cookiejar", fake_load)
    session = create_member_session(
        _settings(tmp_path),
        "auto",
        interactive=True,
        open_browser=lambda url: calls.append(f"open:{url}"),
        input_func=lambda prompt: calls.append(f"input:{prompt}") or "",
    )

    assert session.cookie_result.cookie_count == 1
    assert calls[0] == "load"
    assert calls[1].startswith("open:")
    assert calls[2].startswith("input:")
    assert calls[3] == "load"


def test_open_login_url_uses_selected_edge_executable(tmp_path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"fake")
    launches: list[list[str]] = []
    default_opened: list[str] = []
    appdata = tmp_path / "Roaming"

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda browser: [edge] if browser == "edge" else [])

    opened = open_login_url(
        _settings(tmp_path),
        "edge",
        open_default=lambda url: default_opened.append(url),
        launch_process=lambda args: launches.append(list(args)),
    )

    assert opened == "edge"
    assert launches[0][0] == str(edge)
    assert "--remote-debugging-port=49381" in launches[0]
    assert f"--user-data-dir={appdata / 'EiketsuCollector' / 'browser_login' / 'edge'}" in launches[0]
    assert launches[0][-1] == "https://eiketsu-taisen.net/members/"
    assert default_opened == []


def test_open_login_url_auto_uses_default_browser_when_default_is_unknown(tmp_path, monkeypatch):
    opened_urls: list[str] = []
    monkeypatch.setattr(browser_session, "detect_default_browser_kind", lambda: "")
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda _browser: [])

    opened = open_login_url(_settings(tmp_path), "auto", open_default=lambda url: opened_urls.append(url))

    assert opened == "default-browser"
    assert opened_urls == ["https://eiketsu-taisen.net/members/"]


def test_open_login_url_auto_uses_installed_chrome_when_default_is_unknown(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"fake")
    launches: list[list[str]] = []

    monkeypatch.setenv("EIKETSU_BROWSER_LOGIN_DIR", str(tmp_path / "login"))
    monkeypatch.setattr(browser_session, "detect_default_browser_kind", lambda: "")
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda browser: [chrome] if browser == "chrome" else [])

    opened = open_login_url(_settings(tmp_path), "auto", launch_process=lambda args: launches.append(list(args)))

    assert opened == "chrome"
    assert launches[0][0] == str(chrome)
    assert "--remote-debugging-port=49382" in launches[0]


def test_open_login_url_auto_uses_detected_edge_login_window(tmp_path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"fake")
    launches: list[list[str]] = []
    default_opened: list[str] = []

    monkeypatch.setenv("EIKETSU_BROWSER_LOGIN_DIR", str(tmp_path / "login"))
    monkeypatch.setattr(browser_session, "detect_default_browser_kind", lambda: "edge")
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda browser: [edge] if browser == "edge" else [])

    opened = open_login_url(
        _settings(tmp_path),
        "auto",
        open_default=lambda url: default_opened.append(url),
        launch_process=lambda args: launches.append(list(args)),
    )

    assert opened == "edge"
    assert launches[0][0] == str(edge)
    assert next(item for item in launches[0] if item.startswith("--user-data-dir=")).endswith(r"\login\edge")
    assert default_opened == []


def test_open_login_url_supports_brave_login_window(tmp_path, monkeypatch):
    brave = tmp_path / "brave.exe"
    brave.write_bytes(b"fake")
    launches: list[list[str]] = []

    monkeypatch.setenv("EIKETSU_BROWSER_LOGIN_DIR", str(tmp_path / "login"))
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda browser: [brave] if browser == "brave" else [])

    opened = open_login_url(_settings(tmp_path), "brave", launch_process=lambda args: launches.append(list(args)))

    assert opened == "brave"
    assert launches[0][0] == str(brave)
    assert "--remote-debugging-port=49383" in launches[0]


def test_doctor_browser_uses_live_edge_session(tmp_path, monkeypatch):
    jar = CookieJar()
    jar.set_cookie(_make_cookie())

    def fake_live(settings, browser):
        return BrowserCookieResult(browser, tmp_path / ".tmp" / "browser_login" / browser, jar, 1, "live ok")

    monkeypatch.setattr(browser_session, "load_live_browser_cookiejar", fake_live)

    result = doctor_browser(_settings(tmp_path), "edge")

    assert result["ok"] is True
    assert result["message"] == "live ok"
    assert result["loaded_cookie_count"] == 1
    assert result["candidates"][0]["domain_cookie_count"] == 1


def test_doctor_browser_auto_uses_live_default_edge_session(tmp_path, monkeypatch):
    jar = CookieJar()
    jar.set_cookie(_make_cookie())
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"fake")

    def fake_live(settings, browser):
        return BrowserCookieResult(browser, tmp_path / ".tmp" / "browser_login" / browser, jar, 1, "live ok")

    monkeypatch.setattr(browser_session, "detect_default_browser_kind", lambda: "edge")
    monkeypatch.setattr(browser_session, "_browser_executable_candidates", lambda browser: [edge] if browser == "edge" else [])
    monkeypatch.setattr(browser_session, "load_live_browser_cookiejar", fake_live)

    result = doctor_browser(_settings(tmp_path), "auto")

    assert result["ok"] is True
    assert result["auth_source"] == "edge"
    assert result["message"] == "live ok"


def test_doctor_browser_edge_prompts_open_login_without_offline_cookie_jargon(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser_session,
        "load_live_browser_cookiejar",
        lambda _settings, _browser: (_ for _ in ()).throw(BrowserAuthError("请先点击“打开登录页”")),
    )

    result = doctor_browser(_settings(tmp_path), "edge")

    assert result["ok"] is False
    assert "打开登录页" in result["message"]
    assert result["candidates"][0]["domain_cookie_count"] == 0
    assert "cookie" not in result["message"].lower()


def test_live_browser_cookiejar_requires_member_api_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_session, "_devtools_page_websocket_url", lambda _port, _url: "ws://test")
    monkeypatch.setattr(browser_session, "_DevToolsConnection", _FakeDevToolsConnection)
    monkeypatch.setattr(browser_session, "_devtools_cookies", lambda _devtools: [_make_devtools_cookie()])
    monkeypatch.setattr(
        browser_session.BrowserMemberSession,
        "fetch_text",
        lambda self, url, referer=None, timeout=30: ("<html>login page</html>", "https://eiketsu-taisen.net/members/"),
    )

    try:
        browser_session.load_live_browser_cookiejar(_settings(tmp_path), "edge")
    except BrowserAuthError as exc:
        assert "还没有完成会员区登录" in str(exc) or "还没有确认会员区登录" in str(exc)
    else:
        raise AssertionError("普通目标域 cookie 不应该被当成已登录")


def test_live_browser_cookiejar_accepts_confirmed_member_api(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_session, "_devtools_page_websocket_url", lambda _port, _url: "ws://test")
    monkeypatch.setattr(browser_session, "_DevToolsConnection", _FakeDevToolsConnection)
    monkeypatch.setattr(browser_session, "_devtools_cookies", lambda _devtools: [_make_devtools_cookie()])
    monkeypatch.setattr(
        browser_session.BrowserMemberSession,
        "fetch_text",
        lambda self, url, referer=None, timeout=30: (
            json.dumps({"follow": []}),
            "https://eiketsu-taisen.net/members/follow/api/followlist",
        ),
    )

    result = browser_session.load_live_browser_cookiejar(_settings(tmp_path), "edge")

    assert result.cookie_count == 1
    assert result.message == "会员区登录已确认，可以同步"
