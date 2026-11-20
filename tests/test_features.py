"""Tests for the reply features: GBK decoding, quote extraction, aliases and colours."""

from __future__ import annotations

import httpx
import pytest

from nga_reminder.db import Store
from nga_reminder.models import Floor
from nga_reminder.nga.bbcode import extract_quotes
from nga_reminder.nga.client import NgaClient
from nga_reminder.nga.errors import NgaRequestError
from nga_reminder.watcher import Watcher


def make_client(handler, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://bbs.nga.cn")
    return NgaClient(http_client=http, uid="u", cid="c", **kwargs)


# --------------------------------------------------------------- GBK


def test_gbk_encoded_json_is_decoded_not_rejected():
    """``nuke.php`` answers in GBK while labelling the body as JSON.

    Decoding it as UTF-8 raises UnicodeDecodeError, which used to surface as a
    bogus "响应不是 JSON" error and broke the cookie verification.
    """
    body = '{"data":{"0":{"uid":424242,"username":"someone","group":"学徒"}}}'.encode("gbk")
    handler = lambda request: httpx.Response(  # noqa: E731
        200,
        content=body,
        headers={"Content-Type": "text/javascript; charset=GBK"},
    )
    client = make_client(handler)

    profile = client.get_user_profile(424242)

    assert profile["username"] == "someone"
    assert profile["group"] == "学徒"


def test_utf8_json_still_works():
    handler = lambda request: httpx.Response(  # noqa: E731
        200,
        json={"data": {"0": {"uid": 1, "username": "u"}}},
        headers={"Content-Type": "text/json; charset=UTF-8"},
    )
    client = make_client(handler)

    assert client.get_user_profile(1)["username"] == "u"


def test_xssi_prefix_is_stripped():
    handler = lambda request: httpx.Response(  # noqa: E731
        200,
        content=b")]}'\n" + b'{"data":{"0":{"uid":1}}}',
        headers={"Content-Type": "text/json; charset=UTF-8"},
    )
    client = make_client(handler)

    assert client.get_user_profile(1)["uid"] == 1


def test_undecodable_body_gives_a_helpful_message():
    handler = lambda request: httpx.Response(  # noqa: E731
        200, content=b"<html>login</html>", headers={"Content-Type": "text/html"}
    )
    client = make_client(handler)

    with pytest.raises(NgaRequestError) as exc:
        client.get_user_profile(1)

    message = str(exc.value)
    assert "无法解析为 JSON" in message
    assert "login" in message, "错误信息里应带上有用的响应片段"


# ------------------------------------------------------- link parsing


@pytest.mark.parametrize(
    "value",
    [
        "tid=46046493&authorid=61765516",
        "?tid=46046493&authorid=61765516",
        "https://bbs.nga.cn/read.php?fid=-7&tid=46046493&authorid=61765516",
        "https://bbs.nga.cn/read.php?tid=46046493&authorid=61765516&rand=1",
    ],
)
def test_only_author_links_are_parsed(value):
    from nga_reminder.nga.client import parse_thread_ref

    assert parse_thread_ref(value) == (46046493, 61765516)


def test_link_without_authorid():
    from nga_reminder.nga.client import parse_thread_ref

    assert parse_thread_ref("https://bbs.nga.cn/read.php?tid=46046493") == (46046493, None)


# ------------------------------------------------------ quote parsing


def test_extract_quotes_reads_the_pid_and_the_text():
    content = (
        "[quote][pid=887927595,47920406,1]Reply[/pid] "
        "<b>Post by [uid=66485837]寒武纪[/uid] (2026-11-15 08:01):</b><br/>"
        "这铁壳子回收价值在哪？[/quote]没绷住"
    )

    quotes = extract_quotes(content)

    assert len(quotes) == 1
    assert quotes[0].pid == 887927595
    assert "回收价值" in quotes[0].text
    assert quotes[0].author_name == "寒武纪"


def test_extract_quotes_handles_several_blocks():
    content = "[quote]第一个[/quote]中间[quote][pid=12,3,4]Reply[/pid]第二个[/quote]"

    quotes = extract_quotes(content)

    assert len(quotes) == 2
    assert quotes[0].pid is None
    assert quotes[1].pid == 12


def test_no_quotes_returns_empty_list():
    assert extract_quotes("普通回复") == []
    assert extract_quotes("") == []


# ------------------------------------------------- alias and colour


def test_alias_and_color_are_stored(tmp_path):
    store = Store(tmp_path / "s.db")
    sub = store.upsert_subscription(1, 2, alias="我的关注", color="#A8442F")

    assert sub.alias == "我的关注"
    assert sub.color == "#A8442F"
    assert sub.display_name == "我的关注"

    store.update_subscription_meta(sub.id, alias="改过的名字")
    assert store.get_subscription(sub.id).display_name == "改过的名字"
    store.close()


def test_display_name_falls_back_to_the_real_nickname(tmp_path):
    store = Store(tmp_path / "s.db")
    sub = store.upsert_subscription(1, 2, author_name="真名")

    assert sub.display_name == "真名"
    store.close()


# ----------------------------------------------- quote in the notification


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, title, body, *, url=""):
        self.sent.append((title, body, url))
        return True


class QuotingClient:
    """Serves a thread whose reply quotes another floor."""

    def __init__(self):
        self.has_credentials = True
        self.quoted = Floor(
            pid=999,
            lou=1,
            author_uid=77,
            author_name="被引用的人",
            timestamp=1_700_000_000,
            content="这是被引用的原文内容",
        )

    def get_thread_page(self, tid, page=1, author_id=0):
        from nga_reminder.models import ThreadPage

        reply = Floor(
            pid=100,
            lou=2,
            author_uid=42,
            author_name="回复的人",
            timestamp=1_700_000_100,
            content=(
                "[quote][pid=999,47920406,1]Reply[/pid] "
                "<b>Post by [uid=77]被引用的人[/uid]:</b><br/>"
                "这是被引用的原文内容[/quote]我的回复"
            ),
        )
        return ThreadPage(
            tid=tid,
            page=1,
            total_page=1,
            per_page=20,
            floor_count=2,
            title="某主题",
            forum_name="分区",
            author_uid=1,
            author_name="楼主",
            floors=[reply],
        )

    def get_floor_by_pid(self, tid, pid):
        return self.quoted if pid == 999 else None


def test_notification_carries_the_quoted_original(tmp_path):
    store = Store(tmp_path / "w.db")
    sub = store.upsert_subscription(47920406, 42)
    client = QuotingClient()
    notifier = FakeNotifier()
    watcher = Watcher(store, client, notifier)

    # First round only establishes the baseline...
    assert watcher.poll_subscription(store.get_subscription(sub.id)) == []

    # ...so pretend the baseline already existed when the reply showed up.
    store.update_subscription_meta(sub.id, initialized=True, last_pid=0)
    posts = watcher.poll_subscription(store.get_subscription(sub.id))

    assert len(posts) == 1
    assert "被引用的原文内容" in posts[0].quote_text

    watcher.notify_posts(store.get_subscription(sub.id), posts)
    title, body, _url = notifier.sent[-1]
    assert "引用：" in body
    assert "被引用的原文内容" in body
    store.close()
