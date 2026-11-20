"""Tests for the NGA client: parsing, error translation and reference parsing."""

from __future__ import annotations

import httpx
import pytest

from nga_reminder.nga.client import NgaClient, parse_thread_ref
from nga_reminder.nga.errors import (
    NgaApiError,
    NgaAuthError,
    NgaRequestError,
    NgaThreadUnavailableError,
)
from tests.conftest import RecordingTransport, load_fixture


def make_client(handler, **kwargs) -> tuple[NgaClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://bbs.nga.cn")
    client = NgaClient(http_client=http, uid="u", cid="c", **kwargs)
    return client, transport


# ---------------------------------------------------------------- references


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("46046493", (46046493, None)),
        (46046493, (46046493, None)),
        ("https://bbs.nga.cn/read.php?tid=46046493", (46046493, None)),
        (
            "https://bbs.nga.cn/read.php?tid=46046493&authorid=61765516",
            (46046493, 61765516),
        ),
        ("https://nga.178.com/read.php?tid=123", (123, None)),
    ],
)
def test_parse_thread_ref(value, expected):
    assert parse_thread_ref(value) == expected


@pytest.mark.parametrize("value", ["", "not-a-tid", "https://bbs.nga.cn/thread.php?fid=7"])
def test_parse_thread_ref_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_thread_ref(value)


# ------------------------------------------------------------------ parsing


def test_get_thread_page_parses_floors():
    payload = load_fixture("post_list_page1.json")
    client, _ = make_client(lambda req: httpx.Response(200, json=payload))

    page = client.get_thread_page(47920406, 1)

    assert page.title
    assert page.total_page == 3
    assert page.per_page == 20
    assert page.floors
    first = page.floors[0]
    assert first.author_uid > 0
    assert first.timestamp > 0
    assert first.content


def test_first_floor_is_the_op_and_has_no_pid():
    """The thread's opening post is returned with ``pid == 0``.

    Every reply has a real, monotonically increasing pid, so the watcher must
    ignore floors with a non-positive pid -- otherwise the opening post would
    look like a brand new post on every poll.
    """
    payload = load_fixture("post_list_page1.json")
    client, _ = make_client(lambda req: httpx.Response(200, json=payload))

    page = client.get_thread_page(47920406, 1)

    assert page.floors[0].pid == 0
    assert page.floors[0].lou == 0
    assert all(f.pid > 0 for f in page.floors[1:])
    pids = [f.pid for f in page.floors[1:]]
    assert pids == sorted(pids)


def test_authorid_filtering_renumbers_lou():
    """With ``authorid`` NGA renumbers ``lou`` within the filtered result.

    The real thread floor number is therefore *not* available from a filtered
    response -- ``pid`` is the only stable identity. Documented in CONTEXT.md.
    """
    unfiltered = load_fixture("post_list_page1.json")
    filtered = load_fixture("post_list_authorid.json")

    by_pid = {f["pid"]: f for f in unfiltered["result"]}
    matched = [(f, by_pid[f["pid"]]) for f in filtered["result"] if f["pid"] in by_pid]

    assert matched, "fixtures should share at least one pid"
    for filtered_floor, real_floor in matched:
        assert filtered_floor["lou"] != real_floor["lou"] or filtered_floor["pid"] == 0
        assert filtered_floor["pid"] == real_floor["pid"]


def test_get_thread_page_with_author_id_sends_the_param():
    payload = load_fixture("post_list_authorid.json")
    client, transport = make_client(lambda req: httpx.Response(200, json=payload))

    page = client.get_thread_page(47920406, 1, author_id=67406189)

    body = transport.requests[0].content.decode()
    assert "authorid=67406189" in body
    assert "tid=47920406" in body
    # every returned floor belongs to the requested author
    assert {f.author_uid for f in page.floors} == {67406189}


def test_get_thread_page_sends_credentials():
    payload = load_fixture("post_list_page1.json")
    client, transport = make_client(lambda req: httpx.Response(200, json=payload))

    client.get_thread_page(47920406, 1)

    cookie = transport.requests[0].headers.get("Cookie")
    assert cookie == "ngaPassportUid=u; ngaPassportCid=c"


# ------------------------------------------------------------------- errors


def test_code_12_raises_auth_error():
    payload = load_fixture("post_list_error_12_anonymous.json")
    client, _ = make_client(lambda req: httpx.Response(200, json=payload))

    with pytest.raises(NgaAuthError):
        client.get_thread_page(47920406, 1)


def test_code_42_raises_thread_unavailable():
    payload = load_fixture("post_list_error_42.json")
    client, _ = make_client(lambda req: httpx.Response(200, json=payload))

    with pytest.raises(NgaThreadUnavailableError):
        client.get_thread_page(46046493, 1)


def test_code_151_returns_empty_page():
    payload = load_fixture("post_list_authorid_empty.json")
    client, _ = make_client(lambda req: httpx.Response(200, json=payload))

    page = client.get_thread_page(47920406, 1, author_id=99999999)

    assert page.floors == []


def test_http_403_raises_auth_error():
    client, _ = make_client(lambda req: httpx.Response(403, text="nope"))

    with pytest.raises(NgaAuthError):
        client.get_thread_page(47920406, 1)


def test_nuke_error_shape_is_translated():
    client, _ = make_client(
        lambda req: httpx.Response(200, json={"error": {"0": "ACTION NOT FOUND"}})
    )

    with pytest.raises(NgaApiError, match="ACTION NOT FOUND"):
        client.get_user_profile(424242)


def test_non_json_response_raises_request_error():
    client, _ = make_client(lambda req: httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(NgaRequestError):
        client.get_thread_page(47920406, 1)


def test_transport_errors_are_retried_then_raise():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    client, _ = make_client(handler, max_retries=2, sleeper=lambda _s: None)

    with pytest.raises(NgaRequestError):
        client.get_thread_page(47920406, 1)
    assert attempts["n"] == 2


def test_rate_limit_status_is_retried():
    attempts = {"n": 0}
    payload = load_fixture("post_list_page1.json")

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=payload)

    client, _ = make_client(handler, max_retries=3, sleeper=lambda _s: None)

    page = client.get_thread_page(47920406, 1)
    assert page.floors
    assert attempts["n"] == 3


# ------------------------------------------------------------------ helpers


def test_get_user_profile_extracts_the_record():
    raw = {
        "data": {
            "0": {
                "uid": 424242,
                "username": "someone",
                "group": "学生",
            }
        }
    }
    client, _ = make_client(lambda req: httpx.Response(200, json=raw))

    profile = client.get_user_profile(424242)

    assert profile["uid"] == 424242
    assert profile["username"] == "someone"


def test_get_forum_topics_extracts_tids():
    raw = {"code": 0, "result": {"data": [{"tid": 1}, {"tid": 2}, {"nope": 3}]}}
    client, _ = make_client(lambda req: httpx.Response(200, json=raw))

    assert client.get_forum_topics(-7, 1) == [1, 2]
