"""Host-side notification bridge.

The Docker deployment runs the watcher inside a Linux container, where there is
no way to raise a desktop notification by itself -- so the container POSTs the
notification to a tiny bridge running on the **host**, and the bridge raises a
real system notification for it:

    POST /notify
    {"title": "...", "body": "...", "url": "http://127.0.0.1:8765/"}

Usage (on the host, from the project root)::

    uv run python scripts/notify_bridge.py

Then point the container at it -- in ``docker-compose.yml``::

    environment:
      NGA_NOTIFY_WEBHOOK: "http://host.docker.internal:8766/notify"
      NGA_NOTIFY_WEBHOOK_TOKEN: "<same token as the bridge>"

``host.docker.internal`` is the name Docker gives the host from inside a
container.  The bridge therefore has to listen on a non-loopback address
(``0.0.0.0`` by default); that also puts it on the LAN, so a shared token is
strongly recommended -- set ``NGA_BRIDGE_TOKEN`` on both sides.

The host platform decides how the notification is raised:

==========  ==========================================
Windows     WinRT toast, via ``windows-toasts``
macOS       ``osascript display notification``
Linux       ``notify-send`` (libnotify)
==========  ==========================================

The bridge is a foreground process: like the rest of this project it installs
no autostart, no scheduled task, no registry entry.  Start it when you want
toasts.

Two ways to run it::

    # 带窗口：看得见输出，出错立刻知道
    uv run python scripts/notify_bridge.py

    # 无窗口（开机自启用这个）：日志写文件
    uv run python scripts/notify_bridge.py --log-file data/notify-bridge.log

The second form is what the startup shortcut (``scripts/make_startup_shortcut.ps1``)
uses: it runs ``pythonw.exe``, which has neither stdout nor stderr, so without
``--log-file`` a crash would leave nothing behind.  The shortcut itself is
*not* installed by this project -- the user drags it into ``shell:startup``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nga_reminder.notify import LogNotifier, build_native_notifier  # noqa: E402
from nga_reminder.notify.base import Notifier  # noqa: E402

logger = logging.getLogger("nga_reminder.bridge")

TOKEN_HEADER = "X-NGA-Bridge-Token"

#: ``say()`` 用的独立 logger —— 用来把「已经打印过一遍」的记录和普通日志区分开。
SAY_LOGGER = "nga_reminder.bridge.say"


class BridgeHandler(BaseHTTPRequestHandler):
    """Accepts ``POST /notify`` and raises a real system notification for it."""

    server_version = "NGABridge/1.0"

    # 由 serve() 注入
    notifier: Notifier
    token: str = ""

    # -- helpers ---------------------------------------------------------

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not self.token:
            return True
        given = self.headers.get(TOKEN_HEADER, "")
        return secrets.compare_digest(given, self.token)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
        if self.path.rstrip("/") in ("", "/health"):
            self._reply(200, {"ok": True, "service": "nga-notify-bridge"})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
        if self.path.rstrip("/") != "/notify":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        if not self._authorised():
            logger.warning("拒绝了一个没有正确口令的请求")
            self._reply(401, {"ok": False, "error": "bad token"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(data, dict):
            self._reply(400, {"ok": False, "error": "bad json"})
            return

        title = str(data.get("title") or "")
        body = str(data.get("body") or "")
        url = str(data.get("url") or "")
        if not title and not body:
            self._reply(400, {"ok": False, "error": "empty notification"})
            return

        delivered = self.notifier.notify(title, body, url=url)
        logger.info("通知: %s | %s", title, body)
        if delivered:
            self._reply(200, {"ok": True})
        else:
            self._reply(502, {"ok": False, "error": self.notifier.error or "toast failed"})

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)


def _token_from_dotenv(path: Path, key: str = "NGA_BRIDGE_TOKEN") -> str:
    """Read a key from the project's ``.env`` (if there is one).

    The bridge must find the token no matter which directory it was started
    from, so the path is resolved against the project root rather than the
    current working directory.
    """
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def _http_get_json(url: str, timeout: float = 3.0) -> dict:
    """GET a JSON document; an unreachable host yields ``{}``."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def say(message: str) -> None:
    """Say something -- on the console *and* in the log.

    ``pythonw.exe`` (what the windowless startup shortcut runs) has neither
    stdout nor stderr, so a plain ``print`` would raise or vanish.  Everything
    the user might want to read later goes through here: the log file always
    keeps a copy.

    The console copy is printed directly instead of relying on the logging
    handler, so it shows up exactly once (the file handler keeps its own).
    """
    logging.getLogger(SAY_LOGGER).info(message)
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(message + "\n")
        stream.flush()
    except Exception:  # pragma: no cover - 管道被关掉之类
        pass


class _NotConsole(logging.Filter):
    """让 ``say()`` 的记录只进文件，不在控制台上打印两遍。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != SAY_LOGGER


def setup_logging(log_file: str | Path | None = None) -> Path | None:
    """Send the log to a file as well as to the console.

    A windowless bridge has no console at all: if it dies, the reason is gone.
    With ``--log-file`` every line lands in the file -- the startup banner
    included -- so "为什么没有通知" is answerable after the fact.

    The file is rotated so it cannot grow without bound.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")

    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is not None:
        console = logging.StreamHandler(stream)
        console.setFormatter(formatter)
        console.addFilter(_NotConsole())
        root.addHandler(console)

    if not log_file:
        return None
    path = Path(log_file)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    logger.info("日志文件：%s", path)
    return path


def port_taken(port: int, timeout: float = 1.0) -> bool:
    """Is *anything* listening on that port?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def bridge_running(port: int, timeout: float = 1.0) -> bool:
    """Is *our* bridge already listening on that port?"""
    data = _http_get_json(f"http://127.0.0.1:{port}/health", timeout)
    return data.get("service") == "nga-notify-bridge"


def _notifier_status(notifier: Notifier) -> tuple[bool, str]:
    """Can this backend raise a notification, and if not, why not?

    The two failure modes need different wording.  A backend whose *tool* is
    missing knows why (``osascript`` / ``notify-send`` not found).  But when
    the platform backend could not even be imported, the factory hands back the
    log fallback -- which has neither ``available`` nor ``error``, so reading
    ``error`` would print ``None`` and tell the user nothing.
    """
    if isinstance(notifier, LogNotifier):
        return False, "通知后端模块没加载起来（只拿到日志兜底）——查一下依赖装好没有"
    if getattr(notifier, "available", False):
        return True, ""
    error = getattr(notifier, "error", None)
    return False, str(error) if error else "原因不明"


def run_checks(args, token: str) -> int:
    """Diagnose the whole notification chain without starting anything.

    The chain has four links and it used to break silently: the bridge is a
    foreground process on the host, so after a reboot (or when its console
    window was closed) the container kept posting into the void and the
    notifications simply landed in the log.

    Returns a non-zero exit code when something is wrong, so this doubles as
    a check in a script.
    """
    problems = 0
    say("NGA 提醒助手 · 通知链路自检")
    say("-" * 52)

    # 1. 系统通知后端（桥接自己能不能弹窗）
    notifier = build_native_notifier("NGA Reminder")
    kind = type(notifier).__name__.replace("Notifier", "")
    ok, reason = _notifier_status(notifier)
    if ok:
        say(f"[ ok ] 系统通知后端可用（{kind}）")
    else:
        problems += 1
        label = f"（{kind}）" if kind != "Log" else ""
        say(f"[坏  ] 系统通知后端不可用{label}：{reason}")

    # 2. 桥接进程在不在
    base = f"http://127.0.0.1:{args.port}"
    if bridge_running(args.port):
        say(f"[ ok ] 通知桥接已在运行（{base}）")
    else:
        problems += 1
        say(f"[坏  ] 通知桥接没在运行（{base} 无响应）")
        say("       起它：uv run python scripts/notify_bridge.py")
        say("       它是前台进程——关掉那个窗口就会停（本项目不装任何开机自启）")
        say("       想让它开机就在：把 scripts\\notify-bridge-startup.lnk 拖进")
        say("       shell:startup 文件夹（自己拖，本项目不替你装）")

    # 3. 共享口令：两端都要有，而且要是同一个值
    container_token = (
        os.environ.get("NGA_NOTIFY_WEBHOOK_TOKEN", "")
        or _token_from_dotenv(ROOT / ".env", "NGA_NOTIFY_WEBHOOK_TOKEN")
    )
    if not token:
        problems += 1
        say("[坏  ] 桥接侧没有共享口令：同一局域网里的任何人都能往这个端口发通知")
        say("       在项目根目录的 .env 里写 NGA_BRIDGE_TOKEN")
    elif not container_token:
        problems += 1
        say("[坏  ] 容器侧没有 NGA_NOTIFY_WEBHOOK_TOKEN：桥接会拒绝容器的请求（401）")
    elif token != container_token:
        problems += 1
        say("[坏  ] 两侧共享口令不一致：.env 里的 NGA_BRIDGE_TOKEN 与")
        say("       NGA_NOTIFY_WEBHOOK_TOKEN 必须是同一个值")
    else:
        say("[ ok ] 两侧共享口令已设置且一致")

    # 4. 容器侧指向哪里、能不能连上
    app = args.app_url.rstrip("/")
    status = _http_get_json(f"{app}/api/status")
    notify = status.get("notify") if isinstance(status, dict) else None
    if not isinstance(notify, dict):
        problems += 1
        say(f"[坏  ] 连不上本机应用（{app}）——它可能还没跑起来")
    else:
        say(f"       容器侧通知后端：{notify.get('backend')}")
        url = str(notify.get("webhook_url") or "")
        if not url:
            problems += 1
            say("[坏  ] 容器侧没有配置 NGA_NOTIFY_WEBHOOK：通知只会落进日志")
            say("       在 docker-compose.yml 里补上：")
            say(f"         NGA_NOTIFY_WEBHOOK: \"http://host.docker.internal:{args.port}/notify\"")
        else:
            say(f"[ ok ] 容器侧指向 {url}")
        if notify.get("reachable") is False:
            problems += 1
            say(f"[坏  ] 容器那边连不上桥接：{notify.get('error')}")

    say("-" * 52)
    if problems:
        say(f"发现 {problems} 个问题——按上面的提示处理，桌面通知就会恢复。")
    else:
        say("通知链路完好：桌面通知可以正常弹出。")
    return 1 if problems else 0


def _log_file_from_argv(argv: list[str]) -> Path | None:
    """从命令行里抠出 ``--log-file``。

    崩溃可能发生在参数解析之前（导入就炸了之类），那时候还不知道日志文件
    在哪——但命令行里写着，先按它落盘，总比什么都没有强。
    """
    for index, arg in enumerate(argv):
        if arg == "--log-file" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if arg.startswith("--log-file="):
            return Path(arg.split("=", 1)[1])
    return None


def _dump_crash(detail: str, log_file: Path | None) -> None:
    """把崩溃现场写到日志文件（无窗口模式下屏幕上什么都没有）。"""
    if log_file is not None:
        try:
            if not log_file.is_absolute():
                log_file = ROOT / log_file
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(detail)
            return
        except Exception:  # pragma: no cover - 落盘都失败就只剩屏幕了
            pass
    with contextlib.suppress(Exception):  # pythonw 下 stderr 是 None
        sys.stderr.write(detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NGA Reminder 通知桥接（宿主机侧）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，容器要能访问）")
    parser.add_argument("--port", type=int, default=8766, help="监听端口（默认 8766）")
    parser.add_argument(
        "--token",
        default="",
        help="共享口令；不给就用环境变量 NGA_BRIDGE_TOKEN，再不给就读项目根目录的 .env",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做自检（系统通知后端 / 桥接进程 / 两侧口令 / 容器指向），不启动服务",
    )
    parser.add_argument(
        "--app-url",
        default="http://127.0.0.1:8765",
        help="本机应用地址，自检时用来问它「容器侧指向哪里」（默认 http://127.0.0.1:8765）",
    )
    parser.add_argument(
        "--log-file",
        default="",
        help=(
            "把日志写到这个文件（相对路径按项目根目录算）。"
            "无窗口模式必须给：pythonw 没有 stdout/stderr，出了事不留痕迹"
        ),
    )
    args = parser.parse_args(argv)

    setup_logging(args.log_file or None)

    token = (
        args.token
        or os.environ.get("NGA_BRIDGE_TOKEN", "")
        or _token_from_dotenv(ROOT / ".env")
    )

    if args.check:
        return run_checks(args, token)

    # 已经有一个桥接在跑就别再起一个：开机自启和手动启动撞车很正常，
    # 撞车的后果不该是「端口被占用」的一串红字，而是一句「已经在跑了」。
    if port_taken(args.port):
        if bridge_running(args.port):
            say(f"通知桥接已经在运行（127.0.0.1:{args.port}）——不用再起一个。")
            return 0
        say(f"端口 {args.port} 被别的程序占着，桥接起不来。")
        say("换个端口：--port 8767，并同步改容器里的 NGA_NOTIFY_WEBHOOK。")
        return 1

    notifier = build_native_notifier("NGA Reminder")
    ok, reason = _notifier_status(notifier)
    if not ok:
        say(f"系统通知不可用：{reason}")
        say("桥接要跑在有桌面通知的宿主机上（Windows / macOS / Linux 桌面）。")
        return 1

    BridgeHandler.notifier = notifier
    BridgeHandler.token = token

    try:
        server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    except OSError as exc:
        say(f"端口 {args.port} 起不来：{exc}")
        say("八成是已经有另一个桥接在跑了——再跑一次 `--check` 看看。")
        return 1
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    say(f"通知桥接已启动：http://{shown}:{args.port}/notify")
    say(f"容器侧设置：NGA_NOTIFY_WEBHOOK=http://host.docker.internal:{args.port}/notify")
    if token:
        say("已启用共享口令校验。")
    else:
        say("⚠ 没有设共享口令：同一局域网里的任何人都能往这个端口发通知。")
        say("  建议在项目根目录的 .env 里写 NGA_BRIDGE_TOKEN，")
        say("  并在容器里用 NGA_NOTIFY_WEBHOOK_TOKEN 传同一个值。")
    say("这是前台进程：关掉这个窗口通知就会停（本项目不装任何开机自启）。")
    say("随时可以另开一个窗口跑 `uv run python scripts/notify_bridge.py --check` 自检。")
    say("按 Ctrl+C 停止。")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # pragma: no cover - 兜底：崩了也要留下痕迹
        import traceback

        _dump_crash(traceback.format_exc(), _log_file_from_argv(sys.argv[1:]))
        sys.exit(1)
