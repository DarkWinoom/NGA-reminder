"""Tests for the polling / incremental detection logic."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest

from nga_reminder.db import Store
from nga_reminder.models import Floor, ThreadPage
from nga_reminder.nga.errors import NgaAuthError, NgaThreadUnavailableError
from nga_reminder.watcher import Watcher, WatcherService


class FakeClient:
    """Serves canned pages, one dict per (page, author_id).

    An *unfiltered* request (``author_id == 0``) falls back to the same page
    number of whichever author has it -- that is what a thread with one
    interesting author looks like without the server-side filter.
    """

    def __init__(self, pages=None, error=None, delay: float = 0.0):
        self.pages = pages or {}
        self.error = error
        self.delay = delay
        self.calls: list[tuple[int, int, int]] = []
        self.has_credentials = True
        # 并发探针：同一时刻最多有几个请求在飞。
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()

    def _lookup(self, page: int, author_id: int) -> ThreadPage | None:
        key = (page, author_id)
        if key in self.pages:
            return self.pages[key]
        if author_id == 0:
            # 不带过滤的页：拿同一个页号的作者的页顶上。
            for (p, a) in self.pages:
                if a != 0 and p == page:
                    return self.pages[(p, a)]
            return None
        if key in {(p, 0) for p, _ in self.pages}:
            return self.pages[(page, 0)]
        return None

    def get_thread_page(self, tid: int, page: int = 1, author_id: int = 0) -> ThreadPage:
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            self.calls.append((tid, page, author_id))
            if self.delay:
                time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            found = self._lookup(page, author_id)
            if found is not None:
                return found
            return ThreadPage(
                tid=tid,
                page=page,
                # 不带过滤的空页：它就是最后一页。
                total_page=page if author_id == 0 else 1,
                per_page=20,
                floor_count=0,
                title="标题",
                forum_name="分区",
                author_uid=1,
                author_name="楼主",
                floors=[],
            )
        finally:
            with self._lock:
                self._active -= 1


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        self.sent.append((title, body, url))
        return True


def floor(pid: int, uid: int = 42, content: str = "内容", ts: int = 1_700_000_000) -> Floor:
    return Floor(
        pid=pid,
        lou=0,
        author_uid=uid,
        author_name="目标用户",
        timestamp=ts,
        content=content,
    )


def page(floors, total_page: int = 1, tid: int = 100, title: str = "某主题") -> ThreadPage:
    return ThreadPage(
        tid=tid,
        page=1,
        total_page=total_page,
        per_page=20,
        floor_count=len(floors),
        title=title,
        forum_name="分区",
        author_uid=1,
        author_name="楼主",
        floors=floors,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "w.db")
    yield s
    s.close()


def build(store, client, notifier) -> Watcher:
    return Watcher(store, client, notifier)


def stamp(ts: int) -> str:
    """Unix 秒 -> 订阅时间存的那种 ISO 时间戳。"""
    return datetime.fromtimestamp(ts, UTC).isoformat()


# ---------------------------------------------------------------- baseline


def test_first_poll_only_establishes_a_baseline(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10), floor(20), floor(30)])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)

    posts = watcher.poll_subscription(sub)

    assert posts == []
    assert notifier.sent == []
    assert sub_id_last_pid(store, sub.id) == 30
    assert store.initialized(sub.id) is True
    # the existing floors are remembered, but not as unread
    assert len(store.list_posts(sub.id)) == 3
    assert store.unread_count(sub.id) == 0


def test_baseline_handles_a_thread_with_no_posts_yet(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([])})
    watcher = build(store, client, FakeNotifier())

    assert watcher.poll_subscription(sub) == []
    assert store.initialized(sub.id) is True


# ------------------------------------------------- 订阅时间 / 同主题复用


def test_the_baseline_reports_only_what_came_after_the_subscription_time(store):
    """新加订阅按订阅时间切一刀：之后的回复要报，以前的只记账。

    旧做法是把第一次抓到的楼层全部静默吞掉——用户设的订阅时间就白设了。
    """
    store.set_view_since(stamp(1_700_000_000))
    sub = store.upsert_subscription(100, 42)
    client = FakeClient(
        {
            (1, 42): page(
                [
                    floor(10, ts=1_699_999_000, content="窗口外"),
                    floor(20, ts=1_700_000_500, content="窗口内"),
                ]
            )
        }
    )
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)

    watcher.poll_all()

    # 窗口里的那条报了（弹窗 + 进通知流），窗口外的那条只记账
    assert len(notifier.sent) == 1
    assert "窗口内" in notifier.sent[0][1]
    assert store.unread_count(sub.id) == 1
    assert store.has_seen(sub.id, 10) is True
    assert store.has_seen(sub.id, 20) is True


def test_the_baseline_looks_back_for_the_replies_inside_the_window(store):
    """窗口跨了好几页：往前翻页，把窗口里的回复都找出来。"""
    store.set_view_since(stamp(1_700_000_000))
    store.upsert_subscription(100, 42)
    client = FakeClient(
        {
            (1, 42): page([floor(10, ts=1_699_000_000)], total_page=3),
            (2, 42): page([floor(20, ts=1_699_500_000)], total_page=3),
            (3, 42): page([floor(30, ts=1_700_000_500)], total_page=3),
        }
    )
    watcher = build(store, client, FakeNotifier())

    posts = watcher.poll_subscription(store.list_subscriptions()[0])

    assert [p.floor.pid for p in posts] == [30]


def test_moving_the_subscription_time_back_reports_the_older_replies(store):
    """订阅时间往前挪：这段时间里的回复重新变成「该报的」。"""
    store.upsert_subscription(100, 42)
    client = FakeClient(
        {(1, 42): page([floor(10, ts=1_700_000_000)]), (1, 0): page([floor(10)])}
    )
    watcher = build(store, client, FakeNotifier())
    watcher.poll_subscription(store.list_subscriptions()[0])

    store.set_view_since(stamp(1_699_999_000))
    posts = watcher.rescan_subscription(store.list_subscriptions()[0])

    assert [p.floor.pid for p in posts] == [10]


def test_a_gap_backfill_does_not_report_floors_older_than_the_window(store):
    """断层回退翻出来的老楼层不该被当成新回复弹出去。

    真实的坑：轮询时发现尾页和高水位之间有断层，往前翻页补齐——补出来的
    是更早写的楼层。老逻辑把它们一律当新回复报出去，于是用户看到的偏偏是
    订阅时间**之前**的内容。窗口外的那条只记账，不弹窗、不进通知流。
    """
    store.set_view_since(stamp(1_700_000_000))
    store.upsert_subscription(100, 42)
    client = FakeClient(
        {
            (1, 42): page([floor(10, ts=1_700_000_500)], total_page=2),
            (2, 42): page([floor(20, ts=1_700_000_600)], total_page=2),
        }
    )
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)

    # 第一轮：建基线，窗口里的那条报了
    watcher.poll_all()
    assert len(notifier.sent) == 1
    notifier.sent.clear()

    # 主题又长了一页：尾页上全是新楼层。断层回退会回到前一页，把一条
    # 「订阅时间之前发的」楼层也翻出来。
    client.pages[(2, 42)] = page(
        [floor(30, ts=1_700_000_700), floor(40, ts=1_700_000_800)], total_page=2
    )
    client.pages[(1, 42)] = page(
        [floor(10, ts=1_700_000_500), floor(25, ts=1_699_999_000)], total_page=2
    )

    watcher.poll_all()

    # 只有窗口里的两条被报了
    assert len(notifier.sent) == 1
    # 窗口外的那条记了账，但没进通知流
    assert store.has_seen(store.list_subscriptions()[0].id, 25) is True
    feed_pids = [item["pid"] for item in store.list_feed()]
    assert 25 not in feed_pids
    assert set(feed_pids) == {10, 20, 30, 40}


def test_the_subscription_time_also_filters_the_steady_state_polls(store):
    """稳态轮询里新发现的楼层也要过窗口这一关。"""
    store.set_view_since(stamp(1_700_000_000))
    store.upsert_subscription(100, 42)
    client = FakeClient(
        {
            (1, 42): page([floor(10, ts=1_700_000_500)]),
            (1, 0): page([floor(10, ts=1_700_000_500)]),
        }
    )
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_all()
    notifier.sent.clear()

    # 订阅时间被挪到这些楼层之后：它们就都在窗口外面了
    store.set_view_since(stamp(1_700_001_000))
    client.pages[(1, 42)] = page(
        [floor(10, ts=1_700_000_500), floor(20, ts=1_700_000_600)]
    )
    client.pages[(1, 0)] = page(
        [floor(10, ts=1_700_000_500), floor(20, ts=1_700_000_600)]
    )

    watcher.poll_all()

    assert notifier.sent == []
    assert store.unread_count(store.list_subscriptions()[0].id) == 0


def test_the_complete_content_wins_over_the_truncated_one(store):
    """同一层楼，用不带过滤的那份内容。

    NGA 带 ``authorid`` 过滤时会把图片地址截断——只剩一段相对路径，连
    扩展名都没有；不带过滤的响应里才是完整的地址。地址残缺，界面就渲染
    不出图，所以两份都拿到时以完整的那份为准。
    """
    sub = store.upsert_subscription(100, 42)
    truncated = floor(
        10, content="[img]./mon_202611/20/axyfQ47-f0s4Zh[/img]"
    )
    complete = floor(
        10,
        content=(
            "[img]https://img.nga.cn/attachments/mon_202611/20/"
            "axyfQ47-f0s4ZhT3cSwi-xc.jpeg[/img]"
        ),
    )
    client = FakeClient({(1, 42): page([truncated]), (1, 0): page([complete])})
    watcher = build(store, client, FakeNotifier())

    watcher.poll_subscription(store.list_subscriptions()[0])

    posts = store.list_posts(sub.id, since="")
    assert posts[0]["content"].startswith("[img]https://img.nga.cn/attachments/")


def test_subscriptions_on_the_same_thread_share_one_fetch(store):
    """同一个主题的两个订阅：共享的那一页只取一次。"""
    store.upsert_subscription(100, 42)
    store.upsert_subscription(100, 43)
    client = FakeClient(
        {
            (1, 42): page([floor(10, uid=42)]),
            (1, 43): page([floor(20, uid=43)]),
            (1, 0): page([floor(10, uid=42), floor(20, uid=43)]),
        }
    )
    watcher = build(store, client, FakeNotifier())

    watcher.poll_all()

    # 不带作者过滤的那一页（主题共享的那份）只取了一次
    assert client.calls.count((100, 1, 0)) == 1
    assert client.calls.count((100, 1, 42)) == 1
    assert client.calls.count((100, 1, 43)) == 1


def test_the_next_round_does_not_ask_for_the_page_count_again(store):
    """尾页页号记着：下一轮直接去那一页问，不用再取第 1 页。"""
    store.upsert_subscription(100, 42)
    client = FakeClient(
        {
            (1, 42): page([floor(10)], total_page=2),
            (2, 42): page([floor(10), floor(20)], total_page=2),
            (1, 0): page([floor(10)], total_page=2),
            (2, 0): page([floor(10), floor(20)], total_page=2),
        }
    )
    watcher = build(store, client, FakeNotifier())
    watcher.poll_all()
    client.calls.clear()

    watcher.poll_all()

    # 共享的那一页已经说明了一切：这一轮连按作者过滤的请求都不用发
    assert client.calls == [(100, 2, 0)]


# ------------------------------------------------------------- new posts


def test_new_post_is_detected_and_notified(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10), floor(20)])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_all()

    client.pages[(1, 42)] = page([floor(10), floor(20), floor(30, content="新回复")])
    report = watcher.poll_all()

    assert report.new_posts == 1
    assert len(notifier.sent) == 1
    title, body, url = notifier.sent[0]
    assert "目标用户" in title
    assert "某主题" in title
    assert "新回复" in body
    # 点击桌面通知打开的是本项目，不是 NGA 的楼层链接。
    assert url == "http://127.0.0.1:8765/"
    assert store.unread_count(sub.id) == 1


def test_the_same_post_is_not_reported_twice(store):
    store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_all()

    client.pages[(1, 42)] = page([floor(10), floor(20)])
    watcher.poll_all()
    watcher.poll_all()

    assert len(notifier.sent) == 1


def test_failed_delivery_stays_in_the_feed_for_browser_fallback(store):
    sub = store.upsert_subscription(100, 42)
    store.set_view_since(stamp(1_699_999_999))
    client = FakeClient({(1, 42): page([floor(10)])})
    watcher = build(store, client, FakeNotifier())
    watcher.notifier.notify = lambda *args, **kwargs: False

    first = watcher.poll_all()
    assert (first.new_posts, first.notifications) == (1, 0)
    assert [(item["pid"], item["delivered"]) for item in store.list_feed()] == [(10, 0)]
    assert store.unread_count(sub.id) == 1

    store.set_view_since(stamp(1_700_000_001))
    assert store.list_feed() == []
    store.set_view_since(stamp(1_699_999_999))
    assert [item["pid"] for item in store.list_feed()] == [10]
    assert watcher.rescan_subscription(store.get_subscription(sub.id)) == []


def test_unavailable_topic_is_fetched_once_per_round(store):
    store.upsert_subscription(100, 42)
    store.upsert_subscription(100, 43)
    client = FakeClient(error=NgaThreadUnavailableError(42, "不可读"))

    report = build(store, client, FakeNotifier()).poll_all()

    assert client.calls == [(100, 1, 0)]
    assert report.checked == 2
    assert len(report.errors) == 2


def test_several_new_posts_produce_one_merged_notification(store):
    store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_all()

    client.pages[(1, 42)] = page([floor(10), floor(20), floor(30)])
    report = watcher.poll_all()

    assert report.new_posts == 2
    assert len(notifier.sent) == 1
    title, body, _ = notifier.sent[0]
    assert "2 条" in title
    assert "2 条" in body


def test_a_merged_notification_leads_with_the_newest_reply(store):
    """一次来了好几条：通知正文拿**最新**那条的内容。"""
    store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10, content="最早的那条")])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_all()

    client.pages[(1, 42)] = page(
        [
            floor(10, content="最早的那条"),
            floor(20, content="中间那条"),
            floor(30, content="最新那条"),
        ]
    )
    watcher.poll_all()

    title, body, _ = notifier.sent[-1]
    assert "2 条" in title
    assert "最新那条" in body
    assert "最早那条" not in body


def test_floors_from_other_authors_are_ignored(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10, uid=42)])})
    watcher = build(store, client, FakeNotifier())
    watcher.poll_subscription(sub)

    client.pages[(1, 42)] = page([floor(10, uid=42), floor(20, uid=999)])
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert posts == []


def test_the_opening_post_without_pid_is_never_new(store):
    sub = store.upsert_subscription(100, 42)
    op = Floor(pid=0, lou=0, author_uid=42, author_name="楼主", timestamp=1, content="主楼")
    client = FakeClient({(1, 42): page([op])})
    watcher = build(store, client, FakeNotifier())
    watcher.poll_subscription(sub)

    client.pages[(1, 42)] = page([op, floor(20)])
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert [p.floor.pid for p in posts] == [20]


# ------------------------------------------------------------ gap handling


def test_gap_triggers_a_backwards_scan(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])})
    notifier = FakeNotifier()
    watcher = build(store, client, notifier)
    watcher.poll_subscription(sub)

    # The author's posts now span two pages and the tail page starts *after*
    # our high-water mark -> a gap -> we must walk back.
    client.pages = {
        (1, 42): page([floor(10)], total_page=2),
        (2, 42): page([floor(50), floor(60)], total_page=2),
        # 主题共享的那一页上没有这个作者的楼层 → 只能按作者过滤再取一次
        (2, 0): page([floor(70, uid=99), floor(80, uid=99)], total_page=2),
    }
    client.calls.clear()
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert [p.floor.pid for p in posts] == [50, 60]
    # 第 1 页问过总页数，回退扫描直接用它——同一页不取第二次
    assert client.calls.count((100, 1, 42)) == 1


def test_no_backwards_scan_when_there_is_no_gap(store):
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])})
    watcher = build(store, client, FakeNotifier())
    watcher.poll_subscription(sub)

    client.pages = {
        (1, 42): page([floor(10)], total_page=2),
        (2, 42): page([floor(10), floor(20)], total_page=2),
        # 共享的那一页上没有这个作者的楼层 → 按作者过滤取一次
        (2, 0): page([floor(70, uid=99)], total_page=2),
    }
    client.calls.clear()
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert [p.floor.pid for p in posts] == [20]
    # page 1 is only fetched once -- no backwards scan
    assert client.calls.count((100, 1, 42)) == 1


# ------------------------------------------------------------ error paths


def test_auth_error_marks_the_status_and_stops_the_round(store):
    store.upsert_subscription(100, 42)
    store.upsert_subscription(200, 43)
    client = FakeClient(error=NgaAuthError("未登录"))
    watcher = build(store, client, FakeNotifier())

    report = watcher.poll_all()

    assert report.checked == 1
    assert watcher.status.auth_error is not None
    assert report.errors


def test_thread_unavailable_is_reported_but_not_fatal(store):
    store.upsert_subscription(100, 42)
    store.upsert_subscription(200, 43)
    client = FakeClient(error=NgaThreadUnavailableError(42, "受限"))
    watcher = build(store, client, FakeNotifier())

    report = watcher.poll_all()

    assert report.checked == 2
    assert len(report.errors) == 2


def test_missing_credentials_are_reported(store):
    # 有订阅、想抓的时候才会去看凭据。
    store.upsert_subscription(100, 42)
    client = FakeClient()
    client.has_credentials = False
    watcher = build(store, client, FakeNotifier())

    report = watcher.poll_all()

    assert report.errors
    assert "Cookie" in report.errors[0]


# --------------------------------------------------------- 暂停与单飞


def test_paused_subscriptions_never_reach_nga(store):
    """所有订阅都暂停时，一次请求都不发。"""
    sub = store.upsert_subscription(100, 42)
    store.set_enabled(sub.id, False)
    client = FakeClient({(1, 42): page([floor(10)])})
    watcher = build(store, client, FakeNotifier())

    report = watcher.poll_all()

    assert client.calls == []
    assert report.checked == 0
    assert report.errors == []
    assert watcher.status.paused is True
    assert watcher.status.paused_reason == "所有订阅均已暂停"


def test_no_subscriptions_at_all_is_paused_too(store):
    client = FakeClient()
    watcher = build(store, client, FakeNotifier())

    watcher.poll_all()

    assert client.calls == []
    assert watcher.status.paused is True
    assert watcher.status.paused_reason == "还没有订阅"


def test_a_single_enabled_subscription_is_still_fetched(store):
    paused = store.upsert_subscription(100, 42)
    store.upsert_subscription(200, 43)
    store.set_enabled(paused.id, False)
    client = FakeClient({(1, 42): page([floor(10)]), (1, 43): page([floor(20)])})
    watcher = build(store, client, FakeNotifier())

    report = watcher.poll_all()

    assert report.checked == 1
    # 一轮两次请求：主题共享的那一页（不带作者过滤）+ 按作者过滤的那一页
    assert client.calls == [(200, 1, 0), (200, 1, 43)]
    assert watcher.status.paused is False


def test_a_manual_check_never_runs_next_to_a_round(store):
    """轮询和手动检查不会同时打 NGA——同一时刻只允许一轮抓取。"""
    sub = store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])}, delay=0.08)
    watcher = build(store, client, FakeNotifier())

    def round_() -> None:
        watcher.poll_all()

    def manual_() -> None:
        watcher.poll_subscription(store.get_subscription(sub.id))

    first = threading.Thread(target=round_)
    second = threading.Thread(target=manual_)
    first.start()
    time.sleep(0.01)
    second.start()
    first.join()
    second.join()

    assert client.max_concurrent == 1


async def test_concurrent_checks_share_a_single_round(store):
    """五个网页窗口同时要检查，也只打一轮 NGA。"""
    store.upsert_subscription(100, 42)
    client = FakeClient({(1, 42): page([floor(10)])})
    watcher = build(store, client, FakeNotifier())
    service = WatcherService(watcher, interval=60)

    reports = await asyncio.gather(*[service.poll_now() for _ in range(5)])

    # 一轮只发两次请求（不管来了几个窗口）
    assert client.calls == [(100, 1, 0), (100, 1, 42)]
    # 所有调用拿到的是同一轮的结果
    assert all(r is reports[0] for r in reports)


async def test_the_timer_keeps_running_while_everything_is_paused(store):
    """全部暂停时计时器继续走；重新启用后不必重置，下一轮按原计划发生。"""
    sub = store.upsert_subscription(100, 42)
    store.set_enabled(sub.id, False)
    client = FakeClient({(1, 42): page([floor(10)])})
    watcher = build(store, client, FakeNotifier())
    service = WatcherService(watcher, interval=1)
    service.start()
    try:
        await asyncio.sleep(1.2)
        # 暂停期间一次请求都不该发，但轮次确实在走
        assert client.calls == []
        assert watcher.status.paused is True
        assert watcher.status.last_run_at is not None

        # 重新启用：不用重置计时器，下一轮按原计划就会发生
        store.set_enabled(sub.id, True)
        deadline = time.monotonic() + 3.0
        while not client.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        assert client.calls, "重新启用后应该按原计划继续抓取"
    finally:
        await service.stop()


# ----------------------------------------------------------------- helpers


def sub_id_last_pid(store: Store, sub_id: int) -> int:
    return store.get_subscription(sub_id).last_pid
