from __future__ import annotations

import io
from urllib.error import HTTPError

from eiketsu_env.services.requesting import ConsecutiveHttpPause, RetryPolicy, call_with_retries, follow_concurrency_profile, video_search_concurrency_profile


def test_aggressive_profiles_use_planned_worker_counts_and_retries():
    follow = follow_concurrency_profile("aggressive")
    video = video_search_concurrency_profile("aggressive")

    assert follow.daily_workers == 8
    assert follow.detail_workers == 12
    assert follow.retry_policy.retries == 3
    assert video.api_workers == 8
    assert video.play_workers == 12
    assert video.retry_policy.retries == 4
    assert video.min_request_interval_seconds == 0.25


def test_call_with_retries_retries_503_without_losing_round(monkeypatch):
    sleeps: list[float] = []
    attempts = {"count": 0}

    def flaky_action() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise HTTPError("https://example.test", 503, "busy", {}, io.BytesIO())
        return "ok"

    monkeypatch.setattr("eiketsu_env.services.requesting.time.sleep", lambda seconds: sleeps.append(seconds))

    result = call_with_retries(flaky_action, RetryPolicy(retries=3, delays=(10.0, 20.0, 40.0)))

    assert result == "ok"
    assert attempts["count"] == 3
    assert sleeps == [10.0, 20.0]


def test_consecutive_503_gate_pauses_after_threshold(monkeypatch):
    sleeps: list[float] = []
    gate = ConsecutiveHttpPause(503, threshold=2, pause_seconds=120.0)
    error = HTTPError("https://example.test", 503, "busy", {}, io.BytesIO())

    monkeypatch.setattr("eiketsu_env.services.requesting.time.sleep", lambda seconds: sleeps.append(seconds))

    gate.record_failure(error)
    gate.record_failure(error)

    assert sleeps == [120.0]
