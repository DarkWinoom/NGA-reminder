"""Read-only client for NGA's JSON endpoints.

Only query endpoints are used. Nothing in this module can post, reply, vote or
change account state.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ..models import Floor, ThreadPage
from .errors import (
    NgaApiError,
    NgaAuthError,
    NgaRequestError,
    NgaThreadUnavailableError,
    RateLimitError,
)

POST_LIST_PATH = "/app_api.php?__lib=post&__act=list"
SUBJECT_LIST_PATH = "/app_api.php?__lib=subject&__act=list"
USER_GET_PATH = "/nuke.php?__lib=ucp&__act=get&__output=8"

#: Codes that mean "this thread cannot be read right now".
THREAD_UNAVAILABLE_CODES = {42}

_ANONYMOUS_RE = re.compile(r"^#anony_")

#: Prefixes some endpoints put in front of the JSON body.
_XSSI_PREFIXES = (")]}'", ")]}',", "while(1);", "for(;;);", "/*", "var ")


def _decode_json(response: httpx.Response) -> tuple[Any, str]:
    """Decode a NGA response body into JSON.

    NGA is not consistent about encodings: ``app_api.php`` answers in UTF-8 but
    ``nuke.php`` answers in **GBK** while still labelling the body as JSON.
    Decoding with the charset from the header is what makes the difference
    between "valid JSON" and a bogus "响应不是 JSON".

    Returns:
        The parsed value and the text it was parsed from (for error messages).
    """
    charset = response.charset_encoding or "utf-8"
    try:
        text = response.content.decode(charset, errors="replace")
    except LookupError:
        text = response.content.decode("utf-8", errors="replace")

    stripped = text.lstrip()
    for prefix in _XSSI_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].lstrip()
            break
    return json.loads(stripped), stripped


def post_url(tid: int | None, pid: int | None, page: int | None = None) -> str:
    """Link to the original page anchor, or use NGA's single-reply view."""
    if not tid or not pid:
        return ""
    if page:
        return f"https://bbs.nga.cn/read.php?tid={tid}&page={page}#pid{pid}Anchor"
    return f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"


def parse_thread_ref(value: str | int) -> tuple[int, int | None]:
    """Parse a thread reference into ``(tid, author_id)``.

    Accepts a bare tid (``"46046493"``), a full URL
    (``https://bbs.nga.cn/read.php?tid=46046493&authorid=123``), a bare query
    string (``"tid=46046493&authorid=123"``) or any URL carrying a ``tid``
    query parameter.

    The ``authorid`` parameter is what NGA's "只看楼主" link uses, so pasting
    that link is enough to fill in both halves of a subscription.

    Raises:
        ValueError: if no tid can be found.
    """
    if isinstance(value, int):
        return value, None

    text = str(value).strip()
    if not text:
        raise ValueError("主题引用为空")

    if text.isdigit():
        return int(text), None

    parsed = urlparse(text)
    query = parsed.query
    if not query and "=" in text and "://" not in text:
        # A bare "tid=123&authorid=456" query string.
        query = text.lstrip("?")
    if not query:
        raise ValueError(f"无法解析主题引用: {text}")

    params = parse_qs(query)
    tids = params.get("tid") or []
    if not tids:
        raise ValueError(f"链接中没有 tid 参数: {text}")
    author = params.get("authorid") or []
    return int(tids[0]), (int(author[0]) if author and author[0].isdigit() else None)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class NgaClient:
    """Thin, polite, read-only wrapper around NGA's JSON endpoints."""

    def __init__(
        self,
        *,
        base_url: str = "https://bbs.nga.cn",
        uid: str = "",
        cid: str = "",
        user_agent: str = "",
        timeout: float = 30.0,
        delay_ms: int = 330,
        max_retries: int = 3,
        http_client: httpx.Client | None = None,
        sleeper=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.uid = uid
        self.cid = cid
        self.delay_ms = max(0, int(delay_ms))
        self.max_retries = max(1, int(max_retries))
        self._sleep = sleeper
        self._clock = clock
        self._last_request_at = 0.0
        self._owns_client = http_client is None

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url + "/",
        }
        if user_agent:
            headers["User-Agent"] = user_agent

        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        self._client.headers.update(headers)

    # -- lifecycle --------------------------------------------------------

    def set_credentials(self, uid: str, cid: str) -> None:
        """Replace the stored credentials."""
        self.uid = uid
        self.cid = cid

    @property
    def has_credentials(self) -> bool:
        return bool(self.uid and self.cid)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NgaClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        """Wait between requests so NGA does not treat us as a flood.

        Mirrors the pacing used by the reference implementations: a base delay
        with gaussian jitter, plus an occasional longer pause.
        """
        if self.delay_ms <= 0:
            return
        wait = max(0.05, random.gauss(self.delay_ms / 1000.0, self.delay_ms / 5000.0))
        elapsed = self._clock() - self._last_request_at
        if elapsed < wait:
            self._sleep(wait - elapsed)
        if random.random() < 0.05:
            self._sleep(random.uniform(1.5, 3.5))
        self._last_request_at = self._clock()

    def _cookie_header(self) -> dict[str, str]:
        if not self.has_credentials:
            return {}
        return {"Cookie": f"ngaPassportUid={self.uid}; ngaPassportCid={self.cid}"}

    def _post(self, path: str, form: dict[str, Any]) -> dict[str, Any]:
        """POST a form and return the decoded JSON body."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self._client.post(path, data=form, headers=self._cookie_header())
            except httpx.HTTPError as exc:
                last_error = NgaRequestError(f"网络错误: {exc}")
                self._sleep(1.5 * (attempt + 1))
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RateLimitError(f"HTTP {response.status_code}")
                self._sleep(2.0 * (attempt + 1))
                continue

            if response.status_code in (401, 403):
                raise NgaAuthError(f"HTTP {response.status_code}")

            try:
                payload, text = _decode_json(response)
            except ValueError as exc:
                last_error = NgaRequestError(
                    f"响应无法解析为 JSON (HTTP {response.status_code}, "
                    f"charset={response.charset_encoding or '?'}): {exc} | "
                    f"内容开头: {response.text[:120]!r}"
                )
                self._sleep(1.0 * (attempt + 1))
                continue

            if isinstance(payload, dict):
                return payload
            last_error = NgaRequestError("响应 JSON 结构异常")
            self._sleep(1.0 * (attempt + 1))

        raise last_error or NgaRequestError("请求失败")

    @staticmethod
    def _raise_for_payload(payload: dict[str, Any]) -> None:
        """Translate a NGA error body into a typed exception.

        NGA uses two different error shapes: ``{"code": n, "msg": ...}`` for
        ``app_api.php`` and ``{"error": {"0": "..."}}`` for ``nuke.php``.
        """
        error = payload.get("error")
        if isinstance(error, dict) and error:
            message = str(next(iter(error.values())))
            raise NgaApiError(-1, message)

        code = _as_int(payload.get("code"), -1)
        if code < 0:
            return
        if code == 0:
            return
        message = str(payload.get("msg") or "")
        if code == 12:
            raise NgaAuthError(message)
        if code in THREAD_UNAVAILABLE_CODES:
            raise NgaThreadUnavailableError(code, message)
        raise NgaApiError(code, message)

    # -- endpoints --------------------------------------------------------

    def get_thread_page(self, tid: int, page: int = 1, author_id: int = 0) -> ThreadPage:
        """Fetch one page of a thread.

        When ``author_id`` is given, NGA filters server-side and only returns
        that author's floors -- verified against the live API.

        Raises:
            NgaAuthError: credentials are missing, rejected or expired.
            NgaThreadUnavailableError: the thread cannot be read right now.
            NgaApiError: any other non-zero ``code``.
        """
        form: dict[str, Any] = {"tid": str(tid), "page": str(page)}
        if author_id:
            form["authorid"] = str(author_id)

        payload = self._post(POST_LIST_PATH, form)

        code = _as_int(payload.get("code"), -1)
        if code == 151:
            # "no content for this author" / "no more pages" -- not an error.
            return self._parse_page(payload, tid, page, floors=[])
        self._raise_for_payload(payload)
        floors = self._parse_floors(payload.get("result"))
        return self._parse_page(payload, tid, page, floors=floors)

    def get_thread_title(self, tid: int) -> tuple[str, str]:
        """Return ``(title, forum_name)`` for a thread."""
        payload = self._post(POST_LIST_PATH, {"tid": str(tid), "page": "1"})
        self._raise_for_payload(payload)
        return str(payload.get("tsubject") or ""), str(payload.get("forum_name") or "")

    def get_floor_by_pid(self, tid: int, pid: int) -> Floor | None:
        """Fetch a single floor by its pid.

        Used to resolve the original text behind a ``[quote]`` block, so a
        notification can show what the reply was actually answering.
        """
        payload = self._post(POST_LIST_PATH, {"tid": str(tid), "pid": str(pid)})
        code = _as_int(payload.get("code"), -1)
        if code != 0:
            return None
        floors = self._parse_floors(payload.get("result"))
        return floors[0] if floors else None

    def get_user_profile(self, uid: int) -> dict[str, Any]:
        """Return the public profile of a user.

        This endpoint is readable anonymously, so it doubles as a cheap
        "does this uid exist" probe.
        """
        payload = self._post(USER_GET_PATH, {"uid": str(uid)})
        self._raise_for_payload(payload)
        data = payload.get("data")
        if isinstance(data, dict) and data:
            first = next(iter(data.values()))
            if isinstance(first, dict):
                return first
        return {}

    def get_forum_topics(self, fid: int, page: int = 1) -> list[int]:
        """Return the thread ids currently listed in a forum."""
        payload = self._post(SUBJECT_LIST_PATH, {"fid": str(fid), "page": str(page)})
        self._raise_for_payload(payload)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if not isinstance(data, list):
            return []
        return [item["tid"] for item in data if isinstance(item, dict) and "tid" in item]

    # -- parsing ----------------------------------------------------------

    @staticmethod
    def _parse_floors(raw: Any) -> list[Floor]:
        floors: list[Floor] = []
        if not isinstance(raw, list):
            return floors
        for item in raw:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            author = author if isinstance(author, dict) else {}
            username = str(author.get("username") or "")
            floors.append(
                Floor(
                    pid=_as_int(item.get("pid")),
                    lou=_as_int(item.get("lou")),
                    author_uid=_as_int(author.get("uid")),
                    author_name=username,
                    timestamp=_as_int(item.get("postdatetimestamp")),
                    content=str(item.get("content") or ""),
                    subject=str(item.get("subject") or ""),
                    is_anonymous=bool(_ANONYMOUS_RE.match(username)),
                )
            )
        return floors

    @staticmethod
    def _parse_page(
        payload: dict[str, Any], tid: int, page: int, floors: list[Floor]
    ) -> ThreadPage:
        return ThreadPage(
            tid=_as_int(payload.get("tid"), tid) or tid,
            page=_as_int(payload.get("currentPage"), page) or page,
            total_page=_as_int(payload.get("totalPage"), 1) or 1,
            per_page=_as_int(payload.get("perPage"), 20),
            floor_count=_as_int(payload.get("vrows")),
            title=str(payload.get("tsubject") or ""),
            forum_name=str(payload.get("forum_name") or ""),
            author_uid=_as_int(payload.get("tauthorid")),
            author_name=str(payload.get("tauthor") or ""),
            floors=floors,
        )
