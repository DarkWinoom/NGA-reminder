"""桥接的「无人值守」形态。

桥接是宿主上的前台进程——容器里弹不了桌面通知，弹窗由它代发。这个进程
不跟着容器启动，于是重启之后桌面通知就静悄悄地不来了。

这一组测试钉住三件事：

1. 桥接能**不靠窗口**跑起来，日志写文件（pythonw 没有 stdout/stderr，
   崩了不留痕迹的话，就永远查不出「为什么没有通知」）；
2. 已经有一个桥接在跑时，再起一个要**安静退出**，而不是甩一串红字；
3. 开机自启的快捷方式由脚本**生成**，但安装这一步由用户自己做——
   本项目不写注册表、不建计划任务。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_bridge():
    path = ROOT / "scripts" / "notify_bridge.py"
    spec = importlib.util.spec_from_file_location("notify_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_root_logger():
    """``setup_logging`` 会往 root logger 上挂 handler——测完要还原。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    yield root
    for handler in list(root.handlers):
        if handler not in saved:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)


class _FakeBridge(BaseHTTPRequestHandler):
    """占着端口的「假桥接」，用来测重复启动。"""

    service = "nga-notify-bridge"

    def do_GET(self):  # noqa: N802 - http.server 的命名约定
        body = json.dumps({"ok": True, "service": self.service}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102 - 别刷测试输出
        pass


@pytest.fixture
def fake_bridge():
    """起一个假桥接，返回 (端口, 停止函数)。"""

    def _start(service: str = "nga-notify-bridge"):
        _FakeBridge.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    servers = []

    def _make(service: str = "nga-notify-bridge"):
        server, port = _start(service)
        servers.append(server)
        return port

    yield _make
    for server in servers:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------- 日志落盘


def test_the_bridge_can_write_its_log_to_a_file(tmp_path, clean_root_logger):
    """无窗口模式的关键：日志要落盘。"""
    bridge = _load_bridge()
    log = tmp_path / "logs" / "bridge.log"
    path = bridge.setup_logging(log)
    assert path == log
    bridge.say("通知桥接已启动")
    logging.getLogger("nga_reminder.bridge").info("一条普通日志")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log.read_text(encoding="utf-8")
    assert "通知桥接已启动" in text
    assert "一条普通日志" in text


def test_a_relative_log_path_is_resolved_against_the_project_root(tmp_path, clean_root_logger):
    """快捷方式里写的是相对路径——从哪儿启动都得落到同一个地方。"""
    bridge = _load_bridge()
    path = bridge.setup_logging("data/test-bridge.log")
    assert path == ROOT / "data" / "test-bridge.log"
    assert path.parent.is_dir()


def test_the_bridge_survives_a_windowless_start(tmp_path):
    """真的把桥接拉起来（pythonw 那种没有控制台的环境）。"""
    bridge = _load_bridge()
    if not bridge.build_native_notifier("NGA Reminder").available:
        pytest.skip("本机没有可用的系统通知后端")
    log = tmp_path / "bridge.log"
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "notify_bridge.py"),
            "--port",
            str(port),
            "--log-file",
            str(log),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        alive = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    alive = True
                    break
            except OSError:
                time.sleep(0.2)
        assert alive, "桥接没能在无窗口模式下起来"
        for handler in []:
            handler.close()
        deadline = time.time() + 5
        text = ""
        while time.time() < deadline:
            if log.exists():
                text = log.read_text(encoding="utf-8")
                if "通知桥接已启动" in text:
                    break
            time.sleep(0.2)
        assert "通知桥接已启动" in text
        assert "容器侧设置" in text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            proc.kill()


def test_a_crash_before_argument_parsing_still_leaves_a_trace(tmp_path):
    """崩在参数解析之前也得有地方写——否则无窗口模式等于什么都没留下。"""
    bridge = _load_bridge()
    log = tmp_path / "crash.log"
    bridge._dump_crash("boom\n", log)
    assert "boom" in log.read_text(encoding="utf-8")


# ------------------------------------------------------------- 重复启动


def test_starting_the_bridge_twice_exits_quietly(fake_bridge, clean_root_logger):
    """已经有一个在跑，就安静退出——不该甩一串红字。"""
    bridge = _load_bridge()
    port = fake_bridge()
    assert bridge.main(["--port", str(port)]) == 0


def test_the_bridge_refuses_to_start_on_someone_elses_port(fake_bridge, clean_root_logger):
    """端口被别的程序占着，要说清楚，而不是硬抢。"""
    bridge = _load_bridge()
    port = fake_bridge(service="some-other-service")
    assert bridge.main(["--port", str(port)]) == 1


def test_the_bridge_recognises_its_own_health_endpoint(fake_bridge):
    """只有「像自己的」才算已经在跑。"""
    bridge = _load_bridge()
    assert bridge.bridge_running(fake_bridge()) is True
    assert bridge.bridge_running(fake_bridge(service="other")) is False


def test_an_unused_port_is_reported_as_free():
    bridge = _load_bridge()
    assert bridge.port_taken(_free_port()) is False


# ------------------------------------------------------------- 开机自启


def test_a_startup_shortcut_generator_exists():
    """生成开机自启快捷方式的脚本要在。"""
    path = ROOT / "scripts" / "make_startup_shortcut.ps1"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    # 无窗口（pythonw）+ 日志写文件 + 告诉用户自己拖
    assert "pythonw" in text
    assert "--log-file" in text
    assert "shell:startup" in text


def test_the_generator_never_installs_anything_by_itself():
    """生成 ≠ 安装。脚本不许碰注册表、计划任务、也不许把自己复制进启动文件夹。

    注意：脚本里**提到** ``shell:startup`` 没问题——那是给用户看的指引；
    要禁的是「它自己去装」的那些动作。
    """
    text = (ROOT / "scripts" / "make_startup_shortcut.ps1").read_text(encoding="utf-8")
    for forbidden in (
        "schtasks",
        "reg add",
        "reg.exe",
        "CurrentVersion\\Run",
        "Start Menu",
        "Copy-Item",
        "Move-Item",
        "robocopy",
        "xcopy",
    ):
        assert forbidden not in text, forbidden


def test_the_bridge_never_installs_an_autostart_by_itself():
    """桥接脚本也不许偷偷装自启——这是本项目一直守着的线。"""
    text = (ROOT / "scripts" / "notify_bridge.py").read_text(encoding="utf-8")
    for forbidden in (
        "schtasks",
        "reg add",
        "reg.exe",
        "CurrentVersion\\Run",
        "Start Menu",
        "shutil.copy",
    ):
        assert forbidden not in text, forbidden


def test_the_generated_shortcut_points_at_pythonw_with_a_log_file():
    """生成出来的 .lnk 得真的能用：无窗口 + 日志写文件。"""
    link = ROOT / "scripts" / "notify-bridge-startup.lnk"
    if not link.exists():
        pytest.skip("还没生成快捷方式（跑一下 scripts/make_startup_shortcut.ps1）")
    raw = link.read_bytes()
    # .lnk 里的路径是 UTF-16LE 存的
    assert "pythonw.exe".encode("utf-16-le") in raw
    assert "--log-file".encode("utf-16-le") in raw
    assert "notify_bridge.py".encode("utf-16-le") in raw
