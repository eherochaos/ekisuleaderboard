"""兼容旧 Firefox 登录态入口；新实现已迁移到 browser_session。"""

from __future__ import annotations

from eiketsu_env.config import Settings
from eiketsu_env.services.browser_session import (
    FIREFOX_USER_AGENT as DEFAULT_USER_AGENT,
    BrowserMemberSession,
    FirefoxDoctorResult,
    BrowserCookieResult,
    doctor_firefox,
    load_firefox_cookiejar,
)


class FirefoxMemberSession(BrowserMemberSession):
    """旧调用方仍可显式使用固定 Firefox profile。"""

    def __init__(self, settings: Settings):
        profile = settings.firefox_profile
        if profile is None:
            raise FileNotFoundError("未指定 Firefox profile；请设置 EIKETSU_FIREFOX_PROFILE，或改用统一浏览器自动检测。")
        cookiejar = load_firefox_cookiejar(settings, profile)
        super().__init__(
            settings,
            BrowserCookieResult(
                source="firefox-profile",
                profile_path=profile,
                cookiejar=cookiejar,
                cookie_count=len(list(cookiejar)),
                message="Firefox cookies 可读取",
            ),
        )


__all__ = [
    "DEFAULT_USER_AGENT",
    "FirefoxDoctorResult",
    "FirefoxMemberSession",
    "doctor_firefox",
    "load_firefox_cookiejar",
]
