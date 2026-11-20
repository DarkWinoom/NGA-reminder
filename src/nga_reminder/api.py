"""HTTP API and static file serving."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import POLL_INTERVAL_KEY, Store
from .nga.bbcode import to_display_text
from .nga.client import NgaClient, parse_thread_ref
from .nga.errors import (
    NgaApiError,
    NgaAuthError,
    NgaError,
    NgaThreadUnavailableError,
)
from .nga.floors import FloorLocator
from .notify.factory import build_notifier
from .watcher import Watcher, WatcherService

logger = logging.getLogger("nga_reminder.api")

WEB_DIR = Path(__file__).parent / "web"

UID_KEY = "nga_uid"
CID_KEY = "nga_cid"

#: 一次最多补多少条旧通知的楼号。
#:
#: 补楼号要向 NGA 发请求（二分查找主题页），不能一次全发出去。
#: 每条只试一次：查得到就写上，查不到就记成 -1，不再重试。
FLOOR_BACKFILL_BATCH = 12

#: 通知桥接探测结果的缓存时间（秒）。
#:
#: 状态栏几秒就轮询一次，没必要每次都去戳桥接——十秒一次足够，
#: 也不会给宿主上的桥接添麻烦。
NOTIFY_PROBE_TTL = 10.0


@dataclass
class AppState:
    settings: Settings
    store: Store
    client: NgaClient
    watcher: Watcher
    service: WatcherService
    #: 正在补旧通知的楼号吗？同一时刻只跑一轮。
    floor_backfill_lock: threading.Lock = field(default_factory=threading.Lock)
    #: 通知桥接的探测结果（带缓存，见 ``NOTIFY_PROBE_TTL``）。
    notify_probe_lock: threading.Lock = field(default_factory=threading.Lock)
    notify_probe_at: float = 0.0
    notify_probe_ok: bool | None = None
    notify_probe_error: str | None = None

    def credentials(self) -> tuple[str, str]:
        """Credentials from the local store, falling back to the environment."""
        uid = self.store.get_value(UID_KEY) or self.settings.nga_uid
        cid = self.store.get_value(CID_KEY) or self.settings.nga_cid
        return uid, cid

    def apply_credentials(self) -> None:
        uid, cid = self.credentials()
        self.client.set_credentials(uid, cid)


# ------------------------------------------------------------------ schemas


class CookieBody(BaseModel):
    uid: str = Field(default="", description="ngaPassportUid 的值")
    cid: str = Field(default="", description="ngaPassportCid 的值")
    cookie: str = Field(default="", description="可选：直接粘贴整串 Cookie")


class SubscriptionBody(BaseModel):
    url: str = Field(default="", description="主题链接，如 read.php?tid=123&authorid=456")
    tid: int | None = Field(default=None, description="主题 ID")
    author_uid: int | None = Field(default=None, description="目标用户 UID")
    author_name: str = Field(default="", description="目标用户昵称（仅用于显示）")
    alias: str = Field(default="", description="自定义显示名称")
    color: str = Field(default="", description="标识颜色（hex）")


class SubscriptionPatch(BaseModel):
    enabled: bool | None = None
    author_name: str | None = None
    alias: str | None = None
    color: str | None = None
    title: str | None = None


class IntervalBody(BaseModel):
    seconds: int = Field(default=60, ge=0, le=86400, description="0 表示关闭轮询")


class ViewSinceBody(BaseModel):
    view_since: str = Field(default="", description="订阅时间：只收录这个时间之后的新回复")


def _post_url(tid: int | None, pid: int | None, page: int | None = None) -> str:
    """The link that opens a reply on NGA.

    When the page is known we go straight to the page the reply lives on and
    anchor on the reply itself: ``read.php?tid=…&page=76#pid…Anchor`` lands on
    the right page *and* scrolls to that floor.  Without the page we can only
    link to the single-reply view NGA builds from a pid -- which shows the
    reply, but not the thread around it.

    The ``Anchor`` suffix is not a guess -- it is what NGA puts in the page.
    Measured on 2026-11-18 (thread 47288722, page 5805), every floor carries::

        <td id='postcontainer116091'><a id='pid888185460Anchor'></a><a name='l116091'></a>

    So ``#pid888185460`` matches nothing on the page; ``#pid888185460Anchor``
    is the anchor that actually exists.
    """
    if not tid:
        return ""
    if pid and page:
        return f"https://bbs.nga.cn/read.php?tid={tid}&page={page}#pid{pid}Anchor"
    if pid:
        return f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"
    return ""


def _notify_info(state: AppState) -> dict:
    """The state of the notification channel, for the status endpoint.

    In the Docker deployment the real notification is raised by a **foreground
    process on the host** (``scripts/notify_bridge.py``) -- the container itself
    has no way to raise one.  That bridge is not installed as an autostart
    service, so it simply is not there after a reboot or when its console
    window was closed.  The symptom is nasty: the notification quietly lands in
    the log and the user only notices "no toast appeared" days later.

    So we probe the bridge and report what we find -- the UI turns an
    unreachable bridge into a visible warning with the fix in it.
    """
    notifier = state.watcher.notifier
    webhook = bool(state.settings.notify_webhook)
    info: dict = {
        "backend": type(notifier).__name__,
        "webhook": webhook,
        "webhook_url": state.settings.notify_webhook,
        "reachable": None,
        "error": getattr(notifier, "error", None),
    }
    if not webhook:
        # No bridge configured: either the native Windows backend (which can
        # toast on its own) or nothing at all.
        info["reachable"] = bool(getattr(notifier, "available", True))
        if info["reachable"]:
            info["error"] = None
        return info

    probe = getattr(notifier, "probe", None)
    if probe is None:
        return info
    if state.notify_probe_lock.acquire(blocking=False):
        try:
            if time.monotonic() - state.notify_probe_at > NOTIFY_PROBE_TTL:
                state.notify_probe_ok, state.notify_probe_error = probe()
                state.notify_probe_at = time.monotonic()
        finally:
            state.notify_probe_lock.release()
    info["reachable"] = state.notify_probe_ok
    info["error"] = state.notify_probe_error or getattr(notifier, "error", None)
    return info


# --------------------------------------------------------------------- app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    store = Store(settings.db_path)
    client = NgaClient(
        base_url=settings.nga_base_url,
        uid=settings.nga_uid,
        cid=settings.nga_cid,
        user_agent=settings.nga_ua,
        timeout=settings.request_timeout,
        delay_ms=settings.delay_ms,
        max_retries=settings.max_retries,
    )
    notifier = build_notifier(
        settings.toast_app_id,
        webhook_url=settings.notify_webhook,
        webhook_token=settings.notify_webhook_token,
    )
    # 楼层定位器：把回复的 pid 换算成 NGA 页面上的真实楼号与页码。
    locator = FloorLocator(client)
    watcher = Watcher(
        store, client, notifier, app_url=settings.app_url, locator=locator
    )

    def _stored_interval() -> int:
        """The interval the user picked, falling back to the configured one."""
        raw = store.get_value(POLL_INTERVAL_KEY)
        if raw is None:
            return settings.poll_interval
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return settings.poll_interval

    # 抓取间隔以界面设置为准，改完立刻生效。
    service = WatcherService(
        watcher,
        interval=settings.poll_interval,
        interval_provider=_stored_interval,
    )
    state = AppState(
        settings=settings,
        store=store,
        client=client,
        watcher=watcher,
        service=service,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.apply_credentials()
        # 通知默认不追溯：每次启动都开一个新的窗口，只记录之后新增的内容。
        # 但用户手动设过订阅时间就以那个为准，重启不再覆盖。
        if _store().is_view_since_pinned():
            # 先把账本和窗口对齐：早于订阅时间却标成「报过」的楼层（老逻辑
            # 留下的）改回没报过，通知流里才只剩订阅时间之后的内容。
            fixed = _store().sync_window_ledger()
            if fixed:
                logger.info("清理了 %d 条早于订阅时间的通知记录", fixed)
            _store().collect_since(_store().get_view_since())
            logger.info(
                "沿用已设置的订阅时间 %s", _store().get_view_since()
            )
        else:
            _store().reset_view()
        service.start()
        logger.info(
            "NGA Reminder 已启动，轮询间隔 %ds", _stored_interval()
        )
        try:
            yield
        finally:
            await service.stop()
            client.close()
            store.close()

    app = FastAPI(title="NGA Reminder", version="0.1.0", lifespan=lifespan)
    app.state.nga = state

    def _store() -> Store:
        return state.store

    # -- status ---------------------------------------------------------

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/status")
    def get_status() -> dict:
        uid, cid = state.credentials()
        # 暂停状态实时算一次：用户刚点「暂停追踪」时，界面不必等到下一轮
        # 才反映出来。计时器照常走，所以这里只描述"现在会不会发请求"。
        # fetch_state() 会顺手刷新 status.resume_at，所以字典要在它**之后**取。
        paused, reason = state.watcher.fetch_state()
        watcher = state.watcher.status.to_dict()
        watcher["paused"] = paused
        watcher["paused_reason"] = reason
        return {
            "credentials_configured": bool(uid and cid),
            "credentials_source": "store" if _store().get_value(UID_KEY) else "env",
            "base_url": settings.nga_base_url,
            "poll_interval": _stored_interval(),
            "view_since": _store().get_view_since(),
            "resume_at": _store().get_resume_at(),
            "watcher": watcher,
            "store": _store().summary(),
            "notify": _notify_info(state),
        }

    # -- settings --------------------------------------------------------

    @app.get("/api/settings")
    def get_settings_route() -> dict:
        """Everything the UI needs to render the top bar."""
        return {
            "poll_interval": _stored_interval(),
            "app_url": settings.app_url,
            "view_since": _store().get_view_since(),
            "resume_at": _store().get_resume_at(),
        }

    @app.post("/api/settings/poll-interval")
    def set_poll_interval(body: IntervalBody) -> dict:
        """Change how often we fetch. ``0`` stops fetching altogether."""
        seconds = max(0, int(body.seconds))
        _store().set_value(POLL_INTERVAL_KEY, str(seconds))
        state.service.set_interval(seconds)
        logger.info("抓取间隔改为 %ds", seconds)
        return {"poll_interval": seconds, "fetching": seconds > 0}

    @app.post("/api/settings/view-since")
    def set_view_since(body: ViewSinceBody, background: BackgroundTasks) -> dict:
        """Move the subscription time: only replies after it get collected.

        Moving the time backwards pulls in the replies that were already
        alerted but never collected; each one is still collected only once.

        A time in the future means "do not start before then": fetching is
        frozen until that moment and resumes on its own.
        """
        moment = _parse_local_timestamp(body.view_since)
        if moment is None:
            raise HTTPException(
                status_code=400,
                detail="时间格式不对，请用「2026-11-16 14:00」这样的格式",
            )
        stamp = moment.isoformat()
        future = moment > datetime.now(UTC)
        # 未来的订阅时间 = 冻结到那一刻；过去或当下 = 立刻恢复。
        _store().set_resume_at(stamp if future else None)
        previous = _parse_local_timestamp(_store().get_view_since())
        collected = _store().set_view_since(stamp)
        state.service.wake()
        if future:
            logger.info("订阅时间改为 %s：抓取冻结到那一刻，到点自动恢复", stamp)
        else:
            logger.info("订阅时间改为 %s（新增收录 %d 条）", stamp, collected)
        rescan = False
        if not future and (previous is None or moment < previous):
            # 往前挪了：这段时间里的回复得重新找出来。
            state.watcher.request_rescan()
            background.add_task(state.watcher.rescan_enabled)
            rescan = True
        return {
            "view_since": stamp,
            "collected": collected,
            "resume_at": stamp if future else None,
            "rescan": rescan,
        }

    # -- subscriptions --------------------------------------------------

    @app.get("/api/subscriptions")
    def list_subscriptions() -> list[dict]:
        unread = _store().unread_counts()
        return [
            {**sub.to_dict(), "unread": unread.get(sub.id, 0)}
            for sub in _store().list_subscriptions()
        ]

    @app.post("/api/subscriptions")
    def create_subscription(body: SubscriptionBody) -> dict:
        tid, url_author_uid = (None, None)
        if body.url:
            try:
                tid, url_author_uid = parse_thread_ref(body.url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif body.tid:
            tid = body.tid

        if tid is None or not 0 < tid <= 2**63 - 1:
            raise HTTPException(
                status_code=400,
                detail="请提供主题链接或 tid",
            )

        author_uid = body.author_uid or url_author_uid
        if author_uid is None or not 0 < author_uid <= 2**63 - 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "需要目标用户的数字 UID。"
                    "可以直接粘贴形如 "
                    "read.php?tid=123456&authorid=61765516 的链接，"
                    "昵称无法反查 UID。"
                ),
            )

        sub = _store().upsert_subscription(
            tid,
            author_uid,
            author_name=body.author_name,
            title="",
            alias=body.alias,
            color=body.color,
        )
        try:
            count, delivered, reason = state.watcher.check_subscription(
                sub.id, initialize_only=True
            )
        except NgaError as exc:
            logger.info("新订阅取数失败 (tid=%s): %s", tid, exc)
            count, delivered, reason = 0, False, str(exc)

        refreshed = _store().get_subscription(sub.id) or sub
        return {
            **refreshed.to_dict(),
            "fetch": {
                "new_posts": count,
                "notified": delivered,
                "skipped": bool(reason),
                "reason": reason,
            },
        }

    @app.patch("/api/subscriptions/{sub_id}")
    def patch_subscription(sub_id: int, body: SubscriptionPatch) -> dict:
        if body.enabled is not None:
            _store().set_enabled(sub_id, body.enabled)
        # 一次改完：update_subscription_meta 本来就支持同时带好几个字段，
        # 分成几次调就是多写几遍库。
        _store().update_subscription_meta(
            sub_id,
            title=body.title,
            author_name=body.author_name,
            alias=body.alias,
            color=body.color,
        )
        sub = _store().get_subscription(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        return sub.to_dict()

    @app.delete("/api/subscriptions/{sub_id}")
    def delete_subscription(sub_id: int) -> dict:
        if not _store().delete_subscription(sub_id):
            raise HTTPException(status_code=404, detail="订阅不存在")
        return {"deleted": sub_id}

    @app.get("/api/subscriptions/{sub_id}/posts")
    def list_posts(sub_id: int, limit: int = 50) -> list[dict]:
        sub = _store().get_subscription(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        since = _store().get_view_since()
        posts = _store().list_posts(sub_id, limit=limit, since=since)
        # 库里存的是原始 BBCode，界面要的是能直接读的正文
        for post in posts:
            post["content"] = to_display_text(post.get("content") or "")
            post["url"] = _post_url(
                sub.tid, post.get("pid"), post.get("floor_page")
            )
        return posts

    @app.post("/api/subscriptions/{sub_id}/read")
    def mark_read(sub_id: int) -> dict:
        return {"marked": _store().mark_posts_read(sub_id)}

    @app.post("/api/subscriptions/{sub_id}/check")
    def check_now(sub_id: int) -> dict:
        sub = _store().get_subscription(sub_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="订阅不存在")
        try:
            count, delivered, reason = state.watcher.check_subscription(sub_id)
        except NgaAuthError:
            raise HTTPException(status_code=401, detail="NGA Cookie 无效或已过期") from None
        except NgaThreadUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except NgaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

        return {
            "subscription_id": sub_id,
            "new_posts": count,
            "notified": delivered,
            "skipped": bool(reason),
            "reason": reason,
        }

    @app.post("/api/check")
    async def check_all() -> dict:
        """Run one polling round for every subscription."""
        report = await state.service.poll_now()
        return report.to_dict()

    # -- setup ----------------------------------------------------------

    @app.get("/api/setup/cookie")
    def cookie_status() -> dict:
        """Report whether a credential is configured -- never the secret."""
        uid, cid = state.credentials()
        return {
            "configured": bool(uid and cid),
            "source": "store" if _store().get_value(UID_KEY) else ("env" if uid else None),
            # 完整的 uid：用户要靠它分辨配的是哪个账号。
            # cid 是密钥，任何时候都不回显。
            "uid": uid,
            "uid_hint": f"…{uid[-2:]}" if len(uid) >= 2 else "",
        }

    @app.post("/api/setup/cookie/verify")
    def verify_cookie(body: CookieBody) -> dict:
        uid, cid = _parse_cookie(body)
        if not uid.isascii() or not uid.isdigit() or not cid:
            raise HTTPException(status_code=400, detail="请提供数字 UID 和 CID")

        probe = NgaClient(
            base_url=settings.nga_base_url,
            uid=uid,
            cid=cid,
            user_agent=settings.nga_ua,
            timeout=settings.request_timeout,
            delay_ms=0,
            max_retries=1,
        )
        try:
            profile = probe.get_user_profile(int(uid))
        except NgaAuthError:
            raise HTTPException(
                status_code=401, detail="Cookie 无效或已过期，请重新复制"
            ) from None
        except NgaApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except (NgaError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            probe.close()

        return {
            "ok": True,
            "uid": uid,
            "username": profile.get("username", ""),
            "group": profile.get("group", ""),
        }

    @app.post("/api/setup/cookie")
    def save_cookie(body: CookieBody) -> dict:
        uid, cid = _parse_cookie(body)
        if not uid.isascii() or not uid.isdigit() or not cid:
            raise HTTPException(status_code=400, detail="请提供数字 UID 和 CID")
        _store().set_value(UID_KEY, uid)
        _store().set_value(CID_KEY, cid)
        state.apply_credentials()
        return {"saved": True, "uid": uid}

    @app.delete("/api/setup/cookie")
    def clear_cookie() -> dict:
        _store().delete_value(UID_KEY)
        _store().delete_value(CID_KEY)
        state.apply_credentials()
        return {"cleared": True}

    @app.delete("/api/setup/all")
    def wipe_everything() -> dict:
        """Erase every trace of this install: account, subscriptions, ledger,
        notifications and settings -- back to the state of a first run.

        This is what the account dialog's "清除全部数据" button calls.  After
        it, the app has no credentials and no subscriptions, which is exactly
        the condition that brings the onboarding guide back.
        """
        since = _store().wipe_all()
        state.apply_credentials()
        logger.info("已清除全部数据：账号、订阅、通知与设置")
        return {"wiped": True, "view_since": since}

    # -- notifications --------------------------------------------------

    def _backfill_floors(items: list[dict]) -> None:
        """Give the older notifications their real floor number.

        Replies discovered before the locator existed have no floor number
        yet.  Finding it costs a handful of requests (the thread's pages are
        bisected), so this is done in the background, a few at a time, and
        only while the user is actually looking at the list.

        Each reply is tried once — but only a reply we are *sure* about is
        marked ``-1`` (gone for good, never looked up again).  A reply the
        locator merely ran out of probes for stays ``0``, so the next visit
        gives it another go: 「没找完」和「确实不存在」不是一回事。
        """
        targets = [item for item in items if not item.get("floor_no")][
            :FLOOR_BACKFILL_BATCH
        ]
        if not targets or not state.floor_backfill_lock.acquire(blocking=False):
            return

        try:
            by_thread: dict[int, list[dict]] = {}
            for item in targets:
                tid = item.get("tid")
                if tid:
                    by_thread.setdefault(int(tid), []).append(item)
            for tid, group in by_thread.items():
                pids = [int(item["pid"]) for item in group if item.get("pid")]
                try:
                    locations, inconclusive = state.watcher.locate_history(tid, pids)
                except NgaError as exc:
                    logger.info("补楼号失败 (tid=%s): %s", tid, exc)
                    continue
                for item in group:
                    pid = int(item.get("pid") or 0)
                    location = locations.get(pid)
                    if location:
                        floor_no = location.floor_no
                        floor_page = location.page
                    elif pid in inconclusive:
                        # 这次没找完——留着 0，下次打开列表再试。
                        floor_no = 0
                        floor_page = 0
                    else:
                        # 找遍了都没有：这个楼层大概已经被删了，不再重试。
                        floor_no = -1
                        floor_page = 0
                    _store().set_post_floor(
                        int(item.get("subscription_id") or 0), pid, floor_no, floor_page
                    )
        finally:
            state.floor_backfill_lock.release()

    @app.get("/api/notifications")
    def list_notifications(
        background: BackgroundTasks,
        limit: int = 200,
        after: str = "",
        before: str = "",
        subscription_id: int | None = None,
    ) -> list[dict]:
        """The notification feed: one entry per alerted floor.

        Only what arrived since the app started (or since the list was last
        cleared) is here -- the feed never goes back in time.

        ``after`` / ``before`` are cursors ("<收录时间>|<pid>") and
        ``subscription_id`` narrows the list to one subscription: together
        they drive the endless scroll.
        """
        since = _store().get_view_since()
        feed = _store().list_feed(
            limit=limit,
            since=since,
            after=after,
            before=before,
            subscription_id=subscription_id,
        )
        for item in feed:
            item["content"] = to_display_text(item.get("content") or "")
            item["url"] = _post_url(
                item.get("tid"), item.get("pid"), item.get("floor_page")
            )
            # 游标由服务端生成：客户端原样带回来，不用自己拼时间戳。
            item["cursor"] = f"{item.get('created_at') or ''}|{item.get('pid') or 0}"
        # 老通知还没有楼号——顺手在后台补一批，用户下一轮刷新就能看到。
        missing = [item for item in feed if not item.get("floor_no")]
        if missing and state.client.has_credentials:
            background.add_task(_backfill_floors, missing)
        return feed

    @app.post("/api/notifications/clear")
    def clear_notifications() -> dict:
        """Empty the feed. The de-duplication ledger stays, so nothing repeats.

        The subscription time becomes the current moment -- and a freeze (a
        subscription time in the future) is lifted with it.
        """
        since = _store().clear_view()
        _store().set_resume_at(None)
        state.service.wake()
        return {"cleared": True, "view_since": since}

    @app.post("/api/notify/test")
    def test_notification() -> dict:
        """Fire a test toast so the user can verify the notification channel."""
        backend = type(state.watcher.notifier).__name__
        delivered = False
        error = None
        try:
            delivered = bool(
                state.watcher.notifier.notify(
                    "NGA Reminder",
                    "这是一条测试通知。看到它就说明桌面通知通道正常。",
                    url=settings.app_url,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            error = str(exc)
        # The notifier keeps its own error (unreachable bridge, bad token, …);
        # surface it, otherwise the UI can only say "failed".
        error = error or getattr(state.watcher.notifier, "error", None)
        # The test itself is a fresh answer about the channel: refresh the
        # cached probe so the status line agrees with what we just saw.
        with state.notify_probe_lock:
            state.notify_probe_at = time.monotonic()
            state.notify_probe_ok = delivered if settings.notify_webhook else None
            state.notify_probe_error = error
        return {"backend": backend, "delivered": delivered, "error": error}

    # -- frontend -------------------------------------------------------

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.middleware("http")
        async def _no_cache_frontend(request: Request, call_next):
            """本机自用工具：前端资源每次回源校验，避免改完还在跑旧缓存。"""
            response = await call_next(request)
            path = request.url.path
            if path == "/" or path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    @app.exception_handler(NgaAuthError)
    def _auth_handler(_request: Request, exc: NgaAuthError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    return app


def _parse_local_timestamp(raw: str) -> datetime | None:
    """Turn a user-typed timestamp into an aware UTC datetime.

    A value that carries its own timezone is trusted as-is -- that is what the
    UI sends, because only the browser knows which timezone the user is in.

    A value *without* a timezone falls back to this machine's local time.  That
    fallback is only sensible for a direct API call: the machine running the
    server and the machine showing the browser are not necessarily in the same
    timezone (the Docker image runs on UTC while the desktop runs on UTC+8).
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC)


def _parse_cookie(body: CookieBody) -> tuple[str, str]:
    """Accept either the two fields or a pasted raw Cookie string."""
    uid = (body.uid or "").strip()
    cid = (body.cid or "").strip()
    if uid and cid:
        return uid, cid
    raw = (body.cookie or "").strip()
    if raw:
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, _, value = part.strip().partition("=")
            if key == "ngaPassportUid" and not uid:
                uid = value
            elif key == "ngaPassportCid" and not cid:
                cid = value
    return uid, cid


_app: FastAPI | None = None


def __getattr__(name: str):
    """Build the app lazily.

    ``uvicorn nga_reminder.api:app`` works, but merely importing this module
    no longer creates the database file as a side effect.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
