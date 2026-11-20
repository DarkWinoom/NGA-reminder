"""楼层定位器：把回复的 pid 换算成 NGA 页面上的真实楼号与页码。

为什么需要它
------------
NGA 带 ``authorid`` 过滤时会把 ``lou`` 重新编号——那是「该用户的第几条回复」，
不是主题里的楼层号。真实的楼号只在**不带过滤**的响应里，而那一路是分页的，
没有 pid→页码的接口（实测 ``?pid=`` 返回 ``lou: 0``）。

所以定位的办法是二分：pid 随楼层号单调递增，探测某一页的 pid 区间，
看目标落在哪一半。这里用一套编出来的线程验证它——页数、pid、楼号都是假的，
但三者之间的关系跟真实 API 一致（实测 2026-11-18：第 76 页是 #1500~#1519）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from nga_reminder.api import create_app
from nga_reminder.config import Settings
from nga_reminder.db import Store
from nga_reminder.models import Floor, ThreadPage
from nga_reminder.nga.floors import MAX_PROBES, FloorLocation, FloorLocator
from nga_reminder.watcher import Watcher


class FakeNotifier:
    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        return True


class FakeThread:
    """一页 20 楼、pid 随楼号递增的假主题。"""

    def __init__(self, pages: int = 64, per_page: int = 20):
        self.pages = pages
        self.per_page = per_page
        self.calls: list[int] = []

    def page(self, page: int) -> ThreadPage:
        self.calls.append(page)
        floors: list[Floor] = []
        for index in range(self.per_page):
            lou = (page - 1) * self.per_page + index
            if lou >= self.pages * self.per_page:
                break
            floors.append(
                Floor(
                    pid=1000 + lou * 7,
                    lou=lou,
                    author_uid=150058,
                    author_name="某人",
                    timestamp=1_700_000_000 + lou,
                    content="内容",
                )
            )
        return ThreadPage(
            tid=47288722,
            page=page,
            total_page=self.pages,
            per_page=self.per_page,
            floor_count=self.pages * self.per_page,
            title="某主题",
            forum_name="分区",
            author_uid=150058,
            author_name="楼主",
            floors=floors,
        )


class FakeClient:
    def __init__(self, thread: FakeThread):
        self.thread = thread

    def get_thread_page(self, tid: int, page: int = 1, author_id: int = 0) -> ThreadPage:
        return self.thread.page(page)


def _locator(pages: int = 64) -> tuple[FloorLocator, FakeThread]:
    thread = FakeThread(pages=pages)
    return FloorLocator(FakeClient(thread)), thread


# ------------------------------------------------------------- 二分定位


def test_locator_finds_the_real_floor_number_and_page():
    """目标在中间某一页时，楼号与页码都要算准。"""
    locator, _ = _locator()

    # 第 10 页盖住 lou 180~199，也就是 pid 2260~2393。
    hit = locator.locate(47288722, 1000 + 185 * 7)

    assert hit is not None
    assert hit.floor_no == 185
    assert hit.page == 10


def test_locator_finds_a_floor_on_the_very_first_page():
    locator, _ = _locator()

    hit = locator.locate(47288722, 1000 + 3 * 7)

    assert hit is not None
    assert (hit.floor_no, hit.page) == (3, 1)


def test_locator_probes_far_fewer_pages_than_the_thread_has():
    """几千页的主题也不能一頁一頁地爬——二分才是关键。"""
    locator, thread = _locator(pages=2048)

    hit = locator.locate(47288722, 1000 + 1900 * 7)

    assert hit is not None
    assert hit.floor_no == 1900
    assert hit.page == 96
    # 二分 + 探尾页，十几二十次足够；一页页爬要两千次。
    assert len(thread.calls) < 25


def test_locator_shares_probes_between_neighbouring_replies():
    """两条回复挨得近时，第二次不该重新爬一遍。"""
    locator, thread = _locator()

    first = locator.locate(47288722, 1000 + 185 * 7)
    calls_after_first = len(thread.calls)
    second = locator.locate(47288722, 1000 + 186 * 7)

    assert first is not None and second is not None
    assert (first.floor_no, second.floor_no) == (185, 186)
    # 第二条就住在同一页里，探测次数应该不增加。
    assert len(thread.calls) - calls_after_first <= 1


def test_locator_resolves_several_replies_at_once():
    locator, _ = _locator()

    hits = locator.locate_many(
        47288722,
        [1000 + 5 * 7, 1000 + 6 * 7, 1000 + 700 * 7],
    )

    assert {pid: loc.floor_no for pid, loc in hits.items()} == {
        1000 + 5 * 7: 5,
        1000 + 6 * 7: 6,
        1000 + 700 * 7: 700,
    }


def test_locator_ignores_the_opening_post_and_junk_pids():
    locator, _ = _locator()

    assert locator.locate_many(47288722, [0, -1]) == {}


# ------------------------------------------------------------- 兜底


def test_locator_gives_up_when_the_pid_is_nowhere():
    """主题里没有这条 pid 时不要瞎猜——返回空，界面上就不显示楼号。"""
    locator, _ = _locator()

    assert locator.locate(47288722, 999_999_999) is None


def test_locator_follows_a_thread_that_outgrew_its_page_count():
    """主题比我们记着的总页数多出几页时，最新那条回复也要能找到。

    发帖很勤的主题页面一直在长，定位器缓存里的总页数（几分钟前的）经常已经
    过期，而目标就在「新长出来的那几页」里。实测踩过：缓存记着 5850 页、
    实际已经 5859 页时，最新那条回复（在第 5857 页）被判成「找不到」，
    于是界面上退回不带楼号的链接——看着像把功能改回去了。
    """
    locator, thread = _locator(pages=64)
    target_lou = 63 * 20 + 5  # 第 64 页
    target_pid = 1000 + target_lou * 7

    # 缓存里记着一个过期的总页数——线程其实已经长到 64 页了。
    locator._total_pages[47288722] = 40

    hit = locator.locate(47288722, target_pid)

    assert hit is not None, "主题长出来的那些页也要够得着"
    assert (hit.floor_no, hit.page) == (target_lou, 64)
    # 重新探了一次尾页，多花一次请求而已。
    assert len(thread.calls) <= MAX_PROBES


def test_locator_stops_after_the_probe_budget():
    """探测次数有上限：坏主题不能把整轮轮询拖死。"""
    # 两亿多页的主题：二分要三十几次才能缩到头，正好撞上探测上限。
    locator, thread = _locator(pages=2**28)
    target_pid = 1000 + (2**28 * 20) * 7

    hit = locator.locate(47288722, target_pid)

    assert hit is None
    assert len(thread.calls) <= MAX_PROBES + 2  # +1 探末页，+1 探第一页
    # 「没找完」不等于「确实没有」——它要能被重试。
    assert target_pid in locator.inconclusive


def test_locator_does_not_mark_a_gone_pid_as_inconclusive():
    """整段范围都翻过了还是没这一条，那就是确实没有——不用再试。"""
    locator, _ = _locator()
    missing_pid = 999_999_999

    assert locator.locate(47288722, missing_pid) is None
    assert missing_pid not in locator.inconclusive


# ------------------------------------------------------------- 串起来


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "floors-api.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _post(pid: int, ts: int | None = None) -> Floor:
    """一条「刚刚发的」楼层——窗口是按发帖时间算的。"""
    return Floor(
        pid=pid,
        lou=0,
        author_uid=150058,
        author_name="目标用户",
        timestamp=int(time.time()) if ts is None else ts,
        content="内容",
    )


def test_feed_carries_the_floor_number_and_a_page_aware_link(client):
    """列表里要带楼号，「在 NGA 打开」要落到那一页并锚到那条回复。

    锚点必须是 ``#pid…Anchor``——NGA 页面上的真实 id 就是这个形状
    （``<a id='pid888185460Anchor'></a>``），少写 Anchor 锚不到东西。
    """
    store = client.app.state.nga.store
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    store.record_post(sub.id, _post(877273434), notified=True, floor_no=1481, floor_page=76)
    store.collect_posts(sub.id, [877273434])

    body = client.get("/api/notifications").json()

    assert len(body) == 1
    assert body[0]["floor_no"] == 1481
    assert body[0]["url"] == (
        "https://bbs.nga.cn/read.php?tid=47288722&page=76#pid877273434Anchor"
    )


def test_feed_falls_back_to_the_pid_link_when_the_page_is_unknown(client):
    """定位不到页码时退回到 NGA 的单条回复视图，别给一个错的页码。"""
    store = client.app.state.nga.store
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    store.record_post(sub.id, _post(777), notified=True)
    store.collect_posts(sub.id, [777])

    body = client.get("/api/notifications").json()

    assert body[0]["floor_no"] == 0
    assert body[0]["url"] == "https://bbs.nga.cn/read.php?tid=47288722&pid=777"


class FakeLocator:
    """假装定位过的定位器：有的找到了，有的确实没有，有的只是没找完。"""

    def __init__(self, found=None, gone=(), inconclusive=()):
        self.found = found or {}
        self.gone = set(gone)
        self.inconclusive = set(inconclusive)

    def locate_many(self, tid, pids):
        self.inconclusive = {pid for pid in pids if pid in self.inconclusive}
        return {
            pid: FloorLocation(self.found[pid], 1, pid)
            for pid in pids
            if pid in self.found
        }


def test_backfill_only_gives_up_on_a_reply_it_is_sure_about(client):
    """「确实找不到」才记 -1；「这次没找完」留着 0，下次打开列表再试。

    这条区分很重要：以前一律记 -1，定位器一失手（主题比我们记的页数多出
    几页就会失手）那条回复就永远拿不到楼号了，界面上的「在 NGA 打开」也就
    永远退回不带页码与锚点的样子。
    """
    store = client.app.state.nga.store
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    for pid in (111, 222):
        store.record_post(sub.id, _post(pid), notified=True)
        store.collect_posts(sub.id, [pid])

    client.app.state.nga.client.set_credentials("10001", "sandbox")
    client.app.state.nga.watcher.locator = FakeLocator(
        gone={111}, inconclusive={222}
    )

    client.get("/api/notifications")

    rows = {row["pid"]: row for row in store.list_posts(sub.id, limit=10)}
    assert rows[111]["floor_no"] == -1  # 确实没有——不再重试
    assert rows[222]["floor_no"] == 0  # 没找完——下次再试


def test_feed_pages_back_and_forth_with_cursors(client):
    """往下滑要能接上更早的一段，往上刷要能只拿最新的那几条。"""
    store = client.app.state.nga.store
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    for pid in (100, 200, 300):
        store.record_post(sub.id, _post(pid), notified=True)
        store.collect_posts(sub.id, [pid])

    head = client.get("/api/notifications?limit=2").json()
    assert [item["pid"] for item in head] == [300, 200]

    # 拿这一页里最旧的那条当游标，往前翻剩下的那一条。
    cursor = head[-1]["cursor"]
    older = client.get("/api/notifications", params={"limit": 2, "before": cursor}).json()
    assert [item["pid"] for item in older] == [100]

    # 拿最新那条当游标，往后刷：没有更新的了。
    newest = head[0]["cursor"]
    assert client.get("/api/notifications", params={"after": newest}).json() == []

    # 拿中间那条当游标：比它新的只有 300 那一条。
    assert [
        item["pid"]
        for item in client.get("/api/notifications", params={"after": cursor}).json()
    ] == [300]


def test_feed_cursor_survives_an_unencoded_plus_sign(client):
    """游标里的 ``+00:00`` 被解码成空格时也要认得出来。

    浏览器会自动编码，但接口不该因为调用方少写了一个百分号就不认人。
    """
    store = client.app.state.nga.store
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    for pid in (100, 200):
        store.record_post(sub.id, _post(pid), notified=True)
        store.collect_posts(sub.id, [pid])

    head = client.get("/api/notifications?limit=1").json()
    raw = head[0]["cursor"].replace("+", " ")

    older = client.get(f"/api/notifications?before={raw}").json()
    assert [item["pid"] for item in older] == [100]


def test_feed_can_be_narrowed_to_one_subscription(client):
    store = client.app.state.nga.store
    first = store.upsert_subscription(47288722, 150058, alias="狼大")
    second = store.upsert_subscription(47288723, 150058, alias="另一个")
    store.record_post(first.id, _post(100), notified=True)
    store.collect_posts(first.id, [100])
    store.record_post(second.id, _post(200), notified=True)
    store.collect_posts(second.id, [200])

    only_second = client.get(f"/api/notifications?subscription_id={second.id}").json()

    assert [item["pid"] for item in only_second] == [200]


def test_watcher_stores_the_resolved_floor_number(tmp_path):
    """抓到新回复时要把算出来的楼号一起落库。"""

    class Client:
        def __init__(self):
            self.thread = FakeThread(pages=8)

        def get_thread_page(self, tid: int, page: int = 1, author_id: int = 0) -> ThreadPage:
            if author_id:
                # 带过滤的响应：lou 被重新编号成 0 起算——这正是问题所在。
                floors = [
                    Floor(
                        pid=1000 + 60 * 7,
                        lou=0,
                        author_uid=150058,
                        author_name="某人",
                        timestamp=1_700_000_000,
                        content="新回复",
                    )
                ]
                return ThreadPage(
                    tid=tid,
                    page=1,
                    total_page=1,
                    per_page=20,
                    floor_count=1,
                    title="某主题",
                    forum_name="分区",
                    author_uid=150058,
                    author_name="楼主",
                    floors=floors,
                )
            return self.thread.page(page)

    store = Store(tmp_path / "watcher-floors.db")
    sub = store.upsert_subscription(47288722, 150058, alias="狼大")
    store.update_subscription_meta(sub.id, initialized=True, last_pid=0)

    client = Client()
    watcher = Watcher(store, client, FakeNotifier(), locator=FloorLocator(client))
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert len(posts) == 1
    recorded = store.list_posts(sub.id)
    assert recorded[0]["floor_no"] == 60
    assert recorded[0]["floor_page"] == 4
    store.close()
