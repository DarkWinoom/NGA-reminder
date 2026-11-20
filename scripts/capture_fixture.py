"""T0: capture real NGA responses as offline test fixtures, and map the code:42 pattern.

Usage:
    set NGA_UID / NGA_CID (or NGA_COOKIE) then:
    uv run python scripts/capture_fixture.py

Reads credentials from the environment only; never writes them to disk.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

POST_API = "/app_api.php?__lib=post&__act=list"
SUBJECT_API = "/app_api.php?__lib=subject&__act=list"
BASE = os.environ.get("NGA_BASE_URL", "https://bbs.nga.cn").rstrip("/")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "/",
    "User-Agent": os.environ.get(
        "NGA_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    ),
}


def cookies_from_env() -> dict[str, str]:
    jar: dict[str, str] = {}
    full = os.environ.get("NGA_COOKIE", "").strip()
    if full:
        for part in full.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                jar[k.strip()] = v.strip()
    for env_key, cookie_name in (
        ("NGA_UID", "ngaPassportUid"),
        ("NGA_CID", "ngaPassportCid"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            jar[cookie_name] = val
    return jar


def redact(node):
    """Replace usernames with stable placeholders; keep ids, types and content."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "username" and isinstance(v, str):
                out[k] = f"u{abs(hash(v)) % 100000}"
            else:
                out[k] = redact(v)
        return out
    if isinstance(node, list):
        return [redact(x) for x in node]
    return node


def save(name: str, payload) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def main() -> int:
    jar = cookies_from_env()
    if not jar.get("ngaPassportUid") or not jar.get("ngaPassportCid"):
        print("NGA_UID / NGA_CID (or NGA_COOKIE) are required", file=sys.stderr)
        return 2

    client = httpx.Client(
        base_url=BASE,
        headers=HEADERS,
        cookies=jar,
        timeout=30.0,
        follow_redirects=True,
    )

    print("discovering topics via subject lib...")
    subjects = client.post(SUBJECT_API, data={"fid": "-7", "page": "1"})
    subjects.raise_for_status()
    sj = subjects.json()
    save("subject_list_fid-7.json", sj)
    data = (sj.get("result") or {}).get("data") or []
    tids = [it["tid"] for it in data if isinstance(it, dict) and isinstance(it.get("tid"), int)]
    print(f"  tids: {tids[:5]}")

    if not tids:
        print("no topics discovered; aborting", file=sys.stderr)
        return 1

    tid = tids[0]
    print(f"\ncapturing post list for tid={tid} ...")
    p1 = client.post(POST_API, data={"tid": str(tid), "page": "1"})
    j1 = p1.json()
    if j1.get("code") != 0:
        print(f"  unexpected code={j1.get('code')} msg={j1.get('msg')!r}", file=sys.stderr)
        return 1
    save("post_list_page1.json", j1)
    print(f"  totalPage={j1.get('totalPage')} vrows={j1.get('vrows')} perPage={j1.get('perPage')}")

    if (j1.get("totalPage") or 1) > 1:
        time.sleep(0.5)
        p2 = client.post(POST_API, data={"tid": str(tid), "page": "2"})
        j2 = p2.json()
        if j2.get("code") == 0:
            save("post_list_page2.json", j2)

    # authorid filtering, using a uid that actually posted in this thread
    floors = j1.get("result") or []
    author_uids = [
        (f.get("author") or {}).get("uid") for f in floors if isinstance(f, dict)
    ]
    author_uids = [u for u in author_uids if isinstance(u, int)]
    if author_uids:
        target = author_uids[-1]
        time.sleep(0.5)
        pa = client.post(POST_API, data={"tid": str(tid), "page": "1", "authorid": str(target)})
        ja = pa.json()
        save("post_list_authorid.json", ja)
        uids = {(f.get("author") or {}).get("uid") for f in (ja.get("result") or [])}
        print(f"  authorid={target} -> code={ja.get('code')} totalPage={ja.get('totalPage')} "
              f"floors={len(ja.get('result') or [])} uids={uids}")

    # a uid with no posts in this thread -> the "no such author" response
    time.sleep(0.5)
    pm = client.post(POST_API, data={"tid": str(tid), "page": "1", "authorid": "99999999"})
    save("post_list_authorid_empty.json", pm.json())
    print(f"  authorid=absent -> code={pm.json().get('code')} msg={pm.json().get('msg')!r}")

    # the code:42 shape (older threads) and the code:12 shape (anonymous)
    time.sleep(0.5)
    p42 = client.post(POST_API, data={"tid": "46046493", "page": "1"})
    j42 = p42.json()
    if j42.get("code") != 0:
        save("post_list_error_42.json", j42)
        print(f"  tid=46046493 -> code={j42.get('code')} msg={j42.get('msg')!r}")

    anon = httpx.Client(base_url=BASE, headers=HEADERS, timeout=30.0)
    ja = anon.post(POST_API, data={"tid": str(tid), "page": "1"}).json()
    save("post_list_error_12_anonymous.json", ja)
    print(f"  anonymous -> code={ja.get('code')} msg={ja.get('msg')!r}")

    client.close()
    anon.close()
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
