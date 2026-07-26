# ============================================================
# install_autostart.ps1 — 注册 Windows 计划任务：开机自启 + 崩溃自拉起
# ============================================================
# 用法（在仓库任意位置的 PowerShell 中执行）:
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1            # 安装
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Status    # 查看状态
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall # 卸载
#
# 行为:
#   - 登录时自动启动 python main.py（工作目录 = 仓库根目录）
#   - 进程异常退出（非零退出码/崩溃）时 1 分钟后自动重启，最多连续重试 10 次
#   - 以 S4U 方式在后台运行：不弹出控制台窗口，日志照常写 logs/ 目录
#   - 网页管理端照常可用（http://127.0.0.1:8787/），要停程序用页面重启旁的方式
#     或在此脚本 -Uninstall 后用任务管理器结束 python 进程
# ============================================================
param(
    [switch]$Uninstall,
    [switch]$Status
)

$TaskName = "NogizakaMessagePush"
$RepoDir  = Split-Path -Parent $PSScriptRoot

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "未安装（任务 $TaskName 不存在）"
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "任务:     $TaskName ($($task.State))"
        Write-Host "上次运行: $($info.LastRunTime)  结果: $($info.LastTaskResult)"
        Write-Host "下次运行: $($info.NextRunTime)"
    }
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "任务 $TaskName 不存在，无需卸载"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "✅ 已卸载计划任务 $TaskName（正在运行的进程不受影响）"
    }
    exit 0
}

# ── 安装 ────────────────────────────────────────────────
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Error "找不到 python，请确认已加入 PATH"
    exit 1
}
if (-not (Test-Path (Join-Path $RepoDir "main.py"))) {
    Write-Error "仓库根目录下没有 main.py（推断路径: $RepoDir）"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python -Argument "main.py" -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# S4U：后台运行、不弹控制台窗口、无需保存密码
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
} catch {
    Write-Error ("注册失败: $_`n如提示权限不足，请用管理员 PowerShell 重试。")
    exit 1
}

Write-Host "✅ 已注册计划任务 $TaskName"
Write-Host "   - 登录时自动启动，崩溃后 1 分钟自动重启（最多连续 10 次）"
Write-Host "   - 后台运行无窗口；管理入口: http://127.0.0.1:8787/"
Write-Host ""
$reply = Read-Host "现在就启动一次吗？(y/N)"
if ($reply -eq "y") {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已启动。稍候可访问 http://127.0.0.1:8787/ 查看状态"
}
