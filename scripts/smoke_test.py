"""End-to-end smoke test against the live NGA API.

Starts the real server with real credentials, creates a subscription for a
thread that is actually active, runs a polling round and checks that

  * the API answers,
  * the thread is read successfully,
  * the first round only establishes a baseline (no notification),
  * the Windows toast channel accepts a message.

Usage:
    set NGA_UID / NGA_CID, then:
    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nga_reminder.config import Settings  # noqa: E402
from nga_reminder.nga.client import NgaClient  # noqa: E402

BASE = os.environ.get("NGA_SMOKE_BASE", "http://127.0.0.1:8799")
ok = True


def step(name: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    settings = Settings()
    uid = os.environ.get("NGA_UID", settings.nga_uid)
    cid = os.environ.get("NGA_CID", settings.nga_cid)
    if not uid or not cid:
        print("NGA_UID / NGA_CID are required", file=sys.stderr)
        return 2

    # --- find a live thread and one of its posters ----------------------
    probe = NgaClient(
        base_url=settings.nga_base_url,
        uid=uid,
        cid=cid,
        user_agent=settings.nga_ua,
        delay_ms=0,
        max_retries=2,
    )
    topics = probe.get_forum_topics(-7, 1)
    step("读取当前版块主题列表", bool(topics), f"{len(topics)} 个主题")
    if not topics:
        return 1

    tid = topics[0]
    page = probe.get_thread_page(tid, 1)
    step(f"读取主题 tid={tid}", page.total_page > 0, f"标题《{page.title}》")

    author_uid = None
    for floor in page.floors:
        if floor.pid > 0 and floor.author_uid:
            author_uid = floor.author_uid
            break
    step("在主题中找到一个发帖人", author_uid is not None, f"uid={author_uid}")
    if author_uid is None:
        return 1

    filtered = probe.get_thread_page(tid, 1, author_id=author_uid)
    step(
        "服务端按 authorid 过滤生效",
        all(f.author_uid == author_uid for f in filtered.floors),
        f"{len(filtered.floors)} 条属于 uid={author_uid}",
    )
    probe.close()

    # --- drive the running server ---------------------------------------
    client = httpx.Client(base_url=BASE, timeout=60.0)

    for _ in range(40):
        try:
            if client.get("/api/status").status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        print(f"服务未在 {BASE} 启动", file=sys.stderr)
        return 1

    status = client.get("/api/status").json()
    step("服务已就绪", True, f"凭据来源={status.get('credentials_source')}")
    step("凭据已配置", status.get("credentials_configured") is True)

    page_html = client.get("/")
    step("首页可访问", page_html.status_code == 200 and "NGA" in page_html.text)
    step("静态资源可访问", client.get("/static/app.js").status_code == 200)

    created = client.post(
        "/api/subscriptions",
        json={"tid": tid, "author_uid": author_uid, "author_name": "冒烟测试"},
    )
    step("添加订阅", created.status_code == 200, created.text[:160])
    if created.status_code != 200:
        return 1
    sub_id = created.json()["id"]

    report = client.post(f"/api/subscriptions/{sub_id}/check").json()
    step(
        "首轮轮询建立基线",
        report.get("new_posts") == 0,
        f"报告={report}",
    )
    step("首轮没有产生通知", report.get("notified") is False)

    notes = client.get("/api/notifications").json()
    step("通知历史为空", len(notes) == 0)

    subs = client.get("/api/subscriptions").json()
    step("订阅列表包含新条目", any(s["id"] == sub_id for s in subs))

    posts = client.get(f"/api/subscriptions/{sub_id}/posts").json()
    step("基线楼层已入库", len(posts) > 0, f"{len(posts)} 条")

    # --- notification channel -------------------------------------------
    from nga_reminder.notify.factory import build_notifier

    notifier = build_notifier(settings.toast_app_id)
    delivered = notifier.notify(
        "NGA Reminder 冒烟测试", "如果你看到这条通知，说明桌面通知通道正常。", url=""
    )
    step("桌面通知通道", delivered, f"backend={type(notifier).__name__}")

    client.delete(f"/api/subscriptions/{sub_id}")
    step("清理测试订阅", True)

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
