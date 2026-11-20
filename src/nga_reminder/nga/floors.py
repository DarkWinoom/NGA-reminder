"""Find where a reply sits inside its thread.

Why this module exists
----------------------
NGA renumbers ``lou`` when a request carries ``authorid``: the response of
``app_api.php?__lib=post&__act=list&authorid=<uid>`` numbers *that user's*
replies from 1 again.  So the number the polling code gets is "the n-th reply
of this user", not "floor #1234 of the thread" -- showing it would be a lie.

The unfiltered response carries the real number.  Measured against the live
API on 2026-11-18 (thread 47288722):

* page 1 answers with ``lou`` 0..19, page 76 with 1500..1519, page 5838 with
  116740..116741 -- so ``lou`` is global and the page is ``lou // 20 + 1``;
* NGA's own HTML uses the very same number in its element ids
  (``postcontainer1500``), i.e. the value we display is exactly the ``#1500``
  the reader sees on the site;
* querying by pid (``?pid=``) answers with ``lou: 0``, and no other endpoint
  exists -- ``__act=locate`` / ``find`` / ``read`` all answer
  ``{"code": 2, "msg": "未定义服务"}``.

So the page has to be *found*, not fetched.  pids grow monotonically with the
floor number, therefore the page holding a pid can be bisected: halve the page
range until a probe's pid range brackets the pid.  Every probe also teaches us
the 20 floors of that page, so replies that live close together share probes.

The cost is one extra request per probe (a thread with 5800 pages needs about
13 of them).  That is why the resolver only runs for replies we just
discovered -- backfilling an old feed would mean hundreds of requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import Floor
from .client import NgaClient
from .errors import NgaError

logger = logging.getLogger("nga_reminder.floors")

#: 一次定位最多探测多少页——别让一个异常的主题把整轮轮询拖死。
MAX_PROBES = 24

#: 内存里最多留多少页的楼层缓存。
MAX_CACHED_PAGES = 400


@dataclass(frozen=True, slots=True)
class FloorLocation:
    """Where a reply sits inside its thread."""

    #: NGA 页面上的楼层号（``#1500``）。
    floor_no: int
    #: 所在页（``read.php?page=…``）。
    page: int
    pid: int


class FloorLocator:
    """Turns a reply's pid into the floor number NGA shows."""

    def __init__(self, client: NgaClient) -> None:
        self.client = client
        #: 主题的总页数。主题会长大，所以每次探测都顺手更新。
        self._total_pages: dict[int, int] = {}
        #: 探测过的页 -> 该页的楼层。
        self._pages: dict[tuple[int, int], list[Floor]] = {}
        #: 探测时见过的楼层 -> 位置。
        self._known: dict[tuple[int, int], FloorLocation] = {}
        #: 本轮**没找完**（探测次数用尽）的 pid。
        #:
        #: 「没找完」和「确实不存在」是两回事：前者只是这次没找着，下次还能试；
        #: 后者才值得永久放弃。所以单独记一份，调用方据此决定写 0 还是 -1。
        self.inconclusive: set[int] = set()

    # -- public -----------------------------------------------------------

    def remember(
        self, tid: int, page: int, floors: list[Floor], total_page: int
    ) -> None:
        """Put an already fetched page into the cache.

        The poller reads the thread's last page anyway -- that page carries
        the *real* floor numbers.  Handing it over here saves the locator a
        probe for the very same page, and it teaches the locator the thread's
        current page count for free.
        """
        self._remember(tid, page, list(floors), total_page)

    def locate_many(self, tid: int, pids: list[int]) -> dict[int, FloorLocation]:
        """Resolve several replies at once, sharing the probes between them."""
        targets = sorted({int(p) for p in pids if p and int(p) > 0})
        if not targets:
            return {}
        self.inconclusive = set()

        found: dict[int, FloorLocation] = {}
        pending: list[int] = []
        for pid in targets:
            hit = self._known.get((tid, pid))
            if hit is not None:
                found[pid] = hit
            else:
                pending.append(pid)
        if not pending:
            return found

        try:
            total = self._total_page(tid)
        except NgaError as exc:
            logger.info("拿不到主题 %s 的总页数，跳过楼层定位: %s", tid, exc)
            self.inconclusive.update(pending)
            return found

        for pid in pending:
            try:
                hit = self._bisect(tid, pid, total)
            except NgaError as exc:
                logger.info("定位 tid=%s pid=%s 的楼层失败: %s", tid, pid, exc)
                self.inconclusive.add(pid)
                continue
            if hit is not None:
                found[pid] = hit
        return found

    def locate(self, tid: int, pid: int) -> FloorLocation | None:
        """Resolve one reply; ``None`` when its position cannot be found."""
        return self.locate_many(tid, [pid]).get(pid)

    # -- internals --------------------------------------------------------

    def _total_page(self, tid: int) -> int:
        """How many pages the thread has.

        Any unfiltered answer carries the total, so the first probe teaches us
        -- and its 20 floors land in the cache for free.
        """
        cached = self._total_pages.get(tid)
        if cached:
            return cached
        data = self.client.get_thread_page(tid, 1)
        self._remember(tid, 1, data.floors, data.total_page)
        return self._total_pages.get(tid, 1)

    def _probe(self, tid: int, page: int) -> list[Floor]:
        """Fetch one unfiltered page (cached)."""
        key = (tid, page)
        cached = self._pages.get(key)
        if cached is not None:
            return cached
        data = self.client.get_thread_page(tid, page)
        floors = list(data.floors)
        self._remember(tid, page, floors, data.total_page)
        return floors

    def _remember(
        self, tid: int, page: int, floors: list[Floor], total_page: int
    ) -> None:
        if total_page:
            self._total_pages[tid] = total_page
        if len(self._pages) >= MAX_CACHED_PAGES:
            self._pages.clear()
            self._known.clear()
        self._pages[(tid, page)] = floors
        for floor in floors:
            if floor.pid > 0:
                self._known[(tid, floor.pid)] = FloorLocation(
                    floor_no=floor.lou, page=page, pid=floor.pid
                )

    def _bisect(self, tid: int, pid: int, total: int) -> FloorLocation | None:
        """Find the page that holds ``pid`` by halving the page range."""
        probes = 0

        # 先看最后一页：新回复常常就落在帖子末尾，命中只花两次请求。
        tail = self._probe(tid, total)
        probes += 1
        hit = self._match(tail, pid, total)
        if hit is not None:
            return hit

        # ⚠ 上面这一页的响应里带着**当前**的真实总页数。
        #
        #   发帖很勤的主题，页面是一直在长的：我们记着的那个数（几分钟前的）经常
        #   已经过期，而目标就在「新长出来的那几页」里。用旧数划范围的话，二分
        #   再准也够不着它——实测 5859 页的主题上，缓存记着 5850 时最新那条回复
        #   （在第 5857 页）就被判成「找不到」，而重算总页数就能找到。
        #
        #   所以这里跟着线程长出来的页数再探一次尾页，并用新总数划范围。
        grown = self._total_pages.get(tid, total)
        if grown > total:
            total = grown
            tail = self._probe(tid, total)
            probes += 1
            hit = self._match(tail, pid, total)
            if hit is not None:
                return hit

        lo, hi = 1, max(1, total - 1)
        while lo <= hi:
            if probes >= MAX_PROBES:
                # 只是这次没找完，不是「确实没有」——留给调用方决定要不要再试。
                logger.info("定位 tid=%s pid=%s 超过探测上限（%d 次）", tid, pid, probes)
                self.inconclusive.add(pid)
                return None
            mid = (lo + hi) // 2
            floors = self._probe(tid, mid)
            probes += 1
            if not floors:
                hi = mid - 1
                continue
            first = floors[0].pid
            last = floors[-1].pid
            if pid < first:
                hi = mid - 1
            elif pid > last:
                lo = mid + 1
            else:
                # 落在这一页的区间里，却没找到这一条——多半是楼层被删了。
                return self._match(floors, pid, mid)

        # 整个范围都翻过了，确实没有这一条。
        logger.info("定位 tid=%s pid=%s：整个主题里都没有这一条", tid, pid)
        return None

    @staticmethod
    def _match(floors: list[Floor], pid: int, page: int) -> FloorLocation | None:
        for floor in floors:
            if floor.pid == pid:
                return FloorLocation(floor_no=floor.lou, page=page, pid=pid)
        return None
