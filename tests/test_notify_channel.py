"""通知通道的可见性与自检。

容器里跑着的时候，真正的桌面通知由**宿主上的前台进程**代发
（``scripts/notify_bridge.py``）。那个进程不是开机自启的——重启或关掉窗口
就没了，而症状很隐蔽：通知只是落进日志，界面上什么提示都没有。

所以这一组测试钉住三件事：

1. 状态接口要**说得出**通知通道通不通；
2. 界面上要**看得见**「桥接不可达」，并给出怎么修；
3. 有一条命令能**自检**整条链路。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nga_reminder.api import WEB_DIR, create_app
from nga_reminder.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "notify.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def bridge_client(tmp_path, monkeypatch):
    """一个**配了桥接地址**的应用（容器里的样子）。"""
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    monkeypatch.setenv("NGA_NOTIFY_WEBHOOK", "http://host.docker.internal:8766/notify")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _app_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def _style_css() -> str:
    return (WEB_DIR / "style.css").read_text(encoding="utf-8")


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "scripts" / "notify_bridge.py"
    spec = importlib.util.spec_from_file_location("notify_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------- 状态接口


def test_status_always_reports_the_notification_channel(client):
    """状态接口要有一 ``notify`` 段——不然界面无从判断。"""
    body = client.get("/api/status").json()
    notify = body["notify"]
    assert "backend" in notify
    assert "webhook" in notify
    assert "reachable" in notify
    # 没有配桥接地址 = 原生跑（Windows 后端自己弹），不是「坏了」。
    assert notify["webhook"] is False
    assert notify["reachable"] is not False


def test_status_marks_an_unreachable_bridge(bridge_client, monkeypatch):
    """桥接连不上时，状态接口要明确说「不可达」并带上原因。"""
    from nga_reminder.notify.webhook import WebhookNotifier

    monkeypatch.setattr(
        WebhookNotifier,
        "probe",
        lambda self: (False, "通知桥接不可达: [Errno 111] Connection refused"),
    )
    notify = bridge_client.get("/api/status").json()["notify"]
    assert notify["webhook"] is True
    assert notify["reachable"] is False
    assert "不可达" in notify["error"]
    # 桥接地址本身要露出来（自检脚本要拿它跟本机比）。
    assert notify["webhook_url"] == "http://host.docker.internal:8766/notify"


def test_status_does_not_probe_the_bridge_on_every_call(bridge_client, monkeypatch):
    """状态栏几秒轮询一次，不能每次都去戳桥接——结果要缓存。"""
    from nga_reminder.notify.webhook import WebhookNotifier

    calls: list[int] = []

    def probe(self):
        calls.append(1)
        return True, None

    monkeypatch.setattr(WebhookNotifier, "probe", probe)
    bridge_client.get("/api/status")
    bridge_client.get("/api/status")
    bridge_client.get("/api/status")
    assert len(calls) == 1


def test_test_notification_reports_the_bridge_error(bridge_client, monkeypatch):
    """"测试通知"失败时要把原因带回来，不能只说一句「失败」。"""
    from nga_reminder.notify.webhook import WebhookNotifier

    monkeypatch.setattr(
        WebhookNotifier,
        "notify",
        lambda self, title, body, url="": False,
    )
    monkeypatch.setattr(
        WebhookNotifier,
        "error",
        property(lambda self: "通知桥接不可达: [Errno 111] Connection refused"),
    )
    body = bridge_client.post("/api/notify/test").json()
    assert body["delivered"] is False
    assert "不可达" in body["error"]


# ------------------------------------------------------------- 前端守卫


def test_the_ui_shows_a_banner_when_the_bridge_is_gone():
    """桥接不可达要有一条横幅——不能只在 docker logs 里。"""
    html = _index_html()
    assert 'id="notify-banner"' in html
    assert 'id="notify-banner-text"' in html
    # 横幅里要写清楚怎么修。
    assert "scripts/notify_bridge.py" in html

    app_js = _app_js()
    assert "updateNotifyBanner" in app_js
    assert "n.reachable !== false" in app_js

    css = _style_css()
    assert ".banner-fix {" in css


def test_the_test_notification_button_sits_in_the_add_subscription_modal():
    """「测试通知」放在添加订阅弹窗的左下角。"""
    html = _index_html()
    modal = html.split('id="modal-add"', 1)[1].split("</footer>", 1)[0]
    assert 'id="notify-test"' in modal
    assert 'id="notify-test-result"' in modal
    # 在左下角：按钮排在 .spacer 之前（spacer 把右侧两个按钮推走）。
    assert modal.index('id="notify-test"') < modal.index('class="spacer"')

    app_js = _app_js()
    assert '$("notify-test").onclick = testNotification' in app_js
    assert '"/api/notify/test"' in app_js
    assert "setNotifyResult" in app_js

    css = _style_css()
    assert ".notify-result {" in css


def test_the_status_bar_also_hints_that_the_bridge_is_down():
    """横幅会随通知流滚走，状态栏也要露一眼。"""
    app_js = _app_js()
    assert "通知桥接不可达" in app_js


# ------------------------------------------------------------- 自检脚本


def test_the_bridge_has_a_check_mode():
    """一条命令能查完整条链路。"""
    bridge = _load_bridge()
    assert hasattr(bridge, "run_checks")
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert '"--check"' in source
    # 自检要覆盖这四段：Windows 后端 / 桥接进程 / 两侧口令 / 容器指向。
    for probe in ("通知后端", "通知桥接没在运行", "共享口令", "容器侧"):
        assert probe in source, probe


def test_the_bridge_can_read_a_named_key_from_the_dotenv(tmp_path):
    """自检要同时读两端的口令，所以得能按名字取键。"""
    bridge = _load_bridge()
    env = tmp_path / ".env"
    env.write_text(
        "NGA_BRIDGE_TOKEN=aaa\nNGA_NOTIFY_WEBHOOK_TOKEN=bbb\n", encoding="utf-8"
    )
    assert bridge._token_from_dotenv(env) == "aaa"
    assert bridge._token_from_dotenv(env, "NGA_NOTIFY_WEBHOOK_TOKEN") == "bbb"
    assert bridge._token_from_dotenv(env, "NOPE") == ""


def test_a_double_clickable_launcher_exists():
    """双击就能起桥接——省得记命令。"""
    path = Path(__file__).resolve().parents[1] / "scripts" / "start_notify_bridge.cmd"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "notify_bridge.py" in text


# ------------------------------------------------- 浏览器通知（兜底通道）


def test_the_ui_offers_a_browser_notification_fallback():
    """桌面通道不通时，得还有一条能响的路。"""
    html = _index_html()
    assert 'id="notify-browser-enable"' in html
    assert 'id="browser-notify-toggle"' in html
    assert 'id="notify-banner-fallback"' in html

    app_js = _app_js()
    assert "toggleBrowserNotify" in app_js
    assert "maybeBrowserNotify" in app_js
    assert "Notification.requestPermission()" in app_js


def test_the_browser_notification_switch_sits_next_to_the_test_button():
    """开关紧挨着「测试通知」——都在添加订阅弹窗的左下角。"""
    html = _index_html()
    modal = html.split('id="modal-add"', 1)[1].split("</footer>", 1)[0]
    assert 'id="notify-test"' in modal
    assert 'id="browser-notify-toggle"' in modal
    # 两个都在 .spacer 之前（spacer 把右侧两个按钮推走）。
    assert modal.index('id="notify-test"') < modal.index('class="spacer"')
    assert modal.index('id="browser-notify-toggle"') < modal.index('class="spacer"')

    app_js = _app_js()
    assert '$("browser-notify-toggle").onclick = toggleBrowserNotify' in app_js
    assert '$("notify-browser-enable").onclick = toggleBrowserNotify' in app_js


def test_the_browser_channel_is_only_a_fallback_not_a_second_one():
    """桌面通道正常时不能也响一遍——一件事响两次是骚扰。"""
    app_js = _app_js()
    body = app_js.split("function maybeBrowserNotify(", 1)[1].split("\n}", 1)[0]
    assert "browserNotifyReady()" in body
    # 用每条回复的实际送达状态，健康检查只为旧数据兜底。
    assert "items.filter((item) => !(item.delivered ?? desktopChannelOk()))" in body


def test_the_desktop_channel_state_comes_from_the_server():
    """「桌面通道通不通」不能靠猜——看状态接口报的。"""
    app_js = _app_js()
    assert "state.notify = nt;" in app_js
    body = app_js.split("function desktopChannelOk()", 1)[1].split("\n}", 1)[0]
    assert "state.notify" in body
    assert "n.reachable !== false" in body


def test_the_history_is_not_mistaken_for_brand_new_notifications():
    """第一次加载时列表是空的——那批历史内容不该被当成「刚来的」。"""
    app_js = _app_js()
    assert "feedPrimed: false" in app_js
    body = app_js.split("async function loadFeedHead(", 1)[1].split("\n}", 1)[0]
    assert "state.feedPrimed" in body
    assert "!reset && state.feedPrimed" in body
    assert "collected.filter((n) => !known.has(n.pid))" in body
    assert "maybeBrowserNotify(arrived)" in body


def test_the_browser_notification_preference_is_remembered():
    """开关的状态得记住——不然每次刷新都要重开一遍。"""
    app_js = _app_js()
    assert 'BROWSER_NOTIFY_KEY = "nga-browser-notify"' in app_js
    assert "localStorage.setItem(BROWSER_NOTIFY_KEY" in app_js
    assert "localStorage.getItem(BROWSER_NOTIFY_KEY)" in app_js


def test_the_banner_says_when_the_browser_channel_has_taken_over():
    """兜底通道接管了，横幅上要写明白，别让人以为桌面通知修好了。"""
    app_js = _app_js()
    assert 'notify-banner-fallback' in app_js
    assert "browserNotifyReady()" in app_js
    html = _index_html()
    assert "浏览器通知已接管兜底" in html
