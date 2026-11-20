"""macOS notifications, via the system ``osascript``.

macOS ships a tool that raises a real Notification Centre banner:
``osascript``.  It is part of the OS, so there is nothing to install.

The title and the body are written **into the script source** rather than
passed as arguments.  That is deliberate: ``osascript`` parses its own
command line, so an argument starting with ``-`` (a forum post beginning with
a dash, say) would be swallowed as an option.  Inside a string literal there
is no such parsing to fight with.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("nga_reminder.notify.macos")


def _apple_literal(text: str) -> str:
    """Turn a piece of text into an AppleScript string expression.

    Only two characters need escaping inside a literal: the backslash and the
    double quote.  A newline becomes ``"…" & return & "…"`` -- ``return`` is
    AppleScript's carriage-return constant.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts = []
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'"{escaped}"')
    return " & return & ".join(parts) if parts else '""'


class MacNotifier:
    """Shows a macOS notification banner through ``osascript``."""

    def __init__(self, app_id: str = "NGA Reminder") -> None:
        self.app_id = app_id
        self._tool: str | None = None
        self._load_error: str | None = None
        self._load()

    def _load(self) -> None:
        found = shutil.which("osascript")
        if not found:
            self._load_error = "找不到 osascript（macOS 自带，路径异常）"
            logger.warning(self._load_error)
            return
        self._tool = found

    @property
    def available(self) -> bool:
        return self._tool is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        if self._tool is None:
            return False
        script = (
            f"display notification ({_apple_literal(body)}) "
            f"with title ({_apple_literal(title or self.app_id)})"
        )
        try:
            result = subprocess.run(  # noqa: S603 - 固定命令，参数不过 shell
                [self._tool, "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - depends on the host
            logger.warning("弹窗失败: %s", exc)
            return False
        if result.returncode != 0:
            logger.warning("弹窗失败: %s", (result.stderr or "").strip()[:200])
            return False
        if url:
            # osascript 的 display notification 没有点击回调，这一条就只当提醒。
            logger.debug("macOS 通知没有点击跳转（url=%s）", url)
        return True


__all__ = ["MacNotifier"]
