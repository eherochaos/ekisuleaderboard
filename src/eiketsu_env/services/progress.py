"""CLI 进度显示工具，统一把动态进度写到 stderr。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TextIO


class ProgressReporter:
    def __init__(self, enabled: bool = True, stream: TextIO | None = None, width: int = 28):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self.width = width
        self._line_open = False

    def message(self, text: str) -> None:
        if not self.enabled:
            return
        self._finish_open_line()
        self.stream.write(f"{text}\n")
        self.stream.flush()

    def task(self, label: str, total: int) -> "ProgressTask":
        task = ProgressTask(self, label, max(0, total))
        task.render(force=True)
        return task

    def _render(self, text: str) -> None:
        if not self.enabled:
            return
        self.stream.write("\r" + text)
        self.stream.flush()
        self._line_open = True

    def _finish_open_line(self) -> None:
        if self.enabled and self._line_open:
            self.stream.write("\n")
            self.stream.flush()
            self._line_open = False


@dataclass(slots=True)
class ProgressTask:
    reporter: ProgressReporter
    label: str
    total: int
    completed: int = 0
    suffix: str = ""
    last_render_at: float = 0.0

    def advance(self, step: int = 1, suffix: str = "") -> None:
        self.completed += step
        if suffix:
            self.suffix = suffix
        self.render()

    def finish(self, suffix: str = "done") -> None:
        if self.total > 0:
            self.completed = max(self.completed, self.total)
        self.suffix = suffix
        self.render(force=True)
        self.reporter._finish_open_line()

    def render(self, force: bool = False) -> None:
        if not self.reporter.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_render_at < 0.08 and self.completed < self.total:
            return
        self.last_render_at = now
        self.reporter._render(self._line())

    def _line(self) -> str:
        if self.total <= 0:
            body = f"{self.label} {self.completed}"
        else:
            ratio = min(1.0, self.completed / self.total)
            filled = int(self.reporter.width * ratio)
            bar = "#" * filled + "-" * (self.reporter.width - filled)
            percent = int(ratio * 100)
            body = f"{self.label} [{bar}] {self.completed}/{self.total} {percent:3d}%"
        return f"{body} {self.suffix}".rstrip()
