import queue
from datetime import date

from eiketsu_env.client_gui import (
    GuiProgressReporter,
    _browser_doctor_warning_title,
    _messagebox_title_for_error,
    browser_label_to_source,
    browser_source_to_label,
    default_sync_date_range,
    format_browser_doctor_message,
)
from eiketsu_env.services.share import ShareConfig


def test_browser_choice_mapping_uses_friendly_labels():
    assert browser_label_to_source("自动检测（默认浏览器优先）") == "auto"
    assert browser_label_to_source("Google Chrome") == "chrome"
    assert browser_label_to_source("Microsoft Edge") == "edge"
    assert browser_label_to_source("Brave") == "brave"
    assert browser_source_to_label("brave") == "Brave"
    assert browser_source_to_label("firefox") == "Firefox"
    assert browser_source_to_label("default-browser") == "自动检测（默认浏览器优先）"
    assert browser_source_to_label("firefox-profile") == "Firefox"
    assert browser_label_to_source("看不懂的输入") == "auto"


def test_default_sync_date_range_prefers_yesterday():
    config = ShareConfig(target_version="Ver.Test", date_from="2026-05-01", date_to="2026-05-31")

    assert default_sync_date_range(config, today=date(2026, 5, 16)) == ("2026-05-15", "2026-05-15")


def test_default_sync_date_range_keeps_within_server_dates():
    capped = ShareConfig(target_version="Ver.Test", date_from="2026-05-01", date_to="2026-05-14")
    floored = ShareConfig(target_version="Ver.Test", date_from="2026-05-16", date_to="2026-05-31")

    assert default_sync_date_range(capped, today=date(2026, 5, 16)) == ("2026-05-14", "2026-05-14")
    assert default_sync_date_range(floored, today=date(2026, 5, 16)) == ("2026-05-16", "2026-05-16")


def test_browser_doctor_message_explains_missing_cookie_without_jargon():
    message = format_browser_doctor_message(
        {
            "ok": False,
            "auth_source": "chrome",
            "candidates": [
                {
                    "browser": "chrome",
                    "profile": r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default",
                    "cookie_db_exists": True,
                    "domain_cookie_count": 0,
                }
            ],
        },
        "Google Chrome",
    )

    assert "没有检测到英杰大战.NET 会员区登录状态" in message
    assert "1. 点击“打开登录页”" in message
    assert "回到这个窗口等待自动检测" in message
    assert "检查登录状态" not in message
    assert "当前选择：Google Chrome" in message
    assert "已检查：Google Chrome 1 个用户" in message
    assert "cookie" not in message.lower()
    assert "profile" not in message.lower()
    assert "winerror" not in message.lower()
    assert "moz_cookies" not in message.lower()
    assert r"C:\Users" not in message


def test_browser_doctor_message_uses_dedicated_login_window_when_locked():
    message = format_browser_doctor_message(
        {
            "ok": False,
            "auth_source": "chrome",
            "candidates": [
                {
                    "browser": "chrome",
                    "profile": r"C:\Users\alice\AppData\Local\Google\Chrome\User Data\Default",
                    "cookie_db_exists": True,
                    "domain_cookie_count": 0,
                    "error": "[WinError 32] 另一个程序正在使用此文件，进程无法访问。",
                }
            ],
        },
        "Google Chrome",
    )

    assert "专用的 Chrome/Edge/Brave 登录窗口" in message
    assert "不需要关闭网页" in message
    assert "专用登录窗口" in message
    assert "cookie" not in message.lower()
    assert "winerror" not in message.lower()
    assert r"C:\Users" not in message


def test_browser_doctor_message_explains_chromium_protected_login_data():
    browser = {
        "ok": False,
        "auth_source": "edge",
        "candidates": [
            {
                "browser": "edge",
                "profile": r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Default",
                "cookie_db_exists": True,
                "domain_cookie_count": 1,
                "error": "Chrome/Edge/Brave 新版登录数据受浏览器保护，当前无法直接读取",
            }
        ],
    }
    message = format_browser_doctor_message(
        browser,
        "Microsoft Edge",
    )

    assert "这不是没有登录，也不是没有关网页" in message
    assert "Chrome/Edge/Brave 新版保护了网页登录状态" in message
    assert "离线读取登录态方案" in message
    assert "专用 Chrome/Edge/Brave 窗口" in message
    assert "反复关闭 Chrome/Edge/Brave 通常不会解决" in message
    assert _browser_doctor_warning_title(browser) == "浏览器登录状态暂不可读取"
    assert _messagebox_title_for_error(message) == "浏览器登录状态暂不可读取"
    assert "cookie" not in message.lower()
    assert "profile" not in message.lower()
    assert "winerror" not in message.lower()
    assert r"C:\Users" not in message


def test_browser_doctor_message_reports_success_profile():
    message = format_browser_doctor_message(
        {
            "ok": True,
            "auth_source": "edge",
            "loaded_cookie_count": 3,
            "selected_profile": r"C:\Users\alice\AppData\Local\Microsoft\Edge\User Data\Profile 1",
        },
        "Microsoft Edge",
    )

    assert "Microsoft Edge" in message
    assert "找到会员区登录状态" in message
    assert "可以继续同步" in message
    assert "Profile 1" in message


def test_gui_progress_reporter_emits_task_updates():
    events: queue.Queue = queue.Queue()
    progress = GuiProgressReporter(events)

    progress.message("采集范围：2026-05-10 至 2026-05-12")
    task = progress.task("daily 2026-05-10", 4)
    task.advance(2, suffix="ok=2 err=0")
    task.finish("ok=4 err=0")

    kinds = [events.get_nowait()[0] for _ in range(events.qsize())]
    assert "progress_message" in kinds
    assert kinds.count("progress") >= 2
