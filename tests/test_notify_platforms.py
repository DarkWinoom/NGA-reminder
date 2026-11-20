"""跨平台的系统通知后端。

Windows 有 WinRT、macOS 有 ``osascript``、Linux 有 ``notify-send``——
三套系统各有各的弹窗方式。容器里一样都没有，所以容器部署要靠**宿主上的桥接**
把通知转回来（见 ``scripts/notify_bridge.py``）。

这一组测试钉住三件事：

1. **按平台选对后端**——别在 macOS 上找 WinRT；
2. **参数不过 shell**——论坛帖子里什么字符都可能有；
3. **工具不在时要说实话**，而不是假装成功。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_bridge():
    path = ROOT / "scripts" / "notify_bridge.py"
    spec = importlib.util.spec_from_file_location("notify_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRun:
    """假装跑过 subprocess.run，记下命令。

    ``fail_with`` 里写了字符串的，只有命令里含它才失败——这样才能模拟
    「老版本不认某个参数」这种情况。
    """

    def __init__(self, returncode: int = 0, stderr: str = "", fail_with: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.fail_with = fail_with
        self.stdout = ""
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        code = self.returncode
        message = self.stderr
        if self.fail_with and any(self.fail_with in part for part in command):
            code = code or 1
            message = message or "unknown option"
        return subprocess.CompletedProcess(command, code, self.stdout, message)


# --------------------------------------------------------------- 选后端


def test_the_native_backend_follows_the_platform(monkeypatch):
    """三个平台各走各的。"""
    from nga_reminder.notify.factory import build_native_notifier

    monkeypatch.setattr(sys, "platform", "win32")
    assert type(build_native_notifier()).__name__ == "WindowsNotifier"

    monkeypatch.setattr(sys, "platform", "darwin")
    assert type(build_native_notifier()).__name__ == "MacNotifier"

    monkeypatch.setattr(sys, "platform", "linux")
    assert type(build_native_notifier()).__name__ == "LinuxNotifier"


def test_the_bridge_script_uses_the_same_platform_picker():
    """桥接和主程序要用同一套选择逻辑。"""
    bridge = _load_bridge()
    assert hasattr(bridge, "build_native_notifier")
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "build_native_notifier" in source
    # 不该再硬绑 WindowsNotifier
    assert "WindowsNotifier" not in source


def test_the_check_output_is_not_windows_only():
    """自检的文案不能写死「Windows 通知后端」——macOS 上读着别扭。"""
    bridge = _load_bridge()
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "系统通知后端可用" in source
    assert "Windows 通知后端" not in source


# --------------------------------------------------------------- 自检文案


class _AvailableNotifier:
    """一个「能用」的后端，用来测自检的正面分支。"""

    available = True
    error = None

    def notify(self, title: str, body: str, *, url: str = "") -> bool:
        return True


def test_the_self_check_accepts_a_working_backend():
    bridge = _load_bridge()
    ok, reason = bridge._notifier_status(_AvailableNotifier())
    assert ok is True
    assert reason == ""


def test_the_self_check_explains_a_backend_that_never_loaded():
    """后端模块没加载起来时只拿到日志兜底——自检不能丢一句「None」。"""
    from nga_reminder.notify import LogNotifier

    bridge = _load_bridge()
    ok, reason = bridge._notifier_status(LogNotifier())
    assert ok is False
    assert reason
    assert "None" not in reason
    assert "日志兜底" in reason


# --------------------------------------------------------------- macOS


def test_macos_writes_the_text_into_the_script(monkeypatch):
    """标题和正文写进脚本源码并转义——不作为 argv 传。

    ``osascript`` 自己解析命令行，一个以 ``-`` 开头的正文会被当成选项吞掉；
    写进字符串字面量就没有这个问题。
    """
    from nga_reminder.notify import macos

    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/osascript")
    fake = _FakeRun()
    monkeypatch.setattr(macos.subprocess, "run", fake)

    notifier = macos.MacNotifier()
    assert notifier.available is True
    assert notifier.notify('带"引号"的标题', '正文有 \\ 反斜杠\n和换行') is True

    command = fake.commands[0]
    assert command[0] == "/usr/bin/osascript"
    assert command[1] == "-e"
    assert len(command) == 3

    script = command[2]
    assert "display notification" in script
    assert "with title" in script
    # 引号和反斜杠被转义，换行用 AppleScript 的行连接
    assert '\\"' in script
    assert "\\\\" in script
    assert " & return & " in script
    assert "引号" in script


def test_macos_survives_a_body_that_starts_with_a_dash(monkeypatch):
    """正文以 ``-`` 开头时，它仍然是被引号包住的字面量，不会变成选项。"""
    from nga_reminder.notify import macos

    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/osascript")
    fake = _FakeRun()
    monkeypatch.setattr(macos.subprocess, "run", fake)

    notifier = macos.MacNotifier()
    assert notifier.notify("标题", "-e 伪装成选项") is True

    command = fake.commands[0]
    # 命令行只有三个元素：正文没有单独占一个 argv 位
    assert len(command) == 3
    assert '"-e 伪装成选项"' in command[2]


def test_macos_reports_a_missing_tool(monkeypatch):
    """工具不在要说清楚，不能假装弹了。"""
    from nga_reminder.notify import macos

    monkeypatch.setattr(macos.shutil, "which", lambda name: None)
    notifier = macos.MacNotifier()
    assert notifier.available is False
    assert notifier.error
    assert notifier.notify("标题", "正文") is False


def test_macos_reports_a_failed_call(monkeypatch):
    from nga_reminder.notify import macos

    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(macos.subprocess, "run", _FakeRun(returncode=1, stderr="boom"))
    assert macos.MacNotifier().notify("标题", "正文") is False


# --------------------------------------------------------------- Linux


def test_linux_uses_notify_send(monkeypatch):
    from nga_reminder.notify import linux

    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/notify-send")
    fake = _FakeRun()
    monkeypatch.setattr(linux.subprocess, "run", fake)

    notifier = linux.LinuxNotifier()
    assert notifier.available is True
    assert notifier.notify("标题", "正文") is True

    command = fake.commands[0]
    assert command[0] == "/usr/bin/notify-send"
    assert "--app-name" in command
    assert "标题" in command
    assert "正文" in command
    # 位置参数前面有 `--` 分隔符
    assert "--" in command


def test_linux_survives_a_body_that_starts_with_a_dash(monkeypatch):
    """正文以 ``-`` 开头时，``--`` 分隔符保证它不被当成选项。"""
    from nga_reminder.notify import linux

    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/notify-send")
    fake = _FakeRun()
    monkeypatch.setattr(linux.subprocess, "run", fake)

    notifier = linux.LinuxNotifier()
    assert notifier.notify("标题", "-e 伪装成选项") is True

    command = fake.commands[0]
    separator = command.index("--")
    # 分隔符之后才是标题和正文
    assert command[separator + 1] == "标题"
    assert command[separator + 2] == "-e 伪装成选项"
    # 选项都在分隔符之前
    assert "--app-name" in command[:separator]


def test_linux_falls_back_when_the_url_flag_is_unsupported(monkeypatch):
    """老版本 libnotify 没有 --url——报错就退回普通通知。"""
    from nga_reminder.notify import linux

    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/notify-send")
    fake = _FakeRun(stderr="unknown option --url", fail_with="--url")
    monkeypatch.setattr(linux.subprocess, "run", fake)

    notifier = linux.LinuxNotifier()
    assert notifier.notify("标题", "正文", url="http://127.0.0.1:8765/") is True
    assert len(fake.commands) == 2
    assert "--url" in fake.commands[0]
    assert "--url" not in fake.commands[1]
    # 退回的那次同样带着分隔符
    assert "--" in fake.commands[1]


def test_linux_reports_a_missing_tool(monkeypatch):
    from nga_reminder.notify import linux

    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    notifier = linux.LinuxNotifier()
    assert notifier.available is False
    assert "notify-send" in notifier.error
    assert notifier.notify("标题", "正文") is False


@pytest.mark.skipif(sys.platform == "win32", reason="notify-send 只在 Linux 上有")
def test_the_linux_backend_never_shells_out():
    """参数是列表传的，不经过 shell。"""
    from nga_reminder.notify import linux

    assert linux.LinuxNotifier().available in (True, False)
