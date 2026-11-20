<#
生成「开机自启」快捷方式（.lnk），指向 pythonw.exe 跑通知桥接。

为什么要这个：
    通知桥接是**宿主上的前台进程**——容器里弹不了桌面通知，弹窗由它代发。
    它不跟着容器启动，于是重启之后桌面通知就静悄悄地不来了。
    开机自启是最直接的解法，但本项目**不替你装任何自启**（不写注册表、不建计划任务）：
    脚本只负责把快捷方式**生成出来**，放到哪儿由你决定。

    典型用法（拖一次即可）：
        1) 运行本脚本，生成 scripts\notify-bridge-startup.lnk
        2) Win+R → 输入 shell:startup → 回车
        3) 把 .lnk 拖进那个文件夹

    为什么用 pythonw.exe：
        它是 python.exe 的「无窗口」版本。开机自启时弹一个黑窗口出来既难看
        也容易被当成异常关掉——桥接就这么没了。pythonw 没有窗口，
        所以日志必须写文件（--log-file），出问题才查得到。

    想撤掉：把那个 .lnk 从启动文件夹里删掉即可，本项目不留任何痕迹。
#>
[CmdletBinding()]
param(
    # 输出路径；默认生成到 scripts\notify-bridge-startup.lnk
    [string]$OutputPath,

    # 监听端口（要和容器里的 NGA_NOTIFY_WEBHOOK 对得上）
    [int]$Port = 8766,

    # 日志文件；默认写到项目根的 data\notify-bridge.log
    [string]$LogFile
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot 'notify-bridge-startup.lnk'
}
if (-not $LogFile) {
    $LogFile = Join-Path $root 'data\notify-bridge.log'
}

# pythonw：优先用项目自己的虚拟环境，其次用系统 PATH 上的。
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "找不到 pythonw.exe。先在项目根目录跑一次 uv sync，让 .venv 建起来。"
    }
    $pythonw = $found.Source
}

$bridge = Join-Path $root 'scripts\notify_bridge.py'
if (-not (Test-Path $bridge)) {
    throw "找不到桥接脚本：$bridge"
}

$logDir = Split-Path -Parent $LogFile
if ($logDir -and -not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($OutputPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"{0}" --port {1} --log-file "{2}"' -f $bridge, $Port, $LogFile
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7   # 7 = 最小化；pythonw 本来就没有窗口
$shortcut.Description = 'NGA 提醒助手 · 通知桥接（开机自启）'
$shortcut.IconLocation = '{0},0' -f $pythonw
$shortcut.Save()

Write-Host "已生成快捷方式：$OutputPath"
Write-Host "  目标：$pythonw"
Write-Host "  参数：$($shortcut.Arguments)"
Write-Host "  工作目录：$root"
Write-Host "  日志：$LogFile"
Write-Host ""
Write-Host "接下来（这一步由你自己做，本项目不替你装任何自启）："
Write-Host "  1) Win+R，输入 shell:startup，回车"
Write-Host "  2) 把上面这个 .lnk 拖进去"
Write-Host ""
Write-Host "想撤掉：把那个 .lnk 从启动文件夹里删掉就行。"
