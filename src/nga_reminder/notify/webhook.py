"""Forward notifications to an HTTP endpoint.

The Docker deployment runs the watcher inside a Linux container, where there is
no way to raise a desktop notification by itself.  It can, however, POST the
notification to a tiny bridge running on the Windows host
(``scripts/notify_bridge.py``), which does the actual toast.

The bridge speaks a tiny JSON protocol::

    POST /notify
    {"title": "...", "body": "...", "url": "http://127.0.0.1:8765/"}

An optional shared token is sent in the ``X-NGA-Bridge-Token`` header, so the
bridge can be left running without turning the host into an open relay.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import Notifier

logger = logging.getLogger("nga_reminder.notify.webhook")


class WebhookNotifier:
    """Posts each notification to an HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url
        self.token = token
        self._error: str | None = None
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def available(self) -> bool:
        return bool(self.url)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def health_url(self) -> str:
        """The bridge's health endpoint, derived from the notify URL.

        ``http://host.docker.internal:8766/notify`` -> ``…/health``.
        """
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    @staticmethod
    def _describe(exc: Exception) -> str:
        """A short, readable reason.

        The raw exception goes to the log; the UI gets a sentence the user can
        act on -- "connection refused" on its own tells them nothing.
        """
        text = str(exc)
        lowered = text.lower()
        if "refused" in lowered or isinstance(exc, httpx.ConnectError):
            return "连接被拒绝——宿主上没在跑通知桥接"
        if "timed out" in lowered or isinstance(exc, httpx.TimeoutException):
            return "连接超时——桥接没在监听 0.0.0.0，或者被防火墙挡了"
        return text[:120] or type(exc).__name__

    def probe(self) -> tuple[bool, str | None]:
        """Check that the bridge is alive right now.

        The bridge is a **foreground process on the Windows host** -- when it is
        not running the container cannot raise a toast at all, and the failure
        is otherwise invisible (the notification just quietly lands in the log).
        The status endpoint calls this so the UI can say so out loud.

        Returns ``(reachable, error)``; a failure never raises.
        """
        if not self.url:
            return False, "没有配置通知桥接地址"
        try:
            response = self._client.get(self.health_url, timeout=2.0)
        except Exception as exc:
            self._error = f"通知桥接不可达：{self._describe(exc)}"
            logger.warning("通知桥接不可达: %s", exc)
            return False, self._error
        if response.status_code >= 400:
            self._error = f"通知桥接返回 {response.status_code}"
            return False, self._error
        self._error = None
        return True, None

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        if not self.url:
            return False
        payload = {"title": title, "body": body, "url": url}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-NGA-Bridge-Token"] = self.token
        try:
            response = self._client.post(self.url, json=payload, headers=headers)
        except Exception as exc:  # pragma: no cover - depends on the host
            self._error = f"通知桥接不可达：{self._describe(exc)}"
            logger.warning("%s（通知已落到日志）", self._error)
            return False
        if response.status_code >= 400:
            if response.status_code == 401:
                # 最常见的一种配错：宿主与容器两侧的共享口令不是同一个值。
                self._error = (
                    "通知桥接拒绝了请求：共享口令不一致"
                    "（.env 里的 NGA_BRIDGE_TOKEN 与容器里的 "
                    "NGA_NOTIFY_WEBHOOK_TOKEN 要是同一个值）"
                )
            else:
                self._error = f"通知桥接返回 {response.status_code}"
            logger.warning("%s（通知已落到日志）", self._error)
            return False
        self._error = None
        return True


__all__ = ["WebhookNotifier", "Notifier"]
