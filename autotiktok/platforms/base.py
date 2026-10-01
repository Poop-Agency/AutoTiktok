"""Shared types for platform publishers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Generous timeouts: uploads of ~100 MB from GitHub runners can take a while.
HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)


class PublishError(Exception):
    """Base error. ``retryable`` tells the queue whether a later run may succeed."""

    retryable = True

    def __init__(self, message: str, *, response: object | None = None):
        super().__init__(message)
        self.response = response


class RetryableError(PublishError):
    """Transient failure (network, rate limit, quota): try again on a later run."""

    retryable = True


class PermanentError(PublishError):
    """Failure that retrying cannot fix (bad config, rejected video, missing scope)."""

    retryable = False


@dataclass
class Video:
    path: Path
    caption: str

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    @property
    def content_type(self) -> str:
        return "video/quicktime" if self.path.suffix.lower() == ".mov" else "video/mp4"


@dataclass
class PublishResult:
    post_id: str
    # Free-form details stored in the state file (URL, TikTok publish status…).
    details: dict = field(default_factory=dict)


def poll(
    check: Callable[[], bool],
    *,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Call ``check`` until it returns True or ``timeout`` seconds elapse. Returns the last result."""
    waited = 0.0
    while True:
        if check():
            return True
        if waited >= timeout:
            return False
        sleep(interval)
        waited += interval
