"""Fallback notifier: writes the message to the log.

Used when no native notification backend is available (headless host, missing
dependency) so a new post is never silently dropped.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("nga_reminder.notify")


class LogNotifier:
    """Writes notifications to the application log."""

    def __init__(self, logger_: logging.Logger | None = None) -> None:
        self._logger = logger_ or logger

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        self._logger.warning("通知: %s | %s%s", title, body, f" | {url}" if url else "")
        return True
