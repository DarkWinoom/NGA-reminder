"""Entry point: ``nga-reminder``."""

from __future__ import annotations

import logging
import sys
import threading
import time
import webbrowser

import uvicorn

from .api import create_app
from .config import get_settings


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    def worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - depends on the host
            logging.getLogger("nga_reminder").warning("无法自动打开浏览器，请手动访问 %s", url)

    threading.Thread(target=worker, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    settings = get_settings()

    if argv is None:
        argv = sys.argv[1:]
    if "--test-notification" in argv:
        return _test_notification(settings)

    app = create_app(settings)

    url = f"http://{settings.host}:{settings.port}"
    print(f"NGA Reminder 正在启动： {url}")
    if settings.open_browser:
        _open_browser_later(url)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )
    return 0


def _test_notification(settings) -> int:
    """Fire a test notification so the user can check the toast channel."""
    from .notify.factory import build_notifier

    notifier = build_notifier(
        settings.toast_app_id,
        webhook_url=settings.notify_webhook,
        webhook_token=settings.notify_webhook_token,
    )
    backend = type(notifier).__name__
    print(f"通知后端：{backend}")
    if hasattr(notifier, "error") and notifier.error:
        print(f"后端错误：{notifier.error}")
    print("正在发送测试通知……")

    delivered = notifier.notify(
        "NGA Reminder",
        "这是一条测试通知。看到它就说明桌面通知通道正常。",
        url=settings.app_url,
    )
    if delivered:
        print("已发送。如果没有看到弹窗，请检查系统的「通知」设置或专注模式。")
        return 0
    print("发送失败：当前环境不支持桌面通知，日志后端已记录这条消息。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
