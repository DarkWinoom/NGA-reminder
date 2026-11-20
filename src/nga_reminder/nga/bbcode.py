"""Turn NGA's BBCode-ish bodies into short plain-text summaries.

The API returns a mix of BBCode tags (``[b]``, ``[url=...]``, ``[img]``,
``[s:ac:2]`` smileys, ``[quote]`` blocks) and plain text depending on the
post. For a notification we only need a readable one-liner, so this module
strips markup rather than converting it faithfully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Tags whose content should be dropped entirely.
_DROP_CONTENT = re.compile(r"\[quote(?:=[^\]]*)?\].*?\[/quote\]", re.S | re.I)

#: A quote block, captured so the quoted text can be kept separately.
_QUOTE_BLOCK = re.compile(r"\[quote(?:=[^\]]*)?\](.*?)\[/quote\]", re.S | re.I)

#: The ``[pid=123,tid,page]`` marker inside a quote, plus its "Reply" label.
_QUOTE_MARKER = re.compile(r"\[pid=(\d+)(?:,\d+)*\]\s*(?:Reply|回复)?\s*", re.I)

#: The ``<b>Post by ...</b>`` boilerplate NGA puts in front of the quoted text.
_POST_BY = re.compile(r"<\s*b\s*>\s*Post by .*?<\s*/\s*b\s*>\s*", re.S | re.I)

#: Self-closing / paired tags we can simply unwrap.
_UNWRAP = re.compile(r"\[/?[a-z][a-z0-9]*(?:=[^\]]*)?\]", re.I)

#: NGA smiley placeholders, e.g. ``[s:ac:2]`` or ``[s:a2:goodjob]``.
_SMILEY = re.compile(r"\[s:[^\]]+\]", re.I)

#: Image placeholders, e.g. ``[img]`` / ``[img=100,80]``.
_IMAGE = re.compile(r"\[img[^\]]*\]", re.I)

#: Remaining ``[uid=123]`` / ``[pid=123]`` / ``[tid=123]`` links.
_LINK_TAG = re.compile(r"\[(uid|pid|tid)=(\d+)\]", re.I)

_URL = re.compile(r"\[url=([^\]]*)\](.*?)\[/url\]", re.S | re.I)

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(slots=True)
class Quote:
    """One quoted block inside a post."""

    pid: int | None
    text: str
    author_name: str = ""


def extract_quotes(content: str) -> list[Quote]:
    """Return the quoted blocks of a post, in order.

    NGA writes a quote like::

        [quote][pid=887927595,47920406,1]Reply[/pid]
        <b>Post by [uid=66485837]someone[/uid] (2026-11-15 08:01):</b><br/>
        the original text[/quote]

    The ``pid`` is what lets us fetch the original floor from the API.
    """
    if not content:
        return []

    quotes: list[Quote] = []
    for match in _QUOTE_BLOCK.finditer(content):
        inner = match.group(1)
        marker = _QUOTE_MARKER.search(inner)
        pid = int(marker.group(1)) if marker else None
        body = _QUOTE_MARKER.sub("", inner)
        # The author tag lives *inside* the "Post by" block, so read it first.
        author = ""
        author_match = re.search(r"\[uid=\d+\]([^\[\]]+)\[/uid\]", body)
        if author_match:
            author = author_match.group(1).strip()
        body = _POST_BY.sub("", body)
        if author_match:
            body = body.replace(author_match.group(0), "")
        quotes.append(Quote(pid=pid, text=to_text(body), author_name=author))
    return quotes


def to_text(content: str, *, max_length: int = 0) -> str:
    """Return a readable plain-text version of a post body.

    Quoted blocks are dropped -- use :func:`extract_quotes` when they matter.

    Args:
        content: the raw body as returned by the API.
        max_length: when > 0, truncate to that many characters (with an
            ellipsis) after collapsing whitespace.
    """
    if not content:
        return ""

    text = _DROP_CONTENT.sub(" ", content)
    text = _URL.sub(r"\2 (\1)", text)
    text = _IMAGE.sub("[图片]", text)
    text = _SMILEY.sub("", text)
    text = _LINK_TAG.sub(r"", text)
    text = _UNWRAP.sub("", text)

    # Order matters: <br> must become a newline before generic tags are
    # stripped, and entities must be decoded *after* tag stripping, otherwise
    # "&lt;c&gt;" would be eaten as if it were a tag.
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )

    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES.sub("\n\n", text).strip()

    if max_length > 0 and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def to_single_line(content: str, *, max_length: int = 80) -> str:
    """Return a one-line summary suitable for a toast body."""
    text = to_text(content).replace("\n", " ")
    text = _WHITESPACE.sub(" ", text).strip()
    if max_length > 0 and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def to_display_text(content: str) -> str:
    """Return the full readable body, keeping the author's spacing and breaks.

    Unlike :func:`to_text` this neither collapses runs of spaces nor drops
    line breaks, and it never truncates -- the notification list has to show
    the reply exactly as it was written.  Quote blocks are still removed (they
    are surfaced separately via :func:`extract_quotes`).
    """
    if not content:
        return ""

    text = _DROP_CONTENT.sub(" ", content)
    text = _URL.sub(r"\2 (\1)", text)
    text = _IMAGE.sub("[图片]", text)
    text = _SMILEY.sub("", text)
    text = _LINK_TAG.sub("", text)
    text = _UNWRAP.sub("", text)

    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )

    # 只去掉首尾空白，行内空格与换行原样保留。
    return text.strip()
