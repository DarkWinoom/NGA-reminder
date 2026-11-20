"""Polling loop and incremental detection.

The detection algorithm:

1. Fetch the thread's **last page, unfiltered** -- one request that every
   subscription on that thread shares.  It carries the thread's newest floors.
2. If the subscription's high-water mark already sits on that page, the new
   floors are exactly the author's floors on it (the proof is in
   :meth:`Watcher._quick_new_floors`).  Nothing else has to be requested.
3. Otherwise fall back to the author's **filtered** pages: NGA filters
   server-side, so ``totalPage`` counts the *filtered* pages.  Fetch the last
   filtered page -- new floors always live at the end.
4. If the smallest pid on that page is newer than our high-water mark there is
   a *gap*: the author may have flooded so fast that older floors were pushed
   onto an earlier page.  Walk backwards until a known pid shows up.
5. Keep floors that are newer than the high-water mark, belong to the author,
   and have never been seen.
6. On the very first poll, cut the backlog at the subscription time: floors
   posted after it are reported, older ones are only remembered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .db import Store
from .models import Floor, NewPost, Subscription, ThreadPage
from .nga.bbcode import extract_quotes, to_display_text, to_single_line
from .nga.client import NgaClient
from .nga.errors import NgaAuthError, NgaError, NgaRequestError, NgaThreadUnavailableError
from .nga.floors import FloorLocation, FloorLocator
from .notify.base import Notifier

logger = logging.getLogger("nga_reminder.watcher")

#: 建基线时最多往前翻多少页（每页 20 层）。
#:
#: 新加订阅要按订阅时间往前翻页，把窗口里的回复都找出来。翻太多页既慢又
#: 不礼貌——一个刷屏的作者几个月能刷出几千层，翻到头也没什么意义。
MAX_BACKFILL_PAGES = 30


@dataclass
class PollReport:
    """Outcome of one polling round."""

    checked: int = 0
    new_posts: int = 0
    notifications: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "new_posts": self.new_posts,
            "notifications": self.notifications,
            "errors": list(self.errors),
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


@dataclass
class WatcherStatus:
    """What the UI shows in its status bar."""

    running: bool = False
    last_run_at: datetime | None = None
    last_error: str | None = None
    auth_error: str | None = None
    last_report: dict | None = None
    #: 抓取被挂起的原因（全部订阅暂停 / 没有订阅）。计时器仍在走。
    paused: bool = False
    paused_reason: str | None = None
    #: 订阅时间在将来时，抓取冻结到这一刻；到点自动恢复。
    resume_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "auth_error": self.auth_error,
            "last_report": self.last_report,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "resume_at": self.resume_at,
        }


class TopicFetch:
    """同一个主题的多个订阅共用一份取数结果。

    NGA 的「只看某人」是**服务端**过滤：带上 ``authorid`` 之后，页数和楼层
    都是过滤后的，跟全主题对不上。所以同一主题、两个不同作者的订阅，数据
    本来就不同——能共享的只有**不带过滤**的那一份。

    这一份就是主题的最后一页：整个主题最新的楼层都在上面。谁的高水位已经
    落在这一页里，谁的新回复就一定全在这一页上（证明见
    :meth:`Watcher._quick_new_floors`）；落不上的再按作者过滤取一次。

    主题的页数跨轮次记着：尾页那一页的响应里带着主题**当前**的总页数，
    所以下一轮直接去那一页问一声就行，省掉「先取第 1 页问总页数」。
    """

    def __init__(self, fetch_page: Callable[[int, int, int], ThreadPage]) -> None:
        self._page = fetch_page
        #: 主题现在有多少页（跨轮次留着）。
        self._total_hint: dict[int, int] = {}
        #: 这一轮里取到的尾页。
        self._tails: dict[int, ThreadPage | None] = {}

    def reset(self) -> None:
        """新一轮开始：尾页要重新取，不能拿上一轮的旧数据。"""
        self._tails.clear()

    def tail(self, tid: int) -> ThreadPage | None:
        """主题的最后一页；拿不准就返回 ``None``（调用方走老路）。"""
        if tid not in self._tails:
            self._tails[tid] = self._fetch_tail(tid)
        return self._tails[tid]

    def _fetch_tail(self, tid: int) -> ThreadPage | None:
        page = self._total_hint.get(tid)
        if not page:
            # 不知道主题有多少页：先问第 1 页，顺带把标题也拿了。
            first = self._page(tid, 1, 0)
            page = max(first.total_page, 1)
            self._total_hint[tid] = page
            if page == 1:
                return first

        data = self._page(tid, page, 0)
        total = max(data.total_page, 1)
        if total != page:
            # 主题长了（或缩了）：响应里带着**当前**的总页数，照它再取一次。
            page = total
            self._total_hint[tid] = page
            data = self._page(tid, page, 0)
            total = max(data.total_page, 1)
        self._total_hint[tid] = total
        if total != page or not data.floors:
            # 对不上（主题正在长，或这一页是空的）：这一轮别信它。
            return None
        return data


class Watcher:
    """Polls every enabled subscription and reports new floors."""

    def __init__(
        self,
        store: Store,
        client: NgaClient,
        notifier: Notifier,
        *,
        app_url: str = "http://127.0.0.1:8765/",
        locator: FloorLocator | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.notifier = notifier
        self.app_url = app_url
        self.logger = logger_ or logger
        self.status = WatcherStatus()
        # 楼层定位器：把回复的 pid 换算成 NGA 页面上的真实楼号。
        # 没有它也能跑，只是界面上的楼层号会是空的。
        self.locator = locator
        # 抓取闸门：同一时刻只允许一轮抓取在跑。
        #
        # 轮询循环、界面上的「立即检查」、新建订阅时取标题——这些路径都可能
        # 触发对 NGA 的请求。开多个网页窗口时它们会同时到达，如果没有这把
        # 锁，同一份数据就会被反复拉一遍。RLock 是可重入的：poll_all 在自己
        # 的线程里调用 poll_subscription 不会自锁。
        self._fetch_lock = threading.RLock()
        #: 一轮抓取里取过的页：同一页不取第二次。
        self._pages: dict[tuple[int, int, int], ThreadPage | NgaError] = {}
        #: 一轮抓取里取过的楼层（取引用原文用）：同一层不取第二次。
        self._floors: dict[tuple[int, int], Floor | None] = {}
        #: 每个 (主题, 作者) 的尾页页号——跨轮次留着，下一轮少取一次请求。
        self._tail_hints: dict[tuple[int, int], int] = {}
        #: 主题标题（跨轮次留着）。
        self._titles: dict[int, str] = {}
        #: 同主题共享的取数：主题的最后一页，所有订阅共用。
        self._topic = TopicFetch(self._page)
        #: 当前嵌套了几层「一轮抓取」——最外层才清缓存。
        self._round_depth = 0
        #: 有没有被要求「按当前订阅时间重新扫一遍」。
        self._rescan_requested = False

    # -- fetch state ------------------------------------------------------

    def fetch_state(self) -> tuple[bool, str | None]:
        """Is there anything to fetch right now?

        Returns ``(paused, reason)``.  ``paused`` means *no request goes out* --
        it does not mean the timer stops: the interval keeps running either
        way, so re-enabling a subscription does not need any reset.
        """
        resume = self.store.pending_resume(datetime.now(UTC))
        if resume:
            # 订阅时间被设到了将来：抓取冻结到那一刻，到点自己恢复。
            self.status.resume_at = resume
            return True, "订阅时间在将来"
        self.status.resume_at = None
        # 一次取全量就够了：启用中的有几个、一共几个，同一份列表都能数出来。
        # /api/status 每 15 秒就被问一次，没必要把库查两遍。
        subs = self.store.list_subscriptions()
        enabled = sum(1 for sub in subs if sub.enabled)
        if enabled:
            return False, None
        return True, ("所有订阅均已暂停" if subs else "还没有订阅")

    # -- detection --------------------------------------------------------

    @contextlib.contextmanager
    def _round(self):
        """一轮抓取：里面的请求共享「这一轮取过什么」的记号。

        最外层才清缓存——``poll_all`` 里每个订阅都算同一轮，同主题的共享
        数据和同一页的楼层都只取一次。
        """
        with self._fetch_lock:
            self._round_depth += 1
            try:
                if self._round_depth == 1:
                    self._reset_round()
                yield
            finally:
                self._round_depth -= 1

    def _reset_round(self) -> None:
        """一轮抓取开始：清掉「这一轮里取过什么」的记号。

        尾页页号和标题**不**清——它们要跨轮次留着，下一轮才知道该取哪一页。
        """
        self._pages.clear()
        self._floors.clear()
        self._topic.reset()

    def poll_subscription(self, sub: Subscription) -> list[NewPost]:
        """Check one subscription and return the floors that are new.

        Raises:
            NgaAuthError: the credentials are not working.
            NgaThreadUnavailableError: the thread cannot be read right now.
            NgaError: any other transport or API problem.
        """
        with self._round():
            return self._poll_subscription_locked(sub)

    def rescan_subscription(self, sub: Subscription) -> list[NewPost]:
        """Cut the backlog again, against the *current* subscription time.

        A subscription's baseline is cut once, when it is created.  The
        subscription time is "from when do I care" though, and it moves: when
        it moves backwards the floors the old baseline swallowed have to
        become reportable again.  So the baseline is cleared and the thread
        is read again -- this time against the time that is in force now.
        """
        with self._round():
            self.store.reset_subscription_baseline(sub.id)
            fresh = self.store.get_subscription(sub.id) or sub
            return self._poll_subscription_locked(fresh)

    def check_subscription(
        self, sub_id: int, *, initialize_only: bool = False
    ) -> tuple[int, bool, str]:
        """Check current subscription state and deliver inside the same round."""
        with self._round():
            sub = self.store.get_subscription(sub_id)
            if sub is None:
                return 0, False, "订阅不存在"
            if not sub.enabled:
                return 0, False, "订阅已暂停"
            if initialize_only and sub.initialized:
                return 0, False, "订阅已存在"
            if self.store.pending_resume(datetime.now(UTC)):
                return 0, False, "订阅时间在将来"
            if not self.client.has_credentials:
                raise NgaAuthError("尚未配置 NGA Cookie")
            posts = self._poll_subscription_locked(sub)
            return len(posts), self.notify_posts(sub, posts), ""

    def locate_history(
        self, tid: int, pids: list[int]
    ) -> tuple[dict[int, FloorLocation], set[int]]:
        """Resolve saved replies without competing with an active poll."""
        with self._fetch_lock:
            if self.locator is None or not self.client.has_credentials:
                return {}, set(pids)
            if self.store.pending_resume(datetime.now(UTC)):
                return {}, set(pids)
            locations = self.locator.locate_many(tid, pids)
            return locations, set(self.locator.inconclusive)

    def request_rescan(self) -> None:
        """Ask for a rescan of every subscription (done in the background)."""
        self._rescan_requested = True

    def rescan_enabled(self) -> None:
        """Apply a pending window change to enabled subscriptions in one round."""
        with self._round():
            if not self._rescan_requested:
                return
            self._rescan_requested = False
            if not self.client.has_credentials or self.store.pending_resume(datetime.now(UTC)):
                return
            for sub in self.store.list_subscriptions(enabled_only=True):
                try:
                    posts = self.rescan_subscription(sub)
                except NgaAuthError as exc:
                    self.logger.warning("重新扫描时认证失败: %s", exc)
                    break
                except NgaError as exc:
                    self.logger.warning("订阅 %s 重新扫描失败: %s", sub.id, exc)
                    continue
                self.notify_posts(sub, posts)

    def _poll_subscription_locked(self, sub: Subscription) -> list[NewPost]:
        """The actual fetch for one subscription (the lock is already held)."""
        # 同一个主题的订阅共用这一页：主题里最新的楼层都在上面。
        shared = self._topic.tail(sub.tid)
        quick = self._quick_new_floors(sub, shared)
        locations = self._locations_of(shared)
        last_page = 0
        if quick is None:
            # 这一页说明不了这个作者的进度：按作者过滤取一次（老路）。
            candidates, last_page = self._filtered_candidates(sub)
        else:
            candidates = quick
        candidates = self._walk_back_for_gap(sub, candidates, last_page)
        if not sub.initialized:
            candidates = self._extend_to_window(sub, candidates, last_page)
        if shared is not None:
            self._prefer_complete_content(candidates, shared.floors)

        title = self._titles.get(sub.tid, "") or sub.title
        author_name = self._learn_author_name(sub, candidates)
        new_floors = self._select_new(sub, candidates)
        since = self._window_start()
        eligible = [
            floor for floor in new_floors
            if (sub.initialized or since is not None) and self._in_window(floor, since)
        ]
        eligible_pids = {floor.pid for floor in eligible}
        quotes = self._resolve_quotes(sub.tid, eligible)
        missing = [floor for floor in eligible if floor.pid not in locations]
        locations.update(self._resolve_floors(sub.tid, missing))

        fresh = []
        for floor in new_floors:
            location = locations.get(floor.pid)
            floor_no = location.floor_no if location else 0
            floor_page = location.page if location else 0
            notified = floor.pid in eligible_pids
            quote = quotes.get(floor.pid, "")
            inserted = self.store.record_post(
                sub.id, floor, notified=notified, quote_text=quote,
                floor_no=floor_no, floor_page=floor_page,
            )
            if notified and (
                inserted or self.store.mark_post_notified(
                    sub.id, floor.pid, quote_text=quote, floor_no=floor_no, floor_page=floor_page,
                )
            ):
                fresh.append(NewPost(sub.id, sub.tid, title, floor, quote))

        self.store.update_subscription_meta(
            sub.id,
            title=title or None,
            author_name=author_name or None,
            last_pid=new_floors[-1].pid if new_floors else sub.last_pid,
            checked=True,
            initialized=True,
        )
        return fresh

    # -- 取数：同主题共享 -------------------------------------------------

    def _quick_new_floors(
        self, sub: Subscription, tail: ThreadPage | None
    ) -> list[Floor] | None:
        """只用共享的那一页，判断这个订阅有没有新回复。

        返回 ``None`` = 「这一页说明不了」，调用方要回退到按作者过滤取一次；
        返回 ``[]`` = 「确实没有新回复」。

        为什么这样算是够的：尾页上的 pid 是连续的——``low`` 是这一页最老的
        pid，``high`` 是最新的，而尾页装的正是整个主题里 pid 最大的那些楼层。
        设这个作者的高水位是 ``last_pid``：

        * ``last_pid >= high`` —— 他的高水位比主题里最新的楼层还新，
          他没发过新的；
        * ``last_pid >= low`` —— 主题里所有 pid 大于
          ``last_pid`` 的楼层都在这一页上（更早的页 pid 更小），所以新回复
          全在这儿，一个不漏。

        高水位早于尾页时，按作者过滤再取一次。
        """
        if not sub.initialized or tail is None or not tail.floors:
            return None
        pids = [f.pid for f in tail.floors if f.pid > 0]
        if not pids:
            return None
        if sub.last_pid < min(pids):
            # 他更早的那些新回复可能被挤到前一页去了：这一页说明不了。
            return None
        return [f for f in tail.floors if f.author_uid == sub.author_uid and f.pid > sub.last_pid]

    def _filtered_candidates(self, sub: Subscription) -> tuple[list[Floor], int]:
        """按作者过滤取数：作者自己的尾页。

        返回 ``(尾页上的楼层, 尾页页号)``。

        尾页页号跨轮次记着：那一页的响应里带着主题**当前**的总页数，所以
        只要它没变，尾页还是这一页——省掉一次「先取第 1 页问总页数」。

        注意这里只返回**尾页**的楼层：断层回退要看的是「尾页最老的楼层」，
        混进更早那页的楼层会让它误以为没有断层。
        """
        key = (sub.tid, sub.author_uid)
        hint = self._tail_hints.get(key)
        if hint:
            data = self._page(sub.tid, hint, sub.author_uid)
            last_page = max(data.total_page, 1)
            if last_page == hint:
                return list(data.floors), last_page
            # 作者又发帖了，尾页往后挪了：新尾页也要取回来。中间那几页上的
            # 新楼层交给断层回退去找。
            tail = self._page(sub.tid, last_page, sub.author_uid)
        else:
            first = self._page(sub.tid, 1, sub.author_uid)
            last_page = max(first.total_page, 1)
            if last_page <= 1:
                self._tail_hints[key] = 1
                return list(first.floors), last_page
            tail = self._page(sub.tid, last_page, sub.author_uid)
        self._tail_hints[key] = last_page
        if max(tail.total_page, 1) != last_page:
            self._tail_hints[key] = max(tail.total_page, 1)
            raise NgaRequestError("作者回复的总页数正在变化，下轮再检查")
        return list(tail.floors), last_page

    def _page(self, tid: int, page: int, author_id: int) -> ThreadPage:
        """取一页；这一轮里取过的不再取第二次。"""
        key = (tid, page, author_id)
        cached = self._pages.get(key)
        if isinstance(cached, NgaError):
            raise cached
        if cached is not None:
            return cached
        try:
            data = self.client.get_thread_page(tid, page, author_id=author_id)
        except NgaError as exc:
            self._pages[key] = exc
            raise
        self._pages[key] = data
        self._remember_title(tid, data.title)
        if author_id == 0:
            for floor in data.floors:
                if floor.pid > 0:
                    self._floors[(tid, floor.pid)] = floor
            if self.locator is not None:
                self.locator.remember(tid, data.page, data.floors, data.total_page)
        return data

    def _floor_by_pid(self, tid: int, pid: int) -> Floor | None:
        """取一层楼（取引用原文用）；这一轮里取过的不再取第二次。"""
        key = (tid, pid)
        if key in self._floors:
            return self._floors[key]
        try:
            floor = self.client.get_floor_by_pid(tid, pid)
        except NgaError:
            self._floors[key] = None
            raise
        self._floors[key] = floor
        return floor

    def _remember_title(self, tid: int, title: str) -> None:
        if title:
            self._titles[tid] = title

    @staticmethod
    def _locations_of(page: ThreadPage | None) -> dict[int, FloorLocation]:
        """不带过滤的页上，楼层号就是「真身」——不用再二分定位。"""
        if page is None:
            return {}
        return {
            f.pid: FloorLocation(floor_no=f.lou, page=page.page, pid=f.pid)
            for f in page.floors
            if f.pid > 0
        }

    @staticmethod
    def _prefer_complete_content(candidates: list[Floor], full: list[Floor]) -> None:
        """同一层楼，用内容更完整的那一份。

        NGA 带 ``authorid`` 过滤时会把图片地址**截断**：只剩一段相对路径
        （``./mon_202611/20/axyfQ47-f0s4Zh``），连扩展名都没有；不带过滤的
        响应里才是完整的地址。图片地址残缺，界面就渲染不出图——所以两份都
        拿到时，一律以不带过滤的那份为准。
        """
        better = {f.pid: f.content for f in full if f.pid > 0 and f.content}
        for floor in candidates:
            text = better.get(floor.pid)
            if text and text != floor.content:
                floor.content = text

    # -- 订阅时间 ---------------------------------------------------------

    def _window_start(self) -> int | None:
        """订阅时间对应的 Unix 秒；没设定就是 ``None``。"""
        stamp = self.store.get_view_since()
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return int(moment.timestamp())

    @staticmethod
    def _in_window(floor: Floor, since: int | None) -> bool:
        """这条楼层在订阅时间窗口里吗？

        ``since`` 是订阅时间对应的 Unix 秒，``None`` 表示没设窗口——那就没有
        什么要挡的，全都要。判断用的是**发帖时间**：断层回退会把昨天写的楼层
        今天记进账本，拿「什么时候记下的」当窗口，早于订阅时间的内容就会
        混进通知流。

        时间不明的楼层（``ts`` 为 0）一律放行：宁可多报一条，也不因为
        NGA 少给一个字段就把内容藏起来。
        """
        if since is None:
            return True
        if floor.timestamp <= 0:
            return True
        return floor.timestamp >= since

    def _extend_to_window(
        self, sub: Subscription, candidates: list[Floor], last_page: int
    ) -> list[Floor]:
        """把候选楼层往前扩到订阅时间为止。

        只在建立基线时用：用户要的是「订阅时间之后的回复」，而那些回复可能
        在更早的页上。往前翻到遇见比订阅时间还老的楼层为止。
        """
        since = self._window_start()
        if since is None or last_page <= 1:
            return candidates
        stamps = [f.timestamp for f in candidates if f.timestamp]
        if stamps and min(stamps) < since:
            return candidates  # 已经够到窗口边上了
        pages = 0
        for page in range(last_page - 1, 0, -1):
            if pages >= MAX_BACKFILL_PAGES:
                self.logger.info(
                    "订阅 %s 回填到第 %d 页为止（上限 %d 页）",
                    sub.id,
                    page,
                    MAX_BACKFILL_PAGES,
                )
                break
            data = self._page(sub.tid, page, sub.author_uid)
            pages += 1
            if not data.floors:
                continue
            candidates = list(data.floors) + candidates
            if any(f.timestamp and f.timestamp < since for f in data.floors):
                break
        return candidates

    def _resolve_quotes(self, tid: int, floors: list) -> dict[int, str]:
        """Resolve the original text behind each reply's ``[quote]`` block.

        The inline quote NGA puts in the body is usually enough, but fetching
        the quoted floor by pid gives the authoritative original (and the
        author it belongs to). Only one extra request per quoted reply, and
        only for replies we just discovered.
        """
        resolved: dict[int, str] = {}
        for floor in floors:
            quotes = extract_quotes(floor.content)
            if not quotes:
                continue
            parts: list[str] = []
            for quote in quotes[:2]:
                text = ""
                if quote.pid:
                    try:
                        original = self._floor_by_pid(tid, quote.pid)
                    except NgaError as exc:
                        self.logger.debug("取引用楼层失败 pid=%s: %s", quote.pid, exc)
                        original = None
                    if original is not None:
                        text = to_display_text(original.content)
                if not text:
                    text = to_display_text(quote.text)
                if not text:
                    continue
                who = f"{quote.author_name}：" if quote.author_name else ""
                parts.append(f"{who}{text}")
            if parts:
                resolved[floor.pid] = "\n".join(parts)
        return resolved

    def _resolve_floors(self, tid: int, floors: list) -> dict:
        """Work out where each new reply sits inside the thread.

        NGA renumbers ``lou`` when the request is filtered by ``authorid``, so
        the number the poll sees is "the n-th reply of this user" -- useless
        to show.  The locator bisects the thread's pages to find the real
        floor number and the page it lives on.  It costs a handful of extra
        requests, so it only runs for replies we just discovered, and a
        failure never breaks the poll: the reply is still collected, it just
        goes into the list without a floor number.
        """
        if not floors or self.locator is None:
            return {}
        try:
            locations = self.locator.locate_many(tid, [f.pid for f in floors])
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("解析楼层号失败 (tid=%s): %s", tid, exc)
            return {}
        missing = len(floors) - len(locations)
        if missing:
            self.logger.info(
                "订阅 %s：%d 条新回复没有定位到楼层号", tid, missing
            )
        return locations

    def _walk_back_for_gap(
        self, sub: Subscription, candidates: list, last_page: int
    ) -> list:
        """Extend ``candidates`` with earlier pages when a gap is detected."""
        if not sub.initialized or sub.last_pid <= 0 or last_page <= 1:
            return candidates

        pids = [f.pid for f in candidates if f.pid > 0]
        if not pids or min(pids) <= sub.last_pid:
            return candidates

        self.logger.info(
            "订阅 %s 检测到断层（尾页最小 pid=%d > 高水位 %d），向前回退扫描",
            sub.id,
            min(pids),
            sub.last_pid,
        )
        for page in range(last_page - 1, 0, -1):
            page_data = self._page(sub.tid, page, sub.author_uid)
            if not page_data.floors:
                continue
            candidates.extend(page_data.floors)
            if any(f.pid <= sub.last_pid for f in page_data.floors):
                break
        return candidates

    @staticmethod
    def _learn_author_name(sub: Subscription, candidates: list) -> str:
        """Pick up the author's nickname from the floors if we did not know it.

        Subscriptions are usually created with only a uid; the API always
        carries the nickname with the floor, so we learn it for free.
        """
        if sub.author_name:
            return sub.author_name
        for floor in candidates:
            if (
                floor.author_uid == sub.author_uid
                and floor.author_name
                and not floor.is_anonymous
            ):
                return floor.author_name
        return ""

    @staticmethod
    def _select_new(sub: Subscription, candidates: list[Floor]) -> list[Floor]:
        """Keep the floors that are genuinely new for this subscription."""
        selected: dict[int, Floor] = {}
        for floor in candidates:
            if floor.pid <= 0:
                # The opening post has no pid; it is never "new".
                continue
            if floor.author_uid != sub.author_uid or floor.is_anonymous:
                # Safety net: NGA filters server-side, but never trust that
                # blindly.
                continue
            if floor.pid <= sub.last_pid:
                continue
            selected.setdefault(floor.pid, floor)
        return sorted(selected.values(), key=lambda f: f.pid)

    # -- notification -----------------------------------------------------

    def notify_posts(self, sub: Subscription, posts: list[NewPost]) -> bool:
        """Raise one notification for a batch of new floors.

        The batch is summarised by its **newest** floor: when several replies
        arrive in one round, the one the user cares about is the last thing
        that was said.  The count still says how many there were.
        """
        if not posts:
            return False
        count = len(posts)
        latest = max(posts, key=lambda p: p.floor.pid)
        who = sub.display_name or latest.floor.author_name or f"UID {sub.author_uid}"
        thread_title = sub.title or latest.title or f"主题 {sub.tid}"

        if count == 1:
            title = f"{who} 在《{thread_title}》有新回复"
        else:
            title = f"{who} 在《{thread_title}》有 {count} 条新回复"

        # 桌面弹窗正文要短，界面里的正文要全：两套文案分开。
        toast_body = to_single_line(latest.floor.content, max_length=120)
        if count > 1:
            toast_body = f"{toast_body} 等 {count} 条" if toast_body else f"共 {count} 条"

        quote = latest.quote_text.strip()
        toast_quote = to_single_line(quote, max_length=120)
        if toast_quote:
            prefix = f"{toast_body}\n引用：" if toast_body else "引用："
            toast_body = f"{prefix}{toast_quote}"

        delivered = False
        try:
            # 点击通知直接打开本项目，而不是跳到 NGA。
            delivered = bool(self.notifier.notify(title, toast_body, url=self.app_url))
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("通知失败: %s", exc)

        # 落库的是完整正文，原始空格与换行都保留。
        full_body = to_display_text(latest.floor.content)
        if count > 1:
            suffix = f"本轮共 {count} 条新回复"
            full_body = f"{full_body}\n\n（{suffix}）" if full_body else suffix

        self.store.log_notification(
            sub.id, latest.floor.pid, title, full_body, delivered=delivered, quote_text=quote,
            collected_pids=[post.floor.pid for post in posts],
        )
        return delivered

    # -- rounds -----------------------------------------------------------

    def poll_all(self) -> PollReport:
        """Check every enabled subscription once."""
        report = PollReport(started_at=datetime.now(UTC))
        self.status.running = True
        self.status.auth_error = None

        try:
            with self._round():
                paused, reason = self.fetch_state()

                if paused:
                    # 没有可抓的订阅：一次请求都不发。
                    #
                    # 计时器照常走（外层循环按间隔继续），所以用户重新启用
                    # 订阅时不需要重置任何东西，下一轮按原计划发生即可。
                    self.status.paused = True
                    self.status.paused_reason = reason
                    self.logger.debug("本轮跳过：%s", reason)
                    return report

                self.status.paused = False
                self.status.paused_reason = None

                if not self.client.has_credentials:
                    message = "尚未配置 NGA Cookie，无法抓取"
                    self.status.auth_error = message
                    self.status.last_error = message
                    report.errors.append(message)
                    return report

                for sub in self.store.list_subscriptions(enabled_only=True):
                    report.checked += 1
                    try:
                        posts = self.poll_subscription(sub)
                    except NgaAuthError as exc:
                        self.status.auth_error = str(exc)
                        report.errors.append(f"订阅 {sub.id}: {exc}")
                        self.logger.warning("认证失败，停止本轮轮询: %s", exc)
                        break
                    except NgaThreadUnavailableError as exc:
                        report.errors.append(f"订阅 {sub.id}: {exc}")
                        self.logger.info("主题暂时不可读 (tid=%s): %s", sub.tid, exc)
                        self.store.update_subscription_meta(sub.id, checked=True)
                        continue
                    except NgaError as exc:
                        report.errors.append(f"订阅 {sub.id}: {exc}")
                        self.logger.warning("订阅 %s 抓取失败: %s", sub.id, exc)
                        continue

                    if not posts:
                        continue

                    report.new_posts += len(posts)
                    if self.notify_posts(sub, posts):
                        report.notifications += 1
        finally:
            self.status.running = False
            self.status.last_run_at = datetime.now(UTC)
            self.status.last_report = report.to_dict()
            self.status.last_error = report.errors[0] if report.errors else None

        return report


class WatcherService:
    """Runs :class:`Watcher` in the background on a (changeable) interval.

    The interval is read from a provider before every round, so the UI can
    change it -- or stop fetching altogether -- and have it take effect at
    once instead of at the end of the current sleep.
    """

    def __init__(
        self,
        watcher: Watcher,
        interval: int = 60,
        *,
        interval_provider=None,
    ) -> None:
        self.watcher = watcher
        self._interval = max(0, int(interval))
        self._provider = interval_provider or (lambda: self._interval)
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        #: 正在跑的那一轮抓取。并发触发时后来的调用直接等它，不另起一轮。
        self._inflight: asyncio.Task | None = None

    # -- interval ---------------------------------------------------------

    @property
    def interval(self) -> int:
        """The interval currently in force (``0`` means "do not fetch")."""
        try:
            return max(0, int(self._provider()))
        except (TypeError, ValueError):
            return self._interval

    def set_interval(self, seconds: int) -> int:
        """Change the interval and wake the loop so it applies immediately."""
        self._interval = max(0, int(seconds))
        self.wake()
        return self._interval

    def wake(self) -> None:
        """Interrupt the current sleep so the next round sees the new state.

        Used when the subscription time changes: freezing or unfreezing the
        watcher should not wait for the next tick.
        """
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def _seconds_until_resume(self) -> float | None:
        """Seconds left until the frozen-until moment (``None`` when not frozen)."""
        resume = self.watcher.store.pending_resume(datetime.now(UTC))
        if not resume:
            return None
        try:
            moment = datetime.fromisoformat(resume)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return max(0.0, (moment - datetime.now(UTC)).total_seconds())

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._loop = None
        # 等正在跑的那一轮抓取收尾——它跑在工作线程里，取消不了，
        # 总得等它写完数据库，别在库被关掉之后还去写。
        inflight = self._inflight
        if inflight is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(inflight)

    async def _wait_for_change(self, timeout: float | None) -> bool:
        """Sleep until the interval changes. ``True`` when woken up."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except TimeoutError:
            return False
        self._wake.clear()
        return True

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            interval = self.interval
            if interval <= 0:
                # 已暂停：不抓取，等界面把间隔改回来。
                self.watcher.logger.info("轮询已暂停，等待重新启用")
                await self._wait_for_change(None)
                continue

            try:
                await self.poll_now()
            except Exception as exc:  # pragma: no cover - defensive
                self.watcher.logger.exception("轮询循环异常: %s", exc)

            deadline = asyncio.get_running_loop().time() + self._wait_seconds(interval)
            while not self._stop.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                if not await self._wait_for_change(remaining):
                    break
                # 间隔被改了：立即按新的间隔重新计时。
                new_interval = self.interval
                if new_interval <= 0:
                    break
                deadline = asyncio.get_running_loop().time() + self._wait_seconds(new_interval)

    def _wait_seconds(self, interval: int) -> float:
        """How long to sleep between rounds.

        Normally a full interval.  While the watcher is frozen the sleep is
        cut short to the moment the freeze ends, so fetching resumes right
        then instead of one interval later.
        """
        until_resume = self._seconds_until_resume()
        if until_resume is None:
            return float(interval)
        return float(min(interval, until_resume))

    async def poll_now(self) -> PollReport:
        """Run one polling round now; concurrent callers share the same round.

        Several browser windows can ask for a check at the same moment.  Each
        of them must not turn into its own round against NGA -- the second
        caller simply joins the round that is already running and gets its
        result.
        """
        inflight = self._inflight
        if inflight is not None:
            return await asyncio.shield(inflight)

        task = asyncio.create_task(asyncio.to_thread(self.watcher.poll_all))
        self._inflight = task
        task.add_done_callback(self._clear_inflight)
        return await asyncio.shield(task)

    def _clear_inflight(self, task: asyncio.Task) -> None:
        if self._inflight is task:
            self._inflight = None
