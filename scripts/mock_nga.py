"""Local NGA and notification stand-ins for the Docker sandbox."""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

PORT = 8765
PAGE_SIZE = 20
COOKIE = "ngaPassportUid=10001; ngaPassportCid=sandbox"


class Handler(BaseHTTPRequestHandler):
    replies: dict[int, list[dict]] = {}

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "unknown endpoint"}, 404)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if url.path == "/notify":
            print("[sandbox notification]", raw.decode(), flush=True)
            self.send_json({"ok": True})
            return
        if url.path == "/replies":
            reply = json.loads(raw)
            thread = self.replies.setdefault(reply["tid"], [])
            floor = {
                "pid": time.time_ns() // 10_000,
                "lou": len(thread) + 1,
                "author": {"uid": reply["uid"], "username": f"模拟用户 {reply['uid']}"},
                "postdatetimestamp": int(time.time()),
                "content": reply["content"],
            }
            thread.append(floor)
            self.send_json(floor, 201)
            return
        if url.path != "/app_api.php":
            self.send_json({"error": "unknown endpoint"}, 404)
            return
        if self.headers.get("Cookie") != COOKIE:
            self.send_json({"code": 12, "msg": "请使用沙盒账号"})
            return

        query = parse_qs(url.query)
        form = {key: int(value[0]) for key, value in parse_qs(raw.decode()).items()}
        if query.get("__lib") == ["user"]:
            self.send_json({"code": 0, "data": {"0": {"uid": 10001, "username": "沙盒账号"}}})
        else:
            self.send_json(self.thread_page(form))

    def thread_page(self, form: dict[str, int]) -> dict:
        tid = form["tid"]
        author = form.get("authorid")
        pid = form.get("pid")
        page = form.get("page", 1)
        floors = self.replies.get(tid, [])
        if pid:
            floors = [floor for floor in floors if floor["pid"] == pid]
        if author:
            floors = [floor for floor in floors if floor["author"]["uid"] == author]
            floors = [dict(floor, lou=index) for index, floor in enumerate(floors, 1)]
        start = (page - 1) * PAGE_SIZE
        return {
            "code": 0,
            "tid": tid,
            "tsubject": f"模拟主题 {tid}",
            "currentPage": page,
            "totalPage": max(1, (len(floors) + PAGE_SIZE - 1) // PAGE_SIZE),
            "result": floors[start : start + PAGE_SIZE],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    reply = commands.add_parser("reply", help="发一条模拟回复")
    reply.add_argument("tid", type=int)
    reply.add_argument("uid", type=int)
    reply.add_argument("content")
    args = parser.parse_args()
    if args.command == "reply":
        data = json.dumps({"tid": args.tid, "uid": args.uid, "content": args.content})
        with urlopen(Request(f"http://127.0.0.1:{PORT}/replies", data=data.encode())) as response:
            print(response.read().decode())
    else:
        with HTTPServer(("0.0.0.0", PORT), Handler) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
