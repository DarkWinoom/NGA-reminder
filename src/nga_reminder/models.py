"""Domain models shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Floor:
    """One reply inside a thread.

    ``pid`` is the only thing used for identity -- NGA assigns it monotonically
    and it survives edits, whereas the floor number does not.
    """

    pid: int
    lou: int
    author_uid: int
    author_name: str
    timestamp: int
    content: str
    subject: str = ""
    is_anonymous: bool = False

    @property
    def posted_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "lou": self.lou,
            "author_uid": self.author_uid,
            "author_name": self.author_name,
            "timestamp": self.timestamp,
            "content": self.content,
            "subject": self.subject,
            "is_anonymous": self.is_anonymous,
        }


@dataclass(slots=True)
class ThreadPage:
    """One page of a thread, already normalised."""

    tid: int
    page: int
    total_page: int
    per_page: int
    floor_count: int
    title: str
    forum_name: str
    author_uid: int
    author_name: str
    floors: list[Floor] = field(default_factory=list)

    @property
    def first_pid(self) -> int | None:
        return min((f.pid for f in self.floors), default=None)

    @property
    def last_pid(self) -> int | None:
        return max((f.pid for f in self.floors), default=None)


@dataclass(slots=True)
class Subscription:
    """A tracked ``(thread, user)`` pair."""

    id: int
    tid: int
    author_uid: int
    author_name: str = ""
    title: str = ""
    alias: str = ""
    color: str = ""
    enabled: bool = True
    last_pid: int = 0
    initialized: bool = False
    last_checked_at: datetime | None = None
    created_at: datetime | None = None
    unread: int = 0

    @property
    def display_name(self) -> str:
        """What the user wants to see: their own label, or the real nickname."""
        return self.alias or self.author_name or f"UID {self.author_uid}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tid": self.tid,
            "author_uid": self.author_uid,
            "author_name": self.author_name,
            "alias": self.alias,
            "color": self.color,
            "display_name": self.display_name,
            "title": self.title,
            "enabled": self.enabled,
            "last_pid": self.last_pid,
            "initialized": self.initialized,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "unread": self.unread,
        }


@dataclass(slots=True)
class NewPost:
    """A newly discovered floor, together with the thread it belongs to."""

    subscription_id: int
    tid: int
    title: str
    floor: Floor
    quote_text: str = ""

    @property
    def url(self) -> str:
        return f"https://bbs.nga.cn/read.php?tid={self.tid}&pid={self.floor.pid}"
