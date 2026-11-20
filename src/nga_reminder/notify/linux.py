"""Linux desktop notifications, via ``notify-send``.

``notify-send`` comes from **libnotify** (package ``libnotify-bin`` on
Debian/Ubuntu, ``libnotify`` on Fedora, ``libnotify`` on Arch).  Most desktop
environments already ship it, but a bare server does not -- so the backend
reports a readable hint instead of failing silently.

Arguments are passed as a list (no shell), and a ``--`` separator sits in front
of the title and the body.  That separator matters: a forum post starting with
a dash would otherwise be read as an option by ``notify-send``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("nga_reminder.notify.linux")


class LinuxNotifier:
    """Shows a desktop notification through ``notify-send``."""

    def __init__(self, app_id: str = "NGA Reminder") -> None:
        self.app_id = app_id
        self._tool: str | None = None
        self._load_error: str | None = None
        self._load()

    def _load(self) -> None:
        found = shutil.which("notify-send")
        if not found:
            self._load_error = "找不到 notify-send（装 libnotify：apt install libnotify-bin）"
            logger.warning(self._load_error)
            return
        self._tool = found

    @property
    def available(self) -> bool:
        return self._tool is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    def _command(self, title: str, body: str, url: str = "") -> list[str]:
        """Build the ``notify-send`` command line for one notification.

        Everything after ``--`` is positional (summary, then body), so a title
        or body that starts with ``-`` cannot be mistaken for an option.
        """
        command = [self._tool or "notify-send", "--app-name", self.app_id]
        if url:
            # --url 只在较新的 libnotify 上有；带上试一次，不行就退回去。
            command += ["--url", url]
        command.append("--")
        command.append(title or self.app_id)
        if body:
            command.append(body)
        return command

    def _run(self, command: list[str]) -> subprocess.CompletedProcess | None:
        """Run the command; ``None`` means it could not be run at all."""
        try:
            return subprocess.run(  # noqa: S603 - 固定命令，参数不过 shell
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - depends on the host
            logger.warning("弹窗失败: %s", exc)
            return None

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        if self._tool is None:
            return False
        result = self._run(self._command(title, body, url))
        if result is None:
            return False
        if result.returncode != 0 and url:
            # 老版本不认 --url：去掉重试一次。
            logger.debug("notify-send 不支持 --url，退回普通通知")
            result = self._run(self._command(title, body))
            if result is None:
                return False
        if result.returncode != 0:
            logger.warning("弹窗失败: %s", (result.stderr or "").strip()[:200])
            return False
        return True


__all__ = ["LinuxNotifier"]
