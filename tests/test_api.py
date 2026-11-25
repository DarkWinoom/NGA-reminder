"""Tests for the HTTP API."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from nga_reminder.api import create_app
from nga_reminder.config import Settings
from nga_reminder.models import Floor


class QuietNotifier:
    """测试里不要真的弹窗。"""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        self.sent.append((title, body, url))
        return True


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_status_reports_missing_credentials(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    assert body["credentials_configured"] is False


def test_index_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "NGA 提醒助手" in res.text


def test_static_files_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def _download_name(response) -> str:
    prefix = "attachment; filename*=UTF-8''"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(prefix)
    return unquote(disposition.removeprefix(prefix))


def test_json_export_includes_saved_replies_outside_the_feed_window(client):
    store = client.app.state.nga.store
    sub = store.upsert_subscription(101, 22, alias="订阅用户", title="主题")
    store.record_post(
        sub.id,
        Floor(123, 2, 22, "", 1_700_000_000, "[b]保存的回复[/b]"),
        floor_no=42,
        floor_page=3,
    )

    started = datetime.now(UTC).replace(microsecond=0)
    res = client.post("/api/subscriptions/export", json={
        "subscription_ids": [sub.id, sub.id], "format": "json", "timezone_offset_minutes": 480,
    })

    assert res.status_code == 200
    filename = _download_name(res)
    assert filename.startswith("NGA提醒助手_订阅用户_") and filename.endswith(".json")
    stamp = filename.removeprefix("NGA提醒助手_订阅用户_").removesuffix(".json")
    assert len(stamp) == 14 and stamp.isdecimal()
    exported_at = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone(timedelta(hours=8))
    )
    assert started <= exported_at <= datetime.now(UTC)
    data = res.json()
    assert data["subscription"]["alias"] == "订阅用户"
    assert data["replies"] == [{
        "pid": 123,
        "posted_at": "2023-11-14T22:13:20+00:00",
        "floor_no": 42,
        "page": 3,
        "content": "保存的回复",
        "quote_text": "",
        "url": "https://bbs.nga.cn/read.php?tid=101&page=3#pid123Anchor",
    }]


def test_xlsx_export_has_three_columns_and_one_row_per_reply(client):
    store = client.app.state.nga.store
    sub = store.upsert_subscription(101, 22, alias="用户A")
    store.record_post(sub.id, Floor(52, 2, 22, "", 1_700_000_000, "{=1+1}"))
    store.record_post(sub.id, Floor(51, 1, 22, "", 0, "长" * 33_000))

    res = client.post("/api/subscriptions/export", json={
        "subscription_ids": [sub.id], "format": "xlsx", "timezone_offset_minutes": 480,
    })

    assert res.status_code == 200
    assert res.headers["content-type"].endswith("spreadsheetml.sheet")
    assert _download_name(res).startswith("NGA提醒助手_用户A_")
    with ZipFile(BytesIO(res.content)) as workbook:
        assert "xl/worksheets/sheet2.xml" not in workbook.namelist()
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    namespaces = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = sheet.findall(".//x:row", namespaces)
    assert [row.attrib["r"] for row in rows] == ["1", "2", "3"]
    header = rows[0].findall("x:c/x:is/x:t", namespaces)
    assert [cell.text for cell in header] == ["订阅名称", "回复时间", "回复内容"]
    assert rows[1].findtext("x:c[@r='A2']/x:is/x:t", namespaces=namespaces) == "用户A"
    assert rows[1].findtext("x:c[@r='C2']/x:is/x:t", namespaces=namespaces) == "{=1+1}"
    assert rows[2].findtext("x:c[@r='C3']/x:is/x:t", namespaces=namespaces) == "长" * 32_766 + "…"
    time_value = rows[1].findtext("x:c[@r='B2']/x:v", namespaces=namespaces)
    excel_days = (datetime(2023, 11, 15, 6, 13, 20) - datetime(1899, 12, 30)).total_seconds()
    assert float(time_value) == pytest.approx(excel_days / 86_400, abs=1e-10, rel=0)
    assert sheet.find(".//x:f", namespaces) is None


@pytest.mark.parametrize("file_format", ["json", "xlsx"])
def test_multiple_subscriptions_export_one_file_each(client, file_format):
    store = client.app.state.nga.store
    first = store.upsert_subscription(101, 22, alias="用户/A")
    second = store.upsert_subscription(101, 33, alias="用户/A")
    store.set_enabled(second.id, False)

    res = client.post("/api/subscriptions/export", json={
        "subscription_ids": [second.id, first.id], "format": file_format,
    })

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    filename = _download_name(res)
    assert filename.startswith("NGA提醒助手_") and filename.endswith(".zip")
    stamp = filename.removeprefix("NGA提醒助手_").removesuffix(".zip")
    assert len(stamp) == 14 and stamp.isdecimal()
    datetime.strptime(stamp, "%Y%m%d%H%M%S")
    first_entry = f"NGA提醒助手_用户_A_{stamp}.{file_format}"
    with ZipFile(BytesIO(res.content)) as archive:
        assert archive.namelist() == [
            first_entry, f"NGA提醒助手_用户_A（2）_{stamp}.{file_format}",
        ]
        assert archive.testzip() is None
        if file_format == "xlsx":
            with ZipFile(BytesIO(archive.read(first_entry))) as workbook:
                assert "xl/worksheets/sheet1.xml" in workbook.namelist()


def test_export_rejects_missing_subscriptions_and_invalid_requests(client):
    store = client.app.state.nga.store
    sub = store.upsert_subscription(101, 22)
    for ids, file_format, status in (
        ([sub.id, sub.id + 1], "json", 404),
        ([], "json", 422),
        ([sub.id], "csv", 422),
    ):
        res = client.post("/api/subscriptions/export", json={
            "subscription_ids": ids, "format": file_format,
        })
        assert res.status_code == status


def test_add_subscription_requires_a_target_user(client):
    res = client.post("/api/subscriptions", json={"url": "https://bbs.nga.cn/read.php?tid=123"})
    assert res.status_code == 400
    assert "UID" in res.json()["detail"]


def test_add_subscription_rejects_a_bad_url(client):
    res = client.post("/api/subscriptions", json={"url": "not a url", "author_uid": 1})
    assert res.status_code == 400


def test_subscription_lifecycle(client, monkeypatch):
    _fake_thread(monkeypatch, [])

    res = client.post(
        "/api/subscriptions",
        json={"url": "https://bbs.nga.cn/read.php?tid=46046493&authorid=61765516"},
    )
    assert res.status_code == 200
    body = res.json()
    sub_id = body["id"]
    assert body["tid"] == 46046493
    assert body["author_uid"] == 61765516
    assert body["title"] == "某主题"

    listing = client.get("/api/subscriptions").json()
    assert len(listing) == 1
    assert listing[0]["unread"] == 0

    patched = client.patch(f"/api/subscriptions/{sub_id}", json={"enabled": False})
    assert patched.json()["enabled"] is False

    posts = client.get(f"/api/subscriptions/{sub_id}/posts").json()
    assert posts == []

    assert client.delete(f"/api/subscriptions/{sub_id}").status_code == 200
    assert client.get("/api/subscriptions").json() == []


def test_missing_subscription_returns_404(client):
    assert client.delete("/api/subscriptions/999").status_code == 404
    assert client.get("/api/subscriptions/999/posts").status_code == 404
    assert client.patch("/api/subscriptions/999", json={"enabled": True}).status_code == 404


def test_cookie_can_be_saved_and_cleared(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(
        NgaClient, "get_user_profile", lambda self, uid: {"uid": uid, "username": "someone"}
    )

    verified = client.post(
        "/api/setup/cookie/verify", json={"uid": "424242", "cid": "abc"}
    )
    assert verified.status_code == 200
    assert verified.json()["username"] == "someone"

    saved = client.post("/api/setup/cookie", json={"uid": "424242", "cid": "abc"})
    assert saved.status_code == 200

    status = client.get("/api/status").json()
    assert status["credentials_configured"] is True
    assert status["credentials_source"] == "store"

    assert client.delete("/api/setup/cookie").status_code == 200
    assert client.get("/api/status").json()["credentials_configured"] is False


def test_cookie_accepts_a_pasted_cookie_string(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_user_profile", lambda self, uid: {"uid": uid})
    res = client.post(
        "/api/setup/cookie/verify",
        json={"cookie": "ngaPassportUid=424242; ngaPassportCid=deadbeef"},
    )
    assert res.status_code == 200
    assert res.json()["uid"] == "424242"


def test_cookie_verification_rejects_incomplete_input(client):
    res = client.post("/api/setup/cookie/verify", json={"uid": "424242"})
    assert res.status_code == 400


def test_check_endpoints_exist(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))

    created = client.post(
        "/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516}
    )
    assert created.status_code == 200
    sub_id = created.json()["id"]

    # No credentials are configured, so checking a single subscription reports
    # the auth problem (401) instead of pretending it succeeded.
    one = client.post(f"/api/subscriptions/{sub_id}/check")
    assert one.status_code == 401

    # The "check everything" endpoint answers with a report either way.
    all_of_them = client.post("/api/check")
    assert all_of_them.status_code == 200
    assert "checked" in all_of_them.json()
    assert all_of_them.json()["errors"]


def test_check_on_a_missing_subscription_returns_404(client):
    assert client.post("/api/subscriptions/999/check").status_code == 404


def test_cookie_status_endpoint_reports_without_leaking(client):
    res = client.get("/api/setup/cookie")
    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is False
    assert "cid" not in body


def test_cookie_status_after_saving(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_user_profile", lambda self, uid: {"uid": uid})
    client.post("/api/setup/cookie", json={"uid": "424242", "cid": "secret-value"})

    body = client.get("/api/setup/cookie").json()
    assert body["configured"] is True
    assert body["source"] == "store"
    assert "secret-value" not in str(body)


def test_notify_test_endpoint_reports_the_backend(client):
    res = client.post("/api/notify/test")
    assert res.status_code == 200
    body = res.json()
    assert "backend" in body
    assert isinstance(body["delivered"], bool)


def test_subscription_accepts_alias_and_color(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))

    created = client.post(
        "/api/subscriptions",
        json={
            "url": "https://bbs.nga.cn/read.php?tid=46046493&authorid=61765516",
            "alias": "我的关注",
            "color": "#A8442F",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["alias"] == "我的关注"
    assert body["color"] == "#A8442F"
    assert body["display_name"] == "我的关注"
    assert body["author_uid"] == 61765516
    assert body["tid"] == 46046493


def test_subscription_can_be_renamed_and_recoloured(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))
    created = client.post(
        "/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516}
    ).json()

    patched = client.patch(
        f"/api/subscriptions/{created['id']}",
        json={"alias": "新的名字", "color": "#3B6EA5"},
    ).json()

    assert patched["alias"] == "新的名字"
    assert patched["color"] == "#3B6EA5"


def test_a_subscription_alias_and_colour_can_be_cleared(client, monkeypatch):
    """空字符串是「清掉」，不是「没填」——这两条走的分支不一样。

    ``title`` 和 ``author_name`` 空了不写（空名字没意义），但 ``alias``
    和 ``color`` 空了就是要清掉：用户把自定义名字删掉，得真的删掉。
    """
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))
    created = client.post(
        "/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516}
    ).json()
    client.patch(
        f"/api/subscriptions/{created['id']}",
        json={"alias": "临时名字", "color": "#3B6EA5"},
    )

    cleared = client.patch(
        f"/api/subscriptions/{created['id']}", json={"alias": "", "color": ""}
    ).json()
    assert cleared["alias"] == ""
    assert cleared["color"] == ""


def test_subscription_accepts_a_bare_query_string(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))
    res = client.post(
        "/api/subscriptions", json={"url": "tid=46046493&authorid=61765516"}
    )
    assert res.status_code == 200
    assert res.json()["tid"] == 46046493
    assert res.json()["author_uid"] == 61765516


def test_post_listing_returns_plain_text_not_bbcode(client, monkeypatch):
    """楼层列表要给界面可读正文，不能把原始 BBCode 丢给前端。"""
    from nga_reminder.db import Store
    from nga_reminder.models import Floor
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "get_thread_title", lambda self, tid: ("某主题", "分区"))
    created = client.post(
        "/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516}
    ).json()

    raw = (
        "[quote][pid=888075883,47288722,5694]Reply[/pid] "
        "<b>Post by [uid=1305675]苍地哥[/uid] (2026-11-16 15:09)</b><br/>"
        "近期唯一解是老黄复出吗[/quote]我觉得是我们谁去应聘一下英伟达CEO吧"
    )
    store = Store(Settings().db_path)
    store.record_post(
        created["id"],
        Floor(
            pid=888076845,
            lou=1,
            author_uid=61765516,
            author_name="某人",
            timestamp=int(time.time()),
            content=raw,
        ),
        quote_text="苍地哥：近期唯一解是老黄复出吗",
    )

    posts = client.get(f"/api/subscriptions/{created['id']}/posts").json()
    assert len(posts) == 1
    body = posts[0]["content"]
    assert "[quote]" not in body
    assert "[/quote]" not in body
    assert "<b>" not in body
    assert "苍地哥" not in body  # 引用块被摘掉，单独放在 quote_text
    assert "我觉得是我们谁去应聘一下英伟达CEO吧" in body
    assert posts[0]["quote_text"] == "苍地哥：近期唯一解是老黄复出吗"


def test_frontend_is_never_served_stale(client):
    """本机自用工具改了前端必须立刻生效，所以禁止缓存。"""
    for path in ("/", "/static/app.js", "/static/style.css"):
        res = client.get(path)
        assert res.status_code == 200
        assert "no-cache" in res.headers.get("cache-control", "")


# ------------------------------------------------- 新加订阅：立刻取数


def _fake_thread(monkeypatch, floors):
    """把 NGA 的取数换成一组固定的楼层。"""
    from nga_reminder.models import ThreadPage
    from nga_reminder.nga.client import NgaClient

    monkeypatch.setattr(NgaClient, "has_credentials", property(lambda self: True))

    def fake_page(self, tid, page=1, author_id=0):
        return ThreadPage(
            tid=tid,
            page=page,
            total_page=1,
            per_page=20,
            floor_count=len(floors),
            title="某主题",
            forum_name="分区",
            author_uid=1,
            author_name="楼主",
            floors=floors,
        )

    monkeypatch.setattr(NgaClient, "get_thread_page", fake_page)


def test_adding_a_subscription_pulls_the_data_right_away(client, monkeypatch):
    """新加订阅后立刻按订阅时间取一次数，取到的回复直接进通知流。"""
    from nga_reminder.models import Floor

    _fake_thread(
        monkeypatch,
        [
            Floor(
                pid=777,
                lou=0,
                author_uid=61765516,
                author_name="某人",
                timestamp=1_700_000_000,
                content="窗口里的回复",
            )
        ],
    )
    client.app.state.nga.watcher.notifier = QuietNotifier()
    # 订阅时间放到很久以前：这条回复落在窗口里
    client.post("/api/settings/view-since", json={"view_since": "2000-01-01T00:00:00+00:00"})

    res = client.post("/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516})

    assert res.status_code == 200
    body = res.json()
    assert body["fetch"]["new_posts"] == 1
    assert body["fetch"]["skipped"] is False
    assert body["title"] == "某主题"

    # 这条回复进了通知流
    feed = client.get("/api/notifications").json()
    assert [item["pid"] for item in feed] == [777]


def test_adding_a_subscription_without_credentials_skips_the_fetch(client, monkeypatch):
    """没 Cookie 就不抓：订阅照样加得进去，界面会说清楚为什么没取数。"""
    from nga_reminder.nga.client import NgaClient

    def unexpected_title(self, tid):
        pytest.fail("没有凭据时不该单独请求标题")

    monkeypatch.setattr(NgaClient, "get_thread_title", unexpected_title)
    monkeypatch.setattr(
        NgaClient,
        "get_thread_page",
        lambda self, tid, page=1, author_id=0: pytest.fail("没有凭据时不该发请求"),
    )

    res = client.post("/api/subscriptions", json={"tid": 46046493, "author_uid": 61765516})

    assert res.status_code == 200
    body = res.json()
    assert body["fetch"]["skipped"] is True
    assert body["fetch"]["new_posts"] == 0
    assert "Cookie" in body["fetch"]["reason"]


def test_duplicate_subscription_updates_metadata_without_refetching(client, monkeypatch):
    from nga_reminder.nga.client import NgaClient

    _fake_thread(monkeypatch, [])
    payload = {"tid": 101, "author_uid": 42}
    first = client.post("/api/subscriptions", json=payload).json()
    assert first["initialized"] is True

    def unexpected_page(*args, **kwargs):
        pytest.fail("更新已初始化的订阅时不该再次取数")

    monkeypatch.setattr(NgaClient, "get_thread_page", unexpected_page)
    again = client.post("/api/subscriptions", json={**payload, "alias": "新别名"}).json()
    assert again["id"] == first["id"]
    assert again["alias"] == "新别名"
    assert again["fetch"]["skipped"] is True

    client.patch(f"/api/subscriptions/{first['id']}", json={"enabled": False})
    checked = client.post(f"/api/subscriptions/{first['id']}/check").json()
    assert checked["skipped"] is True
