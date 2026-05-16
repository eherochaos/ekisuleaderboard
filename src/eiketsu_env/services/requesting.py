"""采集请求的并发 profile、限速和重试工具。"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    retries: int = 0
    delays: tuple[float, ...] = ()

    def delay_for(self, retry_index: int) -> float:
        if not self.delays:
            return 0.0
        return self.delays[min(retry_index, len(self.delays) - 1)]


@dataclass(frozen=True, slots=True)
class FollowConcurrencyProfile:
    name: str
    daily_workers: int
    detail_workers: int
    retry_policy: RetryPolicy


@dataclass(frozen=True, slots=True)
class VideoSearchConcurrencyProfile:
    name: str
    api_workers: int
    play_workers: int
    retry_policy: RetryPolicy
    min_request_interval_seconds: float = 0.0
    http_503_pause_threshold: int = 0
    http_503_pause_seconds: float = 0.0


class RequestThrottle:
    """跨 worker 共享的轻量限速器，用来控制请求发起间隔。"""

    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_seconds = self._last_request_at + self.min_interval_seconds - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()


class ConsecutiveHttpPause:
    """连续 503 到达阈值时暂停一段时间，让官网限流有时间恢复。"""

    def __init__(self, status_code: int, threshold: int, pause_seconds: float):
        self.status_code = status_code
        self.threshold = max(0, int(threshold))
        self.pause_seconds = max(0.0, float(pause_seconds))
        self._lock = threading.Lock()
        self._count = 0

    def record_success(self) -> None:
        if self.threshold <= 0:
            return
        with self._lock:
            self._count = 0

    def record_failure(self, exc: BaseException) -> None:
        if self.threshold <= 0 or http_status(exc) != self.status_code:
            return
        should_pause = False
        with self._lock:
            self._count += 1
            if self._count >= self.threshold:
                self._count = 0
                should_pause = True
        if should_pause and self.pause_seconds > 0:
            time.sleep(self.pause_seconds)


def follow_concurrency_profile(name: str) -> FollowConcurrencyProfile:
    normalized = _normalize_profile_name(name)
    if normalized == "aggressive":
        return FollowConcurrencyProfile(
            name="aggressive",
            daily_workers=8,
            detail_workers=12,
            retry_policy=RetryPolicy(retries=3, delays=(10.0, 20.0, 40.0)),
        )
    return FollowConcurrencyProfile(name="default", daily_workers=1, detail_workers=1, retry_policy=RetryPolicy())


def video_search_concurrency_profile(name: str) -> VideoSearchConcurrencyProfile:
    normalized = _normalize_profile_name(name)
    if normalized == "aggressive":
        return VideoSearchConcurrencyProfile(
            name="aggressive",
            api_workers=8,
            play_workers=12,
            retry_policy=RetryPolicy(retries=4, delays=(10.0, 20.0, 40.0, 80.0)),
            min_request_interval_seconds=0.25,
            http_503_pause_threshold=5,
            http_503_pause_seconds=120.0,
        )
    return VideoSearchConcurrencyProfile(name="default", api_workers=1, play_workers=1, retry_policy=RetryPolicy())


def call_with_retries(
    action: Callable[[], T],
    policy: RetryPolicy,
    throttle: RequestThrottle | None = None,
    pause_gate: ConsecutiveHttpPause | None = None,
) -> T:
    retry_index = 0
    while True:
        if throttle is not None:
            throttle.wait()
        try:
            result = action()
        except Exception as exc:
            if pause_gate is not None:
                pause_gate.record_failure(exc)
            if retry_index >= policy.retries or not is_retryable_exception(exc):
                raise
            time.sleep(policy.delay_for(retry_index))
            retry_index += 1
            continue
        if pause_gate is not None:
            pause_gate.record_success()
        return result


def is_retryable_exception(exc: BaseException) -> bool:
    status = http_status(exc)
    if status is not None:
        return status in {408, 429, 500, 502, 503, 504}
    if isinstance(exc, TimeoutError | socket.timeout | ConnectionError):
        return True
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        return isinstance(reason, TimeoutError | socket.timeout | OSError) or "timed out" in str(exc).lower()
    return isinstance(exc, OSError) and "timed out" in str(exc).lower()


def http_status(exc: BaseException) -> int | None:
    if isinstance(exc, HTTPError):
        return int(exc.code)
    return None


def _normalize_profile_name(name: str) -> str:
    normalized = str(name or "default").strip().lower()
    if normalized not in {"default", "aggressive"}:
        raise ValueError(f"不支持的并发 profile：{name}")
    return normalized
