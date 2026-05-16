"""统一读取浏览器登录态，并用 CookieJar 访问英杰大战会员区。"""

from __future__ import annotations

import base64
import ctypes
import http.cookiejar
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import webbrowser
from configparser import ConfigParser
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from eiketsu_env.config import Settings

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) "
    "Gecko/20100101 Firefox/138.0"
)
SUPPORTED_AUTH_SOURCES = {"auto", "default-browser", "chrome", "edge", "brave", "firefox", "firefox-profile"}
CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600
CHROMIUM_PROTECTED_LOGIN_ERROR = "Chrome/Edge/Brave 新版登录数据受浏览器保护，当前无法直接读取"
LIVE_CHROMIUM_BROWSERS = ("edge", "chrome", "brave")
LIVE_BROWSER_PORTS = {"edge": 49381, "chrome": 49382, "brave": 49383}
LIVE_BROWSER_TIMEOUT_SECONDS = 2.0
BROWSER_NAME_FOR_USER = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "brave": "Brave"}


class BrowserAuthError(RuntimeError):
    """浏览器登录态不可用时抛出，供 CLI 转成可读提示。"""


@dataclass(slots=True)
class BrowserProfileCandidate:
    browser: str
    profile_path: Path
    cookie_db: Path
    local_state: Path | None = None
    is_default: bool = False
    order_score: int = 0
    cookie_count: int = 0
    error: str = ""


@dataclass(slots=True)
class BrowserCookieResult:
    source: str
    profile_path: Path
    cookiejar: CookieJar
    cookie_count: int
    message: str


@dataclass(slots=True)
class FirefoxDoctorResult:
    profile_exists: bool
    cookie_db_exists: bool
    loaded_cookie_count: int
    message: str


class BrowserMemberSession:
    """带浏览器 CookieJar 的轻量 HTTP 会话，供采集层复用。"""

    def __init__(self, settings: Settings, cookie_result: BrowserCookieResult):
        self.settings = settings
        self.cookiejar = cookie_result.cookiejar
        self.cookie_result = cookie_result
        self.user_agent = FIREFOX_USER_AGENT if cookie_result.source.startswith("firefox") else DEFAULT_USER_AGENT
        self.opener = build_opener(HTTPCookieProcessor(self.cookiejar))

    def fetch_text(self, url: str, referer: str | None = None, timeout: int = 30) -> tuple[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        }
        if referer:
            headers["Referer"] = referer
        request = Request(url, headers=headers)
        with self.opener.open(request, timeout=timeout) as response:
            payload = response.read()
            final_url = response.geturl()
            encoding = response.headers.get_content_charset() or "utf-8"
        return payload.decode(encoding, errors="replace"), final_url

    def post_form(self, url: str, fields: list[tuple[str, str]], referer: str | None = None, timeout: int = 30) -> tuple[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        request = Request(url, data=urlencode(fields).encode("utf-8"), headers=headers, method="POST")
        with self.opener.open(request, timeout=timeout) as response:
            payload = response.read()
            final_url = response.geturl()
            encoding = response.headers.get_content_charset() or "utf-8"
        return payload.decode(encoding, errors="replace"), final_url


def create_member_session(
    settings: Settings,
    auth_source: str | None = None,
    interactive: bool = False,
    open_browser: Callable[[str], Any] | None = None,
    input_func: Callable[[str], str] | None = None,
) -> BrowserMemberSession:
    """创建采集会话；交互模式会引导朋友登录后重试一次。"""

    selected_source = _normalize_auth_source(auth_source or settings.auth_source)
    try:
        cookie_result = load_browser_cookiejar(settings, selected_source)
    except BrowserAuthError:
        if not interactive:
            raise
        open_login_url(settings, selected_source, open_default=open_browser)
        (input_func or input)("已打开登录页。请在对应浏览器完成登录后按 Enter 继续...")
        cookie_result = load_browser_cookiejar(settings, selected_source)
    return BrowserMemberSession(settings, cookie_result)


def open_login_url(
    settings: Settings,
    auth_source: str | None = None,
    url: str | None = None,
    open_default: Callable[[str], Any] | None = None,
    launch_process: Callable[[Sequence[str]], Any] | None = None,
) -> str:
    """按用户选择的登录态来源打开登录页；Chrome/Edge/Brave 会优先使用可检测的专用登录窗口。"""

    selected_source = _normalize_auth_source(auth_source or settings.auth_source)
    target_url = str(url or settings.login_url)
    browser = _browser_kind_for_open(selected_source)
    if not browser:
        (open_default or webbrowser.open)(target_url)
        return "default-browser"

    executable = _browser_executable_path(browser)
    if executable:
        args = _browser_launch_args(settings, browser, executable, target_url)
        (launch_process or _launch_browser_process)(args)
        return browser

    controller_name = _webbrowser_controller_name(browser)
    if controller_name:
        try:
            webbrowser.get(controller_name).open(target_url)
            return browser
        except webbrowser.Error:
            pass

    if browser == "edge" and os.name == "nt":
        (open_default or webbrowser.open)(f"microsoft-edge:{target_url}")
        return browser

    (open_default or webbrowser.open)(target_url)
    return "default-browser"


def load_browser_cookiejar(
    settings: Settings,
    auth_source: str | None = None,
    decryptor: Callable[[bytes, bytes | None], str] | None = None,
) -> BrowserCookieResult:
    selected_source = _normalize_auth_source(auth_source or settings.auth_source)
    live_browser = _live_chromium_browser_for_auth_source(selected_source)
    if live_browser:
        try:
            return load_live_browser_cookiejar(settings, live_browser)
        except BrowserAuthError:
            pass
    candidates = _candidate_profiles(settings, selected_source)
    if not candidates:
        raise BrowserAuthError(f"没有发现可用浏览器 profile：auth_source={selected_source}")
    _score_candidates(settings, candidates)
    errors: list[str] = []
    for candidate in sorted(candidates, key=lambda item: (item.cookie_count, item.order_score), reverse=True):
        if candidate.cookie_count <= 0:
            continue
        try:
            if candidate.browser in {"chrome", "edge"}:
                jar = load_chromium_cookiejar(settings, candidate, decryptor=decryptor)
            else:
                jar = load_firefox_cookiejar(settings, candidate.profile_path)
        except Exception as exc:  # noqa: BLE001 - 继续尝试其它 profile，并把失败原因留给诊断。
            candidate.error = str(exc)
            errors.append(f"{candidate.browser}:{candidate.profile_path} -> {exc}")
            continue
        count = len(list(jar))
        if count > 0:
            return BrowserCookieResult(candidate.browser, candidate.profile_path, jar, count, "浏览器 cookies 可读取")
    detail = "；".join(errors) if errors else "目标域 cookies 不存在"
    raise BrowserAuthError(f"没有发现英杰大战登录态：{detail}")


def doctor_browser(settings: Settings, auth_source: str | None = None) -> dict[str, Any]:
    selected_source = _normalize_auth_source(auth_source or settings.auth_source)
    live_browser = _live_chromium_browser_for_auth_source(selected_source)
    if live_browser:
        return _doctor_live_chromium_browser(settings, live_browser)
    if selected_source in {"auto", "default-browser"}:
        return _doctor_missing_live_browser(settings, selected_source)
    candidates = _candidate_profiles(settings, selected_source)
    _score_candidates(settings, candidates)
    default_kind = detect_default_browser_kind()
    try:
        result = load_browser_cookiejar(settings, selected_source)
        ok = True
        message = result.message
        selected_profile = str(result.profile_path)
        cookie_count = result.cookie_count
    except Exception as exc:  # noqa: BLE001 - doctor 需要返回诊断而不是中断。
        ok = False
        message = str(exc)
        selected_profile = ""
        cookie_count = 0
    return {
        "ok": ok,
        "auth_source": selected_source,
        "default_browser": default_kind,
        "selected_profile": selected_profile,
        "loaded_cookie_count": cookie_count,
        "login_url": settings.login_url,
        "candidates": [
            {
                "browser": item.browser,
                "profile": str(item.profile_path),
                "cookie_db_exists": _path_exists(item.cookie_db),
                "domain_cookie_count": item.cookie_count,
                "is_default": item.is_default,
                "error": item.error,
            }
            for item in candidates
        ],
        "message": message,
    }


def _doctor_missing_live_browser(settings: Settings, auth_source: str) -> dict[str, Any]:
    default_kind = detect_default_browser_kind()
    installed = _installed_live_chromium_browsers()
    installed_names = "、".join(BROWSER_NAME_FOR_USER[item] for item in installed) or "未发现"
    message = (
        "自动检测需要本机安装 Chrome、Edge 或 Brave 中的一个。"
        f" 当前默认浏览器：{BROWSER_NAME_FOR_USER.get(default_kind, default_kind or '未知')}；"
        f" 已发现：{installed_names}。"
    )
    return {
        "ok": False,
        "auth_source": auth_source,
        "default_browser": default_kind,
        "selected_profile": "",
        "loaded_cookie_count": 0,
        "login_url": settings.login_url,
        "candidates": [
            {
                "browser": item,
                "profile": str(_live_browser_user_data_dir(settings, item)),
                "cookie_db_exists": True,
                "domain_cookie_count": 0,
                "is_default": item == default_kind,
                "error": "",
            }
            for item in installed
        ],
        "message": message,
    }


def _doctor_live_chromium_browser(settings: Settings, browser: str) -> dict[str, Any]:
    default_kind = detect_default_browser_kind()
    live_profile = _live_browser_user_data_dir(settings, browser)
    try:
        result = load_live_browser_cookiejar(settings, browser)
        ok = True
        message = result.message
        selected_profile = str(result.profile_path)
        cookie_count = result.cookie_count
        error = ""
    except Exception as exc:  # noqa: BLE001 - doctor 只负责把用户可操作的信息带回 GUI。
        ok = False
        message = str(exc)
        selected_profile = ""
        cookie_count = 0
        error = message
    return {
        "ok": ok,
        "auth_source": browser,
        "default_browser": default_kind,
        "selected_profile": selected_profile,
        "loaded_cookie_count": cookie_count,
        "login_url": settings.login_url,
        "candidates": [
            {
                "browser": browser,
                "profile": str(live_profile),
                "cookie_db_exists": True,
                "domain_cookie_count": cookie_count,
                "is_default": True,
                "error": error,
            }
        ],
        "message": message,
    }


def load_live_browser_cookiejar(settings: Settings, browser: str) -> BrowserCookieResult:
    if browser not in LIVE_CHROMIUM_BROWSERS:
        raise BrowserAuthError("浏览器内登录态只支持 Chrome、Edge 或 Brave")
    port = _live_browser_port(browser)
    try:
        websocket_url = _devtools_page_websocket_url(port, settings.login_url)
    except BrowserAuthError as exc:
        raise BrowserAuthError(
            f"请先点击“打开登录页”，在程序打开的 {BROWSER_NAME_FOR_USER.get(browser, browser)} 窗口完成登录后再检查。"
        ) from exc
    with _DevToolsConnection(websocket_url) as devtools:
        cookies = _devtools_cookies(devtools)
    jar = CookieJar()
    for item in cookies:
        domain = str(item.get("domain") or "").lower().lstrip(".")
        if _domain_matches(domain, settings.cookie_domains) and item.get("name") and item.get("value"):
            jar.set_cookie(_cookie_from_devtools(item))
    count = len(list(jar))
    if count <= 0:
        raise BrowserAuthError(
            f"程序打开的 {BROWSER_NAME_FOR_USER.get(browser, browser)} 窗口里还没有会员区登录状态。请在该窗口完成登录后再检查。"
        )
    result = BrowserCookieResult(browser, _live_browser_user_data_dir(settings, browser), jar, count, "浏览器内登录态待确认")
    message = _validate_member_login(settings, result)
    return BrowserCookieResult(browser, result.profile_path, jar, count, message)


def _validate_member_login(settings: Settings, cookie_result: BrowserCookieResult) -> str:
    # 只靠 cookie 数量会误把游客 cookie 当登录态；这里实际访问会员区 API，确认服务端认可当前会话。
    session = BrowserMemberSession(settings, cookie_result)
    api_url = f"{settings.base_url}/members/follow/api/followlist"
    referer = f"{settings.base_url}/members/follow/"
    try:
        payload, final_url = session.fetch_text(api_url, referer=referer, timeout=8)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise BrowserAuthError("还没有完成会员区登录。请在程序打开的登录窗口登录后等待自动检测。") from exc
        raise BrowserAuthError(f"无法确认会员区登录：HTTP {exc.code}") from exc
    except (TimeoutError, OSError, http.cookiejar.LoadError, URLError) as exc:
        raise BrowserAuthError(f"无法确认会员区登录：{exc}") from exc

    final_path = urlparse(final_url).path.lower()
    if "/members/follow/api/followlist" not in final_path:
        raise BrowserAuthError("还没有完成会员区登录。请在程序打开的登录窗口登录后等待自动检测。")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BrowserAuthError("还没有确认会员区登录；请确认网页登录完成后稍等几秒。") from exc
    if isinstance(parsed, dict) and isinstance(parsed.get("follow"), list):
        return "会员区登录已确认，可以同步"
    raise BrowserAuthError("会员区登录确认失败；请重新打开登录页并完成登录。")


def doctor_firefox(settings: Settings) -> FirefoxDoctorResult:
    profile = settings.firefox_profile
    if profile is None:
        return FirefoxDoctorResult(False, False, 0, "未指定 Firefox profile；请使用 doctor browser 自动检测，或设置 EIKETSU_FIREFOX_PROFILE。")
    try:
        profile_exists = profile.exists()
        cookie_db_exists = (profile / "cookies.sqlite").exists()
    except PermissionError as exc:
        return FirefoxDoctorResult(False, False, 0, f"Firefox profile 不可访问：{exc}")
    if not cookie_db_exists:
        return FirefoxDoctorResult(profile_exists, cookie_db_exists, 0, "Firefox cookie 数据库不存在")
    try:
        jar = load_firefox_cookiejar(settings, profile)
    except Exception as exc:  # noqa: BLE001 - 兼容旧 doctor 命令的可读错误。
        return FirefoxDoctorResult(profile_exists, cookie_db_exists, 0, f"读取 cookies 失败：{exc}")
    return FirefoxDoctorResult(profile_exists, cookie_db_exists, len(list(jar)), "Firefox cookies 可读取")


def load_firefox_cookiejar(settings: Settings, profile: Path | None = None) -> CookieJar:
    profile = profile or settings.firefox_profile
    if profile is None:
        raise FileNotFoundError("未指定 Firefox profile；请使用自动浏览器检测，或设置 EIKETSU_FIREFOX_PROFILE。")
    cookie_db = profile / "cookies.sqlite"
    if not cookie_db.exists():
        raise FileNotFoundError(f"找不到 Firefox cookies.sqlite：{cookie_db}")
    jar = CookieJar()
    with _copied_sqlite_db(settings, cookie_db, "firefox_cookies") as copied_db:
        connection = sqlite3.connect(copied_db)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT host, path, name, value, expiry, isSecure, isHttpOnly
                FROM moz_cookies
                ORDER BY host, path, name
                """
            ).fetchall()
        finally:
            connection.close()
    for row in rows:
        host = str(row["host"] or "").lower().lstrip(".")
        if _domain_matches(host, settings.cookie_domains) and row["name"]:
            jar.set_cookie(_firefox_cookie_from_row(row))
    return jar


def load_chromium_cookiejar(
    settings: Settings,
    candidate: BrowserProfileCandidate,
    decryptor: Callable[[bytes, bytes | None], str] | None = None,
) -> CookieJar:
    if candidate.local_state is None:
        raise FileNotFoundError("Chromium Local State 路径为空")
    decryptor = decryptor or decrypt_chromium_cookie_value
    master_key = _load_chromium_master_key(candidate.local_state)
    jar = CookieJar()
    matched_rows = 0
    decrypt_errors: list[str] = []
    with _copied_sqlite_db(settings, candidate.cookie_db, f"{candidate.browser}_cookies") as copied_db:
        connection = sqlite3.connect(copied_db)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT host_key, path, name, value, encrypted_value, expires_utc, is_secure, is_httponly
                FROM cookies
                ORDER BY host_key, path, name
                """
            ).fetchall()
        finally:
            connection.close()
    for row in rows:
        host = str(row["host_key"] or "").lower().lstrip(".")
        if not _domain_matches(host, settings.cookie_domains) or not row["name"]:
            continue
        matched_rows += 1
        value = str(row["value"] or "")
        encrypted_value = bytes(row["encrypted_value"] or b"")
        if not value and encrypted_value:
            try:
                value = decryptor(encrypted_value, master_key)
            except Exception as exc:  # noqa: BLE001 - 同一 profile 里可能只有部分登录字段无法解密，先尝试其它字段。
                decrypt_errors.append(str(exc))
                continue
        if value:
            jar.set_cookie(_chromium_cookie_from_row(row, value))
    if matched_rows > 0 and not list(jar) and decrypt_errors:
        if any(_is_chromium_protected_login_error(message) for message in decrypt_errors):
            raise BrowserAuthError(CHROMIUM_PROTECTED_LOGIN_ERROR)
        raise BrowserAuthError("浏览器登录数据已加密，当前 Windows 用户无法读取")
    return jar


def decrypt_chromium_cookie_value(encrypted_value: bytes, master_key: bytes | None) -> str:
    if not encrypted_value:
        return ""
    if encrypted_value.startswith(b"v20"):
        # Chrome/Edge/Brave 新版可能使用 App-Bound Encryption，这类数据不能靠普通 DPAPI 离线读取。
        raise BrowserAuthError(CHROMIUM_PROTECTED_LOGIN_ERROR)
    if encrypted_value.startswith((b"v10", b"v11")):
        if not master_key:
            raise BrowserAuthError("Chromium cookie 需要 Local State master key")
        return _decrypt_chromium_aes_gcm(encrypted_value, master_key).decode("utf-8", errors="replace")
    return _windows_dpapi_unprotect(encrypted_value).decode("utf-8", errors="replace")


def _is_chromium_protected_login_error(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "app-bound",
            "app bound",
            "v20",
            "新版登录数据受浏览器保护",
            "当前无法直接读取",
        )
    )


def detect_default_browser_kind() -> str:
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
    except OSError:
        return ""
    return browser_kind_from_progid(str(prog_id))


def browser_kind_from_progid(prog_id: str) -> str:
    lowered = prog_id.lower()
    if "msedge" in lowered or "edge" in lowered:
        return "edge"
    if "chrome" in lowered:
        return "chrome"
    if "brave" in lowered:
        return "brave"
    if "firefox" in lowered:
        return "firefox"
    return ""


def discover_firefox_profiles(settings: Settings, firefox_root: Path | None = None) -> list[BrowserProfileCandidate]:
    root = firefox_root or Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    profiles_ini = root / "profiles.ini"
    profiles: list[BrowserProfileCandidate] = []
    if settings.browser_profile:
        profile = settings.browser_profile
        return [BrowserProfileCandidate("firefox", profile, profile / "cookies.sqlite", is_default=True, order_score=200)]
    try:
        profiles_ini_exists = profiles_ini.exists()
    except OSError:
        profiles_ini_exists = False
    if not profiles_ini_exists:
        if settings.firefox_profile is None:
            return []
        profile = settings.firefox_profile
        return [BrowserProfileCandidate("firefox", profile, profile / "cookies.sqlite", is_default=True, order_score=100)]

    parser = ConfigParser()
    parser.read(profiles_ini, encoding="utf-8")
    for section in parser.sections():
        if not section.lower().startswith("profile"):
            continue
        raw_path = parser.get(section, "Path", fallback="")
        if not raw_path:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1") == "1"
        profile = (root / raw_path) if is_relative else Path(raw_path)
        is_default = parser.get(section, "Default", fallback="0") == "1"
        profiles.append(
            BrowserProfileCandidate(
                "firefox",
                profile,
                profile / "cookies.sqlite",
                is_default=is_default,
                order_score=150 if is_default else 50,
            )
        )
    return profiles


def discover_chromium_profiles(settings: Settings, browser: str, user_data_root: Path | None = None) -> list[BrowserProfileCandidate]:
    root = user_data_root or _default_chromium_root(browser)
    if settings.browser_profile:
        profile = settings.browser_profile
        return [BrowserProfileCandidate(browser, profile, profile / "Network" / "Cookies", _local_state_for_profile(profile, root), True, 200)]
    local_state = root / "Local State"
    last_used = _chromium_last_used_profile(local_state)
    profiles: list[BrowserProfileCandidate] = []
    try:
        root_exists = root.exists()
    except OSError:
        return profiles
    if not root_exists:
        return profiles
    try:
        profile_dirs = sorted(root.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return profiles
    for profile in profile_dirs:
        if not profile.is_dir():
            continue
        cookie_db = profile / "Network" / "Cookies"
        if not cookie_db.exists():
            continue
        is_default = profile.name == last_used or profile.name == "Default"
        score = 150 if profile.name == last_used else 100 if profile.name == "Default" else 50
        profiles.append(BrowserProfileCandidate(browser, profile, cookie_db, local_state, is_default, score))
    return profiles


def _local_state_for_profile(profile: Path, fallback_root: Path) -> Path:
    # 自定义 Chromium profile 常传到 Default/Profile 1 层级，Local State 则在它的上一级 user data 根目录。
    sibling_local_state = profile.parent / "Local State"
    if sibling_local_state.exists():
        return sibling_local_state
    return fallback_root / "Local State"


def _candidate_profiles(settings: Settings, auth_source: str) -> list[BrowserProfileCandidate]:
    if auth_source == "firefox-profile":
        profile = settings.browser_profile or settings.firefox_profile
        if profile is None:
            return []
        return [BrowserProfileCandidate("firefox-profile", profile, profile / "cookies.sqlite", is_default=True, order_score=250)]
    if auth_source == "firefox":
        return discover_firefox_profiles(settings)
    if auth_source in LIVE_CHROMIUM_BROWSERS:
        return discover_chromium_profiles(settings, auth_source)

    default_kind = detect_default_browser_kind()
    ordered_sources = [default_kind] if default_kind else []
    if auth_source == "auto":
        for source in (*LIVE_CHROMIUM_BROWSERS, "firefox"):
            if source not in ordered_sources:
                ordered_sources.append(source)
    candidates: list[BrowserProfileCandidate] = []
    for source in ordered_sources:
        if source in LIVE_CHROMIUM_BROWSERS:
            candidates.extend(discover_chromium_profiles(settings, source))
        elif source == "firefox":
            candidates.extend(discover_firefox_profiles(settings))
    return candidates


def _score_candidates(settings: Settings, candidates: list[BrowserProfileCandidate]) -> None:
    for candidate in candidates:
        try:
            candidate.cookie_count = _count_domain_cookies(settings, candidate.cookie_db, candidate.browser, settings.cookie_domains)
        except Exception as exc:  # noqa: BLE001 - doctor 展示该 profile 不可读即可。
            candidate.cookie_count = 0
            candidate.error = str(exc)


def _count_domain_cookies(settings: Settings, cookie_db: Path, browser: str, domains: Sequence[str]) -> int:
    if not cookie_db.exists():
        return 0
    column = "host" if browser.startswith("firefox") else "host_key"
    table = "moz_cookies" if browser.startswith("firefox") else "cookies"
    with _copied_sqlite_db(settings, cookie_db, "cookie_count") as copied_db:
        connection = sqlite3.connect(copied_db)
        try:
            rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
        finally:
            connection.close()
    return sum(1 for (host,) in rows if _domain_matches(str(host or "").lower().lstrip("."), domains))


class _CopiedSqliteDb:
    def __init__(self, settings: Settings, source: Path, prefix: str):
        self.settings = settings
        self.source = source
        self.prefix = prefix
        self.tmp_dir: Path | None = None
        self.copied_db: Path | None = None

    def __enter__(self) -> Path:
        temp_root = self.settings.root_dir / ".tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix=f"eiketsu_env_{self.prefix}_", dir=temp_root))
        self.copied_db = self.tmp_dir / self.source.name
        shutil.copy2(self.source, self.copied_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.source) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(self.copied_db) + suffix))
        return self.copied_db

    def __exit__(self, *_exc_info) -> None:
        if self.tmp_dir is not None:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


def _copied_sqlite_db(settings: Settings, source: Path, prefix: str) -> _CopiedSqliteDb:
    return _CopiedSqliteDb(settings, source, prefix)


def _firefox_cookie_from_row(row: sqlite3.Row) -> Cookie:
    host = str(row["host"] or "")
    expiry = int(row["expiry"] or 0)
    if expiry > 10_000_000_000:
        expiry //= 1000
    return _make_cookie(
        name=str(row["name"] or ""),
        value=str(row["value"] or ""),
        domain=host,
        path=str(row["path"] or "/"),
        secure=bool(row["isSecure"]),
        http_only=bool(row["isHttpOnly"]),
        expires=expiry or None,
    )


def _chromium_cookie_from_row(row: sqlite3.Row, value: str) -> Cookie:
    return _make_cookie(
        name=str(row["name"] or ""),
        value=value,
        domain=str(row["host_key"] or ""),
        path=str(row["path"] or "/"),
        secure=bool(row["is_secure"]),
        http_only=bool(row["is_httponly"]),
        expires=_chromium_expires_to_unix(int(row["expires_utc"] or 0)),
    )


def _make_cookie(name: str, value: str, domain: str, path: str, secure: bool, http_only: bool, expires: int | None) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain or domain.lstrip("."),
        domain_specified=domain.startswith("."),
        domain_initial_dot=domain.startswith("."),
        path=path or "/",
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": str(http_only)},
        rfc2109=False,
    )


def _chromium_expires_to_unix(expires_utc: int) -> int | None:
    if expires_utc <= 0:
        return None
    unix = int(expires_utc / 1_000_000 - CHROME_EPOCH_OFFSET_SECONDS)
    return unix if unix > 0 else None


def _default_chromium_root(browser: str) -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    if browser == "edge":
        return local_app_data / "Microsoft" / "Edge" / "User Data"
    if browser == "brave":
        return local_app_data / "BraveSoftware" / "Brave-Browser" / "User Data"
    return local_app_data / "Google" / "Chrome" / "User Data"


def _chromium_last_used_profile(local_state: Path) -> str:
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Default"
    profile = payload.get("profile") if isinstance(payload, dict) else {}
    return str(profile.get("last_used") or "Default") if isinstance(profile, dict) else "Default"


def _browser_kind_for_open(auth_source: str) -> str:
    if auth_source in {"auto", "default-browser"}:
        return _auto_live_chromium_browser()
    if auth_source == "firefox-profile":
        return "firefox"
    return auth_source


def _live_chromium_browser_for_auth_source(auth_source: str) -> str:
    if auth_source in LIVE_CHROMIUM_BROWSERS:
        return auth_source
    if auth_source in {"auto", "default-browser"}:
        return _auto_live_chromium_browser()
    return ""


def _auto_live_chromium_browser() -> str:
    default_kind = detect_default_browser_kind()
    if default_kind in LIVE_CHROMIUM_BROWSERS and _browser_executable_path(default_kind):
        return default_kind
    installed = _installed_live_chromium_browsers()
    if installed:
        return installed[0]
    return default_kind if default_kind in LIVE_CHROMIUM_BROWSERS else ""


def _installed_live_chromium_browsers() -> list[str]:
    return [browser for browser in LIVE_CHROMIUM_BROWSERS if _browser_executable_path(browser)]


def _browser_executable_path(browser: str) -> Path | None:
    for candidate in _browser_executable_candidates(browser):
        if _path_exists(candidate):
            return candidate
    return None


def _browser_executable_candidates(browser: str) -> list[Path]:
    names = {
        "chrome": ["chrome.exe", "chrome"],
        "edge": ["msedge.exe", "msedge"],
        "brave": ["brave.exe", "brave"],
        "firefox": ["firefox.exe", "firefox"],
    }.get(browser, [])
    candidates = [Path(found) for name in names if (found := shutil.which(name))]

    program_files = _env_path("PROGRAMFILES")
    program_files_x86 = _env_path("PROGRAMFILES(X86)")
    local_app_data = _env_path("LOCALAPPDATA")
    if browser == "edge":
        candidates.extend(
            path
            for root in (program_files_x86, program_files, local_app_data)
            if root is not None
            for path in [root / "Microsoft" / "Edge" / "Application" / "msedge.exe"]
        )
    elif browser == "chrome":
        candidates.extend(
            path
            for root in (program_files, program_files_x86, local_app_data)
            if root is not None
            for path in [root / "Google" / "Chrome" / "Application" / "chrome.exe"]
        )
    elif browser == "brave":
        candidates.extend(
            path
            for root in (program_files, program_files_x86, local_app_data)
            if root is not None
            for path in [root / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"]
        )
    elif browser == "firefox":
        candidates.extend(
            path
            for root in (program_files, program_files_x86, local_app_data)
            if root is not None
            for path in [root / "Mozilla Firefox" / "firefox.exe"]
        )
    return candidates


def _browser_profile_args(settings: Settings, browser: str) -> list[str]:
    if browser in LIVE_CHROMIUM_BROWSERS and settings.browser_profile:
        return [f"--profile-directory={settings.browser_profile.name}"]
    if browser == "firefox":
        profile = settings.browser_profile or settings.firefox_profile
        if profile:
            return ["-profile", str(profile)]
    return []


def _browser_launch_args(settings: Settings, browser: str, executable: Path, target_url: str) -> list[str]:
    if browser in LIVE_CHROMIUM_BROWSERS:
        user_data_dir = _live_browser_user_data_dir(settings, browser)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return [
            str(executable),
            f"--remote-debugging-port={_live_browser_port(browser)}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--new-window",
            target_url,
        ]
    return [str(executable), *_browser_profile_args(settings, browser), target_url]


def _live_browser_port(browser: str) -> int:
    return LIVE_BROWSER_PORTS.get(browser, 49380)


def _live_browser_user_data_dir(settings: Settings, browser: str) -> Path:
    # 专用浏览器目录放在用户 AppData 下，换新版 exe 后仍能复用登录态；它不会进入上传包或 Git。
    override = os.environ.get("EIKETSU_BROWSER_LOGIN_DIR")
    if override:
        return Path(override) / browser
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "EiketsuCollector" / "browser_login" / browser
    return settings.root_dir / ".tmp" / "browser_login" / browser


def _devtools_page_websocket_url(port: int, preferred_url: str) -> str:
    targets = _devtools_json(port, "/json/list")
    if not isinstance(targets, list):
        raise BrowserAuthError("浏览器调试接口没有返回页面列表")
    preferred_host = urlparse(preferred_url).hostname or ""
    fallback = ""
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        websocket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not websocket_url:
            continue
        page_host = urlparse(str(target.get("url") or "")).hostname or ""
        if preferred_host and page_host == preferred_host:
            return websocket_url
        fallback = fallback or websocket_url
    if fallback:
        return fallback
    raise BrowserAuthError("程序打开的浏览器窗口还没有可用页面")


def _devtools_json(port: int, path: str) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urlopen(url, timeout=LIVE_BROWSER_TIMEOUT_SECONDS) as response:  # noqa: S310 - 只访问本机 DevTools 端口。
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise BrowserAuthError("程序打开的浏览器还没有准备好") from exc


def _devtools_cookies(devtools: "_DevToolsConnection") -> list[dict[str, Any]]:
    try:
        payload = devtools.call("Network.getAllCookies")
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
    except BrowserAuthError:
        payload = devtools.call("Storage.getCookies")
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
    return [item for item in cookies or [] if isinstance(item, dict)]


def _cookie_from_devtools(item: dict[str, Any]) -> Cookie:
    expires = item.get("expires")
    try:
        expires_int = int(float(expires))
    except (TypeError, ValueError):
        expires_int = 0
    return _make_cookie(
        name=str(item.get("name") or ""),
        value=str(item.get("value") or ""),
        domain=str(item.get("domain") or ""),
        path=str(item.get("path") or "/"),
        secure=bool(item.get("secure")),
        http_only=bool(item.get("httpOnly")),
        expires=expires_int if expires_int > 0 else None,
    )


class _DevToolsConnection:
    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.sock: socket.socket | None = None
        self.next_id = 1

    def __enter__(self) -> "_DevToolsConnection":
        parsed = urlparse(self.websocket_url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise BrowserAuthError("浏览器调试地址格式不正确")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.sock = socket.create_connection((parsed.hostname, port), timeout=LIVE_BROWSER_TIMEOUT_SECONDS)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_headers()
        if " 101 " not in response.split("\r\n", 1)[0]:
            raise BrowserAuthError("浏览器调试连接失败")
        return self

    def __exit__(self, *_exc_info) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        self._send_json({"id": message_id, "method": method, "params": params or {}})
        while True:
            payload = self._recv_json()
            if payload.get("id") != message_id:
                continue
            if payload.get("error"):
                raise BrowserAuthError(str(payload["error"]))
            result = payload.get("result")
            return result if isinstance(result, dict) else {}

    def _read_http_headers(self) -> str:
        assert self.sock is not None
        chunks: list[bytes] = []
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        return b"".join(chunks).decode("iso-8859-1", errors="replace")

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(data)

    def _send_frame(self, data: bytes) -> None:
        assert self.sock is not None
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.extend([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_json(self) -> dict[str, Any]:
        frame = self._recv_frame()
        try:
            payload = json.loads(frame.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BrowserAuthError("浏览器调试返回了无法解析的数据") from exc
        return payload if isinstance(payload, dict) else {}

    def _recv_frame(self) -> bytes:
        assert self.sock is not None
        first = self._recv_exact(2)
        opcode = first[0] & 0x0F
        if opcode == 0x8:
            raise BrowserAuthError("浏览器调试连接已关闭")
        length = first[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        masked = bool(first[1] & 0x80)
        mask = self._recv_exact(4) if masked else b""
        data = self._recv_exact(length)
        if masked:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        if opcode not in {0x1, 0x2}:
            return self._recv_frame()
        return data

    def _recv_exact(self, length: int) -> bytes:
        assert self.sock is not None
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise BrowserAuthError("浏览器调试连接中断")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def _webbrowser_controller_name(browser: str) -> str:
    return {"chrome": "chrome", "firefox": "firefox"}.get(browser, "")


def _launch_browser_process(args: Sequence[str]) -> None:
    subprocess.Popen(list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw) if raw else None


def _load_chromium_master_key(local_state: Path) -> bytes | None:
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(str(payload.get("os_crypt", {}).get("encrypted_key") or ""))
    except Exception:  # noqa: BLE001 - 旧版 Chromium 可能直接用 DPAPI 加密每个 cookie。
        return None
    if not encrypted_key:
        return None
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    return _windows_dpapi_unprotect(encrypted_key)


def _domain_matches(host: str, domains: Sequence[str]) -> bool:
    normalized = host.lower().lstrip(".")
    allowed = [item.lower().lstrip(".") for item in domains]
    return not allowed or any(normalized == domain or normalized.endswith("." + domain) for domain in allowed)


def _normalize_auth_source(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized not in SUPPORTED_AUTH_SOURCES:
        raise ValueError(f"不支持的 auth_source：{value}")
    return normalized


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _windows_dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise BrowserAuthError("DPAPI 只支持 Windows")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _decrypt_chromium_aes_gcm(encrypted_value: bytes, key: bytes) -> bytes:
    if os.name != "nt":
        raise BrowserAuthError("Chromium AES-GCM 解密当前只支持 Windows")
    if len(encrypted_value) < 3 + 12 + 16:
        raise BrowserAuthError("Chromium encrypted_value 长度异常")
    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:-16]
    tag = encrypted_value[-16:]
    return _bcrypt_aes_gcm_decrypt(key, nonce, ciphertext, tag)


def _bcrypt_aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    bcrypt = ctypes.windll.bcrypt
    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), ctypes.c_wchar_p("AES"), None, 0)
    _raise_if_bcrypt_failed(status, "BCryptOpenAlgorithmProvider")
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        status = bcrypt.BCryptSetProperty(h_alg, ctypes.c_wchar_p("ChainingMode"), mode, len(mode) * ctypes.sizeof(ctypes.c_wchar), 0)
        _raise_if_bcrypt_failed(status, "BCryptSetProperty")
        object_length = ctypes.c_ulong()
        result_size = ctypes.c_ulong()
        status = bcrypt.BCryptGetProperty(
            h_alg,
            ctypes.c_wchar_p("ObjectLength"),
            ctypes.byref(object_length),
            ctypes.sizeof(object_length),
            ctypes.byref(result_size),
            0,
        )
        _raise_if_bcrypt_failed(status, "BCryptGetProperty")
        key_object = ctypes.create_string_buffer(object_length.value)
        key_buffer = ctypes.create_string_buffer(key)
        status = bcrypt.BCryptGenerateSymmetricKey(
            h_alg,
            ctypes.byref(h_key),
            key_object,
            object_length.value,
            key_buffer,
            len(key),
            0,
        )
        _raise_if_bcrypt_failed(status, "BCryptGenerateSymmetricKey")
        plaintext = ctypes.create_string_buffer(len(ciphertext))
        plaintext_size = ctypes.c_ulong()
        auth_info = _BcryptAuthenticatedCipherModeInfo(nonce, tag)
        cipher_buffer = ctypes.create_string_buffer(ciphertext)
        status = bcrypt.BCryptDecrypt(
            h_key,
            cipher_buffer,
            len(ciphertext),
            ctypes.byref(auth_info.struct),
            None,
            0,
            plaintext,
            len(plaintext),
            ctypes.byref(plaintext_size),
            0,
        )
        _raise_if_bcrypt_failed(status, "BCryptDecrypt")
        return plaintext.raw[: plaintext_size.value]
    finally:
        if h_key.value:
            bcrypt.BCryptDestroyKey(h_key)
        if h_alg.value:
            bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


class _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.c_void_p),
        ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.c_void_p),
        ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.c_void_p),
        ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.c_void_p),
        ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", ctypes.c_ulong),
    ]


class _BcryptAuthenticatedCipherModeInfo:
    def __init__(self, nonce: bytes, tag: bytes):
        self.nonce_buffer = ctypes.create_string_buffer(nonce)
        self.tag_buffer = ctypes.create_string_buffer(tag)
        self.struct = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        self.struct.cbSize = ctypes.sizeof(_BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO)
        self.struct.dwInfoVersion = 1
        self.struct.pbNonce = ctypes.cast(self.nonce_buffer, ctypes.c_void_p)
        self.struct.cbNonce = len(nonce)
        self.struct.pbTag = ctypes.cast(self.tag_buffer, ctypes.c_void_p)
        self.struct.cbTag = len(tag)


def _raise_if_bcrypt_failed(status: int, operation: str) -> None:
    # BCrypt 返回 NTSTATUS；负数代表失败，0 为 STATUS_SUCCESS。
    if ctypes.c_long(status).value < 0:
        raise BrowserAuthError(f"{operation} 失败：NTSTATUS={status:#x}")
