"""通知窗口（不追溯）、一键清空、订阅时间、抓取间隔热调的行为测试。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nga_reminder.api import WEB_DIR, create_app
from nga_reminder.config import Settings
from nga_reminder.db import POLL_INTERVAL_KEY, Store
from nga_reminder.models import Floor
from nga_reminder.nga.bbcode import to_display_text
from nga_reminder.watcher import Watcher, WatcherService


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "view.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "vw.db")
    yield s
    s.close()


def _floor(pid: int, content: str = "内容", ts: int | None = None) -> Floor:
    """一条楼层。

    默认是「刚刚发的」：窗口是按**发帖时间**算的，楼层得落在窗口里才进得了
    通知流。需要一条「很久以前发的」楼层时显式传 ``ts``。
    """
    return Floor(
        pid=pid,
        lou=pid,
        author_uid=42,
        author_name="目标用户",
        timestamp=int(time.time()) if ts is None else ts,
        content=content,
    )


def _wait_for_next_second() -> None:
    """等到下一秒。

    窗口按整秒算：同一秒里发的楼层分不清是在窗口之前还是之后。要断言
    「重启之后不再追溯」，就得让两个窗口落在不同的秒上。
    """
    time.sleep(1.05 - (time.time() % 1))


def _record(store: Store, sub_id: int, floor: Floor, *, notified: bool = True) -> None:
    """记录一条楼层并（可选地）把它收进通知流。"""
    store.record_post(sub_id, floor, notified=notified)
    if notified:
        store.collect_posts(sub_id, [floor.pid])


# ------------------------------------------------------------ 正文格式


def test_display_text_keeps_spaces_and_newlines():
    raw = "第一行\n\n    第二行带缩进\n第三行"
    assert to_display_text(raw) == raw


def test_display_text_only_strips_markup():
    raw = "[b]粗体[/b]文字<br/>换行"
    out = to_display_text(raw)
    assert "[b]" not in out
    assert "<br/>" not in out
    assert "粗体文字" in out
    assert "换行" in out


def test_display_text_trims_only_the_outer_edges():
    # 首尾空白会去掉，行内的空格必须原样留着。
    assert to_display_text("\n\n  正文  中间  空格  \n\n") == "正文  中间  空格"


# ------------------------------------------------------------ 可见窗口


def test_reset_view_empties_the_feed_but_keeps_the_ledger(store):
    sub = store.upsert_subscription(100, 42)
    _record(store, sub.id, _floor(1))

    assert len(store.list_feed()) == 1

    stamp = store.reset_view()

    # 窗口里的内容没了，但去重账本还在——否则下一次轮询会把旧楼层再报一遍。
    assert store.list_feed() == []
    assert store.has_seen(sub.id, 1) is True
    assert len(store.list_posts(sub.id, since="")) == 1
    assert store.get_view_since() == stamp


def test_list_feed_hides_everything_before_the_window(store):
    sub = store.upsert_subscription(100, 42)
    # 很久以前发的那条：不管什么时候记下来的，都在窗口外面
    _record(store, sub.id, _floor(1, ts=1_700_000_000))
    store.reset_view()
    _record(store, sub.id, _floor(2))

    feed = store.list_feed()
    assert [item["pid"] for item in feed] == [2]


def test_unread_count_respects_the_window(store):
    sub = store.upsert_subscription(100, 42)
    _record(store, sub.id, _floor(1, ts=1_700_000_000))
    # 还没设窗口：什么都算
    assert store.unread_count(sub.id) == 1

    # 窗口定在现在：那条很久以前发的楼层不在窗口里
    store.set_view_since(datetime.now(UTC).isoformat())
    assert store.unread_count(sub.id) == 0

    _record(store, sub.id, _floor(2))
    assert store.unread_count(sub.id) == 1


def test_feeds_only_contain_collected_floors(store):
    sub = store.upsert_subscription(100, 42)
    # 没有被收录的楼层不进通知流
    store.record_post(sub.id, _floor(1), notified=True)
    _record(store, sub.id, _floor(2))

    assert [item["pid"] for item in store.list_feed()] == [2]


def test_feed_is_ordered_newest_first(store):
    sub = store.upsert_subscription(100, 42)
    # 通知流按收录顺序倒排：最新的在最上面
    _record(store, sub.id, _floor(1))
    _record(store, sub.id, _floor(2))
    _record(store, sub.id, _floor(3))

    assert [item["pid"] for item in store.list_feed()] == [3, 2, 1]


# ------------------------------------------------------------ 订阅时间


def test_collect_since_pulls_in_alerted_floors(store):
    sub = store.upsert_subscription(100, 42)
    # 已提醒但还没收录的楼层
    store.record_post(sub.id, _floor(1), notified=True)
    assert store.list_feed() == []

    collected = store.collect_since("2000-01-01T00:00:00+00:00")
    assert collected == 1
    assert [item["pid"] for item in store.list_feed()] == [1]


def test_a_floor_is_collected_only_once(store):
    sub = store.upsert_subscription(100, 42)
    store.record_post(sub.id, _floor(1), notified=True)

    assert store.collect_since("2000-01-01T00:00:00+00:00") == 1
    # 再收一次不会重复收录
    assert store.collect_since("2000-01-01T00:00:00+00:00") == 0
    assert len(store.list_feed()) == 1


def test_moving_the_time_back_collects_the_missing_floors(store):
    sub = store.upsert_subscription(100, 42)
    store.record_post(sub.id, _floor(1), notified=True)
    store.record_post(sub.id, _floor(2), notified=True)

    # 订阅时间设到很早：两条都该被收进来
    collected = store.set_view_since("2000-01-01T00:00:00+00:00")
    assert collected == 2
    assert [item["pid"] for item in store.list_feed()] == [2, 1]


def test_moving_the_time_forward_hides_older_floors(store):
    sub = store.upsert_subscription(100, 42)
    _record(store, sub.id, _floor(1))
    assert len(store.list_feed()) == 1

    # 订阅时间设到未来一点点的现在之后：窗口里的内容被挡掉
    store.set_view_since("2999-01-01T00:00:00+00:00")
    assert store.list_feed() == []


def test_setting_the_window_clears_the_stale_alerts(store):
    """早于订阅时间却标成「报过」的楼层要改回没报过。

    老逻辑会把断层回退翻出来的老楼层当新回复报出去，账本里于是留下
    「早于窗口却报过」的记录。窗口定下来之后这些记录就该清掉：通知流里
    只剩订阅时间之后的内容，而去重账本照旧留着（免得下次又报一遍）。
    """
    sub = store.upsert_subscription(100, 42)
    store.record_post(sub.id, _floor(1, ts=1_700_000_000), notified=True)
    store.collect_posts(sub.id, [1])
    assert len(store.list_feed()) == 1

    store.set_view_since("2023-11-14T23:00:00+00:00")

    assert store.list_feed() == []
    assert store.has_seen(sub.id, 1) is True


def test_the_feed_carries_the_posting_time(client, monkeypatch):
    """通知流条目要带发帖时间——界面显示的就是它。"""
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    posted = int(time.time())
    store.record_post(sub_id, _floor(778, ts=posted), notified=True)
    store.collect_posts(sub_id, [778])
    store.close()

    feed = client.get("/api/notifications").json()
    assert len(feed) == 1
    assert feed[0]["ts"] == posted


def test_the_feed_shows_the_posting_time_not_the_collection_time():
    """界面上显示的时间必须是发帖时间。

    收录时间是「我们什么时候记下这条的」——同一轮里抓回来的一批楼层会被
    写成同一个时刻，用户看到的就是「所有回复都是同一时间发的」。
    """
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "timeEl(n.created_at" not in app_js
    assert "n.ts * 1000" in app_js


def test_the_subscription_time_is_saved_once_after_the_drag():
    """拖时间滑杆不能一路发请求：停手之后才保存一次。

    日期组件每拖一格就派发一次 change。要是每 change 都存一次，就是一场
    请求风暴——每次保存服务端都要按新时间重扫一遍订阅。

    触发点只有一个：日历组件被收起的时候。change 只负责把最新的值记下来，
    等用户停手了再存一次。
    """
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    handler = app_js.split('timeInput.addEventListener("change"')[1].split("});")[0]
    # change 只负责记下最新的值，不直接存。
    assert "scheduleViewSinceSave(value)" in handler
    assert "applyViewSince" not in handler
    # 「有没有真的变」要留到保存的时候再判断：在 change 里判断会出错——
    # 拖滑杆途中会路过服务端那个值，一判断就跳过去，最后存的是上一步的值。
    assert "lastSyncedLocal" not in handler
    assert "lastAppliedLocal" not in handler

    # 组件被收起：判断一下有没有变更，有就攒下来。
    on_hide = app_js.split("onHide: () => {")[1].split("},")[0]
    assert "scheduleViewSinceSave(" in on_hide

    # 攒着 + 等停手：攒下的值只存一次。
    assert "function scheduleViewSinceSave" in app_js
    assert "function flushViewSinceSave" in app_js
    flush = app_js.split("function flushViewSinceSave")[1].split("\n}")[0]
    assert "applyViewSince(value)" in flush
    assert "value === lastSyncedLocal" in flush
    assert "value === lastAppliedLocal" in flush
    # 全文件只有这一个地方真的去存（函数定义不算）。
    assert app_js.count("applyViewSince(value);") == 1
    # 等用户停手：攒值的时候重置一次计时器。
    assert "setTimeout(flushViewSinceSave" in app_js

    # 不需要去监听滑杆的按下/松开。
    assert "sliderDown" not in app_js
    assert 'input[type="range"]' not in app_js


def test_images_keep_their_url_in_the_display_text():
    """图片得留着地址——界面靠它把图片还原出来。"""
    raw = (
        "[img]https://img.nga.cn/attachments/mon_202611/20/"
        "axyfQ47-f0s4ZhT3cSwi-xc.jpeg[/img]"
    )
    out = to_display_text(raw)

    assert out.startswith("[图片]https://img.nga.cn/attachments/")
    assert ".jpeg" in out
    assert "[/img]" not in out


def test_the_frontend_renders_post_images():
    """正文里的图片要变成图片，不是「[图片]https://…」这一串字。"""
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "appendContent" in app_js
    assert "post-img" in app_js


def test_a_missing_image_does_not_leave_a_broken_placeholder():
    """附件被 NGA 删掉时，界面换成一行说明，不留破图占位。"""
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "markMissingImage" in app_js
    # NGA 的「图片不存在」回的是一张 1x1 的占位 GIF——那是「加载成功」，
    # error 事件根本不触发，只能靠 naturalWidth 认出来。
    assert "naturalWidth" in app_js
    assert "post-img-missing" in app_js

    style_css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert ".post-img-missing" in style_css


# ------------------------------------------------------------ HTTP 接口


def _make_subscription(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))
    res = client.post(
        "/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516}
    )
    assert res.status_code == 200
    return res.json()["id"]


def test_notifications_endpoint_returns_plain_text_with_newlines(client, monkeypatch):
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    raw = "第一行\n\n第二行   带空格"
    store.record_post(sub_id, _floor(777, raw), notified=True, quote_text="引用：原文")
    store.collect_posts(sub_id, [777])
    store.close()

    feed = client.get("/api/notifications").json()
    assert len(feed) == 1
    item = feed[0]
    assert item["content"] == raw
    assert item["quote_text"] == "引用：原文"
    assert item["url"] == "https://bbs.nga.cn/read.php?tid=46046493&pid=777"


def test_notifications_endpoint_returns_the_newest_first(client, monkeypatch):
    """最新的通知排在最上面。"""
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    for pid in (781, 782, 783):
        _record(store, sub_id, _floor(pid))
    store.close()

    feed = client.get("/api/notifications").json()
    assert [item["pid"] for item in feed] == [783, 782, 781]


def test_clear_endpoint_empties_the_feed_without_forgetting_floors(client, monkeypatch):
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    _record(store, sub_id, _floor(778))
    store.close()

    assert len(client.get("/api/notifications").json()) == 1

    res = client.post("/api/notifications/clear")
    assert res.status_code == 200
    assert res.json()["cleared"] is True

    assert client.get("/api/notifications").json() == []
    # 去重账本必须还在
    store = Store(Settings().db_path)
    assert store.has_seen(sub_id, 778) is True
    assert len(store.list_posts(sub_id, since="")) == 1
    store.close()


def test_clear_resets_the_subscription_time(client, monkeypatch):
    _make_subscription(client, monkeypatch)
    before = client.get("/api/settings").json()["view_since"]

    res = client.post("/api/notifications/clear")

    assert res.json()["view_since"] >= before
    assert client.get("/api/settings").json()["view_since"] == res.json()["view_since"]


def test_starting_the_app_starts_a_fresh_window(client, monkeypatch):
    """重启后不追溯：启动时已有的楼层不再出现在列表里。"""
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    _record(store, sub_id, _floor(779))
    store.close()

    assert len(client.get("/api/notifications").json()) == 1

    # lifespan 的 startup 会重置窗口。窗口按整秒算，所以要让新窗口落在
    # 下一秒，否则那条楼层跟窗口在同一秒里，分不清前后。
    _wait_for_next_second()
    with TestClient(create_app(Settings())) as fresh:
        assert fresh.get("/api/notifications").json() == []
        assert fresh.get("/api/status").json()["view_since"]


def test_settings_endpoint_reports_the_interval(client):
    body = client.get("/api/settings").json()
    assert body["poll_interval"] == 60
    assert body["app_url"].startswith("http")
    assert "view_since" in body


def test_poll_interval_can_be_changed_and_stops_fetching(client):
    res = client.post("/api/settings/poll-interval", json={"seconds": 0})
    assert res.status_code == 200
    assert res.json() == {"poll_interval": 0, "fetching": False}

    assert client.get("/api/settings").json()["poll_interval"] == 0
    assert client.get("/api/status").json()["poll_interval"] == 0

    # 写进了本地库，重启后仍然是「关闭」（界面上那一档叫「关闭」）
    store = Store(Settings().db_path)
    assert store.get_value(POLL_INTERVAL_KEY) == "0"
    store.close()


def test_poll_interval_is_validated(client):
    assert client.post("/api/settings/poll-interval", json={"seconds": -1}).status_code == 422
    assert client.post("/api/settings/poll-interval", json={"seconds": 999999}).status_code == 422


def test_poll_interval_survives_a_restart(client, monkeypatch):
    client.post("/api/settings/poll-interval", json={"seconds": 300})
    with TestClient(create_app(Settings())) as fresh:
        assert fresh.get("/api/settings").json()["poll_interval"] == 300


def test_subscription_time_can_be_moved(client, monkeypatch):
    sub_id = _make_subscription(client, monkeypatch)
    store = Store(Settings().db_path)
    store.record_post(sub_id, _floor(880), notified=True)
    store.close()

    # 往前挪订阅时间，那条楼层就被收进来了
    res = client.post("/api/settings/view-since", json={"view_since": "2000-01-01T00:00"})
    assert res.status_code == 200
    assert res.json()["collected"] == 1

    feed = client.get("/api/notifications").json()
    assert [item["pid"] for item in feed] == [880]

    assert client.get("/api/settings").json()["view_since"] == res.json()["view_since"]


def test_subscription_time_in_the_future_freezes_fetching(client, monkeypatch):
    """把订阅时间设到将来 = 暂停到那一刻再开始，而不是报错。

    服务端不再拒绝未来的时间：用户明确要的是「选一个未来的日期，
    到那天之前别抓取」。
    """
    _make_subscription(client, monkeypatch)

    res = client.post("/api/settings/view-since", json={"view_since": "2999-01-01T00:00"})
    assert res.status_code == 200
    body = res.json()
    assert body["resume_at"] == body["view_since"]

    status = client.get("/api/status").json()
    assert status["resume_at"] == body["view_since"]
    assert status["watcher"]["paused"] is True
    assert status["watcher"]["resume_at"] == body["view_since"]


def test_subscription_time_in_the_past_unfreezes(client, monkeypatch):
    """改回过去（或当下）的时间就立刻解冻。"""
    _make_subscription(client, monkeypatch)

    client.post("/api/settings/view-since", json={"view_since": "2999-01-01T00:00"})
    res = client.post("/api/settings/view-since", json={"view_since": "2020-05-05T10:30:00+08:00"})
    assert res.status_code == 200
    assert res.json()["resume_at"] is None

    status = client.get("/api/status").json()
    assert status["resume_at"] == ""
    assert status["watcher"]["paused"] is False


def test_clearing_the_feed_also_lifts_the_freeze(client, monkeypatch):
    _make_subscription(client, monkeypatch)

    client.post("/api/settings/view-since", json={"view_since": "2999-01-01T00:00"})
    assert client.get("/api/status").json()["resume_at"] != ""

    client.post("/api/notifications/clear")
    assert client.get("/api/status").json()["resume_at"] == ""


def test_a_freeze_ends_by_itself_once_the_moment_passes(store):
    """到点自动恢复：过了那个时刻，冻结记录自己消失。"""
    from datetime import UTC, datetime, timedelta

    soon = datetime.now(UTC) + timedelta(minutes=5)
    store.set_resume_at(soon.isoformat())
    assert store.pending_resume(datetime.now(UTC)) == soon.isoformat()

    later = datetime.now(UTC) + timedelta(minutes=10)
    assert store.pending_resume(later) is None
    assert store.get_resume_at() == ""


def test_subscription_time_honours_the_offset_it_carries(client, monkeypatch):
    """带时区的时间戳按自己的时区算，不受服务端机器时区影响。

    界面在浏览器里把「墙上时间」补上浏览器所在时区再发过来，所以服务端
    不需要、也不应该去猜时区。容器跑在 UTC、桌面跑在 UTC+8，两边才对得上。
    """
    _make_subscription(client, monkeypatch)

    res = client.post(
        "/api/settings/view-since", json={"view_since": "2020-05-05T10:30:00+08:00"}
    )
    assert res.status_code == 200
    assert res.json()["view_since"] == "2020-05-05T02:30:00+00:00"


def test_subscription_time_accepts_utc(client, monkeypatch):
    _make_subscription(client, monkeypatch)

    res = client.post(
        "/api/settings/view-since", json={"view_since": "2020-05-05T02:30:00Z"}
    )
    assert res.status_code == 200
    assert res.json()["view_since"] == "2020-05-05T02:30:00+00:00"


def test_subscription_time_rejects_garbage(client):
    res = client.post("/api/settings/view-since", json={"view_since": "下周三"})
    assert res.status_code == 400
    assert "格式" in res.json()["detail"]


def test_subscription_time_survives_a_restart(client, monkeypatch):
    client.post("/api/settings/view-since", json={"view_since": "2020-05-05T10:30"})
    with TestClient(create_app(Settings())) as fresh:
        assert fresh.get("/api/settings").json()["view_since"].startswith("2020-05-05")


# ------------------------------------------------------------ 轮询循环


class _CountingClient:
    """只数被调用了几次。"""

    def __init__(self):
        self.calls = 0
        self.has_credentials = True

    def get_thread_page(self, tid: int, page: int = 1, author_id: int = 0):
        self.calls += 1
        from nga_reminder.models import ThreadPage

        return ThreadPage(
            tid=tid,
            page=page,
            total_page=1,
            per_page=20,
            floor_count=0,
            title="标题",
            forum_name="分区",
            author_uid=1,
            author_name="楼主",
            floors=[],
        )


class _QuietNotifier:
    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        return True


async def test_service_does_not_poll_while_the_interval_is_zero(store):
    store.upsert_subscription(100, 42)
    client = _CountingClient()
    watcher = Watcher(store, client, _QuietNotifier())
    service = WatcherService(watcher, interval=0)
    service.start()
    try:
        await asyncio.sleep(0.15)
        assert client.calls == 0
        assert service.interval == 0
    finally:
        await service.stop()


async def test_service_resumes_as_soon_as_the_interval_comes_back(store):
    store.upsert_subscription(100, 42)
    client = _CountingClient()
    watcher = Watcher(store, client, _QuietNotifier())
    service = WatcherService(watcher, interval=0)
    service.start()
    try:
        await asyncio.sleep(0.1)
        assert client.calls == 0

        service.set_interval(60)
        await asyncio.sleep(0.3)

        assert client.calls >= 1
        assert service.interval == 60
    finally:
        await service.stop()


async def test_service_reads_the_interval_from_a_provider(store):
    """间隔存在本地库里，界面改完立刻生效，不需要重启。"""
    store.upsert_subscription(100, 42)
    current = {"seconds": 0}
    client = _CountingClient()
    watcher = Watcher(store, client, _QuietNotifier())
    service = WatcherService(watcher, interval=60, interval_provider=lambda: current["seconds"])
    service.start()
    try:
        await asyncio.sleep(0.1)
        assert client.calls == 0

        current["seconds"] = 30
        service.set_interval(30)
        await asyncio.sleep(0.3)

        assert client.calls >= 1
        assert service.interval == 30
    finally:
        await service.stop()


def test_frontend_never_lets_the_picker_rewrite_the_subscription_time():
    """组件回灌不许改掉用户设的订阅时间。

    Air Datepicker 的 selectDate() 在 updateTime 选项打开时会把时间换成
    「组件以为的现在」——那个「现在」是页面加载那一刻（opts.startDate）固化的。
    标签页开久了（甚至是昨天开的），15 秒一次的刷新就会把组件算出来的旧时间
    回灌进输入框，而 change 事件在我们这儿意味着「用户改了时间」，
    于是用户设的订阅时间被静默改掉。所以同步时不能打开这个选项。

    同理，再点一次已选中的日期不该把选择取消掉（那是误触，不是反悔）。
    """
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "updateTime: true" not in app_js
    assert "toggleSelected: false" in app_js
