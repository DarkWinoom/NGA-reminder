"""Notification backends.

The watcher only talks to the :class:`Notifier` protocol, so adding another
channel (Telegram, Bark, ...) means adding one class -- the choice of which
backend to use lives in :func:`build_notifier`.
"""

from __future__ import annotations

from .base import Notifier
from .factory import build_native_notifier, build_notifier
from .fallback import LogNotifier
from .webhook import WebhookNotifier

__all__ = [
    "Notifier",
    "LogNotifier",
    "WebhookNotifier",
    "build_notifier",
    "build_native_notifier",
]
