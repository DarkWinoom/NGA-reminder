"""Tests for the HTTP notification bridge (container → Windows host)."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from nga_reminder.notify.factory import build_notifier
from nga_reminder.notify.webhook import WebhookNotifier

# --------------------------------------------------------------- 后端本身


def test_webhook_posts_json_with_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-NGA-Bridge-Token")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    notifier = WebhookNotifier(
        "http://bridge.test/notify", token="s3cret", transport=httpx.MockTransport(handler)
    )
    assert notifier.notify("标题", "正文", url="http://127.0.0.1:8765/") is True
    assert seen["url"] == "http://bridge.test/notify"
    assert seen["token"] == "s3cret"
    assert seen["payload"] == {
        "title": "标题",
        "body": "正文",
        "url": "http://127.0.0.1:8765/",
    }


def test_webhook_reports_http_error():
    notifier = WebhookNotifier(
        "http://bridge.test/notify",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    assert notifier.notify("t", "b") is False
    assert "500" in (notifier.error or "")


def test_webhook_reports_unreachable_bridge():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    notifier = WebhookNotifier(
        "http://bridge.test/notify", transport=httpx.MockTransport(boom)
    )
    assert notifier.notify("t", "b") is False
    assert notifier.error


def test_webhook_401_points_at_the_shared_token():
    """401 是最常见的配错：错误信息要直接说出「口令不一致」。"""
    notifier = WebhookNotifier(
        "http://bridge.test/notify",
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    assert notifier.notify("t", "b") is False
    assert "口令" in (notifier.error or "")


# --------------------------------------------------------------- 桥接探活


def test_health_url_is_derived_from_the_notify_url():
    notifier = WebhookNotifier("http://host.docker.internal:8766/notify")
    assert notifier.health_url == "http://host.docker.internal:8766/health"


def test_probe_says_the_bridge_is_alive():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    notifier = WebhookNotifier(
        "http://bridge.test/notify", transport=httpx.MockTransport(handler)
    )
    ok, error = notifier.probe()
    assert ok is True
    assert error is None
    assert seen["url"] == "http://bridge.test/health"


def test_probe_says_the_bridge_is_gone():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    notifier = WebhookNotifier(
        "http://bridge.test/notify", transport=httpx.MockTransport(boom)
    )
    ok, error = notifier.probe()
    assert ok is False
    assert "不可达" in (error or "")


def test_probe_without_a_url_is_not_reachable():
    notifier = WebhookNotifier("")
    ok, error = notifier.probe()
    assert ok is False
    assert error


def test_factory_prefers_the_bridge_over_the_local_backend():
    notifier = build_notifier("NGA Reminder", webhook_url="http://bridge.test/notify")
    assert isinstance(notifier, WebhookNotifier)
    assert notifier.url == "http://bridge.test/notify"


def test_local_dotenv_does_not_leak_into_the_settings():
    """本地的 .env 里可能有真凭据和桥接地址，测试必须看不见它们。"""
    from nga_reminder.config import Settings

    assert Settings().notify_webhook_token == ""
    assert Settings().notify_webhook == ""


# --------------------------------------------------------------- 桥接脚本


class FakeNotifier:
    """Stands in for the Windows toast backend."""

    error = None

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        self.sent.append((title, body, url))
        return True


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "scripts" / "notify_bridge.py"
    spec = importlib.util.spec_from_file_location("notify_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(bridge, notifier, token):
    bridge.BridgeHandler.notifier = notifier
    bridge.BridgeHandler.token = token
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_bridge_raises_a_notification_for_a_valid_post():
    bridge = _load_bridge()
    notifier = FakeNotifier()
    server, base = _serve(bridge, notifier, "right")
    try:
        response = httpx.post(
            f"{base}/notify",
            json={"title": "t", "body": "b", "url": "http://127.0.0.1:8765/"},
            headers={"X-NGA-Bridge-Token": "right"},
        )
        assert response.status_code == 200
        assert notifier.sent == [("t", "b", "http://127.0.0.1:8765/")]
    finally:
        server.shutdown()
        server.server_close()


def test_bridge_rejects_a_post_with_the_wrong_token():
    bridge = _load_bridge()
    notifier = FakeNotifier()
    server, base = _serve(bridge, notifier, "right")
    try:
        response = httpx.post(
            f"{base}/notify",
            json={"title": "t", "body": "b", "url": ""},
            headers={"X-NGA-Bridge-Token": "wrong"},
        )
        assert response.status_code == 401
        assert notifier.sent == []
    finally:
        server.shutdown()
        server.server_close()


def test_bridge_rejects_an_empty_notification():
    bridge = _load_bridge()
    notifier = FakeNotifier()
    server, base = _serve(bridge, notifier, "")
    try:
        response = httpx.post(f"{base}/notify", json={})
        assert response.status_code == 400
        assert notifier.sent == []
    finally:
        server.shutdown()
        server.server_close()
