"""Tests for the SQLite store."""

from __future__ import annotations

import pytest

from nga_reminder.db import Store
from nga_reminder.models import Floor


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_floor(pid: int, uid: int = 42, ts: int = 1_700_000_000, lou: int = 3) -> Floor:
    return Floor(
        pid=pid,
        lou=lou,
        author_uid=uid,
        author_name="someone",
        timestamp=ts,
        content="hello",
    )


# ----------------------------------------------------------- subscriptions


def test_upsert_creates_then_updates(store):
    sub = store.upsert_subscription(123, 42, author_name="alice", title="主题 A")
    assert sub.id > 0
    assert sub.author_name == "alice"
    assert sub.title == "主题 A"

    again = store.upsert_subscription(123, 42, title="主题 A 改名了")
    assert again.id == sub.id
    assert again.title == "主题 A 改名了"
    assert again.author_name == "alice", "空值不应覆盖已有昵称"
    assert len(store.list_subscriptions()) == 1


def test_different_users_in_one_thread_are_separate(store):
    a = store.upsert_subscription(123, 42)
    b = store.upsert_subscription(123, 43)
    assert a.id != b.id
    assert len(store.list_subscriptions()) == 2


def test_list_subscriptions_can_filter_enabled(store):
    a = store.upsert_subscription(1, 1)
    store.upsert_subscription(2, 2)
    store.set_enabled(a.id, False)

    assert len(store.list_subscriptions()) == 2
    assert [s.id for s in store.list_subscriptions(enabled_only=True)] == [
        s.id for s in store.list_subscriptions() if s.enabled
    ]


def test_delete_removes_related_rows(store):
    sub = store.upsert_subscription(1, 1)
    store.record_post(sub.id, make_floor(100))
    store.log_notification(sub.id, 100, "t", "s")

    assert store.delete_subscription(sub.id) is True
    assert store.get_subscription(sub.id) is None
    assert store.list_posts(sub.id) == []
    assert store.list_notifications() == []


def test_update_meta(store):
    sub = store.upsert_subscription(1, 1)
    store.update_subscription_meta(
        sub.id, title="新标题", last_pid=555, checked=True
    )

    refreshed = store.get_subscription(sub.id)
    assert refreshed.title == "新标题"
    assert refreshed.last_pid == 555
    assert refreshed.last_checked_at is not None


# -------------------------------------------------------------------- posts


def test_record_post_deduplicates(store):
    sub = store.upsert_subscription(1, 1)

    assert store.record_post(sub.id, make_floor(100)) is True
    assert store.record_post(sub.id, make_floor(100)) is False
    assert store.has_seen(sub.id, 100) is True
    assert len(store.list_posts(sub.id)) == 1


def test_same_pid_in_two_subscriptions_is_independent(store):
    a = store.upsert_subscription(1, 1)
    b = store.upsert_subscription(2, 2)

    assert store.record_post(a.id, make_floor(100)) is True
    assert store.record_post(b.id, make_floor(100)) is True


def test_unread_count_only_counts_notified_posts(store):
    sub = store.upsert_subscription(1, 1)
    store.record_post(sub.id, make_floor(100), notified=True)
    store.record_post(sub.id, make_floor(101), notified=False)
    # 只有收进通知流的楼层才算未读
    store.collect_posts(sub.id, [100])

    assert store.unread_count(sub.id) == 1

    store.mark_posts_read(sub.id)
    assert store.unread_count(sub.id) == 0


def test_list_posts_is_newest_first(store):
    sub = store.upsert_subscription(1, 1)
    store.record_post(sub.id, make_floor(1, ts=100))
    store.record_post(sub.id, make_floor(2, ts=300))
    store.record_post(sub.id, make_floor(3, ts=200))

    assert [p["pid"] for p in store.list_posts(sub.id)] == [2, 3, 1]


def test_list_posts_respects_limit(store):
    sub = store.upsert_subscription(1, 1)
    for pid in range(10):
        store.record_post(sub.id, make_floor(pid, ts=pid))

    assert len(store.list_posts(sub.id, limit=3)) == 3


# ------------------------------------------------------------ notifications


def test_log_and_list_notifications(store):
    sub = store.upsert_subscription(7, 42, author_name="bob", title="T")
    store.log_notification(sub.id, 100, "T", "摘要", delivered=True)

    notes = store.list_notifications()
    assert len(notes) == 1
    assert notes[0]["title"] == "T"
    assert notes[0]["summary"] == "摘要"
    assert notes[0]["delivered"] == 1
    assert notes[0]["tid"] == 7
    assert notes[0]["author_name"] == "bob"


# -------------------------------------------------------------- key / value


def test_kv_roundtrip(store):
    assert store.get_value("nga_uid") is None
    store.set_value("nga_uid", "12345")
    assert store.get_value("nga_uid") == "12345"
    store.set_value("nga_uid", "67890")
    assert store.get_value("nga_uid") == "67890"
    store.delete_value("nga_uid")
    assert store.get_value("nga_uid") is None


def test_store_persists_across_instances(tmp_path):
    path = tmp_path / "persist.db"
    first = Store(path)
    first.upsert_subscription(123, 42, title="持久化")
    first.close()

    second = Store(path)
    subs = second.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].title == "持久化"
    second.close()
