"""Pick the best available notification backend."""

from __future__ import annotations

import logging
import sys

from .base import Notifier
from .fallback import LogNotifier
from .webhook import WebhookNotifier

logger = logging.getLogger("nga_reminder.notify")


def build_native_notifier(app_id: str = "NGA Reminder") -> Notifier:
    """The best **native** (no HTTP hop) backend for this machine.

    Each platform has its own way of raising a real desktop notification, and
    each of them ships with the OS (or with one tiny package):

    ==========  ==========================================
    Windows     WinRT toast, via ``windows-toasts``
    macOS       ``osascript display notification``
    Linux       ``notify-send`` (libnotify)
    ==========  ==========================================

    The returned backend may report ``available is False`` -- then the caller
    falls back to the log backend.
    """
    if sys.platform == "win32":
        try:
            from .windows import WindowsNotifier
        except Exception as exc:  # pragma: no cover - depends on the host
            logger.warning("加载 Windows 通知后端失败: %s", exc)
            return LogNotifier()
        return WindowsNotifier(app_id)
    if sys.platform == "darwin":
        from .macos import MacNotifier

        return MacNotifier(app_id)

    from .linux import LinuxNotifier

    return LinuxNotifier(app_id)


def build_notifier(
    app_id: str = "NGA Reminder",
    *,
    webhook_url: str = "",
    webhook_token: str = "",
) -> Notifier:
    """Return the notification backend to use.

    Order of preference:

    1. an HTTP bridge (``NGA_NOTIFY_WEBHOOK``) -- this is how the Docker
       deployment gets real desktop notifications, since the container itself
       has no way to raise them;
    2. the native backend for this platform (WinRT toast / osascript /
       notify-send) -- for a natively started app;
    3. the log backend -- nothing else is available.
    """
    if webhook_url:
        logger.info("通知走 HTTP 桥接: %s", webhook_url)
        return WebhookNotifier(webhook_url, token=webhook_token)

    notifier = build_native_notifier(app_id)
    if isinstance(notifier, LogNotifier):
        # 后端模块压根没加载起来——已经是兜底了，别再警告一次。
        return notifier
    if getattr(notifier, "available", False):
        return notifier
    logger.warning("系统通知不可用，改用日志提醒: %s", getattr(notifier, "error", None))
    return LogNotifier()


__all__ = [
    "build_notifier",
    "build_native_notifier",
    "Notifier",
    "LogNotifier",
    "WebhookNotifier",
]
