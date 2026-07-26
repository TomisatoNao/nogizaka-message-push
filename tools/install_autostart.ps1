# ============================================================
# install_autostart.ps1 — 注册 Windows 计划任务：开机自启 + 崩溃自拉起
# ============================================================
# ⚠️ 本文件必须保存为 【UTF-8 with BOM】：
#    Windows PowerShell 5.1 会用系统 ANSI 代码页读取无 BOM 的 .ps1，
#    中文注释会变成乱码并导致语法错误。修改本文件后请确认 BOM 仍在。
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
    [switch]$Status,
    [switch]$Start,     # 安装后立即启动，不做交互询问（自动化 / 非交互终端用）
    [switch]$Stop       # 优雅停止服务（不需要管理员权限）
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

if ($Stop) {
    # 用信号文件让主程序自己优雅退出 —— 计划任务启动的进程通常需要
    # 管理员权限才能强杀，走信号就绕开了权限问题，也保证清理流程走完。
    $logDir = Join-Path $RepoDir "logs"
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
    Set-Content -Path (Join-Path $logDir "service.stop") -Value "stop" -Encoding utf8
    Write-Host "已发送停止信号，等待主程序退出…"

    $stopped = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 1500
        if (-not (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue)) {
            $stopped = $true
            break
        }
    }
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    if ($stopped) {
        Write-Host "✅ 服务已停止"
    } else {
        Write-Warning "等待超时。若仍在运行，请用管理员 PowerShell 执行：taskkill /F /IM python.exe"
    }
    exit 0
}

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "任务 $TaskName 不存在，无需卸载"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "✅ 已卸载计划任务 $TaskName"
        Write-Host "   正在运行的进程不受影响，如需停止请先执行： -Stop"
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

# 调用守护脚本 run_service.ps1：崩溃自拉起由它负责，不依赖 Task Scheduler
# 的"失败后重启"策略（那个策略捕捉不到"子进程被杀但包装器正常退出"）。
# 用 PowerShell 包装还解决了两个问题：直接跑 python.exe 会弹控制台窗口，
# 而 pythonw.exe 下 sys.stdout 为 None 会让程序的 print 抛异常。
$runner = Join-Path $RepoDir "tools\run_service.ps1"
if (-not (Test-Path $runner)) {
    Write-Error "缺少守护脚本: $runner"
    exit 1
}
$wrapper = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $wrapper -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# S4U 可在未登录时也运行，但需要管理员权限授予"作为批处理作业登录"；
# 失败时降级为 Interactive（仅当前用户登录后运行，普通权限即可注册）
$registered = $false
foreach ($logon in @("S4U", "Interactive")) {
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType $logon -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
        $registered = $true
        if ($logon -eq "Interactive") {
            Write-Host "ℹ️ 以 Interactive 方式注册（普通权限）：登录后自动运行。"
            Write-Host "   若想未登录也保持运行，请用管理员 PowerShell 重新执行本脚本。"
        }
        break
    } catch {
        if ($logon -eq "Interactive") {
            Write-Error "注册失败: $($_.Exception.Message)`n请尝试用管理员身份运行 PowerShell 后重试。"
            exit 1
        }
    }
}
if (-not $registered) { exit 1 }

Write-Host "✅ 已注册计划任务 $TaskName"
Write-Host "   - 登录时自动启动，崩溃后 60s 自动拉起（由 tools\run_service.ps1 守护）"
Write-Host "   - 后台运行无窗口；守护日志: logs\service.log"
Write-Host "   - 管理入口: http://127.0.0.1:8787/"
Write-Host ""

# 非交互环境（管道 / 自动化）下不询问，避免卡住
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
if ($Start) {
    $reply = "y"
} elseif ($interactive) {
    $reply = Read-Host "现在就启动一次吗？(y/N)"
} else {
    $reply = "n"
    Write-Host "（非交互模式：未自动启动，加 -Start 可在安装后立即启动）"
}

if ($reply -eq "y") {
    $inUse = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
    if ($inUse) {
        Write-Warning "8787 端口已被占用（可能已有一个实例在跑）。请先结束它，再执行：Start-ScheduledTask -TaskName $TaskName"
    } else {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "已启动。稍候可访问 http://127.0.0.1:8787/ 查看状态"
    }
}
