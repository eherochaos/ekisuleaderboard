from __future__ import annotations

import io

from eiketsu_env.services.progress import ProgressReporter


def test_progress_reporter_writes_bar_to_stderr_style_stream():
    stream = io.StringIO()
    progress = ProgressReporter(enabled=True, stream=stream, width=10)

    task = progress.task("daily 2026-04-22", 4)
    task.advance(2, suffix="ok=2 err=0")
    task.finish("ok=4 err=0")

    text = stream.getvalue()
    assert "daily 2026-04-22" in text
    assert "4/4 100%" in text
    assert "ok=4 err=0" in text


def test_progress_reporter_can_be_disabled():
    stream = io.StringIO()
    progress = ProgressReporter(enabled=False, stream=stream)

    progress.message("hidden")
    progress.task("hidden", 1).finish()

    assert stream.getvalue() == ""
