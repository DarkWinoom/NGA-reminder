"""Windows toast notifications.

``windows-toasts`` speaks the WinRT toast API directly.  It needs no registered
AppUserModelID: Windows creates the notification's identity on first use, so a
toast sent under an unknown id still reaches the notification centre.  We
borrow the stable id ``NGA Reminder`` so the notification shows up under that
name instead of a random one.

That also means the app can be started by hand -- no scheduled task, no
shortcut, no registration step.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("nga_reminder.notify.windows")


class WindowsNotifier:
    """Shows a modern Windows toast notification."""

    def __init__(self, app_id: str = "NGA Reminder") -> None:
        self.app_id = app_id
        self._toaster = None
        self._load_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            from windows_toasts import Toast, ToastDuration, WindowsToaster
        except Exception as exc:  # pragma: no cover - depends on the host
            self._load_error = f"windows-toasts 不可用: {exc}"
            logger.warning(self._load_error)
            return
        try:
            self._toaster = WindowsToaster(self.app_id)
            self._toast_cls = Toast
            self._duration_cls = ToastDuration
        except Exception as exc:  # pragma: no cover - depends on the host
            self._load_error = f"无法初始化 Toast: {exc}"
            logger.warning(self._load_error)

    @property
    def available(self) -> bool:
        return self._toaster is not None

    @property
    def error(self) -> str | None:
        return self._load_error

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        if self._toaster is None:
            return False
        try:
            toast = self._toast_cls(text_fields=[title, body])
            toast.duration = self._duration_cls.Short
            if url:
                # Makes the toast a protocol activation: clicking it opens the
                # link in the default browser.
                toast.launch_action = url
            self._toaster.show_toast(toast)
            return True
        except Exception as exc:  # pragma: no cover - depends on the host
            logger.warning("弹窗失败: %s", exc)
            return False
