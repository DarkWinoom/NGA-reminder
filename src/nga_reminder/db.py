"""SQLite persistence.

One file holds everything: subscriptions, the floors we have already seen,
the notification history and the NGA credentials.

Every public method opens its own short-lived connection, which keeps the
store safe to use from both the request thread and the polling thread without
extra locking. WAL mode makes the concurrent read/write mix cheap.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Floor, Subscription

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tid             INTEGER NOT NULL,
    author_uid      INTEGER NOT NULL,
    author_name     TEXT    NOT NULL DEFAULT '',
    title           TEXT    NOT NULL DEFAULT '',
    alias           TEXT    NOT NULL DEFAULT '',
    color           TEXT    NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_pid        INTEGER NOT NULL DEFAULT 0,
    initialized     INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (tid, author_uid)
);

CREATE TABLE IF NOT EXISTS seen_post (
    subscription_id INTEGER NOT NULL,
    pid             INTEGER NOT NULL,
    lou             INTEGER NOT NULL DEFAULT 0,
    ts              INTEGER NOT NULL DEFAULT 0,
    content         TEXT    NOT NULL DEFAULT '',
    quote_text      TEXT    NOT NULL DEFAULT '',
    notified        INTEGER NOT NULL DEFAULT 0,
    collected       INTEGER NOT NULL DEFAULT 0,
    read_at         TEXT,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (subscription_id, pid)
);

CREATE TABLE IF NOT EXISTS notification_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    pid             INTEGER NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    summary         TEXT    NOT NULL DEFAULT '',
    quote_text      TEXT    NOT NULL DEFAULT '',
    delivered       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_seen_sub ON seen_post (subscription_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notification_log (created_at DESC);
"""

#: Columns added after the first release; applied to existing databases.
MIGRATIONS: dict[str, dict[str, str]] = {
    "subscription": {
        "alias": "TEXT NOT NULL DEFAULT ''",
        "color": "TEXT NOT NULL DEFAULT ''",
    },
    "seen_post": {
        "quote_text": "TEXT NOT NULL DEFAULT ''",
        "delivered": "INTEGER NOT NULL DEFAULT 0",
        "collected": "INTEGER NOT NULL DEFAULT 0",
        # 真实楼层号与所在页。NGA 带 authorid 过滤时会重编 lou，
        # 这里存的是「不带过滤」时拿到的真身，见 nga/floors.py。
        "floor_no": "INTEGER NOT NULL DEFAULT 0",
        "floor_page": "INTEGER NOT NULL DEFAULT 0",
    },
    "notification_log": {
        "quote_text": "TEXT NOT NULL DEFAULT ''",
    },
}

#: kv keys
VIEW_SINCE_KEY = "view_since"
POLL_INTERVAL_KEY = "poll_interval"
#: 用户手动设过订阅时间之后，启动就不再重置它
VIEW_SINCE_PINNED_KEY = "view_since_pinned"
#: 订阅时间被设到未来时，抓取冻结到那一刻；到点自动恢复
RESUME_AT_KEY = "resume_at"


def _now() -> str:
    """The current time as a sortable ISO-8601 string.

    The precision matters: the visible window is compared against these
    timestamps, so truncating to whole seconds would let floors recorded in the
    very same second as a start-up or a "clear" slip into the list -- exactly
    the history the window is supposed to hide.
    """
    return datetime.now(UTC).isoformat()


def _since_epoch(since: str | None) -> int | None:
    """The subscription time as a Unix timestamp (``None`` = no window).

    The window is about the **posting** time, not about the moment we happened
    to write the floor down.  Those are far apart: a gap back-fill writes
    yesterday's floors into the ledger today, so comparing against the write
    time would let replies from before the subscription time into the list --
    exactly the history the window is meant to hide.
    """
    if not since:
        return None
    try:
        moment = datetime.fromisoformat(since)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return int(moment.timestamp())


def _parse_cursor(value: str) -> tuple[str, int]:
    """Split a feed cursor into ``(created_at, pid)``.

    The cursor is ``"<收录时间>|<pid>"``.  A broken cursor is ignored rather
    than raising: a malformed query string should not take the feed down.

    The timestamp ends with ``+00:00`` and a ``+`` in a query string decodes
    to a space -- so a client that did not encode it hands us
    ``"… 00:00"``.  Put the ``+`` back, otherwise the comparison against the
    stored timestamps would silently match nothing.
    """
    stamp, _, raw_pid = str(value).partition("|")
    stamp = stamp.strip()
    if " " in stamp:
        head, _, tail = stamp.rpartition(" ")
        if tail and tail[0].isdigit():
            stamp = f"{head}+{tail}"
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        pid = 0
    return stamp, pid


class Store:
    """All persistence for the application."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            self._memory_conn = self._new_connection()
        self.init_schema()

    # -- plumbing ---------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        return self._new_connection()

    def close(self) -> None:
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    def init_schema(self) -> None:
        conn = self._connect()
        with conn:
            conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that were introduced after the database was created."""
        conn = self._connect()
        for table, columns in MIGRATIONS.items():
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns.items():
                if name not in existing:
                    with conn:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # -- subscriptions ----------------------------------------------------

    def list_subscriptions(self, *, enabled_only: bool = False) -> list[Subscription]:
        sql = "SELECT * FROM subscription"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        conn = self._connect()
        rows = conn.execute(sql).fetchall()
        return [self._row_to_subscription(r) for r in rows]

    def get_subscription(self, sub_id: int) -> Subscription | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM subscription WHERE id = ?", (sub_id,)).fetchone()
        return self._row_to_subscription(row) if row else None

    def initialized(self, sub_id: int) -> bool:
        """Whether the subscription has already been baselined."""
        sub = self.get_subscription(sub_id)
        return bool(sub and sub.initialized)

    def upsert_subscription(
        self,
        tid: int,
        author_uid: int,
        *,
        author_name: str = "",
        title: str = "",
        alias: str = "",
        color: str = "",
    ) -> Subscription:
        """Create a subscription, or refresh the metadata of an existing one.

        The same ``(tid, author_uid)`` pair is never duplicated: adding it a
        second time updates the cached title/name instead of raising.
        """
        conn = self._connect()
        with conn:
            conn.execute(
                """
                INSERT INTO subscription
                    (tid, author_uid, author_name, title, alias, color, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tid, author_uid) DO UPDATE SET
                    author_name = CASE WHEN excluded.author_name != ''
                                       THEN excluded.author_name
                                       ELSE subscription.author_name END,
                    title = CASE WHEN excluded.title != ''
                                 THEN excluded.title
                                 ELSE subscription.title END,
                    alias = CASE WHEN excluded.alias != ''
                                 THEN excluded.alias
                                 ELSE subscription.alias END,
                    color = CASE WHEN excluded.color != ''
                                 THEN excluded.color
                                 ELSE subscription.color END
                """,
                (tid, author_uid, author_name, title, alias, color, _now()),
            )
        row = conn.execute(
            "SELECT * FROM subscription WHERE tid = ? AND author_uid = ?", (tid, author_uid)
        ).fetchone()
        return self._row_to_subscription(row)

    def delete_subscription(self, sub_id: int) -> bool:
        conn = self._connect()
        with conn:
            cur = conn.execute("DELETE FROM subscription WHERE id = ?", (sub_id,))
            conn.execute("DELETE FROM seen_post WHERE subscription_id = ?", (sub_id,))
            conn.execute("DELETE FROM notification_log WHERE subscription_id = ?", (sub_id,))
        return cur.rowcount > 0

    def set_enabled(self, sub_id: int, enabled: bool) -> bool:
        conn = self._connect()
        with conn:
            cur = conn.execute(
                "UPDATE subscription SET enabled = ? WHERE id = ?", (1 if enabled else 0, sub_id)
            )
        return cur.rowcount > 0

    def update_subscription_meta(
        self,
        sub_id: int,
        *,
        title: str | None = None,
        author_name: str | None = None,
        alias: str | None = None,
        color: str | None = None,
        last_pid: int | None = None,
        checked: bool = False,
        initialized: bool | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if title is not None and title:
            sets.append("title = ?")
            args.append(title)
        if author_name is not None and author_name:
            sets.append("author_name = ?")
            args.append(author_name)
        if alias is not None:
            sets.append("alias = ?")
            args.append(alias)
        if color is not None:
            sets.append("color = ?")
            args.append(color)
        if last_pid is not None:
            sets.append("last_pid = ?")
            args.append(last_pid)
        if checked:
            sets.append("last_checked_at = ?")
            args.append(_now())
        if initialized is not None:
            sets.append("initialized = ?")
            args.append(1 if initialized else 0)
        if not sets:
            return
        args.append(sub_id)
        conn = self._connect()
        with conn:
            conn.execute(f"UPDATE subscription SET {', '.join(sets)} WHERE id = ?", args)

    def mark_initialized(self, sub_id: int, last_pid: int) -> None:
        """Mark the subscription as baselined and remember the high-water mark."""
        self.update_subscription_meta(sub_id, last_pid=last_pid, initialized=True)

    def reset_subscription_baseline(self, sub_id: int) -> None:
        """Forget the baseline: the next fetch cuts the backlog again.

        The subscription time is "from when do I care" -- when it moves
        backwards the floors that were swallowed by the old baseline have to
        become reportable again.  Clearing the high-water mark makes the next
        fetch treat the thread as if the subscription were brand new.
        """
        conn = self._connect()
        with conn:
            conn.execute(
                "UPDATE subscription SET initialized = 0, last_pid = 0 WHERE id = ?",
                (sub_id,),
            )

    def mark_post_notified(
        self, subscription_id: int, pid: int, *, quote_text: str, floor_no: int, floor_page: int
    ) -> int:
        """Flag an already recorded floor as reported.

        Returns how many rows changed: ``0`` means it had been reported
        before, so it must not be notified a second time.
        """
        conn = self._connect()
        with conn:
            cur = conn.execute(
                "UPDATE seen_post SET notified = 1, "
                "quote_text = COALESCE(NULLIF(?, ''), quote_text), "
                "floor_no = COALESCE(NULLIF(?, 0), floor_no), "
                "floor_page = COALESCE(NULLIF(?, 0), floor_page) "
                "WHERE subscription_id = ? AND pid = ? AND notified = 0",
                (quote_text, floor_no, floor_page, subscription_id, pid),
            )
            return cur.rowcount

    # -- posts ------------------------------------------------------------

    def has_seen(self, subscription_id: int, pid: int) -> bool:
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM seen_post WHERE subscription_id = ? AND pid = ?",
            (subscription_id, pid),
        ).fetchone()
        return row is not None

    def record_post(
        self,
        subscription_id: int,
        floor: Floor,
        *,
        notified: bool = False,
        quote_text: str = "",
        floor_no: int = 0,
        floor_page: int = 0,
    ) -> bool:
        """Record a floor. Returns ``False`` if it was already known.

        ``floor_no`` / ``floor_page`` are the reply's real position inside the
        thread (the ``#1234`` NGA shows and the page it lives on).  They stay
        ``0`` when the position could not be resolved -- see
        :mod:`nga_reminder.nga.floors`.
        """
        conn = self._connect()
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO seen_post
                    (subscription_id, pid, lou, floor_no, floor_page, ts, content,
                     quote_text, notified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    floor.pid,
                    floor.lou,
                    floor_no,
                    floor_page,
                    floor.timestamp,
                    floor.content,
                    quote_text,
                    1 if notified else 0,
                    _now(),
                ),
            )
        return cur.rowcount > 0

    def list_posts(
        self, subscription_id: int, limit: int = 50, *, since: str | None = None
    ) -> list[dict]:
        """List the floors recorded for a subscription, newest first.

        ``since`` hides everything posted before it -- that is how "the list
        only holds what arrived since startup / since the last clear" is
        implemented without losing the de-duplication ledger.  When it is not
        given the current view window is used, so a caller can never leak old
        floors by forgetting the argument.
        """
        since = self.get_view_since() if since is None else since
        bound = _since_epoch(since)
        sql = """
            SELECT pid, lou, floor_no, floor_page, ts, content, quote_text,
                   notified, delivered, read_at, created_at
            FROM seen_post WHERE subscription_id = ?
        """
        args: list[Any] = [subscription_id]
        if bound is not None:
            sql += " AND ts >= ?"
            args.append(bound)
        sql += " ORDER BY ts DESC, pid DESC LIMIT ?"
        args.append(limit)
        conn = self._connect()
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def list_feed(
        self,
        limit: int = 200,
        *,
        since: str | None = None,
        after: str = "",
        before: str = "",
        subscription_id: int | None = None,
    ) -> list[dict]:
        """The notification feed: one entry per collected floor.

        Rows carry the subscription metadata so the UI can render author,
        thread title and colour without a second round trip.  Only what has
        been *collected* -- see :meth:`collect_since` -- and falls inside the
        current window is returned, newest first so the freshest reply sits at
        the top of the list.

        ``after`` / ``before`` are cursors ("<收录时间>|<pid>") that drive the
        endless scroll: the UI asks for what is newer than the last item it
        has, or for what is older than the oldest one it has.  A cursor is
        used instead of an offset because new replies keep pushing the list
        around -- with an offset the page boundaries would drift.
        """
        since = self.get_view_since() if since is None else since
        sql = """
            SELECT p.pid, p.subscription_id, p.lou, p.floor_no, p.floor_page,
                   p.ts, p.content, p.quote_text,
                   p.delivered, p.created_at,
                   s.tid, s.author_uid, s.author_name, s.title, s.alias, s.color
            FROM seen_post p
            LEFT JOIN subscription s ON s.id = p.subscription_id
            WHERE p.notified = 1 AND p.collected = 1
        """
        args: list[Any] = []
        bound = _since_epoch(since)
        if bound is not None:
            sql += " AND p.ts >= ?"
            args.append(bound)
        if subscription_id is not None:
            sql += " AND p.subscription_id = ?"
            args.append(subscription_id)
        if after:
            stamp, pid = _parse_cursor(after)
            if stamp:
                sql += " AND (p.created_at > ? OR (p.created_at = ? AND p.pid > ?))"
                args.extend([stamp, stamp, pid])
        if before:
            stamp, pid = _parse_cursor(before)
            if stamp:
                sql += " AND (p.created_at < ? OR (p.created_at = ? AND p.pid < ?))"
                args.extend([stamp, stamp, pid])
        sql += " ORDER BY p.created_at DESC, p.pid DESC LIMIT ?"
        args.append(limit)
        conn = self._connect()
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def set_post_floor(
        self, subscription_id: int, pid: int, floor_no: int, floor_page: int
    ) -> int:
        """Write a reply's real position back into the ledger.

        ``floor_no`` is ``-1`` when the position was looked for and could not
        be found -- that marks the attempt so it is not repeated forever.
        """
        conn = self._connect()
        with conn:
            cur = conn.execute(
                """
                UPDATE seen_post SET floor_no = ?, floor_page = ?
                WHERE subscription_id = ? AND pid = ?
                """,
                (floor_no, floor_page, subscription_id, pid),
            )
        return cur.rowcount

    def mark_posts_read(self, subscription_id: int) -> int:
        conn = self._connect()
        with conn:
            cur = conn.execute(
                "UPDATE seen_post SET read_at = ? WHERE subscription_id = ? AND read_at IS NULL",
                (_now(), subscription_id),
            )
        return cur.rowcount

    def unread_count(self, subscription_id: int, *, since: str | None = None) -> int:
        """How many collected floors of this subscription are still unread."""
        return self.unread_counts(since=since, subscription_id=subscription_id).get(
            subscription_id, 0
        )

    def unread_counts(
        self, *, since: str | None = None, subscription_id: int | None = None
    ) -> dict[int, int]:
        since = self.get_view_since() if since is None else since
        sql = """
            SELECT subscription_id, COUNT(*) AS n FROM seen_post
            WHERE notified = 1 AND collected = 1 AND read_at IS NULL
        """
        args: list[Any] = []
        if subscription_id is not None:
            sql += " AND subscription_id = ?"
            args.append(subscription_id)
        bound = _since_epoch(since)
        if bound is not None:
            sql += " AND ts >= ?"
            args.append(bound)
        conn = self._connect()
        rows = conn.execute(sql + " GROUP BY subscription_id", args).fetchall()
        return {row["subscription_id"]: row["n"] for row in rows}

    # -- view window -----------------------------------------------------

    def get_view_since(self) -> str:
        """Timestamp of the moment the visible feed starts."""
        return self.get_value(VIEW_SINCE_KEY) or ""

    def set_view_since(self, stamp: str) -> int:
        """Move the subscription time and collect whatever it now covers.

        Floors that were already alerted but never collected -- because they
        were outside the old window -- are pulled in here, oldest first.  Each
        floor is collected at most once, so moving the time back and forth
        never duplicates a reply.

        Setting the time by hand also *pins* it: from now on a restart keeps
        this moment instead of starting a fresh window.

        Returns how many floors were collected by this call.
        """
        self.set_value(VIEW_SINCE_KEY, stamp)
        self.set_value(VIEW_SINCE_PINNED_KEY, "1")
        self.sync_window_ledger()
        return self.collect_since(stamp)

    def is_view_since_pinned(self) -> bool:
        """Has the user picked a subscription time by hand?"""
        return self.get_value(VIEW_SINCE_PINNED_KEY) == "1"

    def get_resume_at(self) -> str:
        """The moment fetching is frozen until (empty when not frozen).

        A subscription time in the future means "do not start before then":
        the watcher stops sending requests and picks up again on its own once
        that moment has passed.
        """
        return self.get_value(RESUME_AT_KEY) or ""

    def set_resume_at(self, stamp: str | None) -> None:
        """Freeze fetching until ``stamp`` (pass ``None`` to unfreeze)."""
        if stamp:
            self.set_value(RESUME_AT_KEY, stamp)
        else:
            self.delete_value(RESUME_AT_KEY)

    def pending_resume(self, now: datetime) -> str | None:
        """The freeze deadline, if it is still ahead of ``now``.

        Once the moment has passed the freeze is over, so the record is
        dropped and ``None`` comes back -- fetching resumes by itself.
        """
        stamp = self.get_resume_at()
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.astimezone()
        if moment <= now:
            self.set_resume_at(None)
            return None
        return stamp

    def clear_view(self) -> str:
        """Empty the list and start collecting from now on.

        This is what the "清空" button does: the subscription time becomes the
        current moment and stays that way across restarts.
        """
        stamp = self.reset_view()
        self.set_value(VIEW_SINCE_PINNED_KEY, "1")
        return stamp

    def collect_since(self, since: str) -> int:
        """Collect every alerted floor posted at or after ``since``.

        This is the "收录" step: it is idempotent (the ``collected`` flag stops
        a floor from being collected twice) and it walks the floors in
        chronological order.

        The cut is made on the **posting** time -- a floor that was written
        into the ledger today but posted before the subscription time does not
        belong in the window.
        """
        bound = _since_epoch(since)
        if bound is None:
            return 0
        conn = self._connect()
        with conn:
            cur = conn.execute(
                """
                UPDATE seen_post SET collected = 1
                WHERE notified = 1 AND collected = 0 AND ts >= ?
                """,
                (bound,),
            )
            return cur.rowcount

    def sync_window_ledger(self) -> int:
        """Line the ledger up with the subscription time.

        Floors that were posted *before* the subscription time but were marked
        as "already alerted" are a leftover of the old bookkeeping: they were
        pulled in by a gap back-fill and reported even though they are older
        than the window the user asked for.  Marking them as not-alerted keeps
        the feed honest -- and if the user moves the subscription time back,
        those floors can still be reported (once) as they should be.
        """
        bound = _since_epoch(self.get_view_since())
        if bound is None:
            return 0
        conn = self._connect()
        with conn:
            cur = conn.execute(
                """
                UPDATE seen_post SET notified = 0, collected = 0
                WHERE notified = 1 AND collected = 0 AND delivered = 0 AND ts > 0 AND ts < ?
                """,
                (bound,),
            )
            return cur.rowcount

    def collect_posts(self, subscription_id: int, pids: list[int]) -> int:
        """Mark freshly alerted floors as collected (they enter the feed)."""
        if not pids:
            return 0
        placeholders = ",".join("?" for _ in pids)
        conn = self._connect()
        with conn:
            cur = conn.execute(
                f"""
                UPDATE seen_post SET collected = 1
                WHERE subscription_id = ? AND pid IN ({placeholders})
                """,
                (subscription_id, *pids),
            )
            return cur.rowcount

    def reset_view(self) -> str:
        """Start a fresh feed: forget what was shown, keep the ledger.

        The de-duplication ledger (``seen_post``) must survive, otherwise the
        next poll would re-alert on every floor we have already reported.
        """
        stamp = _now()
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM notification_log")
            conn.execute("UPDATE seen_post SET collected = 0")
        self.set_value(VIEW_SINCE_KEY, stamp)
        return stamp

    def wipe_all(self) -> str:
        """Erase everything and go back to a fresh install.

        Deletes the subscriptions, the de-duplication ledger, the notification
        log and every setting -- including the stored NGA cookie.  What is left
        is the state of a tool that has just been started for the first time:
        no account, no subscriptions, nothing collected.

        Returns the fresh window start (now), so the feed starts empty without
        waiting for a restart.
        """
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM seen_post")
            conn.execute("DELETE FROM notification_log")
            conn.execute("DELETE FROM subscription")
            conn.execute("DELETE FROM kv")
        return self.reset_view()

    # -- notifications ----------------------------------------------------

    def log_notification(
        self,
        subscription_id: int,
        pid: int,
        title: str,
        summary: str,
        *,
        delivered: bool = False,
        quote_text: str = "",
        collected_pids: list[int] | None = None,
    ) -> int:
        conn = self._connect()
        with conn:
            cur = conn.execute(
                """
                INSERT INTO notification_log
                    (subscription_id, pid, title, summary, quote_text, delivered, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subscription_id, pid, title, summary, quote_text, 1 if delivered else 0, _now()),
            )
            if collected_pids:
                placeholders = ",".join("?" for _ in collected_pids)
                conn.execute(
                    f"""
                    UPDATE seen_post SET collected = 1, delivered = MAX(delivered, ?)
                    WHERE subscription_id = ? AND pid IN ({placeholders})
                    """,
                    (int(delivered), subscription_id, *collected_pids),
                )
        return int(cur.lastrowid or 0)

    def list_notifications(self, limit: int = 50) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT n.*, s.tid, s.author_uid, s.author_name
            FROM notification_log n
            LEFT JOIN subscription s ON s.id = n.subscription_id
            ORDER BY n.created_at DESC, n.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- key/value (credentials) -----------------------------------------

    def get_value(self, key: str) -> str | None:
        conn = self._connect()
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_value(self, key: str) -> None:
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row) -> Subscription:
        def _dt(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None

        return Subscription(
            id=int(row["id"]),
            tid=int(row["tid"]),
            author_uid=int(row["author_uid"]),
            author_name=row["author_name"] or "",
            title=row["title"] or "",
            alias=row["alias"] or "",
            color=row["color"] or "",
            enabled=bool(row["enabled"]),
            last_pid=int(row["last_pid"]),
            initialized=bool(row["initialized"]),
            last_checked_at=_dt(row["last_checked_at"]),
            created_at=_dt(row["created_at"]),
        )

    def summary(self) -> dict:
        conn = self._connect()
        subs = conn.execute("SELECT COUNT(*) AS n FROM subscription").fetchone()["n"]
        posts = conn.execute("SELECT COUNT(*) AS n FROM seen_post").fetchone()["n"]
        bound = _since_epoch(self.get_view_since())
        sql = "SELECT COUNT(*) AS n FROM seen_post WHERE notified = 1 AND collected = 1"
        args: list[Any] = []
        if bound is not None:
            sql += " AND ts >= ?"
            args.append(bound)
        feed = conn.execute(sql, args).fetchone()["n"]
        return {"subscriptions": subs, "seen_posts": posts, "feed": feed}
