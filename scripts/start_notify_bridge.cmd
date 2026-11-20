@echo off
rem 启动 NGA 提醒助手的通知桥接（宿主机侧）。
rem
rem 容器里弹不了桌面通知——真正的弹窗由这个宿主进程代发：
rem 容器把通知 POST 给它，它再弹成真正的系统通知
rem （Windows 用 WinRT toast，macOS 用 osascript，Linux 用 notify-send）。
rem
rem 双击本文件即可启动。关掉这个黑窗口，桥接就停了
rem （本项目不装任何开机自启——想让它开机就在的，自己把这个文件
rem   复制到「shell:startup」里去，那是你自己的决定）。
rem
rem 想确认整条链路通不通，另开一个窗口跑：
rem   uv run python scripts/notify_bridge.py --check

setlocal
cd /d "%~dp0.."

where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo 找不到 uv。先装一个：https://docs.astral.sh/uv/
  echo.
  pause
  exit /b 1
)

echo 正在启动通知桥接...  按 Ctrl+C 停止。
echo.
uv run python scripts\notify_bridge.py

echo.
echo 桥接退出了。想查原因就跑：
echo   uv run python scripts\notify_bridge.py --check
echo.
pause
