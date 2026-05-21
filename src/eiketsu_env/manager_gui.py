"""维护者用的新版本流程化操作工具。"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from eiketsu_env.config import latest_target_version, version_start_date
from eiketsu_env.services.version_update import (
    PreparedVersionUpdate,
    prepare_version_update,
    vps_refresh_commands,
)


APP_TITLE = "EiketsuCollectorManager"
RELATED_TESTS = (
    "tests/test_prepare_version_update.py",
    "tests/test_card_lookup.py",
    "tests/test_vps_server.py",
)


def main() -> None:
    app = ManagerApp()
    app.mainloop()


class ManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(860, 620)
        self.root_dir = tk.StringVar(value=str(default_repo_root()))
        self.official_root = tk.StringVar(value=str(default_repo_root().parent / "eki_database_v2"))
        default_version = _share_config_value(default_repo_root(), "target_version") or latest_target_version()
        default_start = _share_config_value(default_repo_root(), "date_from") or version_start_date(default_version)
        default_end = _share_config_value(default_repo_root(), "date_to") or default_start
        self.version = tk.StringVar(value=default_version)
        self.start_date = tk.StringVar(value=default_start)
        self.date_to = tk.StringVar(value=default_end)
        self.status = tk.StringVar(value="等待操作")
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._last_result: PreparedVersionUpdate | None = None
        self._preview_process: subprocess.Popen[str] | None = None
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain_queue)

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="新版本配置、卡表 overlay、测试和部署命令的本地维护工具").grid(row=1, column=0, sticky="w", pady=(4, 0))

        form = ttk.LabelFrame(self, text="版本信息", padding=14)
        form.grid(row=1, column=0, sticky="ew", padx=18, pady=(4, 10))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        self._path_row(form, 0, "仓库目录", self.root_dir, self._browse_root)
        self._path_row(form, 1, "官方库目录", self.official_root, self._browse_official_root)
        ttk.Label(form, text="目标版本").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.version, width=24).grid(row=2, column=1, sticky="ew", padx=(10, 20), pady=6)
        ttk.Label(form, text="开始日期").grid(row=2, column=2, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.start_date, width=18).grid(row=2, column=3, sticky="ew", padx=(10, 20), pady=6)
        ttk.Label(form, text="结束日期").grid(row=2, column=4, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.date_to, width=18).grid(row=2, column=5, sticky="ew", padx=(10, 0), pady=6)

        actions = ttk.Frame(self, padding=(18, 0, 18, 10))
        actions.grid(row=2, column=0, sticky="nsew")
        actions.columnconfigure(0, weight=1)
        actions.rowconfigure(1, weight=1)
        buttons = ttk.Frame(actions)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for index, (label, command) in enumerate(
            (
                ("1. 检查准备内容", lambda: self._run_prepare(dry_run=True)),
                ("2. 写入配置和卡表", lambda: self._run_prepare(dry_run=False)),
                ("3. 运行关键测试", self._run_tests),
                ("复制 Git 命令", self._copy_git_commands),
                ("复制 VPS 命令", self._copy_vps_commands),
                ("启动本地预览", self._start_preview),
            )
        ):
            ttk.Button(buttons, text=label, command=command).grid(row=0, column=index, padx=(0, 8), sticky="w")

        self.log = tk.Text(actions, wrap="word", height=24, font=("Consolas", 10))
        self.log.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(actions, orient="vertical", command=self.log.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self._log("建议流程：检查准备内容 -> 写入配置和卡表 -> 运行关键测试 -> 提交 -> 部署 VPS -> 刷新榜单。")

        footer = ttk.Frame(self, padding=(18, 0, 18, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="清空日志", command=lambda: self.log.delete("1.0", "end")).grid(row=0, column=1, sticky="e")

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command: Callable[[], None]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=4, sticky="ew", padx=(10, 10), pady=6)
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=5, sticky="e", pady=6)

    def _browse_root(self) -> None:
        path = filedialog.askdirectory(initialdir=self.root_dir.get() or str(Path.cwd()))
        if path:
            self.root_dir.set(path)
            self._reload_defaults()

    def _browse_official_root(self) -> None:
        path = filedialog.askdirectory(initialdir=self.official_root.get() or str(Path.cwd()))
        if path:
            self.official_root.set(path)

    def _reload_defaults(self) -> None:
        root = self._root_path()
        version = _share_config_value(root, "target_version")
        if version:
            self.version.set(version)
        start = _share_config_value(root, "date_from") or version_start_date(self.version.get())
        if start:
            self.start_date.set(start)
            self.date_to.set(_share_config_value(root, "date_to") or start)

    def _run_prepare(self, *, dry_run: bool) -> None:
        self._run_worker("检查准备内容" if dry_run else "写入配置和卡表", lambda: self._prepare(dry_run=dry_run))

    def _prepare(self, *, dry_run: bool) -> str:
        result = prepare_version_update(
            root=self._root_path(),
            official_root=self._official_root_path(),
            version=self.version.get(),
            start_date=self.start_date.get(),
            date_to=self.date_to.get() or self.start_date.get(),
            dry_run=dry_run,
        )
        self._last_result = result
        lines = [
            "[dry-run] 准备内容" if dry_run else "已写入配置和卡表",
            f"目标版本：{result.version}",
            f"采集日期：{result.start_date} -> {result.date_to}",
            f"官方 base：{result.latest_base_path}",
            f"overlay 卡数：{result.overlay_card_count}，新增：{result.added_overlay_card_count}",
            "",
            "VPS 刷新命令：",
            *vps_refresh_commands(result),
        ]
        if not dry_run:
            lines.extend(["", self._git_status_short()])
        return "\n".join(lines)

    def _run_tests(self) -> None:
        self._run_worker("运行关键测试", self._test_command)

    def _test_command(self) -> str:
        root = self._root_path()
        command = pytest_command(root)
        command.extend(RELATED_TESTS)
        completed = subprocess.run(
            command,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        return "命令：" + " ".join(command) + "\n\n" + completed.stdout

    def _copy_git_commands(self) -> None:
        version = self.version.get().strip() or "Ver.x"
        commands = "\n".join(
            [
                "git status --short",
                "git add shared/share_config.json src/eiketsu_env/config.py assets/card_catalog_overlay.json README.md",
                f'git commit -m "chore: 准备 {version} 新版本配置"',
                "git pull --rebase origin main",
                "git push origin HEAD:main",
            ]
        )
        self._copy_text(commands, "Git 命令已复制")

    def _copy_vps_commands(self) -> None:
        result = self._last_result
        if result is None:
            result = PreparedVersionUpdate(
                version=self.version.get().strip(),
                start_date=self.start_date.get().strip(),
                date_to=(self.date_to.get() or self.start_date.get()).strip(),
                latest_base_path=Path(""),
                overlay_card_count=0,
                added_overlay_card_count=0,
            )
        self._copy_text("\n".join(vps_refresh_commands(result)), "VPS 命令已复制")

    def _copy_text(self, text: str, status: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.set(status)
        self._log(status + "：\n" + text)

    def _start_preview(self) -> None:
        self._run_worker("启动本地预览", self._start_preview_worker)

    def _start_preview_worker(self) -> str:
        root = self._root_path()
        python = repo_python(root)
        if python is None:
            raise FileNotFoundError("找不到 .venv\\Scripts\\python.exe，无法启动本地预览服务")
        if self._preview_process and self._preview_process.poll() is None:
            self._preview_process.terminate()
        port = find_free_port(8780)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        env["EIKETSU_ENV_ROOT"] = str(root)
        env["EIKETSU_CARD_CATALOG_PATH"] = str(root / "assets" / "card_catalog.json")
        self._preview_process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "eiketsu_env.server_app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        wait_for_preview(port)
        url = f"http://127.0.0.1:{port}/leaderboard?version={self.version.get().strip()}&full=1"
        webbrowser.open(url)
        return f"本地预览已启动：{url}"

    def _git_status_short(self) -> str:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=self._root_path(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        return "git status --short：\n" + (completed.stdout.strip() or "干净")

    def _run_worker(self, label: str, func: Callable[[], str]) -> None:
        self.status.set(f"{label}中...")
        threading.Thread(target=self._worker_entry, args=(label, func), daemon=True).start()

    def _worker_entry(self, label: str, func: Callable[[], str]) -> None:
        try:
            output = func()
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("error", f"{label}失败：{exc}"))
        else:
            self._queue.put(("ok", output))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, message = self._queue.get_nowait()
                self._log(message)
                self.status.set("完成" if kind == "ok" else "失败")
                if kind == "error":
                    messagebox.showerror(APP_TITLE, message)
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log.insert("end", f"\n[{timestamp}] {message}\n")
        self.log.see("end")

    def _root_path(self) -> Path:
        return Path(self.root_dir.get()).expanduser().resolve()

    def _official_root_path(self) -> Path:
        return Path(self.official_root.get()).expanduser().resolve()

    def _on_close(self) -> None:
        if self._preview_process and self._preview_process.poll() is None:
            self._preview_process.terminate()
        self.destroy()


def default_repo_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "dist" else exe_dir
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists():
        return cwd
    return Path(__file__).resolve().parents[2]


def _share_config_value(root: Path, key: str) -> str:
    path = root / "shared" / "share_config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get(key) or "").strip()


def repo_python(root: Path) -> Path | None:
    python = root / ".venv" / "Scripts" / "python.exe"
    return python if python.exists() else None


def pytest_command(root: Path) -> list[str]:
    pytest = root / ".venv" / "Scripts" / "pytest.exe"
    if pytest.exists():
        return [str(pytest)]
    python = repo_python(root) or Path(sys.executable)
    return [str(python), "-m", "pytest"]


def find_free_port(start: int) -> int:
    port = start
    while port < start + 200:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    raise RuntimeError("找不到可用本地端口")


def wait_for_preview(port: int) -> None:
    url = f"http://127.0.0.1:{port}/api/v1/config"
    last_error = ""
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"本地预览启动超时：{last_error}")


if __name__ == "__main__":
    main()
