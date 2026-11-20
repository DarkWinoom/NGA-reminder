"""Typed errors raised by the NGA client.

The important one is :class:`NgaApiError`. NGA answers ``HTTP 200`` even when
authentication fails, so the JSON ``code`` field is the only reliable success
signal.
"""

from __future__ import annotations


class NgaError(Exception):
    """Base class for every error raised by this package."""


class NgaApiError(NgaError):
    """NGA answered with a non-zero ``code``.

    Attributes:
        code: the numeric code from the JSON body.
        message: the human-readable message NGA returned (already unescaped).
    """

    #: Human-readable explanations for the codes we have observed.
    KNOWN_CODES = {
        0: "成功",
        2: "未定义服务",
        12: "未登录（Cookie 缺失或已过期）",
        14: "找不到主题",
        42: "帖子发布或回复时间超过限制（NGA 对该主题的读取限制）",
        151: "找不到内容 或 没有更多页了",
    }

    def __init__(self, code: int, message: str = "") -> None:
        self.code = code
        self.message = message
        detail = self.KNOWN_CODES.get(code)
        text = message or detail or "未知错误"
        super().__init__(f"NGA 返回 code={code}: {text}")


class NgaAuthError(NgaApiError):
    """The stored credentials are missing, rejected or expired."""

    def __init__(self, message: str = "") -> None:
        super().__init__(12, message or "未登录")


class NgaThreadUnavailableError(NgaApiError):
    """The thread exists but cannot be read (locked, archived or rate limited)."""

    def __init__(self, code: int = 42, message: str = "") -> None:
        super().__init__(code, message)


class NgaRequestError(NgaError):
    """Transport-level failure after exhausting retries."""


class RateLimitError(NgaRequestError):
    """NGA answered 429 (or repeatedly answered 5xx)."""
